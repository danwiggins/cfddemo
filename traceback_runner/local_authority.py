"""Local, unqualified method identity and authority for ``traceback run`` records.

The locked local measurement policy, the method identity a local record is
signed with, and the durable local method-authority store under
``ROOT/authority`` (golden-path B5a) that ``traceback catalog import`` reads.

Nothing in this module qualifies a method.  Every identity and authority record
it writes is for an unqualified, local development record that is not for
clinical use: the one qualification record says ``development_unqualified`` and
the one display role is ``research_baseline``.

Store layout, one directory per registered reference (each is write-once)::

    ROOT/authority/<reference_id>/method-registry.json   canonical MethodRegistry
    ROOT/authority/<reference_id>/authority-head.json    canonical AuthorityHead
    ROOT/authority/<reference_id>/pins.json              both SHA-256 digests

The result-trust registry the catalog verifies against lives at
``ROOT/trust/result-trust-registry/``; its retained identity and head are pinned
in ``ROOT/trust/result-trust-registry.pin.json``.  Callers hold the operator
lock for every function here that writes.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from pydantic import ValidationError

from evidence_inspector.method_registry import (
    AssetReference,
    AssetRegistration,
    AuthorityHead,
    CurrentMethodCapability,
    DisplayRole,
    DisplayRoleAssignment,
    MethodDefinition,
    MethodFamily,
    MethodRegistry,
    QualificationRecord,
    QualificationState,
    ToolReference,
    ToolRegistration,
    authority_head_for_registry,
    authority_head_sha256,
    canonical_contract_bytes,
    contract_from_canonical_bytes,
    method_definition_sha256,
    registry_sha256,
    resolve_current_capability,
)

from .contracts import (
    ApprovalState,
    BundleMethodIdentity,
    FragmentMeasurementPolicyV2,
    HistogramBin,
    RegisteredReference,
    RunnerContract,
    Sha256,
)
from .filesystem import rename_directory_exclusive_at
from .references import ReferenceProblem, validate_reference_id
from .serialization import canonical_json_bytes, canonical_model_from_bytes

if TYPE_CHECKING:
    from evidence_inspector.result_catalog import (
        CatalogAliases,
        CatalogVerificationContext,
    )
    from evidence_inspector.result_trust_registry import ResultTrustRegistry

LOCAL_POLICY_ID = "aligned-reference-span-local-v2"
LOCAL_METHOD_ID = "mth_fragment_aligned_reference_span"
LOCAL_METHOD_BASE_VERSION = "1.0.0-local"
LOCAL_MIN_MAPPING_QUALITY = 20
LOCAL_BIN_EDGES = (0, 100, 150, 200, 300, 500, 1000)
_PRIMARY_CONTIG = re.compile(r"^chr([0-9]{1,2}|X|Y)$")

# Closed, constant identities of the local method definition.  None of them is
# operator text; only the method version names the registered reference.
LOCAL_QUANTITY_ID = "qty_fragment_aligned_reference_span"
LOCAL_UNIT = "unit_base_pairs"
LOCAL_TOOL_ID = "tool_traceback_aligned_span_scan"
LOCAL_TOOL_VERSION = "1.0.0"
# A tool identity token, not a binary digest: the scan is this package's own
# code, so the token names the locked policy it implements.  Changing the
# policy changes ``parameter_schema_sha256`` (and so the definition) anyway.
LOCAL_TOOL_SHA256 = hashlib.sha256(
    b"traceback.local-tool.v1\0" + LOCAL_POLICY_ID.encode("ascii")
).hexdigest()
LOCAL_ASSET_VERSION = "1.0.0"
LOCAL_REGISTRY_ID = "registry_traceback_local"
LOCAL_AUTHORITY_SCOPE = "scope_local"
LOCAL_QUALIFICATION_REF = "qual_local_development_unqualified"
LOCAL_QUALIFICATION_APPROVAL_REF = "approval_local_development_unqualified"
LOCAL_ROLE_REF = "role_local_research_baseline"

AUTHORITY_DIRECTORY = "authority"
REGISTRY_FILE = "method-registry.json"
HEAD_FILE = "authority-head.json"
PINS_FILE = "pins.json"
_STORE_FILES = frozenset({REGISTRY_FILE, HEAD_FILE, PINS_FILE})
_STAGING_PREFIX = ".staging-"
_MAX_STORE_FILE_BYTES = 1024 * 1024

TRUST_REGISTRY_RELATIVE = Path("trust/result-trust-registry")
TRUST_REGISTRY_PIN_RELATIVE = Path("trust/result-trust-registry.pin.json")
DEVELOPMENT_TRUST_RELATIVE = Path("trust/development-result-trust.json")

AUTH_DOCS_ANCHOR = "docs/OPERATOR-GUIDE.md#real-local-bam-unqualified"


class LocalAuthorityProblem(ReferenceProblem):
    """``TBX-AUTH-LOCAL-*``: the local authority under ROOT is not usable.

    Raised before anything under ROOT changes; exit 3, not retryable.
    """


def _authority_problem(cause: str) -> LocalAuthorityProblem:
    return LocalAuthorityProblem(
        "TBX-AUTH-LOCAL-001",
        "Local method authority under ROOT failed validation; nothing was changed",
        cause=cause,
        fix=(
            "Restore ROOT/authority from a backup, or remove ROOT/authority and "
            "ROOT/catalog together and import the records again"
        ),
    )


def _trust_problem(cause: str) -> LocalAuthorityProblem:
    return LocalAuthorityProblem(
        "TBX-AUTH-LOCAL-002",
        "Local result-trust registry under ROOT failed validation; nothing was changed",
        cause=cause,
        fix=(
            "The registry mirrors public keys from "
            "ROOT/trust/development-result-trust.json: remove "
            "ROOT/trust/result-trust-registry and its .pin.json, then import again"
        ),
    )


# ---------------------------------------------------------------------------
# Locked policy and method identity
# ---------------------------------------------------------------------------


def local_fragment_policy(reference: RegisteredReference) -> FragmentMeasurementPolicyV2:
    """Return the locked ``aligned-reference-span-local-v2`` policy for one reference.

    Contigs are the registered contigs named ``chr1``..``chr99``, ``chrX`` or
    ``chrY``; when none match (for example a tiny test reference) every
    registered contig is measured.  Operators cannot change the policy.
    """

    names = tuple(contig.name for contig in reference.contigs)
    primary = tuple(name for name in names if _PRIMARY_CONTIG.fullmatch(name))
    bins = tuple(
        HistogramBin(lower_inclusive=lower, upper_exclusive=upper)
        for lower, upper in zip(LOCAL_BIN_EDGES, (*LOCAL_BIN_EDGES[1:], None), strict=True)
    )
    return FragmentMeasurementPolicyV2(
        definition_id=f"{LOCAL_POLICY_ID}.{reference.reference_id}",
        approval_state=ApprovalState.UNAPPROVED_LOCAL,
        reference_id=reference.reference_id,
        contigs=primary or names,
        min_mapping_quality=LOCAL_MIN_MAPPING_QUALITY,
        bins=bins,
    )


def local_method_version(reference_id: str) -> str:
    """``1.0.0-local-<reference_id>``: one method version per registered reference."""

    return f"{LOCAL_METHOD_BASE_VERSION}-{validate_reference_id(reference_id)}"


def _local_asset_id(reference_id: str) -> str:
    # Hex of the reference ID, so an operator-chosen ID never becomes a
    # controlled identifier (and cannot carry a reserved privacy term).
    digest = hashlib.sha256(reference_id.encode("utf-8")).hexdigest()[:12]
    return f"asset_local_reference_{digest}"


def local_method_definition(reference: RegisteredReference) -> MethodDefinition:
    """Return the deterministic E01 method definition of the local method.

    ``parameter_schema_sha256`` is the SHA-256 of the canonical locked policy and
    the one asset is the registered reference's ``asset_sha256``, so a different
    policy or reference is a different method definition.
    """

    policy = local_fragment_policy(reference)
    return MethodDefinition(
        method_id=LOCAL_METHOD_ID,
        version=local_method_version(reference.reference_id),
        family=MethodFamily.FRAGMENT_MEASUREMENT,
        quantity_id=LOCAL_QUANTITY_ID,
        unit=LOCAL_UNIT,
        parameter_schema_sha256=hashlib.sha256(canonical_json_bytes(policy)).hexdigest(),
        tools=(
            ToolReference(
                tool_id=LOCAL_TOOL_ID,
                version=LOCAL_TOOL_VERSION,
                artifact_sha256=LOCAL_TOOL_SHA256,
            ),
        ),
        assets=(
            AssetReference(
                asset_id=_local_asset_id(reference.reference_id),
                version=LOCAL_ASSET_VERSION,
                content_sha256=reference.asset_sha256,
            ),
        ),
    )


def local_method_identity(reference: RegisteredReference) -> BundleMethodIdentity:
    """Return the method identity bound into a local record's signed bundle.

    It is exactly the E01 identity of :func:`local_method_definition`, so the
    catalog can bind the signed record to the local method authority.  The
    version names the reference, so records made against different registered
    references never share a method version.
    """

    definition = local_method_definition(reference)
    return BundleMethodIdentity(
        method_id=definition.method_id,
        version=definition.version,
        method_definition_sha256=method_definition_sha256(definition),
    )


# ---------------------------------------------------------------------------
# Durable local method-authority store (ROOT/authority/<reference_id>)
# ---------------------------------------------------------------------------


class LocalAuthorityPins(RunnerContract):
    schema_version: Literal["traceback.local-authority-pins.v1"] = (
        "traceback.local-authority-pins.v1"
    )
    registry_sha256: Sha256
    authority_head_sha256: Sha256


@dataclass(frozen=True)
class LocalMethodAuthority:
    """One validated local authority: the registry, its head and the capability."""

    reference_id: str
    definition: MethodDefinition
    registry: MethodRegistry
    authority_head: AuthorityHead
    authority_head_sha256: str
    capability: CurrentMethodCapability

    def verification_context(self) -> CatalogVerificationContext:
        from evidence_inspector.result_catalog import CatalogVerificationContext

        return CatalogVerificationContext(
            registry=self.registry,
            authority_head=self.authority_head,
            expected_authority_head_sha256=self.authority_head_sha256,
            capability=self.capability,
        )


def _local_registry(reference: RegisteredReference, published_at: datetime) -> MethodRegistry:
    definition = local_method_definition(reference)
    asset = definition.assets[0]
    return MethodRegistry(
        registry_id=LOCAL_REGISTRY_ID,
        registry_version=1,
        authority_revision=2,
        published_at=published_at,
        previous_registry_sha256=None,
        tools=(
            ToolRegistration(
                tool_id=LOCAL_TOOL_ID,
                version=LOCAL_TOOL_VERSION,
                artifact_sha256=LOCAL_TOOL_SHA256,
            ),
        ),
        assets=(
            AssetRegistration(
                asset_id=asset.asset_id,
                version=asset.version,
                content_sha256=asset.content_sha256,
            ),
        ),
        method_definitions=(definition,),
        qualification_records=(
            QualificationRecord(
                record_ref=LOCAL_QUALIFICATION_REF,
                method_ref=definition.method_ref,
                state=QualificationState.DEVELOPMENT_UNQUALIFIED,
                effective_at=published_at,
                approval_ref=LOCAL_QUALIFICATION_APPROVAL_REF,
            ),
        ),
        display_role_assignments=(
            DisplayRoleAssignment(
                assignment_ref=LOCAL_ROLE_REF,
                method_ref=definition.method_ref,
                display_role=DisplayRole.RESEARCH_BASELINE,
                authority_scope=LOCAL_AUTHORITY_SCOPE,
                effective_at=published_at,
            ),
        ),
    )


def _authority_directory(root: Path) -> Path:
    directory = root / AUTHORITY_DIRECTORY
    if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
        raise _authority_problem("ROOT/authority is not a private directory")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    metadata = os.stat(directory, follow_symlinks=False)
    if metadata.st_uid != os.geteuid():
        raise _authority_problem("ROOT/authority is owned by another user")
    if stat.S_IMODE(metadata.st_mode) != 0o700:
        os.chmod(directory, 0o700)
    return directory


def _remove_leftover_staging(directory: Path) -> None:
    for entry in directory.iterdir():
        if entry.name.startswith(_STAGING_PREFIX) and not entry.is_symlink():
            shutil.rmtree(entry)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_new_private(path: Path, content: bytes) -> None:
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def _read_private(path: Path) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_uid != os.geteuid()
            or metadata.st_size > _MAX_STORE_FILE_BYTES
        ):
            raise ValueError("authority file is not a private regular file")
        chunks = []
        while True:
            chunk = os.read(descriptor, 65536)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _create_store(directory: Path, reference: RegisteredReference, now: datetime) -> None:
    published_at = now.astimezone(UTC).replace(microsecond=0)
    registry = _local_registry(reference, published_at)
    head = authority_head_for_registry(registry, issued_at=published_at)
    pins = LocalAuthorityPins(
        registry_sha256=registry_sha256(registry),
        authority_head_sha256=authority_head_sha256(head),
    )
    staging = directory / f"{_STAGING_PREFIX}{reference.reference_id}-{os.getpid()}"
    os.mkdir(staging, 0o700)
    try:
        _write_new_private(staging / REGISTRY_FILE, canonical_contract_bytes(registry))
        _write_new_private(staging / HEAD_FILE, canonical_contract_bytes(head))
        _write_new_private(staging / PINS_FILE, canonical_json_bytes(pins))
        _fsync_directory(staging)
        parent_fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            rename_directory_exclusive_at(parent_fd, staging.name, reference.reference_id)
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    except BaseException:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        raise


def open_local_method_authority(
    root: Path, reference: RegisteredReference
) -> LocalMethodAuthority:
    """Reopen and validate one reference's authority store; never writes.

    Recomputes both SHA-256 digests against ``pins.json``, requires the stored
    registry to be exactly the local unqualified registry for ``reference`` and
    replays the current capability.  Any mismatch raises TBX-AUTH-LOCAL-001.
    """

    directory = root / AUTHORITY_DIRECTORY / validate_reference_id(reference.reference_id)
    try:
        metadata = os.stat(directory, follow_symlinks=False)
    except FileNotFoundError:
        raise _authority_problem(
            f"no local method authority for reference {reference.reference_id!r}"
        ) from None
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o700
        or metadata.st_uid != os.geteuid()
    ):
        raise _authority_problem("the authority directory is not a private directory")
    names = {entry.name for entry in directory.iterdir()}
    if names != _STORE_FILES:
        raise _authority_problem("the authority directory holds unexpected files")
    try:
        registry = contract_from_canonical_bytes(
            MethodRegistry, _read_private(directory / REGISTRY_FILE)
        )
        head = contract_from_canonical_bytes(AuthorityHead, _read_private(directory / HEAD_FILE))
        pins = canonical_model_from_bytes(LocalAuthorityPins, _read_private(directory / PINS_FILE))
    except (OSError, ValueError, ValidationError):
        raise _authority_problem("an authority file is unreadable or not canonical") from None
    if registry_sha256(registry) != pins.registry_sha256:
        raise _authority_problem("method-registry.json does not match its pinned SHA-256")
    head_sha256 = authority_head_sha256(head)
    if head_sha256 != pins.authority_head_sha256:
        raise _authority_problem("authority-head.json does not match its pinned SHA-256")
    try:
        expected_registry = _local_registry(reference, registry.published_at)
        expected_head = authority_head_for_registry(
            expected_registry, issued_at=registry.published_at
        )
    except (ValueError, ValidationError):
        raise _authority_problem("the stored publication time is invalid") from None
    if registry != expected_registry or head != expected_head:
        raise _authority_problem(
            "the stored authority is not the local unqualified authority for this reference"
        )
    definition = registry.method_definitions[0]
    try:
        capability = resolve_current_capability(
            registry,
            head,
            head_sha256,
            definition.method_ref,
            authority_scope=LOCAL_AUTHORITY_SCOPE,
            as_of=head.issued_at,
        )
    except (ValueError, ValidationError):
        raise _authority_problem("the current capability does not replay") from None
    if (
        capability.qualification_state != QualificationState.DEVELOPMENT_UNQUALIFIED
        or capability.display_role != DisplayRole.RESEARCH_BASELINE
        or capability.current_provider_eligible
    ):
        raise _authority_problem("the local capability is not development_unqualified")
    return LocalMethodAuthority(
        reference_id=reference.reference_id,
        definition=definition,
        registry=registry,
        authority_head=head,
        authority_head_sha256=head_sha256,
        capability=capability,
    )


def ensure_local_method_authority(
    root: Path,
    reference: RegisteredReference,
    *,
    now: datetime | None = None,
) -> LocalMethodAuthority:
    """Create (once) or reopen and validate one reference's authority store.

    The caller holds the operator lock.  Creation builds the three files in a
    staging directory, fsyncs them and publishes with one no-replace rename;
    leftover staging directories are removed first.  An existing store is
    never rewritten: it is reopened and validated, and any mismatch raises
    TBX-AUTH-LOCAL-001 with nothing changed.
    """

    directory = _authority_directory(root)
    _remove_leftover_staging(directory)
    target = directory / validate_reference_id(reference.reference_id)
    if not (target.exists() or target.is_symlink()):
        _create_store(directory, reference, now or datetime.now(UTC))
    return open_local_method_authority(root, reference)


# ---------------------------------------------------------------------------
# Catalog aliases
# ---------------------------------------------------------------------------


def local_catalog_aliases(record_id: str) -> CatalogAliases:
    """Deterministic opaque aliases from the first 8 hex of ``sha256(record_id)``."""

    from evidence_inspector.result_catalog import CatalogAliases

    digest = hashlib.sha256(record_id.encode("utf-8")).hexdigest()[:8]
    return CatalogAliases(
        display_alias=f"dsp_{digest}",
        run_alias=f"rnx_{digest}",
        timepoint_alias=f"tpt_{digest}",
    )


# ---------------------------------------------------------------------------
# Result-trust registry (ROOT/trust/result-trust-registry), pinned identity
# ---------------------------------------------------------------------------


class LocalTrustRegistryPin(RunnerContract):
    schema_version: Literal["traceback.local-trust-registry-pin.v1"] = (
        "traceback.local-trust-registry-pin.v1"
    )
    registry_id: str
    registry_epoch_sha256: Sha256
    state_head_sha256: Sha256


def _write_pin(root: Path, registry: ResultTrustRegistry) -> None:
    snapshot = registry.current_trust()
    pin = LocalTrustRegistryPin(
        registry_id=snapshot.registry_id,
        registry_epoch_sha256=snapshot.registry_epoch_sha256,
        state_head_sha256=snapshot.state_head_sha256,
    )
    path = root / TRUST_REGISTRY_PIN_RELATIVE
    content = canonical_json_bytes(pin)
    if path.is_file() and not path.is_symlink() and path.read_bytes() == content:
        return
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.unlink(missing_ok=True)
    _write_new_private(temporary, content)
    try:
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _forward_head(directory: Path, pinned_head: str) -> str | None:
    """Return the journal tail when the pinned head is an earlier point of it.

    Only used when the registry refused the pinned head: a crash between a key
    event and the pin update leaves the pin one or more events behind.  The
    registry constructor still validates the whole chain against the head
    returned here; this only proposes a forward-only candidate.
    """

    from evidence_inspector.result_trust_registry import ResultTrustJournalEntryV2

    try:
        lines = (directory / "registry-journal.jsonl").read_bytes().splitlines()
        entries = [ResultTrustJournalEntryV2.model_validate_json(line) for line in lines]
    except (OSError, ValueError, ValidationError):
        return None
    if not entries:
        return None
    chain = [entries[0].previous_entry_sha256, *(item.entry_sha256 for item in entries)]
    if pinned_head not in chain[:-1]:
        return None
    return chain[-1]


def open_local_result_trust_registry(
    root: Path, *, create: bool = False
) -> ResultTrustRegistry:
    """Open ``ROOT/trust/result-trust-registry`` at its pinned identity and head.

    With ``create`` (operator lock held) a missing registry is created as a v2
    registry, which accepts ``development-local`` keys, and pinned.  A registry
    whose pin is missing or does not open raises TBX-AUTH-LOCAL-002.
    """

    from evidence_inspector.result_trust_registry import (
        ResultTrustRegistry,
        ResultTrustRegistryError,
    )

    directory = root / TRUST_REGISTRY_RELATIVE
    pin_path = root / TRUST_REGISTRY_PIN_RELATIVE
    if not (directory.exists() or directory.is_symlink()):
        if not create:
            raise _trust_problem("no result-trust registry under ROOT/trust")
        if pin_path.exists() or pin_path.is_symlink():
            raise _trust_problem("a registry pin exists without its registry")
        directory.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            registry = ResultTrustRegistry(directory, create_version=2)
        except ResultTrustRegistryError:
            raise _trust_problem("the result-trust registry could not be created") from None
        try:
            _write_pin(root, registry)
        except BaseException:
            registry.close()
            raise
        return registry
    try:
        pin = canonical_model_from_bytes(LocalTrustRegistryPin, _read_private(pin_path))
    except FileNotFoundError:
        raise _trust_problem("the registry's identity pin is missing") from None
    except (OSError, ValueError, ValidationError):
        raise _trust_problem("the registry's identity pin is unreadable") from None
    try:
        return ResultTrustRegistry(
            directory,
            expected_registry_id=pin.registry_id,
            expected_registry_epoch_sha256=pin.registry_epoch_sha256,
            expected_state_head_sha256=pin.state_head_sha256,
        )
    except ResultTrustRegistryError:
        candidate = _forward_head(directory, pin.state_head_sha256)
        if candidate is None or not create:
            raise _trust_problem(
                "the registry does not open at its pinned identity and head"
            ) from None
    try:
        registry = ResultTrustRegistry(
            directory,
            expected_registry_id=pin.registry_id,
            expected_registry_epoch_sha256=pin.registry_epoch_sha256,
            expected_state_head_sha256=candidate,
        )
    except ResultTrustRegistryError:
        raise _trust_problem(
            "the registry does not open at its pinned identity and head"
        ) from None
    try:
        _write_pin(root, registry)
    except BaseException:
        registry.close()
        raise
    return registry


def sync_local_result_trust(root: Path) -> ResultTrustRegistry:
    """Open (or create) the pinned registry and mirror ROOT's local result keys.

    Every ``development-local`` result key in
    ``ROOT/trust/development-result-trust.json`` (the keys ``run`` signed with)
    is added; a key revoked there is revoked here.  Keys are never removed.  The
    caller holds the operator lock and closes the returned registry.
    """

    from evidence_inspector.result_trust_registry import ResultTrustRegistryError

    from .signing import (
        KeyPurpose,
        PublicTrustedKeyV2,
        TrustNamespace,
        parse_development_trust_document,
    )

    trust_path = root / DEVELOPMENT_TRUST_RELATIVE
    document = None
    if trust_path.is_file() and not trust_path.is_symlink():
        try:
            document = parse_development_trust_document(trust_path.read_bytes())
        except (OSError, ValueError, ValidationError):
            raise _trust_problem(
                "ROOT/trust/development-result-trust.json is unreadable"
            ) from None
    registry = open_local_result_trust_registry(root, create=True)
    try:
        if document is not None:
            current = {key.key_id: key for key in registry.current_trust().document.keys}
            for key in document.keys:
                if (
                    getattr(key, "namespace", None) != TrustNamespace.DEVELOPMENT_LOCAL
                    or key.purpose != KeyPurpose.RESULT
                ):
                    continue
                known = current.get(key.key_id)
                try:
                    if known is None:
                        registry.add_key(
                            PublicTrustedKeyV2(
                                key_id=key.key_id,
                                namespace=key.namespace,
                                purpose=key.purpose,
                                public_key_base64=key.public_key_base64,
                            )
                        )
                    if key.revoked and not (known is not None and known.revoked):
                        registry.revoke_key(key.key_id)
                except ResultTrustRegistryError:
                    raise _trust_problem(
                        "a development-local key conflicts with the registry"
                    ) from None
                _write_pin(root, registry)
    except BaseException:
        registry.close()
        raise
    return registry


__all__ = [
    "AUTHORITY_DIRECTORY",
    "LOCAL_AUTHORITY_SCOPE",
    "LOCAL_METHOD_ID",
    "LOCAL_MIN_MAPPING_QUALITY",
    "LOCAL_POLICY_ID",
    "LocalAuthorityPins",
    "LocalAuthorityProblem",
    "LocalMethodAuthority",
    "LocalTrustRegistryPin",
    "ensure_local_method_authority",
    "local_catalog_aliases",
    "local_fragment_policy",
    "local_method_definition",
    "local_method_identity",
    "local_method_version",
    "open_local_method_authority",
    "open_local_result_trust_registry",
    "sync_local_result_trust",
]
