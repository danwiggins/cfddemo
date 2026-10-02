"""B01 session binding to protected longitudinal-reader grants (E12 boundary)."""

from __future__ import annotations

import threading
import time
from datetime import timedelta
from pathlib import Path

import pytest

import evidence_inspector.reader_authorization_registry as registry_module
from evidence_inspector.provider_linkage_store import AuthorityTimeSource
from evidence_inspector.reader_authorization_registry import (
    ReaderAuthorizationDenied,
    ReaderAuthorizationRegistry,
    ReaderDenialReason,
    ReaderRevocationReason,
)
from tests.test_reader_authorization_registry import (
    COHORT,
    NOW,
    OTHER_COHORT,
    OTHER_SCOPE,
    OTHER_SELECTOR,
    SCOPE,
    SELECTOR,
    create_registry,
    grant_for,
)
from traceback_runner.web.auth import (
    BootstrapBroker,
    BoundaryDenied,
    BrowserRequest,
    LocalWebBoundary,
    ReaderSessionBinding,
    build_loopback_config,
)
from traceback_runner.web.reader_session import ReaderSessionBinder

AUTHORITY = "127.0.0.1:8765"
ORIGIN = f"http://{AUTHORITY}"


@pytest.fixture(autouse=True)
def fresh_profile_latch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(registry_module, "_PROCESS_PROFILE", {})


class Clock:
    def __init__(self) -> None:
        self.value = 100.0

    def __call__(self) -> float:
        return self.value


@pytest.fixture
def authority_clock() -> AuthorityTimeSource:
    return AuthorityTimeSource.fixed(NOW)


@pytest.fixture
def registry(tmp_path: Path, authority_clock: AuthorityTimeSource):
    created = create_registry(tmp_path / "reader", clock=authority_clock)
    created.add_grant(grant_for(created))
    created.add_grant(
        grant_for(created, selector=OTHER_SELECTOR, cohorts=(OTHER_COHORT,))
    )
    try:
        yield created
    finally:
        created.close()


@pytest.fixture
def web(registry):
    clock = Clock()
    broker = BootstrapBroker(now=clock)
    boundary = LocalWebBoundary(build_loopback_config(port=8765), broker)
    binder = ReaderSessionBinder(boundary=boundary, registry=registry, now=clock)
    return boundary, broker, binder, clock


def _session(boundary: LocalWebBoundary):
    code = boundary.issue_bootstrap()
    return boundary.exchange_bootstrap(
        BrowserRequest(
            method="POST",
            path="/api/v1/session/bootstrap",
            host=AUTHORITY,
            origin=ORIGIN,
        ),
        code,
    )


def _post(grant, path: str = "/api/v1/longitudinal/session") -> BrowserRequest:
    return BrowserRequest(
        method="POST",
        path=path,
        host=AUTHORITY,
        origin=ORIGIN,
        session_token=grant.session_token,
        csrf_token=grant.csrf_token,
    )


def _get(grant) -> BrowserRequest:
    return BrowserRequest(
        method="GET",
        path="/api/v1/longitudinal/workspace",
        host=AUTHORITY,
        session_token=grant.session_token,
    )


def _bound_session(boundary, binder, selector: str = SELECTOR):
    grant = _session(boundary)
    binder.exchange_launch_credential(
        _post(grant), binder.issue_launch_credential(selector)
    )
    return grant


def _read(binder, grant, *, cohort: str = COHORT, scope=SCOPE):
    with binder.reader_authorization(
        _get(grant), cohort_registry_id=cohort, measurement_scope=scope
    ) as authorization:
        return authorization


def _denied(reason: ReaderDenialReason):
    class _Match:
        def __enter__(self):
            self.ctx = pytest.raises(ReaderAuthorizationDenied)
            self.info = self.ctx.__enter__()
            return self.info

        def __exit__(self, *exc):
            result = self.ctx.__exit__(*exc)
            assert self.info.value.reason is reason
            assert str(self.info.value) == "permission_denied"
            return result

    return _Match()


def test_bare_b01_session_still_authorizes_existing_routes_but_not_e12(web) -> None:
    boundary, broker, binder, _ = web
    grant = _session(boundary)
    boundary.authorize(_get(grant))
    boundary.authorize(_post(grant, "/api/v1/jobs/job_0123456789abcdef/retry"))
    assert broker.reader_binding(grant.session_token, authority=AUTHORITY) is None
    with _denied(ReaderDenialReason.SESSION_UNBOUND):
        _read(binder, grant)


def test_launch_exchange_stores_only_commitment_and_head(web, registry) -> None:
    boundary, broker, binder, _ = web
    grant = _bound_session(boundary, binder)
    binding = broker.reader_binding(grant.session_token, authority=AUTHORITY)
    assert type(binding) is ReaderSessionBinding
    assert set(ReaderSessionBinding.__slots__) == {
        "grant_sha256",
        "registry_head_sha256",
    }
    assert binding.registry_head_sha256 == registry.identity().state_head_sha256
    authorization = _read(binder, grant)
    assert authorization.grant_sha256 == binding.grant_sha256
    assert authorization.cohort_registry_id == COHORT
    # The bound session still works for ordinary B01 routes unchanged.
    boundary.authorize(_get(grant))


def test_launch_credential_is_one_use_expiring_and_selector_only(web) -> None:
    boundary, _, binder, clock = web
    grant = _session(boundary)
    credential = binder.issue_launch_credential(SELECTOR)
    with _denied(ReaderDenialReason.LAUNCH_CREDENTIAL_INVALID):
        binder.exchange_launch_credential(_post(grant), credential + "x")
    # A failed guess does not consume the real credential, but a used one
    # cannot be replayed.
    binder.exchange_launch_credential(_post(grant), credential)
    other = _session(boundary)
    with _denied(ReaderDenialReason.LAUNCH_CREDENTIAL_INVALID):
        binder.exchange_launch_credential(_post(other), credential)
    expiring = binder.issue_launch_credential(SELECTOR)
    clock.value += 60
    with _denied(ReaderDenialReason.LAUNCH_CREDENTIAL_INVALID):
        binder.exchange_launch_credential(_post(other), expiring)
    for role in ("longitudinal_reader", "*", "reader_grant_*"):
        with pytest.raises(ValueError, match="selector"):
            binder.issue_launch_credential(role)
    with _denied(ReaderDenialReason.LAUNCH_CREDENTIAL_INVALID):
        binder.exchange_launch_credential(_post(other), "longitudinal_reader")


def test_exchange_requires_full_b01_mutation_checks(web) -> None:
    boundary, _, binder, _ = web
    grant = _session(boundary)
    credential = binder.issue_launch_credential(SELECTOR)
    no_csrf = BrowserRequest(
        method="POST",
        path="/api/v1/longitudinal/session",
        host=AUTHORITY,
        origin=ORIGIN,
        session_token=grant.session_token,
    )
    with pytest.raises(BoundaryDenied) as info:
        binder.exchange_launch_credential(no_csrf, credential)
    assert info.value.code == "TBX-AUTH-002"
    with pytest.raises(BoundaryDenied):
        binder.exchange_launch_credential(_get(grant), credential)
    unauthenticated = BrowserRequest(
        method="POST", path="/x", host=AUTHORITY, origin=ORIGIN
    )
    with pytest.raises(BoundaryDenied) as info:
        binder.exchange_launch_credential(unauthenticated, credential)
    assert info.value.status_code == 401
    # Transport failures did not consume the credential.
    binder.exchange_launch_credential(_post(grant), credential)


def test_a_session_binds_once(web) -> None:
    boundary, _, binder, _ = web
    grant = _bound_session(boundary, binder)
    with _denied(ReaderDenialReason.SESSION_ALREADY_BOUND):
        binder.exchange_launch_credential(
            _post(grant), binder.issue_launch_credential(OTHER_SELECTOR)
        )
    assert _read(binder, grant).cohort_registry_id == COHORT


def test_exchange_attempts_are_throttled(web) -> None:
    boundary, _, binder, _ = web
    grant = _session(boundary)
    credential = binder.issue_launch_credential(SELECTOR)
    for _ in range(8):
        with _denied(ReaderDenialReason.LAUNCH_CREDENTIAL_INVALID):
            binder.exchange_launch_credential(_post(grant), "A" * 43)
    with _denied(ReaderDenialReason.LAUNCH_CREDENTIAL_INVALID):
        binder.exchange_launch_credential(_post(grant), credential)


def test_wrong_scope_is_permission_denied(web) -> None:
    boundary, _, binder, _ = web
    grant = _bound_session(boundary, binder)
    with _denied(ReaderDenialReason.SCOPE_MISMATCH):
        _read(binder, grant, cohort=OTHER_COHORT)
    with _denied(ReaderDenialReason.SCOPE_MISMATCH):
        _read(binder, grant, scope=OTHER_SCOPE)


def test_revoked_expired_and_stale_bindings_are_denied(
    web, registry, authority_clock: AuthorityTimeSource
) -> None:
    boundary, _, binder, _ = web
    revoked = _bound_session(boundary, binder)
    registry.revoke_grant(SELECTOR, reason=ReaderRevocationReason.PROVIDER_REQUEST)
    # Any registry advance after binding is a stale head for that session.
    with _denied(ReaderDenialReason.STALE_HEAD):
        _read(binder, revoked)
    with _denied(ReaderDenialReason.GRANT_REVOKED):
        _bound_session(boundary, binder)
    current = _bound_session(boundary, binder, OTHER_SELECTOR)
    _read(binder, current, cohort=OTHER_COHORT)
    authority_clock.advance_to(NOW + timedelta(days=1))
    with _denied(ReaderDenialReason.GRANT_NOT_CURRENT):
        _read(binder, current, cohort=OTHER_COHORT)


def test_missing_registry_disables_e12(web) -> None:
    boundary, broker, _, clock = web
    disabled = ReaderSessionBinder(boundary=boundary, registry=None, now=clock)
    assert disabled.enabled is False
    grant = _session(boundary)
    with _denied(ReaderDenialReason.AUTHORITY_ABSENT):
        disabled.exchange_launch_credential(
            _post(grant), disabled.issue_launch_credential(SELECTOR)
        )
    broker.bind_reader_session(
        grant.session_token,
        authority=AUTHORITY,
        binding=ReaderSessionBinding(
            grant_sha256="0" * 64, registry_head_sha256="1" * 64
        ),
    )
    with _denied(ReaderDenialReason.AUTHORITY_ABSENT):
        _read(disabled, grant)


def test_registry_replacement_denies_bound_sessions(web, registry, tmp_path) -> None:
    boundary, _, binder, _ = web
    grant = _bound_session(boundary, binder)
    (tmp_path / "reader").rename(tmp_path / "moved")
    replacement = create_registry(tmp_path / "reader")
    try:
        with _denied(ReaderDenialReason.REGISTRY_UNAVAILABLE):
            _read(binder, grant)
        rebound = ReaderSessionBinder(
            boundary=boundary, registry=replacement, now=web[3]
        )
        with _denied(ReaderDenialReason.STALE_HEAD):
            _read(rebound, grant)
    finally:
        replacement.close()


def test_revocation_cannot_land_before_final_return(web, registry) -> None:
    boundary, _, binder, _ = web
    grant = _bound_session(boundary, binder)
    peer = ReaderAuthorizationRegistry(
        registry.root,
        **_reopen(registry),
    )
    finished = threading.Event()

    def revoke() -> None:
        peer.revoke_grant(SELECTOR, reason=ReaderRevocationReason.PROVIDER_REQUEST)
        finished.set()

    try:
        with binder.reader_authorization(
            _get(grant), cohort_registry_id=COHORT, measurement_scope=SCOPE
        ) as authorization:
            worker = threading.Thread(target=revoke)
            worker.start()
            time.sleep(0.5)
            assert not finished.is_set()
            response = {"grant": authorization.grant_sha256}
        worker.join(timeout=30)
        assert finished.is_set()
        assert response["grant"] == authorization.grant_sha256
        with _denied(ReaderDenialReason.STALE_HEAD):
            _read(binder, grant)
    finally:
        peer.close()


def _reopen(registry):
    from tests.test_reader_authorization_registry import reopen_kwargs

    return reopen_kwargs(registry)


def test_expiry_during_the_protected_build_denies_the_return(
    web, authority_clock: AuthorityTimeSource
) -> None:
    boundary, _, binder, _ = web
    grant = _bound_session(boundary, binder)
    with _denied(ReaderDenialReason.GRANT_NOT_CURRENT):
        with binder.reader_authorization(
            _get(grant), cohort_registry_id=COHORT, measurement_scope=SCOPE
        ):
            authority_clock.advance_to(NOW + timedelta(days=1))


def test_b01_session_expiry_during_the_build_denies_the_return(web) -> None:
    boundary, _, binder, clock = web
    grant = _bound_session(boundary, binder)
    with _denied(ReaderDenialReason.SESSION_UNBOUND):
        with binder.reader_authorization(
            _get(grant), cohort_registry_id=COHORT, measurement_scope=SCOPE
        ):
            clock.value += 9 * 60 * 60


def test_caller_errors_inside_the_fence_propagate_and_release_it(
    web, registry
) -> None:
    boundary, _, binder, _ = web
    grant = _bound_session(boundary, binder)
    with pytest.raises(KeyError):
        with binder.reader_authorization(
            _get(grant), cohort_registry_id=COHORT, measurement_scope=SCOPE
        ):
            raise KeyError("caller")
    registry.revoke_grant(SELECTOR, reason=ReaderRevocationReason.OPERATOR_REQUEST)


def test_denials_carry_no_identifier(web, registry) -> None:
    boundary, _, binder, _ = web
    grant = _bound_session(boundary, binder)
    identity = registry.identity()
    with pytest.raises(ReaderAuthorizationDenied) as info:
        _read(binder, grant, cohort=OTHER_COHORT)
    text = f"{info.value!s} {info.value!r} {info.value.args}"
    for value in (
        SELECTOR,
        COHORT,
        OTHER_COHORT,
        identity.registry_id,
        grant.session_token,
        "qty_short_fraction",
    ):
        assert value not in text
    assert info.value.__cause__ is None
