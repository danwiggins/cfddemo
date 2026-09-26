"""Strict, immutable contracts for the evidence inspector.

The models in this module contain no file access, provider calls, or scientific
computation.  They define the trusted boundary shared by preparation, checks,
review, and UI code.
"""

from __future__ import annotations

import hashlib
import json
import math
from enum import StrEnum
from typing import Annotated, Any, Iterable, Mapping, Sequence

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StringConstraints,
    field_validator,
    model_validator,
)

Identifier = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$",
    ),
]
Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
NonEmptyText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]

MAX_SOURCES = 12
MAX_SOURCE_CHARACTERS = 20_000
MAX_CLAIM_CHARACTERS = 4_000
EXPECTED_CLAIM_COUNT = 3


class StrictModel(BaseModel):
    """Base for public contracts: immutable and closed to unknown fields."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
        validate_default=True,
    )


class LocatorKind(StrEnum):
    PAGE = "page"
    SECTION = "section"


class ClaimKind(StrEnum):
    MEASUREMENT = "measurement"
    COMPARISON = "comparison"
    INTERPRETATION = "interpretation"
    CAUSAL = "causal"


class ArtifactKind(StrEnum):
    BAM_PARENT = "bam_parent"
    DERIVED_LENGTHS = "derived_lengths"
    SAMPLE_TABLE = "sample_table"
    REFERENCE_TABLE = "reference_table"
    SOURCE_BUNDLE = "source_bundle"


class HashScope(StrEnum):
    FULL_ARTIFACT = "full_artifact"
    DERIVED_LENGTHS = "derived_lengths"
    PARENT_METADATA = "parent_metadata"


class IdentityVerification(StrEnum):
    VERIFIED = "verified"
    UNVERIFIED = "unverified"


class SampleLinkageStatus(StrEnum):
    VERIFIED = "verified"
    UNVERIFIED = "unverified"
    MISMATCH = "mismatch"


class TrimmingStatus(StrEnum):
    KNOWN = "known"
    UNKNOWN = "unknown"


class ToolName(StrEnum):
    READ_LENGTH_SUMMARY = "read_length_summary"
    COMPARE_REFERENCE_RANGES = "compare_reference_ranges"
    SOURCE_REVIEW = "source_review"
    INSUFFICIENT_INPUTS = "insufficient_inputs"


class ToolStatus(StrEnum):
    OK = "ok"
    UNAVAILABLE = "unavailable"
    INVALID_INPUT = "invalid_input"
    ERROR = "error"


class VerificationLevel(StrEnum):
    REPORTED = "reported"
    RECOMPUTED = "recomputed"
    SAMPLED_RECOMPUTED = "sampled_recomputed"


class AuditStatus(StrEnum):
    SUPPORTED = "supported"
    CONTRADICTED = "contradicted"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    HYPOTHESIS = "hypothesis"


class RangeClassification(StrEnum):
    BELOW = "below"
    WITHIN = "within"
    ABOVE = "above"


class ExecutionMode(StrEnum):
    LIVE = "live"
    CACHED = "cached"
    FIXTURE = "fixture"


class ErrorCode(StrEnum):
    INVALID_INPUT = "INVALID_INPUT"
    UNAVAILABLE = "UNAVAILABLE"
    MODEL_TIMEOUT = "MODEL_TIMEOUT"
    MODEL_FAILURE = "MODEL_FAILURE"
    INVALID_CITATION = "INVALID_CITATION"
    INVALID_NUMBER = "INVALID_NUMBER"
    STALE_RESULT = "STALE_RESULT"
    AUDIT_SAVE_FAILED = "AUDIT_SAVE_FAILED"


class SourceLocator(StrictModel):
    kind: LocatorKind
    value: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]

    @model_validator(mode="after")
    def validate_locator(self) -> SourceLocator:
        if self.kind == LocatorKind.PAGE:
            if not self.value.isdecimal() or int(self.value) < 1:
                raise ValueError("page locators must be positive integer strings")
        return self


def canonical_json_bytes(value: Any) -> bytes:
    """Serialize JSON-compatible data deterministically for content hashing."""

    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json", exclude_none=False)
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def sha256_bytes(content: bytes) -> str:
    """Return the lowercase SHA-256 digest of exact bytes."""

    return hashlib.sha256(content).hexdigest()


def sha256_text(text: str) -> str:
    """Hash exact submitted text without trimming or Unicode normalization."""

    return sha256_bytes(text.encode("utf-8"))


def source_content_digest(
    *,
    document_id: str,
    locator: SourceLocator,
    quote: str,
    table_row: str | None,
) -> str:
    """Digest source content and locator without a local path."""

    return sha256_bytes(
        canonical_json_bytes(
            {
                "document_id": document_id,
                "locator": locator.model_dump(mode="json"),
                "quote": quote,
                "table_row": table_row,
            }
        )
    )


class Source(StrictModel):
    id: Identifier
    document_id: Identifier
    locator: SourceLocator
    quote: Annotated[str, StringConstraints(min_length=1, max_length=10_000)]
    table_row: Annotated[str, StringConstraints(min_length=1, max_length=1_048_576)] | None = None
    content_sha256: Sha256

    @model_validator(mode="after")
    def validate_content_digest(self) -> Source:
        expected = source_content_digest(
            document_id=self.document_id,
            locator=self.locator,
            quote=self.quote,
            table_row=self.table_row,
        )
        if self.content_sha256 != expected:
            raise ValueError("content_sha256 does not match the source content")
        return self

    @classmethod
    def from_content(
        cls,
        *,
        id: str,
        document_id: str,
        locator: SourceLocator,
        quote: str,
        table_row: str | None = None,
    ) -> Source:
        """Build a source with a digest covering its exact public content."""

        return cls(
            id=id,
            document_id=document_id,
            locator=locator,
            quote=quote,
            table_row=table_row,
            content_sha256=source_content_digest(
                document_id=document_id,
                locator=locator,
                quote=quote,
                table_row=table_row,
            ),
        )


class Claim(StrictModel):
    id: Identifier
    source_ids: tuple[Identifier, ...] = Field(min_length=1)
    original_quote: Annotated[str, StringConstraints(min_length=1, max_length=10_000)]
    text: Annotated[str, StringConstraints(min_length=1, max_length=MAX_CLAIM_CHARACTERS)]
    kind: ClaimKind
    revision: int = Field(ge=1)

    @field_validator("source_ids")
    @classmethod
    def unique_source_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("source_ids must be unique")
        return value


def revise_claim(claim: Claim, text: str) -> Claim:
    """Return a new claim revision, preserving the immutable source quote."""

    if not text or not text.strip():
        raise ValueError("claim text must not be empty")
    if len(text) > MAX_CLAIM_CHARACTERS:
        raise ValueError(f"claim text must be at most {MAX_CLAIM_CHARACTERS} characters")
    if text == claim.text:
        return claim
    return claim.model_copy(update={"text": text, "revision": claim.revision + 1})


class ArtifactIdentity(StrictModel):
    id: Identifier
    kind: ArtifactKind
    label: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
    sha256: Sha256 | None
    hash_scope: HashScope
    size_bytes: int = Field(ge=0)
    identity_verification: IdentityVerification
    order: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_identity(self) -> ArtifactIdentity:
        if self.identity_verification == IdentityVerification.VERIFIED and self.sha256 is None:
            raise ValueError("verified artifact identities require sha256")
        if self.kind == ArtifactKind.DERIVED_LENGTHS:
            if self.sha256 is None or self.hash_scope != HashScope.DERIVED_LENGTHS:
                raise ValueError(
                    "derived length artifacts require a derived_lengths digest"
                )
        if self.kind != ArtifactKind.BAM_PARENT and self.order is not None:
            raise ValueError("order is only valid for parent BAM artifacts")
        return self


class SampleLinkage(StrictModel):
    status: SampleLinkageStatus
    evidence_ids: tuple[Identifier, ...] = ()
    operator_rationale: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1_000)
    ]

    @field_validator("evidence_ids")
    @classmethod
    def unique_evidence_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("linkage evidence_ids must be unique")
        return value

    @model_validator(mode="after")
    def verified_has_evidence(self) -> SampleLinkage:
        if self.status == SampleLinkageStatus.VERIFIED and not self.evidence_ids:
            raise ValueError("verified sample linkage requires evidence_ids")
        return self


class Trimming(StrictModel):
    status: TrimmingStatus
    processing_detail: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1_000)
    ]

    @model_validator(mode="after")
    def known_has_specific_detail(self) -> Trimming:
        if self.status == TrimmingStatus.KNOWN and self.processing_detail.lower() in {
            "unknown",
            "not known",
            "unverified",
        }:
            raise ValueError("known trimming requires concrete processing detail")
        return self


class Capability(StrictModel):
    name: Annotated[
        ToolName,
        Field(
            description="Executable check; insufficient_inputs is never a capability."
        ),
    ]
    available: bool
    artifact_ids: tuple[Identifier, ...] = ()
    unavailable_reason: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1_000)
    ] | None = None

    @model_validator(mode="after")
    def validate_availability(self) -> Capability:
        if self.name == ToolName.INSUFFICIENT_INPUTS:
            raise ValueError("insufficient_inputs is a selection outcome, not a capability")
        if self.available and self.unavailable_reason is not None:
            raise ValueError("available capabilities cannot have unavailable_reason")
        if not self.available and self.unavailable_reason is None:
            raise ValueError("unavailable capabilities require unavailable_reason")
        return self


class SelectionParameters(StrictModel):
    ordering_rule: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)
    ]
    max_accepted_reads: int = Field(default=100_000, ge=1, le=100_000)
    max_inspected_records: int = Field(default=1_000_000, ge=1, le=1_000_000)
    max_elapsed_seconds: float = Field(default=600.0, gt=0, le=600)
    max_serialized_artifact_bytes: int = Field(
        default=2_097_152,
        ge=1_024,
        le=16_777_216,
        description="Cap for the compact derived JSON, not parent BAM bytes.",
    )
    max_read_length_bp: int = Field(default=1_000_000, ge=1, le=1_000_000)
    deduplication_rule: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)
    ] = "first eligible primary record per read ID across ordered inputs"


class Manifest(StrictModel):
    schema_version: Identifier
    artifacts: tuple[ArtifactIdentity, ...]
    sample_linkage: SampleLinkage
    trimming: Trimming
    capabilities: tuple[Capability, ...]
    selection_parameters: SelectionParameters
    tool_versions: Mapping[Identifier, NonEmptyText]
    partial_collection: bool

    @model_validator(mode="after")
    def validate_manifest(self) -> Manifest:
        artifact_ids = [artifact.id for artifact in self.artifacts]
        if len(set(artifact_ids)) != len(artifact_ids):
            raise ValueError("artifact IDs must be unique")
        capability_names = [capability.name for capability in self.capabilities]
        if len(set(capability_names)) != len(capability_names):
            raise ValueError("capability names must be unique")
        known_artifacts = set(artifact_ids)
        for capability in self.capabilities:
            unknown = set(capability.artifact_ids) - known_artifacts
            if unknown:
                raise ValueError(
                    f"capability {capability.name} references unknown artifacts: "
                    f"{sorted(unknown)}"
                )
        return self


class CheckSelection(StrictModel):
    tool: ToolName
    reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2_000)]
    artifact_ids: tuple[Identifier, ...] = ()
    source_ids: tuple[Identifier, ...] = ()

    @model_validator(mode="after")
    def validate_shape(self) -> CheckSelection:
        if len(set(self.artifact_ids)) != len(self.artifact_ids):
            raise ValueError("artifact_ids must be unique")
        if len(set(self.source_ids)) != len(self.source_ids):
            raise ValueError("source_ids must be unique")
        if self.tool == ToolName.SOURCE_REVIEW:
            if self.artifact_ids or not self.source_ids:
                raise ValueError(
                    "source_review requires source_ids and forbids artifact_ids"
                )
        elif self.tool == ToolName.INSUFFICIENT_INPUTS:
            if self.artifact_ids or self.source_ids:
                raise ValueError("insufficient_inputs cannot request evidence")
        else:
            if not self.artifact_ids or self.source_ids:
                raise ValueError(
                    "numerical checks require artifact_ids and forbid source_ids"
                )
            expected_artifacts = (
                1 if self.tool == ToolName.READ_LENGTH_SUMMARY else 2
            )
            if len(self.artifact_ids) != expected_artifacts:
                raise ValueError(
                    f"{self.tool} requires exactly {expected_artifacts} artifact ID(s)"
                )
        return self


class LengthBin(StrictModel):
    length_bp: int = Field(ge=1, le=1_000)
    count: int = Field(ge=1)


class ReadLengthSummaryValues(StrictModel):
    bins: tuple[LengthBin, ...]
    overflow_count: int = Field(ge=0)
    valid_read_count: int = Field(ge=1)
    mode_bp: int = Field(ge=1)
    median_bp: float = Field(gt=0)
    fraction_100_150: float = Field(ge=0, le=1)
    fraction_gt_1000: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def validate_arithmetic(self) -> ReadLengthSummaryValues:
        lengths = [item.length_bp for item in self.bins]
        if lengths != sorted(set(lengths)):
            raise ValueError("length bins must be unique and strictly increasing")
        histogram_count = sum(item.count for item in self.bins)
        if histogram_count + self.overflow_count != self.valid_read_count:
            raise ValueError("histogram plus overflow must equal valid_read_count")
        expected_overflow_fraction = self.overflow_count / self.valid_read_count
        short_count = sum(
            item.count for item in self.bins if 100 <= item.length_bp <= 150
        )
        expected_short_fraction = short_count / self.valid_read_count
        if not math.isclose(
            self.fraction_gt_1000,
            expected_overflow_fraction,
            rel_tol=0,
            abs_tol=1e-12,
        ):
            raise ValueError("fraction_gt_1000 does not match its denominator")
        if not math.isclose(
            self.fraction_100_150,
            expected_short_fraction,
            rel_tol=0,
            abs_tol=1e-12,
        ):
            raise ValueError("fraction_100_150 does not match its denominator")
        return self


class ReferenceComparisonRow(StrictModel):
    cell_type_id: Identifier
    fraction: float = Field(ge=0, le=1)
    min_fraction: float = Field(ge=0, le=1)
    max_fraction: float = Field(ge=0, le=1)
    classification: RangeClassification
    source_ids: tuple[Identifier, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_classification(self) -> ReferenceComparisonRow:
        if self.min_fraction > self.max_fraction:
            raise ValueError("reference range minimum cannot exceed maximum")
        expected = (
            RangeClassification.BELOW
            if self.fraction < self.min_fraction
            else RangeClassification.ABOVE
            if self.fraction > self.max_fraction
            else RangeClassification.WITHIN
        )
        if self.classification != expected:
            raise ValueError("classification does not match the inclusive range")
        if len(set(self.source_ids)) != len(self.source_ids):
            raise ValueError("row source_ids must be unique")
        return self


class ReferenceRangeComparisonValues(StrictModel):
    rows: tuple[ReferenceComparisonRow, ...] = Field(min_length=1, max_length=100)
    partial_table: bool

    @model_validator(mode="after")
    def unique_cell_types(self) -> ReferenceRangeComparisonValues:
        ids = [row.cell_type_id for row in self.rows]
        if len(set(ids)) != len(ids):
            raise ValueError("comparison rows must have unique cell_type_id values")
        return self


class SourceReviewValues(StrictModel):
    reviewed_source_ids: tuple[Identifier, ...] = Field(min_length=1)
    scope: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)
    ]

    @field_validator("reviewed_source_ids")
    @classmethod
    def unique_reviewed_sources(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("reviewed_source_ids must be unique")
        return value


class ToolProvenance(StrictModel):
    artifact_ids: tuple[Identifier, ...] = ()
    artifact_digests: Mapping[Identifier, Sha256] = Field(default_factory=dict)
    sample_rule: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1_000)
    ] | None = None
    inspected_count: int | None = Field(default=None, ge=0)
    accepted_count: int | None = Field(default=None, ge=0)
    exclusions: Mapping[Identifier, int] = Field(default_factory=dict)
    scanned_complete_input: bool | None = None
    stop_reason: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)
    ] | None = None
    elapsed_ms: int = Field(ge=0)
    tool_version: NonEmptyText
    parameters: Mapping[str, JsonValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_counts(self) -> ToolProvenance:
        if (
            self.inspected_count is not None
            and self.accepted_count is not None
            and self.accepted_count > self.inspected_count
        ):
            raise ValueError("accepted_count cannot exceed inspected_count")
        if any(count < 0 for count in self.exclusions.values()):
            raise ValueError("exclusion counts cannot be negative")
        if set(self.artifact_digests) - set(self.artifact_ids):
            raise ValueError("artifact_digests keys must occur in artifact_ids")
        return self


class ToolResult(StrictModel):
    id: Identifier
    tool: Annotated[ToolName, Field(description="An executed tool, never insufficient_inputs.")]
    status: ToolStatus
    values: Mapping[str, JsonValue] = Field(default_factory=dict)
    units: Mapping[str, NonEmptyText] = Field(default_factory=dict)
    definitions: Mapping[str, NonEmptyText] = Field(default_factory=dict)
    denominator: NonEmptyText | None = None
    filters: tuple[NonEmptyText, ...] = ()
    source_ids: tuple[Identifier, ...] = ()
    verification_level: VerificationLevel | None
    provenance: ToolProvenance
    limitations: tuple[NonEmptyText, ...] = ()

    @model_validator(mode="after")
    def validate_result(self) -> ToolResult:
        if self.tool == ToolName.INSUFFICIENT_INPUTS:
            raise ValueError("insufficient_inputs does not execute a tool")
        if self.status == ToolStatus.OK:
            if not self.values:
                raise ValueError("successful tool results require values")
            if self.verification_level is None:
                raise ValueError("successful tool results require verification_level")
            value_model: type[StrictModel]
            if self.tool == ToolName.READ_LENGTH_SUMMARY:
                value_model = ReadLengthSummaryValues
            elif self.tool == ToolName.COMPARE_REFERENCE_RANGES:
                value_model = ReferenceRangeComparisonValues
            else:
                value_model = SourceReviewValues
            value_model.model_validate(self.values)
        elif self.values:
            raise ValueError("unsuccessful tool results cannot publish values")
        if len(set(self.source_ids)) != len(self.source_ids):
            raise ValueError("source_ids must be unique")
        unknown_unit_fields = set(self.units) - set(self.values)
        unknown_definition_fields = set(self.definitions) - set(self.values)
        if unknown_unit_fields or unknown_definition_fields:
            raise ValueError("units and definitions must refer to result value fields")
        return self


class NumericAssertion(StrictModel):
    evidence_id: Identifier
    field: Identifier
    value: int | float
    unit: NonEmptyText
    definition: NonEmptyText
    denominator: NonEmptyText | None = None
    filters: tuple[NonEmptyText, ...] = ()

    @field_validator("value")
    @classmethod
    def finite_number(cls, value: int | float) -> int | float:
        if isinstance(value, bool) or not math.isfinite(float(value)):
            raise ValueError("numeric assertion value must be finite")
        return value


class AuditBinding(StrictModel):
    claim_id: Identifier
    claim_revision: int = Field(ge=1)
    claim_text_hash: Sha256
    dataset_revision: Sha256


def bind_claim(claim: Claim, dataset_revision: str) -> AuditBinding:
    """Bind an audit to the exact claim text, revision, and evidence revision."""

    return AuditBinding(
        claim_id=claim.id,
        claim_revision=claim.revision,
        claim_text_hash=sha256_text(claim.text),
        dataset_revision=dataset_revision,
    )


class AuditResult(StrictModel):
    id: Identifier
    claim_id: Identifier
    claim_revision: int = Field(ge=1)
    claim_text_hash: Sha256
    dataset_revision: Sha256
    status: AuditStatus
    verification_level: VerificationLevel
    summary: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=10_000)]
    evidence_ids: tuple[Identifier, ...]
    numeric_assertions: tuple[NumericAssertion, ...] = ()
    tool_results: tuple[ToolResult, ...] = ()
    assumptions: tuple[NonEmptyText, ...] = ()
    missing_validation: tuple[NonEmptyText, ...] = ()
    revised_text: Annotated[str, StringConstraints(min_length=1, max_length=MAX_CLAIM_CHARACTERS)]
    next_checks: tuple[NonEmptyText, ...] = ()
    execution_mode: ExecutionMode
    model_id: Identifier
    prompt_version: Identifier

    @model_validator(mode="after")
    def validate_result_shape(self) -> AuditResult:
        if len(set(self.evidence_ids)) != len(self.evidence_ids):
            raise ValueError("evidence_ids must be unique")
        result_ids = [result.id for result in self.tool_results]
        if len(set(result_ids)) != len(result_ids):
            raise ValueError("tool result IDs must be unique")
        if self.status == AuditStatus.SUPPORTED and not self.evidence_ids:
            raise ValueError("supported audits require applicable evidence")
        if any(item.evidence_id not in self.evidence_ids for item in self.numeric_assertions):
            raise ValueError("numeric assertions must cite an audit evidence_id")
        return self

    @property
    def binding(self) -> AuditBinding:
        return AuditBinding(
            claim_id=self.claim_id,
            claim_revision=self.claim_revision,
            claim_text_hash=self.claim_text_hash,
            dataset_revision=self.dataset_revision,
        )


class AuditError(StrictModel):
    code: ErrorCode
    message: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2_000)]
    retryable: bool
    fix: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2_000)]


def canonical_dataset_revision(
    manifest: Manifest, sources: Sequence[Source]
) -> str:
    """Hash all immutable public evidence identities and processing settings."""

    ordered_sources = sorted(sources, key=lambda source: source.id)
    payload = {
        "manifest": manifest.model_dump(mode="json", exclude_none=False),
        "sources": [
            source.model_dump(mode="json", exclude_none=False)
            for source in ordered_sources
        ],
    }
    return sha256_bytes(canonical_json_bytes(payload))


class Case(StrictModel):
    case_id: Identifier
    dataset_id: Identifier
    dataset_revision: Sha256
    manifest: Manifest
    sources: tuple[Source, ...] = Field(max_length=MAX_SOURCES)
    claims: tuple[Claim, ...] = Field(
        min_length=EXPECTED_CLAIM_COUNT, max_length=EXPECTED_CLAIM_COUNT
    )
    tool_results: tuple[ToolResult, ...] = ()

    @model_validator(mode="after")
    def validate_case(self) -> Case:
        source_ids = [source.id for source in self.sources]
        claim_ids = [claim.id for claim in self.claims]
        result_ids = [result.id for result in self.tool_results]
        if len(set(source_ids)) != len(source_ids):
            raise ValueError("source IDs must be unique")
        if len(set(claim_ids)) != len(claim_ids):
            raise ValueError("claim IDs must be unique")
        if len(set(result_ids)) != len(result_ids):
            raise ValueError("tool result IDs must be unique")
        if set(source_ids) & set(result_ids):
            raise ValueError("source and tool result evidence IDs must be disjoint")
        if sum(len(source.quote) for source in self.sources) > MAX_SOURCE_CHARACTERS:
            raise ValueError(
                f"source quotes exceed the {MAX_SOURCE_CHARACTERS}-character limit"
            )
        known_sources = set(source_ids)
        known_artifacts = {artifact.id for artifact in self.manifest.artifacts}
        unknown_linkage_evidence = (
            set(self.manifest.sample_linkage.evidence_ids)
            - known_sources
            - known_artifacts
        )
        if unknown_linkage_evidence:
            raise ValueError(
                "sample linkage references unknown evidence: "
                f"{sorted(unknown_linkage_evidence)}"
            )
        for claim in self.claims:
            unknown = set(claim.source_ids) - known_sources
            if unknown:
                raise ValueError(
                    f"claim {claim.id} references unknown sources: {sorted(unknown)}"
                )
            quotes = {
                source.quote for source in self.sources if source.id in claim.source_ids
            }
            if claim.original_quote not in quotes:
                raise ValueError(
                    f"claim {claim.id} original_quote must exactly match a cited source"
                )
        for result in self.tool_results:
            unknown_sources = set(result.source_ids) - known_sources
            unknown_artifacts = (
                set(result.provenance.artifact_ids) - known_artifacts
            )
            if unknown_sources:
                raise ValueError(
                    f"tool result {result.id} references unknown sources: "
                    f"{sorted(unknown_sources)}"
                )
            if unknown_artifacts:
                raise ValueError(
                    f"tool result {result.id} references unknown artifacts: "
                    f"{sorted(unknown_artifacts)}"
                )
        expected_revision = canonical_dataset_revision(self.manifest, self.sources)
        if self.dataset_revision != expected_revision:
            raise ValueError("dataset_revision does not match manifest and sources")
        return self

    def source_by_id(self, source_id: str) -> Source:
        """Resolve one exact source ID; no lexical or path lookup is performed."""

        for source in self.sources:
            if source.id == source_id:
                return source
        raise KeyError(f"unknown source ID: {source_id}")

    def claim_by_id(self, claim_id: str) -> Claim:
        for claim in self.claims:
            if claim.id == claim_id:
                return claim
        raise KeyError(f"unknown claim ID: {claim_id}")


def build_case(
    *,
    case_id: str,
    dataset_id: str,
    manifest: Manifest,
    sources: Sequence[Source],
    claims: Sequence[Claim],
    tool_results: Sequence[ToolResult] = (),
) -> Case:
    """Build a case and derive its revision from immutable evidence."""

    source_tuple = tuple(sources)
    return Case(
        case_id=case_id,
        dataset_id=dataset_id,
        dataset_revision=canonical_dataset_revision(manifest, source_tuple),
        manifest=manifest,
        sources=source_tuple,
        claims=tuple(claims),
        tool_results=tuple(tool_results),
    )


def resolve_sources(case: Case, source_ids: Iterable[str]) -> tuple[Source, ...]:
    """Resolve explicit source IDs in request order, rejecting duplicates."""

    requested = tuple(source_ids)
    if len(set(requested)) != len(requested):
        raise ValueError("source IDs must be unique")
    return tuple(case.source_by_id(source_id) for source_id in requested)


def validate_check_selection(selection: CheckSelection, case: Case) -> CheckSelection:
    """Validate a model-selected check against one immutable case revision."""

    if selection.tool == ToolName.INSUFFICIENT_INPUTS:
        return selection
    capabilities = {
        capability.name: capability for capability in case.manifest.capabilities
    }
    capability = capabilities.get(selection.tool)
    if capability is None or not capability.available:
        raise ValueError(f"tool is unavailable for this case: {selection.tool}")
    allowed_artifacts = set(capability.artifact_ids)
    if not set(selection.artifact_ids).issubset(allowed_artifacts):
        raise ValueError("selection references an artifact outside the tool capability")
    resolve_sources(case, selection.source_ids)
    return selection


def binding_is_current(
    binding: AuditBinding,
    *,
    claim: Claim,
    dataset_revision: str,
) -> bool:
    """Return whether a result binding still matches current session state."""

    return binding == bind_claim(claim, dataset_revision)


def audit_is_stale(
    audit: AuditResult,
    *,
    claim: Claim,
    dataset_revision: str,
) -> bool:
    """Return true when any claim/dataset binding component has changed."""

    return not binding_is_current(
        audit.binding,
        claim=claim,
        dataset_revision=dataset_revision,
    )


def least_verification_level(
    levels: Iterable[VerificationLevel],
) -> VerificationLevel:
    """Apply the contract's conclusion-level verification precedence."""

    present = set(levels)
    if not present:
        raise ValueError("at least one verification level is required")
    for level in (
        VerificationLevel.REPORTED,
        VerificationLevel.SAMPLED_RECOMPUTED,
        VerificationLevel.RECOMPUTED,
    ):
        if level in present:
            return level
    raise AssertionError("unreachable verification level")


def validate_numeric_assertions(audit: AuditResult) -> None:
    """Reject numbers whose field, value, unit, or semantics do not match evidence."""

    result_by_id = {result.id: result for result in audit.tool_results}
    for assertion in audit.numeric_assertions:
        result = result_by_id.get(assertion.evidence_id)
        if result is None or result.status != ToolStatus.OK:
            raise ValueError(
                f"numeric assertion cites no successful tool result: "
                f"{assertion.evidence_id}"
            )
        if assertion.field not in result.values:
            raise ValueError(f"unknown evidence field: {assertion.field}")
        observed = result.values[assertion.field]
        if isinstance(observed, bool) or not isinstance(observed, (int, float)):
            raise ValueError(f"evidence field is not numeric: {assertion.field}")
        if not math.isclose(
            float(assertion.value), float(observed), rel_tol=0.0, abs_tol=1e-12
        ):
            raise ValueError(f"numeric assertion value mismatch: {assertion.field}")
        if result.units.get(assertion.field) != assertion.unit:
            raise ValueError(f"numeric assertion unit mismatch: {assertion.field}")
        if result.definitions.get(assertion.field) != assertion.definition:
            raise ValueError(
                f"numeric assertion definition mismatch: {assertion.field}"
            )
        if result.denominator != assertion.denominator:
            raise ValueError(
                f"numeric assertion denominator mismatch: {assertion.field}"
            )
        if result.filters != assertion.filters:
            raise ValueError(f"numeric assertion filters mismatch: {assertion.field}")


def validate_audit_result(audit: AuditResult, case: Case) -> AuditResult:
    """Validate citations, evidence applicability, and conclusion verification."""

    claim = case.claim_by_id(audit.claim_id)
    if not binding_is_current(
        audit.binding, claim=claim, dataset_revision=case.dataset_revision
    ):
        raise ValueError("audit binding does not match the current case")
    available_evidence = {source.id for source in case.sources} | {
        result.id for result in audit.tool_results
    }
    unknown = set(audit.evidence_ids) - available_evidence
    if unknown:
        raise ValueError(f"audit references unknown evidence IDs: {sorted(unknown)}")
    validate_numeric_assertions(audit)
    cited_levels: list[VerificationLevel] = []
    source_ids = {source.id for source in case.sources}
    result_by_id = {result.id: result for result in audit.tool_results}
    for evidence_id in audit.evidence_ids:
        if evidence_id in source_ids:
            cited_levels.append(VerificationLevel.REPORTED)
        else:
            result = result_by_id[evidence_id]
            if result.status != ToolStatus.OK or result.verification_level is None:
                raise ValueError("unsuccessful evidence cannot support an audit")
            cited_levels.append(result.verification_level)
    if cited_levels:
        expected = least_verification_level(cited_levels)
        if audit.verification_level != expected:
            raise ValueError(
                "audit verification_level does not match cited evidence precedence"
            )
    return audit


__all__ = [
    "ArtifactIdentity",
    "ArtifactKind",
    "AuditBinding",
    "AuditError",
    "AuditResult",
    "AuditStatus",
    "Capability",
    "Case",
    "CheckSelection",
    "Claim",
    "ClaimKind",
    "ErrorCode",
    "ExecutionMode",
    "HashScope",
    "IdentityVerification",
    "LocatorKind",
    "Manifest",
    "NumericAssertion",
    "RangeClassification",
    "ReadLengthSummaryValues",
    "ReferenceComparisonRow",
    "ReferenceRangeComparisonValues",
    "SampleLinkage",
    "SampleLinkageStatus",
    "SelectionParameters",
    "Source",
    "SourceLocator",
    "SourceReviewValues",
    "ToolName",
    "ToolProvenance",
    "ToolResult",
    "ToolStatus",
    "Trimming",
    "TrimmingStatus",
    "VerificationLevel",
    "audit_is_stale",
    "bind_claim",
    "binding_is_current",
    "build_case",
    "canonical_dataset_revision",
    "canonical_json_bytes",
    "least_verification_level",
    "resolve_sources",
    "revise_claim",
    "sha256_bytes",
    "sha256_text",
    "source_content_digest",
    "validate_audit_result",
    "validate_check_selection",
    "validate_numeric_assertions",
]
