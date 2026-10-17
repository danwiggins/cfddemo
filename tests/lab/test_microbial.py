"""Synthetic microbial classification and compatible-cohort contracts."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pysam
import pytest

from traceback_lab.algorithms import microbial as algorithm
from traceback_lab.cohort_contracts import CohortGeneration, CohortMember, CohortResult
from traceback_lab.envelope import Metric, ResultEnvelope, provenance
from traceback_lab.microbial_db import MicrobialDbError, inspect_database, register_database
from traceback_lab.microbial_view import profile_for_sample
from traceback_lab.registry import InputContext
from traceback_lab.store import LabStore
from traceback_lab.views import InputView, SampleView


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _database(tmp_path: Path) -> tuple[LabStore, Path]:
    store = LabStore(tmp_path / "root")
    directory = tmp_path / "kraken-db"
    directory.mkdir()
    for name in ("hash.k2d", "opts.k2d", "taxo.k2d", "database75mers.kmer_distrib"):
        (directory / name).write_bytes(f"synthetic {name}".encode())
    with store.writer("microbial db add", "r-20261016-0000-aabb", step="database"):
        register_database(store, directory, 75, "synthetic database")
    return store, directory


def test_database_is_pinned_and_refuses_changed_bytes(tmp_path) -> None:
    store, directory = _database(tmp_path)
    registered = inspect_database(store)
    assert registered.read_length == 75 and len(registered.files) == 4
    (directory / "hash.k2d").write_bytes(b"different")
    with pytest.raises(MicrobialDbError, match="bytes changed"):
        inspect_database(store)


def test_database_registration_and_inspection_cli(tmp_path, capsys) -> None:
    from traceback_runner import cli

    directory = tmp_path / "operator-db"
    directory.mkdir()
    for name in ("hash.k2d", "opts.k2d", "taxo.k2d", "database75mers.kmer_distrib"):
        (directory / name).write_bytes(name.encode())
    root = tmp_path / "root"
    assert cli.main(["lab", "microbial", "db", "add", "--directory", str(directory),
                     "--read-length", "75", "--source-label", "local-synthetic",
                     "--root", str(root), "--json"]) == 0
    added = json.loads(capsys.readouterr().out)
    assert str(directory) not in json.dumps(added)
    assert cli.main(["lab", "microbial", "db", "inspect", "--root", str(root), "--json"]) == 0
    inspected = json.loads(capsys.readouterr().out)
    assert str(directory) not in json.dumps(inspected)


def test_tool_identity_changes_when_executed_helper_changes(tmp_path, monkeypatch) -> None:
    launcher = tmp_path / "kraken2"
    launcher.write_bytes(b"launcher")
    (tmp_path / "classify").write_bytes(b"classifier v1")
    (tmp_path / "kraken2lib.pm").write_bytes(b"library")
    monkeypatch.setattr(algorithm.shutil, "which", lambda name: str(launcher) if name == "kraken2" else None)
    first = algorithm._tool("kraken2")
    assert first is not None
    (tmp_path / "classify").write_bytes(b"classifier v2")
    assert algorithm._tool("kraken2")[1] != first[1]


def test_report_lineage_excludes_human_and_unknown_taxa(tmp_path) -> None:
    report = tmp_path / "report"
    report.write_text(
        "0.00\t1\t1\tU\t0\tunclassified\n"
        "0.00\t2\t0\tR\t1\troot\n"
        "0.00\t1\t0\tD\t2\t  Bacteria\n"
        "0.00\t1\t1\tS\t123\t    Candidate species\n"
        "0.00\t1\t0\tD\t2759\t  Eukaryota\n"
        "0.00\t1\t1\tS\t9606\t    Homo sapiens\n"
    )
    lineages, classified, unclassified = algorithm._report_lineages(report, 3)
    assert (classified, unclassified) == (2, 1)
    assert lineages[123] == "bacteria"
    assert 9606 not in lineages


def test_run_clips_only_unmapped_eligible_reads_and_uses_primary_denominator(tmp_path, monkeypatch) -> None:
    store, _ = _database(tmp_path)
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    bam = input_dir / "aligned.bam"
    header = {"HD": {"VN": "1.6"}, "SQ": [{"SN": "chr1", "LN": 1000}]}

    def read(name: str, length: int, flag: int) -> pysam.AlignedSegment:
        item = pysam.AlignedSegment()
        item.query_name = name
        item.query_sequence = "A" * length
        item.flag = flag
        item.reference_id = -1 if flag & 4 else 0
        item.reference_start = -1 if flag & 4 else 0
        item.mapping_quality = 0 if flag & 4 else 60
        item.cigar = () if flag & 4 else ((0, length),)
        return item

    with pysam.AlignmentFile(bam, "wb", header=header) as stream:
        for item in (read("mapped-private-id", 100, 0), read("candidate-private-id", 100, 4),
                     read("short-private-id", 50, 4), read("duplicate-private-id", 100, 4 | 1024)):
            stream.write(item)
    monkeypatch.setattr(algorithm, "_tool", lambda name: (name, "a" * 64))
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        if "--report" in argv:
            candidate_fasta = Path(argv[-1]).read_text()
            assert candidate_fasta == ">r1\n" + "A" * 75 + "\n"
            Path(argv[argv.index("--report") + 1]).write_text(
                "0.00\t0\t0\tU\t0\tunclassified\n"
                "100.00\t1\t0\tR\t1\troot\n"
                "100.00\t1\t0\tD\t2\t  Bacteria\n"
                "100.00\t1\t1\tS\t123\t    Candidate species\n"
            )
        else:
            Path(argv[argv.index("-o") + 1]).write_text(
                "name\ttaxonomy_id\ttaxonomy_lvl\tkraken_assigned_reads\tadded_reads\tnew_est_reads\tfraction_total_reads\n"
                "Candidate species\t123\tS\t1\t0\t1.0\t1.0\n"
            )
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(algorithm.subprocess, "run", fake_run)
    ctx = InputContext(store.root, "a" * 64, "b" * 64, "ref", "c" * 64, input_dir)
    output = algorithm.MICROBIAL.run(ctx, tmp_path / "out")
    metrics = {row["metric_id"]: row["value"] for row in output.metrics}
    assert metrics["primary_reads"] == 3
    assert metrics["candidate_unmapped_reads"] == 1
    assert metrics["too_short_unmapped_reads"] == 1
    assert metrics["taxon_123_rpm"] == pytest.approx(1_000_000 / 3)
    assert len(calls) == 2 and "-r" in calls[1] and calls[1][calls[1].index("-r") + 1] == "75"
    artifact = output.artifacts["microbial_profile.json"]
    assert b"private-id" not in artifact and b"candidate-private-id" not in artifact


def test_all_unclassified_candidates_yield_zero_without_bracken(tmp_path, monkeypatch) -> None:
    store, _ = _database(tmp_path)
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    header = {"HD": {"VN": "1.6"}, "SQ": [{"SN": "chr1", "LN": 1000}]}
    read = pysam.AlignedSegment()
    read.query_name = "private-name"
    read.query_sequence = "A" * 75
    read.flag = 4
    read.reference_id = -1
    read.reference_start = -1
    with pysam.AlignmentFile(input_dir / "aligned.bam", "wb", header=header) as stream:
        stream.write(read)
    monkeypatch.setattr(algorithm, "_tool", lambda name: (name, "a" * 64))
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        assert "--report" in argv, "Bracken must not run without classified reads"
        Path(argv[argv.index("--report") + 1]).write_text("100.00\t1\t1\tU\t0\tunclassified\n")
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(algorithm.subprocess, "run", fake_run)
    ctx = InputContext(store.root, "a" * 64, "b" * 64, "ref", "c" * 64, input_dir)
    output = algorithm.MICROBIAL.run(ctx, tmp_path / "out")
    metrics = {row["metric_id"]: row["value"] for row in output.metrics}
    assert len(calls) == 1
    assert metrics["microbial_rpm_primary"] == 0
    assert metrics["unclassified_candidate_reads"] == 1
    assert b"private-name" not in output.artifacts["microbial_profile.json"]


def test_run_refuses_changed_planned_database_or_tool(tmp_path, monkeypatch) -> None:
    store, _ = _database(tmp_path)
    monkeypatch.setattr(algorithm, "_tool", lambda name: (name, "a" * 64))
    database = inspect_database(store)
    tools = [tool.identity() for tool in algorithm.MICROBIAL.tools]
    common = (store.root, "a" * 64, "b" * 64, "ref", "c" * 64, tmp_path / "input")
    with pytest.raises(RuntimeError, match="database changed after execution planning"):
        algorithm.MICROBIAL.run(InputContext(*common, extras={
            "planned_asset_sha256s": ["0" * 64], "planned_tool_pins": tools,
        }), tmp_path / "out")
    with pytest.raises(RuntimeError, match="toolchain changed after execution planning"):
        algorithm.MICROBIAL.run(InputContext(*common, extras={
            "planned_asset_sha256s": [database.sha256],
            "planned_tool_pins": [{"name": "kraken2", "version": "0" * 64}, tools[1]],
        }), tmp_path / "out")


def test_zero_primary_reads_are_unavailable_not_a_measured_zero(tmp_path, monkeypatch) -> None:
    store, _ = _database(tmp_path)
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    with pysam.AlignmentFile(input_dir / "aligned.bam", "wb", header={"HD": {"VN": "1.6"}}):
        pass
    monkeypatch.setattr(algorithm, "_tool", lambda name: (name, "a" * 64))
    ctx = InputContext(store.root, "a" * 64, "b" * 64, "ref", "c" * 64, input_dir)
    output = algorithm.MICROBIAL.run(ctx, tmp_path / "out")
    metrics = {row["metric_id"]: row["value"] for row in output.metrics}
    assert metrics["primary_reads"] == 0
    assert metrics["microbial_rpm_primary"] is None


def test_run_refuses_symlinked_lab_temporary_directory(tmp_path, monkeypatch) -> None:
    store, _ = _database(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (store.lab / "tmp").symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(algorithm, "_tool", lambda name: (name, "a" * 64))
    ctx = InputContext(store.root, "a" * 64, "b" * 64, "ref", "c" * 64, tmp_path / "input")
    with pytest.raises(RuntimeError, match="temporary directory is unsafe"):
        algorithm.MICROBIAL.run(ctx, tmp_path / "out")
    assert list(outside.iterdir()) == []


def test_cohort_975_zero_fills_complete_absent_taxa_and_refuses_missing_reference(tmp_path, monkeypatch) -> None:
    from traceback_lab import microbial_view as view

    store = LabStore(tmp_path / "root")
    source, input_id, identity = "a" * 64, "b" * 64, "c" * 64
    prov = provenance(tool="fixture", parameters_sha256=identity, source_id=source,
                      input_id=input_id, reference_id="ref")
    summary = ResultEnvelope(algorithm_id="read-summary", algorithm_version="1", status="ok", provenance=prov)
    measured = ResultEnvelope(
        algorithm_id="microbial", algorithm_version="1", status="ok", provenance=prov,
        metrics=[Metric(metric_id="microbial_rpm_primary", value=4.0),
                 Metric(metric_id="taxon_123_rpm", value=4.0)],
    )
    sample = SampleView(source, source[:12], False,
                        InputView("full", input_id, None, {}, {"read-summary": summary, "microbial": measured}))
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    (artifacts / "microbial_profile.json").write_text(json.dumps({
        "schema": "traceback.lab.microbial-profile.v1", "source_id": source, "input_id": input_id,
        "database_sha256": "d" * 64, "database_source_label": "synthetic database",
        "read_length": 75, "denominator": 100,
        "candidate_unmapped_reads": 5, "omitted_taxa": 0,
        "taxa": [{"taxid": 123, "name": "Candidate species", "category": "bacteria",
                  "estimated_reads": 1.0, "rpm_primary": 4.0}],
    }))
    monkeypatch.setattr(view, "_tool", lambda name: (name, "e" * 64))
    monkeypatch.setattr(view, "inspect_database", lambda _store: SimpleNamespace(
        sha256="d" * 64, source_label="synthetic database", read_length=75))
    monkeypatch.setattr(view, "resolve_reference", lambda *_args, **_kwargs: SimpleNamespace(sha256="f" * 64))
    monkeypatch.setattr(view, "execution_identity", lambda *_args, **_kwargs: identity)
    monkeypatch.setattr(store, "selected_artifacts", lambda *_args: artifacts)
    members = []
    for index in range(10):
        result = CohortResult(
            algorithm_id="microbial", parameters_sha256=identity, cohort_compat_key=identity,
            metrics={"primary_reads": 100, "microbial_rpm_primary": 5.0, "omitted_taxa": 0,
                     **({"taxon_123_rpm": 5.0} if index == 9 else {})},
        )
        members.append(CohortMember(source_id=f"{index + 1:064x}", input_id=f"{index + 101:064x}",
                                    aligner_identity="same aligner", results=(result,)))
    members.append(CohortMember(
        source_id="e" * 64, input_id="f" * 64, aligner_identity="same aligner",
        results=(CohortResult(algorithm_id="microbial", parameters_sha256=identity,
                              cohort_compat_key=identity,
                              metrics={"primary_reads": 0, "microbial_rpm_primary": 0.0,
                                       "omitted_taxa": 0}),),
    ))
    generation = CohortGeneration(cohort_id="healthy-v1", version=1, members=tuple(members))
    target = CohortMember(source_id=source, input_id=input_id, aligner_identity="same aligner", results=())
    projected = profile_for_sample(store, sample, generation, target, "ready")
    assert projected["state"] == "ready"
    assert projected["taxa"][0]["cohort_n"] == 10
    assert projected["cohort_n"] == 10
    assert projected["taxa"][0]["cohort_p975"] == pytest.approx(3.875)
    assert projected["taxa"][0]["above_cohort_p975"] is True
    unavailable = profile_for_sample(store, sample, None, None, "not_built")
    assert unavailable["state"] == "cohort_unavailable" and unavailable["cohort_p975"] is None
    bad_profile = json.loads((artifacts / "microbial_profile.json").read_text())
    bad_profile["denominator"] = -1
    (artifacts / "microbial_profile.json").write_text(json.dumps(bad_profile))
    assert profile_for_sample(store, sample, generation, target, "ready")["state"] == "stored_profile_unavailable"
    bad_profile["denominator"] = 0
    bad_profile["candidate_unmapped_reads"] = 0
    (artifacts / "microbial_profile.json").write_text(json.dumps(bad_profile))
    absent = profile_for_sample(store, sample, generation, target, "ready")
    assert absent["state"] == "measurement_unavailable" and absent["taxa"] == []
