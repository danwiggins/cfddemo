"""``traceback catalog import``: catalog one local record and persist its explorer view.

Golden-path B5a/B5b.  The import:

1. reads the record's reference ID (unverified; only to choose the authority),
2. creates or validates the local method authority for that reference
   (``ROOT/authority/<reference_id>``) and the pinned v2 result-trust registry
   (``ROOT/trust/result-trust-registry``),
3. imports the bundle into ``ROOT/catalog`` through E04 with the trust
   registry (never a caller ``TrustStore``) and the local bundle reader
   registry, so the row is ``development_unqualified``,
4. builds the E06 result view from the verified bundle and writes the
   canonical ``ExplorerArtifactRecord`` and its ``CatalogAuthorityBinding`` to
   ``ROOT/explorer/{artifacts,bindings}/<result_id>.json`` (0600, temp + fsync
   + no-replace link).

Every record here is unqualified, local and not for clinical use.  No input
path is written to the catalog or the explorer files.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from .local_authority import (
    LocalAuthorityProblem,
    LocalMethodAuthority,
    ensure_local_method_authority,
    local_catalog_aliases,
    open_local_method_authority,
    open_local_result_trust_registry,
    sync_local_result_trust,
)
from .references import ReferenceProblem, load_reference
from .serialization import canonical_json_bytes

if TYPE_CHECKING:
    from evidence_inspector.result_catalog import CatalogResultRef, ResultCatalog
    from evidence_inspector.result_trust_registry import ResultTrustRegistry
    from traceback_runner.bundles import VerifiedBundle
    from traceback_runner.web.explorer import (
        CatalogAuthorityBinding,
        ExplorerArtifactRecord,
        IntegratedExplorerSource,
    )

CATALOG_DIRECTORY = "catalog"
EXPLORER_DIRECTORY = "explorer"
ARTIFACTS_DIRECTORY = "artifacts"
BINDINGS_DIRECTORY = "bindings"
_IMPORT_ROOT_ID = "root_local_records"

# Closed vocabulary only (security must-fix 6): no operator-entered text.
ACCESSIBLE_LABEL = "Fragment length, unqualified local record"
QC_LABEL = "unqualified"
_FILTER_ID = "filter_local_unqualified"
_COMPATIBILITY_POLICY_ID = "policy_local_unqualified"
_RESULT_SCHEMA_ID = "schema_fragment_measurement"
_RESULT_SCHEMA_VERSION = "2.0.0"  # traceback.fragment-measurement.v2


class CatalogImportProblem(ReferenceProblem):
    """``TBX-CAT-001``: the input is not a verifiable local record; nothing changed."""


def _not_a_record(cause: str) -> CatalogImportProblem:
    return CatalogImportProblem(
        "TBX-CAT-001",
        "Not a verifiable local record bundle; no catalog row was written",
        cause=cause,
        fix=(
            "Pass a record directory made by `traceback run` under ROOT/records, and "
            "check it with `traceback verify RECORD_ID --root ROOT`"
        ),
    )


@dataclass(frozen=True)
class CatalogImportOutcome:
    reference: CatalogResultRef
    record_id: str
    explorer_artifact: str  # written | unchanged | repaired
    authority_binding: str  # written | unchanged | repaired


# ---------------------------------------------------------------------------
# Unverified peek (only selects which authority to open)
# ---------------------------------------------------------------------------


_MAX_PEEK_BYTES = 4 * 1024 * 1024


def _read_peek(path: Path) -> bytes:
    """Read one small bundle file, bounded, before any verification."""

    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError("bundle file is not a regular file")
        content = os.read(descriptor, _MAX_PEEK_BYTES + 1)
        if len(content) > _MAX_PEEK_BYTES:
            raise ValueError("bundle file exceeds its byte bound")
        while len(content) <= _MAX_PEEK_BYTES:
            chunk = os.read(descriptor, _MAX_PEEK_BYTES + 1 - len(content))
            if not chunk:
                break
            content += chunk
        if len(content) > _MAX_PEEK_BYTES:
            raise ValueError("bundle file exceeds its byte bound")
        return content
    finally:
        os.close(descriptor)


def _peek_local_record(bundle: Path) -> tuple[str, str]:
    """Return (record_id, reference_id) of a v3 local bundle; nothing is trusted yet."""

    from .bundles import MANIFEST_PATH, MEASUREMENT_PATH
    from .contracts import FragmentMeasurementV2, ResultBundleManifestV3, parse_fragment_measurement
    from .serialization import canonical_model_from_bytes

    if bundle.is_symlink() or not bundle.is_dir():
        raise _not_a_record("the path is not a record directory")
    try:
        manifest_bytes = _read_peek(bundle / MANIFEST_PATH)
        if json.loads(manifest_bytes).get("schema_version") != "traceback.result-bundle.v3":
            raise _not_a_record("the bundle is not a local (result-bundle v3) record")
        manifest = canonical_model_from_bytes(ResultBundleManifestV3, manifest_bytes)
        measurement = parse_fragment_measurement(_read_peek(bundle / MEASUREMENT_PATH))
    except CatalogImportProblem:
        raise
    except (OSError, ValueError, ValidationError, AttributeError):
        raise _not_a_record("the bundle manifest or measurement is missing or invalid") from None
    if type(measurement) is not FragmentMeasurementV2:
        raise _not_a_record("the bundle does not carry a local measurement")
    return manifest.record_id, measurement.reference_id


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------


def open_local_catalog(
    root: Path,
    trust: ResultTrustRegistry,
    *,
    import_root: Path | None = None,
) -> ResultCatalog:
    """Open ``ROOT/catalog`` bound to the trust registry and the local reader registry."""

    from evidence_inspector.result_catalog import (
        LOCAL_RESULT_BUNDLE_READER_REGISTRY,
        ResultCatalog,
    )

    return ResultCatalog(
        root / CATALOG_DIRECTORY,
        import_roots={
            _IMPORT_ROOT_ID: (import_root or (root / "records")).absolute(),
        },
        result_trust_registry=trust,
        reader_registry=LOCAL_RESULT_BUNDLE_READER_REGISTRY,
    )


# ---------------------------------------------------------------------------
# B5b: the E06 view of one imported local record
# ---------------------------------------------------------------------------


def _hex40(tag: bytes, value: str) -> str:
    return hashlib.sha256(tag + value.encode("utf-8")).hexdigest()[:40]


def _denominator(verified: VerifiedBundle) -> Any:
    from evidence_inspector.result_view import (
        AttritionReason,
        AttritionStage,
        CountState,
        CountValue,
        DenominatorLedger,
    )

    measurement = verified.measurement
    excluded = measurement.exclusions

    def observed(value: int, label: str) -> CountValue:
        return CountValue(state=CountState.OBSERVED, value=value, accessible_label=label)

    def reason(stage: AttritionStage, code: str, value: int, label: str) -> AttritionReason:
        return AttritionReason(
            stage=stage,
            reason_code=f"reason_{code}",
            accessible_label=label,
            count=observed(value, label),
        )

    acceptance = (
        ("duplicate", excluded.duplicate, "Duplicate alignments"),
        ("qc_failure", excluded.qc_failure, "QC-failed alignments"),
        ("secondary", excluded.secondary, "Secondary alignments"),
        ("supplementary", excluded.supplementary, "Supplementary alignments"),
        ("unmapped", excluded.unmapped, "Unmapped records"),
    )
    eligibility = (
        ("low_mapping_quality", excluded.low_mapping_quality, "Below MAPQ 20"),
        ("no_reference_span", excluded.no_reference_span, "No reference span"),
        ("unregistered_contig", excluded.unregistered_contig, "Contig outside the policy"),
    )
    accepted = measurement.records_scanned - sum(item[1] for item in acceptance)
    attrition = tuple(
        sorted(
            (
                *(reason(AttritionStage.ACCEPTANCE, *item) for item in acceptance),
                *(reason(AttritionStage.ELIGIBILITY, *item) for item in eligibility),
                reason(AttritionStage.DISPLAY, "none_withheld", 0, "None withheld"),
            ),
            key=lambda item: item.sort_key,
        )
    )
    return DenominatorLedger(
        input_records=observed(measurement.records_scanned, "Records scanned"),
        accepted_records=observed(accepted, "Primary mapped alignments"),
        eligible_records=observed(measurement.eligible_alignments, "Eligible alignments"),
        displayed_records=observed(measurement.eligible_alignments, "Displayed alignments"),
        attrition=attrition,
    )


def build_local_explorer_artifact(
    reference: CatalogResultRef,
    verified: VerifiedBundle,
    authority: LocalMethodAuthority,
) -> tuple[ExplorerArtifactRecord, CatalogAuthorityBinding]:
    """Build the canonical E06 artifact and E04 binding for one imported local record.

    The record's compatibility decision is against a derived *no-comparator*
    placeholder (``result_nocomparator_*``, execution ``not_run``): a single
    local record has no registered comparison, so the decision is ``unknown``
    and never allows a delta or shared axis.  Grid, atlas and panel assets do not
    apply to fragment length and stay unset.
    """

    from evidence_inspector.compatibility import (
        AllowedMethodDefinition,
        CompatibilityPolicy,
        CompatibilityPolicyReference,
        CompatibilityRequest,
        ExecutionState,
        InformationState,
        MeasurementCompatibilityKey,
        MeasurementCompatibilityPolicy,
        ResultSchemaReference,
        TrustState,
        VerifiedMeasurementRecord,
        compatibility_policy_sha256,
        decide_compatibility,
    )
    from evidence_inspector.result_view import (
        ResultViewRequest,
        bind_result_view_source,
        build_result_view,
        normalize_result_filters,
    )
    from traceback_runner.web.explorer import (
        CatalogAuthorityBinding,
        ExplorerArtifactRecord,
    )

    definition = authority.definition
    capability = authority.capability
    if (
        reference.method_ref != definition.method_ref
        or reference.method_definition_sha256 != capability.method_definition_sha256
        or reference.capability_as_of != capability.as_of
    ):
        raise ValueError("catalog row is not bound to this local authority")
    schema = ResultSchemaReference(
        schema_id=_RESULT_SCHEMA_ID, version=_RESULT_SCHEMA_VERSION
    )
    policy_ref = CompatibilityPolicyReference(
        policy_id=_COMPATIBILITY_POLICY_ID, version="1.0.0"
    )
    key = MeasurementCompatibilityKey(
        measurement_family=definition.family,
        quantity_id=definition.quantity_id,
        unit=definition.unit,
        result_schema=schema,
        reference_asset=definition.assets[0],
        grid_asset=None,
        atlas_asset=None,
        panel_asset=None,
        normalization_semantics_id="sem_fragment_length_histogram",
        coordinate_semantics_id="sem_aligned_reference_span",
        denominator_semantics_id="sem_eligible_alignments",
        registered_policy=policy_ref,
    )
    hex40 = reference.result_id.removeprefix("result_")
    record = VerifiedMeasurementRecord(
        result_id=reference.result_id,
        result_sha256=reference.bundle_manifest_sha256,
        bundle_id=f"bundle_{hex40}",
        bundle_sha256=reference.bundle_sha256,
        method=definition,
        method_definition_sha256=reference.method_definition_sha256,
        current_capability=capability,
        execution_state=ExecutionState.COMPLETE,
        information_state=InformationState.SUFFICIENT,
        trust_state=TrustState.VERIFIED,
        compatibility_key=key,
    )
    placeholder_hex = _hex40(b"traceback.local-no-comparator.v1\0", reference.result_id)
    placeholder = VerifiedMeasurementRecord(
        result_id=f"result_nocomparator_{placeholder_hex}",
        result_sha256=hashlib.sha256(
            b"traceback.local-no-comparator.result.v1\0" + reference.result_id.encode()
        ).hexdigest(),
        bundle_id=f"bundle_nocomparator_{placeholder_hex}",
        bundle_sha256=hashlib.sha256(
            b"traceback.local-no-comparator.bundle.v1\0" + reference.result_id.encode()
        ).hexdigest(),
        method=definition,
        method_definition_sha256=reference.method_definition_sha256,
        current_capability=capability,
        execution_state=ExecutionState.NOT_RUN,
        information_state=InformationState.UNKNOWN,
        trust_state=TrustState.UNKNOWN,
        compatibility_key=key,
    )
    policy = CompatibilityPolicy(
        policy_id=policy_ref.policy_id,
        version=policy_ref.version,
        registry_sha256=capability.registry_sha256,
        registry_version=capability.registry_version,
        authority_head_sha256=capability.authority_head_sha256,
        authority_revision=capability.authority_revision,
        measurement_policies=(
            MeasurementCompatibilityPolicy(
                measurement_family=definition.family,
                quantity_id=definition.quantity_id,
                unit=definition.unit,
                allowed_method_definitions=(
                    AllowedMethodDefinition(
                        method_ref=definition.method_ref,
                        method_definition_sha256=reference.method_definition_sha256,
                    ),
                ),
                allowed_result_schemas=(schema,),
                delta_allowed_when_comparable=False,
                shared_axis_allowed_when_comparable=False,
            ),
        ),
    )
    decision = decide_compatibility(
        CompatibilityRequest(
            left=record,
            right=placeholder,
            policy=policy,
            trusted_policy_sha256=compatibility_policy_sha256(policy),
            trusted_authority_head_sha256=capability.authority_head_sha256,
        )
    )
    source = bind_result_view_source(
        record=record,
        compatibility_decision=decision,
        denominator=_denominator(verified),
        accessible_label=ACCESSIBLE_LABEL,
        qc_label=QC_LABEL,
    )
    request = ResultViewRequest(
        filter_id=_FILTER_ID,
        sources=(source,),
        filters=normalize_result_filters(),
    )
    artifact = ExplorerArtifactRecord(
        result_id=reference.result_id,
        result_view_request=request,
        result_view=build_result_view(request),
    )
    binding = CatalogAuthorityBinding(
        result_id=reference.result_id,
        context=authority.verification_context(),
    )
    return artifact, binding


# ---------------------------------------------------------------------------
# B5b: durable, fail-closed persistence
# ---------------------------------------------------------------------------


def _private_directory(path: Path) -> Path:
    if path.is_symlink() or (path.exists() and not path.is_dir()):
        raise OSError(f"{path.name} is not a private directory")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    metadata = os.stat(path, follow_symlinks=False)
    if metadata.st_uid != os.geteuid():
        raise OSError(f"{path.name} is owned by another user")
    if stat.S_IMODE(metadata.st_mode) != 0o700:
        os.chmod(path, 0o700)
    return path


def explorer_paths(root: Path, result_id: str) -> tuple[Path, Path]:
    base = root / EXPLORER_DIRECTORY
    return (
        base / ARTIFACTS_DIRECTORY / f"{result_id}.json",
        base / BINDINGS_DIRECTORY / f"{result_id}.json",
    )


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _persist_once(path: Path, content: bytes) -> str:
    """Write ``content`` once (temp + fsync + no-replace link), 0600.

    An existing file with identical bytes is left untouched (``unchanged``).
    An existing file with other bytes (truncated, edited) is replaced
    atomically by the rebuilt bytes (``repaired``): every byte is derived from
    the verified bundle and the validated authority, so nothing is lost.
    """

    directory = _private_directory(path.parent)
    existing: bytes | None = None
    if path.is_symlink():
        path.unlink()
    elif path.exists():
        if not path.is_file():
            raise OSError("explorer file path is not a regular file")
        metadata = os.stat(path, follow_symlinks=False)
        if metadata.st_uid != os.geteuid():
            raise OSError("explorer file is owned by another user")
        # Restore the private mode first, so even an unreadable file is repaired.
        mode_repaired = stat.S_IMODE(metadata.st_mode) != 0o600
        if mode_repaired:
            os.chmod(path, 0o600)
        existing = path.read_bytes()
        if existing == content:
            return "repaired" if mode_repaired else "unchanged"
    temporary = directory / f".{path.name}.{os.getpid()}.tmp"
    temporary.unlink(missing_ok=True)
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if existing is None:
            os.link(temporary, path)
        else:
            os.replace(temporary, path)
        _fsync_directory(directory)
    finally:
        temporary.unlink(missing_ok=True)
    return "written" if existing is None else "repaired"


def persist_explorer_artifact(
    root: Path,
    artifact: ExplorerArtifactRecord,
    binding: CatalogAuthorityBinding,
) -> tuple[str, str]:
    """Validate, then persist the artifact and its binding; returns both states."""

    from traceback_runner.web.explorer import (
        CanonicalExplorerArtifactRepository,
        CatalogAuthorityIndex,
    )

    if artifact.result_id != binding.result_id:
        raise ValueError("explorer artifact and binding name different results")
    # The same validation the explorer applies on load: replay plus the public
    # projection boundary.  Nothing invalid reaches disk.
    CanonicalExplorerArtifactRepository((artifact,))
    CatalogAuthorityIndex((binding,))
    _private_directory(root / EXPLORER_DIRECTORY)
    artifact_path, binding_path = explorer_paths(root, artifact.result_id)
    binding_state = _persist_once(binding_path, canonical_json_bytes(binding))
    artifact_state = _persist_once(artifact_path, canonical_json_bytes(artifact))
    return artifact_state, binding_state


@dataclass(frozen=True)
class LoadedExplorerArtifacts:
    records: tuple[Any, ...]
    bindings: tuple[Any, ...]
    skipped: int


def _read_bounded_regular(path: Path, maximum: int) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_size > maximum
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            raise ValueError("explorer file is not a private bounded regular file")
        chunks = []
        remaining = maximum + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(remaining, 1 << 20))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        content = b"".join(chunks)
        if len(content) > maximum:
            raise ValueError("explorer file exceeds its byte bound")
        return content
    finally:
        os.close(descriptor)


def load_explorer_artifacts(root: Path) -> LoadedExplorerArtifacts:
    """Load every persisted artifact with its binding; skip and count invalid ones.

    A file that is truncated, edited, non-canonical, misnamed or unpaired is
    skipped, so its catalog row reports ``has_registered_view=false`` and its
    detail is unavailable while every other row still serves.
    """

    from traceback_runner.web.explorer import (
        MAX_EXPLORER_ARTIFACT_BYTES,
        CanonicalExplorerArtifactRepository,
        CatalogAuthorityBinding,
        CatalogAuthorityIndex,
        ExplorerArtifactRecord,
    )

    base = root / EXPLORER_DIRECTORY
    artifacts_dir = base / ARTIFACTS_DIRECTORY
    bindings_dir = base / BINDINGS_DIRECTORY
    artifacts_present = artifacts_dir.is_dir() and not artifacts_dir.is_symlink()
    artifact_paths = sorted(artifacts_dir.glob("*.json")) if artifacts_present else []
    records = []
    bindings = []
    skipped = 0
    for path in artifact_paths:
        result_id = path.name.removesuffix(".json")
        try:
            content = _read_bounded_regular(path, MAX_EXPLORER_ARTIFACT_BYTES)
            record = ExplorerArtifactRecord.model_validate_json(content)
            if canonical_json_bytes(record) != content or record.result_id != result_id:
                raise ValueError("explorer artifact is not canonical or misnamed")
            binding_content = _read_bounded_regular(
                bindings_dir / path.name, MAX_EXPLORER_ARTIFACT_BYTES
            )
            binding = CatalogAuthorityBinding.model_validate_json(binding_content)
            if (
                canonical_json_bytes(binding) != binding_content
                or binding.result_id != result_id
            ):
                raise ValueError("authority binding is not canonical or misnamed")
            CanonicalExplorerArtifactRepository((record,))
            CatalogAuthorityIndex((binding,))
        except (OSError, ValueError, ValidationError):
            skipped += 1
            continue
        records.append(record)
        bindings.append(binding)
    if bindings_dir.is_dir() and not bindings_dir.is_symlink():
        # A binding without its artifact (a crash between the two writes) is
        # unpaired: counted, never served.
        artifact_names = {path.name for path in artifact_paths}
        skipped += sum(
            1 for path in bindings_dir.glob("*.json") if path.name not in artifact_names
        )
    return LoadedExplorerArtifacts(
        records=tuple(records), bindings=tuple(bindings), skipped=skipped
    )


@dataclass(frozen=True)
class LocalExplorer:
    source: IntegratedExplorerSource
    catalog: ResultCatalog
    skipped: int


@contextmanager
def open_local_explorer(root: Path) -> Iterator[LocalExplorer | None]:
    """Library-level explorer over ROOT's catalog and persisted artifacts.

    Yields ``None`` when ``ROOT/catalog`` does not exist.  Read-only: it never
    creates the trust registry or an authority store.  The catalog and trust
    registry are closed on exit.  (B6 serves this source over HTTP.)
    """

    from traceback_runner.web.explorer import (
        CanonicalExplorerArtifactRepository,
        CatalogAuthorityIndex,
        IntegratedExplorerSource,
    )

    if not (root / CATALOG_DIRECTORY).is_dir():
        yield None
        return
    trust = open_local_result_trust_registry(root, create=False)
    try:
        catalog = open_local_catalog(root, trust)
        try:
            loaded = load_explorer_artifacts(root)
            yield LocalExplorer(
                source=IntegratedExplorerSource(
                    catalog=catalog,
                    authority=CatalogAuthorityIndex(loaded.bindings),
                    artifacts=CanonicalExplorerArtifactRepository(loaded.records),
                ),
                catalog=catalog,
                skipped=loaded.skipped,
            )
        finally:
            catalog.close()
    finally:
        trust.close()


# ---------------------------------------------------------------------------
# The import itself (the caller holds the operator lock)
# ---------------------------------------------------------------------------


def import_local_record(root: Path, bundle: Path) -> CatalogImportOutcome:
    """Catalog one local record and persist its explorer artifact (idempotent)."""

    from evidence_inspector.result_catalog import CatalogError

    from .bundles import BundleError
    from .signing import SigningError

    bundle = bundle.absolute()
    record_id, reference_id = _peek_local_record(bundle)
    registered = load_reference(root, reference_id).registered
    authority = ensure_local_method_authority(root, registered)
    trust = sync_local_result_trust(root)
    try:
        catalog = open_local_catalog(root, trust, import_root=bundle.parent)
        try:
            try:
                reference = catalog.import_bundle(
                    root_id=_IMPORT_ROOT_ID,
                    relative_path=bundle.name,
                    registry=authority.registry,
                    authority_head=authority.authority_head,
                    expected_authority_head_sha256=authority.authority_head_sha256,
                    capability=authority.capability,
                    aliases=local_catalog_aliases(record_id),
                )
                verified, _ = catalog.verify_reference(reference)
            except (BundleError, SigningError) as exc:
                raise _not_a_record(
                    f"bundle verification failed ({type(exc).__name__}); a record must be "
                    "signed by a development-local key in ROOT's trust"
                ) from None
            except CatalogError as exc:
                raise _not_a_record(f"the catalog refused the bundle ({exc})") from None
            artifact, binding = build_local_explorer_artifact(reference, verified, authority)
            artifact_state, binding_state = persist_explorer_artifact(root, artifact, binding)
        finally:
            catalog.close()
    finally:
        trust.close()
    return CatalogImportOutcome(
        reference=reference,
        record_id=record_id,
        explorer_artifact=artifact_state,
        authority_binding=binding_state,
    )


__all__ = [
    "ACCESSIBLE_LABEL",
    "CatalogImportOutcome",
    "CatalogImportProblem",
    "LoadedExplorerArtifacts",
    "LocalAuthorityProblem",
    "LocalExplorer",
    "QC_LABEL",
    "build_local_explorer_artifact",
    "explorer_paths",
    "import_local_record",
    "load_explorer_artifacts",
    "open_local_catalog",
    "open_local_explorer",
    "open_local_method_authority",
    "persist_explorer_artifact",
]
