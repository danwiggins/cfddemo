"""Offline Ed25519 trust tests for synthetic development records."""

from __future__ import annotations

import base64

import pytest

from traceback_runner.signing import (
    InvalidSignatureError,
    KeyPurpose,
    RevokedKeyError,
    SignatureEnvelope,
    TrustStore,
    UnknownKeyError,
    WrongPurposeError,
    development_trust_bytes,
    generate_development_keypair,
    load_development_trust,
    sign_bytes,
    verify_signature,
)


def test_ed25519_verifies_offline_with_independently_supplied_public_key() -> None:
    key = generate_development_keypair(KeyPurpose.RESULT)
    store = TrustStore()
    store.add_signing_key(key)
    content = b"canonical aggregate bytes"

    signature = sign_bytes(content, key, purpose=KeyPurpose.RESULT)

    verify_signature(content, signature, store, purpose=KeyPurpose.RESULT)


def test_public_development_trust_round_trips_without_private_key_material() -> None:
    key = generate_development_keypair(KeyPurpose.RESULT)
    content = development_trust_bytes(key)
    assert b"private" not in content

    restored = load_development_trust(content)
    signature = sign_bytes(b"record", key, purpose=KeyPurpose.RESULT)
    verify_signature(b"record", signature, restored, purpose=KeyPurpose.RESULT)

    with pytest.raises(ValueError, match="canonical"):
        load_development_trust(b" \n" + content)

    tampered = content.replace(key.key_id.encode(), b"dev-result-000000000000000000000000")
    with pytest.raises(ValueError, match="does not match"):
        load_development_trust(tampered)


def test_signature_rejects_tampering_wrong_key_and_missing_key() -> None:
    key = generate_development_keypair(KeyPurpose.RESULT)
    signature = sign_bytes(b"original", key, purpose=KeyPurpose.RESULT)
    correct_store = TrustStore()
    correct_store.add_signing_key(key)

    with pytest.raises(InvalidSignatureError):
        verify_signature(b"tampered", signature, correct_store, purpose=KeyPurpose.RESULT)

    wrong_key = generate_development_keypair(KeyPurpose.RESULT)
    wrong_store = TrustStore()
    wrong_store.add_signing_key(wrong_key)
    with pytest.raises(UnknownKeyError):
        verify_signature(b"original", signature, wrong_store, purpose=KeyPurpose.RESULT)

    with pytest.raises(UnknownKeyError):
        verify_signature(b"original", signature, TrustStore(), purpose=KeyPurpose.RESULT)


def test_release_and_result_key_purposes_are_not_interchangeable() -> None:
    release_key = generate_development_keypair(KeyPurpose.RELEASE)
    result_key = generate_development_keypair(KeyPurpose.RESULT)
    assert release_key.key_id != result_key.key_id

    with pytest.raises(WrongPurposeError):
        sign_bytes(b"record", release_key, purpose=KeyPurpose.RESULT)

    store = TrustStore()
    store.add_signing_key(result_key)
    signature = sign_bytes(b"record", result_key, purpose=KeyPurpose.RESULT)
    with pytest.raises(WrongPurposeError):
        verify_signature(b"record", signature, store, purpose=KeyPurpose.RELEASE)


def test_revoked_and_non_development_trust_fail_closed() -> None:
    key = generate_development_keypair(KeyPurpose.RESULT)
    store = TrustStore()
    store.add_signing_key(key)
    signature = sign_bytes(b"record", key, purpose=KeyPurpose.RESULT)
    store.revoke(key.key_id)
    with pytest.raises(RevokedKeyError):
        verify_signature(b"record", signature, store, purpose=KeyPurpose.RESULT)

    # A malformed future namespace cannot silently become development trust.
    with pytest.raises(ValueError):
        SignatureEnvelope.model_validate(
            {
                **signature.model_dump(mode="json"),
                "namespace": "production",
            }
        )


def test_malformed_signature_is_reported_as_invalid() -> None:
    key = generate_development_keypair(KeyPurpose.RESULT)
    store = TrustStore()
    store.add_signing_key(key)
    valid = sign_bytes(b"record", key, purpose=KeyPurpose.RESULT)
    malformed = valid.model_copy(
        update={"signature_base64": base64.b64encode(b"x" * 64).decode("ascii")}
    )
    with pytest.raises(InvalidSignatureError):
        verify_signature(b"record", malformed, store, purpose=KeyPurpose.RESULT)
