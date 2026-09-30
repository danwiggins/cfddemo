"""Ed25519 signing with explicit purpose and synthetic-only development trust."""

from __future__ import annotations

import base64
import hashlib
import json
import threading
from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, Literal

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from pydantic import BaseModel, ConfigDict, StringConstraints


class SigningError(ValueError):
    """Base class for expected trust and signature failures."""


class UnknownKeyError(SigningError):
    """The signature names a key absent from the trust store."""


class RevokedKeyError(SigningError):
    """The signature names a locally revoked key."""


class WrongPurposeError(SigningError):
    """A key or signature is being used for the wrong signing purpose."""


class InvalidSignatureError(SigningError):
    """The Ed25519 signature does not authenticate the supplied bytes."""


class TrustNamespaceError(SigningError):
    """The requested trust namespace is not allowed for this record."""


class KeyPurpose(StrEnum):
    RELEASE = "release"
    RESULT = "result"


class TrustNamespace(StrEnum):
    DEVELOPMENT_SYNTHETIC = "development-synthetic"
    EXTERNAL_RELEASE = "external-release"


KeyId = Annotated[
    str,
    StringConstraints(
        min_length=8,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$",
    ),
]


class SignatureEnvelope(BaseModel):
    """Portable signature metadata; signed content is stored separately."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    schema_version: Annotated[
        str, StringConstraints(pattern=r"^traceback\.signature\.v1$")
    ] = "traceback.signature.v1"
    algorithm: Annotated[str, StringConstraints(pattern=r"^Ed25519$")] = "Ed25519"
    namespace: TrustNamespace
    purpose: KeyPurpose
    key_id: KeyId
    signature_base64: Annotated[str, StringConstraints(min_length=88, max_length=88)]


class PublicTrustedKey(BaseModel):
    """Strict public-only key representation for an offline trust file."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    key_id: KeyId
    namespace: Literal[TrustNamespace.DEVELOPMENT_SYNTHETIC] = (
        TrustNamespace.DEVELOPMENT_SYNTHETIC
    )
    purpose: KeyPurpose
    public_key_base64: Annotated[str, StringConstraints(min_length=44, max_length=44)]
    revoked: bool = False


class DevelopmentTrustDocument(BaseModel):
    """Public development trust roots; possession does not grant signing authority."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    schema_version: Literal["traceback.development-trust.v1"] = (
        "traceback.development-trust.v1"
    )
    namespace: Literal[TrustNamespace.DEVELOPMENT_SYNTHETIC] = (
        TrustNamespace.DEVELOPMENT_SYNTHETIC
    )
    keys: tuple[PublicTrustedKey, ...]


@dataclass(frozen=True)
class DevelopmentSigningKey:
    """Ephemeral private key for synthetic development records only."""

    key_id: str
    purpose: KeyPurpose
    private_key: Ed25519PrivateKey
    namespace: TrustNamespace = TrustNamespace.DEVELOPMENT_SYNTHETIC

    def __post_init__(self) -> None:
        expected = _development_key_id(self.public_key_bytes(), self.purpose)
        if self.key_id != expected:
            raise SigningError(
                "development key identifier does not match its public key"
            )

    def public_key_bytes(self) -> bytes:
        return self.private_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )


@dataclass(frozen=True)
class TrustedKey:
    key_id: str
    purpose: KeyPurpose
    public_key_bytes: bytes
    namespace: TrustNamespace = TrustNamespace.DEVELOPMENT_SYNTHETIC
    revoked: bool = False

    def __post_init__(self) -> None:
        if len(self.public_key_bytes) != 32:
            raise ValueError("Ed25519 public keys must be exactly 32 bytes")
        expected = trusted_key_id(
            self.public_key_bytes, self.purpose, namespace=self.namespace
        )
        if self.key_id != expected:
            raise SigningError("trusted key identifier does not match its public key")


class TrustStore:
    """Offline key registry with local revocation state."""

    def __init__(self, keys: tuple[TrustedKey, ...] = ()) -> None:
        self._lock = threading.RLock()
        self._keys: dict[str, TrustedKey] = {}
        for key in keys:
            self.add(key)

    def add(self, key: TrustedKey) -> None:
        with self._lock:
            existing = self._keys.get(key.key_id)
            if existing is not None and existing != key:
                raise SigningError(f"conflicting trust entry for key {key.key_id!r}")
            self._keys[key.key_id] = key

    def add_signing_key(self, key: DevelopmentSigningKey) -> None:
        self.add(
            TrustedKey(
                key_id=key.key_id,
                purpose=key.purpose,
                public_key_bytes=key.public_key_bytes(),
                namespace=key.namespace,
            )
        )

    def revoke(self, key_id: str) -> None:
        with self._lock:
            key = self._keys.get(key_id)
            if key is None:
                raise UnknownKeyError(f"unknown signing key {key_id!r}")
            self._keys[key_id] = TrustedKey(
                key_id=key.key_id,
                purpose=key.purpose,
                public_key_bytes=key.public_key_bytes,
                namespace=key.namespace,
                revoked=True,
            )

    def resolve(self, key_id: str) -> TrustedKey:
        with self._lock:
            try:
                return self._keys[key_id]
            except KeyError as exc:
                raise UnknownKeyError(f"unknown signing key {key_id!r}") from exc


def _development_key_id(public_key_bytes: bytes, purpose: KeyPurpose) -> str:
    return trusted_key_id(
        public_key_bytes,
        purpose,
        namespace=TrustNamespace.DEVELOPMENT_SYNTHETIC,
    )


def trusted_key_id(
    public_key_bytes: bytes,
    purpose: KeyPurpose,
    *,
    namespace: TrustNamespace,
) -> str:
    """Derive a namespace-bound public key identifier."""

    fingerprint = hashlib.sha256(public_key_bytes).hexdigest()[:24]
    prefix = "dev" if namespace == TrustNamespace.DEVELOPMENT_SYNTHETIC else "external"
    return f"{prefix}-{purpose.value}-{fingerprint}"


def generate_development_keypair(purpose: KeyPurpose) -> DevelopmentSigningKey:
    """Create an ephemeral synthetic-only Ed25519 signing key."""

    private_key = Ed25519PrivateKey.generate()
    public_bytes = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return DevelopmentSigningKey(
        key_id=_development_key_id(public_bytes, purpose),
        purpose=purpose,
        private_key=private_key,
    )


def sign_bytes(
    content: bytes,
    key: DevelopmentSigningKey,
    *,
    purpose: KeyPurpose,
) -> SignatureEnvelope:
    """Sign exact bytes, refusing cross-purpose key reuse."""

    if key.purpose != purpose:
        raise WrongPurposeError(
            f"{key.purpose.value} key cannot sign {purpose.value} content"
        )
    signature = key.private_key.sign(content)
    return SignatureEnvelope(
        namespace=key.namespace,
        purpose=purpose,
        key_id=key.key_id,
        signature_base64=base64.b64encode(signature).decode("ascii"),
    )


def verify_signature(
    content: bytes,
    envelope: SignatureEnvelope,
    trust_store: TrustStore,
    *,
    purpose: KeyPurpose,
    namespace: TrustNamespace = TrustNamespace.DEVELOPMENT_SYNTHETIC,
) -> None:
    """Verify exact bytes against trusted purpose, namespace, and revocation state."""

    if envelope.purpose != purpose:
        raise WrongPurposeError(
            f"{envelope.purpose.value} signature cannot verify {purpose.value} content"
        )
    if envelope.namespace != namespace:
        raise TrustNamespaceError(
            f"signature namespace {envelope.namespace.value!r} is not allowed"
        )
    trusted = trust_store.resolve(envelope.key_id)
    if trusted.revoked:
        raise RevokedKeyError(f"signing key {trusted.key_id!r} is revoked")
    if trusted.purpose != purpose:
        raise WrongPurposeError(
            f"trusted {trusted.purpose.value} key cannot verify {purpose.value} content"
        )
    if trusted.namespace != namespace:
        raise TrustNamespaceError(
            f"key namespace {trusted.namespace.value!r} is not allowed"
        )
    try:
        signature = base64.b64decode(envelope.signature_base64, validate=True)
        Ed25519PublicKey.from_public_bytes(trusted.public_key_bytes).verify(
            signature, content
        )
    except (InvalidSignature, ValueError) as exc:
        raise InvalidSignatureError("Ed25519 signature verification failed") from exc


def development_trust_bytes(*keys: DevelopmentSigningKey) -> bytes:
    """Serialize only public development keys for a later offline invocation."""

    public_keys = tuple(
        PublicTrustedKey(
            key_id=key.key_id,
            purpose=key.purpose,
            public_key_base64=base64.b64encode(key.public_key_bytes()).decode("ascii"),
        )
        for key in sorted(keys, key=lambda item: item.key_id)
    )
    if len({key.key_id for key in public_keys}) != len(public_keys):
        raise SigningError("development trust keys must have unique key identifiers")
    document = DevelopmentTrustDocument(keys=public_keys)
    return (
        json.dumps(
            document.model_dump(mode="json"),
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        + b"\n"
    )


def load_development_trust(content: bytes) -> TrustStore:
    """Load a strict public-only development trust document from exact bytes."""

    try:
        raw = json.loads(content)
        document = DevelopmentTrustDocument.model_validate(raw)
    except Exception as exc:
        raise SigningError("invalid development trust document") from exc
    expected = development_trust_document_bytes(document)
    if content != expected:
        raise SigningError("development trust document is not canonical JSON")
    store = TrustStore()
    for key in document.keys:
        try:
            public_bytes = base64.b64decode(key.public_key_base64, validate=True)
        except ValueError as exc:
            raise SigningError("invalid public key encoding") from exc
        store.add(
            TrustedKey(
                key_id=key.key_id,
                purpose=key.purpose,
                public_key_bytes=public_bytes,
                namespace=key.namespace,
                revoked=key.revoked,
            )
        )
    return store


def development_trust_document_bytes(document: DevelopmentTrustDocument) -> bytes:
    """Return canonical public trust-document bytes."""

    return (
        json.dumps(
            document.model_dump(mode="json"),
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        + b"\n"
    )


__all__ = [
    "DevelopmentSigningKey",
    "DevelopmentTrustDocument",
    "InvalidSignatureError",
    "KeyPurpose",
    "RevokedKeyError",
    "SignatureEnvelope",
    "SigningError",
    "TrustNamespace",
    "TrustNamespaceError",
    "TrustStore",
    "TrustedKey",
    "UnknownKeyError",
    "WrongPurposeError",
    "development_trust_bytes",
    "development_trust_document_bytes",
    "generate_development_keypair",
    "load_development_trust",
    "sign_bytes",
    "trusted_key_id",
    "verify_signature",
]
