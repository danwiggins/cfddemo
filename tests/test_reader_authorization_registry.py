"""Protected reader-authorization registry: grants, fence, trust, persistence."""

from __future__ import annotations

import base64
import fcntl
import hashlib
import json
import os
import subprocess
import sys
import textwrap
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import ValidationError

import evidence_inspector.reader_authorization_registry as registry_module
from evidence_inspector.method_registry import MethodFamily
from evidence_inspector.provider_linkage_store import AuthorityTimeSource
from evidence_inspector.reader_authorization_registry import (
    MAX_GRANT_LIFETIME,
    SYNTHETIC_READER_AUTHORITY_ID,
    SYNTHETIC_READER_PUBLIC_KEYS,
    MeasurementScope,
    ReaderAuthorityKey,
    ReaderAuthorizationDenied,
    ReaderAuthorizationProfile,
    ReaderAuthorizationRegistry,
    ReaderAuthorizationRegistryConflict,
    ReaderAuthorizationRegistryUnsafe,
    ReaderDenialReason,
    ReaderGrantPayload,
    ReaderKeyStatus,
    ReaderProviderTrust,
    ReaderRevocationReason,
    ReaderRole,
    SignedReaderGrant,
    reader_registry_backup_from_bytes,
    reader_trust_sha256,
)
from evidence_inspector.reader_authorization_synthetic import (
    SYNTHETIC_KEY_VERSIONS,
    sign_reader_grant,
    synthetic_public_key_base64,
    synthetic_reader_grant,
    synthetic_reader_trust,
)
from tests import registry_storage_checks as storage_checks
from tests.forking import CHILD_RAISED, run_in_child, wait_child

NOW = datetime(2026, 9, 1, 12, tzinfo=UTC)
COHORT = "cohort_registry_" + "b" * 32
OTHER_COHORT = "cohort_registry_" + "c" * 32
SELECTOR = "reader_grant_" + "a" * 32
OTHER_SELECTOR = "reader_grant_" + "d" * 32
SCOPE = MeasurementScope(
    family=MethodFamily.FRAGMENT_MEASUREMENT,
    quantity_id="qty_short_fraction",
    unit="unit_fraction",
)
OTHER_SCOPE = MeasurementScope(
    family=MethodFamily.COPY_NUMBER,
    quantity_id="qty_tumor_fraction",
    unit="unit_fraction",
)
SYNTHETIC = ReaderAuthorizationProfile.SYNTHETIC
PROVIDER = ReaderAuthorizationProfile.PROVIDER


@pytest.fixture(autouse=True)
def fresh_profile_latch(monkeypatch: pytest.MonkeyPatch) -> None:
    # Each test models a separate run; the latch is process state.
    monkeypatch.setattr(registry_module, "_PROCESS_PROFILE", {})


def create_registry(
    root: Path,
    *,
    clock: AuthorityTimeSource | None = None,
    trust: ReaderProviderTrust | None = None,
    profile: ReaderAuthorizationProfile = SYNTHETIC,
) -> ReaderAuthorizationRegistry:
    trust = trust or synthetic_reader_trust()
    return ReaderAuthorizationRegistry.create(
        root,
        profile=profile,
        configured_trust=trust,
        expected_trust_sha256=reader_trust_sha256(trust),
        time_source=clock or AuthorityTimeSource.fixed(NOW),
    )


def grant_for(
    registry: ReaderAuthorizationRegistry,
    *,
    selector: str = SELECTOR,
    issued_at: datetime = NOW - timedelta(hours=1),
    expires_at: datetime = NOW + timedelta(days=1),
    key_version: int = 1,
    cohorts: tuple[str, ...] = (COHORT,),
    scopes: tuple[MeasurementScope, ...] = (SCOPE,),
) -> SignedReaderGrant:
    identity = registry.identity()
    return synthetic_reader_grant(
        registry_id=identity.registry_id,
        registry_epoch_sha256=identity.registry_epoch_sha256,
        grant_selector=selector,
        cohort_registry_ids=cohorts,
        measurement_scopes=scopes,
        issued_at=issued_at,
        expires_at=expires_at,
        key_version=key_version,
    )


def authorize(
    registry: ReaderAuthorizationRegistry,
    binding,
    *,
    cohort: str = COHORT,
    scope: MeasurementScope = SCOPE,
):
    with registry.authority_read_fence():
        return registry.authorize_reader_in_fence(
            binding.grant_sha256,
            expected_state_head_sha256=binding.state_head_sha256,
            cohort_registry_id=cohort,
            measurement_scope=scope,
        )


def bind(registry: ReaderAuthorizationRegistry, selector: str = SELECTOR):
    with registry.authority_read_fence():
        return registry.bind_grant_in_fence(selector)


def reopen_kwargs(registry: ReaderAuthorizationRegistry, trust=None, clock=None):
    identity = registry.identity()
    trust = trust or synthetic_reader_trust()
    return {
        "profile": identity.profile,
        "configured_trust": trust,
        "expected_trust_sha256": reader_trust_sha256(trust),
        "expected_registry_id": identity.registry_id,
        "expected_registry_epoch_sha256": identity.registry_epoch_sha256,
        "expected_state_head_sha256": identity.state_head_sha256,
        "time_source": clock or AuthorityTimeSource.fixed(NOW),
    }


def denied(reason: ReaderDenialReason):
    class _Match:
        def __enter__(self):
            self.ctx = pytest.raises(ReaderAuthorizationDenied)
            self.info = self.ctx.__enter__()
            return self.info

        def __exit__(self, *exc):
            result = self.ctx.__exit__(*exc)
            assert self.info.value.reason is reason
            assert str(self.info.value) == "permission_denied"
            assert self.info.value.code == "permission_denied"
            return result

    return _Match()


def provider_key() -> tuple[Ed25519PrivateKey, str]:
    key = Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
    )
    return key, base64.b64encode(public).decode("ascii")


def provider_trust(public: str) -> ReaderProviderTrust:
    return ReaderProviderTrust(
        profile=PROVIDER,
        authority_id="reader_authority_" + "e" * 32,
        revision=1,
        previous_trust_sha256=None,
        keys=(
            ReaderAuthorityKey(
                key_version=1, public_key_base64=public, status=ReaderKeyStatus.ACTIVE
            ),
        ),
    )


@pytest.fixture
def clock() -> AuthorityTimeSource:
    return AuthorityTimeSource.fixed(NOW)


@pytest.fixture
def registry(tmp_path: Path, clock: AuthorityTimeSource):
    created = create_registry(tmp_path / "reader", clock=clock)
    try:
        yield created
    finally:
        created.close()


# --- synthetic authority and contracts -------------------------------------


def test_checked_in_synthetic_keys_match_the_registry_constants() -> None:
    assert SYNTHETIC_KEY_VERSIONS == tuple(SYNTHETIC_READER_PUBLIC_KEYS)
    for version in SYNTHETIC_KEY_VERSIONS:
        assert synthetic_public_key_base64(version) == (
            SYNTHETIC_READER_PUBLIC_KEYS[version]
        )


def test_synthetic_authority_is_refused_by_the_provider_profile() -> None:
    synthetic = synthetic_reader_trust()
    with pytest.raises(ValidationError, match="synthetic authority"):
        ReaderProviderTrust(
            **{**synthetic.model_dump(), "profile": PROVIDER},
        )
    _, public = provider_key()
    with pytest.raises(ValidationError, match="synthetic authority"):
        ReaderProviderTrust(
            profile=PROVIDER,
            authority_id="reader_authority_" + "e" * 32,
            revision=1,
            previous_trust_sha256=None,
            keys=(
                ReaderAuthorityKey(
                    key_version=1,
                    public_key_base64=public,
                    status=ReaderKeyStatus.ACTIVE,
                ),
                ReaderAuthorityKey(
                    key_version=2,
                    public_key_base64=SYNTHETIC_READER_PUBLIC_KEYS[1],
                    status=ReaderKeyStatus.ACTIVE,
                ),
            ),
        )
    with pytest.raises(ValidationError, match="checked-in synthetic"):
        provider = provider_trust(public)
        ReaderProviderTrust(**{**provider.model_dump(), "profile": SYNTHETIC})


def test_synthetic_grant_cannot_authorize_a_provider_registry(tmp_path: Path) -> None:
    private, public = provider_key()
    trust = provider_trust(public)
    registry = create_registry(tmp_path / "provider", trust=trust, profile=PROVIDER)
    try:
        identity = registry.identity()
        synthetic = synthetic_reader_grant(
            registry_id=identity.registry_id,
            registry_epoch_sha256=identity.registry_epoch_sha256,
            grant_selector=SELECTOR,
            cohort_registry_ids=(COHORT,),
            measurement_scopes=(SCOPE,),
            issued_at=NOW - timedelta(hours=1),
            expires_at=NOW + timedelta(days=1),
        )
        with pytest.raises(ReaderAuthorizationRegistryConflict):
            registry.add_grant(synthetic)
        # The same payload relabelled as provider and signed by the public
        # synthetic seed still fails: the provider trust holds no such key.
        relabelled = sign_reader_grant(
            synthetic.payload.model_copy(
                update={
                    "profile": PROVIDER,
                    "authority_id": trust.authority_id,
                }
            ),
            registry_module_synthetic_key(),
        )
        with pytest.raises(ReaderAuthorizationRegistryConflict, match="trusted"):
            registry.add_grant(relabelled)
        real = sign_reader_grant(
            synthetic.payload.model_copy(
                update={"profile": PROVIDER, "authority_id": trust.authority_id}
            ),
            private,
        )
        receipt = registry.add_grant(real)
        binding = bind(registry)
        assert binding.grant_sha256 == receipt.grant_sha256
        assert authorize(registry, binding).synthetic_only is False
    finally:
        registry.close()


def registry_module_synthetic_key() -> Ed25519PrivateKey:
    from evidence_inspector.reader_authorization_synthetic import (
        synthetic_private_key,
    )

    return synthetic_private_key(1)


def test_one_run_cannot_open_two_profiles(tmp_path: Path) -> None:
    synthetic = create_registry(tmp_path / "synthetic")
    synthetic.close()
    _, public = provider_key()
    with pytest.raises(ReaderAuthorizationRegistryUnsafe, match="profile"):
        create_registry(tmp_path / "provider", trust=provider_trust(public), profile=PROVIDER)
    assert not (tmp_path / "provider").exists()


@pytest.mark.parametrize(
    "update",
    (
        {"role": "admin"},
        {"role": "*"},
        {"cohort_registry_ids": ("*",)},
        {"cohort_registry_ids": ()},
        {"measurement_scopes": ()},
        {"cohort_registry_ids": (OTHER_COHORT, COHORT)},
        {"expires_at": NOW - timedelta(hours=2)},
        {"expires_at": NOW + MAX_GRANT_LIFETIME},
        {"issued_at": NOW.replace(microsecond=5)},
        {"cohort_registry_ids": tuple(
            f"cohort_registry_{index:032x}" for index in range(17)
        )},
    ),
)
def test_grant_contract_has_no_wildcard_or_open_scope(update: dict) -> None:
    payload = {
        "profile": SYNTHETIC,
        "registry_id": "reader_registry_" + "0" * 32,
        "registry_epoch_sha256": "1" * 64,
        "grant_selector": SELECTOR,
        "authority_id": SYNTHETIC_READER_AUTHORITY_ID,
        "key_version": 1,
        "role": ReaderRole.LONGITUDINAL_READER,
        "cohort_registry_ids": (COHORT,),
        "measurement_scopes": (SCOPE,),
        "issued_at": NOW - timedelta(hours=1),
        "expires_at": NOW + timedelta(days=1),
    }
    ReaderGrantPayload(**payload)
    with pytest.raises(ValidationError):
        ReaderGrantPayload(**{**payload, **update})


# --- grant admission ---------------------------------------------------------


def test_grant_roundtrip_binds_only_commitment_and_head(registry) -> None:
    receipt = registry.add_grant(grant_for(registry))
    binding = bind(registry)
    assert set(binding.model_dump()) == {"grant_sha256", "state_head_sha256"}
    assert binding.grant_sha256 == receipt.grant_sha256
    assert binding.state_head_sha256 == receipt.state_head_sha256
    authorization = authorize(registry, binding)
    assert authorization.role is ReaderRole.LONGITUDINAL_READER
    assert authorization.cohort_registry_id == COHORT
    assert authorization.measurement_scope == SCOPE
    assert authorization.synthetic_only is True
    assert authorization.evaluated_at == NOW


def test_forged_and_misbound_grants_are_never_admitted(registry) -> None:
    good = grant_for(registry)
    tampered = SignedReaderGrant(
        payload=good.payload.model_copy(
            update={"cohort_registry_ids": (COHORT, OTHER_COHORT)}
        ),
        signature_base64=good.signature_base64,
    )
    forger, _ = provider_key()
    forged = sign_reader_grant(good.payload, forger)
    other_registry = sign_reader_grant(
        good.payload.model_copy(update={"registry_epoch_sha256": "f" * 64}),
        registry_module_synthetic_key(),
    )
    unknown_key = synthetic_reader_grant(
        registry_id=good.payload.registry_id,
        registry_epoch_sha256=good.payload.registry_epoch_sha256,
        grant_selector=SELECTOR,
        cohort_registry_ids=(COHORT,),
        measurement_scopes=(SCOPE,),
        issued_at=NOW - timedelta(hours=1),
        expires_at=NOW + timedelta(days=1),
        key_version=2,
    )
    for grant in (tampered, forged, other_registry, unknown_key):
        with pytest.raises(ReaderAuthorizationRegistryConflict):
            registry.add_grant(grant)
    assert registry.identity().state_version == 1


def test_not_yet_valid_or_expired_grants_are_not_admitted(registry) -> None:
    with pytest.raises(ReaderAuthorizationRegistryConflict):
        registry.add_grant(
            grant_for(registry, issued_at=NOW + timedelta(seconds=1))
        )
    with pytest.raises(ReaderAuthorizationRegistryConflict):
        registry.add_grant(
            grant_for(
                registry,
                issued_at=NOW - timedelta(days=1),
                expires_at=NOW,
            )
        )
    registry.add_grant(grant_for(registry, issued_at=NOW))


def test_duplicate_selector_and_subclass_inputs_are_rejected(registry) -> None:
    registry.add_grant(grant_for(registry))
    with pytest.raises(ReaderAuthorizationRegistryConflict):
        registry.add_grant(grant_for(registry))
    with pytest.raises(ReaderAuthorizationRegistryConflict):
        registry.add_grant(grant_for(registry, expires_at=NOW + timedelta(days=2)))

    class Shadow(SignedReaderGrant):
        pass

    good = grant_for(registry, selector=OTHER_SELECTOR)
    with pytest.raises(ReaderAuthorizationRegistryConflict, match="exact contract"):
        registry.add_grant(Shadow(**good.model_dump()))
    private = good.model_copy()
    object.__setattr__(private, "__pydantic_private__", {"hidden": "role"})
    with pytest.raises(ReaderAuthorizationRegistryConflict, match="exact contract"):
        registry.add_grant(private)


def test_grant_count_is_bounded(registry, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(registry_module, "MAX_GRANTS", 2)
    registry.add_grant(grant_for(registry))
    registry.add_grant(grant_for(registry, selector=OTHER_SELECTOR))
    with pytest.raises(ReaderAuthorizationRegistryConflict, match="admissible"):
        registry.add_grant(grant_for(registry, selector="reader_grant_" + "9" * 32))


# --- resolution denials ------------------------------------------------------


def test_expiry_edges_are_exact(registry, clock: AuthorityTimeSource) -> None:
    registry.add_grant(grant_for(registry, expires_at=NOW + timedelta(hours=1)))
    binding = bind(registry)
    clock.advance_to(NOW + timedelta(hours=1) - timedelta(seconds=1))
    authorize(registry, binding)
    clock.advance_to(NOW + timedelta(hours=1))
    with denied(ReaderDenialReason.GRANT_NOT_CURRENT):
        authorize(registry, binding)


def test_revoked_grant_is_denied_and_cannot_be_revoked_twice(
    registry, clock: AuthorityTimeSource
) -> None:
    registry.add_grant(grant_for(registry))
    registry.add_grant(grant_for(registry, selector=OTHER_SELECTOR))
    receipt = registry.revoke_grant(
        SELECTOR, reason=ReaderRevocationReason.OPERATOR_REQUEST
    )
    with denied(ReaderDenialReason.GRANT_REVOKED):
        bind(registry)
    binding = bind(registry, OTHER_SELECTOR)
    assert binding.state_head_sha256 == receipt.state_head_sha256
    authorize(registry, binding)
    clock.advance_to(NOW + timedelta(seconds=5))
    with pytest.raises(ReaderAuthorizationRegistryConflict, match="admissible"):
        registry.revoke_grant(SELECTOR, reason=ReaderRevocationReason.KEY_COMPROMISE)
    with pytest.raises(ReaderAuthorizationRegistryConflict, match="not registered"):
        registry.revoke_grant(
            "reader_grant_" + "7" * 32, reason=ReaderRevocationReason.PROVIDER_REQUEST
        )


def test_wrong_scope_is_denied(registry) -> None:
    registry.add_grant(grant_for(registry))
    binding = bind(registry)
    with denied(ReaderDenialReason.SCOPE_MISMATCH):
        authorize(registry, binding, cohort=OTHER_COHORT)
    with denied(ReaderDenialReason.SCOPE_MISMATCH):
        authorize(registry, binding, scope=OTHER_SCOPE)
    with denied(ReaderDenialReason.SCOPE_MISMATCH):
        authorize(registry, binding, cohort="*")


def test_unrelated_registry_changes_do_not_end_a_bound_session(registry) -> None:
    registry.add_grant(grant_for(registry))
    binding = bind(registry)
    # Another grant, its revocation, and a trust revision that only adds a key
    # all move the head; none of them touches this grant or its signing key.
    registry.add_grant(grant_for(registry, selector=OTHER_SELECTOR))
    authorize(registry, binding)
    registry.revoke_grant(
        OTHER_SELECTOR, reason=ReaderRevocationReason.OPERATOR_REQUEST
    )
    authorize(registry, binding)
    first = synthetic_reader_trust()
    second = synthetic_reader_trust(revision=2, previous=first)
    registry.rotate_trust(second, expected_trust_sha256=reader_trust_sha256(second))
    authorization = authorize(registry, binding)
    assert authorization.state_head_sha256 == registry.identity().state_head_sha256
    assert authorization.state_head_sha256 != binding.state_head_sha256
    # Its own revocation ends it.
    registry.revoke_grant(SELECTOR, reason=ReaderRevocationReason.OPERATOR_REQUEST)
    with denied(ReaderDenialReason.GRANT_REVOKED):
        authorize(registry, binding)


def test_rotating_out_the_bound_grants_key_ends_the_session(registry) -> None:
    registry.add_grant(grant_for(registry))
    binding = bind(registry)
    first = synthetic_reader_trust()
    second = synthetic_reader_trust(revision=2, previous=first)
    registry.rotate_trust(second, expected_trust_sha256=reader_trust_sha256(second))
    authorize(registry, binding)
    third = synthetic_reader_trust(
        revision=3, previous=second, statuses={1: ReaderKeyStatus.REVOKED}
    )
    registry.rotate_trust(third, expected_trust_sha256=reader_trust_sha256(third))
    with denied(ReaderDenialReason.UNTRUSTED_KEY):
        authorize(registry, binding)


def test_a_head_outside_the_committed_chain_is_stale(registry) -> None:
    registry.add_grant(grant_for(registry))
    binding = bind(registry)
    with denied(ReaderDenialReason.STALE_HEAD):
        authorize(
            registry, binding.model_copy(update={"state_head_sha256": "0" * 64})
        )


def test_grant_states_list_selectors_and_states_only(
    registry, clock: AuthorityTimeSource
) -> None:
    from evidence_inspector.reader_authorization_registry import (
        ReaderGrantListing,
        ReaderGrantState,
    )

    assert registry.grant_states() == ()
    registry.add_grant(grant_for(registry))
    registry.add_grant(
        grant_for(registry, selector=OTHER_SELECTOR, expires_at=NOW + timedelta(hours=1))
    )
    registry.revoke_grant(SELECTOR, reason=ReaderRevocationReason.OPERATOR_REQUEST)
    assert set(ReaderGrantListing.model_fields) == {"grant_selector", "state"}
    assert registry.grant_states() == (
        ReaderGrantListing(grant_selector=SELECTOR, state=ReaderGrantState.REVOKED),
        ReaderGrantListing(
            grant_selector=OTHER_SELECTOR, state=ReaderGrantState.ACTIVE
        ),
    )
    clock.advance_to(NOW + timedelta(hours=1))
    assert registry.grant_states()[1].state is ReaderGrantState.EXPIRED


def test_missing_grant_is_denied(registry) -> None:
    registry.add_grant(grant_for(registry))
    binding = bind(registry)
    with denied(ReaderDenialReason.GRANT_MISSING):
        authorize(registry, binding.model_copy(update={"grant_sha256": "0" * 64}))
    with denied(ReaderDenialReason.GRANT_MISSING):
        bind(registry, OTHER_SELECTOR)
    with denied(ReaderDenialReason.GRANT_MISSING):
        bind(registry, "longitudinal_reader")


def test_key_rotation_revoking_the_signing_key_denies_its_grants(registry) -> None:
    registry.add_grant(grant_for(registry))
    first = synthetic_reader_trust()
    second = synthetic_reader_trust(revision=2, previous=first)
    registry.rotate_trust(second, expected_trust_sha256=reader_trust_sha256(second))
    registry.add_grant(grant_for(registry, selector=OTHER_SELECTOR, key_version=2))
    authorize(registry, bind(registry))
    third = synthetic_reader_trust(
        revision=3, previous=second, statuses={1: ReaderKeyStatus.REVOKED}
    )
    registry.rotate_trust(third, expected_trust_sha256=reader_trust_sha256(third))
    with denied(ReaderDenialReason.UNTRUSTED_KEY):
        bind(registry)
    authorize(registry, bind(registry, OTHER_SELECTOR))
    with pytest.raises(ReaderAuthorizationRegistryConflict, match="trusted"):
        registry.add_grant(grant_for(registry, selector="reader_grant_" + "8" * 32))


@pytest.mark.parametrize("case", ("drop", "reactivate", "previous", "pin", "skip"))
def test_trust_rotation_must_extend_the_current_trust(registry, case: str) -> None:
    first = synthetic_reader_trust()
    second = synthetic_reader_trust(revision=2, previous=first)
    registry.rotate_trust(second, expected_trust_sha256=reader_trust_sha256(second))
    revoked = synthetic_reader_trust(
        revision=3, previous=second, statuses={1: ReaderKeyStatus.REVOKED}
    )
    registry.rotate_trust(revoked, expected_trust_sha256=reader_trust_sha256(revoked))
    base = {
        "profile": SYNTHETIC,
        "authority_id": SYNTHETIC_READER_AUTHORITY_ID,
        "revision": 4,
        "previous_trust_sha256": reader_trust_sha256(revoked),
        "keys": revoked.keys,
    }
    candidate = {
        "drop": {**base, "keys": revoked.keys[1:]},
        "reactivate": {
            **base,
            "keys": tuple(
                key.model_copy(update={"status": ReaderKeyStatus.ACTIVE})
                for key in revoked.keys
            ),
        },
        "previous": {**base, "previous_trust_sha256": reader_trust_sha256(first)},
        "pin": base,
        "skip": {**base, "revision": 5},
    }[case]
    trust = ReaderProviderTrust(**candidate)
    pin = "0" * 64 if case == "pin" else reader_trust_sha256(trust)
    with pytest.raises(ReaderAuthorizationRegistryConflict):
        registry.rotate_trust(trust, expected_trust_sha256=pin)


def test_clock_rollback_below_the_last_record_is_denied(
    registry, clock: AuthorityTimeSource
) -> None:
    registry.add_grant(grant_for(registry))
    binding = bind(registry)
    kwargs = reopen_kwargs(
        registry, clock=AuthorityTimeSource.fixed(NOW - timedelta(seconds=1))
    )
    peer = ReaderAuthorizationRegistry(registry.root, **kwargs)
    try:
        with denied(ReaderDenialReason.CLOCK_ROLLBACK):
            authorize(peer, binding)
    finally:
        peer.close()


# --- the fence ---------------------------------------------------------------


def test_in_fence_reads_require_the_fence_and_it_is_not_reentrant(registry) -> None:
    registry.add_grant(grant_for(registry))
    with pytest.raises(ReaderAuthorizationRegistryUnsafe, match="fence is absent"):
        registry.bind_grant_in_fence(SELECTOR)
    binding = bind(registry)
    with pytest.raises(ReaderAuthorizationRegistryUnsafe, match="fence is absent"):
        registry.authorize_reader_in_fence(
            binding.grant_sha256,
            expected_state_head_sha256=binding.state_head_sha256,
            cohort_registry_id=COHORT,
            measurement_scope=SCOPE,
        )
    with registry.authority_read_fence():
        with pytest.raises(ReaderAuthorizationRegistryUnsafe, match="already held"):
            with registry.authority_read_fence():
                pass
        with pytest.raises(ReaderAuthorizationRegistryUnsafe, match="already held"):
            registry.revoke_grant(
                SELECTOR, reason=ReaderRevocationReason.OPERATOR_REQUEST
            )
    authorize(registry, binding)


_REVOKER = textwrap.dedent(
    """
    import json, sys
    from datetime import datetime
    from evidence_inspector.provider_linkage_store import AuthorityTimeSource
    from evidence_inspector.reader_authorization_registry import (
        ReaderAuthorizationProfile, ReaderAuthorizationRegistry,
        ReaderRevocationReason,
    )
    from evidence_inspector.reader_authorization_synthetic import (
        synthetic_reader_trust,
    )
    from evidence_inspector.reader_authorization_registry import reader_trust_sha256
    args = json.loads(sys.argv[1])
    trust = synthetic_reader_trust()
    registry = ReaderAuthorizationRegistry(
        args["root"],
        profile=ReaderAuthorizationProfile.SYNTHETIC,
        configured_trust=trust,
        expected_trust_sha256=reader_trust_sha256(trust),
        expected_registry_id=args["registry_id"],
        expected_registry_epoch_sha256=args["epoch"],
        expected_state_head_sha256=args["head"],
        time_source=AuthorityTimeSource.fixed(datetime.fromisoformat(args["now"])),
    )
    print("opened", flush=True)
    registry.revoke_grant(
        args["selector"], reason=ReaderRevocationReason.PROVIDER_REQUEST
    )
    print("revoked", flush=True)
    """
)


def test_cross_process_revocation_waits_for_the_read_fence(registry) -> None:
    registry.add_grant(grant_for(registry))
    binding = bind(registry)
    identity = registry.identity()
    args = json.dumps(
        {
            "root": str(registry.root),
            "registry_id": identity.registry_id,
            "epoch": identity.registry_epoch_sha256,
            "head": identity.state_head_sha256,
            "now": NOW.isoformat(),
            "selector": SELECTOR,
        }
    )
    with registry.authority_read_fence():
        first = registry.authorize_reader_in_fence(
            binding.grant_sha256,
            expected_state_head_sha256=binding.state_head_sha256,
            cohort_registry_id=COHORT,
            measurement_scope=SCOPE,
        )
        process = subprocess.Popen(
            [sys.executable, "-c", _REVOKER, args],
            cwd=Path(__file__).resolve().parents[1],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            # The peer cannot even open (its startup takes the exclusive lock),
            # let alone revoke, while this fence is held.
            time.sleep(1.5)
            assert process.poll() is None
            final = registry.authorize_reader_in_fence(
                binding.grant_sha256,
                expected_state_head_sha256=binding.state_head_sha256,
                cohort_registry_id=COHORT,
                measurement_scope=SCOPE,
            )
            assert final == first
        except BaseException:
            process.kill()
            raise
    stdout, stderr = process.communicate(timeout=60)
    assert process.returncode == 0, stderr
    assert stdout.split() == ["opened", "revoked"]
    with denied(ReaderDenialReason.GRANT_REVOKED):
        authorize(registry, binding)
    with denied(ReaderDenialReason.GRANT_REVOKED):
        bind(registry)


def test_cross_process_open_peer_revocation_also_waits(registry, tmp_path) -> None:
    registry.add_grant(grant_for(registry))
    binding = bind(registry)
    identity = registry.identity()
    script = _REVOKER.replace(
        'print("opened", flush=True)',
        'print("opened", flush=True)\nsys.stdin.readline()',
    )
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            script,
            json.dumps(
                {
                    "root": str(registry.root),
                    "registry_id": identity.registry_id,
                    "epoch": identity.registry_epoch_sha256,
                    "head": identity.state_head_sha256,
                    "now": NOW.isoformat(),
                    "selector": SELECTOR,
                }
            ),
        ],
        cwd=Path(__file__).resolve().parents[1],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout is not None and process.stdin is not None
        assert process.stdout.readline().strip() == "opened"
        with registry.authority_read_fence():
            process.stdin.write("go\n")
            process.stdin.flush()
            time.sleep(1.5)
            assert process.poll() is None
            registry.authorize_reader_in_fence(
                binding.grant_sha256,
                expected_state_head_sha256=binding.state_head_sha256,
                cohort_registry_id=COHORT,
                measurement_scope=SCOPE,
            )
        stdout, stderr = process.communicate(timeout=60)
    except BaseException:
        process.kill()
        raise
    assert process.returncode == 0, stderr
    assert stdout.strip() == "revoked"
    with denied(ReaderDenialReason.GRANT_REVOKED):
        bind(registry)


# --- startup pins, trust, rollback ------------------------------------------


@pytest.mark.parametrize(
    "field", ("expected_registry_id", "expected_registry_epoch_sha256",
              "expected_state_head_sha256", "expected_trust_sha256")
)
def test_reopen_requires_every_exact_startup_pin(registry, field: str) -> None:
    kwargs = reopen_kwargs(registry)
    ReaderAuthorizationRegistry(registry.root, **kwargs).close()
    wrong = "reader_registry_" + "0" * 32 if field == "expected_registry_id" else "0" * 64
    with pytest.raises(ReaderAuthorizationRegistryUnsafe):
        ReaderAuthorizationRegistry(registry.root, **{**kwargs, field: wrong})
    with pytest.raises(ReaderAuthorizationRegistryUnsafe):
        ReaderAuthorizationRegistry(registry.root, **{**kwargs, field: None})


def test_missing_registry_does_not_bootstrap(tmp_path: Path, registry) -> None:
    kwargs = reopen_kwargs(registry)
    with pytest.raises(ReaderAuthorizationRegistryUnsafe, match="missing"):
        ReaderAuthorizationRegistry(tmp_path / "absent", **kwargs)
    assert not (tmp_path / "absent").exists()
    with pytest.raises(ReaderAuthorizationRegistryConflict, match="exists"):
        create_registry(registry.root)


def test_configured_trust_must_be_the_current_trust(registry) -> None:
    first = synthetic_reader_trust()
    second = synthetic_reader_trust(revision=2, previous=first)
    registry.rotate_trust(second, expected_trust_sha256=reader_trust_sha256(second))
    with pytest.raises(ReaderAuthorizationRegistryUnsafe, match="trust"):
        ReaderAuthorizationRegistry(registry.root, **reopen_kwargs(registry))
    ReaderAuthorizationRegistry(
        registry.root, **reopen_kwargs(registry, trust=second)
    ).close()


def test_peer_rejects_rollback_to_an_earlier_head(registry) -> None:
    peer = ReaderAuthorizationRegistry(registry.root, **reopen_kwargs(registry))
    journal = registry.root / "registry-journal.jsonl"
    before = journal.read_bytes()
    try:
        registry.add_grant(grant_for(registry))
        peer.identity()
        journal.write_bytes(before)
        with pytest.raises(ReaderAuthorizationRegistryUnsafe, match="rollback"):
            peer.identity()
    finally:
        peer.close()


def test_registry_root_replacement_fails_closed(registry, tmp_path: Path) -> None:
    registry.add_grant(grant_for(registry))
    binding = bind(registry)
    moved = tmp_path / "moved"
    os.rename(registry.root, moved)
    replacement = create_registry(registry.root)
    try:
        with pytest.raises(ReaderAuthorizationRegistryUnsafe, match="storage"):
            authorize(registry, binding)
        replacement.add_grant(grant_for(replacement))
        # A same-selector grant in the replacement is a different commitment
        # under a different head, so the old binding never resolves there.
        with denied(ReaderDenialReason.STALE_HEAD):
            authorize(replacement, binding)
    finally:
        replacement.close()


# --- storage ----------------------------------------------------------------


def test_object_tamper_and_extra_objects_fail_closed(registry) -> None:
    registry.add_grant(grant_for(registry))
    objects = registry.root / "objects"
    (objects / ("f" * 64 + ".json")).write_bytes(b"{}")
    (objects / ("f" * 64 + ".json")).chmod(0o600)
    (objects / ("e" * 64 + ".json")).write_bytes(b"{}")
    with pytest.raises(ReaderAuthorizationRegistryUnsafe, match="inconsistent"):
        registry.identity()
    (objects / ("e" * 64 + ".json")).unlink()
    registry.identity()
    target = next(
        path for path in objects.iterdir() if b'"grant"' in path.read_bytes()
        and b'"kind":"grant"' in path.read_bytes()
    )
    content = target.read_bytes()
    target.write_bytes(content.replace(COHORT.encode(), OTHER_COHORT.encode()))
    with pytest.raises(ReaderAuthorizationRegistryUnsafe, match="digest"):
        registry.identity()


def test_interrupted_append_remnant_is_removed_on_next_mutation(registry) -> None:
    registry.add_grant(grant_for(registry))
    forged = registry.root / "objects" / ("f" * 64 + ".json")
    forged.write_bytes(b"{}")
    forged.chmod(0o600)
    with pytest.raises(ReaderAuthorizationRegistryUnsafe, match="digest"):
        registry.add_grant(grant_for(registry, selector=OTHER_SELECTOR))
    forged.unlink()
    orphan = registry.root / "objects" / (hashlib.sha256(b"{}").hexdigest() + ".json")
    orphan.write_bytes(b"{}")
    orphan.chmod(0o600)
    receipt = registry.add_grant(grant_for(registry, selector=OTHER_SELECTOR))
    assert receipt.state_version == 3
    assert not orphan.exists()
    kwargs = reopen_kwargs(registry)
    remnant = registry.root / "objects" / (".tmp-" + "a" * 32)
    remnant.write_bytes(b"partial")
    ReaderAuthorizationRegistry(registry.root, **kwargs).close()
    assert not remnant.exists()


def test_torn_journal_append_is_truncated_and_retries(
    registry, monkeypatch: pytest.MonkeyPatch
) -> None:
    journal = registry.root / "registry-journal.jsonl"
    committed = journal.read_bytes()
    original_write = os.write

    def torn_write(descriptor: int, content) -> int:
        data = bytes(content)
        if data.endswith(b"\n") and b"reader-registry-journal-entry" in data:
            original_write(descriptor, data[: len(data) // 2])
            raise OSError("disk full")
        return original_write(descriptor, content)

    monkeypatch.setattr(os, "write", torn_write)
    with pytest.raises(ReaderAuthorizationRegistryUnsafe, match="append failed"):
        registry.add_grant(grant_for(registry))
    monkeypatch.undo()
    assert journal.read_bytes() == committed
    assert registry.identity().state_version == 1
    assert registry.add_grant(grant_for(registry)).state_version == 2


@pytest.mark.parametrize("name", (".registry.lock", "registry-metadata.json"))
def test_bound_control_file_substitution_fails_closed(registry, name: str) -> None:
    path = registry.root / name
    bound = registry.root / f"{name}.bound"
    os.replace(path, bound)
    path.write_bytes(bound.read_bytes())
    path.chmod(0o600)
    try:
        with pytest.raises(ReaderAuthorizationRegistryUnsafe, match="storage"):
            registry.identity()
    finally:
        path.unlink()
        os.replace(bound, path)


def test_backup_restore_preserves_identity_and_grants(registry, tmp_path) -> None:
    registry.add_grant(grant_for(registry))
    binding = bind(registry)
    backup = registry.backup_bytes()
    kwargs = reopen_kwargs(registry)
    restored = ReaderAuthorizationRegistry.restore(tmp_path / "restored", backup, **kwargs)
    try:
        assert restored.identity() == registry.identity()
        authorize(restored, binding)
    finally:
        restored.close()
    with pytest.raises(ReaderAuthorizationRegistryConflict, match="expected head"):
        ReaderAuthorizationRegistry.restore(
            tmp_path / "wrong", backup, **{**kwargs, "expected_state_head_sha256": "0" * 64}
        )
    assert not (tmp_path / "wrong").exists()


def test_tampered_backup_rejects_before_creating_a_target(registry, tmp_path) -> None:
    registry.add_grant(grant_for(registry))
    backup = registry.backup_bytes()
    tampered = backup.replace(COHORT.encode(), OTHER_COHORT.encode())
    with pytest.raises(ReaderAuthorizationRegistryConflict):
        reader_registry_backup_from_bytes(tampered)
    with pytest.raises(ReaderAuthorizationRegistryConflict):
        ReaderAuthorizationRegistry.restore(
            tmp_path / "tampered", tampered, **reopen_kwargs(registry)
        )
    assert not (tmp_path / "tampered").exists()
    with pytest.raises(ReaderAuthorizationRegistryConflict):
        reader_registry_backup_from_bytes(b"x" * (registry_module.MAX_BACKUP_BYTES + 1))


def test_failed_restore_reopen_removes_the_target_and_can_retry(
    registry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry.add_grant(grant_for(registry))
    backup = registry.backup_bytes()
    kwargs = reopen_kwargs(registry)
    target = tmp_path / "reopen"
    restored_lock = target / ".registry.lock"
    original_flock = fcntl.flock

    def failing_flock(descriptor, operation):
        if restored_lock.exists():
            lock = restored_lock.stat()
            bound = os.fstat(descriptor)
            if (bound.st_dev, bound.st_ino) == (lock.st_dev, lock.st_ino):
                raise OSError("lock unavailable")
        return original_flock(descriptor, operation)

    monkeypatch.setattr(fcntl, "flock", failing_flock)
    with pytest.raises(ReaderAuthorizationRegistryUnsafe):
        ReaderAuthorizationRegistry.restore(target, backup, **kwargs)
    monkeypatch.undo()
    assert not target.exists()
    ReaderAuthorizationRegistry.restore(target, backup, **kwargs).close()


def test_failed_restore_publication_removes_the_partial_target(
    registry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backup = registry.backup_bytes()
    kwargs = reopen_kwargs(registry)
    original_link = os.link

    def failing_link(source, destination, *args, **kw):
        if destination == "registry-journal.jsonl":
            raise OSError("disk full")
        return original_link(source, destination, *args, **kw)

    monkeypatch.setattr(os, "link", failing_link)
    with pytest.raises(ReaderAuthorizationRegistryUnsafe, match="failed"):
        ReaderAuthorizationRegistry.restore(tmp_path / "partial", backup, **kwargs)
    monkeypatch.undo()
    assert not (tmp_path / "partial").exists()


# --- seals and privacy -------------------------------------------------------


def test_instance_and_class_callable_shadows_are_rejected(
    registry, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry.add_grant(grant_for(registry))
    for name in ("authorize_reader_in_fence", "bind_grant_in_fence", "revoke_grant"):
        object.__getattribute__(registry, "__dict__")[name] = lambda *a, **k: None
        with pytest.raises(ReaderAuthorizationRegistryUnsafe, match="callable"):
            getattr(registry, name)
        del object.__getattribute__(registry, "__dict__")[name]
    monkeypatch.setattr(
        ReaderAuthorizationRegistry, "_current_grant", lambda *a: (None, NOW)
    )
    with pytest.raises(ReaderAuthorizationRegistryUnsafe, match="callable"):
        bind(registry)
    monkeypatch.undo()


def test_pinned_authority_and_alias_replacement_is_rejected(
    registry, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry.add_grant(grant_for(registry))
    monkeypatch.setattr(registry_module, "_PINNED_TIME_READ", lambda source: NOW)
    with pytest.raises(ReaderAuthorizationRegistryUnsafe, match="authority callable"):
        bind(registry)
    monkeypatch.undo()
    monkeypatch.setattr(registry_module, "_RR_AUTHORIZATION", dict)
    with pytest.raises(ReaderAuthorizationRegistryUnsafe, match="authority callable"):
        bind(registry)
    monkeypatch.undo()


@pytest.mark.parametrize("name", ("_metadata", "_trusted_head_sha256", "_head_key"))
def test_instance_authority_state_replacement_is_rejected(registry, name: str) -> None:
    instance = object.__getattribute__(registry, "__dict__")
    original = instance[name]
    instance[name] = {
        "_metadata": lambda: original.model_copy(
            update={"registry_epoch_sha256": "0" * 64}
        ),
        "_trusted_head_sha256": lambda: "0" * 64,
        "_head_key": lambda: ("x", "y"),
    }[name]()
    try:
        with pytest.raises(ReaderAuthorizationRegistryUnsafe, match="authority state"):
            registry.identity()
    finally:
        instance[name] = original


def test_interpreter_warning_registry_does_not_disable_the_registry(
    registry, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(registry_module.__dict__, "__warningregistry__", {})
    registry.add_grant(grant_for(registry))
    authorize(registry, bind(registry))


def test_denials_and_errors_carry_no_reader_or_scope_identifier(registry) -> None:
    registry.add_grant(grant_for(registry))
    binding = bind(registry)
    identity = registry.identity()
    secrets_ = (
        SELECTOR,
        COHORT,
        OTHER_COHORT,
        SYNTHETIC_READER_AUTHORITY_ID,
        identity.registry_id,
        binding.grant_sha256,
        "qty_short_fraction",
    )
    failures: list[BaseException] = []
    for call in (
        lambda: authorize(registry, binding, cohort=OTHER_COHORT),
        lambda: bind(registry, OTHER_SELECTOR),
        lambda: registry.add_grant(grant_for(registry)),
        lambda: registry.revoke_grant(
            OTHER_SELECTOR, reason=ReaderRevocationReason.OPERATOR_REQUEST
        ),
    ):
        with pytest.raises(Exception) as info:
            call()
        failures.append(info.value)
    for failure in failures:
        text = f"{failure!s} {failure!r}"
        assert not any(value in text for value in secrets_)
        assert failure.__cause__ is None


def test_crash_torn_journal_tail_fails_closed_without_repair(registry) -> None:
    registry.add_grant(grant_for(registry))
    binding = bind(registry)
    kwargs = reopen_kwargs(registry)
    journal = registry.root / "registry-journal.jsonl"
    with journal.open("ab") as handle:
        handle.write(b'{"entry_sha256":"00')
    torn = journal.read_bytes()
    with pytest.raises(ReaderAuthorizationRegistryUnsafe, match="incomplete"):
        registry.identity()
    with pytest.raises(ReaderAuthorizationRegistryUnsafe, match="incomplete"):
        authorize(registry, binding)
    with pytest.raises(ReaderAuthorizationRegistryUnsafe, match="incomplete"):
        ReaderAuthorizationRegistry(registry.root, **kwargs)
    assert journal.read_bytes() == torn


def test_forked_child_cannot_use_the_parent_lock(registry) -> None:
    registry.add_grant(grant_for(registry))
    def child() -> None:  # pragma: no cover - child process
        with pytest.raises(ReaderAuthorizationRegistryUnsafe):
            registry.revoke_grant(
                SELECTOR, reason=ReaderRevocationReason.OPERATOR_REQUEST
            )

    with registry.authority_read_fence():
        status = run_in_child(child)
    assert status == 0
    authorize(registry, bind(registry))


def test_forked_child_unwinding_the_fence_does_not_release_it(registry) -> None:
    registry.add_grant(grant_for(registry))
    probe = os.open(registry.root / ".registry.lock", os.O_RDWR)
    pid = None
    child_code = CHILD_RAISED
    try:
        with registry.authority_read_fence():
            pid = os.fork()
            if pid != 0:
                assert wait_child(pid) == 0
                # The child unwound the inherited fence; it is still held.
                with pytest.raises(BlockingIOError):
                    fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if pid == 0:  # pragma: no cover - child process
            child_code = 0
    except ReaderAuthorizationRegistryUnsafe:
        if pid != 0:
            raise
        child_code = 0  # pragma: no cover - child process
    finally:
        # Every child path ends here; it never returns into pytest.
        if pid == 0:  # pragma: no cover - child process
            os._exit(child_code)
        os.close(probe)
    authorize(registry, bind(registry))


def _storage_reopen(values) -> dict[str, object]:
    trust = synthetic_reader_trust()
    return {
        "profile": SYNTHETIC,
        "configured_trust": trust,
        "expected_trust_sha256": reader_trust_sha256(trust),
        "time_source": AuthorityTimeSource.fixed(NOW),
        **storage_checks.expected(values),
    }


# --- shared storage behaviour (tests/registry_storage_checks.py) ----------------


def test_storage_torn_tail_needs_explicit_operator_recovery(
    registry: ReaderAuthorizationRegistry,
) -> None:
    storage_checks.check_torn_tail_recovery(
        registry,
        lambda: registry.add_grant(grant_for(registry)),
        lambda values: ReaderAuthorizationRegistry(
            registry.root, **_storage_reopen(values)
        ),
        ReaderAuthorizationRegistryUnsafe,
    )


def test_storage_interrupted_append_truncates_on_any_exception(
    registry: ReaderAuthorizationRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage_checks.check_append_interrupt_truncates(
        registry,
        registry_module,
        lambda: registry.add_grant(grant_for(registry)),
        monkeypatch,
    )


def test_storage_lock_descriptor_is_read_under_the_process_lock(
    registry: ReaderAuthorizationRegistry, tmp_path: Path
) -> None:
    storage_checks.check_lock_reads_descriptor_under_process_lock(
        registry, ReaderAuthorizationRegistryUnsafe, tmp_path
    )


def test_storage_integrity_check_waits_for_an_in_flight_head_seal(
    registry: ReaderAuthorizationRegistry
) -> None:
    storage_checks.check_integrity_reads_head_and_seal_under_process_lock(
        registry, lambda: registry.add_grant(grant_for(registry))
    )


def test_storage_owned_temporaries_are_swept_and_directories_fail_closed(
    registry: ReaderAuthorizationRegistry,
) -> None:
    registry.add_grant(grant_for(registry))
    storage_checks.check_owned_temporaries(
        registry,
        lambda values: ReaderAuthorizationRegistry(
            registry.root, **_storage_reopen(values)
        ),
        ReaderAuthorizationRegistryUnsafe,
    )


def test_storage_interrupted_creation_is_recoverable(
    registry: ReaderAuthorizationRegistry,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage_checks.check_interrupted_creation(
        lambda root: create_registry(root),
        lambda root, values: ReaderAuthorizationRegistry(
            root, **_storage_reopen(values)
        ),
        tmp_path / "created-by-storage-check",
        registry_module,
        "_commit_staging_directory",
        "_remove_partial_target",
        monkeypatch,
    )


def test_storage_interrupted_restore_is_staged(
    registry: ReaderAuthorizationRegistry,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry.add_grant(grant_for(registry))
    storage_checks.check_interrupted_restore(
        registry,
        lambda target, backup, values: ReaderAuthorizationRegistry.restore(
            target, backup, **_storage_reopen(values)
        ),
        registry_module,
        tmp_path,
        monkeypatch,
    )
