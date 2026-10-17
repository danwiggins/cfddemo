"""Synthetic contract and real-process checks for exploratory viral side alignment."""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
from pathlib import Path
from types import SimpleNamespace

import pysam
import pytest
import numpy as np

from traceback_lab.asset_command import add_asset_parser
from traceback_lab.assets import AssetError
from traceback_lab.algorithms import viral_reads
from traceback_lab.algorithms.viral_reads import VIRAL_READS, _count_hits, _derived_keep, _write_queries
from traceback_lab.registry import InputContext, Ready, Skip
from traceback_lab.read_summary import DS_SALT, SPLIT_SALT, downsample_mask, h64, split_mask
from traceback_lab.pipeline import Stage, run_pipeline
from traceback_lab.store import LabStore
from traceback_lab.tools import MIN_MINIMAP2, find_minimap2
from traceback_lab.viral_assets import add_viral_reference, inspect_viral_reference


def _sources(tmp_path: Path) -> tuple[Path, Path, dict[str, str]]:
    randomizer = random.Random(87155)
    sequences = {
        virus: "".join(randomizer.choices("ACGT", k=3000))
        for virus in ("EBV", "HPV", "HBV")
    }
    fasta = tmp_path / "reference.fasta"
    targets = tmp_path / "targets.tsv"
    fasta.write_text("".join(f">{virus}\n{sequence}\n" for virus, sequence in sequences.items()))
    targets.write_text(
        "contig\tvirus\taccession\n"
        + "".join(f"{virus}\t{virus}\tSYNTH_{virus}.1\n" for virus in sequences)
    )
    return fasta, targets, sequences


def _add(store: LabStore, fasta: Path, targets: Path) -> None:
    with store.writer("asset add", "r-20261016-1200-0000", step="viral-fasta"):
        add_viral_reference(
            store, fasta, targets, source_label="synthetic-viral-reference",
            terms="known", license_id="synthetic-test",
        )


def _context(root: Path, input_dir: Path, *, derived: dict | None = None) -> InputContext:
    return InputContext(
        root=root, source_id="synthetic", input_id="synthetic", reference_id="hg38",
        reference_sha256="a" * 64, input_dir=input_dir, derived=derived,
    )


def test_registration_cli_identity_and_tamper(tmp_path: Path) -> None:
    store = LabStore(tmp_path / "root")
    fasta, targets, _ = _sources(tmp_path)
    parser = argparse.ArgumentParser()
    add_asset_parser(parser.add_subparsers(dest="command", required=True), lambda child: child.add_argument("--root"))
    args = parser.parse_args([
        "asset", "add", "viral-fasta", "--file", str(fasta), "--targets", str(targets),
        "--source-label", "synthetic-viral-reference", "--terms", "known",
        "--license-id", "synthetic-test",
    ])
    code, payload = args.handler(args, store)
    assert code == 0
    record = inspect_viral_reference(store)
    assert payload["data"]["asset"]["asset_id"] == f"viral-fasta:{hashlib.sha256(fasta.read_bytes()).hexdigest()}"
    assert len(record.targets) == 3
    assert VIRAL_READS.asset_sha256s(store.root) == record.asset_sha256s
    args = parser.parse_args(["asset", "inspect", "viral-fasta"])
    assert args.handler(args, store)[0] == 0
    targets.write_text(targets.read_text().replace("SYNTH_EBV.1", "SYNTH_EBV.2"))
    with pytest.raises(AssetError, match="different viral reference"):
        _add(store, fasta, targets)
    assert record.asset_sha256s == (
        hashlib.sha256(fasta.read_bytes()).hexdigest(),
        hashlib.sha256((record.fasta.parent / "targets.tsv").read_bytes()).hexdigest(),
    )
    (record.fasta.parent / "targets.tsv").write_text("tampered")
    with pytest.raises(AssetError):
        inspect_viral_reference(store)
    assert VIRAL_READS.asset_sha256s(store.root) == ()
    status = VIRAL_READS.preflight(_context(store.root, tmp_path))
    assert isinstance(status, Skip)
    assert status.reason_code == "invalid_asset"


@pytest.mark.parametrize("fasta_bytes,targets_bytes", [
    (b">\nACGT\n", None),
    (b">EBV\nACGT\n>HPV\nACGT\n>HBV\n", None),
    (None, b"contig\tvirus\taccession\nEBV\tEBV\tSYNTH_EBV.1\n"),
])
def test_invalid_reference_does_not_publish(
    tmp_path: Path, fasta_bytes: bytes | None, targets_bytes: bytes | None,
) -> None:
    store = LabStore(tmp_path / "root")
    fasta, targets, _ = _sources(tmp_path)
    if fasta_bytes is not None:
        fasta.write_bytes(fasta_bytes)
    if targets_bytes is not None:
        targets.write_bytes(targets_bytes)
    with pytest.raises(AssetError):
        _add(store, fasta, targets)
    assert not (store.lab / "assets" / "viral-fasta").exists()


def test_reference_accepts_blank_fasta_separators(tmp_path: Path) -> None:
    store = LabStore(tmp_path / "root")
    fasta, targets, _ = _sources(tmp_path)
    fasta.write_text(fasta.read_text().replace("\n>", "\n\n>") + "\n")
    _add(store, fasta, targets)
    assert len(inspect_viral_reference(store).targets) == 3


def test_absent_reference_is_explicitly_unavailable(tmp_path: Path) -> None:
    status = VIRAL_READS.preflight(_context(tmp_path, tmp_path))
    assert isinstance(status, Skip)
    assert status.reason_code == "missing_asset"


def test_paf_counts_unique_primary_virus_and_excludes_cross_virus(tmp_path: Path) -> None:
    store = LabStore(tmp_path / "root")
    fasta, targets, _ = _sources(tmp_path)
    _add(store, fasta, targets)
    paf = tmp_path / "hits.paf"
    def row(qid: int, virus: str, tag: str = "tp:A:P") -> str:
        return f"q{qid}\t500\t0\t500\t+\t{virus}\t3000\t0\t500\t500\t500\t60\t{tag}\n"
    paf.write_text(
        row(1, "EBV") + row(1, "EBV") + row(2, "HPV") + row(2, "HBV")
        + row(3, "HBV", "tp:A:S") + row(4, "EBV") + row(4, "HPV", "tp:A:S")
    )
    counts = _count_hits(paf, inspect_viral_reference(store), 4, tmp_path / "hits.sqlite")
    assert counts == {"EBV": 1, "HPV": 0, "HBV": 0}


def test_malformed_paf_closes_sqlite_connection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import sqlite3

    store = LabStore(tmp_path / "root")
    fasta, targets, _ = _sources(tmp_path)
    _add(store, fasta, targets)
    paf = tmp_path / "malformed.paf"
    paf.write_text("not PAF\n")
    real_connect = sqlite3.connect
    closed: list[bool] = []

    class TrackedConnection:
        def __init__(self, path: Path) -> None:
            self.connection = real_connect(path)

        def execute(self, *args: object) -> object:
            return self.connection.execute(*args)

        def close(self) -> None:
            self.connection.close()
            closed.append(True)

    monkeypatch.setattr(viral_reads.sqlite3, "connect", TrackedConnection)
    db = tmp_path / "malformed.sqlite"
    with pytest.raises(RuntimeError, match="malformed PAF"):
        _count_hits(paf, inspect_viral_reference(store), 1, db)
    assert closed == [True]
    db.unlink()


def test_derived_membership_is_deterministic(tmp_path: Path) -> None:
    parent = _context(tmp_path, tmp_path)
    downsample = _context(tmp_path, tmp_path, derived={"transform": "downsample", "fraction": 0.5})
    half_a = _context(tmp_path, tmp_path, derived={"transform": "split", "half": "A"})
    half_b = _context(tmp_path, tmp_path, derived={"transform": "split", "half": "B"})
    assert _derived_keep(parent, "read-1")
    assert 0 < sum(_derived_keep(downsample, f"read-{i}") for i in range(100)) < 100
    assert all(_derived_keep(half_a, f"read-{i}") != _derived_keep(half_b, f"read-{i}") for i in range(100))
    names = [f"read-{index}" for index in range(100)]
    ds_hashes = np.array([h64(DS_SALT, name) for name in names], dtype=np.uint64)
    split_hashes = np.array([h64(SPLIT_SALT, name) for name in names], dtype=np.uint64)
    for fraction in (0.1, 0.3, 0.5, 0.9):
        derived = _context(tmp_path, tmp_path, derived={"transform": "downsample", "fraction": fraction})
        assert [_derived_keep(derived, name) for name in names] == downsample_mask(ds_hashes, fraction).tolist()
    assert [_derived_keep(half_a, name) for name in names] == split_mask(split_hashes, "A").tolist()
    assert [_derived_keep(half_b, name) for name in names] == split_mask(split_hashes, "B").tolist()


def test_preflight_rejects_invalid_derived_transform(tmp_path: Path) -> None:
    for derived in ({"transform": "unknown"}, {"transform": "split", "half": "C"},
                    {"transform": "downsample", "fraction": 2.0}):
        status = VIRAL_READS.preflight(_context(tmp_path, tmp_path, derived=derived))
        assert isinstance(status, Skip)
        assert status.reason_code == "unsupported_transform"


def test_query_staging_anonymizes_names_without_aligner(tmp_path: Path) -> None:
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    with pysam.AlignmentFile(str(input_dir / "aligned.bam"), "wb", header={
        "HD": {"VN": "1.6"}, "SQ": [{"SN": "chr1", "LN": 10000}],
    }) as bam:
        record = pysam.AlignedSegment()
        record.query_name = "private-donor-read"
        record.flag = 4
        record.query_sequence = "ACGT" * 20
        bam.write(record)
    staged = tmp_path / "queries.fasta"
    assert _write_queries(_context(tmp_path, input_dir), staged) == (1, 1, True)
    assert staged.read_text() == ">q1\n" + "ACGT" * 20 + "\n"
    assert "private-donor-read" not in staged.read_text()


def test_real_minimap2_side_alignment_on_synthetic_bam(tmp_path: Path) -> None:
    tool = find_minimap2()
    if tool.path is None or tool.numeric is None or tool.numeric < MIN_MINIMAP2:
        pytest.skip("minimap2 >= 2.28 unavailable")
    store = LabStore(tmp_path / "root")
    fasta, targets, sequences = _sources(tmp_path)
    _add(store, fasta, targets)
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    with pysam.AlignmentFile(str(input_dir / "aligned.bam"), "wb", header={"HD": {"VN": "1.6"}, "SQ": [{"SN": "chr1", "LN": 10000}]}) as bam:
        for name, sequence in [
            ("donor-private-a", sequences["EBV"][200:1400]),
            ("donor-private-b", sequences["HPV"][300:1500]),
            ("donor-private-c", sequences["HBV"][400:1600]),
            ("donor-private-d", sequences["EBV"][500:1700]),
        ]:
            record = pysam.AlignedSegment()
            record.query_name = name
            record.query_sequence = sequence
            record.flag = 4
            bam.write(record)
        mapped = pysam.AlignedSegment()
        mapped.query_name = "human-mapped"
        mapped.query_sequence = "A" * 1200
        mapped.flag = 0
        mapped.reference_id = 0
        mapped.reference_start = 1
        mapped.cigarstring = "1200M"
        bam.write(mapped)
    output = tmp_path / "output"
    output.mkdir()
    ctx = _context(store.root, input_dir)
    staged_queries = tmp_path / "queries.fasta"
    assert _write_queries(ctx, staged_queries) == (4, 4, True)
    assert [line for line in staged_queries.read_text().splitlines() if line.startswith(">")] == [
        ">q1", ">q2", ">q3", ">q4",
    ]
    assert "donor-private" not in staged_queries.read_text()
    assert isinstance(VIRAL_READS.preflight(ctx), Ready)
    result = VIRAL_READS.run(ctx, output)
    assert {item["metric_id"]: item["value"] for item in result.metrics} == {
        "ebv_rpm": 500000.0, "hpv_rpm": 250000.0, "hbv_rpm": 250000.0,
    }
    assert all(item["n"] == 4 for item in result.metrics)
    summary = json.loads(result.artifacts["viral_reads.json"])
    assert summary["denominator_unmapped_primaries"] == 4
    assert summary["hits"] == {"EBV": 2, "HPV": 1, "HBV": 1}
    assert summary["missing_sequences"] == 0
    assert summary["availability_reason"] is None
    assert not list(tmp_path.glob("viral-*"))


def test_real_minimap2_excludes_reported_cross_group_secondary(tmp_path: Path) -> None:
    tool = find_minimap2()
    if tool.path is None or tool.numeric is None or tool.numeric < MIN_MINIMAP2:
        pytest.skip("minimap2 >= 2.28 unavailable")
    store = LabStore(tmp_path / "root")
    fasta, targets, sequences = _sources(tmp_path)
    fasta.write_text(
        f">EBV\n{sequences['EBV']}\n>HPV\n{sequences['EBV']}\n>HBV\n{sequences['HBV']}\n"
    )
    _add(store, fasta, targets)
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    with pysam.AlignmentFile(str(input_dir / "aligned.bam"), "wb", header={
        "HD": {"VN": "1.6"}, "SQ": [{"SN": "chr1", "LN": 10000}],
    }) as bam:
        record = pysam.AlignedSegment()
        record.query_name = "private-ambiguous"
        record.flag = 4
        record.query_sequence = sequences["EBV"][200:1400]
        bam.write(record)
    output = tmp_path / "output"
    output.mkdir()
    result = VIRAL_READS.run(_context(store.root, input_dir), output)
    assert all(item["value"] == 0 and item["n"] == 1 for item in result.metrics)
    assert json.loads(result.artifacts["viral_reads.json"])["hits"] == {"EBV": 0, "HPV": 0, "HBV": 0}
    assert not list(tmp_path.glob("viral-*"))


@pytest.mark.parametrize("sequences,expected_reason,expected_value", [
    ([], "no_unmapped_primaries", None),
    ([None, None], "missing_sequence", None),
    ([None, "virus"], "missing_sequence", None),
    (["N" * 1200], None, 0.0),
])
def test_missing_sequence_never_looks_like_a_negative_result(
    tmp_path: Path, monkeypatch, sequences: list[str | None],
    expected_reason: str | None, expected_value: float | None,
) -> None:
    tool = find_minimap2()
    if tool.path is None or tool.numeric is None or tool.numeric < MIN_MINIMAP2:
        if any(sequence is not None for sequence in sequences):
            pytest.skip("minimap2 >= 2.28 unavailable")
        monkeypatch.setattr(viral_reads, "_minimap2", lambda: (sys.executable, "2.28", (2, 28)))
    store = LabStore(tmp_path / "root")
    fasta, targets, viruses = _sources(tmp_path)
    _add(store, fasta, targets)
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    with pysam.AlignmentFile(str(input_dir / "aligned.bam"), "wb", header={"HD": {"VN": "1.6"}, "SQ": [{"SN": "chr1", "LN": 10000}]}) as bam:
        for index, sequence in enumerate(sequences):
            record = pysam.AlignedSegment()
            record.query_name = f"private-{index}"
            record.flag = 4
            record.query_sequence = viruses["EBV"][200:1400] if sequence == "virus" else sequence
            bam.write(record)
    output = tmp_path / "output"
    output.mkdir()
    result = VIRAL_READS.run(_context(store.root, input_dir), output)
    assert {item["value"] for item in result.metrics} == {expected_value}
    assert all(item["n"] == len(sequences) for item in result.metrics)
    summary = json.loads(result.artifacts["viral_reads.json"])
    assert summary["denominator_unmapped_primaries"] == len(sequences)
    assert summary["missing_sequences"] == sequences.count(None)
    assert summary["availability_reason"] == expected_reason
    assert summary["hits"] == ({"EBV": 0, "HPV": 0, "HBV": 0} if expected_reason is None else None)
    assert not list(tmp_path.glob("viral-*"))


def test_query_budget_returns_unavailable_without_running_aligner(tmp_path: Path, monkeypatch) -> None:
    store = LabStore(tmp_path / "root")
    fasta, targets, viruses = _sources(tmp_path)
    _add(store, fasta, targets)
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    with pysam.AlignmentFile(str(input_dir / "aligned.bam"), "wb", header={
        "HD": {"VN": "1.6"}, "SQ": [{"SN": "chr1", "LN": 10000}],
    }) as bam:
        for index in range(2):
            record = pysam.AlignedSegment()
            record.query_name = f"private-{index}"
            record.flag = 4
            record.query_sequence = viruses["EBV"][200:1400]
            bam.write(record)
    monkeypatch.setattr(viral_reads, "MAX_QUERY_FASTA_BYTES", 100)
    monkeypatch.setattr(viral_reads, "_minimap2", lambda: (sys.executable, "2.28", (2, 28)))
    output = tmp_path / "output"
    output.mkdir()
    result = VIRAL_READS.run(_context(store.root, input_dir), output)
    assert all(item["value"] is None and item["n"] == 2 for item in result.metrics)
    summary = json.loads(result.artifacts["viral_reads.json"])
    assert summary["availability_reason"] == "query_budget_exceeded"
    assert summary["queries_with_sequence"] == 2
    assert summary["hits"] is None
    assert not list(tmp_path.glob("viral-*"))


def test_aligner_failure_and_paf_budget_return_unavailable(tmp_path: Path, monkeypatch) -> None:
    store = LabStore(tmp_path / "root")
    fasta, targets, viruses = _sources(tmp_path)
    _add(store, fasta, targets)
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    with pysam.AlignmentFile(str(input_dir / "aligned.bam"), "wb", header={
        "HD": {"VN": "1.6"}, "SQ": [{"SN": "chr1", "LN": 10000}],
    }) as bam:
        record = pysam.AlignedSegment()
        record.query_name = "private-read"
        record.flag = 4
        record.query_sequence = viruses["EBV"][200:1400]
        bam.write(record)
    monkeypatch.setattr(viral_reads, "_minimap2", lambda: (sys.executable, "2.28", (2, 28)))
    output = tmp_path / "output"
    output.mkdir()

    def failed(*args, **kwargs):
        return SimpleNamespace(ok=False, timed_out=True)

    monkeypatch.setattr(viral_reads, "run_pipeline", failed)
    result = VIRAL_READS.run(_context(store.root, input_dir), output)
    summary = json.loads(result.artifacts["viral_reads.json"])
    assert summary["availability_reason"] == "alignment_timeout"
    assert summary["hits"] is None
    assert all(item["value"] is None for item in result.metrics)
    assert not list(tmp_path.glob("viral-*"))

    def oversized(*args, **kwargs):
        kwargs["stdout_path"].write_bytes(b"x" * 2)
        kwargs["on_tick"](0.0)
        pytest.fail("oversized PAF must abort the side aligner")

    monkeypatch.setattr(viral_reads, "MAX_PAF_BYTES", 1)
    monkeypatch.setattr(viral_reads, "run_pipeline", oversized)
    result = VIRAL_READS.run(_context(store.root, input_dir), output)
    summary = json.loads(result.artifacts["viral_reads.json"])
    assert summary["availability_reason"] == "alignment_budget_exceeded"
    assert summary["hits"] is None
    assert not list(tmp_path.glob("viral-*"))


def test_real_pipeline_aborts_on_paf_budget_tick(tmp_path: Path) -> None:
    paf = tmp_path / "hits.paf"

    def budget_tick(_elapsed: float) -> None:
        if paf.exists() and paf.stat().st_size > 1024:
            raise viral_reads.ViralBudgetExceeded("PAF budget exceeded")

    with pytest.raises(viral_reads.ViralBudgetExceeded, match="PAF budget"):
        run_pipeline(
            [Stage("emit", [sys.executable, "-c", "import sys, time\nwhile True:\n print('x' * 4096, flush=True)\n time.sleep(0.01)"])],
            timeout_s=10, stdout_path=paf, on_tick=budget_tick,
        )
    assert paf.stat().st_size > 1024
