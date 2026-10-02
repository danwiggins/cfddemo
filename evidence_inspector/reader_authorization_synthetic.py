"""Checked-in synthetic provider authority for reader-authorization fixtures.

The private seeds below are public by construction.  Anyone can sign a grant
with them, which is why :mod:`evidence_inspector.reader_authorization_registry`
accepts this authority only in the ``synthetic`` profile and refuses its ID and
keys in the ``provider`` profile.  Nothing here authorizes real data.
"""

from __future__ import annotations

import base64
import hashlib
from datetime import datetime

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from evidence_inspector.reader_authorization_registry import (
    SYNTHETIC_READER_AUTHORITY_ID,
    MeasurementScope,
    ReaderAuthorityKey,
    ReaderAuthorizationProfile,
    ReaderGrantPayload,
    ReaderKeyStatus,
    ReaderProviderTrust,
    ReaderRole,
    SignedReaderGrant,
    reader_grant_payload_bytes,
    reader_trust_sha256,
)

_SEED_DOMAIN = b"traceback-synthetic-reader-authority-key-v1\0"
SYNTHETIC_KEY_VERSIONS = (1, 2)


def synthetic_private_key(key_version: int) -> Ed25519PrivateKey:
    if key_version not in SYNTHETIC_KEY_VERSIONS:
        raise ValueError("unknown synthetic reader key version")
    seed = hashlib.sha256(_SEED_DOMAIN + str(key_version).encode("ascii")).digest()
    return Ed25519PrivateKey.from_private_bytes(seed)


def synthetic_public_key_base64(key_version: int) -> str:
    public = synthetic_private_key(key_version).public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return base64.b64encode(public).decode("ascii")


def synthetic_reader_trust(
    *,
    revision: int = 1,
    previous: ReaderProviderTrust | None = None,
    statuses: dict[int, ReaderKeyStatus] | None = None,
) -> ReaderProviderTrust:
    """Return a synthetic trust revision; revision one lists key version 1 only."""

    statuses = statuses or {}
    versions = (1,) if revision == 1 else SYNTHETIC_KEY_VERSIONS
    return ReaderProviderTrust(
        profile=ReaderAuthorizationProfile.SYNTHETIC,
        authority_id=SYNTHETIC_READER_AUTHORITY_ID,
        revision=revision,
        previous_trust_sha256=(
            None if previous is None else reader_trust_sha256(previous)
        ),
        keys=tuple(
            ReaderAuthorityKey(
                key_version=version,
                public_key_base64=synthetic_public_key_base64(version),
                status=statuses.get(version, ReaderKeyStatus.ACTIVE),
            )
            for version in versions
        ),
    )


def sign_reader_grant(
    payload: ReaderGrantPayload, private_key: Ed25519PrivateKey
) -> SignedReaderGrant:
    signature = private_key.sign(reader_grant_payload_bytes(payload))
    return SignedReaderGrant(
        payload=payload,
        signature_base64=base64.b64encode(signature).decode("ascii"),
    )


def synthetic_reader_grant(
    *,
    registry_id: str,
    registry_epoch_sha256: str,
    grant_selector: str,
    cohort_registry_ids: tuple[str, ...],
    measurement_scopes: tuple[MeasurementScope, ...],
    issued_at: datetime,
    expires_at: datetime,
    key_version: int = 1,
) -> SignedReaderGrant:
    """Sign one synthetic ``longitudinal_reader`` grant with a checked-in key."""

    payload = ReaderGrantPayload(
        profile=ReaderAuthorizationProfile.SYNTHETIC,
        registry_id=registry_id,
        registry_epoch_sha256=registry_epoch_sha256,
        grant_selector=grant_selector,
        authority_id=SYNTHETIC_READER_AUTHORITY_ID,
        key_version=key_version,
        role=ReaderRole.LONGITUDINAL_READER,
        cohort_registry_ids=cohort_registry_ids,
        measurement_scopes=measurement_scopes,
        issued_at=issued_at,
        expires_at=expires_at,
    )
    return sign_reader_grant(payload, synthetic_private_key(key_version))


__all__ = [
    "SYNTHETIC_KEY_VERSIONS",
    "sign_reader_grant",
    "synthetic_private_key",
    "synthetic_public_key_base64",
    "synthetic_reader_grant",
    "synthetic_reader_trust",
]
