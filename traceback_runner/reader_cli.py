"""Local operator reader authority: key, trust, grants and launch links (E12).

Decided 2026-10-01 (docs/E12-INTEGRATION-PLAN.md, "Reader authorization"): the
provider authority for ``longitudinal_reader`` grants is a local operator
authority.  One Ed25519 signing key is generated on this workstation and held
by the operator; the CLI provisions and rotates its trust in the protected
reader-authorization registry, issues and revokes grants, and prints one-use
launch links to the terminal, as a notebook prints its token.

Profile: the operator authority is a configuration of the existing
``provider`` registry profile.  That profile already refuses the checked-in
synthetic authority ID and every synthetic public key, and the registry cannot
tell where a private key lives, so a separate ``operator`` profile would add a
closed-enum value with no additional check.

Key custody is local-file only and is NOT production custody: the private key
is an unencrypted PKCS#8 PEM file, mode ``0600``, owner-only, in a private
``0700`` authority directory that may not overlap the registry root.  It is
never printed or logged.  The same directory holds the operator's retained
registry pins (registry ID, epoch, head, trust and trust digest), which every
command needs to open the registry.

Threat model: the process/OS-user boundary is the trust boundary.  In-process
code mutation and same-user filesystem races are out of scope.
"""

from __future__ import annotations

import argparse
import base64
import fcntl
import json
import os
import secrets
import stat
import sys
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, TextIO

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from evidence_inspector.method_registry import MethodFamily
from evidence_inspector.provider_linkage_store import AuthorityTimeSource
from evidence_inspector.reader_authorization_registry import (
    MAX_GRANT_LIFETIME,
    MAX_KEYS,
    MeasurementScope,
    ReaderAuthorityKey,
    ReaderAuthorizationProfile,
    ReaderAuthorizationRegistry,
    ReaderAuthorizationRegistryError,
    ReaderGrantPayload,
    ReaderGrantState,
    ReaderKeyStatus,
    ReaderProviderTrust,
    ReaderRevocationReason,
    ReaderRole,
    SignedReaderGrant,
    reader_grant_payload_bytes,
    reader_trust_sha256,
)

PROFILE = ReaderAuthorizationProfile.PROVIDER
DEFAULT_AUTHORITY_DIR = Path.home() / ".traceback" / "reader-authority"
DEFAULT_REGISTRY = Path.home() / ".traceback" / "reader-registry"
_PINS_NAME = "authority.json"
_LOCK_NAME = ".operator.lock"
_MAX_FILE_BYTES = 64 * 1024
_DIRECTORY_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)
_FILE_FLAGS = getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)


class OperatorAuthorityError(RuntimeError):
    """Sanitized operator-authority failure; never carries key material."""


class OperatorAuthorityPins(BaseModel):
    """What the operator retains to open the registry: public data only."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["traceback.reader-operator-authority.v1"] = (
        "traceback.reader-operator-authority.v1"
    )
    registry_root: str = Field(min_length=1, max_length=4096)
    registry_id: str
    registry_epoch_sha256: str
    state_head_sha256: str
    trust: ReaderProviderTrust
    trust_sha256: str


def _key_name(version: int) -> str:
    return f"reader-key-v{version}.pem"


def _require_private(descriptor: int, *, directory: bool) -> os.stat_result:
    observed = os.fstat(descriptor)
    kind_ok = stat.S_ISDIR(observed.st_mode) if directory else stat.S_ISREG(observed.st_mode)
    if (
        not kind_ok
        or stat.S_IMODE(observed.st_mode) != (0o700 if directory else 0o600)
        or observed.st_uid != os.geteuid()
        or (not directory and observed.st_nlink != 1)
    ):
        raise OperatorAuthorityError(
            "operator authority storage must be owner-only (0700 directory, 0600 files)"
        )
    return observed


def _disjoint(authority_dir: Path, registry_root: Path) -> None:
    left = Path(os.path.realpath(authority_dir))
    right = Path(os.path.realpath(registry_root))
    if left == right or left.is_relative_to(right) or right.is_relative_to(left):
        raise OperatorAuthorityError(
            "the operator authority directory and the registry root must not overlap"
        )


class _AuthorityDirectory:
    """One pinned private authority directory, accessed descriptor-relative."""

    def __init__(self, path: Path, *, create: bool) -> None:
        self.path = Path(os.path.abspath(path))
        if create:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            try:
                os.mkdir(self.path, 0o700)
            except FileExistsError:
                raise OperatorAuthorityError(
                    "operator authority directory already exists"
                ) from None
            os.chmod(self.path, 0o700)
        try:
            before = os.stat(self.path, follow_symlinks=False)
        except FileNotFoundError:
            raise OperatorAuthorityError(
                "operator authority is not initialized; run "
                "`traceback reader authority init`"
            ) from None
        if not stat.S_ISDIR(before.st_mode):
            raise OperatorAuthorityError("operator authority directory is invalid")
        self.fd = os.open(self.path, _DIRECTORY_FLAGS)
        try:
            pinned = _require_private(self.fd, directory=True)
            if (pinned.st_dev, pinned.st_ino) != (before.st_dev, before.st_ino):
                raise OperatorAuthorityError("operator authority directory changed")
        except BaseException:
            os.close(self.fd)
            raise

    def close(self) -> None:
        os.close(self.fd)

    @contextmanager
    def lock(self, *, exclusive: bool) -> Iterator[None]:
        descriptor = os.open(
            _LOCK_NAME, os.O_RDWR | os.O_CREAT | _FILE_FLAGS, 0o600, dir_fd=self.fd
        )
        try:
            _require_private(descriptor, directory=False)
            fcntl.flock(descriptor, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
            try:
                if exclusive:
                    # An interrupted write can leave a temporary copy (possibly
                    # of a private key); remove it under the exclusive lock.
                    for entry in os.listdir(self.fd):
                        if entry.startswith(".tmp-"):
                            os.unlink(entry, dir_fd=self.fd)
                yield
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)

    def _read(self, name: str) -> bytes:
        try:
            descriptor = os.open(name, os.O_RDONLY | _FILE_FLAGS, dir_fd=self.fd)
        except FileNotFoundError:
            raise OperatorAuthorityError(
                "operator authority file is missing"
            ) from None
        try:
            _require_private(descriptor, directory=False)
            content = os.read(descriptor, _MAX_FILE_BYTES + 1)
        finally:
            os.close(descriptor)
        if len(content) > _MAX_FILE_BYTES:
            raise OperatorAuthorityError("operator authority file exceeds its bound")
        return content

    def _write_new(self, name: str, content: bytes) -> None:
        """Publish ``name`` complete or not at all; never overwrite it."""

        temporary = self._write_temporary(content)
        try:
            os.link(
                temporary,
                name,
                src_dir_fd=self.fd,
                dst_dir_fd=self.fd,
                follow_symlinks=False,
            )
        finally:
            os.unlink(temporary, dir_fd=self.fd)
        os.fsync(self.fd)

    def _write_temporary(self, content: bytes) -> str:
        name = f".tmp-{secrets.token_hex(16)}"
        descriptor = os.open(
            name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _FILE_FLAGS, 0o600, dir_fd=self.fd
        )
        try:
            try:
                os.fchmod(descriptor, 0o600)
                _require_private(descriptor, directory=False)
                view = memoryview(content)
                while view:
                    view = view[os.write(descriptor, view) :]
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        except BaseException:
            os.unlink(name, dir_fd=self.fd)
            raise
        return name

    # -- private keys ------------------------------------------------------

    def has_key(self, version: int) -> bool:
        try:
            os.stat(_key_name(version), dir_fd=self.fd, follow_symlinks=False)
        except FileNotFoundError:
            return False
        return True

    def write_key(self, version: int, key: Ed25519PrivateKey) -> None:
        try:
            self._write_new(
                _key_name(version),
                key.private_bytes(
                    encoding=serialization.Encoding.PEM,
                    format=serialization.PrivateFormat.PKCS8,
                    encryption_algorithm=serialization.NoEncryption(),
                ),
            )
        except FileExistsError:
            raise OperatorAuthorityError(
                "operator signing key file already exists"
            ) from None

    def load_key(self, version: int) -> Ed25519PrivateKey:
        try:
            key = serialization.load_pem_private_key(
                self._read(_key_name(version)), password=None
            )
        except (TypeError, ValueError):
            raise OperatorAuthorityError("operator signing key is unreadable") from None
        if not isinstance(key, Ed25519PrivateKey):
            raise OperatorAuthorityError("operator signing key is not Ed25519")
        return key

    def read_key(self, version: int, *, public_key_base64: str) -> Ed25519PrivateKey:
        key = self.load_key(version)
        if _public_base64(key) != public_key_base64:
            raise OperatorAuthorityError(
                "operator signing key does not match the trusted public key"
            )
        return key

    def remove_key(self, version: int) -> None:
        try:
            os.unlink(_key_name(version), dir_fd=self.fd)
        except FileNotFoundError:
            return
        os.fsync(self.fd)

    # -- retained pins -------------------------------------------------------

    def read_pins(self) -> OperatorAuthorityPins:
        try:
            pins = OperatorAuthorityPins.model_validate_json(self._read(_PINS_NAME))
        except ValidationError:
            raise OperatorAuthorityError("operator authority pins are invalid") from None
        if reader_trust_sha256(pins.trust) != pins.trust_sha256:
            raise OperatorAuthorityError("operator authority pins are invalid")
        _disjoint(self.path, Path(pins.registry_root))
        return pins

    def write_pins(self, pins: OperatorAuthorityPins) -> None:
        content = json.dumps(
            pins.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
        ).encode() + b"\n"
        temporary = self._write_temporary(content)
        try:
            os.rename(temporary, _PINS_NAME, src_dir_fd=self.fd, dst_dir_fd=self.fd)
        except BaseException:
            os.unlink(temporary, dir_fd=self.fd)
            raise
        os.fsync(self.fd)


def _public_base64(key: Ed25519PrivateKey) -> str:
    return base64.b64encode(
        key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
        )
    ).decode("ascii")


def _open_registry(
    pins: OperatorAuthorityPins, time_source: AuthorityTimeSource
) -> ReaderAuthorizationRegistry:
    return ReaderAuthorizationRegistry(
        pins.registry_root,
        profile=PROFILE,
        configured_trust=pins.trust,
        expected_trust_sha256=pins.trust_sha256,
        expected_registry_id=pins.registry_id,
        expected_registry_epoch_sha256=pins.registry_epoch_sha256,
        expected_state_head_sha256=pins.state_head_sha256,
        time_source=time_source,
    )


def _whole_second(time_source: AuthorityTimeSource) -> datetime:
    now = time_source.read()
    return now.replace(microsecond=0)


def _signing_version(trust: ReaderProviderTrust, directory: _AuthorityDirectory) -> int:
    active = [
        key.key_version
        for key in trust.keys
        if key.status is ReaderKeyStatus.ACTIVE and directory.has_key(key.key_version)
    ]
    if not active:
        raise OperatorAuthorityError("no active operator signing key is held locally")
    return max(active)


def _rotated(
    trust: ReaderProviderTrust, keys: tuple[ReaderAuthorityKey, ...]
) -> ReaderProviderTrust:
    return ReaderProviderTrust(
        profile=PROFILE,
        authority_id=trust.authority_id,
        revision=trust.revision + 1,
        previous_trust_sha256=reader_trust_sha256(trust),
        keys=keys,
    )


# -- commands -----------------------------------------------------------------


def _authority_init(
    args: argparse.Namespace, time_source: AuthorityTimeSource, out: TextIO
) -> int:
    authority_dir = Path(args.authority_dir)
    registry_root = Path(os.path.abspath(args.registry))
    _disjoint(authority_dir, registry_root)
    if os.path.lexists(registry_root):
        raise OperatorAuthorityError("reader registry root already exists")
    directory = _AuthorityDirectory(authority_dir, create=True)
    completed = False
    try:
        with directory.lock(exclusive=True):
            key = Ed25519PrivateKey.generate()
            directory.write_key(1, key)
            trust = ReaderProviderTrust(
                profile=PROFILE,
                authority_id=f"reader_authority_{secrets.token_hex(16)}",
                revision=1,
                previous_trust_sha256=None,
                keys=(
                    ReaderAuthorityKey(
                        key_version=1,
                        public_key_base64=_public_base64(key),
                        status=ReaderKeyStatus.ACTIVE,
                    ),
                ),
            )
            trust_sha256 = reader_trust_sha256(trust)
            registry_root.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            registry = ReaderAuthorizationRegistry.create(
                registry_root,
                profile=PROFILE,
                configured_trust=trust,
                expected_trust_sha256=trust_sha256,
                time_source=time_source,
            )
            try:
                identity = registry.identity()
            finally:
                registry.close()
            directory.write_pins(
                OperatorAuthorityPins(
                    registry_root=str(registry_root),
                    registry_id=identity.registry_id,
                    registry_epoch_sha256=identity.registry_epoch_sha256,
                    state_head_sha256=identity.state_head_sha256,
                    trust=trust,
                    trust_sha256=trust_sha256,
                )
            )
            completed = True
    finally:
        if not completed:
            # Leave nothing half-provisioned: remove the key and the directory
            # so init can be retried.  A registry that was created stays and
            # must be removed by hand (init refuses an existing root).
            for name in (_key_name(1), _LOCK_NAME):
                try:
                    os.unlink(name, dir_fd=directory.fd)
                except FileNotFoundError:
                    pass
        directory.close()
        if not completed:
            try:
                os.rmdir(directory.path)
            except OSError:
                pass
    print(f"authority {trust.authority_id} (profile provider, local operator key)", file=out)
    print("signing key v1 generated; private key stored owner-only (0600)", file=out)
    print(f"registry {identity.registry_id} at {registry_root}", file=out)
    print(f"trust revision 1 sha256 {trust_sha256}", file=out)
    return 0


def _authority_rotate(
    args: argparse.Namespace, time_source: AuthorityTimeSource, out: TextIO
) -> int:
    """Add key version N+1, then revoke every older active key.

    Resumable: if an earlier rotation stopped after adding its key (more than
    one key active), this run only finishes it by revoking all but the newest
    active key.  Every run deletes the private key files of revoked versions.
    """

    directory = _AuthorityDirectory(Path(args.authority_dir), create=False)
    try:
        with directory.lock(exclusive=True):
            pins = directory.read_pins()
            trust = pins.trust
            active = [
                key.key_version
                for key in trust.keys
                if key.status is ReaderKeyStatus.ACTIVE
            ]
            resuming = len(active) > 1
            if not resuming and len(trust.keys) >= MAX_KEYS:
                raise OperatorAuthorityError(
                    "operator trust holds 16 key versions (15 rotations); "
                    "initialize a new authority and registry"
                )
            registry = _open_registry(pins, time_source)
            try:
                if resuming:
                    version = max(active)
                    added = trust
                else:
                    version = max(key.key_version for key in trust.keys) + 1
                    # A key file left by an interrupted rotation is reused,
                    # never overwritten.
                    if directory.has_key(version):
                        key = directory.load_key(version)
                    else:
                        key = Ed25519PrivateKey.generate()
                        directory.write_key(version, key)
                    added = _rotated(
                        trust,
                        (
                            *trust.keys,
                            ReaderAuthorityKey(
                                key_version=version,
                                public_key_base64=_public_base64(key),
                                status=ReaderKeyStatus.ACTIVE,
                            ),
                        ),
                    )
                    receipt = registry.rotate_trust(
                        added, expected_trust_sha256=reader_trust_sha256(added)
                    )
                    pins = pins.model_copy(
                        update={
                            "trust": added,
                            "trust_sha256": receipt.trust_sha256,
                            "state_head_sha256": receipt.state_head_sha256,
                        }
                    )
                    directory.write_pins(pins)
                retiring = [item for item in active if item != version]
                retired = _rotated(
                    added,
                    tuple(
                        key.model_copy(update={"status": ReaderKeyStatus.REVOKED})
                        if key.key_version in retiring
                        else key
                        for key in added.keys
                    ),
                )
                receipt = registry.rotate_trust(
                    retired, expected_trust_sha256=reader_trust_sha256(retired)
                )
                directory.write_pins(
                    pins.model_copy(
                        update={
                            "trust": retired,
                            "trust_sha256": receipt.trust_sha256,
                            "state_head_sha256": receipt.state_head_sha256,
                        }
                    )
                )
            finally:
                registry.close()
            for key in retired.keys:
                if key.status is ReaderKeyStatus.REVOKED:
                    directory.remove_key(key.key_version)
    finally:
        directory.close()
    if resuming:
        print(f"finished an interrupted rotation; signing key v{version} active", file=out)
    else:
        print(f"signing key v{version} added and active", file=out)
    print(
        "revoked key versions: " + ", ".join(f"v{item}" for item in retiring),
        file=out,
    )
    print(
        f"trust revision {retired.revision} sha256 {reader_trust_sha256(retired)}",
        file=out,
    )
    print(
        "grants signed by a revoked key no longer authorize; reissue them", file=out
    )
    return 0


def _authority_recover(
    args: argparse.Namespace, time_source: AuthorityTimeSource, out: TextIO
) -> int:
    """Re-pin after a crash between a registry append and the pins write.

    Accepted only when the retained head is a committed ancestor of the
    journal tail and the registry then opens and fully replays at that tail.
    """

    directory = _AuthorityDirectory(Path(args.authority_dir), create=False)
    try:
        with directory.lock(exclusive=True):
            pins = directory.read_pins()
            journal = Path(pins.registry_root) / "registry-journal.jsonl"
            with open(journal, "rb") as stream:
                content = stream.read(2 * 1024 * 1024 + 1)
            entries = [json.loads(line) for line in content.splitlines() if line]
            heads = [entry.get("entry_sha256") for entry in entries]
            if pins.state_head_sha256 not in heads or any(
                type(head) is not str for head in heads
            ):
                raise OperatorAuthorityError(
                    "retained head is not in the registry history; refusing to re-pin"
                )
            tail = heads[-1]
            trust = pins.trust
            registry = None
            # Only a trust whose active keys this operator holds is adopted;
            # the registry replay then proves the tail extends the pin.
            for candidate in _candidate_trusts(pins, entries):
                if not _held_by_operator(candidate, directory):
                    continue
                try:
                    registry = _open_registry(
                        pins.model_copy(
                            update={
                                "state_head_sha256": tail,
                                "trust": candidate,
                                "trust_sha256": reader_trust_sha256(candidate),
                            }
                        ),
                        time_source,
                    )
                except ReaderAuthorizationRegistryError:
                    continue
                trust = candidate
                break
            if registry is None:
                raise OperatorAuthorityError("registry could not be re-pinned")
            registry.close()
            directory.write_pins(
                pins.model_copy(
                    update={
                        "state_head_sha256": tail,
                        "trust": trust,
                        "trust_sha256": reader_trust_sha256(trust),
                    }
                )
            )
    finally:
        directory.close()
    print("operator pins now match the registry head", file=out)
    return 0


def _held_by_operator(
    trust: ReaderProviderTrust, directory: _AuthorityDirectory
) -> bool:
    """A recovered trust must be one this operator provisioned: same authority,
    and every active key matches a private key held in the authority dir."""

    active = [key for key in trust.keys if key.status is ReaderKeyStatus.ACTIVE]
    for key in active:
        if not directory.has_key(key.key_version):
            return False
        try:
            directory.read_key(key.key_version, public_key_base64=key.public_key_base64)
        except OperatorAuthorityError:
            return False
    return bool(active)


def _candidate_trusts(
    pins: OperatorAuthorityPins, entries: list[dict[str, Any]]
) -> Iterator[ReaderProviderTrust]:
    yield pins.trust
    objects = Path(pins.registry_root) / "objects"
    for entry in reversed(entries):
        digest = entry.get("object_sha256")
        if not isinstance(digest, str) or len(digest) != 64:
            continue
        try:
            record = json.loads((objects / f"{digest}.json").read_bytes()[:65536])
            if record.get("kind") == "trust":
                yield ReaderProviderTrust.model_validate(record["trust"])
        except (OSError, ValueError, KeyError, ValidationError):
            continue


def _authority_show(
    args: argparse.Namespace, time_source: AuthorityTimeSource, out: TextIO
) -> int:
    del time_source
    directory = _AuthorityDirectory(Path(args.authority_dir), create=False)
    try:
        with directory.lock(exclusive=False):
            pins = directory.read_pins()
    finally:
        directory.close()
    print(f"authority {pins.trust.authority_id} (profile provider)", file=out)
    print(f"registry {pins.registry_id} at {pins.registry_root}", file=out)
    print(f"trust revision {pins.trust.revision} sha256 {pins.trust_sha256}", file=out)
    for key in pins.trust.keys:
        print(f"key v{key.key_version} {key.status.value}", file=out)
    return 0


def _parse_scope(value: str) -> MeasurementScope:
    parts = value.split(":")
    if len(parts) != 3:
        raise OperatorAuthorityError(
            "measurement scope must be FAMILY:QUANTITY:UNIT, e.g. "
            "fragment_measurement:qty_short_fraction:unit_fraction"
        )
    try:
        return MeasurementScope(
            family=MethodFamily(parts[0]), quantity_id=parts[1], unit=parts[2]
        )
    except ValueError:
        raise OperatorAuthorityError("measurement scope is invalid") from None


def _grant_issue(
    args: argparse.Namespace, time_source: AuthorityTimeSource, out: TextIO
) -> int:
    lifetime = timedelta(days=args.expires_in_days)
    if not timedelta(0) < lifetime <= MAX_GRANT_LIFETIME:
        raise OperatorAuthorityError(
            f"grant lifetime must be 1 to {MAX_GRANT_LIFETIME.days} days"
        )
    scopes = tuple(
        sorted({_parse_scope(item) for item in args.measurement}, key=lambda s: s.sort_key())
    )
    cohorts = tuple(sorted(set(args.cohort)))
    directory = _AuthorityDirectory(Path(args.authority_dir), create=False)
    try:
        with directory.lock(exclusive=True):
            pins = directory.read_pins()
            version = _signing_version(pins.trust, directory)
            public = next(
                key.public_key_base64
                for key in pins.trust.keys
                if key.key_version == version
            )
            key = directory.read_key(version, public_key_base64=public)
            issued_at = _whole_second(time_source)
            selector = f"reader_grant_{secrets.token_hex(16)}"
            try:
                payload = ReaderGrantPayload(
                    profile=PROFILE,
                    registry_id=pins.registry_id,
                    registry_epoch_sha256=pins.registry_epoch_sha256,
                    grant_selector=selector,
                    authority_id=pins.trust.authority_id,
                    key_version=version,
                    role=ReaderRole.LONGITUDINAL_READER,
                    cohort_registry_ids=cohorts,
                    measurement_scopes=scopes,
                    issued_at=issued_at,
                    expires_at=issued_at + lifetime,
                )
            except ValidationError:
                raise OperatorAuthorityError(
                    "grant scope is invalid (1-16 exact cohort_registry_<32 hex> "
                    "IDs and 1-16 exact measurement scopes)"
                ) from None
            grant = SignedReaderGrant(
                payload=payload,
                signature_base64=base64.b64encode(
                    key.sign(reader_grant_payload_bytes(payload))
                ).decode("ascii"),
            )
            registry = _open_registry(pins, time_source)
            try:
                receipt = registry.add_grant(grant)
            finally:
                registry.close()
            directory.write_pins(
                pins.model_copy(update={"state_head_sha256": receipt.state_head_sha256})
            )
    finally:
        directory.close()
    print(selector, file=out)
    print(f"expires {payload.expires_at.isoformat()} (signed by key v{version})", file=out)
    return 0


def _grant_revoke(
    args: argparse.Namespace, time_source: AuthorityTimeSource, out: TextIO
) -> int:
    directory = _AuthorityDirectory(Path(args.authority_dir), create=False)
    try:
        with directory.lock(exclusive=True):
            pins = directory.read_pins()
            registry = _open_registry(pins, time_source)
            try:
                receipt = registry.revoke_grant(
                    args.selector, reason=ReaderRevocationReason(args.reason)
                )
            finally:
                registry.close()
            directory.write_pins(
                pins.model_copy(update={"state_head_sha256": receipt.state_head_sha256})
            )
    finally:
        directory.close()
    print(f"{args.selector} revoked", file=out)
    return 0


def _grant_list(
    args: argparse.Namespace, time_source: AuthorityTimeSource, out: TextIO
) -> int:
    directory = _AuthorityDirectory(Path(args.authority_dir), create=False)
    try:
        with directory.lock(exclusive=False):
            pins = directory.read_pins()
            registry = _open_registry(pins, time_source)
            try:
                listings = registry.grant_states()
            finally:
                registry.close()
    finally:
        directory.close()
    for item in listings:
        print(f"{item.grant_selector} {item.state.value}", file=out)
    return 0


def _launch(
    args: argparse.Namespace,
    time_source: AuthorityTimeSource,
    out: TextIO,
    stdin: TextIO,
) -> int:
    from traceback_runner.store import JobStore
    from traceback_runner.web.server import RunningLocalWebService

    directory = _AuthorityDirectory(Path(args.authority_dir), create=False)
    try:
        with directory.lock(exclusive=False):
            pins = directory.read_pins()
            registry = _open_registry(pins, time_source)
    finally:
        directory.close()
    try:
        states = {item.grant_selector: item.state for item in registry.grant_states()}
        if states.get(args.grant) is not ReaderGrantState.ACTIVE:
            raise OperatorAuthorityError("grant is not active")
        root = Path(args.root)
        store = JobStore(root / "runner.sqlite3")
        with RunningLocalWebService.start(
            store=store,
            state_directory=root / "reader-web",
            ipv6=args.ipv6,
            reader_registry=registry,
        ) as service:
            print(
                "Open this one-use link in a browser on this machine within 60 seconds.",
                file=out,
            )
            print(
                "Press Enter for a fresh link; Ctrl-D or Ctrl-C stops the server.",
                file=out,
            )
            while True:
                try:
                    link = service.issue_reader_launch_url(args.grant)
                except RuntimeError:
                    link = "Too many unused links; wait 60 seconds, then press Enter."
                print(link, file=out, flush=True)
                try:
                    line = stdin.readline()
                except KeyboardInterrupt:
                    break
                if not line:
                    break
    except KeyboardInterrupt:
        pass
    finally:
        registry.close()
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="traceback reader",
        description=(
            "Local operator reader authority (E12). Key custody is a local file, "
            "not production custody."
        ),
    )
    commands = parser.add_subparsers(dest="command", required=True)

    def authority_dir(command: argparse.ArgumentParser) -> None:
        command.add_argument(
            "--authority-dir",
            default=str(DEFAULT_AUTHORITY_DIR),
            help=f"private operator authority directory (default: {DEFAULT_AUTHORITY_DIR})",
        )

    authority = commands.add_parser("authority", help="operator signing key and trust")
    authority_commands = authority.add_subparsers(dest="authority_command", required=True)
    init = authority_commands.add_parser(
        "init", help="generate the operator key and create the registry"
    )
    authority_dir(init)
    init.add_argument(
        "--registry",
        default=str(DEFAULT_REGISTRY),
        help=f"new reader registry root (default: {DEFAULT_REGISTRY})",
    )
    for name, text in (
        ("rotate", "add a new key version, then revoke the old one"),
        ("show", "show the authority, trust revision and key states"),
        ("recover", "re-pin after an interrupted command (head must extend the pin)"),
    ):
        authority_dir(authority_commands.add_parser(name, help=text))

    grant = commands.add_parser("grant", help="issue, revoke and list reader grants")
    grant_commands = grant.add_subparsers(dest="grant_command", required=True)
    issue = grant_commands.add_parser("issue", help="sign and register one grant")
    authority_dir(issue)
    issue.add_argument("--cohort", action="append", required=True, metavar="ID")
    issue.add_argument(
        "--measurement",
        action="append",
        required=True,
        metavar="FAMILY:QUANTITY:UNIT",
    )
    issue.add_argument(
        "--expires-in-days",
        type=int,
        required=True,
        help=f"1 to {MAX_GRANT_LIFETIME.days}",
    )
    revoke = grant_commands.add_parser("revoke", help="revoke one grant")
    authority_dir(revoke)
    revoke.add_argument("selector")
    revoke.add_argument(
        "--reason",
        choices=[item.value for item in ReaderRevocationReason],
        default=ReaderRevocationReason.OPERATOR_REQUEST.value,
    )
    authority_dir(grant_commands.add_parser("list", help="list selectors and states"))

    launch = commands.add_parser(
        "launch", help="serve on loopback and print a one-use launch link for a grant"
    )
    authority_dir(launch)
    launch.add_argument("--grant", required=True, metavar="SELECTOR")
    launch.add_argument("--root", default=".traceback", help="local runner data root")
    launch.add_argument("--ipv6", action="store_true")
    return parser


def run(
    argv: Sequence[str],
    *,
    time_source: AuthorityTimeSource | None = None,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    """Run one reader command; errors print a sanitized message, never key data."""

    out = stdout or sys.stdout
    err = stderr or sys.stderr
    try:
        args = _parser().parse_args(list(argv))
    except SystemExit as exc:
        return int(exc.code or 0)
    clock = time_source or AuthorityTimeSource.system()
    try:
        if args.command == "authority":
            handler = {
                "init": _authority_init,
                "rotate": _authority_rotate,
                "show": _authority_show,
                "recover": _authority_recover,
            }[args.authority_command]
            return handler(args, clock, out)
        if args.command == "grant":
            handler = {
                "issue": _grant_issue,
                "revoke": _grant_revoke,
                "list": _grant_list,
            }[args.grant_command]
            return handler(args, clock, out)
        return _launch(args, clock, out, stdin or sys.stdin)
    except OperatorAuthorityError as exc:
        print(f"error: {exc}", file=err)
        return 3
    except ReaderAuthorizationRegistryError as exc:
        print(f"error: reader registry refused the operation: {exc}", file=err)
        return 3
    except OSError as exc:
        print(f"error: local storage operation failed ({type(exc).__name__})", file=err)
        return 6


def main(argv: Sequence[str]) -> int:
    """Entry point for ``traceback reader ...``; ``argv`` excludes ``reader``."""

    return run(argv)


__all__ = [
    "OperatorAuthorityError",
    "OperatorAuthorityPins",
    "main",
    "run",
]
