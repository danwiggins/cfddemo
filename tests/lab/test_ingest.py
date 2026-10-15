"""D9 ingest refusals and chunk handling (TBX-LAB-005 to -009)."""

from __future__ import annotations

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
    calls: list[Path] = []
    monkeypatch.setattr(ingest, "_sha256_file", lambda path: calls.append(path) or "0" * 64)
    second = ingest.inspect_chunks(paths, store)
    assert calls == []
    assert first.source_id == second.source_id
    assert second.total_records == 1800


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
