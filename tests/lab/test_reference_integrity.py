"""Registered FASTA and .fai bytes remain bound to Lab execution identities."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import replace
from pathlib import Path

import pysam
import pytest

from traceback_lab.ingest import cached_sha256, ensure_mmi, mmi_path, resolve_reference
from traceback_lab.problems import LabProblem
from traceback_lab.reference_integrity import verify_reference_source
from traceback_lab.runner import RunTarget, _verify_target_reference
from traceback_lab.scrub import Scrubber
from traceback_lab.store import LabStore
from traceback_runner.cli import main
from traceback_runner.references import register_reference


def _reference(tmp_path: Path) -> tuple[LabStore, Path]:
    root = tmp_path / "root"
    fasta = tmp_path / "tiny.fa"
    fasta.write_text(">chr1\n" + "ACGT" * 25 + "\n")
    pysam.faidx(str(fasta))
    register_reference(root, fasta, "hg38", assembly="GRCh38")
    store = LabStore(root)
    store.ensure()
    return store, fasta


def _bam(tmp_path: Path) -> Path:
    path = tmp_path / "tiny.bam"
    header = pysam.AlignmentHeader.from_dict({
        "HD": {"VN": "1.6", "SO": "coordinate"},
        "SQ": [{"SN": "chr1", "LN": 100, "AS": "GRCh38"}],
        "RG": [{"ID": "synthetic", "PL": "ONT"}],
    })
    with pysam.AlignmentFile(str(path), "wb", header=header) as writer:
        read = pysam.AlignedSegment(header)
        read.query_name = "synthetic-1"
        read.reference_id = 0
        read.reference_start = 10
        read.mapping_quality = 60
        read.query_sequence = "G" * 40
        read.query_qualities = pysam.qualitystring_to_array("I" * 40)
        read.cigarstring = "40M"
        read.set_tag("RG", "synthetic")
        writer.write(read)
    pysam.index(str(path))
    return path


def test_verified_reference_cache_is_fast_and_dry_run_does_not_write(tmp_path, monkeypatch) -> None:
    store, fasta = _reference(tmp_path)
    reference = resolve_reference(store.root, "hg38")
    assert not store.hash_cache_file.exists()
    before = {path: path.read_bytes() for path in (fasta, Path(f"{fasta}.fai"))}
    verify_reference_source(store, reference, write_cache=False)
    assert not store.hash_cache_file.exists()
    assert all(path.read_bytes() == content for path, content in before.items())

    proof = verify_reference_source(store, reference, write_cache=True)
    from traceback_lab import reference_integrity

    def no_rescan(_path: Path) -> str:
        raise AssertionError("unchanged reference must reuse its verified proof")

    monkeypatch.setattr(reference_integrity, "_raw_sha256", no_rescan)
    assert verify_reference_source(
        store, reference, write_cache=True, expected_proof=proof,
    ) == proof


def test_cached_proof_is_bound_to_registration_contig_m5(tmp_path) -> None:
    store, _fasta = _reference(tmp_path)
    reference = resolve_reference(store.root, "hg38")
    proof = verify_reference_source(store, reference, write_cache=True)
    contig = reference.loaded.registered.contigs[0]
    changed_contig = contig.model_copy(update={"md5": "0" * 32})
    changed_registration = reference.loaded.registered.model_copy(update={"contigs": (changed_contig,)})
    changed = replace(reference, loaded=replace(reference.loaded, registered=changed_registration))
    with pytest.raises(LabProblem) as error:
        verify_reference_source(store, changed, write_cache=True, expected_proof=proof)
    assert error.value.code == "TBX-LAB-004"
    assert "indexed" in error.value.cause or "index" in error.value.cause


def test_same_size_restored_mtime_fasta_edit_fails_registered_digest(tmp_path) -> None:
    store, fasta = _reference(tmp_path)
    reference = resolve_reference(store.root, "hg38")
    verify_reference_source(store, reference, write_cache=True)
    old = fasta.stat()
    old_key = store.hash_cache_key(fasta)
    fasta.write_text(fasta.read_text().replace("ACGT", "TCGT", 1))
    os.utime(fasta, ns=(old.st_atime_ns, old.st_mtime_ns))
    assert store.hash_cache_key(fasta) == old_key
    assert fasta.stat().st_ctime_ns != old.st_ctime_ns
    with pytest.raises(LabProblem, match="No usable registered reference") as error:
        verify_reference_source(store, reference, write_cache=True)
    assert error.value.code == "TBX-LAB-004"


def test_unreadable_registered_fasta_reports_reference_error(tmp_path, monkeypatch) -> None:
    store, _fasta = _reference(tmp_path)
    reference = resolve_reference(store.root, "hg38")

    def deny_open(_path: Path) -> int:
        raise PermissionError("synthetic read denial")

    monkeypatch.setattr("traceback_lab.reference_integrity.open_regular_no_follow", deny_open)
    with pytest.raises(LabProblem) as error:
        verify_reference_source(store, reference, write_cache=False)
    assert error.value.code == "TBX-LAB-004"
    assert "SHA-256" in error.value.cause


def test_run_target_must_consume_registered_fasta_path(tmp_path) -> None:
    store, fasta = _reference(tmp_path)
    reference = resolve_reference(store.root, "hg38")
    target = RunTarget(
        input_id="a" * 64, source_id="b" * 64, input_dir=tmp_path,
        reference_id="hg38", reference_sha256=reference.sha256,
        extras={"reference_fasta": str(fasta.with_name("another.fa"))},
        reference_integrity_required=True,
    )
    with pytest.raises(LabProblem) as error:
        _verify_target_reference(store, target, write_cache=False)
    assert error.value.code == "TBX-LAB-004"
    assert "path differs" in error.value.cause


def test_fai_offset_edit_fails_indexed_m5_even_when_names_and_lengths_match(tmp_path) -> None:
    store, fasta = _reference(tmp_path)
    reference = resolve_reference(store.root, "hg38")
    verify_reference_source(store, reference, write_cache=True)
    fai = Path(f"{fasta}.fai")
    old = fai.stat()
    old_weak_key = store.hash_cache_key(fai)
    fields = fai.read_text().strip().split("\t")
    fields[2] = str(int(fields[2]) + 1)
    fai.write_text("\t".join(fields) + "\n")
    os.utime(fai, ns=(old.st_atime_ns, old.st_mtime_ns))
    assert store.hash_cache_key(fai) == old_weak_key
    with pytest.raises(LabProblem) as error:
        verify_reference_source(store, reference, write_cache=True)
    assert error.value.code == "TBX-LAB-004"
    assert "index" in error.value.cause


def test_index_edit_then_restore_invalidates_pre_use_proof(tmp_path) -> None:
    store, fasta = _reference(tmp_path)
    reference = resolve_reference(store.root, "hg38")
    proof = verify_reference_source(store, reference, write_cache=True)
    fai = Path(f"{fasta}.fai")
    original = fai.read_bytes()
    old = fai.stat()
    fields = fai.read_text().strip().split("\t")
    fields[2] = str(int(fields[2]) + 1)
    fai.write_text("\t".join(fields) + "\n")
    fai.write_bytes(original)
    os.utime(fai, ns=(old.st_atime_ns, old.st_mtime_ns))
    assert fai.stat().st_ctime_ns != old.st_ctime_ns
    with pytest.raises(LabProblem) as error:
        verify_reference_source(store, reference, write_cache=True, expected_proof=proof)
    assert error.value.code == "TBX-LAB-004"
    assert "during consumption" in error.value.cause


def test_index_build_edit_then_restore_does_not_publish_mmi(tmp_path, monkeypatch) -> None:
    from types import SimpleNamespace

    from traceback_lab import ingest

    store, fasta = _reference(tmp_path)
    reference = resolve_reference(store.root, "hg38")
    proof = verify_reference_source(store, reference, write_cache=True)
    original = fasta.read_bytes()

    def mutate_during_index(stages, **_kwargs):
        staging = Path(stages[0].argv[-2])
        staging.write_bytes(b"synthetic-index")
        old = fasta.stat()
        fasta.write_bytes(original.replace(b"ACGT", b"TCGT", 1))
        fasta.write_bytes(original)
        os.utime(fasta, ns=(old.st_atime_ns, old.st_mtime_ns))
        return SimpleNamespace(ok=True)

    monkeypatch.setattr(ingest, "run_pipeline", mutate_during_index)
    with pytest.raises(LabProblem) as error:
        ensure_mmi(
            store, reference, SimpleNamespace(path=tmp_path / "minimap2"),
            reference_proof=proof, override=None, scrubber=Scrubber(root=store.root),
            timeout_s=1,
        )
    assert error.value.code == "TBX-LAB-004"
    assert not mmi_path(store, reference).exists()
    assert not list(mmi_path(store, reference).parent.glob(".staging-*.mmi"))


def test_symlinked_fai_proof_tracks_link_target_edits(tmp_path) -> None:
    fasta = tmp_path / "tiny.fa"
    fasta.write_text(">chr1\n" + "ACGT" * 25 + "\n")
    pysam.faidx(str(fasta))
    fai = Path(f"{fasta}.fai")
    target = tmp_path / "index-target.fai"
    fai.rename(target)
    fai.symlink_to(target)
    root = tmp_path / "root"
    register_reference(root, fasta, "hg38", assembly="GRCh38")
    store = LabStore(root)
    store.ensure()
    reference = resolve_reference(root, "hg38")
    verify_reference_source(store, reference, write_cache=True)
    old = target.stat()
    fields = target.read_text().strip().split("\t")
    fields[2] = str(int(fields[2]) + 1)
    target.write_text("\t".join(fields) + "\n")
    os.utime(target, ns=(old.st_atime_ns, old.st_mtime_ns))
    with pytest.raises(LabProblem) as error:
        verify_reference_source(store, reference, write_cache=True)
    assert error.value.code == "TBX-LAB-004"


def test_alias_only_fai_requires_index_beside_canonical_fasta(tmp_path) -> None:
    real = tmp_path / "real.fa"
    real.write_text(">chr1\n" + "ACGT" * 25 + "\n")
    alias = tmp_path / "alias.fa"
    alias.symlink_to(real)
    pysam.faidx(str(alias))
    assert Path(f"{alias}.fai").is_file()
    assert not Path(f"{real}.fai").exists()
    root = tmp_path / "root"
    register_reference(root, alias, "hg38", assembly="GRCh38")
    store = LabStore(root)
    store.ensure()
    reference = resolve_reference(root, "hg38")
    assert reference.fasta == real
    with pytest.raises(LabProblem) as error:
        verify_reference_source(store, reference, write_cache=False)
    assert error.value.code == "TBX-LAB-004"
    assert "canonical FASTA path" in error.value.cause


def test_mmi_cache_rehashes_same_size_restored_mtime_replacement(tmp_path) -> None:
    store = LabStore(tmp_path / "root")
    store.ensure()
    mmi = tmp_path / "tiny.mmi"
    mmi.write_bytes(b"aaaa")
    first = cached_sha256(store, mmi)
    old = mmi.stat()
    old_weak_key = store.hash_cache_key(mmi)
    mmi.write_bytes(b"bbbb")
    os.utime(mmi, ns=(old.st_atime_ns, old.st_mtime_ns))
    assert store.hash_cache_key(mmi) == old_weak_key
    assert cached_sha256(store, mmi) == hashlib.sha256(b"bbbb").hexdigest() != first
    replacement = tmp_path / "replacement.mmi"
    replacement.write_bytes(b"cccc")
    os.utime(replacement, ns=(old.st_atime_ns, old.st_mtime_ns))
    os.replace(replacement, mmi)
    assert store.hash_cache_key(mmi) == old_weak_key
    assert cached_sha256(store, mmi) == hashlib.sha256(b"cccc").hexdigest()


def test_normal_cached_run_and_dry_run_refuse_changed_reference(tmp_path, capsys) -> None:
    store, fasta = _reference(tmp_path)
    bam = _bam(tmp_path)
    argv = [
        "lab", "run", str(bam), "--reference", "hg38", "--only", "bam-qc",
        "--no-downsample", "--no-controls", "--root", str(store.root), "--json",
    ]
    assert main([*argv, "--dry-run"]) == 0
    capsys.readouterr()
    assert not store.hash_cache_file.exists()
    assert main(argv) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["data"]["counts"]["ok"] == 2
    assert main(argv) == 0
    cached = json.loads(capsys.readouterr().out)
    assert all(step["cached"] for step in cached["data"]["steps"])

    old = fasta.stat()
    fasta.write_text(fasta.read_text().replace("ACGT", "TCGT", 1))
    os.utime(fasta, ns=(old.st_atime_ns, old.st_mtime_ns))
    assert main(argv) == 3
    blocked = json.loads(capsys.readouterr().out)
    assert blocked["data"]["code"] == "TBX-LAB-004"
    assert main([*argv, "--dry-run"]) == 3
    dry = json.loads(capsys.readouterr().out)
    assert dry["data"]["code"] == "TBX-LAB-004"
    stored = [
        "lab", "run", first["data"]["sample"]["id12"], "--only", "bam-qc",
        "--no-downsample", "--no-controls", "--root", str(store.root), "--json",
    ]
    assert main(stored) == 3
    assert json.loads(capsys.readouterr().out)["data"]["code"] == "TBX-LAB-004"
    assert main([*stored, "--dry-run"]) == 3
    assert json.loads(capsys.readouterr().out)["data"]["code"] == "TBX-LAB-004"


def test_worker_reference_change_blocks_result_publication(tmp_path, capsys, monkeypatch) -> None:
    from traceback_lab import runner

    store, fasta = _reference(tmp_path)
    bam = _bam(tmp_path)
    actual_pipeline = runner.run_pipeline
    changed = False

    def change_after_worker(*args, **kwargs):
        nonlocal changed
        outcome = actual_pipeline(*args, **kwargs)
        if outcome.ok and not changed:
            changed = True
            fasta.write_text(fasta.read_text().replace("ACGT", "TCGT", 1))
        return outcome

    monkeypatch.setattr(runner, "run_pipeline", change_after_worker)
    code = main([
        "lab", "run", str(bam), "--reference", "hg38", "--only", "bam-qc",
        "--no-downsample", "--no-controls", "--root", str(store.root), "--json",
    ])
    assert changed
    assert code == 3
    blocked = json.loads(capsys.readouterr().out)
    assert blocked["data"]["code"] == "TBX-LAB-004"
    assert not list(store.results_dir.rglob("selected.json"))


def test_worker_edit_then_restore_cannot_publish_consumed_bases(tmp_path, capsys, monkeypatch) -> None:
    from traceback_lab import runner

    root = tmp_path / "root"
    fasta = tmp_path / "three-bins.fa"
    bins = ["AAAACCCCCC", "AAAAACCCCC", "AAAAAACCCC"]
    with fasta.open("w") as stream:
        stream.write(">chr1\n")
        for motif in bins:
            sequence = motif * 100_000
            for start in range(0, len(sequence), 100):
                stream.write(sequence[start:start + 100] + "\n")
    original = fasta.read_bytes()
    changed = original.replace(b"AAAACCCCCC", b"AAAAAAACCC")
    assert len(changed) == len(original) and changed != original
    pysam.faidx(str(fasta))
    register_reference(root, fasta, "hg38", assembly="GRCh38")
    store = LabStore(root)
    store.ensure()

    bam = tmp_path / "three-bins.bam"
    header = pysam.AlignmentHeader.from_dict({
        "HD": {"VN": "1.6", "SO": "coordinate"},
        "SQ": [{"SN": "chr1", "LN": 3_000_000, "AS": "GRCh38"}],
        "RG": [{"ID": "synthetic", "PL": "ONT"}],
    })
    with pysam.AlignmentFile(str(bam), "wb", header=header) as writer:
        serial = 0
        for bin_index, count in enumerate((5, 10, 20)):
            for index in range(count):
                read = pysam.AlignedSegment(header)
                read.query_name = f"synthetic-{serial}"
                read.reference_id = 0
                read.reference_start = bin_index * 1_000_000 + index * 100
                read.mapping_quality = 60
                read.query_sequence = "A" * 100
                read.query_qualities = pysam.qualitystring_to_array("I" * 100)
                read.cigarstring = "100M"
                read.set_tag("RG", "synthetic")
                writer.write(read)
                serial += 1
    pysam.index(str(bam))

    actual_pipeline = runner.run_pipeline
    consumed: dict[str, float | int | bool | None] = {}

    def edit_during_bam_qc(stages, **kwargs):
        request = Path(stages[0].argv[-1])
        if request.parent.name != "bam-qc-full":
            return actual_pipeline(stages, **kwargs)
        old = fasta.stat()
        fasta.write_bytes(changed)
        os.utime(fasta, ns=(old.st_atime_ns, old.st_mtime_ns))
        outcome = actual_pipeline(stages, **kwargs)
        produced = json.loads((request.parent / "output.json").read_text())
        consumed["gc_slope"] = next(
            metric["value"] for metric in produced["metrics"]
            if metric["metric_id"] == "gc_bias_slope"
        )
        fasta.write_bytes(original)
        os.utime(fasta, ns=(old.st_atime_ns, old.st_mtime_ns))
        consumed["ctime_changed"] = fasta.stat().st_ctime_ns != old.st_ctime_ns
        return outcome

    monkeypatch.setattr(runner, "run_pipeline", edit_during_bam_qc)
    code = main([
        "lab", "run", str(bam), "--reference", "hg38", "--only", "bam-qc",
        "--no-downsample", "--no-controls", "--root", str(root), "--json",
    ])
    assert isinstance(consumed["gc_slope"], float) and consumed["gc_slope"] > 0
    assert consumed["ctime_changed"] is True
    assert code == 3
    assert json.loads(capsys.readouterr().out)["data"]["code"] == "TBX-LAB-004"
    assert not list(store.results_dir.glob("*/bam-qc/*/selected.json"))


@pytest.mark.parametrize("drift", ["missing_registration", "changed_fasta"])
def test_simulate_reference_block_records_run_end(tmp_path, capsys, drift) -> None:
    store, fasta = _reference(tmp_path)
    bam = _bam(tmp_path)
    assert main([
        "lab", "run", str(bam), "--reference", "hg38", "--only", "bam-qc",
        "--no-downsample", "--no-controls", "--root", str(store.root), "--json",
    ]) == 0
    first = json.loads(capsys.readouterr().out)
    before = set(store.run_ids())
    if drift == "missing_registration":
        registration = store.root / "references" / "hg38"
        registration.rename(registration.with_name("unavailable"))
    else:
        fasta.write_text(fasta.read_text().replace("ACGT", "TCGT", 1))
    code = main([
        "lab", "simulate", "split", first["data"]["sample"]["id12"],
        "--root", str(store.root), "--json",
    ])
    assert code == 3
    assert json.loads(capsys.readouterr().out)["data"]["code"] == "TBX-LAB-004"
    (run_id,) = set(store.run_ids()) - before
    events = store.run_log(run_id).events()
    assert events[-1]["event"] == "run_end"
    assert events[-1]["status"] == "blocked"
    assert events[-1]["code"] == "TBX-LAB-004"
