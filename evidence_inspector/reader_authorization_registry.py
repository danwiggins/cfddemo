"""Protected, durable registry of provider-signed ``longitudinal_reader`` grants.

E12 needs reader authorization that a browser session cannot mint for itself.
This registry stores immutable grants that an external provider authority
signed with Ed25519 (the provider-linkage signature pattern), append-only
revocations, and chained provider-trust revisions (key rotation).  Every grant
binds this registry's ID and epoch, an opaque grant selector, the provider
authority ID and key version, the exact ``longitudinal_reader`` role, the
allowed D05 cohort-registry and D02 measurement scopes, and its issue and
expiry times.  There is no wildcard role and no caller-selected scope.

Grant add, revocation, trust rotation, and every authorization check share one
cross-process fence: the registry lock file.  Mutations take it exclusively;
``authority_read_fence`` holds it shared, so no mutation from any process can
land while a reader authorization is being checked and returned.

Two profiles exist.  ``synthetic`` accepts only the checked-in synthetic
provider authority in :mod:`evidence_inspector.reader_authorization_synthetic`;
``provider`` rejects that authority and its keys outright.  One process may
open registries of one profile only.

Threat model: the process/OS-user boundary is the trust boundary.  In-process
code mutation and same-user filesystem races are out of scope; seals here
detect accidental or naive replacement only.  The synthetic authority is a
public fixture and never authorizes real data.
"""

from __future__ import annotations

import base64
import fcntl
import hashlib
import os
import secrets
import stat
import threading
import weakref
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Annotated, Literal

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from pydantic import Field, StringConstraints, model_validator

from evidence_inspector.method_registry import (
    MethodFamily,
    QuantityId,
    RegistryContract,
    UnitId,
    canonical_contract_bytes,
)
from evidence_inspector.provider_linkage import Base64PublicKey, Base64Signature
from evidence_inspector.provider_linkage_store import AuthorityTimeSource
from evidence_inspector.safe_ingress import (
    bounded_json_loads,
    contract_type_graph,
    exact_model_bytes,
)

MAX_GRANTS = 1_000
MAX_TRUST_REVISIONS = 64
MAX_RECORDS = 2 * MAX_GRANTS + MAX_TRUST_REVISIONS
MAX_KEYS = 16
MAX_SCOPES = 16
MAX_GRANT_LIFETIME = timedelta(days=90)
MAX_OBJECT_BYTES = 16 * 1024
MAX_TOTAL_OBJECT_BYTES = MAX_RECORDS * MAX_OBJECT_BYTES
MAX_JOURNAL_BYTES = 2 * 1024 * 1024
MAX_BACKUP_BYTES = 64 * 1024 * 1024
MAX_PATH_CHARS = 4096
MAX_PATH_PARTS = 256

# Public synthetic authority: the private seeds are checked in, so these keys
# authorize nothing outside the synthetic profile.  Tests assert that the
# synthetic module derives exactly these public keys.
SYNTHETIC_READER_AUTHORITY_ID = "reader_authority_756788bd0f2a6f9ff07382731101bda7"
SYNTHETIC_READER_PUBLIC_KEYS = MappingProxyType(
    {
        1: "I8/WuZhlI1sWjQfzMHInmJkOZDKMXJcuQgI3VYsQ7mo=",
        2: "1esCcbXYJ8/M4qiTQZHMXZ96Y++3W3VDX6BPNnwD25Y=",
    }
)

_GRANT_SIGNATURE_DOMAIN = b"traceback-reader-grant-v1\0"
_REGISTRY_PROCESS_LOCK = threading.RLock()
_REGISTRY_PROCESS_HEADS: dict[tuple[str, str], str] = {}
# Root identity -> (pid, thread) of the current shared-fence holder.
_FENCE_HOLDERS: dict[tuple[int, int], tuple[int, int]] = {}
_PROCESS_PROFILE: dict[str, ReaderAuthorizationProfile] = {}
_REGISTRY_INSTANCE_SEALS: weakref.WeakKeyDictionary[
    object, tuple[object, ...]
] = weakref.WeakKeyDictionary()
_PINNED_TIME_READ = AuthorityTimeSource.read
_PINNED_PUBLIC_KEY = Ed25519PublicKey.from_public_bytes

ReaderRegistryId = Annotated[
    str, StringConstraints(pattern=r"^reader_registry_[0-9a-f]{32}$")
]
ReaderAuthorityId = Annotated[
    str, StringConstraints(pattern=r"^reader_authority_[0-9a-f]{32}$")
]
ReaderGrantSelector = Annotated[
    str, StringConstraints(pattern=r"^reader_grant_[0-9a-f]{32}$")
]
CohortRegistryId = Annotated[
    str, StringConstraints(pattern=r"^cohort_registry_[0-9a-f]{32}$")
]
Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class ReaderAuthorizationRegistryError(RuntimeError):
    """Sanitized registry failure."""


class ReaderAuthorizationRegistryConflict(ReaderAuthorizationRegistryError):
    pass


class ReaderAuthorizationRegistryUnsafe(ReaderAuthorizationRegistryError):
    pass


class ReaderDenialReason(StrEnum):
    """Closed diagnostic reasons; never carries a reader or scope identifier."""

    AUTHORITY_ABSENT = "authority_absent"
    GRANT_MISSING = "grant_missing"
    GRANT_REVOKED = "grant_revoked"
    GRANT_NOT_CURRENT = "grant_not_current"
    SCOPE_MISMATCH = "scope_mismatch"
    UNTRUSTED_KEY = "untrusted_key"
    SIGNATURE_INVALID = "signature_invalid"
    STALE_HEAD = "stale_head"
    CLOCK_ROLLBACK = "clock_rollback"
    REGISTRY_UNAVAILABLE = "registry_unavailable"
    SESSION_UNBOUND = "session_unbound"
    SESSION_ALREADY_BOUND = "session_already_bound"
    LAUNCH_CREDENTIAL_INVALID = "launch_credential_invalid"


class ReaderAuthorizationDenied(PermissionError):
    """The one typed denial E12 routes surface; its text is constant."""

    code: Literal["permission_denied"] = "permission_denied"

    def __init__(self, reason: ReaderDenialReason) -> None:
        super().__init__("permission_denied")
        self.reason = reason


class ReaderAuthorizationProfile(StrEnum):
    SYNTHETIC = "synthetic"
    PROVIDER = "provider"


class ReaderRole(StrEnum):
    LONGITUDINAL_READER = "longitudinal_reader"


class ReaderKeyStatus(StrEnum):
    ACTIVE = "active"
    REVOKED = "revoked"


class ReaderRevocationReason(StrEnum):
    PROVIDER_REQUEST = "provider_request"
    OPERATOR_REQUEST = "operator_request"
    KEY_COMPROMISE = "key_compromise"


class ReaderRecordKind(StrEnum):
    TRUST = "trust"
    GRANT = "grant"
    REVOCATION = "revocation"


def _utc_second(value: datetime, label: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{label} must be an aware UTC timestamp")
    if value.microsecond:
        raise ValueError(f"{label} must use whole-second precision")
    return value


class ReaderContract(RegistryContract):
    """Closed immutable base for reader-authorization contracts."""


class ReaderAuthorityKey(ReaderContract):
    key_version: int = Field(ge=1, le=1024, strict=True)
    public_key_base64: Base64PublicKey
    status: ReaderKeyStatus


class ReaderProviderTrust(ReaderContract):
    """Independently configured public trust for one provider authority."""

    schema_version: Literal["traceback.reader-provider-trust.v1"] = (
        "traceback.reader-provider-trust.v1"
    )
    profile: ReaderAuthorizationProfile
    authority_id: ReaderAuthorityId
    revision: int = Field(ge=1, le=MAX_TRUST_REVISIONS, strict=True)
    previous_trust_sha256: Sha256 | None
    keys: tuple[ReaderAuthorityKey, ...] = Field(min_length=1, max_length=MAX_KEYS)

    @model_validator(mode="after")
    def coherent_trust(self) -> ReaderProviderTrust:
        if (self.revision == 1) != (self.previous_trust_sha256 is None):
            raise ValueError("only trust revision one may omit its predecessor")
        versions = [key.key_version for key in self.keys]
        if versions != sorted(set(versions)):
            raise ValueError("trust key versions must be uniquely sorted")
        public = [key.public_key_base64 for key in self.keys]
        if len(public) != len(set(public)):
            raise ValueError("a trust public key cannot appear twice")
        if not any(key.status is ReaderKeyStatus.ACTIVE for key in self.keys):
            raise ValueError("trust requires at least one active key")
        synthetic_keys = set(SYNTHETIC_READER_PUBLIC_KEYS.values())
        if self.profile is ReaderAuthorizationProfile.SYNTHETIC:
            if self.authority_id != SYNTHETIC_READER_AUTHORITY_ID or any(
                SYNTHETIC_READER_PUBLIC_KEYS.get(key.key_version)
                != key.public_key_base64
                for key in self.keys
            ):
                raise ValueError(
                    "synthetic trust may name only the checked-in synthetic authority"
                )
        elif self.authority_id == SYNTHETIC_READER_AUTHORITY_ID or any(
            key in synthetic_keys for key in public
        ):
            raise ValueError("provider trust cannot name the synthetic authority")
        return self


class MeasurementScope(ReaderContract):
    """One exact D02 measurement identity a grant may read."""

    family: MethodFamily
    quantity_id: QuantityId
    unit: UnitId

    def sort_key(self) -> tuple[str, str, str]:
        return (self.family.value, self.quantity_id, self.unit)


class ReaderGrantPayload(ReaderContract):
    schema_version: Literal["traceback.reader-grant.v1"] = "traceback.reader-grant.v1"
    profile: ReaderAuthorizationProfile
    registry_id: ReaderRegistryId
    registry_epoch_sha256: Sha256
    grant_selector: ReaderGrantSelector
    authority_id: ReaderAuthorityId
    key_version: int = Field(ge=1, le=1024, strict=True)
    role: Literal[ReaderRole.LONGITUDINAL_READER]
    cohort_registry_ids: tuple[CohortRegistryId, ...] = Field(
        min_length=1, max_length=MAX_SCOPES
    )
    measurement_scopes: tuple[MeasurementScope, ...] = Field(
        min_length=1, max_length=MAX_SCOPES
    )
    issued_at: datetime
    expires_at: datetime

    @model_validator(mode="after")
    def coherent_grant(self) -> ReaderGrantPayload:
        _utc_second(self.issued_at, "grant issued_at")
        _utc_second(self.expires_at, "grant expires_at")
        if not self.issued_at < self.expires_at <= self.issued_at + MAX_GRANT_LIFETIME:
            raise ValueError("grant validity window is invalid")
        if self.cohort_registry_ids != tuple(sorted(set(self.cohort_registry_ids))):
            raise ValueError("grant cohort scopes must be uniquely sorted")
        keys = [scope.sort_key() for scope in self.measurement_scopes]
        if keys != sorted(set(keys)):
            raise ValueError("grant measurement scopes must be uniquely sorted")
        return self


class SignedReaderGrant(ReaderContract):
    payload: ReaderGrantPayload
    signature_base64: Base64Signature


class ReaderGrantRevocation(ReaderContract):
    schema_version: Literal["traceback.reader-grant-revocation.v1"] = (
        "traceback.reader-grant-revocation.v1"
    )
    grant_selector: ReaderGrantSelector
    grant_sha256: Sha256
    reason: ReaderRevocationReason


class ReaderRegistryObject(ReaderContract):
    """One immutable journal-committed record."""

    schema_version: Literal["traceback.reader-registry-object.v1"] = (
        "traceback.reader-registry-object.v1"
    )
    kind: ReaderRecordKind
    recorded_at: datetime
    trust: ReaderProviderTrust | None
    grant: SignedReaderGrant | None
    revocation: ReaderGrantRevocation | None

    @model_validator(mode="after")
    def one_record(self) -> ReaderRegistryObject:
        _utc_second(self.recorded_at, "recorded_at")
        present = {
            ReaderRecordKind.TRUST: self.trust is not None,
            ReaderRecordKind.GRANT: self.grant is not None,
            ReaderRecordKind.REVOCATION: self.revocation is not None,
        }
        if [kind for kind, value in present.items() if value] != [self.kind]:
            raise ValueError("registry object must carry exactly its kind")
        return self


class ReaderRegistryMetadata(ReaderContract):
    schema_version: Literal["traceback.reader-registry-metadata.v1"] = (
        "traceback.reader-registry-metadata.v1"
    )
    registry_id: ReaderRegistryId
    registry_epoch_sha256: Sha256
    profile: ReaderAuthorizationProfile
    authority_id: ReaderAuthorityId
    genesis_trust_sha256: Sha256


class ReaderJournalEntry(ReaderContract):
    schema_version: Literal["traceback.reader-registry-journal-entry.v1"] = (
        "traceback.reader-registry-journal-entry.v1"
    )
    sequence: int = Field(ge=1, le=MAX_RECORDS, strict=True)
    previous_entry_sha256: Sha256
    object_sha256: Sha256
    object_bytes: int = Field(ge=1, le=MAX_OBJECT_BYTES, strict=True)
    entry_sha256: Sha256


class ReaderRegistryReceipt(ReaderContract):
    """Mutation receipt; the operator retains the head for the next startup."""

    schema_version: Literal["traceback.reader-registry-receipt.v1"] = (
        "traceback.reader-registry-receipt.v1"
    )
    registry_id: ReaderRegistryId
    registry_epoch_sha256: Sha256
    state_version: int = Field(ge=1, le=MAX_RECORDS)
    state_head_sha256: Sha256
    kind: ReaderRecordKind
    object_sha256: Sha256
    trust_sha256: Sha256
    grant_sha256: Sha256 | None


class ReaderRegistryIdentity(ReaderContract):
    schema_version: Literal["traceback.reader-registry-identity.v1"] = (
        "traceback.reader-registry-identity.v1"
    )
    registry_id: ReaderRegistryId
    registry_epoch_sha256: Sha256
    profile: ReaderAuthorizationProfile
    state_version: int = Field(ge=1, le=MAX_RECORDS)
    state_head_sha256: Sha256
    trust_sha256: Sha256


class ReaderGrantBinding(ReaderContract):
    """What a session may retain: the grant commitment and the registry head."""

    grant_sha256: Sha256
    state_head_sha256: Sha256


class ReaderAuthorization(ReaderContract):
    """Protected, as-of proof of one current grant; never crosses HTTP."""

    schema_version: Literal["traceback.reader-authorization.v1"] = (
        "traceback.reader-authorization.v1"
    )
    registry_id: ReaderRegistryId
    registry_epoch_sha256: Sha256
    state_version: int = Field(ge=1, le=MAX_RECORDS)
    state_head_sha256: Sha256
    trust_sha256: Sha256
    grant_sha256: Sha256
    role: Literal[ReaderRole.LONGITUDINAL_READER]
    cohort_registry_id: CohortRegistryId
    measurement_scope: MeasurementScope
    evaluated_at: datetime
    expires_at: datetime
    synthetic_only: bool

    @model_validator(mode="after")
    def coherent_window(self) -> ReaderAuthorization:
        _utc_second(self.evaluated_at, "evaluated_at")
        _utc_second(self.expires_at, "expires_at")
        if self.evaluated_at >= self.expires_at:
            raise ValueError("authorization must be current")
        return self


class ReaderRegistryBackupObject(ReaderContract):
    object_sha256: Sha256
    object_json: Annotated[str, StringConstraints(max_length=MAX_OBJECT_BYTES)]


class ReaderRegistryBackup(ReaderContract):
    schema_version: Literal["traceback.reader-registry-backup.v1"] = (
        "traceback.reader-registry-backup.v1"
    )
    metadata: ReaderRegistryMetadata
    state_version: int = Field(ge=1, le=MAX_RECORDS)
    state_head_sha256: Sha256
    journal: tuple[ReaderJournalEntry, ...] = Field(
        min_length=1, max_length=MAX_RECORDS
    )
    objects: tuple[ReaderRegistryBackupObject, ...] = Field(
        min_length=1, max_length=MAX_RECORDS
    )


_OBJECT_TYPES = contract_type_graph(ReaderRegistryObject)
_TRUST_TYPES = contract_type_graph(ReaderProviderTrust)
_GRANT_TYPES = contract_type_graph(SignedReaderGrant)
_SCOPE_TYPES = contract_type_graph(MeasurementScope)
_METADATA_TYPES = contract_type_graph(ReaderRegistryMetadata)
_BACKUP_TYPES = contract_type_graph(ReaderRegistryBackup)


def _exact_bytes(
    value: object,
    model: type[ReaderContract],
    types: tuple[frozenset, frozenset],
    max_bytes: int,
) -> bytes:
    return exact_model_bytes(
        value,
        model,
        model_types=types[0],
        enum_types=types[1],
        max_bytes=max_bytes,
        max_nodes=max_bytes,
        max_depth=16,
        max_collection_items=max(MAX_RECORDS, MAX_SCOPES),
        max_string_bytes=max_bytes,
    )


def _parse_exact(
    content: bytes,
    model: type[ReaderContract],
    types: tuple[frozenset, frozenset],
    max_bytes: int,
) -> ReaderContract:
    """Parse bounded canonical bytes and require an exact round trip."""

    try:
        bounded_json_loads(
            content,
            max_bytes=max_bytes,
            max_depth=16,
            max_nodes=max_bytes,
            max_collection_items=max(MAX_RECORDS, MAX_SCOPES),
            max_string_bytes=max_bytes,
        )
        value = model.model_validate_json(content)
        if _exact_bytes(value, model, types, max_bytes) != content:
            raise ValueError("not canonical")
        return value
    except (TypeError, ValueError):
        raise ValueError("reader registry contract is not canonical") from None


def _capture(
    value: object,
    model: type[ReaderContract],
    types: tuple[frozenset, frozenset],
    max_bytes: int,
) -> tuple[ReaderContract, bytes]:
    """Capture a caller contract as exact bytes before any authority read."""

    try:
        content = _exact_bytes(value, model, types, max_bytes)
    except (TypeError, ValueError):
        raise ReaderAuthorizationRegistryConflict(
            "reader registry input is not an exact contract"
        ) from None
    try:
        return _parse_exact(content, model, types, max_bytes), content
    except ValueError:
        raise ReaderAuthorizationRegistryConflict(
            "reader registry input is not an exact contract"
        ) from None


def reader_object_bytes(value: ReaderRegistryObject) -> bytes:
    return _exact_bytes(value, ReaderRegistryObject, _OBJECT_TYPES, MAX_OBJECT_BYTES)


def reader_object_from_bytes(content: bytes) -> ReaderRegistryObject:
    value = _parse_exact(content, ReaderRegistryObject, _OBJECT_TYPES, MAX_OBJECT_BYTES)
    assert type(value) is ReaderRegistryObject
    return value


def reader_trust_sha256(trust: ReaderProviderTrust) -> str:
    return hashlib.sha256(canonical_contract_bytes(trust)).hexdigest()


def reader_grant_sha256(grant: SignedReaderGrant) -> str:
    """The grant commitment a bound session retains."""

    return hashlib.sha256(canonical_contract_bytes(grant)).hexdigest()


def reader_grant_payload_bytes(payload: ReaderGrantPayload) -> bytes:
    """Exact bytes a provider authority signs for one grant."""

    return _GRANT_SIGNATURE_DOMAIN + canonical_contract_bytes(payload)


def _snapshot_path(value: str | Path) -> Path:
    if type(value) is str:
        raw = value
    elif type(value) is type(Path()):
        try:
            parts = object.__getattribute__(value, "_parts")
        except AttributeError:
            raise TypeError("reader registry path is invalid") from None
        if (
            type(parts) is not list
            or not 1 <= len(parts) <= MAX_PATH_PARTS
            or any(type(part) is not str for part in parts)
        ):
            raise TypeError("reader registry path is invalid")
        raw = os.path.join(*tuple(parts))
    else:
        raise TypeError("reader registry path must be an exact string or platform path")
    if not raw or len(raw) > MAX_PATH_CHARS:
        raise ValueError("reader registry path is invalid")
    path = Path(os.path.abspath(raw))
    if len(path.parts) > MAX_PATH_PARTS:
        raise ValueError("reader registry path is invalid")
    return path


def _is_sha256(value: object) -> bool:
    return (
        type(value) is str
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _is_token(value: object, prefix: str) -> bool:
    return (
        type(value) is str
        and len(value) == len(prefix) + 32
        and value.startswith(prefix)
        and all(character in "0123456789abcdef" for character in value[len(prefix) :])
    )


def _write_all(descriptor: int, content: bytes) -> None:
    view = memoryview(content)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("short write")
        view = view[written:]


def _read_bounded(descriptor: int, maximum: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = os.read(descriptor, min(64 * 1024, maximum + 1 - total))
        if not chunk:
            return b"".join(chunks)
        total += len(chunk)
        if total > maximum:
            raise ReaderAuthorizationRegistryUnsafe(
                "reader registry file exceeds its bound"
            )
        chunks.append(chunk)


def _publish_file(directory_fd: int, name: str, content: bytes) -> None:
    temporary = f".tmp-{secrets.token_hex(16)}"
    descriptor: int | None = None
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=directory_fd,
        )
        _write_all(descriptor, content)
        os.fsync(descriptor)
        os.link(
            temporary,
            name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
            follow_symlinks=False,
        )
        os.fsync(directory_fd)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            os.unlink(temporary, dir_fd=directory_fd)
        except FileNotFoundError:
            pass
        os.fsync(directory_fd)


def _read_exact_object(directory_fd: int, digest: str) -> bytes:
    descriptor: int | None = None
    try:
        descriptor = os.open(
            f"{digest}.json",
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=directory_fd,
        )
        observed = os.fstat(descriptor)
        if (
            not stat.S_ISREG(observed.st_mode)
            or stat.S_IMODE(observed.st_mode) != 0o600
            or observed.st_uid != os.geteuid()
            or observed.st_nlink != 1
        ):
            raise ReaderAuthorizationRegistryUnsafe("reader registry object is unsafe")
        content = _read_bounded(descriptor, MAX_OBJECT_BYTES)
    except OSError:
        raise ReaderAuthorizationRegistryUnsafe(
            "reader registry object is unsafe"
        ) from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if hashlib.sha256(content).hexdigest() != digest:
        raise ReaderAuthorizationRegistryUnsafe(
            "reader registry object digest is invalid"
        )
    return content


def _journal_entry_sha256(entry: ReaderJournalEntry) -> str:
    placeholder = entry.model_copy(update={"entry_sha256": "0" * 64})
    return hashlib.sha256(
        b"traceback-reader-registry-journal-v1\0" + canonical_contract_bytes(placeholder)
    ).hexdigest()


def _metadata_genesis_sha256(metadata: ReaderRegistryMetadata) -> str:
    return hashlib.sha256(
        b"traceback-reader-registry-genesis-v1\0" + canonical_contract_bytes(metadata)
    ).hexdigest()


def _build_journal_entry(
    *, sequence: int, previous_entry_sha256: str, object_sha256: str, object_bytes: int
) -> ReaderJournalEntry:
    placeholder = ReaderJournalEntry.model_construct(
        sequence=sequence,
        previous_entry_sha256=previous_entry_sha256,
        object_sha256=object_sha256,
        object_bytes=object_bytes,
        entry_sha256="0" * 64,
    )
    return ReaderJournalEntry(
        **placeholder.model_dump(mode="python", exclude={"entry_sha256"}),
        entry_sha256=_journal_entry_sha256(placeholder),
    )


def _signature_valid(grant: SignedReaderGrant, trust: ReaderProviderTrust) -> bool:
    """Verify one grant against the exact current trust (active key only)."""

    payload = grant.payload
    if (
        payload.authority_id != trust.authority_id
        or payload.profile is not trust.profile
    ):
        return False
    keys = [key for key in trust.keys if key.key_version == payload.key_version]
    if len(keys) != 1 or keys[0].status is not ReaderKeyStatus.ACTIVE:
        return False
    try:
        public_key = base64.b64decode(keys[0].public_key_base64, validate=True)
        signature = base64.b64decode(grant.signature_base64, validate=True)
        _PINNED_PUBLIC_KEY(public_key).verify(
            signature, reader_grant_payload_bytes(payload)
        )
    except (InvalidSignature, ValueError):
        return False
    return True


@dataclass
class _ReaderState:
    """Validated state replayed from the committed journal in order."""

    metadata: ReaderRegistryMetadata
    head: str
    version: int = 0
    trust: ReaderProviderTrust | None = None
    trust_sha256: str = ""
    last_recorded_at: datetime | None = None
    grants: dict[str, tuple[SignedReaderGrant, str]] = field(default_factory=dict)
    grant_selectors: dict[str, str] = field(default_factory=dict)
    revoked: set[str] = field(default_factory=set)
    object_bytes: dict[str, bytes] = field(default_factory=dict)
    chain: frozenset[str] = frozenset()


def _validate_rotation(current: ReaderProviderTrust, new: ReaderProviderTrust) -> None:
    if (
        new.revision != current.revision + 1
        or new.previous_trust_sha256 != reader_trust_sha256(current)
        or new.authority_id != current.authority_id
        or new.profile is not current.profile
    ):
        raise ValueError("trust rotation does not extend the current trust")
    previous = {key.key_version: key for key in current.keys}
    for key in new.keys:
        old = previous.pop(key.key_version, None)
        if old is None:
            if key.key_version <= max(item.key_version for item in current.keys):
                raise ValueError("new trust keys must use a new version")
            continue
        if old.public_key_base64 != key.public_key_base64:
            raise ValueError("a trust key version cannot change its public key")
        if old.status is ReaderKeyStatus.REVOKED and key.status is not old.status:
            raise ValueError("a revoked trust key cannot be reactivated")
    if previous:
        raise ValueError("trust rotation cannot drop a key version")
    old_public = {key.public_key_base64 for key in current.keys}
    if any(
        key.public_key_base64 in old_public
        and key.key_version not in {item.key_version for item in current.keys}
        for key in new.keys
    ):
        raise ValueError("trust rotation cannot reuse a public key")


def _apply_record(state: _ReaderState, value: ReaderRegistryObject) -> None:
    """Apply one record's semantics; shared by journal replay and append."""

    metadata = state.metadata
    if state.last_recorded_at is not None and value.recorded_at < state.last_recorded_at:
        raise ValueError("registry record time moved backwards")
    if state.trust is None:
        if (
            value.kind is not ReaderRecordKind.TRUST
            or value.trust is None
            or value.trust.revision != 1
            or reader_trust_sha256(value.trust) != metadata.genesis_trust_sha256
        ):
            raise ValueError("registry must begin with its genesis trust")
    if value.kind is ReaderRecordKind.TRUST:
        trust = value.trust
        assert trust is not None
        if trust.profile is not metadata.profile or trust.authority_id != (
            metadata.authority_id
        ):
            raise ValueError("trust does not match the registry authority")
        if state.trust is not None:
            _validate_rotation(state.trust, trust)
        state.trust = trust
        state.trust_sha256 = reader_trust_sha256(trust)
    elif value.kind is ReaderRecordKind.GRANT:
        grant = value.grant
        assert grant is not None and state.trust is not None
        payload = grant.payload
        if (
            payload.profile is not metadata.profile
            or payload.registry_id != metadata.registry_id
            or payload.registry_epoch_sha256 != metadata.registry_epoch_sha256
            or payload.authority_id != metadata.authority_id
        ):
            raise ValueError("grant does not bind this registry")
        keys = [k for k in state.trust.keys if k.key_version == payload.key_version]
        if len(keys) != 1 or keys[0].status is not ReaderKeyStatus.ACTIVE:
            raise ValueError("grant key was not active when recorded")
        if not payload.issued_at <= value.recorded_at < payload.expires_at:
            raise ValueError("grant was not current when recorded")
        if payload.grant_selector in state.grant_selectors:
            raise ValueError("grant selector is already registered")
        if len(state.grants) >= MAX_GRANTS:
            raise ValueError("grant bound exceeded")
        digest = reader_grant_sha256(grant)
        if digest in state.grants:
            raise ValueError("grant is already registered")
        state.grants[digest] = (grant, payload.grant_selector)
        state.grant_selectors[payload.grant_selector] = digest
    else:
        revocation = value.revocation
        assert revocation is not None
        if state.grant_selectors.get(revocation.grant_selector) != (
            revocation.grant_sha256
        ):
            raise ValueError("revocation does not name a registered grant")
        if revocation.grant_sha256 in state.revoked:
            raise ValueError("grant is already revoked")
        state.revoked.add(revocation.grant_sha256)
    state.last_recorded_at = value.recorded_at


def _validate_backup(backup: ReaderRegistryBackup) -> None:
    if backup.state_version != len(backup.journal) or len(backup.objects) != len(
        backup.journal
    ):
        raise ReaderAuthorizationRegistryConflict("reader registry backup is invalid")
    contents: dict[str, bytes] = {}
    previous_digest = ""
    for item in backup.objects:
        if item.object_sha256 <= previous_digest:
            raise ReaderAuthorizationRegistryConflict(
                "reader registry backup order is invalid"
            )
        previous_digest = item.object_sha256
        content = item.object_json.encode("utf-8")
        if hashlib.sha256(content).hexdigest() != item.object_sha256:
            raise ReaderAuthorizationRegistryConflict(
                "reader registry backup digest is invalid"
            )
        contents[item.object_sha256] = content
    if {entry.object_sha256 for entry in backup.journal} != set(contents):
        raise ReaderAuthorizationRegistryConflict("reader registry backup is invalid")
    state = _ReaderState(
        metadata=backup.metadata, head=_metadata_genesis_sha256(backup.metadata)
    )
    try:
        for sequence, entry in enumerate(backup.journal, start=1):
            if (
                entry.sequence != sequence
                or entry.previous_entry_sha256 != state.head
                or entry.entry_sha256 != _journal_entry_sha256(entry)
                or entry.object_bytes != len(contents[entry.object_sha256])
            ):
                raise ValueError("journal is invalid")
            _apply_record(state, reader_object_from_bytes(contents[entry.object_sha256]))
            state.head = entry.entry_sha256
    except ValueError:
        raise ReaderAuthorizationRegistryConflict(
            "reader registry backup is invalid"
        ) from None
    if state.head != backup.state_head_sha256:
        raise ReaderAuthorizationRegistryConflict("reader registry backup is invalid")


def reader_registry_backup_from_bytes(content: bytes) -> ReaderRegistryBackup:
    if type(content) is not bytes or len(content) > MAX_BACKUP_BYTES:
        raise ReaderAuthorizationRegistryConflict(
            "reader registry backup exceeds its bound"
        )
    try:
        backup = _parse_exact(
            content, ReaderRegistryBackup, _BACKUP_TYPES, MAX_BACKUP_BYTES
        )
    except ValueError:
        raise ReaderAuthorizationRegistryConflict(
            "reader registry backup is invalid"
        ) from None
    assert type(backup) is ReaderRegistryBackup
    _validate_backup(backup)
    return backup


def _remove_partial_target(
    parent_fd: int | None, name: str, root_fd: int, objects_fd: int | None
) -> None:
    """Remove only what a failed create or restore wrote, then its root."""

    try:
        if objects_fd is not None:
            for entry in os.listdir(objects_fd):
                os.unlink(entry, dir_fd=objects_fd)
            os.rmdir("objects", dir_fd=root_fd)
        for entry in os.listdir(root_fd):
            try:
                os.unlink(entry, dir_fd=root_fd)
            except OSError:
                os.rmdir(entry, dir_fd=root_fd)
        if parent_fd is not None:
            os.rmdir(name, dir_fd=parent_fd)
            os.fsync(parent_fd)
    except OSError:
        pass


def _registry_instance_snapshot(
    registry: ReaderAuthorizationRegistry,
) -> tuple[object, ...]:
    """Capture authority-critical state without invoking caller-owned hooks."""

    instance = object.__getattribute__(registry, "__dict__")
    required = (
        "root",
        "_time_source",
        "_root_identity",
        "_objects_identity",
        "_lock_identity",
        "_journal_identity",
        "_metadata_identity",
        "_process_lock",
        "_metadata",
        "_genesis_head_sha256",
        "_head_key",
        "_trusted_head_sha256",
        "_metadata_fd",
        "_owner_pid",
    )
    if type(instance) is not dict or any(name not in instance for name in required):
        raise ReaderAuthorizationRegistryUnsafe(
            "reader registry authority state changed"
        )
    metadata = instance["_metadata"]
    descriptor = instance["_metadata_fd"]
    try:
        metadata_bytes = _exact_bytes(
            metadata, ReaderRegistryMetadata, _METADATA_TYPES, 4096
        )
    except (TypeError, ValueError):
        raise ReaderAuthorizationRegistryUnsafe(
            "reader registry authority state changed"
        ) from None
    if type(instance["_time_source"]) is not AuthorityTimeSource:
        raise ReaderAuthorizationRegistryUnsafe(
            "reader registry authority state changed"
        )
    if descriptor is not None:
        if type(descriptor) is not int:
            raise ReaderAuthorizationRegistryUnsafe(
                "reader registry authority state changed"
            )
        try:
            persisted = os.pread(descriptor, 4097, 0)
            observed = os.fstat(descriptor)
        except OSError:
            raise ReaderAuthorizationRegistryUnsafe(
                "reader registry authority state changed"
            ) from None
        if (
            persisted != metadata_bytes
            or instance["_metadata_identity"] != (observed.st_dev, observed.st_ino)
            or instance["_genesis_head_sha256"] != _metadata_genesis_sha256(metadata)
            or instance["_head_key"]
            != (metadata.registry_id, metadata.registry_epoch_sha256)
        ):
            raise ReaderAuthorizationRegistryUnsafe(
                "reader registry authority state changed"
            )
    return (
        id(instance["root"]),
        id(instance["_time_source"]),
        instance["_root_identity"],
        instance["_objects_identity"],
        instance["_lock_identity"],
        instance["_journal_identity"],
        instance["_metadata_identity"],
        id(instance["_process_lock"]),
        instance["_owner_pid"],
        metadata_bytes,
        instance["_genesis_head_sha256"],
        instance["_head_key"],
        instance["_trusted_head_sha256"],
    )


def _seal_registry_instance(registry: ReaderAuthorizationRegistry) -> None:
    _REGISTRY_INSTANCE_SEALS[registry] = _registry_instance_snapshot(registry)


_DIRECTORY_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)


class ReaderAuthorizationRegistry:
    """Descriptor-relative append-only reader grants under one shared fence."""

    def __getattribute__(self, name: str) -> object:
        # Keep this list literal so a mutable module global cannot disable it.
        if name in (
            "add_grant",
            "authority_read_fence",
            "authorize_reader_in_fence",
            "backup_bytes",
            "bind_grant_in_fence",
            "close",
            "identity",
            "revoke_grant",
            "rotate_trust",
        ):
            instance = object.__getattribute__(self, "__dict__")
            if name in instance:
                raise ReaderAuthorizationRegistryUnsafe(
                    "reader registry callable changed"
                )
        return object.__getattribute__(self, name)

    def __init__(
        self,
        root: str | Path,
        *,
        profile: ReaderAuthorizationProfile,
        configured_trust: ReaderProviderTrust,
        expected_trust_sha256: str,
        expected_registry_id: str,
        expected_registry_epoch_sha256: str,
        expected_state_head_sha256: str,
        time_source: AuthorityTimeSource,
    ) -> None:
        """Open an existing registry; every startup pin is required."""

        _require_registry_integrity(self)
        if type(time_source) is not AuthorityTimeSource:
            raise TypeError("reader registry requires the exact authority time source")
        if type(profile) is not ReaderAuthorizationProfile:
            raise ReaderAuthorizationRegistryUnsafe("reader registry profile is invalid")
        if (
            not _is_token(expected_registry_id, "reader_registry_")
            or not _is_sha256(expected_registry_epoch_sha256)
            or not _is_sha256(expected_state_head_sha256)
            or not _is_sha256(expected_trust_sha256)
        ):
            raise ReaderAuthorizationRegistryUnsafe(
                "reader registry expected identity or head is invalid"
            )
        trust, _ = _capture(configured_trust, ReaderProviderTrust, _TRUST_TYPES, 8192)
        assert type(trust) is ReaderProviderTrust
        if (
            reader_trust_sha256(trust) != expected_trust_sha256
            or trust.profile is not profile
        ):
            raise ReaderAuthorizationRegistryUnsafe(
                "reader registry configured trust is invalid"
            )
        self.root = _snapshot_path(root)
        self._time_source = time_source
        self._root_fd: int | None = None
        self._objects_fd: int | None = None
        self._lock_fd: int | None = None
        self._metadata_fd: int | None = None
        self._journal_fd: int | None = None
        self._process_lock = threading.RLock()
        # flock locks belong to the open file description, which a fork()
        # shares; a forked child must never use this instance's lock.
        self._owner_pid = os.getpid()
        try:
            try:
                root_lstat = os.stat(self.root, follow_symlinks=False)
            except FileNotFoundError:
                raise ReaderAuthorizationRegistryUnsafe(
                    "reader registry is missing"
                ) from None
            if (
                not stat.S_ISDIR(root_lstat.st_mode)
                or stat.S_IMODE(root_lstat.st_mode) != 0o700
                or root_lstat.st_uid != os.geteuid()
            ):
                raise ReaderAuthorizationRegistryUnsafe(
                    "reader registry root must be private"
                )
            self._root_fd = os.open(self.root, _DIRECTORY_FLAGS)
            bound = os.fstat(self._root_fd)
            if (bound.st_dev, bound.st_ino) != (root_lstat.st_dev, root_lstat.st_ino):
                raise ReaderAuthorizationRegistryUnsafe("reader registry root changed")
            self._root_identity = (bound.st_dev, bound.st_ino)
            self._objects_fd = os.open("objects", _DIRECTORY_FLAGS, dir_fd=self._root_fd)
            self._objects_identity = self._private_identity(
                self._objects_fd, directory=True
            )
            self._lock_fd = os.open(
                ".registry.lock",
                os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=self._root_fd,
            )
            self._lock_identity = self._private_identity(self._lock_fd, directory=False)
            self._journal_fd = os.open(
                "registry-journal.jsonl",
                os.O_RDWR
                | os.O_APPEND
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=self._root_fd,
            )
            self._journal_identity = self._private_identity(
                self._journal_fd, directory=False
            )
            self._metadata_fd = os.open(
                "registry-metadata.json",
                os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=self._root_fd,
            )
            self._metadata_identity = self._private_identity(
                self._metadata_fd, directory=False
            )
            try:
                metadata = _parse_exact(
                    _read_bounded(self._metadata_fd, 4096),
                    ReaderRegistryMetadata,
                    _METADATA_TYPES,
                    4096,
                )
            except ValueError:
                raise ReaderAuthorizationRegistryUnsafe(
                    "reader registry metadata is invalid"
                ) from None
            assert type(metadata) is ReaderRegistryMetadata
            self._metadata = metadata
            self._genesis_head_sha256 = _metadata_genesis_sha256(metadata)
            self._head_key = (metadata.registry_id, metadata.registry_epoch_sha256)
            self._trusted_head_sha256 = expected_state_head_sha256
            if (
                metadata.registry_id,
                metadata.registry_epoch_sha256,
                metadata.profile,
                metadata.authority_id,
            ) != (
                expected_registry_id,
                expected_registry_epoch_sha256,
                profile,
                trust.authority_id,
            ):
                raise ReaderAuthorizationRegistryUnsafe(
                    "reader registry expected identity or head is invalid"
                )
            with _RR_LOCK(self, exclusive=True):
                _RR_RECOVER_TEMPORARY_OBJECTS(self)
                _RR_RECOVER_TORN_JOURNAL(self)
                state = _RR_LOAD_STATE(self, check_trusted_head=False)
                if state.head != expected_state_head_sha256:
                    raise ReaderAuthorizationRegistryUnsafe(
                        "reader registry expected identity or head is invalid"
                    )
                if state.trust_sha256 != expected_trust_sha256:
                    raise ReaderAuthorizationRegistryUnsafe(
                        "reader registry configured trust is not current"
                    )
                latched = _PROCESS_PROFILE.setdefault("profile", profile)
                if latched is not profile:
                    raise ReaderAuthorizationRegistryUnsafe(
                        "reader registry profile conflicts with this run"
                    )
                _RR_ACCEPT_OBSERVED_HEAD(self, state, check_instance=False)
                _seal_registry_instance(self)
        except OSError:
            self._close_descriptors()
            raise ReaderAuthorizationRegistryUnsafe(
                "reader registry storage is unavailable"
            ) from None
        except BaseException:
            # The instance seal is not installed yet, so cleanup cannot pass
            # through the integrity-checked public close boundary.
            self._close_descriptors()
            raise

    @staticmethod
    def _private_identity(descriptor: int, *, directory: bool) -> tuple[int, int]:
        observed = os.fstat(descriptor)
        kind_ok = (
            stat.S_ISDIR(observed.st_mode) if directory else stat.S_ISREG(observed.st_mode)
        )
        if (
            not kind_ok
            or stat.S_IMODE(observed.st_mode) != (0o700 if directory else 0o600)
            or observed.st_uid != os.geteuid()
        ):
            raise ReaderAuthorizationRegistryUnsafe("reader registry storage is unsafe")
        return (observed.st_dev, observed.st_ino)

    def _close_descriptors(self) -> None:
        for name in (
            "_journal_fd",
            "_metadata_fd",
            "_lock_fd",
            "_objects_fd",
            "_root_fd",
        ):
            descriptor = getattr(self, name, None)
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
                setattr(self, name, None)

    @classmethod
    def create(
        cls,
        root: str | Path,
        *,
        profile: ReaderAuthorizationProfile,
        configured_trust: ReaderProviderTrust,
        expected_trust_sha256: str,
        time_source: AuthorityTimeSource,
    ) -> ReaderAuthorizationRegistry:
        """Create one new registry whose first record is the configured trust."""

        _require_registry_class_integrity(cls)
        if type(time_source) is not AuthorityTimeSource:
            raise TypeError("reader registry requires the exact authority time source")
        if type(profile) is not ReaderAuthorizationProfile or not _is_sha256(
            expected_trust_sha256
        ):
            raise ReaderAuthorizationRegistryUnsafe(
                "reader registry creation pins are invalid"
            )
        trust, _ = _capture(configured_trust, ReaderProviderTrust, _TRUST_TYPES, 8192)
        assert type(trust) is ReaderProviderTrust
        if (
            trust.revision != 1
            or trust.profile is not profile
            or reader_trust_sha256(trust) != expected_trust_sha256
        ):
            raise ReaderAuthorizationRegistryUnsafe(
                "reader registry configured trust is invalid"
            )
        recorded_at = _read_time(time_source)
        metadata = ReaderRegistryMetadata(
            registry_id=f"reader_registry_{secrets.token_hex(16)}",
            registry_epoch_sha256=secrets.token_hex(32),
            profile=profile,
            authority_id=trust.authority_id,
            genesis_trust_sha256=expected_trust_sha256,
        )
        content = reader_object_bytes(
            ReaderRegistryObject(
                kind=ReaderRecordKind.TRUST,
                recorded_at=recorded_at,
                trust=trust,
                grant=None,
                revocation=None,
            )
        )
        digest = hashlib.sha256(content).hexdigest()
        entry = _build_journal_entry(
            sequence=1,
            previous_entry_sha256=_metadata_genesis_sha256(metadata),
            object_sha256=digest,
            object_bytes=len(content),
        )
        return _RR_MATERIALIZE(
            root,
            metadata,
            ((digest, content),),
            (entry,),
            profile=profile,
            configured_trust=trust,
            expected_trust_sha256=expected_trust_sha256,
            time_source=time_source,
        )

    @staticmethod
    def _materialize(
        root: str | Path,
        metadata: ReaderRegistryMetadata,
        objects: tuple[tuple[str, bytes], ...],
        journal: tuple[ReaderJournalEntry, ...],
        *,
        profile: ReaderAuthorizationProfile,
        configured_trust: ReaderProviderTrust,
        expected_trust_sha256: str,
        time_source: AuthorityTimeSource,
    ) -> ReaderAuthorizationRegistry:
        """Write one new private root, reopen it, and remove it on any failure."""

        target = _snapshot_path(root)
        parent_fd: int | None = None
        root_fd: int | None = None
        objects_fd: int | None = None
        created = False
        completed = False
        try:
            parent_lstat = os.stat(target.parent, follow_symlinks=False)
            parent_fd = os.open(target.parent, _DIRECTORY_FLAGS)
            parent_bound = os.fstat(parent_fd)
            if not stat.S_ISDIR(parent_lstat.st_mode) or (
                parent_lstat.st_dev,
                parent_lstat.st_ino,
            ) != (parent_bound.st_dev, parent_bound.st_ino):
                raise ReaderAuthorizationRegistryUnsafe(
                    "reader registry parent changed"
                )
            os.mkdir(target.name, 0o700, dir_fd=parent_fd)
            created = True
            root_lstat = os.stat(target.name, dir_fd=parent_fd, follow_symlinks=False)
            root_fd = os.open(target.name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
            root_bound = os.fstat(root_fd)
            if (root_lstat.st_dev, root_lstat.st_ino) != (
                root_bound.st_dev,
                root_bound.st_ino,
            ):
                raise ReaderAuthorizationRegistryUnsafe("reader registry root changed")
            ReaderAuthorizationRegistry._private_identity(root_fd, directory=True)
            os.mkdir("objects", 0o700, dir_fd=root_fd)
            objects_fd = os.open("objects", _DIRECTORY_FLAGS, dir_fd=root_fd)
            ReaderAuthorizationRegistry._private_identity(objects_fd, directory=True)
            lock_fd = os.open(
                ".registry.lock",
                os.O_RDWR
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=root_fd,
            )
            os.close(lock_fd)
            _publish_file(
                root_fd, "registry-metadata.json", canonical_contract_bytes(metadata)
            )
            for digest, content in objects:
                _publish_file(objects_fd, f"{digest}.json", content)
            _publish_file(
                root_fd,
                "registry-journal.jsonl",
                b"".join(canonical_contract_bytes(entry) + b"\n" for entry in journal),
            )
            os.fsync(objects_fd)
            os.fsync(root_fd)
            os.fsync(parent_fd)
            # Reopen through every normal check before the target counts as
            # complete; a target that cannot reopen is removed for a retry.
            opened = _RR_CONSTRUCT(
                target,
                profile=profile,
                configured_trust=configured_trust,
                expected_trust_sha256=expected_trust_sha256,
                expected_registry_id=metadata.registry_id,
                expected_registry_epoch_sha256=metadata.registry_epoch_sha256,
                expected_state_head_sha256=journal[-1].entry_sha256,
                time_source=time_source,
            )
            completed = True
        except FileExistsError:
            raise ReaderAuthorizationRegistryConflict(
                "reader registry target already exists"
            ) from None
        except OSError:
            raise ReaderAuthorizationRegistryUnsafe(
                "reader registry materialization failed"
            ) from None
        finally:
            if created and not completed:
                if root_fd is not None:
                    _remove_partial_target(parent_fd, target.name, root_fd, objects_fd)
                elif parent_fd is not None:
                    try:
                        os.rmdir(target.name, dir_fd=parent_fd)
                        os.fsync(parent_fd)
                    except OSError:
                        pass
            for descriptor in (objects_fd, root_fd, parent_fd):
                if descriptor is not None:
                    try:
                        os.close(descriptor)
                    except OSError:
                        pass
        return opened

    def close(self) -> None:
        _require_registry_integrity(self)
        lock = getattr(self, "_process_lock", None)
        if lock is None:
            return
        with lock:
            self._close_descriptors()

    def __enter__(self) -> ReaderAuthorizationRegistry:
        _require_registry_integrity(self)
        return self

    def __exit__(self, *_: object) -> None:
        _RR_CLOSE(self)

    def __del__(self) -> None:
        try:
            _RR_CLOSE(self)
        except Exception:
            pass

    def _held_by_current_thread(self) -> bool:
        identity = getattr(self, "_root_identity", None)
        return identity is not None and _FENCE_HOLDERS.get(identity) == (
            os.getpid(),
            threading.get_ident(),
        )

    @contextmanager
    def _lock(self, *, exclusive: bool) -> Iterator[None]:
        descriptor = self._lock_fd
        if descriptor is None:
            raise ReaderAuthorizationRegistryUnsafe("reader registry is closed")
        if self._owner_pid != os.getpid():
            raise ReaderAuthorizationRegistryUnsafe(
                "reader registry belongs to another process"
            )
        if _RR_HELD(self):
            # flock would silently convert the held shared fence in place.
            raise ReaderAuthorizationRegistryUnsafe(
                "reader registry fence is already held"
            )
        with _REGISTRY_PROCESS_LOCK, self._process_lock:
            fcntl.flock(descriptor, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
            try:
                _RR_VALIDATE_STORAGE(self)
                yield
                if self._owner_pid != os.getpid():
                    raise ReaderAuthorizationRegistryUnsafe(
                        "reader registry belongs to another process"
                    )
                _RR_VALIDATE_STORAGE(self)
            finally:
                # A child forked inside the fence shares this lock's open
                # file description; its unwinding must not release the
                # parent's fence.
                if self._owner_pid == os.getpid():
                    fcntl.flock(descriptor, fcntl.LOCK_UN)

    @contextmanager
    def authority_read_fence(self) -> Iterator[None]:
        """Hold the shared cross-process fence across a reader authorization.

        Grant add, revocation and trust rotation need the exclusive lock, so
        none of them can land until this context exits.  The fence is not
        reentrant.
        """

        _require_registry_integrity(self)
        with _RR_LOCK(self, exclusive=False):
            _FENCE_HOLDERS[self._root_identity] = (os.getpid(), threading.get_ident())
            try:
                yield
            finally:
                _FENCE_HOLDERS.pop(self._root_identity, None)

    def _require_fence(self) -> None:
        if not _RR_HELD(self):
            raise ReaderAuthorizationRegistryUnsafe(
                "reader registry fence is absent"
            )

    def _validate_storage(self) -> None:
        if (
            self._root_fd is None
            or self._objects_fd is None
            or self._lock_fd is None
            or self._journal_fd is None
            or self._metadata_fd is None
        ):
            raise ReaderAuthorizationRegistryUnsafe("reader registry is closed")
        checks = (
            (None, self._root_fd, self._root_identity, True),
            ("objects", self._objects_fd, self._objects_identity, True),
            (".registry.lock", self._lock_fd, self._lock_identity, False),
            ("registry-journal.jsonl", self._journal_fd, self._journal_identity, False),
            (
                "registry-metadata.json",
                self._metadata_fd,
                self._metadata_identity,
                False,
            ),
        )
        try:
            for name, descriptor, identity, directory in checks:
                if name is None:
                    path = os.stat(self.root, follow_symlinks=False)
                else:
                    path = os.stat(name, dir_fd=self._root_fd, follow_symlinks=False)
                bound = os.fstat(descriptor)
                kind_ok = (
                    stat.S_ISDIR(path.st_mode)
                    if directory
                    else stat.S_ISREG(path.st_mode)
                )
                if (
                    not kind_ok
                    or (path.st_dev, path.st_ino) != identity
                    or (bound.st_dev, bound.st_ino) != identity
                    or stat.S_IMODE(bound.st_mode) != (0o700 if directory else 0o600)
                    or bound.st_uid != os.geteuid()
                ):
                    raise ReaderAuthorizationRegistryUnsafe(
                        "reader registry storage changed"
                    )
        except OSError:
            raise ReaderAuthorizationRegistryUnsafe(
                "reader registry storage changed"
            ) from None

    def _recover_temporary_objects(self) -> None:
        if self._objects_fd is None:
            raise ReaderAuthorizationRegistryUnsafe("reader registry is closed")
        try:
            names = os.listdir(self._objects_fd)
            for name in names:
                if (
                    type(name) is str
                    and len(name) == 37
                    and name.startswith(".tmp-")
                    and all(character in "0123456789abcdef" for character in name[5:])
                ):
                    os.unlink(name, dir_fd=self._objects_fd)
            os.fsync(self._objects_fd)
        except OSError:
            raise ReaderAuthorizationRegistryUnsafe(
                "reader registry recovery is unsafe"
            ) from None

    def _recover_torn_journal(self) -> None:
        """Drop a crash-torn suffix: an entry commits only with its newline."""

        descriptor = self._journal_fd
        if descriptor is None:
            raise ReaderAuthorizationRegistryUnsafe("reader registry is closed")
        try:
            content = os.pread(descriptor, MAX_JOURNAL_BYTES + 1, 0)
            if len(content) > MAX_JOURNAL_BYTES or not content or content.endswith(
                b"\n"
            ):
                return
            os.ftruncate(descriptor, content.rfind(b"\n") + 1)
            os.fsync(descriptor)
        except OSError:
            raise ReaderAuthorizationRegistryUnsafe(
                "reader registry recovery is unsafe"
            ) from None

    def _load_journal(self) -> tuple[ReaderJournalEntry, ...]:
        descriptor = self._journal_fd
        if descriptor is None:
            raise ReaderAuthorizationRegistryUnsafe("reader registry is closed")
        try:
            content = os.pread(descriptor, MAX_JOURNAL_BYTES + 1, 0)
        except OSError:
            raise ReaderAuthorizationRegistryUnsafe(
                "reader registry journal is unavailable"
            ) from None
        if len(content) > MAX_JOURNAL_BYTES:
            raise ReaderAuthorizationRegistryUnsafe(
                "reader registry journal bound exceeded"
            )
        if not content or not content.endswith(b"\n"):
            raise ReaderAuthorizationRegistryUnsafe(
                "reader registry journal is incomplete"
            )
        entries: list[ReaderJournalEntry] = []
        for line in content.split(b"\n")[:-1]:
            if len(entries) >= MAX_RECORDS:
                raise ReaderAuthorizationRegistryUnsafe(
                    "reader registry journal bound exceeded"
                )
            try:
                entry = _parse_exact(line, ReaderJournalEntry, _ENTRY_TYPES, 1024)
            except ValueError:
                raise ReaderAuthorizationRegistryUnsafe(
                    "reader registry journal is invalid"
                ) from None
            assert type(entry) is ReaderJournalEntry
            entries.append(entry)
        return tuple(entries)

    def _append_journal(self, entry: ReaderJournalEntry) -> None:
        descriptor = self._journal_fd
        if descriptor is None:
            raise ReaderAuthorizationRegistryUnsafe("reader registry is closed")
        content = canonical_contract_bytes(entry) + b"\n"
        try:
            committed_size = os.fstat(descriptor).st_size
        except OSError:
            raise ReaderAuthorizationRegistryUnsafe(
                "reader registry journal append failed"
            ) from None
        try:
            _write_all(descriptor, content)
            os.fsync(descriptor)
        except OSError:
            # Truncate any torn suffix so the committed chain stays readable;
            # the object it named stays an uncommitted remnant for cleanup.
            try:
                os.ftruncate(descriptor, committed_size)
                os.fsync(descriptor)
            except OSError:
                pass
            raise ReaderAuthorizationRegistryUnsafe(
                "reader registry journal append failed"
            ) from None

    def _accept_observed_head(self, state: _ReaderState, *, check_instance: bool) -> None:
        chain = state.chain
        process_head = _REGISTRY_PROCESS_HEADS.get(self._head_key)
        if process_head is not None and process_head not in chain:
            raise ReaderAuthorizationRegistryUnsafe(
                "reader registry state rollback detected"
            )
        if self._trusted_head_sha256 not in chain:
            raise ReaderAuthorizationRegistryUnsafe(
                "reader registry state rollback detected"
            )
        _REGISTRY_PROCESS_HEADS[self._head_key] = state.head
        self._trusted_head_sha256 = state.head
        if check_instance:
            _seal_registry_instance(self)

    def _load_state(self, *, check_trusted_head: bool = True) -> _ReaderState:
        """Replay only journal-committed objects; extra or missing files fail."""

        if self._objects_fd is None:
            raise ReaderAuthorizationRegistryUnsafe("reader registry is closed")
        journal = _RR_LOAD_JOURNAL(self)
        try:
            names = os.listdir(self._objects_fd)
        except OSError:
            raise ReaderAuthorizationRegistryUnsafe(
                "reader registry objects are unavailable"
            ) from None
        if len(names) > MAX_RECORDS + 1 or any(
            type(name) is not str
            or len(name) != 69
            or not name.endswith(".json")
            or any(character not in "0123456789abcdef" for character in name[:64])
            for name in names
        ):
            raise ReaderAuthorizationRegistryUnsafe(
                "reader registry contains an invalid object"
            )
        committed = {f"{entry.object_sha256}.json" for entry in journal}
        # Publication writes the object before its journal entry, so at most
        # one uncommitted object can exist after an interrupted append.
        if len(set(names) - committed) > 1 or not committed <= set(names):
            raise ReaderAuthorizationRegistryUnsafe(
                "reader registry committed objects are inconsistent"
            )
        state = _ReaderState(metadata=self._metadata, head=self._genesis_head_sha256)
        chain = [self._genesis_head_sha256]
        total = 0
        try:
            for sequence, entry in enumerate(journal, start=1):
                if (
                    entry.sequence != sequence
                    or entry.previous_entry_sha256 != state.head
                    or entry.entry_sha256 != _journal_entry_sha256(entry)
                    or entry.object_sha256 in state.object_bytes
                ):
                    raise ValueError("journal chain is invalid")
                content = _read_exact_object(self._objects_fd, entry.object_sha256)
                total += len(content)
                if len(content) != entry.object_bytes or total > MAX_TOTAL_OBJECT_BYTES:
                    raise ValueError("journal binding is invalid")
                _apply_record(state, reader_object_from_bytes(content))
                state.object_bytes[entry.object_sha256] = content
                state.head = entry.entry_sha256
                state.version = sequence
                chain.append(entry.entry_sha256)
        except ValueError:
            raise ReaderAuthorizationRegistryUnsafe(
                "reader registry journal is invalid"
            ) from None
        if state.trust is None:
            raise ReaderAuthorizationRegistryUnsafe("reader registry journal is invalid")
        state.chain = frozenset(chain)
        if check_trusted_head:
            _RR_ACCEPT_OBSERVED_HEAD(self, state, check_instance=True)
        else:
            process_head = _REGISTRY_PROCESS_HEADS.get(self._head_key)
            if process_head is not None and process_head not in chain:
                raise ReaderAuthorizationRegistryUnsafe(
                    "reader registry state rollback detected"
                )
        return state

    def _now(self, state: _ReaderState) -> datetime:
        now = _read_time(self._time_source)
        if state.last_recorded_at is not None and now < state.last_recorded_at:
            raise ReaderAuthorizationDenied(ReaderDenialReason.CLOCK_ROLLBACK)
        return now

    def _append_record(self, value: ReaderRegistryObject) -> ReaderRegistryReceipt:
        """Publish one validated record under the exclusive fence."""

        content = reader_object_bytes(value)
        digest = hashlib.sha256(content).hexdigest()
        state = _RR_LOAD_STATE(self)
        assert self._objects_fd is not None
        # The journal is the commit point: an object without an entry is an
        # interrupted append remnant and is removed unless it is these bytes.
        for name in os.listdir(self._objects_fd):
            if name[:64] not in state.object_bytes and name != f"{digest}.json":
                _read_exact_object(self._objects_fd, name[:64])
                os.unlink(name, dir_fd=self._objects_fd)
        os.fsync(self._objects_fd)
        if digest in state.object_bytes:
            raise ReaderAuthorizationRegistryConflict("reader registry record exists")
        if state.version >= MAX_RECORDS:
            raise ReaderAuthorizationRegistryConflict("reader registry is full")
        try:
            _apply_record(state, value)
        except ValueError:
            raise ReaderAuthorizationRegistryConflict(
                "reader registry record is not admissible"
            ) from None
        try:
            _publish_file(self._objects_fd, f"{digest}.json", content)
        except FileExistsError:
            if _read_exact_object(self._objects_fd, digest) != content:
                raise ReaderAuthorizationRegistryConflict(
                    "reader registry publication conflicts"
                ) from None
        _RR_APPEND_JOURNAL(
            self,
            _build_journal_entry(
                sequence=state.version + 1,
                previous_entry_sha256=state.head,
                object_sha256=digest,
                object_bytes=len(content),
            ),
        )
        final = _RR_LOAD_STATE(self)
        if final.object_bytes.get(digest) != content:
            raise ReaderAuthorizationRegistryUnsafe(
                "reader registry publication is unproven"
            )
        grant_sha256 = None
        if value.grant is not None:
            grant_sha256 = reader_grant_sha256(value.grant)
        elif value.revocation is not None:
            grant_sha256 = value.revocation.grant_sha256
        return _RR_RECEIPT(
            registry_id=self._metadata.registry_id,
            registry_epoch_sha256=self._metadata.registry_epoch_sha256,
            state_version=final.version,
            state_head_sha256=final.head,
            kind=value.kind,
            object_sha256=digest,
            trust_sha256=final.trust_sha256,
            grant_sha256=grant_sha256,
        )

    def add_grant(self, grant: SignedReaderGrant) -> ReaderRegistryReceipt:
        """Admit one provider-signed grant that is current and verifies now."""

        _require_registry_integrity(self)
        captured, _ = _capture(grant, SignedReaderGrant, _GRANT_TYPES, MAX_OBJECT_BYTES)
        assert type(captured) is SignedReaderGrant
        with _RR_LOCK(self, exclusive=True):
            state = _RR_LOAD_STATE(self)
            assert state.trust is not None
            try:
                now = self._now(state)
            except ReaderAuthorizationDenied:
                raise ReaderAuthorizationRegistryConflict(
                    "reader registry clock moved backwards"
                ) from None
            if not _signature_valid(captured, state.trust):
                raise ReaderAuthorizationRegistryConflict(
                    "reader grant is not signed by a trusted active key"
                )
            return _RR_APPEND_RECORD(
                self,
                ReaderRegistryObject(
                    kind=ReaderRecordKind.GRANT,
                    recorded_at=now,
                    trust=None,
                    grant=captured,
                    revocation=None,
                ),
            )

    def revoke_grant(
        self, grant_selector: str, *, reason: ReaderRevocationReason
    ) -> ReaderRegistryReceipt:
        """Append one revocation; revocation only removes authority."""

        _require_registry_integrity(self)
        if not _is_token(grant_selector, "reader_grant_") or type(
            reason
        ) is not ReaderRevocationReason:
            raise ReaderAuthorizationRegistryConflict("reader revocation is invalid")
        with _RR_LOCK(self, exclusive=True):
            state = _RR_LOAD_STATE(self)
            digest = state.grant_selectors.get(grant_selector)
            if digest is None:
                raise ReaderAuthorizationRegistryConflict(
                    "reader grant is not registered"
                )
            try:
                now = self._now(state)
            except ReaderAuthorizationDenied:
                raise ReaderAuthorizationRegistryConflict(
                    "reader registry clock moved backwards"
                ) from None
            return _RR_APPEND_RECORD(
                self,
                ReaderRegistryObject(
                    kind=ReaderRecordKind.REVOCATION,
                    recorded_at=now,
                    trust=None,
                    grant=None,
                    revocation=ReaderGrantRevocation(
                        grant_selector=grant_selector,
                        grant_sha256=digest,
                        reason=reason,
                    ),
                ),
            )

    def rotate_trust(
        self, trust: ReaderProviderTrust, *, expected_trust_sha256: str
    ) -> ReaderRegistryReceipt:
        """Append the next independently pinned trust revision."""

        _require_registry_integrity(self)
        captured, _ = _capture(trust, ReaderProviderTrust, _TRUST_TYPES, 8192)
        assert type(captured) is ReaderProviderTrust
        if not _is_sha256(expected_trust_sha256) or (
            reader_trust_sha256(captured) != expected_trust_sha256
        ):
            raise ReaderAuthorizationRegistryConflict(
                "reader trust rotation pin is invalid"
            )
        with _RR_LOCK(self, exclusive=True):
            state = _RR_LOAD_STATE(self)
            try:
                now = self._now(state)
            except ReaderAuthorizationDenied:
                raise ReaderAuthorizationRegistryConflict(
                    "reader registry clock moved backwards"
                ) from None
            return _RR_APPEND_RECORD(
                self,
                ReaderRegistryObject(
                    kind=ReaderRecordKind.TRUST,
                    recorded_at=now,
                    trust=captured,
                    grant=None,
                    revocation=None,
                ),
            )

    def identity(self) -> ReaderRegistryIdentity:
        """Return the identity and head an operator retains for startup."""

        _require_registry_integrity(self)
        with _RR_LOCK(self, exclusive=False):
            state = _RR_LOAD_STATE(self)
            return _RR_IDENTITY(
                registry_id=self._metadata.registry_id,
                registry_epoch_sha256=self._metadata.registry_epoch_sha256,
                profile=self._metadata.profile,
                state_version=state.version,
                state_head_sha256=state.head,
                trust_sha256=state.trust_sha256,
            )

    def _current_grant(
        self, state: _ReaderState, grant_sha256: str
    ) -> tuple[SignedReaderGrant, datetime]:
        """Check one grant against live state; deny before any other read."""

        found = state.grants.get(grant_sha256)
        if found is None:
            raise ReaderAuthorizationDenied(ReaderDenialReason.GRANT_MISSING)
        grant = found[0]
        if grant_sha256 in state.revoked:
            raise ReaderAuthorizationDenied(ReaderDenialReason.GRANT_REVOKED)
        assert state.trust is not None
        if not _signature_valid(grant, state.trust):
            raise ReaderAuthorizationDenied(ReaderDenialReason.UNTRUSTED_KEY)
        now = self._now(state)
        if not grant.payload.issued_at <= now < grant.payload.expires_at:
            raise ReaderAuthorizationDenied(ReaderDenialReason.GRANT_NOT_CURRENT)
        return grant, now

    def bind_grant_in_fence(self, grant_selector: str) -> ReaderGrantBinding:
        """Resolve a server-side grant selector for a new session binding."""

        _require_registry_integrity(self)
        _RR_REQUIRE_FENCE(self)
        if not _is_token(grant_selector, "reader_grant_"):
            raise ReaderAuthorizationDenied(ReaderDenialReason.GRANT_MISSING)
        state = _RR_LOAD_STATE(self)
        digest = state.grant_selectors.get(grant_selector)
        if digest is None:
            raise ReaderAuthorizationDenied(ReaderDenialReason.GRANT_MISSING)
        _RR_CURRENT_GRANT(self, state, digest)
        return _RR_BINDING(grant_sha256=digest, state_head_sha256=state.head)

    def authorize_reader_in_fence(
        self,
        grant_sha256: str,
        *,
        expected_state_head_sha256: str,
        cohort_registry_id: str,
        measurement_scope: MeasurementScope,
    ) -> ReaderAuthorization:
        """Re-resolve a bound grant for one exact requested scope."""

        _require_registry_integrity(self)
        _RR_REQUIRE_FENCE(self)
        if (
            not _is_sha256(grant_sha256)
            or not _is_sha256(expected_state_head_sha256)
            or not _is_token(cohort_registry_id, "cohort_registry_")
        ):
            raise ReaderAuthorizationDenied(ReaderDenialReason.SCOPE_MISMATCH)
        try:
            scope, _ = _capture(measurement_scope, MeasurementScope, _SCOPE_TYPES, 1024)
        except ReaderAuthorizationRegistryConflict:
            raise ReaderAuthorizationDenied(ReaderDenialReason.SCOPE_MISMATCH) from None
        state = _RR_LOAD_STATE(self)
        if state.head != expected_state_head_sha256:
            raise ReaderAuthorizationDenied(ReaderDenialReason.STALE_HEAD)
        grant, now = _RR_CURRENT_GRANT(self, state, grant_sha256)
        payload = grant.payload
        if (
            cohort_registry_id not in payload.cohort_registry_ids
            or scope not in payload.measurement_scopes
        ):
            raise ReaderAuthorizationDenied(ReaderDenialReason.SCOPE_MISMATCH)
        return _RR_AUTHORIZATION(
            registry_id=self._metadata.registry_id,
            registry_epoch_sha256=self._metadata.registry_epoch_sha256,
            state_version=state.version,
            state_head_sha256=state.head,
            trust_sha256=state.trust_sha256,
            grant_sha256=grant_sha256,
            role=payload.role,
            cohort_registry_id=cohort_registry_id,
            measurement_scope=scope,
            evaluated_at=now,
            expires_at=payload.expires_at,
            synthetic_only=(
                self._metadata.profile is ReaderAuthorizationProfile.SYNTHETIC
            ),
        )

    def backup_bytes(self) -> bytes:
        """Return one protected canonical backup; it is not an export artifact."""

        _require_registry_integrity(self)
        with _RR_LOCK(self, exclusive=False):
            state = _RR_LOAD_STATE(self)
            backup = ReaderRegistryBackup(
                metadata=self._metadata,
                state_version=state.version,
                state_head_sha256=state.head,
                journal=_RR_LOAD_JOURNAL(self),
                objects=tuple(
                    ReaderRegistryBackupObject(
                        object_sha256=digest, object_json=content.decode("utf-8")
                    )
                    for digest, content in sorted(state.object_bytes.items())
                ),
            )
            return _exact_bytes(
                backup, ReaderRegistryBackup, _BACKUP_TYPES, MAX_BACKUP_BYTES
            )

    @classmethod
    def restore(
        cls,
        root: str | Path,
        backup_content: bytes,
        *,
        profile: ReaderAuthorizationProfile,
        configured_trust: ReaderProviderTrust,
        expected_trust_sha256: str,
        expected_registry_id: str,
        expected_registry_epoch_sha256: str,
        expected_state_head_sha256: str,
        time_source: AuthorityTimeSource,
    ) -> ReaderAuthorizationRegistry:
        """Restore a verified backup into one new private root."""

        _require_registry_class_integrity(cls)
        backup = reader_registry_backup_from_bytes(backup_content)
        if (
            type(expected_registry_id) is not str
            or type(expected_registry_epoch_sha256) is not str
            or type(expected_state_head_sha256) is not str
            or expected_registry_id != backup.metadata.registry_id
            or expected_registry_epoch_sha256 != backup.metadata.registry_epoch_sha256
            or expected_state_head_sha256 != backup.state_head_sha256
        ):
            raise ReaderAuthorizationRegistryConflict(
                "reader registry backup expected head is invalid"
            )
        return _RR_MATERIALIZE(
            root,
            backup.metadata,
            tuple(
                (item.object_sha256, item.object_json.encode("utf-8"))
                for item in backup.objects
            ),
            backup.journal,
            profile=profile,
            configured_trust=configured_trust,
            expected_trust_sha256=expected_trust_sha256,
            time_source=time_source,
        )


def _read_time(time_source: AuthorityTimeSource) -> datetime:
    try:
        value = _PINNED_TIME_READ(time_source)
    except Exception:
        raise ReaderAuthorizationRegistryUnsafe(
            "reader registry time is unavailable"
        ) from None
    if type(value) is not datetime:
        raise ReaderAuthorizationRegistryUnsafe("reader registry time is invalid")
    try:
        return _utc_second(value, "authority time")
    except ValueError:
        raise ReaderAuthorizationRegistryUnsafe(
            "reader registry time is invalid"
        ) from None


_ENTRY_TYPES = contract_type_graph(ReaderJournalEntry)

_REGISTRY_METHOD_SEAL = MappingProxyType(
    {
        name: ReaderAuthorizationRegistry.__dict__[name]
        for name in (
            "__getattribute__",
            "__init__",
            "__enter__",
            "__exit__",
            "_private_identity",
            "_close_descriptors",
            "create",
            "_materialize",
            "close",
            "_held_by_current_thread",
            "_lock",
            "authority_read_fence",
            "_require_fence",
            "_validate_storage",
            "_recover_temporary_objects",
            "_recover_torn_journal",
            "_load_journal",
            "_append_journal",
            "_accept_observed_head",
            "_load_state",
            "_now",
            "_append_record",
            "add_grant",
            "revoke_grant",
            "rotate_trust",
            "identity",
            "_current_grant",
            "bind_grant_in_fence",
            "authorize_reader_in_fence",
            "backup_bytes",
            "restore",
        )
    }
)


def _require_registry_class_integrity(cls: type[object]) -> None:
    if cls is not ReaderAuthorizationRegistry or any(
        ReaderAuthorizationRegistry.__dict__.get(name) is not expected
        for name, expected in _REGISTRY_METHOD_SEAL.items()
    ):
        raise ReaderAuthorizationRegistryUnsafe("reader registry callable changed")


def _require_registry_integrity(registry: ReaderAuthorizationRegistry) -> None:
    _require_registry_class_integrity(type(registry))
    instance = object.__getattribute__(registry, "__dict__")
    if any(name in instance for name in _REGISTRY_METHOD_SEAL):
        raise ReaderAuthorizationRegistryUnsafe("reader registry callable changed")
    authority_sources = {
        "_PINNED_TIME_READ": AuthorityTimeSource.read,
        "_PINNED_PUBLIC_KEY": Ed25519PublicKey.from_public_bytes,
    }
    if any(
        globals().get(name) is not expected or authority_sources[name] != expected
        for name, expected in _REGISTRY_AUTHORITY_SEAL.items()
    ) or any(
        globals().get(name) is not expected
        for name, expected in _REGISTRY_ALIAS_SEAL.items()
    ):
        raise ReaderAuthorizationRegistryUnsafe(
            "reader registry authority callable changed"
        )
    if "_metadata" not in instance:
        return
    expected = _REGISTRY_INSTANCE_SEALS.get(registry)
    if expected is None or _registry_instance_snapshot(registry) != expected:
        raise ReaderAuthorizationRegistryUnsafe(
            "reader registry authority state changed"
        )


_RR_CONSTRUCT = ReaderAuthorizationRegistry
_RR_MATERIALIZE = ReaderAuthorizationRegistry._materialize
_RR_CLOSE = ReaderAuthorizationRegistry.close
_RR_LOCK = ReaderAuthorizationRegistry._lock
_RR_HELD = ReaderAuthorizationRegistry._held_by_current_thread
_RR_REQUIRE_FENCE = ReaderAuthorizationRegistry._require_fence
_RR_VALIDATE_STORAGE = ReaderAuthorizationRegistry._validate_storage
_RR_RECOVER_TEMPORARY_OBJECTS = ReaderAuthorizationRegistry._recover_temporary_objects
_RR_RECOVER_TORN_JOURNAL = ReaderAuthorizationRegistry._recover_torn_journal
_RR_LOAD_JOURNAL = ReaderAuthorizationRegistry._load_journal
_RR_APPEND_JOURNAL = ReaderAuthorizationRegistry._append_journal
_RR_ACCEPT_OBSERVED_HEAD = ReaderAuthorizationRegistry._accept_observed_head
_RR_LOAD_STATE = ReaderAuthorizationRegistry._load_state
_RR_APPEND_RECORD = ReaderAuthorizationRegistry._append_record
_RR_CURRENT_GRANT = ReaderAuthorizationRegistry._current_grant
# Result constructors are sealed so a module-global replacement cannot pair a
# grant commitment with another grant's scope or head.
_RR_RECEIPT = ReaderRegistryReceipt
_RR_IDENTITY = ReaderRegistryIdentity
_RR_BINDING = ReaderGrantBinding
_RR_AUTHORIZATION = ReaderAuthorization
_REGISTRY_AUTHORITY_SEAL = MappingProxyType(
    {
        "_PINNED_TIME_READ": _PINNED_TIME_READ,
        "_PINNED_PUBLIC_KEY": _PINNED_PUBLIC_KEY,
    }
)
_REGISTRY_ALIAS_SEAL = MappingProxyType(
    {
        name: globals()[name]
        for name in (
            "_RR_CONSTRUCT",
            "_RR_MATERIALIZE",
            "_RR_CLOSE",
            "_RR_LOCK",
            "_RR_HELD",
            "_RR_REQUIRE_FENCE",
            "_RR_VALIDATE_STORAGE",
            "_RR_RECOVER_TEMPORARY_OBJECTS",
            "_RR_RECOVER_TORN_JOURNAL",
            "_RR_LOAD_JOURNAL",
            "_RR_APPEND_JOURNAL",
            "_RR_ACCEPT_OBSERVED_HEAD",
            "_RR_LOAD_STATE",
            "_RR_APPEND_RECORD",
            "_RR_CURRENT_GRANT",
            "_RR_RECEIPT",
            "_RR_IDENTITY",
            "_RR_BINDING",
            "_RR_AUTHORIZATION",
        )
    }
)


__all__ = [
    "MAX_GRANTS",
    "MAX_GRANT_LIFETIME",
    "MAX_SCOPES",
    "SYNTHETIC_READER_AUTHORITY_ID",
    "SYNTHETIC_READER_PUBLIC_KEYS",
    "MeasurementScope",
    "ReaderAuthorityKey",
    "ReaderAuthorization",
    "ReaderAuthorizationDenied",
    "ReaderAuthorizationProfile",
    "ReaderAuthorizationRegistry",
    "ReaderAuthorizationRegistryConflict",
    "ReaderAuthorizationRegistryError",
    "ReaderAuthorizationRegistryUnsafe",
    "ReaderDenialReason",
    "ReaderGrantBinding",
    "ReaderGrantPayload",
    "ReaderGrantRevocation",
    "ReaderJournalEntry",
    "ReaderKeyStatus",
    "ReaderProviderTrust",
    "ReaderRecordKind",
    "ReaderRegistryBackup",
    "ReaderRegistryIdentity",
    "ReaderRegistryMetadata",
    "ReaderRegistryObject",
    "ReaderRegistryReceipt",
    "ReaderRevocationReason",
    "ReaderRole",
    "SignedReaderGrant",
    "reader_grant_payload_bytes",
    "reader_grant_sha256",
    "reader_object_bytes",
    "reader_object_from_bytes",
    "reader_registry_backup_from_bytes",
    "reader_trust_sha256",
]
