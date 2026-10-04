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
from .references import ReferenceProblem, load_reference, validate_reference_id
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
    # O_NONBLOCK: a FIFO at a store file name is rejected by the fstat check
    # below instead of blocking the open (and serve or run with it).
    descriptor = os.open(
        path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    )
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


def validate_local_method_authorities(root: Path) -> tuple[str, ...]:
    """Reopen and validate every reference's authority store under ROOT; never writes.

    ``traceback serve`` (B6) calls this before it starts a listener: a catalog
    whose authority is missing or fails validation is refused (exit 3) rather
    than served in a partial state.  Leftover staging directories are ignored,
    not removed.  Returns the validated reference IDs.
    """

    directory = root / AUTHORITY_DIRECTORY
    try:
        metadata = os.stat(directory, follow_symlinks=False)
    except FileNotFoundError:
        raise _authority_problem("no local method authority under ROOT/authority") from None
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o700
        or metadata.st_uid != os.geteuid()
    ):
        raise _authority_problem("ROOT/authority is not a private directory")
    reference_ids = sorted(
        entry.name for entry in directory.iterdir() if not entry.name.startswith(_STAGING_PREFIX)
    )
    if not reference_ids:
        raise _authority_problem("no local method authority under ROOT/authority")
    for reference_id in reference_ids:
        try:
            validate_reference_id(reference_id)
        except ValueError:
            raise _authority_problem("ROOT/authority holds an unexpected entry") from None
        open_local_method_authority(root, load_reference(root, reference_id).registered)
    return tuple(reference_ids)


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
    try:
        _write_pin_unchecked(root, registry)
    except OSError:
        # The next import advances a pin left behind along the journal.
        raise _trust_problem("the registry's identity pin could not be written") from None


def _write_pin_unchecked(root: Path, registry: ResultTrustRegistry) -> None:
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


# E04 bounds a trust journal at 256 KiB; recovery never reads more than that.
_MAX_FORWARD_JOURNAL_BYTES = 256 * 1024


def _forward_head(directory: Path, pinned_head: str) -> str | None:
    """Return the journal tail when the pinned head is an earlier point of it.

    Only used when the registry refused the pinned head: a crash between a key
    event and the pin update leaves the pin one or more events behind.  The
    registry constructor still validates the whole chain against the head
    returned here; this only proposes a forward-only candidate.
    """

    from evidence_inspector.result_trust_registry import ResultTrustJournalEntryV2

    try:
        journal = directory / "registry-journal.jsonl"
        if journal.is_symlink() or journal.stat().st_size > _MAX_FORWARD_JOURNAL_BYTES:
            return None
        lines = journal.read_bytes().splitlines()
        entries = [ResultTrustJournalEntryV2.model_validate_json(line) for line in lines]
    except (OSError, ValueError, ValidationError):
        return None
    if not entries:
        return None
    chain = [entries[0].previous_entry_sha256, *(item.entry_sha256 for item in entries)]
    if pinned_head not in chain[:-1]:
        return None
    return chain[-1]


def _is_empty_registry(directory: Path) -> bool:
    """A real registry directory whose journal holds no event at all."""

    if directory.is_symlink() or not directory.is_dir():
        return False
    journal = directory / "registry-journal.jsonl"
    try:
        metadata = os.stat(journal, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return stat.S_ISREG(metadata.st_mode) and metadata.st_size == 0


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
        except (ResultTrustRegistryError, OSError):
            raise _trust_problem("the result-trust registry could not be created") from None
        try:
            _write_pin(root, registry)
        except BaseException:
            registry.close()
            raise
        return registry
    if create and not (pin_path.exists() or pin_path.is_symlink()) and _is_empty_registry(
        directory
    ):
        # A crash between creating the registry and writing its pin leaves a
        # registry with no key events (keys are added only after the pin is
        # written).  It holds nothing and nothing binds its identity, so it is
        # replaced by a fresh, pinned registry.
        shutil.rmtree(directory)
        return open_local_result_trust_registry(root, create=True)
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
    except (ResultTrustRegistryError, OSError):
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
    except (ResultTrustRegistryError, OSError):
        raise _trust_problem(
            "the registry does not open at its pinned identity and head"
        ) from None
    try:
        _write_pin(root, registry)
    except BaseException:
        registry.close()
        raise
    return registry


def sync_local_result_trust(
    root: Path, *, key_ids: frozenset[str] | None = None
) -> ResultTrustRegistry:
    """Open (or create) the pinned registry and mirror ROOT's local result keys.

    Every ``development-local`` result key in
    ``ROOT/trust/development-result-trust.json`` (the keys ``run`` signed with)
    is added; a key revoked there is revoked here.  Keys are never removed.
    ``key_ids`` limits mirroring to those keys (``catalog import`` passes the
    one key that signed the bundle, so the registry holds only keys of
    imported records).  The caller holds the operator lock and closes the
    returned registry.
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
                    or (key_ids is not None and key.key_id not in key_ids)
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
                except (ResultTrustRegistryError, OSError):
                    raise _trust_problem(
                        "a development-local key conflicts with the registry"
                    ) from None
                _write_pin(root, registry)
    except BaseException:
        registry.close()
        raise
    return registry


# ---------------------------------------------------------------------------
# Method-parameterised authority stores (ROOT/method-authority, signal SH1)
# ---------------------------------------------------------------------------
#
# Layout, one append-only directory per method definition::
#
#     ROOT/<tree>/<reference_id>/<method_slug>/<method_definition_sha256>/
#         method-registry.json   canonical MethodRegistry (one definition)
#         authority-head.json    canonical AuthorityHead
#         pins.json              MethodAuthorityPins (location + both digests)
#
# ``<tree>`` is ``method-authority`` for the signal methods; the research
# authority (usability B2b) reuses the same code with its own tree name.  A
# store is validated only against the definition and publication time stored
# in it, never against current machine state: a tool reinstall or an asset
# re-registration yields a new definition hash and so a new directory, and the
# older stores (and the records bound to them) stay valid.  ``ROOT/authority``
# is never read or written by this section.

METHOD_AUTHORITY_DIRECTORY = "method-authority"
METHOD_SLUG_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_MAX_METHOD_SLUG_LENGTH = 64
_SHA256_NAME = re.compile(r"^[0-9a-f]{64}$")
_TREE_NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
METHOD_AUTHORITY_DAMAGED_CODE = "TBX-AUTH-LOCAL-003"


def _method_store_problem(cause: str) -> LocalAuthorityProblem:
    return LocalAuthorityProblem(
        METHOD_AUTHORITY_DAMAGED_CODE,
        "A method authority store under ROOT failed validation; nothing was changed",
        cause=cause,
        fix=(
            "Records bound to this store stay hidden; other records are unaffected. "
            "Restore the named ROOT/method-authority directory from a backup"
        ),
    )


def validate_method_slug(value: str) -> str:
    """Return ``value`` when it is a lowercase, hyphen-separated method slug."""

    if len(value) > _MAX_METHOD_SLUG_LENGTH or not METHOD_SLUG_PATTERN.fullmatch(value):
        raise ValueError(
            "method slug must match ^[a-z0-9]+(-[a-z0-9]+)*$ and be at most 64 characters"
        )
    return value


def _validate_tree(tree: str) -> str:
    if tree == AUTHORITY_DIRECTORY or not _TREE_NAME.fullmatch(tree):
        raise ValueError("authority tree must be a single lowercase name other than 'authority'")
    return tree


class MethodAuthorityPins(RunnerContract):
    """Where one method store lives and the digests of its two authority files."""

    schema_version: Literal["traceback.method-authority-pins.v1"] = (
        "traceback.method-authority-pins.v1"
    )
    reference_id: str
    method_slug: str
    method_definition_sha256: Sha256
    registry_sha256: Sha256
    authority_head_sha256: Sha256


@dataclass(frozen=True)
class MethodAuthority:
    """One validated method store: its location, registry, head and capability."""

    tree: str
    reference_id: str
    method_slug: str
    method_definition_sha256: str
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


@dataclass(frozen=True)
class DamagedMethodAuthority:
    """One entry under the tree that failed validation; its records stay hidden.

    ``location`` holds the path parts below the tree (reference ID, slug,
    definition hash) as far as they were read; it never holds a local path.
    """

    location: tuple[str, ...]
    problem: LocalAuthorityProblem


@dataclass(frozen=True)
class MethodAuthorityTree:
    """The result of validating one tree: valid stores and damaged entries."""

    tree: str
    stores: tuple[MethodAuthority, ...]
    damaged: tuple[DamagedMethodAuthority, ...]

    def find(
        self, reference_id: str, method_slug: str, method_definition_sha256: str
    ) -> MethodAuthority | None:
        """The valid store for one record's binding, or ``None`` (the record is hidden)."""

        for store in self.stores:
            if (
                store.reference_id == reference_id
                and store.method_slug == method_slug
                and store.method_definition_sha256 == method_definition_sha256
            ):
                return store
        return None


def method_authority_registry(
    definition: MethodDefinition, published_at: datetime
) -> MethodRegistry:
    """The unqualified local registry for one method definition.

    Tools and assets are registered exactly as the definition references them;
    the one qualification record is ``development_unqualified`` and the one
    display role is ``research_baseline``, as for the built-in store.
    """

    return MethodRegistry(
        registry_id=LOCAL_REGISTRY_ID,
        registry_version=1,
        authority_revision=2,
        published_at=published_at,
        previous_registry_sha256=None,
        tools=tuple(
            ToolRegistration(
                tool_id=tool.tool_id, version=tool.version, artifact_sha256=tool.artifact_sha256
            )
            for tool in definition.tools
        ),
        assets=tuple(
            AssetRegistration(
                asset_id=asset.asset_id,
                version=asset.version,
                content_sha256=asset.content_sha256,
            )
            for asset in definition.assets
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


def _require_local_version(definition: MethodDefinition, reference_id: str) -> None:
    # The base is MAJOR.MINOR.PATCH, so the first "-local-" is the delimiter
    # and everything after it is the whole reference ID (never a suffix match).
    if definition.version.partition("-local-")[2] != reference_id:
        raise ValueError("the method version must name the registered reference")


def _private_directory(path: Path, label: str) -> Path:
    """Create (0700) or check one directory of the tree; never follows a symlink."""

    try:
        metadata = os.stat(path, follow_symlinks=False)
    except FileNotFoundError:
        os.mkdir(path, 0o700)
        metadata = os.stat(path, follow_symlinks=False)
    if not stat.S_ISDIR(metadata.st_mode):
        raise _method_store_problem(f"{label} is not a private directory")
    if metadata.st_uid != os.geteuid():
        raise _method_store_problem(f"{label} is owned by another user")
    if stat.S_IMODE(metadata.st_mode) != 0o700:
        os.chmod(path, 0o700)
    return path


def _is_private_directory(path: Path) -> bool:
    try:
        metadata = os.stat(path, follow_symlinks=False)
    except OSError:
        return False
    return (
        stat.S_ISDIR(metadata.st_mode)
        and stat.S_IMODE(metadata.st_mode) == 0o700
        and metadata.st_uid == os.geteuid()
    )


def _create_method_store(
    directory: Path,
    reference_id: str,
    method_slug: str,
    definition: MethodDefinition,
    now: datetime,
) -> None:
    published_at = now.astimezone(UTC).replace(microsecond=0)
    registry = method_authority_registry(definition, published_at)
    head = authority_head_for_registry(registry, issued_at=published_at)
    definition_sha256 = method_definition_sha256(definition)
    pins = MethodAuthorityPins(
        reference_id=reference_id,
        method_slug=method_slug,
        method_definition_sha256=definition_sha256,
        registry_sha256=registry_sha256(registry),
        authority_head_sha256=authority_head_sha256(head),
    )
    staging = directory / f"{_STAGING_PREFIX}{definition_sha256[:12]}-{os.getpid()}"
    os.mkdir(staging, 0o700)
    try:
        _write_new_private(staging / REGISTRY_FILE, canonical_contract_bytes(registry))
        _write_new_private(staging / HEAD_FILE, canonical_contract_bytes(head))
        _write_new_private(staging / PINS_FILE, canonical_json_bytes(pins))
        _fsync_directory(staging)
        parent_fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            rename_directory_exclusive_at(parent_fd, staging.name, definition_sha256)
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    except BaseException:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        raise


def open_method_authority(
    root: Path,
    reference_id: str,
    method_slug: str,
    method_definition_sha256_hex: str,
    *,
    tree: str = METHOD_AUTHORITY_DIRECTORY,
) -> MethodAuthority:
    """Reopen and validate one method store; never writes.

    The store is checked against what it holds: the exact file set and modes,
    both pinned SHA-256 digests, the pinned location against the directory
    path, the stored definition against the directory name, and the registry
    and head against the ones rebuilt from the stored definition at the stored
    second.  The current capability must replay as development_unqualified.
    Any mismatch raises TBX-AUTH-LOCAL-003.
    """

    _validate_tree(tree)
    validate_reference_id(reference_id)
    validate_method_slug(method_slug)
    if not _SHA256_NAME.fullmatch(method_definition_sha256_hex):
        raise ValueError("method definition SHA-256 must be 64 lowercase hex characters")
    directory = root / tree / reference_id / method_slug / method_definition_sha256_hex
    for parent in (root / tree, root / tree / reference_id, root / tree / reference_id / method_slug):
        if not _is_private_directory(parent):
            raise _method_store_problem("a method authority directory is not private")
    if not _is_private_directory(directory):
        if not (directory.exists() or directory.is_symlink()):
            raise _method_store_problem("no method authority store for this definition")
        raise _method_store_problem("the method authority store is not a private directory")
    names = {entry.name for entry in directory.iterdir()}
    if names != _STORE_FILES:
        raise _method_store_problem("the method authority store holds unexpected files")
    try:
        registry = contract_from_canonical_bytes(
            MethodRegistry, _read_private(directory / REGISTRY_FILE)
        )
        head = contract_from_canonical_bytes(AuthorityHead, _read_private(directory / HEAD_FILE))
        pins = canonical_model_from_bytes(MethodAuthorityPins, _read_private(directory / PINS_FILE))
    except (OSError, ValueError, ValidationError, RecursionError):
        # RecursionError: deeply nested JSON fails in the parser itself.
        raise _method_store_problem("an authority file is unreadable or not canonical") from None
    if (pins.reference_id, pins.method_slug, pins.method_definition_sha256) != (
        reference_id,
        method_slug,
        method_definition_sha256_hex,
    ):
        raise _method_store_problem("pins.json names a different store location")
    if registry_sha256(registry) != pins.registry_sha256:
        raise _method_store_problem("method-registry.json does not match its pinned SHA-256")
    head_sha256 = authority_head_sha256(head)
    if head_sha256 != pins.authority_head_sha256:
        raise _method_store_problem("authority-head.json does not match its pinned SHA-256")
    if len(registry.method_definitions) != 1:
        raise _method_store_problem("the store must hold exactly one method definition")
    definition = registry.method_definitions[0]
    if method_definition_sha256(definition) != method_definition_sha256_hex:
        raise _method_store_problem("the stored definition does not match the directory name")
    try:
        _require_local_version(definition, reference_id)
        expected_registry = method_authority_registry(definition, registry.published_at)
        expected_head = authority_head_for_registry(
            expected_registry, issued_at=registry.published_at
        )
    except (ValueError, ValidationError):
        raise _method_store_problem(
            "the stored definition or publication time is invalid for this store"
        ) from None
    if registry != expected_registry or head != expected_head:
        raise _method_store_problem(
            "the stored authority is not the local unqualified authority for its definition"
        )
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
        raise _method_store_problem("the current capability does not replay") from None
    if (
        capability.qualification_state != QualificationState.DEVELOPMENT_UNQUALIFIED
        or capability.display_role != DisplayRole.RESEARCH_BASELINE
        or capability.current_provider_eligible
    ):
        raise _method_store_problem("the method capability is not development_unqualified")
    return MethodAuthority(
        tree=tree,
        reference_id=reference_id,
        method_slug=method_slug,
        method_definition_sha256=method_definition_sha256_hex,
        definition=definition,
        registry=registry,
        authority_head=head,
        authority_head_sha256=head_sha256,
        capability=capability,
    )


def ensure_method_authority(
    root: Path,
    reference_id: str,
    method_slug: str,
    definition: MethodDefinition,
    *,
    tree: str = METHOD_AUTHORITY_DIRECTORY,
    now: datetime | None = None,
) -> MethodAuthority:
    """Create (once) or reopen and validate the store for one method definition.

    The caller holds the operator lock.  The store is append-only: a new
    definition hash adds a sibling directory and never touches an existing
    one.  An existing store is reopened and validated; any mismatch raises
    TBX-AUTH-LOCAL-003 with nothing changed.  ``ROOT/authority`` is untouched.
    """

    _validate_tree(tree)
    validate_reference_id(reference_id)
    validate_method_slug(method_slug)
    _require_local_version(definition, reference_id)
    tree_directory = _private_directory(root / tree, f"ROOT/{tree}")
    reference_directory = _private_directory(tree_directory / reference_id, "a reference directory")
    directory = _private_directory(reference_directory / method_slug, "a method directory")
    _remove_leftover_staging(directory)
    definition_sha256 = method_definition_sha256(definition)
    target = directory / definition_sha256
    if not (target.exists() or target.is_symlink()):
        _create_method_store(
            directory, reference_id, method_slug, definition, now or datetime.now(UTC)
        )
    return open_method_authority(root, reference_id, method_slug, definition_sha256, tree=tree)


def _visible_entries(directory: Path) -> list[Path]:
    return sorted(
        (entry for entry in directory.iterdir() if not entry.name.startswith(_STAGING_PREFIX)),
        key=lambda entry: entry.name,
    )


def validate_method_authority_tree(
    root: Path, *, tree: str = METHOD_AUTHORITY_DIRECTORY
) -> MethodAuthorityTree:
    """Reopen and validate every store under ``ROOT/<tree>``; never writes.

    The sibling of :func:`validate_local_method_authorities`.  A missing tree is
    valid and empty (nothing is created).  A tree root that is not a private
    directory raises TBX-AUTH-LOCAL-003.  Every other failure is local: the
    damaged entry is listed in ``damaged`` and only the records bound to it are
    hidden; every other store stays valid.  Leftover staging directories are
    ignored, not removed.
    """

    _validate_tree(tree)
    directory = root / tree
    if not (directory.exists() or directory.is_symlink()):
        return MethodAuthorityTree(tree=tree, stores=(), damaged=())
    if not _is_private_directory(directory):
        raise _method_store_problem(f"ROOT/{tree} is not a private directory")
    stores: list[MethodAuthority] = []
    damaged: list[DamagedMethodAuthority] = []

    def fail(location: tuple[str, ...], cause: str) -> None:
        damaged.append(DamagedMethodAuthority(location, _method_store_problem(cause)))

    for reference_entry in _visible_entries(directory):
        reference_id = reference_entry.name
        try:
            validate_reference_id(reference_id)
        except ValueError:
            fail((), "the tree holds an entry that is not a reference ID")
            continue
        if not _is_private_directory(reference_entry):
            fail((reference_id,), "a reference directory is not private")
            continue
        for slug_entry in _visible_entries(reference_entry):
            method_slug = slug_entry.name
            try:
                validate_method_slug(method_slug)
            except ValueError:
                fail((reference_id,), "a reference directory holds an entry that is not a slug")
                continue
            if not _is_private_directory(slug_entry):
                fail((reference_id, method_slug), "a method directory is not private")
                continue
            for store_entry in _visible_entries(slug_entry):
                name = store_entry.name
                if not _SHA256_NAME.fullmatch(name):
                    fail(
                        (reference_id, method_slug),
                        "a method directory holds an entry that is not a definition hash",
                    )
                    continue
                try:
                    stores.append(
                        open_method_authority(root, reference_id, method_slug, name, tree=tree)
                    )
                except LocalAuthorityProblem as problem:
                    damaged.append(
                        DamagedMethodAuthority((reference_id, method_slug, name), problem)
                    )
    return MethodAuthorityTree(tree=tree, stores=tuple(stores), damaged=tuple(damaged))


def validate_all_method_authorities(
    root: Path,
) -> tuple[tuple[str, ...], MethodAuthorityTree]:
    """Validate ``ROOT/authority`` and ``ROOT/method-authority``; never writes.

    ``serve`` calls this before it starts a listener.  The built-in store is
    validated exactly as :func:`validate_local_method_authorities` does, except
    that a missing ``ROOT/authority`` is not an error when the method tree
    holds at least one valid store (a ROOT with only new-method records).
    Returns the validated reference IDs of the built-in store (empty when it
    is absent) and the validated method tree.
    """

    methods = validate_method_authority_tree(root)
    builtin = root / AUTHORITY_DIRECTORY
    if not (builtin.exists() or builtin.is_symlink()) and methods.stores:
        return (), methods
    return validate_local_method_authorities(root), methods


__all__ = [
    "AUTHORITY_DIRECTORY",
    "LOCAL_AUTHORITY_SCOPE",
    "LOCAL_METHOD_ID",
    "LOCAL_MIN_MAPPING_QUALITY",
    "LOCAL_POLICY_ID",
    "METHOD_AUTHORITY_DIRECTORY",
    "DamagedMethodAuthority",
    "LocalAuthorityPins",
    "LocalAuthorityProblem",
    "LocalMethodAuthority",
    "LocalTrustRegistryPin",
    "MethodAuthority",
    "MethodAuthorityPins",
    "MethodAuthorityTree",
    "ensure_local_method_authority",
    "ensure_method_authority",
    "method_authority_registry",
    "open_method_authority",
    "validate_all_method_authorities",
    "validate_method_authority_tree",
    "validate_method_slug",
    "local_catalog_aliases",
    "local_fragment_policy",
    "local_method_definition",
    "local_method_identity",
    "local_method_version",
    "open_local_method_authority",
    "open_local_result_trust_registry",
    "sync_local_result_trust",
    "validate_local_method_authorities",
]
