"""Focused integration contracts for cell-origin orchestration."""

from __future__ import annotations

import csv
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

import evidence_inspector.cell_origin_pipeline as pipeline_module
from evidence_inspector.cell_origin_pipeline import (
    CellOriginResultBundle,
    PipelineConfig,
    _normalize_native_modkit,
    build_alignment_command_plan,
    preflight,
    run_pipeline,
)
from evidence_inspector.cell_origin_models import (
    AtlasUMatrix,
    AtlasUMatrixRow,
    AtlasUValue,
    BootstrapInformationStatus,
    BootstrapResultV2,
    DeconvolutionOutputV2,
    GenomicMarker,
    LOYFER_UXM_METHOD,
    MarkerCountRow,
)
from evidence_inspector.uxm import (
    UxmClassificationResult,
    UxmDiagnostics,
    UxmStopReason,
)


def _config(tmp_path: Path, *, extract_tsv: Path) -> PipelineConfig:
    marker_bed = tmp_path / "regions.bed"
    marker_metadata = tmp_path / "markers.tsv"
    atlas = tmp_path / "atlas.tsv"
    for path in (marker_bed, marker_metadata, atlas):
        path.write_text("registered\n", encoding="utf-8")
    return PipelineConfig(
        marker_bed=marker_bed,
        marker_metadata=marker_metadata,
        atlas_u_matrix=atlas,
        extract_tsv=extract_tsv,
        output_path=tmp_path / "result.json",
    )


def test_alignment_plan_preserves_modification_tags_without_shell(tmp_path: Path) -> None:
    plan = build_alignment_command_plan(
        tmp_path / "reads.bam",
        tmp_path / "hg38.mmi",
        tmp_path / "work",
        threads=3,
    )

    assert plan.preserved_tags == ("MM", "ML", "MN")
    assert plan.steps[0].argv[:4] == (
        "samtools",
        "fastq",
        "-T",
        "MM,ML,MN",
    )
    assert "-y" in plan.steps[1].argv
    assert all(len(step.argv) > 1 for step in plan.steps)


def test_modkit_064_extract_is_normalized_strictly(tmp_path: Path) -> None:
    source = tmp_path / "native.tsv"
    destination = tmp_path / "normalized.tsv"
    source.write_text(
        "\t".join(
            (
                "read_id",
                "ref_position",
                "chrom",
                "mod_strand",
                "modified_primary_base",
                "fail",
                "call_code",
                "call_prob",
                "ignored",
            )
        )
        + "\n"
        + "read-1\t12\tchr1\t+\tC\tfalse\t-\t0.91\tx\n"
        + "read-2\t13\tchr1\t-\tC\tfalse\tm\t0.99\tx\n",
        encoding="utf-8",
    )

    _normalize_native_modkit(source, destination)

    with destination.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle, delimiter="\t"))
    assert [row["call_code"] for row in rows] == ["C", "m"]
    assert [row["modified_probability"] for row in rows] == ["0.91", "0.99"]


def test_preflight_does_not_require_modkit_for_existing_extract(
    tmp_path: Path,
) -> None:
    extract = tmp_path / "calls.tsv"
    extract.write_text("calls\n", encoding="utf-8")
    config = _config(tmp_path, extract_tsv=extract)

    report = preflight(
        config,
        require_modkit=False,
        executable_finder=lambda name: None,
    )

    modkit = next(item for item in report.items if item.item_id == "software.modkit")
    assert modkit.ready is False
    assert modkit.blocking is False
    assert any(
        item.item_id == "input.methylation-calls" and item.ready
        for item in report.items
    )


def test_production_pipeline_publishes_v2_unavailable_uncertainty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    extract = tmp_path / "calls.tsv"
    config = _config(tmp_path, extract_tsv=extract)
    extract.write_text("synthetic\n", encoding="utf-8")
    marker = GenomicMarker(
        marker_id="marker.only",
        chromosome="chr1",
        start0=0,
        end0=10,
        target_cell_type_id="only",
        atlas_id="atlas.sparse.v1",
        source_ids=("source.synthetic-atlas",),
    )
    atlas = AtlasUMatrix(
        atlas_id="atlas.sparse.v1",
        method=LOYFER_UXM_METHOD,
        cell_type_ids=("only",),
        rows=(
            AtlasUMatrixRow(
                marker_id="marker.only",
                values=(AtlasUValue(cell_type_id="only", u_fraction=1.0),),
            ),
        ),
        source_ids=("source.synthetic-atlas",),
    )
    resources = pipeline_module._LoyferResources(
        markers=(marker,),
        atlas=atlas,
        labels={"only": "Only"},
        registered_marker_count=1,
        excluded_incomplete_count=0,
        collapsed_duplicate_count=0,
    )
    classified = UxmClassificationResult(
        observations=(),
        marker_counts=(
            MarkerCountRow(
                marker_id="marker.only",
                u_count=1,
                x_count=0,
                m_count=0,
                classified_fragment_count=1,
                u_fraction=1.0,
            ),
        ),
        marker_weights=(),
        diagnostics=UxmDiagnostics(
            inspected_calls=1,
            retained_unique_cpg_calls=1,
            duplicate_cpg_calls=0,
            excluded_failed_calls=0,
            excluded_non_c_calls=0,
            excluded_unsupported_modification_calls=0,
            excluded_invalid_calls=0,
            excluded_outside_marker_calls=0,
            excluded_ambiguous_marker_calls=0,
            excluded_unknown_marker_calls=0,
            excluded_conflicting_duplicate_groups=0,
            excluded_oversized_groups=0,
            fragment_marker_group_count=1,
            classified_fragment_marker_count=1,
            excluded_fewer_than_four_cpgs=0,
            stop_reason=UxmStopReason.COMPLETE_INPUT,
            partial_input=False,
        ),
    )
    monkeypatch.setattr(
        pipeline_module, "_load_loyfer_resources", lambda _config: resources
    )
    monkeypatch.setattr(
        pipeline_module,
        "load_modkit_extract_calls",
        lambda *_args, **_kwargs: (SimpleNamespace(fragment_digest="a" * 64),),
    )
    monkeypatch.setattr(
        pipeline_module,
        "_partitioned_classification",
        lambda *_args, **_kwargs: classified,
    )
    monkeypatch.setattr(
        pipeline_module, "_load_healthy", lambda *_args, **_kwargs: None
    )
    config = replace(
        config,
        healthy_table=None,
        bootstrap_replicates=10,
    )

    bundle = run_pipeline(config, fragment_hash_salt=b"private-test-salt")

    assert isinstance(bundle.result.bootstrap, BootstrapResultV2)
    assert bundle.result.bootstrap.information_status == (
        BootstrapInformationStatus.INSUFFICIENT_INFORMATION
    )
    chart = bundle.charts.composition_rows[0]
    assert not chart.uncertainty_available
    assert chart.lower_fraction is None
    assert chart.upper_fraction is None
    published = json.loads(config.output_path.read_text(encoding="utf-8"))
    assert published["charts"]["composition_rows"][0]["lower_fraction"] is None
    reloaded = CellOriginResultBundle.model_validate_json(
        config.output_path.read_bytes()
    )
    assert reloaded == bundle
    assert isinstance(reloaded.result.deconvolution, DeconvolutionOutputV2)
    assert reloaded.result.deconvolution.schema_version == (
        "cell-origin-deconvolution.v2"
    )
    assert len(reloaded.result.deconvolution.atlas_sha256) == 64
    assert reloaded.result.deconvolution.diagnostics.row_scale.value == (
        "sqrt_count"
    )
    assert reloaded.result.bootstrap is not None
    assert reloaded.result.bootstrap.diagnostics.nnls_row_scale == (
        reloaded.result.deconvolution.diagnostics.row_scale
    )

    published["result"]["deconvolution"]["diagnostics"]["row_scale"] = (
        "reference_count"
    )
    with pytest.raises(ValidationError, match="do not match deconvolution"):
        CellOriginResultBundle.model_validate_json(json.dumps(published))

    published["result"]["deconvolution"]["diagnostics"]["row_scale"] = (
        "sqrt_count"
    )
    published["result"]["bootstrap"]["intervals"][0]["estimate"] = 0.5
    with pytest.raises(
        ValidationError, match="estimate does not match deconvolution"
    ):
        CellOriginResultBundle.model_validate_json(json.dumps(published))
