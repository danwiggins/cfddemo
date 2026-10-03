"""H1 core: session kinds, the exact route table, slot keeping, idle and logout.

Security spec: docs/PILOT-SECURITY-HARDENING.md § H1 and the Eng review's
corrected acceptance list (criteria 1-5, regressions R1 and R3).
"""

from __future__ import annotations

import json
import subprocess
import threading
from pathlib import Path

import pytest

import evidence_inspector.reader_authorization_registry as reader_module
from tests.web.longitudinal_env import (
    NODE,
    PREFIX,
    STATIC,
    Env,
    _call_route,
    _every_route,
    _http,
    _json,
    needs_node,
    shared_env,
)
from tests.web.test_loopback_server import _exchange, _request, _store
from traceback_runner.web import server as server_module
from traceback_runner.web.auth import (
    ANY_SESSION,
    IDLE_TIMEOUT_SECONDS,
    OPERATOR_ONLY,
    READER_ONLY,
    BootstrapBroker,
    BoundaryDenied,
    BrowserRequest,
    LocalWebBoundary,
    build_loopback_config,
)
from traceback_runner.web.longitudinal import GET_ROUTE_PATHS, POST_ROUTE_PATHS
from traceback_runner.web.server import RunningLocalWebService

AUTHORITY = "127.0.0.1:8765"
ORIGIN = f"http://{AUTHORITY}"
APP_HARNESS = Path("tests/web/app_dom_harness.js")
AUTH_007 = {"error": {"code": "TBX-AUTH-007"}}
AUTH_001 = {"error": {"code": "TBX-AUTH-001"}}
JOB_PATH = "/api/v1/jobs/job_" + "0" * 32
RESULT_PATH = "/api/v1/explorer/results/result_" + "0" * 40
COMPARE_PATH = (
    "/api/v1/explorer/compare?left=result_" + "0" * 40 + "&right=result_" + "1" * 40
)


# --- the route table ------------------------------------------------------------------

# Every path a handler serves, with the kinds the spec's corrected H1 table
# gives it.  A new handler must be added here and to the table.
EXPECTED = {
    **{
        ("GET", asset): frozenset()
        for asset in (
            "/",
            "/assets/app.js",
            "/assets/styles.css",
            "/assets/longitudinal.js",
        )
    },
    ("POST", "/api/v1/session/bootstrap"): frozenset(),
    ("POST", "/api/v1/session/validate"): ANY_SESSION,
    ("POST", "/api/v1/session/reader-launch"): READER_ONLY,
    ("POST", "/api/v1/session/logout"): ANY_SESSION,
    ("GET", "/api/v1/jobs"): OPERATOR_ONLY,
    ("GET", "job_detail"): OPERATOR_ONLY,
    ("GET", "/api/v1/explorer/catalog"): OPERATOR_ONLY,
    ("GET", "/api/v1/explorer/compare"): OPERATOR_ONLY,
    ("GET", "explorer_result"): OPERATOR_ONLY,
    **{("GET", path): READER_ONLY for path in GET_ROUTE_PATHS},
    **{("POST", path): READER_ONLY for path in POST_ROUTE_PATHS},
}


def test_route_table_lists_every_handled_route_with_its_kinds() -> None:
    gets = {k: v for k, v in EXPECTED.items() if k[0] == "GET"}
    heads = {("HEAD", key): kinds for (_, key), kinds in gets.items()}
    assert server_module._ROUTE_KINDS == {**EXPECTED, **heads}
    # The merged browser routes are copied from their own constants: 3 GET
    # and 4 POST longitudinal routes, all reader-only.
    assert len(GET_ROUTE_PATHS) == 3 and len(POST_ROUTE_PATHS) == 4
    for path in GET_ROUTE_PATHS | POST_ROUTE_PATHS:
        assert path.startswith(PREFIX)


def test_every_handler_is_reachable_only_through_the_table() -> None:
    handlers = {
        name
        for name in vars(server_module._Handler)
        if name.startswith("_route_")
    }
    routed = {
        route.handler.__name__
        for route in (
            *server_module._ROUTES.values(),
            *(route for _, route in server_module._PATTERN_ROUTES),
        )
    }
    assert handlers == routed


@pytest.mark.parametrize(
    ("method", "path", "key"),
    [
        ("GET", JOB_PATH, "job_detail"),
        ("GET", RESULT_PATH, "explorer_result"),
        ("GET", "/api/v1/jobs", "/api/v1/jobs"),
        ("POST", PREFIX + "workspace", PREFIX + "workspace"),
    ],
)
def test_resolution_is_exact_or_a_named_full_match(method, path, key) -> None:
    route, _ = server_module._resolve_route(method, path)
    assert route.key == key
    for near_miss in (path + "/", path + "x", "/x" + path, path.upper()):
        assert server_module._resolve_route(method, near_miss) is None
    other = "POST" if method == "GET" else "GET"
    assert server_module._resolve_route(other, path) is None


# --- one shared E12 world: reader and operator sessions over real HTTP ----------------


@pytest.fixture(scope="module")
def env(tmp_path_factory: pytest.TempPathFactory):
    yield from shared_env(tmp_path_factory)


def _get(env: Env, path: str, cookie: str, method: str = "GET"):
    return _http(env.service, method, path, {"Cookie": cookie})


def test_reader_session_is_denied_every_operator_route(env: Env) -> None:
    """Criterion 1, against the composition the browser PR wires (it supplies
    an explorer): 403 TBX-AUTH-007 on jobs, job detail, catalog, compare and
    result document, for GET and HEAD; an operator session passes the table."""

    for path in ("/api/v1/jobs", JOB_PATH, "/api/v1/explorer/catalog", COMPARE_PATH, RESULT_PATH):
        status, content = _get(env, path, env.cookie)
        assert (status, _json(content)) == (403, AUTH_007), path
        status, _ = _get(env, path, env.cookie, method="HEAD")
        assert status == 403, path
    operator, _ = env.operator()
    status, content = _get(env, "/api/v1/jobs", operator)
    assert status == 200 and "jobs" in _json(content)
    status, content = _get(env, "/api/v1/explorer/catalog", operator)
    assert status == 200 and "results" in _json(content)
    # Unknown identities: the operator passes the table and reaches the
    # handler's own not-found.  (Compare of unknown identities is covered by
    # the H6 dispatcher catch.)
    for path in (JOB_PATH, RESULT_PATH):
        status, content = _get(env, path, operator)
        assert status == 404 and b"TBX-AUTH" not in content, path


def test_bound_reader_keeps_every_longitudinal_route(env: Env) -> None:
    """R3: every browser-PR route (GET and POST) still works for a reader."""

    for method, route, value in _every_route(env):
        status, content = _call_route(env, method, route, value)
        assert b"TBX-AUTH" not in content, (method, route)
        if route == "reopen":
            # An unknown saved selector is the route's own bounded denial.
            assert (status, _json(content)) == (
                403,
                {"error": {"code": "permission_denied"}},
            )
        else:
            assert status == 200, (method, route, content)


def test_unauthenticated_requests_get_401_before_the_kind_check(env: Env) -> None:
    for path in ("/api/v1/jobs", PREFIX + "selectors", RESULT_PATH):
        status, content = _http(env.service, "GET", path, {})
        assert (status, _json(content)) == (401, AUTH_001), path


def test_unknown_paths_are_not_found_for_every_kind(env: Env) -> None:
    operator, operator_csrf = env.operator()
    for cookie, csrf in ((env.cookie, env.csrf), (operator, operator_csrf)):
        for method, path in (
            ("GET", "/api/v1/explorer"),
            ("GET", "/api/v1/longitudinal/"),
            ("GET", PREFIX + "workspace"),
            ("POST", PREFIX + "selectors"),
            ("POST", "/api/v1/jobs"),
            ("GET", "/api/v1/session/validate"),
        ):
            status, _ = _http(
                env.service,
                method,
                path,
                {"Cookie": cookie, "Origin": env.service.base_url, "X-Traceback-CSRF": csrf},
                {} if method == "POST" else None,
            )
            assert status == 404, (method, path)


def test_a_route_removed_from_the_table_is_unreachable(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    operator, _ = env.operator()
    assert _get(env, "/api/v1/jobs", operator)[0] == 200
    routes = dict(server_module._ROUTES)
    del routes[("GET", "/api/v1/jobs")]
    monkeypatch.setattr(server_module, "_ROUTES", routes)
    assert _get(env, "/api/v1/jobs", operator)[0] == 404


def test_denied_requests_do_not_extend_a_session(env: Env) -> None:
    broker = env.binder()._boundary.broker
    token = env.cookie.split("=", 1)[1]
    authority = env.service.config.authority
    before = broker.require_session(token, authority=authority).last_seen_at
    assert _get(env, "/api/v1/jobs", env.cookie)[0] == 403
    assert broker.require_session(token, authority=authority).last_seen_at == before
    assert env.get("saved")[0] == 200
    assert broker.require_session(token, authority=authority).last_seen_at >= before


# --- R1: the reader page lands on the longitudinal view -------------------------------


def _run_app(tmp_path: Path, scenario: dict) -> dict:
    path = tmp_path / "app-scenario.json"
    path.write_text(json.dumps({"static": str(STATIC), **scenario}))
    result = subprocess.run(
        [NODE, str(APP_HARNESS), str(path)],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["errors"] == []
    return report


OPERATOR_SECTIONS = ("explorer-filters", "results", "explorer-provenance", "operator-jobs")


def _calls(report: dict) -> list[tuple[str, str, int]]:
    return [
        (item["method"], item["path"].split("?")[0], item["status"])
        for item in report["requests"]
    ]


@needs_node
def test_reader_link_lands_on_the_longitudinal_view_through_the_real_server(
    env: Env, tmp_path: Path
) -> None:
    """R1 end to end: both packaged scripts, driven against the real loopback
    server with a real reader link, bind, reveal the longitudinal view and
    load its selectors; the page never requests an operator route and never
    says the session failed."""

    link = env.service.issue_reader_launch_url(env.world.grant.payload.grant_selector)
    report = _run_app(
        tmp_path,
        {"baseUrl": env.service.base_url, "hash": "#" + link.split("#", 1)[1]},
    )
    assert _calls(report) == [
        ("POST", "/api/v1/session/bootstrap", 200),
        ("POST", "/api/v1/session/reader-launch", 200),
        ("GET", PREFIX + "selectors", 200),
        ("GET", PREFIX + "saved", 200),
    ]
    assert report["status"] == "Longitudinal reader session bound"
    assert report["dataset"] == {"sessionKind": "reader", "readerSession": "bound"}
    assert report["hidden"]["longitudinal"] is False
    assert all(report["hidden"][section] for section in OPERATOR_SECTIONS)
    assert report["lgStatus"].startswith("Choose a cohort version")


@needs_node
def test_reader_bootstrap_without_its_credential_never_takes_the_operator_path(
    env: Env, tmp_path: Path
) -> None:
    """The page branches on the server-assigned session kind, not on the
    fragment: a reader link stripped of its credential stays a reader page."""

    link = env.service.issue_reader_launch_url(env.world.grant.payload.grant_selector)
    bootstrap = link.split("#", 1)[1].split("&", 1)[0]
    report = _run_app(
        tmp_path, {"baseUrl": env.service.base_url, "hash": "#" + bootstrap}
    )
    assert _calls(report) == [("POST", "/api/v1/session/bootstrap", 200)]
    assert "incomplete" in report["status"]
    assert "unavailable" not in report["status"]
    assert all(report["hidden"][section] for section in OPERATOR_SECTIONS)


@needs_node
def test_operator_link_still_loads_jobs_and_catalog(env: Env, tmp_path: Path) -> None:
    code = env.service.issue_bootstrap()
    report = _run_app(
        tmp_path, {"baseUrl": env.service.base_url, "hash": f"#bootstrap={code}"}
    )
    assert _calls(report)[:3] == [
        ("POST", "/api/v1/session/bootstrap", 200),
        ("GET", "/api/v1/jobs", 200),
        ("GET", "/api/v1/explorer/catalog", 200),
    ]
    assert not any(item[1].startswith(PREFIX) for item in _calls(report))
    assert report["dataset"]["sessionKind"] == "operator"
    assert "readerSession" not in report["dataset"]
    assert not any(report["hidden"][section] for section in OPERATOR_SECTIONS)
    assert report["hidden"]["longitudinal"] is True


@needs_node
def test_replaced_or_expired_reader_link_says_so(tmp_path: Path) -> None:
    report = _run_app(
        tmp_path,
        {
            "hash": "#bootstrap=" + "A" * 43 + "&reader_launch=" + "B" * 43,
            "responses": {
                "/api/v1/session/bootstrap": [{"status": 401, "payload": AUTH_001}]
            },
        },
    )
    assert _calls(report) == [("POST", "/api/v1/session/bootstrap", 401)]
    assert "expired or was replaced" in report["status"]
    assert report["dataset"] == {"readerSession": "link-unavailable"}


@needs_node
def test_denied_reader_launch_keeps_its_message_and_requests_nothing_else(
    tmp_path: Path,
) -> None:
    report = _run_app(
        tmp_path,
        {
            "hash": "#bootstrap=" + "A" * 43 + "&reader_launch=" + "B" * 43,
            "responses": {
                "/api/v1/session/bootstrap": [
                    {
                        "status": 200,
                        "payload": {"csrf_token": "C" * 43, "session_kind": "reader"},
                    }
                ],
                "/api/v1/session/reader-launch": [
                    {"status": 403, "payload": {"error": {"code": "permission_denied"}}}
                ],
            },
        },
    )
    assert _calls(report) == [
        ("POST", "/api/v1/session/bootstrap", 200),
        ("POST", "/api/v1/session/reader-launch", 403),
    ]
    assert "denied" in report["status"]
    assert report["hidden"]["longitudinal"] is True


# --- bootstrap slot, logout and idle over real HTTP ------------------------------------


@pytest.fixture
def service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(reader_module, "_PROCESS_PROFILE", {})
    store, _ = _store(tmp_path)
    with RunningLocalWebService.start(
        store=store, state_directory=tmp_path / "state"
    ) as running:
        yield running


def _bootstrap(service, code: str):
    return _request(
        service,
        "POST",
        "/api/v1/session/bootstrap",
        headers={"Origin": service.base_url},
        payload={"bootstrap": code},
    )


def test_wrong_code_does_not_burn_the_pending_bootstrap(service) -> None:
    """Criterion 3: garbage, then the real code: 401 then 200."""

    status, _, content = _bootstrap(service, "A" * 43)
    assert (status, json.loads(content)) == (401, AUTH_001)
    status, _, _ = _bootstrap(service, "not a code")
    assert status == 401
    status, _, _ = _bootstrap(service, service.bootstrap_code)
    assert status == 200


def test_tripped_rate_limit_answers_429_and_keeps_the_slot(service) -> None:
    for _ in range(8):
        assert _bootstrap(service, "A" * 43)[0] == 401
    status, _, content = _bootstrap(service, "A" * 43)
    assert (status, json.loads(content)) == (429, {"error": {"code": "TBX-AUTH-005"}})
    status, _, _ = _bootstrap(service, service.bootstrap_code)
    assert status == 200
    # One use: the exchanged code is gone.
    assert _bootstrap(service, service.bootstrap_code)[0] == 429


def test_logout_ends_the_session_and_a_repeat_is_401(service) -> None:
    cookie, csrf = _exchange(service)
    mutation = {"Cookie": cookie, "Origin": service.base_url, "X-Traceback-CSRF": csrf}
    status, _, _ = _request(service, "POST", "/api/v1/session/logout", headers={"Cookie": cookie, "Origin": service.base_url})
    assert status == 403  # CSRF is required
    status, headers, content = _request(
        service, "POST", "/api/v1/session/logout", headers=mutation
    )
    assert (status, content) == (204, b"")
    assert headers["cache-control"] == "no-store"
    for path in ("/api/v1/session/logout", "/api/v1/session/validate"):
        status, _, content = _request(service, "POST", path, headers=mutation)
        assert (status, json.loads(content)) == (401, AUTH_001), path
    status, _, _ = _request(service, "GET", "/api/v1/jobs", headers={"Cookie": cookie})
    assert status == 401


# --- broker unit tests: idle timeout, refresh and the one-lock check --------------------


class Clock:
    def __init__(self) -> None:
        self.value = 1_000.0

    def __call__(self) -> float:
        return self.value


def _web(clock: Clock) -> tuple[LocalWebBoundary, BootstrapBroker]:
    broker = BootstrapBroker(now=clock)
    return LocalWebBoundary(build_loopback_config(port=8765), broker), broker


def _session(boundary: LocalWebBoundary):
    request = BrowserRequest(
        method="POST", path="/api/v1/session/bootstrap", host=AUTHORITY, origin=ORIGIN
    )
    return boundary.exchange_bootstrap(request, boundary.issue_bootstrap())


def _get_request(grant) -> BrowserRequest:
    return BrowserRequest(
        method="GET", path="/api/v1/jobs", host=AUTHORITY, session_token=grant.session_token
    )


def test_idle_timeout_is_exact_and_activity_extends_it() -> None:
    """Criterion 4 with the injected clock: 1200 s idle passes, 1201 s is 401."""

    clock = Clock()
    boundary, _ = _web(clock)
    grant = _session(boundary)
    clock.value += IDLE_TIMEOUT_SECONDS
    boundary.authorize(_get_request(grant), kinds=OPERATOR_ONLY)
    clock.value += IDLE_TIMEOUT_SECONDS
    boundary.authorize(_get_request(grant), kinds=OPERATOR_ONLY)
    clock.value += IDLE_TIMEOUT_SECONDS + 1
    with pytest.raises(BoundaryDenied) as info:
        boundary.authorize(_get_request(grant), kinds=OPERATOR_ONLY)
    assert (info.value.status_code, info.value.code) == (401, "TBX-AUTH-001")


def test_the_eight_hour_limit_still_applies_to_an_active_session() -> None:
    clock = Clock()
    boundary, _ = _web(clock)
    grant = _session(boundary)
    for _ in range(24):
        clock.value += IDLE_TIMEOUT_SECONDS
        boundary.authorize(_get_request(grant))
    clock.value += 1
    with pytest.raises(BoundaryDenied):
        boundary.authorize(_get_request(grant))


def test_a_denied_kind_does_not_refresh_and_the_idle_clock_still_runs() -> None:
    clock = Clock()
    boundary, broker = _web(clock)
    grant = _session(boundary)
    clock.value += IDLE_TIMEOUT_SECONDS
    with pytest.raises(BoundaryDenied) as info:
        boundary.authorize(_get_request(grant), kinds=READER_ONLY)
    assert (info.value.status_code, info.value.code) == (403, "TBX-AUTH-007")
    clock.value += 1
    with pytest.raises(BoundaryDenied) as info:
        boundary.authorize(_get_request(grant))
    assert info.value.status_code == 401
    assert broker.end_session(grant.session_token) is False


def test_kind_check_follows_authentication_and_csrf() -> None:
    clock = Clock()
    boundary, _ = _web(clock)
    grant = _session(boundary)
    no_csrf = BrowserRequest(
        method="POST",
        path=PREFIX + "workspace",
        host=AUTHORITY,
        origin=ORIGIN,
        session_token=grant.session_token,
    )
    with pytest.raises(BoundaryDenied) as info:
        boundary.authorize(no_csrf, kinds=READER_ONLY)
    assert info.value.code == "TBX-AUTH-002"
    anonymous = BrowserRequest(method="GET", path=PREFIX + "saved", host=AUTHORITY)
    with pytest.raises(BoundaryDenied) as info:
        boundary.authorize(anonymous, kinds=READER_ONLY)
    assert info.value.status_code == 401
    with pytest.raises(ValueError):
        boundary.authorize(_get_request(grant), kinds=frozenset())


def test_reader_bootstrap_needs_its_launch_digest() -> None:
    clock = Clock()
    boundary, _ = _web(clock)
    with pytest.raises(ValueError):
        boundary.issue_bootstrap(kind="reader")
    with pytest.raises(ValueError):
        boundary.issue_bootstrap(launch_credential_sha256=b"0" * 32)
    with pytest.raises(ValueError):
        boundary.issue_bootstrap(kind="admin")  # type: ignore[arg-type]


def test_logout_racing_requests_never_resurrects_the_session() -> None:
    """A refresh and ``end_session`` share one lock: after the race the
    session is gone whatever the interleaving."""

    for _ in range(50):
        clock = Clock()
        boundary, broker = _web(clock)
        grant = _session(boundary)
        barrier = threading.Barrier(5)

        def request() -> None:
            barrier.wait()
            for _ in range(20):
                try:
                    boundary.authorize(_get_request(grant))
                except BoundaryDenied:
                    return

        def logout() -> None:
            barrier.wait()
            broker.end_session(grant.session_token)

        workers = [threading.Thread(target=request) for _ in range(4)]
        workers.append(threading.Thread(target=logout))
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=30)
        with pytest.raises(BoundaryDenied):
            broker.require_session(grant.session_token, authority=AUTHORITY)
