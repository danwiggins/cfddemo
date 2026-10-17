"""Platform declarations control Lab alignment and result compatibility."""

from pathlib import Path
import json
from types import SimpleNamespace

import pysam
import pytest

from traceback_lab.platforms import for_input, from_rg_lines
from traceback_runner import cli as signed_cli
from traceback_runner.references import register_reference


@pytest.mark.parametrize("lines,expected", [
    (["@RG\tID:a\tPL:ONT"], "ont"),
    (["@RG\tID:a\tPL:OXFORD_NANOPORE"], "ont"),
    (["@RG\tID:a\tPL:ILLUMINA"], "illumina"),
    (["@RG\tID:a\tPL:ONT", "@RG\tID:b\tPL:ILLUMINA"], "unknown"),
    (["@RG\tID:a"], "unknown"),
    ([], "unknown"),
])
def test_read_group_platform(lines: list[str], expected: str) -> None:
    assert from_rg_lines(lines) == expected


@pytest.mark.parametrize("platform", [None, "unknown", "pacbio"])
def test_worker_context_refuses_missing_or_unsupported_platform(platform: str | None) -> None:
    from traceback_lab.registry import context_from_json

    payload = {
        "root": "/tmp/lab", "source_id": "s", "input_id": "i",
        "reference_id": "hg38", "reference_sha256": "r", "input_dir": "/tmp/lab/input",
    }
    if platform is not None:
        payload["platform"] = platform
    with pytest.raises(ValueError, match="no supported platform"):
        context_from_json(payload)


def test_derived_platform_skip_is_persisted_and_shown_in_depth_curve(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from traceback_lab import controls, views
    from traceback_lab.algorithms.short_read_fragment_size import SHORT_READ_FRAGMENT_SIZE
    from traceback_lab.derived import DerivedInput, downsample
    from traceback_lab.registry import PlanItem
    from traceback_lab.runner import RunTarget, StepResult, run_plan
    from traceback_lab.scrub import Scrubber
    from traceback_lab.store import LabStore

    store = LabStore(tmp_path / "store")
    store.ensure()
    log = store.run_log("r-20261017-1200-aaaa")
    parent = RunTarget(input_id="a" * 64, source_id="b" * 64, input_dir=tmp_path,
                       reference_id="hg38", reference_sha256="c" * 64,
                       platform="ont", eligible_reads=5_000_000, approx_depth_x=10.0)
    algorithm = SHORT_READ_FRAGMENT_SIZE
    monkeypatch.setattr(algorithm, "preflight", lambda _: pytest.fail("platform skip must precede preflight"))
    monkeypatch.setattr(algorithm, "run", lambda *_: pytest.fail("platform skip must not run worker"))
    items = [PlanItem(algorithm, False)]
    entries = {algorithm.id: "traceback_lab.algorithms.short_read_fragment_size:SHORT_READ_FRAGMENT_SIZE"}
    scrubber = Scrubber(root=store.root)
    full, = run_plan(store, None, log, parent, items, entries, run_id=log.run_id, scrubber=scrubber)
    assert (full.status, full.skip_reason) == ("skipped", "platform")
    from traceback_lab.registry import execution_identity
    assert full.parameters_sha256 == execution_identity(
        algorithm, reference_sha256=parent.reference_sha256,
    )

    monkeypatch.setattr(controls.ReadSummary, "open", lambda *_: object())
    monkeypatch.setattr(controls, "derived_mask", lambda *_: None)
    monkeypatch.setattr(controls, "depth_stamp", lambda *_: {
        "eligible_reads": 2_500_000, "mean_span_bp": 150.0, "approx_depth_x": 5.0,
    })
    monkeypatch.setattr(controls, "record_derived", lambda *_: None)

    def materialize(_store, parent_id, _bam, specs, **_kwargs):
        return {spec.label(): DerivedInput(spec, spec.input_id(parent_id), parent_id, tmp_path)
                for spec in specs}

    monkeypatch.setattr(controls, "materialize", materialize)
    summary = StepResult("read-summary", "ok", True, "d" * 64, artifacts_dir=str(tmp_path))
    derived, = controls.run_derived(
        store, None, log, parent, [downsample(0.5)], items, entries,
        run_id=log.run_id, read_summary=summary, scrubber=scrubber, timeout_s=30,
    )
    result, = derived.results
    assert (result.status, result.skip_reason) == ("skipped", "platform")
    persisted = store.read_result(derived.derived.input_id, algorithm.id, result.parameters_sha256)
    assert persisted is not None and persisted.status == "skipped" and persisted.skip_reason == "platform"
    sample = views.SampleView(
        source_id=parent.source_id, id12=parent.source_id[:12], example=False,
        full=views.InputView("full", parent.input_id, None, {}, views.selected_results(store, parent.input_id)),
        derived=[views.InputView("0.5", derived.derived.input_id, "downsample", {"fraction": 0.5},
                                 views.selected_results(store, derived.derived.input_id), 0.5)],
    )
    row = next(row for row in views.depth_curve(sample)["rows"] if row["algorithm_id"] == algorithm.id)
    assert row["cells"][0]["state"] == "skipped" and row["cells"][0]["reason"] == "platform"


def test_each_unaligned_chunk_must_declare_the_platform() -> None:
    from traceback_lab.ingest import Chunk, ChunkSet, _refuse_mixed_models
    from traceback_lab.problems import LabProblem
    from traceback_lab.run_command import _declared_platform

    declared = Chunk(Path("declared.bam"), "declared.bam", 1, 1,
                     rg_lines=("@RG\tID:one\tPL:ONT",))
    missing = Chunk(Path("missing.bam"), "missing.bam", 2, 1)
    # Neither header declares a basecall model, so the older model check
    # cannot substitute for per-chunk platform verification.
    _refuse_mixed_models((declared, missing))
    with pytest.raises(LabProblem, match="TBX-LAB-024"):
        _declared_platform(ChunkSet([declared, missing]))

    second = Chunk(Path("second.bam"), "second.bam", 2, 1,
                   rg_lines=("@RG\tID:two\tPL:OXFORD_NANOPORE",))
    assert _declared_platform(ChunkSet([declared, second])) == "ont"


def test_stored_platform_must_match_bam_header(tmp_path: Path) -> None:
    bam_path = tmp_path / "aligned.bam"
    header = {"HD": {"VN": "1.6"}, "SQ": [{"SN": "chr1", "LN": 1000}],
              "RG": [{"ID": "one", "PL": "ILLUMINA"}]}
    with pysam.AlignmentFile(str(bam_path), "wb", header=header):
        pass
    assert for_input({"kind": "prealigned"}, bam_path) == "illumina"
    assert for_input({"kind": "prealigned", "platform": "illumina"}, bam_path) == "illumina"
    with pytest.raises(ValueError, match="differs"):
        for_input({"kind": "prealigned", "platform": "ont"}, bam_path)
    with pytest.raises(ValueError, match="does not declare ONT"):
        for_input({"kind": "aligned", "platform": "ont"}, bam_path)


def test_duplicate_platform_tags_are_unknown() -> None:
    from traceback_lab.platforms import from_rg_lines

    assert from_rg_lines(["@RG\tID:one\tPL:ONT\tPL:ILLUMINA"]) == "unknown"
    assert from_rg_lines(["@RG\tID:one\tPL:ILLUMINA\tPL:ONT"]) == "unknown"


def test_legacy_illumina_source_record_keeps_header_platform(tmp_path: Path) -> None:
    from traceback_lab.ingest import write_source
    from traceback_lab.store import LabStore

    store = LabStore(tmp_path)
    store.ensure()
    chunks = SimpleNamespace(chunks=[], excluded_indexes=[], basecall_model=None,
                             rg_lines=["@RG\tID:legacy\tPL:ILLUMINA"])
    aligned = SimpleNamespace(source_id="a" * 64, input_id="b" * 64,
                              manifest={"kind": "prealigned", "reference_id": "hg38", "tools": None})
    record = write_source(store, chunks, aligned)
    assert record["inputs"][0]["platform"] == "illumina"


@pytest.mark.parametrize("platforms,kind,declared,expected", [
    (("ILLUMINA",), "prealigned", None, "skipped"),
    ((None,), "prealigned", None, "skipped"),
    (("ONT",), "prealigned", None, "ok"),
    (("ILLUMINA",), "aligned", None, "skipped"),
    ((None,), "prealigned", "unknown", "skipped"),
    ((), "aligned", None, "ok"),
])
def test_stored_view_qualifies_unverified_platform_before_rerun(
    tmp_path: Path, platforms: tuple[str | None, ...], kind: str, declared: str | None, expected: str,
) -> None:
    import shutil

    from traceback_lab.envelope import ResultEnvelope, provenance
    from traceback_lab.ingest import ALIGNER_IDENTITY_ARGV
    from traceback_lab.store import LabStore
    from traceback_lab.interpretation.engine import load_library
    from traceback_lab.views import InputView, cell_state, load_sample, selected_results
    from traceback_lab.web.api import LabApi

    root, bam_path = _prealigned_world(tmp_path, platforms)
    store = LabStore(root)
    store.ensure()
    source_id, input_id = "b" * 64, "c" * 64
    input_dir = store.inputs_dir / input_id
    input_dir.mkdir(parents=True)
    shutil.copyfile(bam_path, input_dir / "aligned.bam")
    manifest = {"kind": kind, "reference_id": "hg38"}
    if declared is not None:
        manifest["platform"] = declared
    if kind == "aligned":
        manifest["aligner_argv"] = list(ALIGNER_IDENTITY_ARGV)
    (input_dir / "manifest.json").write_text(json.dumps(manifest))
    store.sources_dir.mkdir(parents=True)
    (store.sources_dir / f"{source_id}.json").write_text(json.dumps({
        "schema": "traceback.lab.source.v1", "source_id": source_id,
        "inputs": [{"input_id": input_id, "kind": kind, "used_utc": "2026-10-16T00:00:00Z"}],
        "derived": [],
    }))
    old = ResultEnvelope.model_validate({
        "algorithm_id": "fragment-size", "algorithm_version": "1", "status": "ok",
        "provenance": provenance(
            tool="legacy", parameters_sha256="a" * 64, source_id=source_id,
            input_id=input_id, reference_id="hg38",
        ),
        "metrics": [{"metric_id": "mode_bp", "value": 100}],
    })
    store.publish_result(old, input_id=input_id)
    raw = selected_results(store, input_id)
    assert raw["fragment-size"].status == "ok"
    old_view = InputView("full", input_id, None, {}, raw)
    assert cell_state(old_view, old_view, "fragment-size", "mode_bp", load_library())["state"] == "ok_usable"

    sample = load_sample(store, source_id)
    assert sample is not None
    assert sample.full.results["fragment-size"].status == expected
    if expected == "skipped":
        assert sample.full.results["fragment-size"].skip_reason == "platform"
    response = LabApi(store).sample(source_id[:12])
    visible = next(item for item in response["algorithms"] if item["algorithm_id"] == "fragment-size")
    assert visible["status"] == expected
    if expected == "skipped":
        assert visible["skip_reason"] == "platform"
    assert store.read_result(input_id, "fragment-size", "a" * 64) == old


def test_legacy_lab_aligned_without_read_groups_uses_pinned_map_ont(tmp_path: Path) -> None:
    from traceback_lab.ingest import ALIGNER_IDENTITY_ARGV

    bam_path = tmp_path / "legacy.bam"
    header = {"HD": {"VN": "1.6"}, "SQ": [{"SN": "chr1", "LN": 1000}]}
    with pysam.AlignmentFile(str(bam_path), "wb", header=header):
        pass
    old_manifest = {"kind": "aligned", "aligner_argv": list(ALIGNER_IDENTITY_ARGV)}
    assert for_input(old_manifest, bam_path) == "ont"
    with pytest.raises(ValueError, match="does not declare ONT"):
        for_input({**old_manifest, "platform": "ont"}, bam_path)
    with pytest.raises(ValueError, match="does not declare ONT"):
        for_input({"kind": "aligned", "aligner_argv": ["other"]}, bam_path)


@pytest.mark.parametrize("groups", [
    [{"ID": "one", "PL": "PROMETHION"}],
    [{"ID": "one"}, {"ID": "two", "PL": "nanopore"}],
])
def test_legacy_lab_aligned_with_unknown_read_group_platform_uses_exact_recipe(
    tmp_path: Path, groups: list[dict[str, str]],
) -> None:
    from traceback_lab.ingest import aligner_argv_identity

    bam_path = tmp_path / "legacy-rg.bam"
    header = {"HD": {"VN": "1.6"}, "SQ": [{"SN": "chr1", "LN": 1000}], "RG": groups}
    with pysam.AlignmentFile(str(bam_path), "wb", header=header):
        pass
    with pysam.AlignmentFile(str(bam_path), "rb") as bam:
        rg_lines = [line for line in str(bam.header).splitlines() if line.startswith("@RG\t")]
    old_manifest = {"kind": "aligned", "aligner_argv": aligner_argv_identity(rg_lines)}
    assert for_input(old_manifest, bam_path) == "ont"
    with pytest.raises(ValueError, match="does not declare ONT"):
        for_input({**old_manifest, "platform": "ont"}, bam_path)
    with pytest.raises(ValueError, match="does not declare ONT"):
        for_input({**old_manifest, "aligner_argv": ["other"]}, bam_path)


def test_legacy_lab_aligned_rejects_explicit_illumina_in_mixed_unknown_read_groups(tmp_path: Path) -> None:
    from traceback_lab.ingest import aligner_argv_identity

    bam_path = tmp_path / "mixed-rg.bam"
    header = {"HD": {"VN": "1.6"}, "SQ": [{"SN": "chr1", "LN": 1000}],
              "RG": [{"ID": "one", "PL": "ILLUMINA"}, {"ID": "two"}]}
    with pysam.AlignmentFile(str(bam_path), "wb", header=header):
        pass
    with pysam.AlignmentFile(str(bam_path), "rb") as bam:
        rg_lines = [line for line in str(bam.header).splitlines() if line.startswith("@RG\t")]
    with pytest.raises(ValueError, match="does not declare ONT"):
        for_input({"kind": "aligned", "aligner_argv": aligner_argv_identity(rg_lines)}, bam_path)


@pytest.mark.parametrize("platform", ["ILLUMINA", None])
def test_unaligned_non_ont_is_refused_before_alignment(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], platform: str | None,
) -> None:
    from traceback_lab.synthetic import rg_line, write_chunks, write_reference

    reference = write_reference(tmp_path / "reference.fa", length=1000)
    root = tmp_path / "root"
    register_reference(root, reference.fasta, "synth")
    group = rg_line()
    if platform is None:
        group.pop("PL")
    else:
        group["PL"] = platform
    chunk = write_chunks(tmp_path / "chunks", reference, chunks=1, reads_per_chunk=1, rg=group)[0]
    code = signed_cli.main(["lab", "run", str(chunk), "--root", str(root), "--dry-run", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 3
    assert payload["data"]["code"] == "TBX-LAB-024"


def _prealigned_world(tmp_path: Path, platforms: tuple[str | None, ...]) -> tuple[Path, Path]:
    reference = tmp_path / "reference.fa"
    reference.write_text(">chr1\n" + "A" * 1000 + "\n")
    pysam.faidx(str(reference))
    root = tmp_path / "root"
    register_reference(root, reference, "hg38")
    bam_path = tmp_path / "aligned.bam"
    header = {"HD": {"VN": "1.6"}, "SQ": [{"SN": "chr1", "LN": 1000}],
              "RG": [{"ID": f"group-{index}", **({"PL": platform} if platform else {})}
                     for index, platform in enumerate(platforms)]}
    with pysam.AlignmentFile(str(bam_path), "wb", header=header) as bam:
        read = pysam.AlignedSegment()
        read.query_name = "synthetic-read"
        read.query_sequence = "A" * 100
        read.flag = 0
        read.reference_id = 0
        read.reference_start = 100
        read.mapping_quality = 60
        read.cigarstring = "100M"
        bam.write(read)
    return root, bam_path


@pytest.mark.parametrize("platforms", [(), (None,), ("ONT", "ILLUMINA"), ("PACBIO",)])
@pytest.mark.parametrize("dry_run", [False, True])
def test_unknown_prealigned_platform_is_blocked_in_normal_and_dry_runs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], platforms: tuple[str | None, ...], dry_run: bool,
) -> None:
    root, bam_path = _prealigned_world(tmp_path, platforms)
    argv = ["lab", "run", str(bam_path), "--root", str(root), "--reference", "hg38", "--json"]
    if dry_run:
        argv.append("--dry-run")
    code = signed_cli.main(argv)
    payload = json.loads(capsys.readouterr().out)
    assert code == 3
    assert payload["data"]["code"] == "TBX-LAB-024"


def test_prealigned_writer_refuses_unknown_platform_before_publishing(tmp_path: Path) -> None:
    from traceback_lab.ingest import ingest_prealigned, inspect_chunks, resolve_reference
    from traceback_lab.problems import LabProblem
    from traceback_lab.store import LabStore

    root, bam_path = _prealigned_world(tmp_path, (None,))
    store = LabStore(root)
    chunkset = inspect_chunks([bam_path], store, write_cache=False)
    reference = resolve_reference(root, "hg38")
    with pytest.raises(LabProblem, match="TBX-LAB-024"):
        ingest_prealigned(store, chunkset, reference)
    assert not store.inputs_dir.exists() or not list(store.inputs_dir.iterdir())


def test_prealigned_writer_requires_one_bam_even_when_one_declares_ont(tmp_path: Path) -> None:
    from traceback_lab.ingest import Chunk, ChunkSet, ingest_prealigned, resolve_reference
    from traceback_lab.problems import LabProblem
    from traceback_lab.store import LabStore

    root, bam_path = _prealigned_world(tmp_path, ("ONT",))
    store = LabStore(root)
    chunks = ChunkSet([
        Chunk(bam_path, bam_path.name, 1, 1, rg_lines=("@RG\tID:one\tPL:ONT",)),
        Chunk(bam_path, "other.bam", 2, 1),
    ])
    with pytest.raises(LabProblem, match="TBX-LAB-009"):
        ingest_prealigned(store, chunks, resolve_reference(root, "hg38"))


@pytest.mark.parametrize("tamper", ["platform", "kind", "list"])
@pytest.mark.parametrize("dry_run", [False, True])
def test_stored_platform_mismatch_is_same_typed_refusal(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], dry_run: bool, tamper: str,
) -> None:
    from traceback_lab.store import LabStore
    from traceback_lab.views import load_sample

    root, bam_path = _prealigned_world(tmp_path, ("ILLUMINA",))
    code = signed_cli.main([
        "lab", "run", str(bam_path), "--root", str(root), "--reference", "hg38",
        "--only", "short-read-fragment-size", "--no-downsample", "--no-controls", "--json",
    ])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0, payload
    input_id = payload["data"]["sample"]["input_id"]
    source_id = payload["data"]["sample"]["source_id"]
    manifest = root / "lab" / "inputs" / input_id / "manifest.json"
    changed = json.loads(manifest.read_text())
    if tamper == "list":
        changed = ["malformed stored manifest"]
    else:
        changed["platform"] = "ont"
        if tamper == "kind":
            changed["kind"] = "aligned"
    manifest.write_text(json.dumps(changed))
    argv = ["lab", "run", "latest", "--root", str(root), "--json"]
    if dry_run:
        argv.append("--dry-run")
    code = signed_cli.main(argv)
    payload = json.loads(capsys.readouterr().out)
    assert code == 3
    assert payload["data"]["code"] == "TBX-LAB-024"
    assert payload["data"]["input_id"] == input_id
    assert "fresh ROOT" in payload["data"]["fix"]
    sample = load_sample(LabStore(root), source_id)
    assert sample is not None and sample.full.results
    assert all(result.status == "skipped" and result.skip_reason == "platform"
               for result in sample.full.results.values())


def test_cohort_refuses_mismatched_stored_platform(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from traceback_lab.cohorts import member_from_store
    from traceback_lab.problems import LabProblem
    from traceback_lab.store import LabStore

    root, bam_path = _prealigned_world(tmp_path, ("ILLUMINA",))
    code = signed_cli.main([
        "lab", "run", str(bam_path), "--root", str(root), "--reference", "hg38",
        "--only", "short-read-fragment-size", "--no-downsample", "--no-controls", "--json",
    ])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0, payload
    sample = payload["data"]["sample"]
    manifest = root / "lab" / "inputs" / sample["input_id"] / "manifest.json"
    changed = json.loads(manifest.read_text())
    changed["platform"] = "ont"
    manifest.write_text(json.dumps(changed))
    with pytest.raises(LabProblem, match="TBX-LAB-024") as error:
        member_from_store(LabStore(root), sample["source_id"])
    assert error.value.data["input_id"] == sample["input_id"]


def test_valid_dry_run_reports_platform_in_sample(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root, bam_path = _prealigned_world(tmp_path, ("ILLUMINA",))
    code = signed_cli.main([
        "lab", "run", str(bam_path), "--root", str(root), "--reference", "hg38",
        "--dry-run", "--controls", "--json",
    ])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0, payload
    assert payload["data"]["sample"]["platform"] == "illumina"
    decisions = {row["algorithm_id"]: row["decision"] for row in payload["data"]["plan"]}
    assert decisions["copy-number"] == "skip expected (platform)"
    assert decisions["read-summary"] == "decided after alignment"
    assert payload["data"]["controls"]["cna_skipped"] == "platform"


def test_simulate_cna_refuses_illumina_before_work(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    from traceback_lab import simulate_command

    root, bam_path = _prealigned_world(tmp_path, ("ILLUMINA",))
    code = signed_cli.main([
        "lab", "run", str(bam_path), "--root", str(root), "--reference", "hg38",
        "--only", "short-read-fragment-size", "--no-downsample", "--no-controls", "--json",
    ])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0, payload

    def unexpected(*_args, **_kwargs):
        pytest.fail("simulation work started before platform refusal")

    monkeypatch.setattr(simulate_command, "run_plan", unexpected)
    monkeypatch.setattr(simulate_command, "cna_specs", unexpected)
    code = signed_cli.main(["lab", "simulate", "cna", "latest", "--root", str(root), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 3
    assert payload["data"]["code"] == "TBX-LAB-024"
    assert payload["data"]["platform"] == "illumina"
