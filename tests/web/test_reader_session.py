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


def _session(boundary: LocalWebBoundary, code: str | None = None):
    """An operator session, or the session a given bootstrap code creates."""

    code = boundary.issue_bootstrap() if code is None else code
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


def _reader_session(boundary, binder, selector: str = SELECTOR):
    """What a reader launch link does: a reader bootstrap plus its credential."""

    bootstrap, credential = binder.issue_launch(selector)
    return _session(boundary, bootstrap), credential


def _bound_session(boundary, binder, selector: str = SELECTOR):
    grant, credential = _reader_session(boundary, binder, selector)
    binder.exchange_launch_credential(_post(grant), credential)
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
    grant, credential = _reader_session(boundary, binder)
    with _denied(ReaderDenialReason.LAUNCH_CREDENTIAL_INVALID):
        binder.exchange_launch_credential(_post(grant), credential + "x")
    # A failed guess does not consume the real credential, but a used one
    # cannot be replayed.
    binder.exchange_launch_credential(_post(grant), credential)
    other, _ = _reader_session(boundary, binder)
    with _denied(ReaderDenialReason.LAUNCH_CREDENTIAL_INVALID):
        binder.exchange_launch_credential(_post(other), credential)
    expiring_session, expiring = _reader_session(boundary, binder)
    clock.value += 60
    with _denied(ReaderDenialReason.LAUNCH_CREDENTIAL_INVALID):
        binder.exchange_launch_credential(_post(expiring_session), expiring)
    for role in ("longitudinal_reader", "*", "reader_grant_*"):
        with pytest.raises(ValueError, match="selector"):
            binder.issue_launch_credential(role)
    with _denied(ReaderDenialReason.LAUNCH_CREDENTIAL_INVALID):
        binder.exchange_launch_credential(_post(other), "longitudinal_reader")


def test_exchange_requires_full_b01_mutation_checks(web) -> None:
    boundary, _, binder, _ = web
    grant, credential = _reader_session(boundary, binder)
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
    boundary, broker, binder, _ = web
    grant = _bound_session(boundary, binder)
    # H1: another link's credential belongs to another session, so a bound
    # session cannot even present it.
    _, other = _reader_session(boundary, binder, OTHER_SELECTOR)
    with _denied(ReaderDenialReason.LAUNCH_CREDENTIAL_INVALID):
        binder.exchange_launch_credential(_post(grant), other)
    with pytest.raises(BoundaryDenied) as info:
        broker.bind_reader_session(
            grant.session_token,
            authority=AUTHORITY,
            binding=ReaderSessionBinding(
                grant_sha256="0" * 64, registry_head_sha256="1" * 64
            ),
        )
    assert info.value.code == "TBX-AUTH-006"
    assert _read(binder, grant).cohort_registry_id == COHORT


def test_failed_presentations_never_consume_another_links_credential(web) -> None:
    """H1 (replaces the old throttle test, whose trip cleared every pending
    credential): wrong guesses and wrong-session presentations, however
    many, leave each link's own credential redeemable by its own session."""

    boundary, _, binder, _ = web
    grant, credential = _reader_session(boundary, binder)
    spammer, spammer_credential = _reader_session(boundary, binder, OTHER_SELECTOR)
    for _ in range(32):
        with _denied(ReaderDenialReason.LAUNCH_CREDENTIAL_INVALID):
            binder.exchange_launch_credential(_post(grant), "A" * 43)
        with _denied(ReaderDenialReason.LAUNCH_CREDENTIAL_INVALID):
            binder.exchange_launch_credential(_post(spammer), credential)
    binder.exchange_launch_credential(_post(grant), credential)
    binder.exchange_launch_credential(_post(spammer), spammer_credential)
    assert _read(binder, grant).cohort_registry_id == COHORT


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
    # Its own revocation ends the session.
    with _denied(ReaderDenialReason.GRANT_REVOKED):
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
    grant, credential = _reader_session(boundary, disabled)
    with _denied(ReaderDenialReason.AUTHORITY_ABSENT):
        disabled.exchange_launch_credential(_post(grant), credential)
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
        with _denied(ReaderDenialReason.GRANT_REVOKED):
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


# --- H1: session kinds and launch-digest binding ---------------------------------------


def test_operator_session_cannot_redeem_a_reader_link_credential(web) -> None:
    """Criterion 2: a session from operator bootstrap A cannot redeem link B's
    credential, and the attempt does not consume it; B's own reader session
    then redeems it."""

    boundary, _, binder, _ = web
    reader, credential = _reader_session(boundary, binder)
    operator = _session(boundary)
    with _denied(ReaderDenialReason.LAUNCH_CREDENTIAL_INVALID):
        binder.exchange_launch_credential(_post(operator), credential)
    binder.exchange_launch_credential(_post(reader), credential)
    assert _read(binder, reader).cohort_registry_id == COHORT


def test_reader_session_redeems_only_its_own_links_credential(web) -> None:
    boundary, _, binder, _ = web
    first, first_credential = _reader_session(boundary, binder)
    second, second_credential = _reader_session(boundary, binder, OTHER_SELECTOR)
    with _denied(ReaderDenialReason.LAUNCH_CREDENTIAL_INVALID):
        binder.exchange_launch_credential(_post(first), second_credential)
    with _denied(ReaderDenialReason.LAUNCH_CREDENTIAL_INVALID):
        binder.exchange_launch_credential(_post(second), first_credential)
    binder.exchange_launch_credential(_post(first), first_credential)
    binder.exchange_launch_credential(_post(second), second_credential)
    assert _read(binder, first).cohort_registry_id == COHORT
    assert (
        _read(binder, second, cohort=OTHER_COHORT).cohort_registry_id == OTHER_COHORT
    )


def test_an_operator_session_can_never_carry_a_reader_binding(web) -> None:
    boundary, broker, _, _ = web
    operator = _session(boundary)
    with pytest.raises(BoundaryDenied) as info:
        broker.bind_reader_session(
            operator.session_token,
            authority=AUTHORITY,
            binding=ReaderSessionBinding(
                grant_sha256="0" * 64, registry_head_sha256="1" * 64
            ),
        )
    assert info.value.code == "TBX-AUTH-007"
    assert broker.reader_binding(operator.session_token, authority=AUTHORITY) is None


def test_own_grant_revocation_ends_the_session_other_denials_do_not(
    web, registry
) -> None:
    boundary, broker, binder, _ = web
    grant = _bound_session(boundary, binder)
    with _denied(ReaderDenialReason.SCOPE_MISMATCH):
        _read(binder, grant, cohort=OTHER_COHORT)
    broker.require_session(grant.session_token, authority=AUTHORITY)
    registry.revoke_grant(SELECTOR, reason=ReaderRevocationReason.OPERATOR_REQUEST)
    with _denied(ReaderDenialReason.GRANT_REVOKED):
        _read(binder, grant)
    with pytest.raises(BoundaryDenied) as info:
        broker.require_session(grant.session_token, authority=AUTHORITY)
    assert (info.value.status_code, info.value.code) == (401, "TBX-AUTH-001")


def test_grant_no_longer_current_ends_the_session(
    web, authority_clock: AuthorityTimeSource
) -> None:
    boundary, broker, binder, _ = web
    grant = _bound_session(boundary, binder)
    authority_clock.advance_to(NOW + timedelta(days=1))
    with _denied(ReaderDenialReason.GRANT_NOT_CURRENT):
        _read(binder, grant)
    with pytest.raises(BoundaryDenied):
        broker.require_session(grant.session_token, authority=AUTHORITY)


def test_a_failed_bootstrap_issue_drops_the_launch_credential(
    web, monkeypatch: pytest.MonkeyPatch
) -> None:
    boundary, _, binder, _ = web

    def refuse(**kwargs):
        raise RuntimeError("strong unique credential issuance failed")

    monkeypatch.setattr(boundary, "issue_bootstrap", refuse)
    with pytest.raises(RuntimeError):
        binder.issue_launch(SELECTOR)
    assert binder._pending == {}
