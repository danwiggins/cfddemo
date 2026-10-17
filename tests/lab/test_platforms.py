"""Platform declarations control Lab alignment and result compatibility."""

from pathlib import Path
import json

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


@pytest.mark.parametrize("tamper", ["platform", "kind"])
@pytest.mark.parametrize("dry_run", [False, True])
def test_stored_platform_mismatch_is_same_typed_refusal(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], dry_run: bool, tamper: str,
) -> None:
    root, bam_path = _prealigned_world(tmp_path, ("ILLUMINA",))
    code = signed_cli.main([
        "lab", "run", str(bam_path), "--root", str(root), "--reference", "hg38",
        "--only", "short-read-fragment-size", "--no-downsample", "--no-controls", "--json",
    ])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0, payload
    input_id = payload["data"]["sample"]["input_id"]
    manifest = root / "lab" / "inputs" / input_id / "manifest.json"
    changed = json.loads(manifest.read_text())
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
        "--dry-run", "--json",
    ])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0, payload
    assert payload["data"]["sample"]["platform"] == "illumina"
