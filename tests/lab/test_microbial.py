"""Synthetic microbial classification and compatible-cohort contracts."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pysam
import pytest

from tests.lab.test_lab_server import _chrome

from traceback_lab.algorithms import microbial as algorithm
from traceback_lab import cohorts
from traceback_lab.cohort_contracts import CohortGeneration, CohortMember, CohortResult
from traceback_lab.envelope import Metric, ResultEnvelope, provenance
from traceback_lab.microbial_db import MicrobialDbError, inspect_database, register_database
from traceback_lab.microbial_view import profile_for_sample
from traceback_lab.registry import InputContext, ToolPin
from traceback_lab.registry import execution_identity
from traceback_lab.store import LabStore
from traceback_lab.views import InputView, SampleView


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _all_strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for child in value.values():
            yield from _all_strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from _all_strings(child)


def _database(tmp_path: Path) -> tuple[LabStore, Path]:
    store = LabStore(tmp_path / "root")
    directory = tmp_path / "kraken-db"
    directory.mkdir()
    for name in ("hash.k2d", "opts.k2d", "taxo.k2d", "database75mers.kmer_distrib"):
        (directory / name).write_bytes(f"synthetic {name}".encode())
    with store.writer("microbial db add", "r-20261016-0000-aabb", step="database"):
        register_database(store, directory, 75, "synthetic database")
    return store, directory


def _private_out(store: LabStore) -> Path:
    out = store.lab / "tmp" / "test-worker" / "artifacts"
    out.parent.mkdir(parents=True, exist_ok=True)
    return out


def test_planned_result_resolves_into_sample_profile_without_identity_stubs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, synthetic_world,
) -> None:
    """The runner and page resolver must agree on actual tool and reference pins."""
    from traceback_runner import cli as signed_cli
    from traceback_runner.references import register_reference
    from traceback_lab.ingest import source_ids
    from traceback_lab.views import load_sample
    from tests.lab.bam_fixtures import Read, write_bam

    store, _ = _database(tmp_path)
    register_reference(store.root, synthetic_world["fasta"], "synth")
    tools_dir = tmp_path / "tools"
    tools_dir.mkdir()
    kraken = tools_dir / "kraken2"
    kraken.write_text(
        '#!/bin/sh\nreport=""\nwhile [ "$#" -gt 0 ]; do\n'
        '  if [ "$1" = "--report" ]; then shift; report="$1"; fi\n'
        '  shift\ndone\nprintf "100.00\\t1\\t1\\tU\\t0\\tunclassified\\n" > "$report"\n'
    )
    bracken = tools_dir / "bracken"
    bracken.write_text("#!/bin/sh\nexit 99\n")
    for launcher in (kraken, bracken):
        launcher.chmod(0o755)
    (tools_dir / "classify").write_bytes(b"pinned synthetic helper")
    (tools_dir / "kraken2lib.pm").write_bytes(b"pinned synthetic library")
    (tools_dir / "src").mkdir()
    (tools_dir / "src" / "est_abundance.py").write_bytes(b"pinned synthetic Bracken helper")
    (tools_dir / "python").symlink_to(sys.executable)
    monkeypatch.setenv("PATH", f"{tools_dir}{os.pathsep}{os.environ['PATH']}")
    with pysam.FastaFile(str(synthetic_world["fasta"])) as fasta:
        contigs = list(zip(fasta.references, fasta.lengths))
    bam = write_bam(tmp_path / "aligned.bam", contigs, [
        Read("mapped", "chr1", 100, "A" * 75),
        Read("unmapped", "chr1", 0, "A" * 75, flag=4),
    ], platform="ONT")
    assert signed_cli.main(["lab", "run", str(bam), "--root", str(store.root),
                            "--only", "microbial", "--no-downsample", "--no-controls", "--json"]) == 0
    source = source_ids(store)[0]
    sample = load_sample(store, source)
    assert sample is not None and sample.full.results["microbial"].status == "ok"
    profile = profile_for_sample(store, sample, None, None, "not_built")
    assert profile is not None and profile["state"] == "cohort_unavailable"
    assert profile["candidate_unmapped_reads"] == 1
    assert profile["microbial_rpm_primary"] == 0


def test_outer_timeout_removes_private_candidate_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, synthetic_world,
) -> None:
    """A killed worker cannot leave read-level sequence beside runner scratch."""
    from traceback_runner import cli as signed_cli
    from traceback_runner.references import register_reference
    from tests.lab.bam_fixtures import Read, write_bam

    store, _ = _database(tmp_path)
    register_reference(store.root, synthetic_world["fasta"], "synth")
    tools_dir = tmp_path / "tools"
    tools_dir.mkdir()
    kraken = tools_dir / "kraken2"
    kraken.write_text('#!/bin/sh\ntouch "$(dirname "$0")/started"\nsleep 90\n')
    bracken = tools_dir / "bracken"
    bracken.write_text("#!/bin/sh\nexit 99\n")
    for launcher in (kraken, bracken):
        launcher.chmod(0o755)
    (tools_dir / "classify").write_bytes(b"pinned synthetic helper")
    (tools_dir / "kraken2lib.pm").write_bytes(b"pinned synthetic library")
    (tools_dir / "src").mkdir()
    (tools_dir / "src" / "est_abundance.py").write_bytes(b"pinned synthetic Bracken helper")
    (tools_dir / "python").symlink_to(sys.executable)
    monkeypatch.setenv("PATH", f"{tools_dir}{os.pathsep}{os.environ['PATH']}")
    with pysam.FastaFile(str(synthetic_world["fasta"])) as fasta:
        contigs = list(zip(fasta.references, fasta.lengths))
    bam = write_bam(tmp_path / "aligned.bam", contigs, [
        Read("mapped", "chr1", 100, "A" * 75),
        Read("candidate", "chr1", 0, "A" * 75, flag=4),
    ], platform="ONT")
    common = [str(bam), "--root", str(store.root), "--no-downsample", "--no-controls", "--json"]
    assert signed_cli.main(["lab", "run", *common, "--only", "read-summary"]) == 0
    signed_cli.main(["lab", "run", *common, "--only", "microbial", "--timeout-s", "15"])
    assert (tools_dir / "started").exists(), "Kraken must start before the timeout assertion"
    scratch = store.lab / "tmp"
    assert not list(scratch.glob("microbial-*"))
    assert not list(scratch.rglob("candidate.fa"))
    assert not list(scratch.rglob("kraken.output"))


def test_production_cohort_snapshot_excludes_unassayable_and_changed_db(tmp_path, monkeypatch) -> None:
    """The production selector must preserve microbial DB identity and zero semantics."""

    store = LabStore(tmp_path / "root")
    source_id, input_id, reference_sha = "a" * 64, "b" * 64, "c" * 64
    db_digest = ["d" * 64]
    summary = SimpleNamespace(id="read-summary", version="1", upstream=(), parameters={}, tools=[])
    pins = [ToolPin("kraken2", "f" * 64, "kraken2-mit")]
    microbial = SimpleNamespace(id="microbial", version="3", upstream=("read-summary",),
                                parameters={}, tools=pins, asset_sha256s=lambda root: db_digest)
    monkeypatch.setattr(cohorts, "load_registry", lambda: (summary, microbial))
    manifest = {"kind": "prealigned", "source_id": source_id, "reference_sha256": reference_sha}
    input_dir = store.inputs_dir / input_id
    input_dir.mkdir(parents=True)
    with pysam.AlignmentFile(str(input_dir / "aligned.bam"), "wb", header={
        "HD": {"VN": "1.6", "SO": "coordinate"},
        "SQ": [{"SN": "chr1", "LN": 1000}],
        "RG": [{"ID": "synthetic", "PL": "ONT"}],
    }):
        pass
    summary_identity = execution_identity(summary, reference_sha256=reference_sha)
    microbial_identity = execution_identity(
        microbial, reference_sha256=reference_sha, asset_sha256s=db_digest,
        upstream=[{"algorithm_id": "read-summary", "parameters_sha256": summary_identity}],
        tool_pins=[pin.identity() for pin in pins],
    )
    for item, identity in ((summary, summary_identity), (microbial, microbial_identity)):
        store.publish_result(ResultEnvelope(
            algorithm_id=item.id, algorithm_version=item.version, status="ok",
            provenance=provenance(tool="fixture", parameters_sha256=identity, source_id=source_id,
                                  input_id=input_id, reference_id="synthetic"),
            metrics=([Metric(metric_id="primary_reads", value=100),
                      Metric(metric_id="candidate_unmapped_reads", value=0),
                      Metric(metric_id="too_short_unmapped_reads", value=0),
                      Metric(metric_id="missing_sequence_unmapped_reads", value=0),
                      Metric(metric_id="microbial_rpm_primary", value=0.0)] if item.id == "microbial" else []),
        ), input_id=input_id)
    selected = cohorts._selected_results(store, input_id, manifest)
    assert [result.algorithm_id for result in selected] == ["read-summary", "microbial"]
    assert selected[1].cohort_compat_key != microbial_identity
    aligned = {**manifest, "kind": "aligned", "tools": {"minimap2": "one", "samtools": "one"},
               "mmi_sha256": "e" * 64, "aligner_argv": ["minimap2"]}
    assert (cohorts._selected_results(store, input_id, aligned)[1].cohort_compat_key
            != selected[1].cohort_compat_key)

    db_digest[0] = "e" * 64
    assert [result.algorithm_id for result in cohorts._selected_results(store, input_id, manifest)] == ["read-summary"]
    new_identity = execution_identity(
        microbial, reference_sha256=reference_sha, asset_sha256s=db_digest,
        upstream=[{"algorithm_id": "read-summary", "parameters_sha256": summary_identity}],
        tool_pins=[pin.identity() for pin in pins],
    )
    store.publish_result(ResultEnvelope(
        algorithm_id="microbial", algorithm_version="3", status="ok",
        provenance=provenance(tool="fixture", parameters_sha256=new_identity, source_id=source_id,
                              input_id=input_id, reference_id="synthetic"),
        metrics=[Metric(metric_id="primary_reads", value=100),
                 Metric(metric_id="candidate_unmapped_reads", value=0),
                 Metric(metric_id="too_short_unmapped_reads", value=0),
                 Metric(metric_id="missing_sequence_unmapped_reads", value=0),
                 Metric(metric_id="microbial_rpm_primary", value=0.0)],
    ), input_id=input_id)
    changed = cohorts._selected_results(store, input_id, manifest)[1]
    assert changed.parameters_sha256 == new_identity
    assert changed.cohort_compat_key != selected[1].cohort_compat_key
    assert (cohorts._selected_results(store, input_id, aligned)[1].cohort_compat_key
            != changed.cohort_compat_key)

    db_digest[0] = "d" * 64
    store.publish_result(ResultEnvelope(
        algorithm_id="microbial", algorithm_version="3", status="ok",
        provenance=provenance(tool="fixture", parameters_sha256=microbial_identity, source_id=source_id,
                              input_id=input_id, reference_id="synthetic"),
        metrics=[Metric(metric_id="primary_reads", value=100),
                 Metric(metric_id="candidate_unmapped_reads", value=0),
                 Metric(metric_id="too_short_unmapped_reads", value=2),
                 Metric(metric_id="missing_sequence_unmapped_reads", value=0),
                 Metric(metric_id="microbial_rpm_primary", value=0.0)],
    ), input_id=input_id)
    assert [result.algorithm_id for result in cohorts._selected_results(store, input_id, manifest)] == ["read-summary"]


def test_database_is_pinned_and_refuses_changed_bytes(tmp_path) -> None:
    store, directory = _database(tmp_path)
    registered = inspect_database(store)
    assert registered.read_length == 75 and len(registered.files) == 4
    (directory / "hash.k2d").write_bytes(b"different")
    with pytest.raises(MicrobialDbError, match="bytes changed"):
        inspect_database(store)


def test_sample_page_uses_bounded_receipt_and_reports_changed_database(tmp_path, monkeypatch) -> None:
    from traceback_lab import microbial_db as db, microbial_view as view

    store, directory = _database(tmp_path)
    database = inspect_database(store)
    source, input_id, identity = "a" * 64, "b" * 64, "c" * 64
    prov = provenance(tool="fixture", parameters_sha256=identity, source_id=source,
                      input_id=input_id, reference_id="ref")
    summary = ResultEnvelope(algorithm_id="read-summary", algorithm_version="1", status="ok", provenance=prov)
    measured = ResultEnvelope(algorithm_id="microbial", algorithm_version="3", status="ok", provenance=prov,
                                  metrics=[Metric(metric_id="primary_reads", value=1),
                                           Metric(metric_id="candidate_unmapped_reads", value=0),
                                           Metric(metric_id="too_short_unmapped_reads", value=0),
                                           Metric(metric_id="missing_sequence_unmapped_reads", value=0),
                                           Metric(metric_id="omitted_taxa", value=0),
                                           Metric(metric_id="microbial_estimated_reads", value=0.0),
                                           Metric(metric_id="microbial_rpm_primary", value=0.0)])
    sample = SampleView(source, source[:12], False,
                        InputView("full", input_id, None, {}, {"read-summary": summary, "microbial": measured}))
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    (artifacts / "microbial_profile.json").write_text(json.dumps({
        "schema": "traceback.lab.microbial-profile.v1", "source_id": source, "input_id": input_id,
        "database_sha256": database.sha256, "database_source_label": database.source_label,
        "read_length": database.read_length, "denominator": 1, "candidate_unmapped_reads": 0,
        "too_short_unmapped_reads": 0, "missing_sequence_unmapped_reads": 0,
        "omitted_taxa": 0, "taxa": [],
    }))
    tool_calls = []
    def pinned_tool(name):
        tool_calls.append(name)
        return name, "e" * 64
    monkeypatch.setattr(view, "_tool", pinned_tool)
    monkeypatch.setattr(view, "resolve_reference", lambda *_args, **_kwargs: SimpleNamespace(sha256="f" * 64))
    def pinned_identity(*_args, **kwargs):
        assert kwargs["tool_pins"] == [
            view.ToolPin("kraken2", "e" * 64, "kraken2-mit").identity(),
            view.ToolPin("bracken", "e" * 64, "bracken-gpl3").identity(),
        ]
        return identity
    monkeypatch.setattr(view, "execution_identity", pinned_identity)
    monkeypatch.setattr(store, "selected_result_artifacts", lambda *_args: (measured, artifacts))
    monkeypatch.setattr(db, "_digest_file", lambda _path: pytest.fail("page streamed database bytes"))

    assert profile_for_sample(store, sample, None, None, "not_built")["state"] == "cohort_unavailable"
    assert tool_calls == ["kraken2", "bracken"]
    missing_summary = SampleView(source, source[:12], False,
                                 InputView("full", input_id, None, {}, {"microbial": measured}))
    assert profile_for_sample(store, missing_summary, None, None, "not_built")["state"] == "upstream_unavailable"
    (directory / "hash.k2d").write_bytes(b"changed and different sized bytes")
    assert profile_for_sample(store, sample, None, None, "not_built")["state"] == "asset_unavailable"
    (directory / "hash.k2d").unlink()
    assert profile_for_sample(store, sample, None, None, "not_built")["state"] == "asset_unavailable"


def _profile_with_nonzero_exclusions(tmp_path: Path, monkeypatch) -> dict:
    from traceback_lab import microbial_view as view

    store = LabStore(tmp_path / "measure")
    source, input_id, summary_identity = "a" * 64, "b" * 64, "c" * 64
    monkeypatch.setattr(algorithm, "_tool", lambda name: (name, "e" * 64))
    monkeypatch.setattr(view, "_tool", lambda name: (name, "e" * 64))
    identity = execution_identity(
        algorithm.MICROBIAL, reference_sha256="f" * 64, asset_sha256s=["d" * 64],
        upstream=[{"algorithm_id": "read-summary", "parameters_sha256": summary_identity}],
    )
    summary_prov = provenance(tool="fixture", parameters_sha256=summary_identity, source_id=source,
                              input_id=input_id, reference_id="ref")
    measured_prov = provenance(tool="fixture", parameters_sha256=identity, source_id=source,
                               input_id=input_id, reference_id="ref")
    summary = ResultEnvelope(algorithm_id="read-summary", algorithm_version="1", status="ok",
                             provenance=summary_prov)
    measured = ResultEnvelope(algorithm_id="microbial", algorithm_version="3", status="ok",
                              provenance=measured_prov,
                              metrics=[Metric(metric_id="primary_reads", value=10),
                                       Metric(metric_id="candidate_unmapped_reads", value=5),
                                       Metric(metric_id="too_short_unmapped_reads", value=3),
                                       Metric(metric_id="missing_sequence_unmapped_reads", value=2),
                                       Metric(metric_id="omitted_taxa", value=0),
                                       Metric(metric_id="microbial_estimated_reads", value=0),
                                       Metric(metric_id="microbial_rpm_primary", value=0)])
    sample = SampleView(source, source[:12], False,
                        InputView("full", input_id, None, {}, {"read-summary": summary, "microbial": measured}))
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    (artifacts / "microbial_profile.json").write_text(json.dumps({
        "schema": "traceback.lab.microbial-profile.v1", "source_id": source, "input_id": input_id,
        "database_sha256": "d" * 64, "database_source_label": "anonymous synthetic database",
        "read_length": 75, "denominator": 10, "candidate_unmapped_reads": 5,
        "too_short_unmapped_reads": 3, "missing_sequence_unmapped_reads": 2,
        "omitted_taxa": 0, "taxa": [],
    }))
    monkeypatch.setattr(view, "inspect_database", lambda _store, **_kwargs: SimpleNamespace(
        sha256="d" * 64, source_label="anonymous synthetic database", read_length=75))
    monkeypatch.setattr(view, "resolve_reference", lambda *_args, **_kwargs: SimpleNamespace(sha256="f" * 64))
    monkeypatch.setattr(store, "selected_result_artifacts", lambda *_args: (measured, artifacts))
    return profile_for_sample(store, sample, None, None, "not_built")


def test_validated_microbe_exclusions_reach_profile_payload(tmp_path: Path, monkeypatch) -> None:
    profile = _profile_with_nonzero_exclusions(tmp_path, monkeypatch)
    assert profile["state"] == "cohort_unavailable"
    assert profile["candidate_unmapped_reads"] == 5
    assert profile["too_short_unmapped_reads"] == 3
    assert profile["missing_sequence_unmapped_reads"] == 2


def test_profile_refuses_artifact_from_newly_selected_attempt(tmp_path: Path, monkeypatch) -> None:
    from traceback_lab import microbial_view as view

    store = LabStore(tmp_path / "root")
    store.ensure()
    source, input_id, identity = "a" * 64, "b" * 64, "c" * 64
    prov = provenance(tool="fixture", parameters_sha256=identity, source_id=source,
                      input_id=input_id, reference_id="ref")
    summary = ResultEnvelope(algorithm_id="read-summary", algorithm_version="1", status="ok", provenance=prov)

    def result(denominator: int) -> ResultEnvelope:
        return ResultEnvelope(algorithm_id="microbial", algorithm_version="3", status="ok",
                              provenance=prov, metrics=[
                                  Metric(metric_id="primary_reads", value=denominator),
                                  Metric(metric_id="candidate_unmapped_reads", value=0),
                                  Metric(metric_id="too_short_unmapped_reads", value=0),
                                  Metric(metric_id="missing_sequence_unmapped_reads", value=0),
                                  Metric(metric_id="omitted_taxa", value=0),
                                  Metric(metric_id="microbial_estimated_reads", value=0.0),
                                  Metric(metric_id="microbial_rpm_primary", value=0.0)])

    def artifact(denominator: int) -> bytes:
        return json.dumps({
            "schema": "traceback.lab.microbial-profile.v1", "source_id": source, "input_id": input_id,
            "database_sha256": "d" * 64, "database_source_label": "synthetic database",
            "read_length": 75, "denominator": denominator, "candidate_unmapped_reads": 0,
            "too_short_unmapped_reads": 0, "missing_sequence_unmapped_reads": 0,
            "omitted_taxa": 0, "taxa": [],
        }).encode()

    first = result(1)
    store.publish_result(first, input_id=input_id, artifacts={"microbial_profile.json": artifact(1)})
    sample = SampleView(source, source[:12], False,
                        InputView("full", input_id, None, {}, {"read-summary": summary, "microbial": first}))
    monkeypatch.setattr(view, "_tool", lambda name: (name, "e" * 64))
    monkeypatch.setattr(view, "inspect_database", lambda *_args, **_kwargs: SimpleNamespace(
        sha256="d" * 64, source_label="synthetic database", read_length=75))
    monkeypatch.setattr(view, "resolve_reference", lambda *_args, **_kwargs: SimpleNamespace(sha256="f" * 64))
    monkeypatch.setattr(view, "execution_identity", lambda *_args, **_kwargs: identity)
    assert profile_for_sample(store, sample, None, None, "not_built")["state"] == "cohort_unavailable"
    store.publish_result(result(2), input_id=input_id, artifacts={"microbial_profile.json": artifact(2)})
    assert profile_for_sample(store, sample, None, None, "not_built")["state"] == "stored_profile_unavailable"


@pytest.fixture(scope="module")
def microbial_dom_root(tmp_path_factory, synthetic_world):
    from traceback_runner import cli as signed_cli
    from traceback_runner.references import register_reference
    from tests.lab.bam_fixtures import Read, write_bam

    base = tmp_path_factory.mktemp("microbial-page")
    root = base / "root"
    register_reference(root, synthetic_world["fasta"], "synth")
    with pysam.FastaFile(str(synthetic_world["fasta"])) as fasta:
        contigs = list(zip(fasta.references, fasta.lengths))
    bam = write_bam(base / "aligned.bam", contigs,
                    [Read("anonymous-synthetic", "chr1", 100, "A" * 75)])
    assert signed_cli.main(["lab", "run", str(bam), "--root", str(root),
                            "--only", "read-summary", "--no-downsample", "--no-controls", "--json"]) == 0
    return root


@pytest.mark.skipif(_chrome() is None, reason="no Chrome/Chromium for the headless DOM check")
def test_nonzero_microbe_exclusions_reach_api_and_dom(tmp_path: Path, monkeypatch, microbial_dom_root) -> None:
    from tests.lab.test_landing import _dom, _id12
    from traceback_lab.web import api as web_api
    from traceback_lab.web.server import RunningLabServer

    profile = _profile_with_nonzero_exclusions(tmp_path, monkeypatch)
    monkeypatch.setattr(web_api, "profile_for_sample", lambda *_args: profile)
    root = LabStore(microbial_dom_root)
    id12 = _id12(microbial_dom_root)
    payload = web_api.LabApi(root).sample(id12)
    assert payload["microbial_profile"]["too_short_unmapped_reads"] == 3
    assert payload["microbial_profile"]["missing_sequence_unmapped_reads"] == 2
    with RunningLabServer(root) as server:
        dom = _dom(server, tmp_path / "dom", route=f"/sample/{id12}/trust-qc",
                   want=lambda text: "3 unmapped reads were shorter" in text and "2 lacked sequence" in text)
    assert "3 unmapped reads were shorter" in dom
    assert "2 lacked sequence" in dom


def test_same_bytes_at_different_database_directory_are_refused(tmp_path) -> None:
    store, directory = _database(tmp_path)
    alternate = tmp_path / "alternate-db"
    alternate.mkdir()
    for file in directory.iterdir():
        (alternate / file.name).write_bytes(file.read_bytes())
    with store.writer("microbial db add", "r-20261016-0001-aabb", step="database"):
        with pytest.raises(MicrobialDbError, match="different directory"):
            register_database(store, alternate, 75, "synthetic database")


def test_symlinked_database_directory_is_refused_before_publication(tmp_path) -> None:
    _, directory = _database(tmp_path)
    linked = tmp_path / "db-link"
    linked.symlink_to(directory, target_is_directory=True)
    fresh = LabStore(tmp_path / "new-root")
    with fresh.writer("microbial db add", "r-20261016-0004-aabb", step="database"):
        with pytest.raises(MicrobialDbError, match="resolved directory"):
            register_database(fresh, linked, 75, "synthetic database")
    assert not (fresh.lab / "assets" / "microbial-db").exists()


def test_legacy_registration_upgrades_by_full_verification(tmp_path) -> None:
    store, directory = _database(tmp_path)
    manifest = store.lab / "assets" / "microbial-db" / "manifest.json"
    old = json.loads(manifest.read_text())
    old["schema"] = "traceback.lab.microbial-db.v1"
    del old["file_stats"]
    manifest.write_text(json.dumps(old))
    with pytest.raises(MicrobialDbError, match="re-registration"):
        inspect_database(store, verify_bytes=False)
    assert inspect_database(store).sha256 == old["sha256"]
    with store.writer("microbial db add", "r-20261016-0002-aabb", step="database"):
        upgraded = register_database(store, directory, 75, "synthetic database")
    assert upgraded.sha256 == old["sha256"]
    assert json.loads(manifest.read_text())["schema"] == "traceback.lab.microbial-db.v2"
    assert inspect_database(store, verify_bytes=False).sha256 == old["sha256"]


def test_reregistration_write_failure_preserves_manifest_and_hides_path(tmp_path, monkeypatch) -> None:
    from traceback_lab import store as store_module

    store, directory = _database(tmp_path)
    manifest = store.lab / "assets" / "microbial-db" / "manifest.json"
    original = manifest.read_bytes()
    with store.writer("microbial db add", "r-20261016-0003-aabb", step="database"):
        with monkeypatch.context() as patch:
            def fail_write(*_args, **_kwargs):
                raise OSError(f"private path: {manifest}")
            patch.setattr(store_module, "write_atomic", fail_write)
            with pytest.raises(MicrobialDbError, match="could not be updated") as caught:
                register_database(store, directory, 75, "synthetic database")
    assert str(manifest) not in str(caught.value)
    assert manifest.read_bytes() == original


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
    assert all(str(directory) not in value for value in _all_strings(added))
    assert cli.main(["lab", "microbial", "db", "inspect", "--root", str(root), "--json"]) == 0
    inspected = json.loads(capsys.readouterr().out)
    assert all(str(directory) not in value for value in _all_strings(inspected))


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

    bracken = tmp_path / "bracken"
    bracken.write_bytes(b"bracken launcher")
    source = tmp_path / "src" / "est_abundance.py"
    source.parent.mkdir()
    source.write_bytes(b"interpreter v1")
    python = tmp_path / "python"
    python.write_bytes(b"python interpreter v1")
    locations = {"bracken": bracken, "python": python}
    monkeypatch.setattr(algorithm.shutil, "which", lambda name: str(locations[name]))
    first_bracken = algorithm._tool("bracken")
    assert first_bracken is not None
    source.write_bytes(b"interpreter v2")
    second_bracken = algorithm._tool("bracken")
    assert second_bracken is not None and second_bracken[1] != first_bracken[1]
    python.write_bytes(b"python interpreter v2")
    third_bracken = algorithm._tool("bracken")
    assert third_bracken is not None and third_bracken[1] != second_bracken[1]


def test_installed_launcher_without_required_helpers_has_actionable_skip(tmp_path, monkeypatch) -> None:
    launcher = tmp_path / "kraken2"
    launcher.write_bytes(b"launcher")
    monkeypatch.setattr(algorithm.shutil, "which", lambda _name: str(launcher))
    ctx = InputContext(tmp_path, "a" * 64, "b" * 64, "ref", "c" * 64, tmp_path)
    result = algorithm.MICROBIAL.preflight(ctx)
    assert result.reason_code == "missing_toolchain"
    assert "upstream helpers" in result.message


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
    lineages, classified, unclassified, species_present = algorithm._report_lineages(report, 3)
    assert (classified, unclassified) == (2, 1)
    assert species_present
    assert lineages[123] == "bacteria"
    assert 9606 not in lineages


def test_human_only_species_does_not_require_bracken(tmp_path) -> None:
    report = tmp_path / "report"
    report.write_text(
        "0.00\t0\t0\tU\t0\tunclassified\n"
        "100.00\t1\t0\tR\t1\troot\n"
        "100.00\t1\t0\tD\t2759\t  Eukaryota\n"
        "100.00\t1\t1\tS\t9606\t    Homo sapiens\n"
    )
    lineages, classified, unclassified, species_present = algorithm._report_lineages(report, 1)
    assert (classified, unclassified) == (1, 0)
    assert 9606 not in lineages
    assert not species_present


def test_bracken_bounded_species_retains_total_and_refuses_malformed_rows(tmp_path: Path) -> None:
    header = "\t".join(algorithm._BRACKEN_COLUMNS) + "\n"
    rows = [f"Species {taxid}\t{taxid}\tS\t1\t0\t1.0\t0.003" for taxid in range(1, 301)]
    table = tmp_path / "bracken.tsv"
    table.write_text(header + "\n".join(rows) + "\n")
    displayed, total, omitted = algorithm._bracken_species(
        table, {taxid: "bacteria" for taxid in range(1, 301)}, 1000)
    assert len(displayed) == 256 and omitted == 44 and total == 300
    assert displayed[0]["rpm_primary"] == 1000
    assert 300 not in {row["taxid"] for row in displayed}

    for bad in (
        header + "Species 1\t1\tS\t1\t0\tnan\t0.1\n",
        header + "Species 1\t1\tS\t1\t0\t1\tinf\n",
        header + "Species 1\t1\tS\t1\t0\t1\n",
        header + "Species 1\t1\tS\t-1\t0\t1\t0.1\n",
        header.replace("added_reads", "unexpected") + rows[0] + "\n",
    ):
        table.write_text(bad)
        with pytest.raises(RuntimeError, match="Bracken output"):
            algorithm._bracken_species(table, {1: "bacteria"}, 1000)


def test_kraken_accounting_must_cover_all_submitted_candidates(tmp_path: Path) -> None:
    report = tmp_path / "kraken.report"
    report.write_text("100.00\t1\t1\tU\t0\tunclassified\n")
    with pytest.raises(RuntimeError, match="account for submitted"):
        algorithm._report_lineages(report, 2)


def test_microbe_rate_and_display_count_have_distinct_stability_policies() -> None:
    from traceback_lab.interpretation.engine import load_library

    library = load_library()
    assert library.entry("microbial", "microbial_rpm_primary").stability["kind"] == "relative"
    assert library.entry("microbial", "primary_reads").stability["kind"] == "scales_with_depth"
    assert library.entry("microbial", "omitted_taxa").stability["kind"] == "absolute"


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
    output = algorithm.MICROBIAL.run(ctx, _private_out(store))
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
    output = algorithm.MICROBIAL.run(ctx, _private_out(store))
    metrics = {row["metric_id"]: row["value"] for row in output.metrics}
    assert len(calls) == 1
    assert metrics["microbial_rpm_primary"] == 0
    assert metrics["unclassified_candidate_reads"] == 1
    assert b"private-name" not in output.artifacts["microbial_profile.json"]


def test_classified_above_species_level_yields_zero_without_bracken(tmp_path, monkeypatch) -> None:
    store, _ = _database(tmp_path)
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    read = pysam.AlignedSegment()
    read.query_name = "private-name"
    read.query_sequence = "A" * 75
    read.flag = 4
    read.reference_id = -1
    read.reference_start = -1
    with pysam.AlignmentFile(input_dir / "aligned.bam", "wb",
                             header={"HD": {"VN": "1.6"}, "SQ": [{"SN": "chr1", "LN": 1000}]}) as stream:
        stream.write(read)
    monkeypatch.setattr(algorithm, "_tool", lambda name: (name, "a" * 64))
    calls = []

    def fake_run(argv, **_kwargs):
        calls.append(argv)
        assert "--report" in argv, "Bracken cannot estimate absent species rows"
        Path(argv[argv.index("--report") + 1]).write_text(
            "0.00\t0\t0\tU\t0\tunclassified\n"
            "100.00\t1\t0\tR\t1\troot\n"
            "100.00\t1\t0\tD\t2\t  Bacteria\n"
            "100.00\t1\t1\tG\t123\t    Candidate genus\n")
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(algorithm.subprocess, "run", fake_run)
    ctx = InputContext(store.root, "a" * 64, "b" * 64, "ref", "c" * 64, input_dir)
    output = algorithm.MICROBIAL.run(ctx, _private_out(store))
    metrics = {row["metric_id"]: row["value"] for row in output.metrics}
    assert len(calls) == 1
    assert metrics["classified_candidate_reads"] == 1
    assert metrics["microbial_rpm_primary"] == 0


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
    output = algorithm.MICROBIAL.run(ctx, _private_out(store))
    metrics = {row["metric_id"]: row["value"] for row in output.metrics}
    assert metrics["primary_reads"] == 0
    assert metrics["microbial_rpm_primary"] is None


@pytest.mark.parametrize("sequence,missing,short", [("A" * 50, 0, 1), (None, 1, 0)])
def test_unassayable_unmapped_reads_are_unavailable(tmp_path, monkeypatch, sequence, missing, short) -> None:
    store, _ = _database(tmp_path)
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    read = pysam.AlignedSegment()
    read.query_name = "private-name"
    read.query_sequence = sequence
    read.flag = 4
    read.reference_id = -1
    read.reference_start = -1
    with pysam.AlignmentFile(input_dir / "aligned.bam", "wb", header={"HD": {"VN": "1.6"}}) as bam:
        bam.write(read)
    monkeypatch.setattr(algorithm, "_tool", lambda name: (name, "a" * 64))
    ctx = InputContext(store.root, "a" * 64, "b" * 64, "ref", "c" * 64, input_dir)
    output = algorithm.MICROBIAL.run(ctx, _private_out(store))
    metrics = {row["metric_id"]: row["value"] for row in output.metrics}
    assert metrics["primary_reads"] == 1
    assert metrics["candidate_unmapped_reads"] == 0
    assert metrics["too_short_unmapped_reads"] == short
    assert metrics["missing_sequence_unmapped_reads"] == missing
    assert metrics["microbial_estimated_reads"] is None
    assert metrics["microbial_rpm_primary"] is None
    assert b"private-name" not in output.artifacts["microbial_profile.json"]


def test_all_human_mapped_reads_are_measured_zero(tmp_path, monkeypatch) -> None:
    store, _ = _database(tmp_path)
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    read = pysam.AlignedSegment()
    read.query_name = "private-name"
    read.query_sequence = "A" * 75
    read.flag = 0
    read.reference_id = 0
    read.reference_start = 0
    read.cigar = ((0, 75),)
    header = {"HD": {"VN": "1.6"}, "SQ": [{"SN": "chr1", "LN": 1000}]}
    with pysam.AlignmentFile(input_dir / "aligned.bam", "wb", header=header) as bam:
        bam.write(read)
    monkeypatch.setattr(algorithm, "_tool", lambda name: (name, "a" * 64))
    ctx = InputContext(store.root, "a" * 64, "b" * 64, "ref", "c" * 64, input_dir)
    output = algorithm.MICROBIAL.run(ctx, _private_out(store))
    metrics = {row["metric_id"]: row["value"] for row in output.metrics}
    assert metrics["candidate_unmapped_reads"] == 0
    assert metrics["microbial_rpm_primary"] == 0


def test_run_refuses_symlinked_lab_temporary_directory(tmp_path, monkeypatch) -> None:
    store, _ = _database(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (store.lab / "tmp").symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(algorithm, "_tool", lambda name: (name, "a" * 64))
    ctx = InputContext(store.root, "a" * 64, "b" * 64, "ref", "c" * 64, tmp_path / "input")
    with pytest.raises(RuntimeError, match="temporary directory is unsafe"):
        algorithm.MICROBIAL.run(ctx, store.lab / "tmp" / "test-worker" / "artifacts")
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
        metrics=[Metric(metric_id="primary_reads", value=100),
                 Metric(metric_id="candidate_unmapped_reads", value=5),
                 Metric(metric_id="too_short_unmapped_reads", value=0),
                 Metric(metric_id="missing_sequence_unmapped_reads", value=0),
                 Metric(metric_id="omitted_taxa", value=0),
                 Metric(metric_id="microbial_estimated_reads", value=0.0004),
                 Metric(metric_id="microbial_rpm_primary", value=4.0),
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
        "candidate_unmapped_reads": 5, "too_short_unmapped_reads": 0,
        "missing_sequence_unmapped_reads": 0, "omitted_taxa": 0,
        "taxa": [{"taxid": 123, "name": "Candidate species", "category": "bacteria",
                  "estimated_reads": 0.0004, "rpm_primary": 4.0}],
    }))
    monkeypatch.setattr(view, "_tool", lambda name: (name, "e" * 64))
    monkeypatch.setattr(view, "inspect_database", lambda _store, **_kwargs: SimpleNamespace(
        sha256="d" * 64, source_label="synthetic database", read_length=75))
    monkeypatch.setattr(view, "resolve_reference", lambda *_args, **_kwargs: SimpleNamespace(sha256="f" * 64))
    monkeypatch.setattr(view, "execution_identity", lambda *_args, **_kwargs: identity)
    monkeypatch.setattr(store, "selected_result_artifacts", lambda *_args: (measured, artifacts))
    members = []
    for index in range(10):
        result = CohortResult(
            algorithm_id="microbial", parameters_sha256=identity, cohort_compat_key=identity,
            metrics={"primary_reads": 100, "candidate_unmapped_reads": 5,
                     "too_short_unmapped_reads": 0, "missing_sequence_unmapped_reads": 0,
                     "microbial_rpm_primary": 5.0, "omitted_taxa": 0,
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
    members.append(CohortMember(
        source_id="d" * 64, input_id="e" * 64, aligner_identity="same aligner",
        results=(CohortResult(algorithm_id="microbial", parameters_sha256=identity,
                              cohort_compat_key=identity,
                              metrics={"primary_reads": 100, "candidate_unmapped_reads": 0,
                                       "too_short_unmapped_reads": 2,
                                       "missing_sequence_unmapped_reads": 0,
                                       "microbial_rpm_primary": 0.0, "omitted_taxa": 0}),),
    ))
    generation = CohortGeneration(cohort_id="healthy-v1", version=1, members=tuple(members))
    target = CohortMember(
        source_id=source, input_id=input_id, aligner_identity="same aligner",
        results=(CohortResult(algorithm_id="microbial", parameters_sha256=identity,
                              cohort_compat_key=identity, metrics={"primary_reads": 100,
                                                                  "microbial_rpm_primary": 4.0}),),
    )
    projected = profile_for_sample(store, sample, generation, target, "ready")
    assert projected["state"] == "ready"
    assert projected["taxa"][0]["cohort_n"] == 10
    assert projected["cohort_n"] == 10
    assert projected["taxa"][0]["cohort_p975"] == pytest.approx(3.875)
    assert projected["taxa"][0]["above_cohort_p975"] is True
    incompatible = list(members)
    first = members[0]
    incompatible[0] = CohortMember(
        source_id=first.source_id, input_id=first.input_id,
        aligner_identity=first.aligner_identity,
        results=(CohortResult(algorithm_id="microbial", parameters_sha256=identity,
                              cohort_compat_key="0" * 64, metrics=first.results[0].metrics),),
    )
    mismatched_generation = CohortGeneration(cohort_id="healthy-v1", version=2,
                                             members=tuple(incompatible))
    assert profile_for_sample(store, sample, mismatched_generation, target, "ready")["cohort_n"] == 9
    # An omitted taxon cannot be interpreted as a measured zero in that
    # member; a tenth compatible value is then unavailable for this species.
    limited_members = list(members)
    result = first.results[0]
    limited_members[0] = CohortMember(
        source_id=first.source_id, input_id=first.input_id,
        aligner_identity=first.aligner_identity,
        results=(CohortResult(algorithm_id="microbial", parameters_sha256=identity,
                              cohort_compat_key=identity,
                              metrics={**result.metrics, "omitted_taxa": 1}),),
    )
    limited_generation = CohortGeneration(cohort_id="healthy-v1", version=2,
                                          members=tuple(limited_members))
    limited = profile_for_sample(store, sample, limited_generation, target, "ready")
    assert limited["taxa"][0]["cohort_n"] == 9
    assert limited["taxa"][0]["cohort_p975"] is None
    invalid_members = list(members)
    invalid_members[0] = CohortMember(
        source_id=first.source_id, input_id=first.input_id,
        aligner_identity=first.aligner_identity,
        results=(CohortResult(algorithm_id="microbial", parameters_sha256=identity,
                              cohort_compat_key=identity,
                              metrics={**result.metrics, "taxon_123_rpm": -1.0}),),
    )
    invalid_generation = CohortGeneration(cohort_id="healthy-v1", version=3,
                                          members=tuple(invalid_members))
    invalid = profile_for_sample(store, sample, invalid_generation, target, "ready")
    assert invalid["taxa"][0]["cohort_n"] == 9
    assert invalid["taxa"][0]["cohort_p975"] is None
    missing_target = profile_for_sample(store, sample, generation, None, "ready")
    assert missing_target["state"] == "cohort_unavailable"
    assert "target" in missing_target["reason"].lower()
    unavailable = profile_for_sample(store, sample, None, None, "not_built")
    assert unavailable["state"] == "cohort_unavailable" and unavailable["cohort_p975"] is None
    changed = measured.model_copy(update={"metrics": [
        *measured.metrics[:-1], Metric(metric_id="taxon_123_rpm", value=9.0)]})
    monkeypatch.setattr(store, "selected_result_artifacts", lambda *_args: (changed, artifacts))
    assert profile_for_sample(store, sample, generation, target, "ready")["state"] == "stored_profile_unavailable"
    monkeypatch.setattr(store, "selected_result_artifacts", lambda *_args: (measured, artifacts))
    bad_profile = json.loads((artifacts / "microbial_profile.json").read_text())
    bad_profile["taxa"][0]["rpm_primary"] = 9.0
    (artifacts / "microbial_profile.json").write_text(json.dumps(bad_profile))
    assert profile_for_sample(store, sample, generation, target, "ready")["state"] == "stored_profile_unavailable"
    bad_profile["taxa"][0]["rpm_primary"] = 4.0
    bad_profile["candidate_unmapped_reads"] = 6
    (artifacts / "microbial_profile.json").write_text(json.dumps(bad_profile))
    assert profile_for_sample(store, sample, generation, target, "ready")["state"] == "stored_profile_unavailable"
    bad_profile["candidate_unmapped_reads"] = 5
    bad_profile["denominator"] = -1
    (artifacts / "microbial_profile.json").write_text(json.dumps(bad_profile))
    assert profile_for_sample(store, sample, generation, target, "ready")["state"] == "stored_profile_unavailable"
    bad_profile["denominator"] = 0
    bad_profile["candidate_unmapped_reads"] = 0
    (artifacts / "microbial_profile.json").write_text(json.dumps(bad_profile))
    absent = profile_for_sample(store, sample, generation, target, "ready")
    assert absent["state"] == "stored_profile_unavailable" and absent["taxa"] == []
    bad_profile["denominator"] = 100
    bad_profile["too_short_unmapped_reads"] = 1
    (artifacts / "microbial_profile.json").write_text(json.dumps(bad_profile))
    unassayable = profile_for_sample(store, sample, generation, target, "ready")
    assert unassayable["state"] == "stored_profile_unavailable" and unassayable["taxa"] == []
