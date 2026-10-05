"""Per-request, non-persisted views of local records for the site (usability C1/C5).

``LocalRecordView`` is built for each request and never stored or digested.  It
is not the E07 ``fragment`` model (that contract compares two distinct v1
records); it is one verified local record's own measurement.

Each view is built only through the authority-bound path:

1. the explorer's live reader (``IntegratedExplorerSource.get``) replays the
   bound method capability, re-verifies the catalog's copy of the bundle and
   rejects a stale or revoked authority;
2. the on-disk method authority for the record's reference is reopened and must
   still equal the capability the catalog row is bound to (an authority edited
   after ``serve`` started makes the record unavailable);
3. the catalog re-verifies its bundle copy (``verify_reference``) for the signed
   measurement, whose canonical SHA-256 must equal the result digest bound in
   step 1.

Any failure makes the record unavailable (HTTP 503, ``TBX-WEB-503``): nothing
from it is shown.  The operator label (``ROOT/labels/<record_id>.json``, written
by ``traceback label`` / ``run --label``) is unsigned and optional; a missing or
invalid label file means no label.  The run's preflight report comes from the
job store, is not part of the signed record, and is shown only when its receipt
verifies.

Every value here is unqualified, local and not for clinical use.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, Field, model_validator

from evidence_inspector.result_catalog import CatalogError, CatalogQuery
from traceback_runner.contracts import (
    FragmentMeasurementV2,
    PreflightReport,
    RunnerContract,
)
from traceback_runner.measurement_schemas import (
    FRAGMENT_VIEW_SCHEMA,
    BundleMeasurementSchema,
    measurement_schema,
    schema_version_of,
)
from traceback_runner.references import ReferenceProblem
from traceback_runner.serialization import canonical_json_bytes

from .contracts import validate_public_projection, validate_public_text
from .state_copy import (
    CURRENT_METHOD_VERSION,
    EARLIER_METHOD_VERSION,
    NOT_ASSIGNED,
    NOT_COMPARED,
    PREFLIGHT_NOT_AVAILABLE,
    STATE_COPY,
    copy_for,
)

if TYPE_CHECKING:
    from evidence_inspector.result_catalog import ResultCatalog
    from traceback_runner.store import JobStore

    from traceback_runner.bundles import VerifiedBundle
    from traceback_runner.local_authority import MethodAuthority

    from .explorer import ExplorerCatalogProjection, ExplorerDocument

RECORD_ID_PATTERN = re.compile(r"^record-[0-9a-f]{24}$")
SHORT_ID_LENGTH = 12
MAX_LIST_RECORDS = 500
LABEL_MAX_CHARS = 80
_LABEL_MAX_BYTES = 4096
_PREFLIGHT_MAX_BYTES = 1024 * 1024
INPUT_DIGEST_LENGTH = 12
_SHA256_NAME = re.compile(r"^[0-9a-f]{64}$")
#: The fixed banner every non-fragment record view carries (signal SH5).
ANALYSIS_BANNER = "Unqualified. Local development record. Not for clinical use. Descriptive only."
AnalysisToken = Literal["fragment", "cell_origin", "copy_number"]


class RecordNotFound(KeyError):
    """No catalog row carries this record ID (HTTP 404)."""


class RecordUnavailable(RuntimeError):
    """The record failed verification or its authority is stale (HTTP 503)."""


# ---------------------------------------------------------------------------
# Models (not persisted, not digested)
# ---------------------------------------------------------------------------


class PolicyBin(RunnerContract):
    lower: int = Field(ge=0)
    upper: int | None = Field(default=None, gt=0)


class RecordPolicy(RunnerContract):
    id: str = Field(min_length=1, max_length=128)
    builtin: bool
    min_mapq: int | None = Field(default=None, ge=0, le=255)
    bins: tuple[PolicyBin, ...] = Field(min_length=1)


class HistogramRow(RunnerContract):
    lower: int = Field(ge=0)
    upper: int | None = Field(default=None, gt=0)
    count: int = Field(ge=0)


class ExclusionRow(RunnerContract):
    reason: str
    stage: Literal["acceptance", "eligibility"]
    label: str
    count: int = Field(ge=0)


class PreflightCheckView(RunnerContract):
    code: str
    outcome: str
    summary: str | None = None


class PreflightView(RunnerContract):
    outcome: str
    origin: Literal["job_store", "not_available"]
    checks: tuple[PreflightCheckView, ...] = ()


class StateRow(RunnerContract):
    axis: str
    token: str
    label: str
    meaning: str


class LocalRecordView(RunnerContract):
    """The fragment-length record view; unchanged by the signal methods (SH5)."""

    schema_version: Literal["traceback.local-record-view.v1"] = (
        "traceback.local-record-view.v1"
    )
    record_id: str = Field(pattern=RECORD_ID_PATTERN.pattern)
    short_id: str
    result_id: str = Field(pattern=r"^result_[0-9a-f]{40}$")
    label: str | None = None
    reference_id: str
    policy: RecordPolicy
    method_version: str
    records_scanned: int = Field(ge=0)
    eligible_alignments: int = Field(ge=0)
    exclusions: tuple[ExclusionRow, ...]
    histogram: tuple[HistogramRow, ...] = Field(min_length=1)
    measurement_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    preflight: PreflightView
    warnings: int = Field(ge=0)
    states: tuple[StateRow, ...]
    imported_at: datetime | None = None

    @model_validator(mode="after")
    def reconciles(self) -> LocalRecordView:
        if sum(row.count for row in self.histogram) != self.eligible_alignments:
            raise ValueError("histogram counts must sum to eligible_alignments")
        if (
            self.eligible_alignments + sum(row.count for row in self.exclusions)
            != self.records_scanned
        ):
            raise ValueError("eligible plus exclusions must equal records_scanned")
        if tuple(PolicyBin(lower=row.lower, upper=row.upper) for row in self.histogram) != (
            self.policy.bins
        ):
            raise ValueError("histogram rows must follow the policy bins")
        return self


class LocalAnalysisRecordView(RunnerContract):
    """A non-fragment record's view: a common envelope around its analysis body.

    The view schema, the analysis and the body come from the measurement
    schema's registered :class:`~traceback_runner.measurement_schemas.RecordViewBinding`.
    The envelope always carries the fixed :data:`ANALYSIS_BANNER`.
    """

    schema_version: str = Field(
        pattern=r"^traceback\.local-[a-z0-9]+(?:-[a-z0-9]+)*-view\.v[1-9][0-9]*$"
    )
    analysis: Literal["cell_origin", "copy_number"]
    analysis_label: str
    banner: Literal[
        "Unqualified. Local development record. Not for clinical use. Descriptive only."
    ] = "Unqualified. Local development record. Not for clinical use. Descriptive only."
    record_id: str = Field(pattern=RECORD_ID_PATTERN.pattern)
    short_id: str
    result_id: str = Field(pattern=r"^result_[0-9a-f]{40}$")
    label: str | None = None
    reference_id: str
    method_version: str
    measurement_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    input_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{12}$")
    key_count: int = Field(ge=0)
    key_count_unit: str = Field(min_length=1, max_length=64)
    preflight: PreflightView
    warnings: int = Field(ge=0)
    states: tuple[StateRow, ...]
    imported_at: datetime | None = None
    body: dict[str, Any]

    @model_validator(mode="after")
    def not_the_fragment_view(self) -> LocalAnalysisRecordView:
        if self.schema_version == FRAGMENT_VIEW_SCHEMA:
            raise ValueError("the fragment view schema is reserved for fragment records")
        return self


AnyRecordView = LocalRecordView | LocalAnalysisRecordView


class RecordSummary(RunnerContract):
    """One catalog row; counts only for verified records.

    The fields are closed and common to every analysis: no estimate (a mixture
    or tumour fraction, a ploidy) is ever projected here
    (``tests/web/test_record_dispatch.py`` pins the field set).
    """

    record_id: str = Field(pattern=RECORD_ID_PATTERN.pattern)
    short_id: str
    status: Literal["verified", "failed_verification", "view_unavailable"]
    status_label: str
    label: str | None = None
    analysis: AnalysisToken | None = None
    analysis_label: str | None = None
    input_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{12}$")
    key_count: int | None = Field(default=None, ge=0)
    key_count_unit: str | None = None
    method_version_state: str | None = None
    reference_id: str | None = None
    policy_label: str | None = None
    eligible_alignments: int | None = None
    records_scanned: int | None = None
    preflight: str | None = None
    preflight_label: str | None = None
    preflight_warnings: int | None = None
    warning_texts: tuple[str, ...] = ()
    method_version: str | None = None
    imported_at: datetime | None = None


class StateCopyRow(RunnerContract):
    token: str
    label: str
    meaning: str


class LocalRecordList(RunnerContract):
    schema_version: Literal["traceback.local-record-list.v1"] = (
        "traceback.local-record-list.v1"
    )
    records: tuple[RecordSummary, ...]
    truncated: bool
    job_states: tuple[StateCopyRow, ...]
    analyses: tuple[StateCopyRow, ...] = ()


# ---------------------------------------------------------------------------
# The source the server holds
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LocalRecordSource:
    """What the record routes read: ROOT, the explorer's catalog, the job store.

    ``catalog`` must be the same open catalog the installed explorer source
    reads, so both checks see one catalog.  ``store`` is optional: without it
    preflight details are reported as unavailable.
    """

    root: Path
    catalog: ResultCatalog
    store: JobStore | None = None


GetDocument = Callable[[str], "ExplorerDocument"]
QueryCatalog = Callable[[CatalogQuery], "ExplorerCatalogProjection"]


def short_record_id(record_id: str) -> str:
    return record_id.removeprefix("record-")[:SHORT_ID_LENGTH]


def _state(axis: str, token: str) -> StateRow:
    label, meaning = copy_for(axis, token)
    return StateRow(axis=axis, token=token, label=label, meaning=meaning)


# ---------------------------------------------------------------------------
# Unsigned side files: label and imported time
# ---------------------------------------------------------------------------


def _read_private_bounded(path: Path, maximum: int) -> bytes | None:
    """No-follow, owner-only, bounded read; ``None`` when absent or unsafe."""

    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        return None
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_size > maximum
        ):
            return None
        content = os.read(descriptor, maximum + 1)
        return None if len(content) > maximum else content
    except OSError:
        return None
    finally:
        os.close(descriptor)


def valid_label(value: object) -> str | None:
    """The read side of the one label grammar (A4b, ``labels.label_violation``)."""

    from traceback_runner.labels import label_violation

    if type(value) is not str or label_violation(value) is not None:
        return None
    return value


def read_record_label(root: Path, record_id: str) -> str | None:
    """``ROOT/labels/<record_id>.json`` -> label, or ``None`` (never raises)."""

    if not RECORD_ID_PATTERN.fullmatch(record_id):
        return None
    labels = root / "labels"
    if labels.is_symlink():
        return None
    content = _read_private_bounded(labels / f"{record_id}.json", _LABEL_MAX_BYTES)
    if content is None:
        return None
    try:
        payload = json.loads(content)
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    return valid_label(payload.get("label"))


def _imported_at_ns(root: Path, result_id: str) -> int | None:
    """Exact import time (nanoseconds) of a record's persisted explorer artifact."""

    from traceback_runner.local_catalog import explorer_paths

    artifact, _ = explorer_paths(root, result_id)
    try:
        return os.stat(artifact, follow_symlinks=False).st_mtime_ns
    except OSError:
        return None


def _imported_at(root: Path, result_id: str) -> datetime | None:
    stamp = _imported_at_ns(root, result_id)
    return None if stamp is None else datetime.fromtimestamp(stamp / 1e9, UTC)


# ---------------------------------------------------------------------------
# Preflight from the job store (unsigned, receipt-verified)
# ---------------------------------------------------------------------------


def _preflight_report(source: LocalRecordSource, run_token: str) -> PreflightReport | None:
    """The run's preflight report, through its verified stage receipt."""

    from traceback_runner.receipts import ReceiptError, verify_receipt

    store = source.store
    prefix = run_token.removeprefix("local-run-")
    if store is None or prefix == run_token or not re.fullmatch(r"[0-9a-f]{16}", prefix):
        return None
    try:
        matches = [
            item.job_id for item in store.list_jobs(limit=1000) if item.job_id.startswith(prefix)
        ]
        if len(matches) != 1:
            return None
        job_id = matches[0]
        definition = store.committed_stage_definitions(job_id).get("validate")
        if definition is None:
            return None
        stage_root = source.root / "runner" / "artifacts" / job_id / "validate"
        if stage_root.is_symlink() or not stage_root.is_dir():
            return None
        candidates = []
        for directory in stage_root.iterdir():
            if directory.is_symlink() or not directory.is_dir():
                continue
            receipt = verify_receipt(directory)
            if receipt.stage_definition_sha256 == definition:
                candidates.append((receipt.contract.attempt, directory, receipt))
        if not candidates:
            return None
        latest = max(item[0] for item in candidates)
        chosen = [item for item in candidates if item[0] == latest]
        if len(chosen) != 1:
            return None
        _, directory, receipt = chosen[0]
        locator = receipt.output_locators.get("preflight_report")
        if locator is None:
            return None
        content = _read_private_bounded(directory / locator, _PREFLIGHT_MAX_BYTES)
        if content is None:
            return None
        return PreflightReport.model_validate_json(content)
    except (OSError, ValueError, KeyError, TypeError, ReceiptError):
        # Optional, unsigned metadata: any damage means "not available".
        return None


def _public_or_none(value: str) -> str | None:
    """``value`` if it passes the public-text boundary, else ``None``.

    Tag lists such as ``M5/AS`` are spelled with commas first, since a slash
    reads as a path to the boundary.
    """

    value = value.replace("/", ", ")
    try:
        validate_public_text(value)
    except ValueError:
        return None
    return value


def _preflight_view(report: PreflightReport | None) -> PreflightView:
    if report is None:
        return PreflightView(outcome=PREFLIGHT_NOT_AVAILABLE, origin="not_available")
    return PreflightView(
        outcome=report.outcome.value,
        origin="job_store",
        checks=tuple(
            PreflightCheckView(
                code=check.code,
                outcome=check.outcome.value,
                summary=_public_or_none(check.problem),
            )
            for check in report.checks
            if check.outcome.value != "pass"
        ),
    )


def _warning_count(preflight: PreflightView, reference_match: str) -> int:
    """Non-passing preflight checks; without a report, the signed record's own.

    The signed limitations say when the reference was matched by name and
    length only (preflight's M5 warning), so that one warning is counted even
    when the job store has no report.
    """

    if preflight.origin == "job_store":
        return len(preflight.checks)
    return 1 if reference_match == "name_and_length_only" else 0


# ---------------------------------------------------------------------------
# Policy, exclusions and the view itself
# ---------------------------------------------------------------------------


def _policy(
    root: Path,
    measurement: FragmentMeasurementV2,
    parameter_schema_sha256: str,
) -> RecordPolicy:
    """Describe the policy from the authority-bound method definition.

    The built-in policy is recognised only when the record's definition ID
    names it and the method definition's parameter digest equals the digest
    of the locked built-in policy for the registered reference; its MAPQ then
    comes from that policy.  Anything else is described from the signed
    measurement alone, without a MAPQ.
    """

    from traceback_runner.local_authority import LOCAL_POLICY_ID, local_fragment_policy
    from traceback_runner.references import load_reference

    bins = tuple(
        PolicyBin(lower=item.bin.lower_inclusive, upper=item.bin.upper_exclusive)
        for item in measurement.histogram
    )
    if measurement.definition_id == f"{LOCAL_POLICY_ID}.{measurement.reference_id}":
        registered = load_reference(root, measurement.reference_id).registered
        policy = local_fragment_policy(registered)
        if hashlib.sha256(canonical_json_bytes(policy)).hexdigest() == parameter_schema_sha256:
            policy_bins = tuple(
                PolicyBin(lower=item.lower_inclusive, upper=item.upper_exclusive)
                for item in policy.bins
            )
            if policy_bins != bins:
                raise RecordUnavailable("record bins differ from the built-in policy")
            return RecordPolicy(
                id="built-in",
                builtin=True,
                min_mapq=policy.min_mapping_quality,
                bins=bins,
            )
    return RecordPolicy(id=measurement.definition_id, builtin=False, bins=bins)


def _exclusions(measurement: FragmentMeasurementV2, min_mapq: int | None) -> tuple[ExclusionRow, ...]:
    excluded = measurement.exclusions
    mapq = f"Below MAPQ {min_mapq}" if min_mapq is not None else "Below the policy's MAPQ"
    rows = (
        ("unmapped", "acceptance", "Unmapped records", excluded.unmapped),
        ("secondary", "acceptance", "Secondary alignments", excluded.secondary),
        ("supplementary", "acceptance", "Supplementary alignments", excluded.supplementary),
        ("qc_failure", "acceptance", "QC-failed alignments", excluded.qc_failure),
        ("duplicate", "acceptance", "Duplicate alignments", excluded.duplicate),
        ("low_mapping_quality", "eligibility", mapq, excluded.low_mapping_quality),
        ("unregistered_contig", "eligibility", "Contig outside the policy", excluded.unregistered_contig),
        ("no_reference_span", "eligibility", "No reference span", excluded.no_reference_span),
    )
    return tuple(
        ExclusionRow(reason=reason, stage=stage, label=label, count=count)  # type: ignore[arg-type]
        for reason, stage, label, count in rows
    )


def resolve_result_id(query_catalog: QueryCatalog, record_id: str) -> str:
    """Map a public record ID to its catalog result ID (exact match only)."""

    from traceback_runner.local_authority import local_catalog_aliases

    if not RECORD_ID_PATTERN.fullmatch(record_id):
        raise RecordNotFound(record_id)
    # Import gives every local record a deterministic, unique display alias;
    # query by it (an exact, unbounded lookup), then require the record ID.
    alias = local_catalog_aliases(record_id).display_alias
    page = query_catalog(CatalogQuery(limit=10, display_alias=alias))
    matches = [item.ref for item in page.results if item.ref.bundle_record_id == record_id]
    if len(matches) != 1:
        raise RecordNotFound(record_id)
    return matches[0].result_id


def _catalog_rows(query_catalog: QueryCatalog) -> list[tuple[object, bool]]:
    """Every catalog row (bounded), with ``has_registered_view``."""

    rows: list[tuple[object, bool]] = []
    cursor: str | None = None
    while len(rows) <= MAX_LIST_RECORDS:
        query = CatalogQuery(limit=100, cursor=cursor) if cursor else CatalogQuery(limit=100)
        page = query_catalog(query)
        rows.extend((item.ref, item.has_registered_view) for item in page.results)
        cursor = page.next_cursor
        if cursor is None:
            break
    return rows


def _input_digest(verified: VerifiedBundle) -> str | None:
    """A short digest that groups records made from one sealed input.

    It is a prefix of the signed provenance's keyed commitment to the sealed
    analysis BAM (an HMAC under ROOT's provenance key), so records made from
    the same input in one ROOT share it; it is never a raw content digest.
    """

    for artifact in verified.provenance.artifacts:
        if artifact.role == "analysis_bam":
            return artifact.provider_hmac_sha256[:INPUT_DIGEST_LENGTH]
    return None


def _capability_is_bound(capability: Any, ref: Any) -> bool:
    return bool(
        capability.method_ref == ref.method_ref
        and capability.method_definition_sha256 == ref.method_definition_sha256
        and capability.registry_sha256 == ref.registry_sha256
        and capability.authority_head_sha256 == ref.authority_head_sha256
        and capability.authority_revision == ref.authority_revision
        and capability.as_of == ref.capability_as_of
    )


def _later_store_exists(root: Path, store: MethodAuthority) -> bool:
    """Whether a valid sibling store (same reference and slug) was published later.

    Read-only and never raises: the bound store is already validated, and a
    damaged or unreadable sibling is ignored (it hides only its own records).
    This is the "made under an earlier method version" state, never a failure.
    """

    from traceback_runner.local_authority import (
        METHOD_AUTHORITY_DIRECTORY,
        LocalAuthorityProblem,
        open_method_authority,
    )

    directory = root / METHOD_AUTHORITY_DIRECTORY / store.reference_id / store.method_slug
    try:
        names = sorted(entry.name for entry in directory.iterdir())
    except OSError:
        return False
    published = store.registry.published_at
    for name in names:
        if name == store.method_definition_sha256 or not _SHA256_NAME.fullmatch(name):
            continue
        try:
            sibling = open_method_authority(root, store.reference_id, store.method_slug, name)
        except (LocalAuthorityProblem, ValueError, OSError):
            continue
        if sibling.registry.published_at > published:
            return True
    return False


def _bound_v4_authority(root: Path, reference_id: str, ref: Any) -> bool:
    """Reopen the authority a v4 row is bound to; return whether it is earlier.

    Every store under ``ROOT/method-authority/<reference>/<slug>/`` named by the
    row's definition hash is reopened and validated (never written); the one
    whose capability equals the row's is the record's authority.  Damaged or
    differently bound stores are skipped, so they hide only their own records.
    Without a bound method store, the built-in fragment store must be the
    bound one.  Otherwise the record is unavailable (HTTP 503); a later sibling
    store is a state, never a failure.
    """

    from traceback_runner.local_authority import (
        METHOD_AUTHORITY_DIRECTORY,
        LocalAuthorityProblem,
        open_local_method_authority,
        open_method_authority,
        validate_method_slug,
    )
    from traceback_runner.references import load_reference

    definition_sha256 = ref.method_definition_sha256
    base = root / METHOD_AUTHORITY_DIRECTORY / reference_id
    try:
        entries = (
            sorted(entry.name for entry in base.iterdir())
            if base.is_dir() and not base.is_symlink()
            else []
        )
    except OSError:
        entries = []  # unreadable tree: only the built-in store can bind the row
    for slug in entries:
        try:
            validate_method_slug(slug)
        except ValueError:
            continue  # a staging directory or a stray entry
        try:
            store = open_method_authority(root, reference_id, slug, definition_sha256)
        except (LocalAuthorityProblem, ValueError, OSError):
            # Absent, damaged or unreadable: only records bound to it are hidden.
            continue
        if _capability_is_bound(store.capability, ref):
            return _later_store_exists(root, store)
    registered = load_reference(root, reference_id).registered
    capability = open_local_method_authority(root, registered).capability
    if not _capability_is_bound(capability, ref):
        raise RecordUnavailable("no method authority on disk is the bound one")
    return False


def _states(ref: Any, reference_match: str, preflight: PreflightView) -> tuple[StateRow, ...]:
    display_role = ref.display_role.value if ref.display_role is not None else NOT_ASSIGNED
    return (
        _state("qualification", ref.qualification_state.value),
        _state("trust", "development_signature_verified"),
        _state("display_role", display_role),
        _state("reference_match", reference_match),
        _state("preflight", preflight.outcome),
        _state("comparison", NOT_COMPARED),
    )


def _analysis_view(
    source: LocalRecordSource,
    spec: BundleMeasurementSchema,
    verified: VerifiedBundle,
    ref: Any,
    *,
    result_id: str,
    record_id: str,
    measurement_sha256: str,
) -> LocalAnalysisRecordView:
    """A v4 record's view, built by the view its measurement schema registered."""

    binding = spec.record_view
    if binding is None:
        raise RecordUnavailable("no record view is registered for this measurement schema")
    measurement: Any = verified.measurement
    reference_id = measurement.reference_id
    # (2) The on-disk authority must still be the one the row is bound to.
    earlier = _bound_v4_authority(source.root, reference_id, ref)
    body = binding.build_body(measurement)
    if not isinstance(body, BaseModel):
        raise RecordUnavailable("the registered view body is not a contract")
    key_count = binding.key_count(measurement)
    if type(key_count) is not int:
        raise RecordUnavailable("the registered key count is not an integer")
    reference_match = verified.limitations.reference_match  # type: ignore[union-attr]
    preflight = _preflight_view(_preflight_report(source, verified.provenance.run_token))
    method_state = EARLIER_METHOD_VERSION if earlier else CURRENT_METHOD_VERSION
    return LocalAnalysisRecordView(
        schema_version=binding.view_schema_version,
        analysis=binding.analysis,  # type: ignore[arg-type]
        analysis_label=copy_for("analysis", binding.analysis)[0],
        record_id=record_id,
        short_id=short_record_id(record_id),
        result_id=result_id,
        label=read_record_label(source.root, record_id),
        reference_id=reference_id,
        method_version=ref.method_ref.version,
        measurement_sha256=measurement_sha256,
        input_digest=_input_digest(verified),
        key_count=key_count,
        key_count_unit=binding.key_count_unit,
        preflight=preflight,
        warnings=_warning_count(preflight, reference_match),
        states=(
            *_states(ref, reference_match, preflight),
            _state("method_version", method_state),
        ),
        imported_at=_imported_at(source.root, result_id),
        body=body.model_dump(mode="json"),
    )


def _fragment_view(
    source: LocalRecordSource,
    verified: VerifiedBundle,
    ref: Any,
    record: Any,
    *,
    result_id: str,
    record_id: str,
    measurement_sha256: str,
) -> LocalRecordView:
    """The fragment-length view (``traceback.local-record-view.v1``), unchanged."""

    from traceback_runner.local_authority import open_local_method_authority
    from traceback_runner.references import load_reference

    measurement = verified.measurement
    if type(measurement) is not FragmentMeasurementV2:
        raise RecordUnavailable("not a local measurement")
    # (2) The on-disk authority must still be the one the row is bound to.
    registered = load_reference(source.root, measurement.reference_id).registered
    authority = open_local_method_authority(source.root, registered)
    if not _capability_is_bound(authority.capability, ref):
        raise RecordUnavailable("the method authority on disk is not the bound one")
    policy = _policy(source.root, measurement, record.method.parameter_schema_sha256)
    report = _preflight_report(source, verified.provenance.run_token)
    preflight = _preflight_view(report)
    reference_match = verified.limitations.reference_match  # type: ignore[union-attr]
    return LocalRecordView(
        record_id=record_id,
        short_id=short_record_id(record_id),
        result_id=result_id,
        label=read_record_label(source.root, record_id),
        reference_id=measurement.reference_id,
        policy=policy,
        method_version=ref.method_ref.version,
        records_scanned=measurement.records_scanned,
        eligible_alignments=measurement.eligible_alignments,
        exclusions=_exclusions(measurement, policy.min_mapq),
        histogram=tuple(
            HistogramRow(
                lower=item.bin.lower_inclusive,
                upper=item.bin.upper_exclusive,
                count=item.count,
            )
            for item in measurement.histogram
        ),
        measurement_sha256=measurement_sha256,
        preflight=preflight,
        warnings=_warning_count(preflight, reference_match),
        states=_states(ref, reference_match, preflight),
        imported_at=_imported_at(source.root, result_id),
    )


def _verified(
    source: LocalRecordSource,
    get_document: GetDocument,
    result_id: str,
    record_id: str,
) -> tuple[AnyRecordView, str | None]:
    """One record's verified view (dispatched by measurement type) and input digest."""

    from traceback_runner.bundles import RESULT_BUNDLE_V4

    # (1) The authority-bound live read: replays the bound capability and
    # re-verifies the bundle; a stale or revoked authority raises here.
    document = get_document(result_id)
    ref = document.models.catalog_ref
    if ref.result_id != result_id or ref.bundle_record_id != record_id:
        raise RecordUnavailable("catalog row identity changed")
    record = next(
        item.record
        for item in document.models.result_view_request.sources
        if item.record.result_id == result_id
    )
    # (3) The signed measurement from the catalog's verified bundle copy.
    verified, _ = source.catalog.verify_reference(ref)
    measurement = verified.measurement
    measurement_sha256 = hashlib.sha256(canonical_json_bytes(measurement)).hexdigest()
    if measurement_sha256 != record.result_sha256:
        raise RecordUnavailable("measurement differs from the bound result digest")
    if verified.manifest.record_id != record_id:
        raise RecordUnavailable("bundle record identity changed")
    view: AnyRecordView
    if type(measurement) is FragmentMeasurementV2:
        if verified.manifest.schema_version == RESULT_BUNDLE_V4:
            raise RecordUnavailable("a fragment measurement never travels in v4")
        view = _fragment_view(
            source,
            verified,
            ref,
            record,
            result_id=result_id,
            record_id=record_id,
            measurement_sha256=measurement_sha256,
        )
    else:
        spec = measurement_schema(schema_version_of(measurement))
        if (
            verified.manifest.schema_version != RESULT_BUNDLE_V4
            or spec is None
            or type(measurement) is not spec.measurement_model
        ):
            raise RecordUnavailable("not a local measurement")
        view = _analysis_view(
            source,
            spec,
            verified,
            ref,
            result_id=result_id,
            record_id=record_id,
            measurement_sha256=measurement_sha256,
        )
    validate_public_projection(view.model_dump(mode="json"))
    return view, _input_digest(verified)


def _verified_view(
    source: LocalRecordSource,
    get_document: GetDocument,
    result_id: str,
    record_id: str,
) -> AnyRecordView:
    """Dispatch by measurement type: fragment keeps its v1 view; v4 its registered one."""

    return _verified(source, get_document, result_id, record_id)[0]


_UNAVAILABLE = (CatalogError, ReferenceProblem, ValueError, KeyError, OSError, StopIteration)


def build_local_record_view(
    source: LocalRecordSource,
    *,
    get_document: GetDocument,
    query_catalog: QueryCatalog,
    record_id: str,
) -> AnyRecordView:
    """One verified record's view.  ``RecordNotFound`` or ``RecordUnavailable``."""

    if type(source) is not LocalRecordSource:
        raise TypeError("record routes require the package-owned record source")
    result_id = resolve_result_id(query_catalog, record_id)
    try:
        return _verified_view(source, get_document, result_id, record_id)
    except RecordUnavailable:
        raise
    except _UNAVAILABLE as exc:
        raise RecordUnavailable("record failed verification") from exc


def _warning_texts(view: AnyRecordView) -> tuple[str, ...]:
    texts = [
        f"{check.code}: {check.summary or 'see the operator guide for this code'}"
        for check in view.preflight.checks
    ]
    if view.preflight.origin != "job_store" and view.warnings:
        texts.append("Reference matched by contig name and length only (stated in the signed record)")
    return tuple(texts)


FRAGMENT_KEY_COUNT_UNIT = "eligible alignments"


def _summary(view: AnyRecordView, input_digest: str | None) -> RecordSummary:
    """The catalog row: the common columns only, never an estimate."""

    preflight = view.preflight
    common = {
        "record_id": view.record_id,
        "short_id": view.short_id,
        "status": "verified",
        "status_label": copy_for("record_status", "verified")[0],
        "label": view.label,
        "input_digest": input_digest,
        "reference_id": view.reference_id,
        "preflight": preflight.outcome,
        "preflight_label": copy_for("preflight", preflight.outcome)[0],
        "preflight_warnings": view.warnings,
        "warning_texts": _warning_texts(view),
        "method_version": view.method_version,
        "imported_at": view.imported_at,
    }
    if isinstance(view, LocalRecordView):
        return RecordSummary(
            **common,
            analysis="fragment",
            analysis_label=copy_for("analysis", "fragment")[0],
            key_count=view.eligible_alignments,
            key_count_unit=FRAGMENT_KEY_COUNT_UNIT,
            policy_label="built-in" if view.policy.builtin else view.policy.id,
            eligible_alignments=view.eligible_alignments,
            records_scanned=view.records_scanned,
        )
    method_state = next(row.token for row in view.states if row.axis == "method_version")
    return RecordSummary(
        **common,
        analysis=view.analysis,
        analysis_label=copy_for("analysis", view.analysis)[0],
        key_count=view.key_count,
        key_count_unit=view.key_count_unit,
        method_version_state=method_state,
    )


def build_local_record_list(
    source: LocalRecordSource,
    *,
    get_document: GetDocument,
    query_catalog: QueryCatalog,
) -> LocalRecordList:
    """Every catalog row, each verified on its own; failures do not hide others."""

    if type(source) is not LocalRecordSource:
        raise TypeError("record routes require the package-owned record source")
    rows = _catalog_rows(query_catalog)
    truncated = len(rows) > MAX_LIST_RECORDS
    summaries: list = []
    for ref, has_view in rows[:MAX_LIST_RECORDS]:
        record_id = ref.bundle_record_id  # type: ignore[attr-defined]
        if not RECORD_ID_PATTERN.fullmatch(record_id):
            continue  # not a local record (never produced by `traceback run`)
        status: Literal["failed_verification", "view_unavailable"]
        if has_view:
            try:
                summaries.append(
                    (
                        _imported_at_ns(source.root, ref.result_id),  # type: ignore[attr-defined]
                        _summary(*_verified(source, get_document, ref.result_id, record_id)),  # type: ignore[attr-defined]
                    )
                )
                continue
            except (RecordUnavailable, *_UNAVAILABLE):
                status = "failed_verification"
        else:
            status = "view_unavailable"
        stamp = _imported_at_ns(source.root, ref.result_id)  # type: ignore[attr-defined]
        summaries.append(
            (
                stamp,
                RecordSummary(
                    record_id=record_id,
                    short_id=short_record_id(record_id),
                    status=status,
                    status_label=copy_for("record_status", status)[0],
                    label=read_record_label(source.root, record_id),
                    imported_at=(
                        None if stamp is None else datetime.fromtimestamp(stamp / 1e9, UTC)
                    ),
                ),
            )
        )
    # Oldest import first by exact nanosecond mtime (the displayed datetime is
    # only microsecond-precise); a row with no known import time goes last.
    summaries.sort(
        key=lambda item: (item[0] is None, item[0] or 0, item[1].record_id)
    )
    summaries = [summary for _, summary in summaries]
    result = LocalRecordList(
        records=tuple(summaries),
        truncated=truncated,
        job_states=tuple(
            StateCopyRow(token=token, label=label, meaning=meaning)
            for token, (label, meaning) in STATE_COPY["job"].items()
        ),
        analyses=tuple(
            StateCopyRow(token=token, label=label, meaning=meaning)
            for token, (label, meaning) in STATE_COPY["analysis"].items()
        ),
    )
    validate_public_projection(result.model_dump(mode="json"))
    return result


__all__ = [
    "ANALYSIS_BANNER",
    "AnyRecordView",
    "LocalAnalysisRecordView",
    "LocalRecordList",
    "LocalRecordSource",
    "LocalRecordView",
    "RecordNotFound",
    "RecordSummary",
    "RecordUnavailable",
    "build_local_record_list",
    "build_local_record_view",
    "read_record_label",
    "resolve_result_id",
    "short_record_id",
    "valid_label",
]
