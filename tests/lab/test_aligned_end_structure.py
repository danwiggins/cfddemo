"""Pinned catalogue safety and exact ONT aligned-end counting."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

from traceback_lab.algorithms.aligned_end_structure import ALIGNED_END_STRUCTURE
from traceback_lab.cli import build_parser
from traceback_lab.end_catalog_assets import EndCatalogueError, inspect_end_catalogues, register_end_catalogues
from traceback_lab.read_summary import COLUMNS, SCHEMA
from traceback_lab.registry import InputContext, PlanItem, Ready, registry_entries
from traceback_lab.runner import RunTarget, run_plan
from traceback_lab.scrub import Scrubber
from traceback_lab.store import LabStore, new_run_id


def _catalogues(tmp_path: Path) -> tuple[Path, Path, Path]:
    root = tmp_path / "root"
    centers = tmp_path / "centers.bed"
    preferred = tmp_path / "preferred.bed"
    centers.write_text("chr1\t490\t510\n", encoding="ascii")  # center = 500
    preferred.write_text("chr1\t440\t441\tU\nchr1\t560\t561\tD\n", encoding="ascii")
    register_end_catalogues(root, centers=centers, preferred=preferred,
                            source_label="synthetic hg38 end catalogues",
                            centers_source="synthetic-centers-v1", preferred_source="synthetic-sites-v1",
                            terms="unknown", license_id=None)
    (root / "input").mkdir()
    (root / "input" / "manifest.json").write_text(json.dumps({"kind": "aligned"}), encoding="utf-8")
    return root, centers, preferred


def _summary(parent: Path) -> None:
    columns = parent / "columns"
    columns.mkdir(parents=True)
    # One valid molecule has U at −60 and D at +60. Duplicate and low-MAPQ
    # copies must contribute neither to the score nor preferred-site support.
    values = {
        "contig": [0, 0, 0], "start": [440, 440, 440], "end": [561, 561, 561],
        "flag": [0, 0x400, 0], "mapq": [60, 60, 10],
    }
    for name, dtype in COLUMNS.items():
        np.asarray(values.get(name, [0, 0, 0]), dtype=dtype).tofile(columns / f"{name}.bin")
    (columns / "meta.json").write_text(json.dumps({
        "schema": SCHEMA, "rows": 3, "columns": COLUMNS, "contigs": ["chr1"],
        "lengths": [1000], "index_mapped_upper_bound": 3,
    }), encoding="utf-8")


def _context(root: Path, summary_parent: Path, *, depth: float = 10.0, **updates: object) -> InputContext:
    data = dict(
        root=root, source_id="1" * 64, input_id="2" * 64, reference_id="hg38",
        reference_sha256="3" * 64, input_dir=root / "input",
        eligible_reads=3, approx_depth_x=depth,
        extras={"upstream": {"read-summary": str(summary_parent)}},
    )
    data.update(updates)
    return InputContext(**data)


def test_exact_ocf_like_sign_and_preferred_end_support(tmp_path: Path) -> None:
    root, _, _ = _catalogues(tmp_path)
    summary_parent = tmp_path / "read-summary"
    _summary(summary_parent)
    ctx = _context(root, summary_parent)
    assert isinstance(ALIGNED_END_STRUCTURE.preflight(ctx), Ready)
    output = ALIGNED_END_STRUCTURE.run(ctx, tmp_path / "out")
    metrics = {item["metric_id"]: item for item in output.metrics}
    assert metrics["ocf_like_end_asymmetry"]["value"] == -2
    assert metrics["ocf_like_end_asymmetry"]["n"] == 1
    assert metrics["preferred_end_support_fraction"]["value"] == 1.0
    assert metrics["preferred_end_support_fraction"]["n"] == 2
    artifact = json.loads(output.artifacts["aligned_end_structure.json"])
    assert artifact["u_count"][40] == 1 and artifact["d_count"][160] == 1
    assert artifact["ocf_like_left"] == -1 and artifact["ocf_like_right"] == -1
    assert artifact["valid_molecules"] == 1 and artifact["preferred_u_hits"] == 1
    assert artifact["preferred_d_hits"] == 1
    assert artifact["observed_depth_x"] == 10.0
    assert len(artifact["centers_sha256"]) == 64 and len(artifact["preferred_sha256"]) == 64
    assert "no tissue-origin calibration" in artifact["interpretation"]


def test_depth_full_input_and_pinned_asset_gates(tmp_path: Path) -> None:
    root, centers, _ = _catalogues(tmp_path)
    summary_parent = tmp_path / "read-summary"
    assert ALIGNED_END_STRUCTURE.preflight(_context(root, summary_parent, depth=9.99)).reason_code == "depth"
    assert isinstance(ALIGNED_END_STRUCTURE.preflight(_context(root, summary_parent, depth=10.0)), Ready)
    assert ALIGNED_END_STRUCTURE.preflight(_context(root, summary_parent, derived={"transform": "split", "half": "A"})).reason_code == "derived_input"
    assert ALIGNED_END_STRUCTURE.preflight(_context(root, summary_parent, example=True)).reason_code == "example_reference"
    old_identity = inspect_end_catalogues(root).identity
    centers.write_text("chr1\t491\t511\n", encoding="ascii")
    with pytest.raises(EndCatalogueError, match="changed"):
        inspect_end_catalogues(root)
    assert ALIGNED_END_STRUCTURE.asset_sha256s(root) == []
    assert ALIGNED_END_STRUCTURE.preflight(_context(root, summary_parent)).reason_code == "missing_asset"
    assert old_identity


@pytest.mark.parametrize("bad", [
    "chr1\t490\t510\nchr1\t600\t620\n",  # windows overlap
    "chr2\t490\t510\nchr1\t700\t720\n",  # unsorted chromosomes
    "chr1\t490\t510\nchr1\t490\t510\n",  # duplicate
    "chrM\t490\t510\n",  # not a canonical autosome
])
def test_bad_centers_do_not_replace_pinned_registration(tmp_path: Path, bad: str) -> None:
    root, centers, preferred = _catalogues(tmp_path)
    registration = root / "lab" / "assets" / "aligned-ends" / "registration.json"
    original = registration.read_bytes()
    centers.write_text(bad, encoding="ascii")
    with pytest.raises(EndCatalogueError):
        register_end_catalogues(root, centers=centers, preferred=preferred,
                                source_label="synthetic hg38 end catalogues",
                                centers_source="synthetic-centers-v1", preferred_source="synthetic-sites-v1",
                                terms="unknown", license_id=None)
    assert registration.read_bytes() == original


def test_registration_rejects_symlink_and_path_label(tmp_path: Path) -> None:
    root, centers, preferred = _catalogues(tmp_path)
    linked = tmp_path / "linked.bed"
    linked.symlink_to(centers)
    with pytest.raises(EndCatalogueError, match="symlink"):
        register_end_catalogues(root, centers=linked, preferred=preferred,
                                source_label="synthetic", centers_source="synthetic-centers-v1",
                                preferred_source="synthetic-sites-v1", terms="unknown", license_id=None)
    with pytest.raises(EndCatalogueError, match="not paths"):
        register_end_catalogues(root, centers=centers, preferred=preferred,
                                source_label=str(centers), centers_source="synthetic-centers-v1",
                                preferred_source="synthetic-sites-v1", terms="unknown", license_id=None)


def test_cli_registration_and_inspection_return_path_free_results(tmp_path: Path) -> None:
    root, centers, preferred = _catalogues(tmp_path)
    store = LabStore(root)
    store.ensure()
    args = build_parser().parse_args([
        "asset", "end-catalogues-register", "--centers", str(centers),
        "--preferred", str(preferred), "--source-label", "synthetic hg38 end catalogues",
        "--centers-source", "synthetic-centers-v1", "--preferred-source", "synthetic-sites-v1",
        "--terms", "unknown", "--root", str(root),
    ])
    code, payload = args.handler(args, store)
    assert code == 0 and payload["data"]["identity"] == inspect_end_catalogues(root).identity
    assert str(centers) not in json.dumps(payload) and str(preferred) not in json.dumps(payload)
    inspect_args = build_parser().parse_args(["asset", "end-catalogues-inspect", "--root", str(root)])
    code, inspected = inspect_args.handler(inspect_args, store)
    assert code == 0 and inspected["data"]["identity"] == payload["data"]["identity"]


@pytest.mark.parametrize("condition,expected", [
    ("depth", "depth"), ("derived", "derived_input"), ("missing_manifest", "input_kind"),
])
def test_preflight_skip_publishes_through_runner(
    tmp_path: Path, condition: str, expected: str,
) -> None:
    root, _, _ = _catalogues(tmp_path)
    store = LabStore(root)
    store.ensure()
    if condition == "missing_manifest":
        (root / "input" / "manifest.json").unlink()
    target = RunTarget(
        input_id="2" * 64, source_id="1" * 64, input_dir=root / "input",
        reference_id="hg38", reference_sha256="3" * 64,
        approx_depth_x=9.99 if condition == "depth" else 10.0,
        derived={"transform": "split", "half": "A"} if condition == "derived" else None,
    )
    # The real read-summary upstream supplies observed depth. Isolate the
    # publication boundary after that value is already on the RunTarget.
    algorithm = replace(ALIGNED_END_STRUCTURE, upstream=())
    run_id = new_run_id()
    result = run_plan(
        store, None, store.run_log(run_id), target, [PlanItem(algorithm, False)], {},
        run_id=run_id, scrubber=Scrubber(root=root),
    )[0]
    assert result.status == "skipped" and result.skip_reason == expected
    envelope = store.read_result(target.input_id, algorithm.id, result.parameters_sha256)
    assert envelope is not None and envelope.status == "skipped" and envelope.skip_reason == expected


def test_ten_x_synthetic_ont_bam_runs_read_summary_and_end_worker(tmp_path: Path) -> None:
    import pysam

    root, _, _ = _catalogues(tmp_path)
    bam = root / "input" / "aligned.bam"
    with pysam.AlignmentFile(str(bam), "wb", header={
        "HD": {"VN": "1.6"}, "SQ": [{"SN": "chr1", "LN": 1000}],
    }) as stream:
        for index in range(100):
            record = pysam.AlignedSegment()
            record.query_name = f"synthetic-{index}"
            record.query_sequence = "A" * 121
            record.flag = 0
            record.reference_id = 0
            record.reference_start = 440 if index == 0 else 0
            record.mapping_quality = 60
            record.cigarstring = "121M"
            stream.write(record)
    store = LabStore(root)
    store.ensure()
    target = RunTarget(
        input_id="2" * 64, source_id="1" * 64, input_dir=bam.parent,
        reference_id="hg38", reference_sha256="3" * 64,
    )
    registered = registry_entries(["read_summary:READ_SUMMARY", "aligned_end_structure:ALIGNED_END_STRUCTURE"])
    entries = {algorithm.id: entry for entry, algorithm in registered}
    run_id = new_run_id()
    results = run_plan(
        store, None, store.run_log(run_id), target,
        [PlanItem(algorithm, False) for _, algorithm in registered], entries,
        run_id=run_id, scrubber=Scrubber(root=root), timeout_s=60,
    )
    assert [result.status for result in results] == ["ok", "ok"], results
    assert target.approx_depth_x == 12.1
    envelope = store.read_result(target.input_id, ALIGNED_END_STRUCTURE.id, results[1].parameters_sha256)
    assert envelope is not None and envelope.status == "ok"
    metrics = {item.metric_id: item.value for item in envelope.metrics}
    assert metrics["ocf_like_end_asymmetry"] == -2
    assert metrics["preferred_end_support_fraction"] == 0.01
    attempt = store.selected_attempt(target.input_id, ALIGNED_END_STRUCTURE.id, results[1].parameters_sha256)
    assert attempt is not None
    artifact = json.loads((attempt / "artifacts" / "aligned_end_structure.json").read_bytes())
    assert artifact["valid_molecules"] == 100 and artifact["eligible_centers"] == 1
