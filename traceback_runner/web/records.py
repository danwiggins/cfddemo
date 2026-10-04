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
from typing import TYPE_CHECKING, Literal

from pydantic import Field, model_validator

from evidence_inspector.result_catalog import CatalogError, CatalogQuery
from traceback_runner.contracts import (
    FragmentMeasurementV2,
    PreflightReport,
    RunnerContract,
)
from traceback_runner.references import ReferenceProblem
from traceback_runner.serialization import canonical_json_bytes

from .contracts import validate_public_projection, validate_public_text
from .state_copy import (
    NOT_ASSIGNED,
    NOT_COMPARED,
    PREFLIGHT_NOT_AVAILABLE,
    STATE_COPY,
    copy_for,
)

if TYPE_CHECKING:
    from evidence_inspector.result_catalog import ResultCatalog
    from traceback_runner.store import JobStore

    from .explorer import ExplorerCatalogProjection, ExplorerDocument

RECORD_ID_PATTERN = re.compile(r"^record-[0-9a-f]{24}$")
SHORT_ID_LENGTH = 12
MAX_LIST_RECORDS = 500
LABEL_MAX_CHARS = 80
_LABEL_MAX_BYTES = 4096
_PREFLIGHT_MAX_BYTES = 1024 * 1024
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")


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


class RecordSummary(RunnerContract):
    """One catalog row; counts only for verified records."""

    record_id: str = Field(pattern=RECORD_ID_PATTERN.pattern)
    short_id: str
    status: Literal["verified", "failed_verification", "view_unavailable"]
    status_label: str
    label: str | None = None
    reference_id: str | None = None
    policy_label: str | None = None
    eligible_alignments: int | None = None
    records_scanned: int | None = None
    preflight: str | None = None
    preflight_label: str | None = None
    preflight_warnings: int | None = None
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
    """The read-side label grammar (A4b): 1-80 public-text characters."""

    if type(value) is not str:
        return None
    if (
        not 1 <= len(value) <= LABEL_MAX_CHARS
        or value != value.strip()
        or "/" in value
        or "\\" in value
        or _CONTROL.search(value)
    ):
        return None
    try:
        validate_public_text(value)
    except ValueError:
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


def _imported_at(root: Path, result_id: str) -> datetime | None:
    from traceback_runner.local_catalog import explorer_paths

    artifact, _ = explorer_paths(root, result_id)
    try:
        metadata = os.stat(artifact, follow_symlinks=False)
    except OSError:
        return None
    return datetime.fromtimestamp(int(metadata.st_mtime), UTC)


# ---------------------------------------------------------------------------
# Preflight from the job store (unsigned, receipt-verified)
# ---------------------------------------------------------------------------


def _preflight_report(source: LocalRecordSource, run_token: str) -> PreflightReport | None:
    """The run's preflight report, through its verified stage receipt."""

    from traceback_runner.receipts import verify_receipt

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
    except (OSError, ValueError, KeyError, TypeError):
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

    if not RECORD_ID_PATTERN.fullmatch(record_id):
        raise RecordNotFound(record_id)
    for ref, _ in _catalog_rows(query_catalog):
        if ref.bundle_record_id == record_id:
            return ref.result_id
    raise RecordNotFound(record_id)


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


def _verified_view(
    source: LocalRecordSource,
    get_document: GetDocument,
    result_id: str,
    record_id: str,
) -> LocalRecordView:
    from traceback_runner.local_authority import open_local_method_authority
    from traceback_runner.references import load_reference

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
    if type(measurement) is not FragmentMeasurementV2:
        raise RecordUnavailable("not a local measurement")
    measurement_sha256 = hashlib.sha256(canonical_json_bytes(measurement)).hexdigest()
    if measurement_sha256 != record.result_sha256:
        raise RecordUnavailable("measurement differs from the bound result digest")
    if verified.manifest.record_id != record_id:
        raise RecordUnavailable("bundle record identity changed")
    # (2) The on-disk authority must still be the one the row is bound to.
    registered = load_reference(source.root, measurement.reference_id).registered
    authority = open_local_method_authority(source.root, registered)
    capability = authority.capability
    if (
        capability.method_ref != ref.method_ref
        or capability.method_definition_sha256 != ref.method_definition_sha256
        or capability.registry_sha256 != ref.registry_sha256
        or capability.authority_head_sha256 != ref.authority_head_sha256
        or capability.authority_revision != ref.authority_revision
        or capability.as_of != ref.capability_as_of
    ):
        raise RecordUnavailable("the method authority on disk is not the bound one")
    policy = _policy(source.root, measurement, record.method.parameter_schema_sha256)
    report = _preflight_report(source, verified.provenance.run_token)
    preflight = _preflight_view(report)
    display_role = ref.display_role.value if ref.display_role is not None else NOT_ASSIGNED
    states = (
        _state("qualification", ref.qualification_state.value),
        _state("trust", "development_signature_verified"),
        _state("display_role", display_role),
        _state("reference_match", verified.limitations.reference_match),
        _state("preflight", preflight.outcome),
        _state("comparison", NOT_COMPARED),
    )
    view = LocalRecordView(
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
        warnings=_warning_count(preflight, verified.limitations.reference_match),
        states=states,
        imported_at=_imported_at(source.root, result_id),
    )
    validate_public_projection(view.model_dump(mode="json"))
    return view


_UNAVAILABLE = (CatalogError, ReferenceProblem, ValueError, KeyError, OSError, StopIteration)


def build_local_record_view(
    source: LocalRecordSource,
    *,
    get_document: GetDocument,
    query_catalog: QueryCatalog,
    record_id: str,
) -> LocalRecordView:
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


def _summary(view: LocalRecordView) -> RecordSummary:
    preflight = view.preflight
    return RecordSummary(
        record_id=view.record_id,
        short_id=view.short_id,
        status="verified",
        status_label=copy_for("record_status", "verified")[0],
        label=view.label,
        reference_id=view.reference_id,
        policy_label="built-in" if view.policy.builtin else view.policy.id,
        eligible_alignments=view.eligible_alignments,
        records_scanned=view.records_scanned,
        preflight=preflight.outcome,
        preflight_label=copy_for("preflight", preflight.outcome)[0],
        preflight_warnings=view.warnings,
        imported_at=view.imported_at,
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
    summaries: list[RecordSummary] = []
    for ref, has_view in rows[:MAX_LIST_RECORDS]:
        record_id = ref.bundle_record_id  # type: ignore[attr-defined]
        if not RECORD_ID_PATTERN.fullmatch(record_id):
            continue  # not a local record (never produced by `traceback run`)
        status: Literal["failed_verification", "view_unavailable"]
        if has_view:
            try:
                summaries.append(_summary(_verified_view(source, get_document, ref.result_id, record_id)))  # type: ignore[attr-defined]
                continue
            except (RecordUnavailable, *_UNAVAILABLE):
                status = "failed_verification"
        else:
            status = "view_unavailable"
        summaries.append(
            RecordSummary(
                record_id=record_id,
                short_id=short_record_id(record_id),
                status=status,
                status_label=copy_for("record_status", status)[0],
                label=read_record_label(source.root, record_id),
            )
        )
    summaries.sort(
        key=lambda item: (
            item.imported_at or datetime.min.replace(tzinfo=UTC),
            item.record_id,
        )
    )
    result = LocalRecordList(
        records=tuple(summaries),
        truncated=truncated,
        job_states=tuple(
            StateCopyRow(token=token, label=label, meaning=meaning)
            for token, (label, meaning) in STATE_COPY["job"].items()
        ),
    )
    validate_public_projection(result.model_dump(mode="json"))
    return result


__all__ = [
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
