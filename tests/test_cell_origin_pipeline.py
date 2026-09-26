"""Focused integration contracts for cell-origin orchestration."""

from __future__ import annotations

import csv
from pathlib import Path

from evidence_inspector.cell_origin_pipeline import (
    PipelineConfig,
    _normalize_native_modkit,
    build_alignment_command_plan,
    preflight,
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
