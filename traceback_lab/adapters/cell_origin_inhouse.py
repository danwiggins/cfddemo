"""Unsigned Loyfer UXM computation fed by the Lab's MM/ML decoder.

The signed cell-origin job is untouched. This Lab-only path reuses its atlas
loader, UXM classifier, NNLS solver, bootstrap and validation checks while
replacing the external modkit extraction step.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from evidence_inspector.cell_origin_models import DigestArtifact, MarkerCountRow, NnlsRowScale
from evidence_inspector.cell_origin_pipeline import (
    PipelineConfig,
    _filtered_atlas,
    _load_loyfer_resources,
    _partitioned_classification,
    validation_report,
)
from evidence_inspector.cell_origin_prefilter import read_bed_regions, write_prefiltered_bam
from evidence_inspector.deconvolution import bootstrap_uxm_v2, deconvolve_uxm_v2
from traceback_runner.cell_origin_method import CellOriginParametersV1

from .mmml_extract import Extraction, extract_cpg_calls


@dataclass(frozen=True)
class InhouseMeasurement:
    fit: object
    bootstrap: object
    extraction: Extraction
    marker_counts: tuple[MarkerCountRow, ...]
    classified_fragments: int
    observed_markers: int
    registered_markers: int
    prefilter_records: int
    prefilter_passing: int


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(16 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def compute_inhouse(
    *,
    parameters: CellOriginParametersV1,
    bam: Path,
    fasta: Path,
    atlas: Path,
    markers: Path,
    regions: Path,
    scratch: Path,
) -> InhouseMeasurement:
    """Compute one Lab-only result with locked caps and source-asset checks."""

    config = PipelineConfig(
        marker_bed=regions, marker_metadata=markers, atlas_u_matrix=atlas,
        output_path=scratch / "unused-result.json", aligned_modbam=bam,
        reference_fasta=fasta, job_directory=scratch,
        maximum_calls=parameters.caps.maximum_calls,
        maximum_groups=parameters.caps.maximum_groups,
        maximum_cpgs_per_group=parameters.caps.maximum_cpgs_per_group,
        bootstrap_replicates=parameters.bootstrap_replicates,
        random_seed=parameters.bootstrap_random_seed,
        modkit_filter_threshold=parameters.modkit_filter_threshold,
        min_mapq=parameters.min_mapq,
    )
    resources = _load_loyfer_resources(config)
    scratch.mkdir(mode=0o700, parents=True, exist_ok=True)
    filtered = scratch / "prefiltered.bam"
    region_index = read_bed_regions(regions)
    prefilter = write_prefiltered_bam(bam, filtered, region_index, min_mapq=parameters.min_mapq)
    if prefilter.written == 0:
        raise ValueError("no alignment passed the marker-region prefilter")
    extraction = extract_cpg_calls(
        filtered, fasta, maximum_calls=parameters.caps.maximum_calls,
        filter_threshold=parameters.modkit_filter_threshold,
        include_regions=region_index,
    )
    if not extraction.calls:
        raise ValueError("no known, confident CpG calls overlap marker regions")
    classified = _partitioned_classification(
        extraction.calls, resources.markers,
        maximum_groups=parameters.caps.maximum_groups,
        maximum_cpgs_per_group=parameters.caps.maximum_cpgs_per_group,
    )
    diagnostics = classified.diagnostics
    if diagnostics.partial_input or diagnostics.excluded_oversized_groups:
        raise ValueError("cell-origin fragment-marker groups exceed the locked cap")
    rows = classified.marker_counts
    classified_fragments = sum(row.u_count + row.m_count for row in rows)
    if (not rows or classified_fragments < parameters.min_classified_fragments
            or len(rows) < parameters.min_observed_markers):
        raise ValueError("cell-origin classified fragment and marker floors are not met")
    atlas_rows = _filtered_atlas(resources.atlas, tuple(row.marker_id for row in rows))
    fit = deconvolve_uxm_v2(
        rows, atlas_rows, row_scale=NnlsRowScale(parameters.nnls_row_scale),
        tolerance=parameters.nnls_tolerance, max_iterations=parameters.nnls_max_iterations,
    )
    if not fit.diagnostics.converged:
        raise ValueError("cell-origin NNLS did not converge")
    bootstrap = bootstrap_uxm_v2(
        rows, atlas_rows, fit, replicates=parameters.bootstrap_replicates,
        random_seed=parameters.bootstrap_random_seed,
        confidence_level=parameters.bootstrap_confidence_level,
    )
    copies = tuple(
        (DigestArtifact(artifact_id=f"artifact.{kind}", sha256=_digest(path), size_bytes=path.stat().st_size), path)
        for kind, path in (("loyfer-atlas", atlas), ("loyfer-markers", markers), ("loyfer-regions", regions))
    )
    report = validation_report(
        marker_counts=rows, classified_fragment_marker_count=diagnostics.classified_fragment_marker_count,
        atlas=atlas_rows, deconvolution=fit, bootstrap=bootstrap, artifacts=copies,
    )
    if not report.passed:
        raise ValueError("cell-origin validation failed: " + ", ".join(
            record.check.value for record in report.records if not record.passed
        ))
    return InhouseMeasurement(
        fit, bootstrap, extraction, rows, classified_fragments, len(rows), len(resources.markers),
        prefilter.records_scanned, prefilter.passing,
    )


__all__ = ["InhouseMeasurement", "compute_inhouse"]
