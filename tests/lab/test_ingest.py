"""D9 ingest refusals and chunk handling (TBX-LAB-005 to -009)."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pysam
import pytest

from traceback_lab import ingest
from traceback_lab.problems import LabProblem
from traceback_lab.store import LabStore
from traceback_lab.synthetic import rg_line, write_chunks, write_reference


def _problem(callable_, *args, **kwargs) -> LabProblem:
    with pytest.raises(LabProblem) as caught:
        callable_(*args, **kwargs)
    return caught.value


def test_chunks_are_ordered_by_index_then_name(tmp_path: Path) -> None:
    paths = [tmp_path / name for name in ("x_10.bam", "x_2.bam", "x_1.bam", "notes.bam")]
    assert [path.name for path in sorted(paths, key=ingest._order_key)] == [
        "x_1.bam",
        "x_2.bam",
        "x_10.bam",
        "notes.bam",
    ]


def test_symlinked_chunk_and_symlinked_dir_entry_are_refused(lab_world) -> None:
    chunks = lab_world["chunks"]
    link = lab_world["tmp"] / "link.bam"
    link.symlink_to(chunks / "SYNTH0001_pass_synthetic_0.bam")
    problem = _problem(ingest.collect_paths, [str(link)])
    assert problem.code == "TBX-LAB-005" and problem.exit_code == 3
    assert problem.data == {"argument": 1, "kind": "a symlink"}
    (chunks / "extra_9.bam").symlink_to(chunks / "SYNTH0001_pass_synthetic_1.bam")
    assert _problem(ingest.collect_paths, [str(chunks)]).code == "TBX-LAB-005"


def test_non_regular_and_missing_inputs_are_refused(tmp_path: Path) -> None:
    fifo = tmp_path / "pipe_1.bam"
    os.mkfifo(fifo)
    assert _problem(ingest.collect_paths, [str(fifo)]).data["kind"] == "not a regular file"
    assert _problem(ingest.collect_paths, [str(tmp_path / "nope.bam")]).data["kind"] == "missing"
    empty = tmp_path / "empty"
    empty.mkdir()
    assert _problem(ingest.collect_paths, [str(empty)]).code == "TBX-LAB-005"


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads everything")
def test_unreadable_chunk_is_refused(lab_world) -> None:
    target = lab_world["chunks"] / "SYNTH0001_pass_synthetic_0.bam"
    target.chmod(0o000)
    try:
        assert _problem(ingest.collect_paths, [str(target)]).data["kind"] == "unreadable"
    finally:
        target.chmod(0o644)


def test_collect_paths_resolves_absolute_realpaths(lab_world, monkeypatch) -> None:
    monkeypatch.chdir(lab_world["tmp"])
    paths = ingest.collect_paths(["chunks"])
    assert all(path.is_absolute() for path in paths)
    assert len(paths) == 3


def _truncate(path: Path) -> None:
    data = path.read_bytes()
    path.write_bytes(data[: len(data) // 2])


def test_truncated_chunk_blocks_unless_skipped(lab_world) -> None:
    store = LabStore(lab_world["root"])
    store.ensure()
    _truncate(lab_world["chunks"] / "SYNTH0001_pass_synthetic_1.bam")
    paths = ingest.collect_paths([str(lab_world["chunks"])])
    problem = _problem(ingest.inspect_chunks, paths, store)
    assert problem.code == "TBX-LAB-006" and problem.data == {"chunk_indexes": [1]}
    chunkset = ingest.inspect_chunks(paths, store, skip_bad_chunks=True)
    assert [chunk.index for chunk in chunkset.chunks] == [0, 2]
    assert chunkset.excluded_indexes == [1]
    whole = ingest.ChunkSet(chunks=chunkset.chunks)
    assert chunkset.source_id != whole.source_id


def test_duplicate_content_and_duplicate_index_are_refused(lab_world) -> None:
    store = LabStore(lab_world["root"])
    store.ensure()
    chunks = lab_world["chunks"]
    shutil.copyfile(chunks / "SYNTH0001_pass_synthetic_0.bam", chunks / "SYNTH0001_pass_synthetic_7.bam")
    problem = _problem(ingest.inspect_chunks, ingest.collect_paths([str(chunks)]), store)
    assert problem.code == "TBX-LAB-007" and sorted(problem.data["chunk_indexes"]) == [0, 7]
    (chunks / "SYNTH0001_pass_synthetic_7.bam").unlink()
    other = lab_world["tmp"] / "other"
    reference = write_reference(lab_world["tmp"] / "r2.fa", length=5000, contigs=("chr1",))
    write_chunks(other, reference, chunks=1, reads_per_chunk=20, seed=99, stem="ELSE")
    problem = _problem(
        ingest.inspect_chunks,
        ingest.collect_paths([str(chunks / "SYNTH0001_pass_synthetic_0.bam"), str(other / "ELSE_0.bam")]),
        store,
    )
    assert problem.code == "TBX-LAB-007" and problem.data["kind"] == "same_index"


def test_mixed_basecall_models_are_refused(lab_world) -> None:
    store = LabStore(lab_world["root"])
    store.ensure()
    reference = write_reference(lab_world["tmp"] / "r3.fa", length=5000, contigs=("chr1",))
    write_chunks(
        lab_world["chunks"],
        reference,
        chunks=1,
        reads_per_chunk=20,
        seed=5,
        stem="OTHER_pass_model",
        rg=rg_line(run="other", model="dna_r9.4.1_e8_hac@v3.3"),
    )
    (lab_world["chunks"] / "OTHER_pass_model_0.bam").rename(lab_world["chunks"] / "OTHER_pass_model_5.bam")
    problem = _problem(ingest.inspect_chunks, ingest.collect_paths([str(lab_world["chunks"])]), store)
    assert problem.code == "TBX-LAB-008"
    assert set(problem.data["models"]) == {"dna_r10.4.1_e8.2_400bps_sup@v5.0.0", "dna_r9.4.1_e8_hac@v3.3"}


def test_hash_cache_avoids_rehashing(lab_world, monkeypatch) -> None:
    store = LabStore(lab_world["root"])
    store.ensure()
    paths = ingest.collect_paths([str(lab_world["chunks"])])
    first = ingest.inspect_chunks(paths, store)
    cached_file = store.hash_cache_file.read_bytes()
    calls: list[Path] = []
    counts: list[Path] = []
    original_hash, original_count = ingest._sha256_file, ingest.count_records
    monkeypatch.setattr(ingest, "_sha256_file", lambda path: calls.append(path) or original_hash(path))
    monkeypatch.setattr(ingest, "count_records", lambda path, *flags: counts.append(path) or original_count(path, *flags))
    second = ingest.inspect_chunks(paths, store)
    assert calls == [] and counts == []
    assert first.source_id == second.source_id
    assert second.total_records == 1800
    assert store.hash_cache_file.read_bytes() == cached_file
    # --rehash ignores every cached entry, recomputes, and rewrites the same identity.
    third = ingest.inspect_chunks(paths, store, rehash=True)
    assert sorted(calls) == sorted(paths) and sorted(counts) == sorted(paths)
    assert third.source_id == first.source_id
    assert store.hash_cache_file.read_bytes() == cached_file
    # A rehash with write_cache=False (dry run, preflight) also recomputes and writes nothing.
    calls.clear()
    ingest.inspect_chunks(paths, store, rehash=True, write_cache=False)
    assert sorted(calls) == sorted(paths)
    assert store.hash_cache_file.read_bytes() == cached_file


def _rewrite_uncompressed(src: Path, dst: Path, *, flip_first_base: bool) -> None:
    """Write ``src`` as an uncompressed BAM so a one-base edit keeps the byte size."""

    with pysam.AlignmentFile(str(src), "rb", check_sq=False) as inp, pysam.AlignmentFile(
        str(dst), "wbu", header=inp.header
    ) as out:
        for number, record in enumerate(inp):
            if flip_first_base and number == 0:
                sequence = list(record.query_sequence)
                qualities = record.query_qualities
                sequence[0] = "C" if sequence[0] != "C" else "G"
                record.query_sequence = "".join(sequence)
                record.query_qualities = qualities
            out.write(record)


def test_same_size_same_mtime_replacement_on_a_new_inode_is_rehashed(lab_world, monkeypatch) -> None:
    store = LabStore(lab_world["root"])
    store.ensure()
    chunks = lab_world["chunks"]
    target = chunks / "SYNTH0001_pass_synthetic_1.bam"
    staged = lab_world["tmp"] / "uncompressed.bam"
    _rewrite_uncompressed(target, staged, flip_first_base=False)
    os.replace(staged, target)
    paths = ingest.collect_paths([str(chunks)])
    first = ingest.inspect_chunks(paths, store)
    before = target.stat()
    first_sha = next(chunk.sha256 for chunk in first.chunks if chunk.path == target)

    restored = lab_world["tmp"] / "restored.bam"
    _rewrite_uncompressed(target, restored, flip_first_base=True)
    os.utime(restored, ns=(before.st_atime_ns, before.st_mtime_ns))
    os.replace(restored, target)
    after = target.stat()
    assert (after.st_size, after.st_mtime_ns) == (before.st_size, before.st_mtime_ns)
    assert after.st_ino != before.st_ino

    calls: list[Path] = []
    original_hash = ingest._sha256_file
    monkeypatch.setattr(ingest, "_sha256_file", lambda path: calls.append(path) or original_hash(path))
    second = ingest.inspect_chunks(paths, store)
    assert calls == [target]
    second_sha = next(chunk.sha256 for chunk in second.chunks if chunk.path == target)
    assert second_sha != first_sha
    assert second.source_id != first.source_id
    assert second.total_records == first.total_records == 1800
    # The fresh entry is cached under the new key: a third pass recomputes nothing.
    calls.clear()
    assert ingest.inspect_chunks(paths, store).source_id == second.source_id
    assert calls == []


def test_in_place_rewrite_with_restored_mtime_is_rehashed_without_the_flag(lab_world, monkeypatch) -> None:
    store = LabStore(lab_world["root"])
    store.ensure()
    chunks = lab_world["chunks"]
    target = chunks / "SYNTH0001_pass_synthetic_1.bam"
    staged = lab_world["tmp"] / "uncompressed.bam"
    _rewrite_uncompressed(target, staged, flip_first_base=False)
    os.replace(staged, target)
    paths = ingest.collect_paths([str(chunks)])
    first = ingest.inspect_chunks(paths, store)
    before = target.stat()

    # `cp -p` onto an existing chunk: truncate and rewrite the same inode, restore the mtime.
    source = lab_world["tmp"] / "edited.bam"
    _rewrite_uncompressed(target, source, flip_first_base=True)
    target.write_bytes(source.read_bytes())
    os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))
    after = target.stat()
    assert after.st_ino == before.st_ino
    assert (after.st_size, after.st_mtime_ns) == (before.st_size, before.st_mtime_ns)
    assert after.st_ctime_ns != before.st_ctime_ns

    calls: list[Path] = []
    original_hash = ingest._sha256_file
    monkeypatch.setattr(ingest, "_sha256_file", lambda path: calls.append(path) or original_hash(path))
    second = ingest.inspect_chunks(paths, store)
    assert calls == [target]
    assert second.source_id != first.source_id
    calls.clear()
    assert ingest.inspect_chunks(paths, store).source_id == second.source_id
    assert calls == []


def test_rehash_corrects_a_stale_entry_and_overwrites_it(lab_world, monkeypatch) -> None:
    """A cache that lies about current bytes is corrected by one --rehash run."""

    store = LabStore(lab_world["root"])
    store.ensure()
    chunks = lab_world["chunks"]
    target = chunks / "SYNTH0001_pass_synthetic_1.bam"
    staged = lab_world["tmp"] / "uncompressed.bam"
    _rewrite_uncompressed(target, staged, flip_first_base=False)
    os.replace(staged, target)
    paths = ingest.collect_paths([str(chunks)])
    first = ingest.inspect_chunks(paths, store)
    stale_sha = next(chunk.sha256 for chunk in first.chunks if chunk.path == target)

    edited = lab_world["tmp"] / "edited.bam"
    _rewrite_uncompressed(target, edited, flip_first_base=True)
    os.replace(edited, target)
    # Simulate a cache the key cannot see through: the stale digest planted under the current key.
    cache = store.load_hash_cache()
    cache[store.hash_cache_key(target)] = {"sha256": stale_sha, "records": 600}
    store.save_hash_cache(cache)

    calls: list[Path] = []
    original_hash = ingest._sha256_file
    monkeypatch.setattr(ingest, "_sha256_file", lambda path: calls.append(path) or original_hash(path))
    served_stale = ingest.inspect_chunks(paths, store)
    assert calls == []
    assert served_stale.source_id == first.source_id

    corrected = ingest.inspect_chunks(paths, store, rehash=True)
    assert sorted(calls) == sorted(paths)
    assert corrected.source_id != first.source_id
    fresh_sha = next(chunk.sha256 for chunk in corrected.chunks if chunk.path == target)
    assert fresh_sha != stale_sha
    assert store.load_hash_cache()[store.hash_cache_key(target)]["sha256"] == fresh_sha

    calls.clear()
    assert ingest.inspect_chunks(paths, store).source_id == corrected.source_id
    assert calls == []


def test_unknown_cache_schema_is_announced_and_rewritten(lab_world) -> None:
    store = LabStore(lab_world["root"])
    store.ensure()
    paths = ingest.collect_paths([str(lab_world["chunks"])])
    store.hash_cache_file.parent.mkdir(parents=True, exist_ok=True)
    store.hash_cache_file.write_bytes(json.dumps({"schema": "traceback.lab.hash-cache.v9", "entries": {}}).encode())
    lines: list[str] = []
    ingest.inspect_chunks(paths, store, progress=lines.append)
    assert lines[0] == (
        "ingest  hash cache schema 'traceback.lab.hash-cache.v9' is unknown to this checkout: "
        "ignored, every digest is recomputed once"
    )
    assert json.loads(store.hash_cache_file.read_bytes())["schema"] == "traceback.lab.hash-cache.v2"
    lines.clear()
    ingest.inspect_chunks(paths, store, progress=lines.append)
    assert lines == []


def test_cache_blind_filesystem_is_announced_at_ingest(lab_world, monkeypatch) -> None:
    store = LabStore(lab_world["root"])
    store.ensure()
    paths = ingest.collect_paths([str(lab_world["chunks"])])
    monkeypatch.setattr(ingest, "filesystem_type", lambda path: "exfat")
    lines: list[str] = []
    ingest.inspect_chunks(paths, store, progress=lines.append)
    assert lines[0] == (
        "ingest  inputs are on exfat: the hash cache cannot detect an in-place rewrite there; "
        "rerun with --rehash after any timestamp-preserving copy"
    )
    monkeypatch.setattr(ingest, "filesystem_type", lambda path: "apfs")
    lines.clear()
    ingest.inspect_chunks(paths, store, progress=lines.append)
    assert lines == []
    monkeypatch.setattr(ingest, "filesystem_type", lambda path: None)
    ingest.inspect_chunks(paths, store, progress=lines.append)
    assert lines == []


def test_filesystem_type_is_best_effort_and_picks_the_longest_mount(tmp_path: Path) -> None:
    darwin = (
        "/dev/disk3s1s1 on / (apfs, sealed, local, read-only, journaled)\n"
        "devfs on /dev (devfs, local, nobrowse)\n"
        "map auto_home on /System/Volumes/Data/home (autofs, automounted, nobrowse)\n"
        "/dev/disk4s1 on /Volumes/KIT (exfat, local, nodev, nosuid, noowners)\n"
        "//user@nas/share on /Volumes/My Share (smbfs, nodev, nosuid, mounted by user)\n"
    )
    mounts = ingest._mounts_from_darwin(darwin)
    assert ("/Volumes/KIT", "exfat") in mounts and ("/Volumes/My Share", "smbfs") in mounts
    assert ingest._mount_type("/Volumes/KIT/run1/chunk_0.bam", mounts) == "exfat"
    assert ingest._mount_type("/Volumes/KITCHEN/chunk_0.bam", mounts) == "apfs"
    assert ingest._mount_type("/Volumes/My Share/x.bam", mounts) == "smbfs"
    assert ingest._mount_type("/Users/op/x.bam", mounts) == "apfs"
    assert ingest._mount_type("/Volumes/KIT", mounts) == "exfat"
    proc = (
        "/dev/nvme0n1p2 / ext4 rw,relatime 0 0\n"
        "/dev/sdb1 /mnt/kit\\040disk vfat rw,uid=1000 0 0\n"
        "//nas/share /mnt/nas cifs rw 0 0\n"
    )
    mounts = ingest._mounts_from_proc(proc)
    assert ingest._mount_type("/mnt/kit disk/chunk_0.bam", mounts) == "vfat"
    assert ingest._mount_type("/mnt/nas/x.bam", mounts) == "cifs"
    assert ingest._mount_type("/home/op/x.bam", mounts) == "ext4"
    assert ingest._mount_type("/x", []) is None
    live = ingest.filesystem_type(tmp_path)
    assert live is None or (isinstance(live, str) and live == live.lower())
    assert ingest.cache_blind_filesystem(tmp_path) in (None, *ingest.CACHE_BLIND_FILESYSTEMS)


def test_legacy_cache_file_is_announced_once(lab_world) -> None:
    store = LabStore(lab_world["root"])
    store.ensure()
    paths = ingest.collect_paths([str(lab_world["chunks"])])
    entries = {}
    for path in paths:
        info = path.stat()
        entries[f"{os.path.realpath(path)}\0{info.st_size}\0{info.st_mtime_ns}"] = {"sha256": "0" * 64, "records": 1}
    store.hash_cache_file.parent.mkdir(parents=True, exist_ok=True)
    store.hash_cache_file.write_bytes(json.dumps({"schema": "traceback.lab.hash-cache.v1", "entries": entries}).encode())
    lines: list[str] = []
    chunkset = ingest.inspect_chunks(paths, store, progress=lines.append)
    assert lines[0] == "ingest  hash cache written by an older version (3 chunk entries dropped): every chunk is hashed once"
    assert sum(line.startswith("ingest  hashing chunk") for line in lines) == 3
    assert chunkset.total_records == 1800
    assert json.loads(store.hash_cache_file.read_bytes())["schema"] == "traceback.lab.hash-cache.v2"
    lines.clear()
    ingest.inspect_chunks(paths, store, progress=lines.append)
    assert lines == []


def _aligned_header_bam(path: Path, contigs) -> None:
    header = {"HD": {"VN": "1.6", "SO": "coordinate"}, "SQ": [{"SN": name, "LN": length} for name, length in contigs]}
    with pysam.AlignmentFile(str(path), "wb", header=header):
        pass


def test_aligned_input_with_foreign_sq_is_refused(lab_world) -> None:
    store = LabStore(lab_world["root"])
    reference = ingest.resolve_reference(lab_world["root"], None)
    contigs = list(reference.contigs)
    contigs[0] = (contigs[0][0], contigs[0][1] + 1)
    contigs.append(("chrUn_extra", 1000))
    bam = lab_world["tmp"] / "aligned.bam"
    _aligned_header_bam(bam, contigs)
    chunkset = ingest.inspect_chunks(ingest.collect_paths([str(bam)]), store)
    problem = _problem(ingest.check_sq, chunkset.chunks[0], reference)
    assert problem.code == "TBX-LAB-009"
    assert problem.data["total"] == 2
    assert problem.data["differences"][0].startswith("chr1: length")


def test_reference_rules(tmp_path: Path, synthetic_world) -> None:
    from traceback_runner.references import register_reference

    root = tmp_path / "root"
    problem = _problem(ingest.resolve_reference, root, None)
    assert problem.code == "TBX-LAB-004" and problem.exit_code == 3
    register_reference(root, synthetic_world["fasta"], "lab-example")
    # The example's reference is never a default for real input.
    assert _problem(ingest.resolve_reference, root, None).exit_code == 3
    assert ingest.resolve_reference(root, None, example=True).reference_id == "lab-example"
    register_reference(root, synthetic_world["fasta"], "one")
    assert ingest.resolve_reference(root, None).reference_id == "one"
    register_reference(root, synthetic_world["fasta"], "two")
    problem = _problem(ingest.resolve_reference, root, None)
    assert problem.exit_code == 2 and problem.data["registered"] == ["one", "two"]
    assert ingest.resolve_reference(root, "two").reference_id == "two"
    assert _problem(ingest.resolve_reference, root, "three").code == "TBX-LAB-004"


def test_disk_budget_parts(lab_world) -> None:
    store = LabStore(lab_world["root"])
    store.ensure()
    chunkset = ingest.inspect_chunks(ingest.collect_paths([str(lab_world["chunks"])]), store)
    budget = ingest.disk_budget(store, chunkset, mmi_needed=True, fasta_bytes=1_000_000)
    size = chunkset.total_bytes
    assert budget.parts["aligned"] + budget.parts["sort_temp"] == pytest.approx(2.2 * size, abs=2)
    assert budget.parts["mmi"] == 2_500_000
    assert budget.parts["margin"] == 2 * 1024**3
    short = ingest.Budget(parts=budget.parts, free=1)
    with pytest.raises(LabProblem) as caught:
        ingest.require_budget(short, "align")
    data = caught.value.data
    assert caught.value.code == "TBX-LAB-010" and caught.value.retryable
    assert data["step"] == "align" and data["free_bytes"] == 1 and data["required_bytes"] == budget.required


def test_aligner_identity_argv_has_no_paths(lab_world) -> None:
    argv = ingest.aligner_argv_identity(["@RG\tID:x"])
    assert all("/" not in part for part in argv)
    assert "minimap2 -ax map-ont -y -R <@RG>" in argv
    multi = ingest.aligner_argv_identity(["@RG\tID:x", "@RG\tID:y"])
    assert any("addreplacerg" in part for part in multi)


def test_one_chunk_declaring_two_models_is_refused(lab_world) -> None:
    store = LabStore(lab_world["root"])
    store.ensure()
    header = {
        "HD": {"VN": "1.6", "SO": "unknown"},
        "RG": [rg_line(run="a", model="model-a@v1"), rg_line(run="b", model="model-b@v1")],
    }
    path = lab_world["tmp"] / "two_0.bam"
    with pysam.AlignmentFile(str(path), "wb", header=header):
        pass
    problem = _problem(ingest.inspect_chunks, ingest.collect_paths([str(path)]), store)
    assert problem.code == "TBX-LAB-008"
    assert set(problem.data["models"]) == {"model-a@v1", "model-b@v1"}
