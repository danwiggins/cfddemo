"""Deterministic, entirely synthetic case bundle for offline development."""

from __future__ import annotations

import os
from pathlib import Path

from pydantic import ValidationError

from .models import (
    ArtifactIdentity,
    ArtifactKind,
    Capability,
    Case,
    Claim,
    ClaimKind,
    HashScope,
    IdentityVerification,
    LocatorKind,
    Manifest,
    RangeClassification,
    ReferenceComparisonRow,
    ReferenceRangeComparisonValues,
    SampleLinkage,
    SampleLinkageStatus,
    SelectionParameters,
    Source,
    SourceLocator,
    ToolName,
    ToolProvenance,
    ToolResult,
    ToolStatus,
    Trimming,
    TrimmingStatus,
    VerificationLevel,
    build_case,
    canonical_json_bytes,
    sha256_bytes,
)

SYNTHETIC_LENGTHS = (100, 150, 151, 151, 167, 167, 167, 1001)
SYNTHETIC_LENGTH_ARTIFACT_ID = "artifact.synthetic-lengths.v1"
SYNTHETIC_SAMPLE_TABLE_ID = "artifact.synthetic-sample-table.v1"
SYNTHETIC_REFERENCE_TABLE_ID = "artifact.synthetic-reference-table.v1"
DEFAULT_LOCAL_CASE_PATH = Path("data/local/case.json")
MAX_CASE_BUNDLE_BYTES = 4_194_304


class CaseBundleLoadError(ValueError):
    """A sanitized failure to read or validate a configured local case."""

_SAMPLE_ROWS = (
    {"cell_type_id": "immune", "parent_id": None, "is_leaf": True, "fraction": 0.20},
    {"cell_type_id": "vascular", "parent_id": None, "is_leaf": True, "fraction": 0.01},
)
_REFERENCE_ROWS = (
    {
        "cell_type_id": "immune",
        "min_fraction": 0.10,
        "max_fraction": 0.30,
        "cohort_id": "synthetic-cohort",
        "source_id": "source.synthetic-table",
        "assay": "synthetic assay",
    },
    {
        "cell_type_id": "vascular",
        "min_fraction": 0.02,
        "max_fraction": 0.08,
        "cohort_id": "synthetic-cohort",
        "source_id": "source.synthetic-table",
        "assay": "synthetic assay",
    },
)


def _artifact(
    artifact_id: str,
    kind: ArtifactKind,
    label: str,
    content: object,
    hash_scope: HashScope = HashScope.FULL_ARTIFACT,
) -> ArtifactIdentity:
    encoded = canonical_json_bytes(content)
    return ArtifactIdentity(
        id=artifact_id,
        kind=kind,
        label=label,
        sha256=sha256_bytes(encoded),
        hash_scope=hash_scope,
        size_bytes=len(encoded),
        identity_verification=IdentityVerification.VERIFIED,
    )


def _sources() -> tuple[Source, ...]:
    return (
        Source.from_content(
            id="source.synthetic-length",
            document_id="synthetic-report",
            locator=SourceLocator(kind=LocatorKind.SECTION, value="Synthetic lengths"),
            quote=(
                "Synthetic fixture statement: the modal query-sequence length "
                "is 167 bp."
            ),
        ),
        Source.from_content(
            id="source.synthetic-method-current",
            document_id="synthetic-report",
            locator=SourceLocator(kind=LocatorKind.SECTION, value="Synthetic method"),
            quote=(
                "Synthetic current method: calculate length from the aligned "
                "reference span without constant subtraction."
            ),
        ),
        Source.from_content(
            id="source.synthetic-method-instructions",
            document_id="synthetic-report",
            locator=SourceLocator(
                kind=LocatorKind.SECTION, value="Synthetic reproduction instructions"
            ),
            quote=(
                "Synthetic reproduction instruction: subtract a constant 45 bp "
                "from each query-sequence length."
            ),
        ),
        Source.from_content(
            id="source.synthetic-causal",
            document_id="synthetic-slides",
            locator=SourceLocator(kind=LocatorKind.PAGE, value="1"),
            quote=(
                "Synthetic assertion: the observed cell-fraction difference is "
                "caused by disease."
            ),
        ),
        Source.from_content(
            id="source.synthetic-table",
            document_id="synthetic-slides",
            locator=SourceLocator(kind=LocatorKind.PAGE, value="2"),
            quote="Synthetic cell-fraction table for contract tests.",
            table_row="immune=0.20; vascular=0.01",
        ),
        Source.from_content(
            id="source.synthetic-paper-method",
            document_id="synthetic-paper",
            locator=SourceLocator(kind=LocatorKind.PAGE, value="3"),
            quote=(
                "Synthetic methods note: a ratio with a restricted denominator "
                "is not the same as a fraction over all accepted reads."
            ),
        ),
    )


def _manifest(sources: tuple[Source, ...]) -> Manifest:
    length_artifact = _artifact(
        SYNTHETIC_LENGTH_ARTIFACT_ID,
        ArtifactKind.DERIVED_LENGTHS,
        "Synthetic query-length array",
        SYNTHETIC_LENGTHS,
        HashScope.DERIVED_LENGTHS,
    )
    sample_artifact = _artifact(
        SYNTHETIC_SAMPLE_TABLE_ID,
        ArtifactKind.SAMPLE_TABLE,
        "Synthetic sample fractions",
        _SAMPLE_ROWS,
    )
    reference_artifact = _artifact(
        SYNTHETIC_REFERENCE_TABLE_ID,
        ArtifactKind.REFERENCE_TABLE,
        "Synthetic reference ranges",
        _REFERENCE_ROWS,
    )
    source_artifact = _artifact(
        "artifact.synthetic-sources.v1",
        ArtifactKind.SOURCE_BUNDLE,
        "Synthetic source bundle",
        [source.model_dump(mode="json") for source in sources],
    )
    return Manifest(
        schema_version="evidence-inspector.v1",
        artifacts=(
            length_artifact,
            sample_artifact,
            reference_artifact,
            source_artifact,
        ),
        sample_linkage=SampleLinkage(
            status=SampleLinkageStatus.UNVERIFIED,
            evidence_ids=(),
            operator_rationale=(
                "Synthetic artifacts are not linked to any person, report, or specimen."
            ),
        ),
        trimming=Trimming(
            status=TrimmingStatus.UNKNOWN,
            processing_detail=(
                "The synthetic query lengths carry no biological trimming claim."
            ),
        ),
        capabilities=(
            Capability(
                name=ToolName.READ_LENGTH_SUMMARY,
                available=True,
                artifact_ids=(SYNTHETIC_LENGTH_ARTIFACT_ID,),
            ),
            Capability(
                name=ToolName.COMPARE_REFERENCE_RANGES,
                available=True,
                artifact_ids=(
                    SYNTHETIC_SAMPLE_TABLE_ID,
                    SYNTHETIC_REFERENCE_TABLE_ID,
                ),
            ),
            Capability(
                name=ToolName.SOURCE_REVIEW,
                available=True,
                artifact_ids=(),
            ),
        ),
        selection_parameters=SelectionParameters(
            ordering_rule="single deterministic synthetic fixture"
        ),
        tool_versions={
            "contracts": "1",
            "fixture": "1",
            "read_length_summary": "1",
            "compare_reference_ranges": "1",
        },
        partial_collection=True,
    )


def _claims() -> tuple[Claim, ...]:
    return (
        Claim(
            id="claim.synthetic-length",
            source_ids=("source.synthetic-length",),
            original_quote=(
                "Synthetic fixture statement: the modal query-sequence length "
                "is 167 bp."
            ),
            text="The synthetic subset's modal query-sequence length is 167 bp.",
            kind=ClaimKind.MEASUREMENT,
            revision=1,
        ),
        Claim(
            id="claim.synthetic-method-equivalence",
            source_ids=(
                "source.synthetic-method-current",
                "source.synthetic-method-instructions",
            ),
            original_quote=(
                "Synthetic reproduction instruction: subtract a constant 45 bp "
                "from each query-sequence length."
            ),
            text=(
                "The synthetic reproduction instruction uses the same length "
                "method as the synthetic current method."
            ),
            kind=ClaimKind.COMPARISON,
            revision=1,
        ),
        Claim(
            id="claim.synthetic-causal",
            source_ids=("source.synthetic-causal", "source.synthetic-table"),
            original_quote=(
                "Synthetic assertion: the observed cell-fraction difference is "
                "caused by disease."
            ),
            text=(
                "The synthetic cell-fraction difference is caused by disease."
            ),
            kind=ClaimKind.CAUSAL,
            revision=1,
        ),
    )


def _length_result(manifest: Manifest) -> ToolResult:
    artifact = next(
        item
        for item in manifest.artifacts
        if item.id == SYNTHETIC_LENGTH_ARTIFACT_ID
    )
    assert artifact.sha256 is not None
    return ToolResult(
        id="result.synthetic-length-summary.v1",
        tool=ToolName.READ_LENGTH_SUMMARY,
        status=ToolStatus.OK,
        values={
            "bins": [
                {"length_bp": length, "count": count}
                for length, count in (
                    (100, 1),
                    (150, 1),
                    (151, 2),
                    (167, 3),
                )
            ],
            "overflow_count": 1,
            "valid_read_count": 8,
            "mode_bp": 167,
            "median_bp": 159.0,
            "fraction_100_150": 0.25,
            "fraction_gt_1000": 0.125,
        },
        units={
            "overflow_count": "reads",
            "valid_read_count": "reads",
            "mode_bp": "bp",
            "median_bp": "bp",
            "fraction_100_150": "fraction",
            "fraction_gt_1000": "fraction",
        },
        definitions={
            "mode_bp": "lowest query-sequence length among tied highest counts",
            "median_bp": "median accepted query-sequence length",
            "fraction_100_150": "count of lengths 100 through 150 bp inclusive",
            "fraction_gt_1000": "count of lengths strictly greater than 1000 bp",
            "valid_read_count": "accepted unique positive-length primary reads",
            "overflow_count": "accepted query-sequence lengths greater than 1000 bp",
        },
        denominator="accepted unique positive-length primary reads (n=8)",
        filters=(
            "primary records only",
            "first eligible record per read ID",
            "positive query-sequence length",
        ),
        source_ids=("source.synthetic-length",),
        verification_level=VerificationLevel.SAMPLED_RECOMPUTED,
        provenance=ToolProvenance(
            artifact_ids=(SYNTHETIC_LENGTH_ARTIFACT_ID,),
            artifact_digests={SYNTHETIC_LENGTH_ARTIFACT_ID: artifact.sha256},
            sample_rule="all eight values in the bounded synthetic fixture",
            inspected_count=8,
            accepted_count=8,
            exclusions={},
            scanned_complete_input=True,
            stop_reason="complete synthetic fixture",
            elapsed_ms=0,
            tool_version="1",
            parameters={"histogram_min_bp": 1, "histogram_max_bp": 1000},
        ),
        limitations=(
            "Synthetic fixture; not biological or clinical evidence.",
            "Query-sequence length is not aligned reference span.",
        ),
    )


def _range_result(manifest: Manifest) -> ToolResult:
    artifacts = {artifact.id: artifact for artifact in manifest.artifacts}
    sample_artifact = artifacts[SYNTHETIC_SAMPLE_TABLE_ID]
    reference_artifact = artifacts[SYNTHETIC_REFERENCE_TABLE_ID]
    assert sample_artifact.sha256 is not None
    assert reference_artifact.sha256 is not None
    values = ReferenceRangeComparisonValues(
        rows=(
            ReferenceComparisonRow(
                cell_type_id="immune",
                fraction=0.20,
                min_fraction=0.10,
                max_fraction=0.30,
                classification=RangeClassification.WITHIN,
                source_ids=("source.synthetic-table",),
            ),
            ReferenceComparisonRow(
                cell_type_id="vascular",
                fraction=0.01,
                min_fraction=0.02,
                max_fraction=0.08,
                classification=RangeClassification.BELOW,
                source_ids=("source.synthetic-table",),
            ),
        ),
        partial_table=False,
    )
    return ToolResult(
        id="result.synthetic-reference-comparison.v1",
        tool=ToolName.COMPARE_REFERENCE_RANGES,
        status=ToolStatus.OK,
        values=values.model_dump(mode="json"),
        units={"rows": "canonical fractions from 0 to 1"},
        definitions={
            "rows": (
                "independent inclusive comparisons of supplied sample fractions "
                "to supplied observed cohort bounds"
            )
        },
        denominator=None,
        filters=(
            "no parent-child aggregation",
            "inclusive minimum and maximum bounds",
            "matched cell_type_id rows only",
        ),
        source_ids=("source.synthetic-table",),
        verification_level=VerificationLevel.RECOMPUTED,
        provenance=ToolProvenance(
            artifact_ids=(
                SYNTHETIC_SAMPLE_TABLE_ID,
                SYNTHETIC_REFERENCE_TABLE_ID,
            ),
            artifact_digests={
                SYNTHETIC_SAMPLE_TABLE_ID: sample_artifact.sha256,
                SYNTHETIC_REFERENCE_TABLE_ID: reference_artifact.sha256,
            },
            sample_rule="all validated supplied table rows; no aggregation",
            inspected_count=2,
            accepted_count=2,
            exclusions={},
            scanned_complete_input=True,
            stop_reason="complete validated tables",
            elapsed_ms=0,
            tool_version="1",
            parameters={
                "canonical_unit": "fraction",
                "inclusive_bounds": True,
                "partial_table": False,
            },
        ),
        limitations=(
            "Synthetic fixture; not biological or clinical evidence.",
            "Observed cohort ranges are not clinical normal intervals.",
            "Parent and child categories are never summed.",
        ),
    )


def build_synthetic_case() -> Case:
    """Return a fresh validated case with three deterministic curated claims."""

    sources = _sources()
    manifest = _manifest(sources)
    return build_case(
        case_id="case.synthetic.v1",
        dataset_id="dataset.synthetic.v1",
        manifest=manifest,
        sources=sources,
        claims=_claims(),
        tool_results=(_length_result(manifest), _range_result(manifest)),
    )


def load_case_bundle(path: str | Path) -> Case:
    """Load and strictly validate one local serialized case.

    Error messages intentionally omit the configured path and parser details so
    local filesystem information cannot flow into model-visible application data.
    """

    candidate = Path(path)
    try:
        size = candidate.stat().st_size
        if not candidate.is_file():
            raise OSError
        if size > MAX_CASE_BUNDLE_BYTES:
            raise CaseBundleLoadError(
                "Configured case bundle exceeds the 4 MiB size limit."
            )
        payload = candidate.read_bytes()
    except CaseBundleLoadError:
        raise
    except OSError:
        raise CaseBundleLoadError(
            "Configured case bundle is unavailable or is not a regular file."
        ) from None
    if len(payload) > MAX_CASE_BUNDLE_BYTES:
        raise CaseBundleLoadError(
            "Configured case bundle exceeds the 4 MiB size limit."
        )
    try:
        return Case.model_validate_json(payload)
    except (ValidationError, ValueError):
        raise CaseBundleLoadError(
            "Configured case bundle is malformed or fails contract validation."
        ) from None


def load_default_case(path: str | Path | None = None) -> Case:
    """Load explicit, environment, conventional-local, or synthetic case data."""

    if path is not None:
        return load_case_bundle(path)
    environment_path = os.environ.get("TRACEBACK_CASE_PATH")
    if environment_path:
        return load_case_bundle(environment_path)
    if DEFAULT_LOCAL_CASE_PATH.is_file():
        return load_case_bundle(DEFAULT_LOCAL_CASE_PATH)
    return build_synthetic_case()


SYNTHETIC_CASE = build_synthetic_case()

__all__ = [
    "CaseBundleLoadError",
    "DEFAULT_LOCAL_CASE_PATH",
    "MAX_CASE_BUNDLE_BYTES",
    "SYNTHETIC_CASE",
    "SYNTHETIC_LENGTHS",
    "SYNTHETIC_LENGTH_ARTIFACT_ID",
    "SYNTHETIC_REFERENCE_TABLE_ID",
    "SYNTHETIC_SAMPLE_TABLE_ID",
    "build_synthetic_case",
    "load_case_bundle",
    "load_default_case",
]
