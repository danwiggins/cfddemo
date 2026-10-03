"""Shared environment for the E12 longitudinal route and browser tests.

One coherent synthetic world over every merged E12 store
(``tests/longitudinal_workspace_world``), a real
``LongitudinalComparisonRegistry`` behind a ``CompositeAuthorityFence``, the
packaged loopback server with the longitudinal adapter, and one bound reader
session.  The packaged view is driven by an offline DOM harness under node.

Each environment starts its own loopback service on its own state directory
(the B01 startup anchor admits one service per state directory).  A test
module uses either the shared read-only ``env`` or a per-test ``fresh``
environment.
"""

from __future__ import annotations

import base64
import http.client
import json
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import timedelta
from html.parser import HTMLParser
from pathlib import Path

import pytest

import evidence_inspector.reader_authorization_registry as reader_module
import traceback_runner.web.longitudinal as longitudinal_module
from evidence_inspector.cohort_manifest import cohort_manifest_sha256
from evidence_inspector.composite_authority_fence import (
    CompositeAuthorityCoordinator,
    CompositeAuthorityFence,
)
from evidence_inspector.longitudinal_comparison_registry import (
    LongitudinalComparisonRegistry,
)
from tests.longitudinal_workspace_world import (
    NOW,
    World,
    make_world,
    protected_tokens,
)
from tests.web.test_loopback_server import _exchange, _store
from traceback_runner.web import server as server_module
from traceback_runner.web.explorer import (
    CanonicalExplorerArtifactRepository,
    CatalogAuthorityIndex,
    IntegratedExplorerSource,
)
from traceback_runner.web.longitudinal import (
    LongitudinalExplorerSource,
)
from traceback_runner.web.server import RunningLocalWebService

STATIC = Path("traceback_runner/web/static")
HARNESS = Path("tests/web/longitudinal_dom_harness.js")
DENIED = {"error": {"code": "permission_denied"}}
PREFIX = "/api/v1/longitudinal/"
NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node is not installed")


def _coordinator(world: World) -> CompositeAuthorityCoordinator:
    return CompositeAuthorityCoordinator(
        linkage_store=world.linkage,
        record_history_store=world.history,
        cohort_registry=world.cohort,
        reader_registry=world.reader,
        record_catalog=world.records,
        result_catalog=world.results,
        result_trust_registry=world.trust,
        source_registry=world.sources,
        decision_registry=world.d03,
        comparison_registry=world.d07,
        d09_registry=world.d09,
        d10_registry=world.d10,
        family_source_registry=world.family,
        anchor_registry=world.anchors,
        projection_registry=world.projections,
    )


@dataclass
class Env:
    root: Path
    world: World
    saved: LongitudinalComparisonRegistry | None
    service: RunningLocalWebService
    cookie: str
    csrf: str
    closers: list[object] = field(default_factory=list)
    cache: dict[str, object] = field(default_factory=dict)

    @property
    def request_json(self) -> dict[str, object]:
        return json.loads(self.world.request.model_dump_json())

    def get(self, route: str, query: str = "", *, cookie: str | None = None):
        path = PREFIX + route + (f"?{query}" if query else "")
        return _http(self.service, "GET", path, {"Cookie": cookie or self.cookie})

    def post(
        self, route: str, payload: object, *, cookie: str | None = None, csrf=None
    ):
        headers = {
            "Cookie": cookie or self.cookie,
            "Origin": self.service.base_url,
            "X-Traceback-CSRF": csrf or self.csrf,
        }
        return _http(self.service, "POST", PREFIX + route, headers, payload)

    def unbound(self) -> tuple[str, str]:
        return _exchange(self.service, self.service.issue_bootstrap())

    def binder(self):
        return server_module._RUNTIMES[self.service._runtime_id].reader

    def close(self) -> None:
        for item in reversed(self.closers):
            item.close()


def _http(service, method, path, headers=None, payload=None, timeout=180):
    config = service.config
    connection = http.client.HTTPConnection(
        config.bind_host, config.port, timeout=timeout
    )
    body = (
        json.dumps(payload, separators=(",", ":")).encode()
        if payload is not None
        else None
    )
    connection.putrequest(method, path, skip_host=True)
    connection.putheader("Host", config.authority)
    if body is not None:
        connection.putheader("Content-Type", "application/json")
        connection.putheader("Content-Length", str(len(body)))
    for name, value in (headers or {}).items():
        connection.putheader(name, value)
    connection.endheaders(body)
    response = connection.getresponse()
    content = response.read()
    connection.close()
    return response.status, content


def _bind(service, grant_selector: str) -> tuple[str, str]:
    link = service.issue_reader_launch_url(grant_selector)
    values = dict(item.split("=", 1) for item in link.split("#", 1)[1].split("&"))
    cookie, csrf = _exchange(service, values["bootstrap"])
    status, content = _http(
        service,
        "POST",
        "/api/v1/session/reader-launch",
        {"Cookie": cookie, "Origin": service.base_url, "X-Traceback-CSRF": csrf},
        {"launch": values["reader_launch"]},
    )
    assert status == 200, content
    return cookie, csrf


def _make_env(
    root: Path, *, with_registry: bool = True, now=None, extra_scopes=(), scopes=None
) -> Env:
    world = make_world(root / "world")
    closers: list[object] = [world]
    saved = None
    if with_registry:
        saved = LongitudinalComparisonRegistry(
            root / "saved",
            dependency_fence=CompositeAuthorityFence(_coordinator(world)),
        )
        closers.append(saved)
    source = LongitudinalExplorerSource(
        stores=world.stores(),
        measurement_scopes=scopes or (world.extra["scope"], *extra_scopes),
        comparison_registry=saved,
        now=now,
    )
    explorer = IntegratedExplorerSource(
        catalog=world.results,
        authority=CatalogAuthorityIndex(()),
        artifacts=CanonicalExplorerArtifactRepository(()),
        longitudinal=source,
    )
    store, _ = _store(root)
    service = RunningLocalWebService.start(
        store=store,
        state_directory=root / "state",
        explorer=explorer,
        reader_registry=world.reader,
    )
    closers.append(service)
    cookie, csrf = _bind(service, world.grant.payload.grant_selector)
    return Env(root, world, saved, service, cookie, csrf, closers)


def shared_env(tmp_path_factory: pytest.TempPathFactory):
    """Generator for one read-only environment shared by a module's tests."""

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(reader_module, "_PROCESS_PROFILE", {})
        value = _make_env(tmp_path_factory.mktemp("e12-browser"))
        try:
            yield value
        finally:
            value.close()


def fresh_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Generator for one per-test environment that a test may mutate."""
    monkeypatch.setattr(reader_module, "_PROCESS_PROFILE", {})
    value = _make_env(tmp_path)
    try:
        yield value
    finally:
        value.close()


def _json(content: bytes) -> dict:
    return json.loads(content)


def _cohort_query(env: Env, **extra: str) -> str:
    request = env.world.request
    values = {
        "cohort_selector_id": request.cohort_selector_id,
        "cohort_version": str(request.cohort_version),
        "family": request.measurement.family.value,
        "quantity_id": request.measurement.quantity_id,
        "unit": request.measurement.unit,
    }
    values.update(extra)
    return "&".join(f"{key}={value}" for key, value in values.items())


def _workspace(env: Env, **filters) -> dict:
    key = json.dumps(filters, sort_keys=True)
    cached = env.cache.get(key)
    if cached is not None:
        return cached  # type: ignore[return-value]
    request = env.request_json
    request["filters"].update(filters)
    status, content = env.post("workspace", {"request": request})
    assert status == 200, content
    env.cache[key] = _json(content)
    return env.cache[key]  # type: ignore[return-value]


def _every_route(env: Env):
    """(method, route, payload/query) for every E12 route, well formed."""

    request = env.request_json
    return [
        ("GET", "selectors", ""),
        ("GET", "selectors", _cohort_query(env)),
        ("GET", "diff", _cohort_query(env)),
        ("GET", "saved", ""),
        ("POST", "workspace", {"request": request}),
        ("POST", "source", {"request": request, "row_ordinal": 1}),
        ("POST", "save", {"request": request}),
        (
            "POST",
            "reopen",
            {
                "saved_selector_id": "saved_comparison_" + "1" * 40,
                "comparison_version": 1,
                "stage": "diff",
            },
        ),
    ]


def _call_route(env: Env, method, route, value, *, cookie=None, csrf=None):
    if method == "GET":
        return env.get(route, value, cookie=cookie)
    return env.post(route, value, cookie=cookie, csrf=csrf)


def _assert_denied(status: int, content: bytes) -> None:
    assert status == 403
    assert _json(content) == DENIED


def _record_reads(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []
    original_call = longitudinal_module._call
    original_build = longitudinal_module._PINNED_BUILD

    def call(store, cls, name, *args, **kwargs):
        calls.append(f"{cls.__name__}.{name}")
        return original_call(store, cls, name, *args, **kwargs)

    def build(*args, **kwargs):
        calls.append("build_longitudinal_workspace")
        return original_build(*args, **kwargs)

    monkeypatch.setattr(longitudinal_module, "_call", call)
    monkeypatch.setattr(longitudinal_module, "_PINNED_BUILD", build)
    return calls


def _deny_everywhere(env: Env) -> None:
    for method, route, value in _every_route(env):
        status, content = _call_route(env, method, route, value)
        _assert_denied(status, content)


def _assert_no_protected(env: Env, content: bytes) -> None:
    text = content.decode()
    tokens = [
        *protected_tokens(env.world),
        env.world.credential.grant_sha256,
        env.world.credential.state_head_sha256,
        env.world.grant.payload.grant_selector,
        env.csrf,
        env.cookie.split("=", 1)[1],
    ]
    for token in tokens:
        for encoded in (
            token,
            token.upper(),
            token.encode().hex(),
            base64.b64encode(token.encode()).decode(),
        ):
            assert encoded not in text, token


def _save(env: Env, request=None):
    return env.post("save", {"request": request or env.request_json})


def _object_files(env: Env) -> dict[str, bytes]:
    objects = env.saved.root / "objects"  # type: ignore[union-attr]
    return {path.name: path.read_bytes() for path in sorted(objects.iterdir())}


def _reopen(env: Env, receipt: dict, stage: str):
    return env.post(
        "reopen",
        {
            "saved_selector_id": receipt["saved_selector_id"],
            "comparison_version": receipt["comparison_version"],
            "stage": stage,
        },
    )


def _register_cohort_v2(world: World) -> None:
    """Register version 2 of the selected cohort without its last member."""

    from tests.test_cohort_manifest import _authority as _provider_authority
    from tests.test_cohort_manifest import _manifest

    manifest = world.manifest
    created = manifest.created_at + timedelta(seconds=1)
    if created > NOW:
        vars(world.linkage)["_time_source"].advance_to(created)
    world.cohort.register(
        _manifest(
            _provider_authority(world.linkage.active_snapshot()),
            manifest.members[:-1],
            version=2,
            previous_manifest_sha256=cohort_manifest_sha256(manifest),
            created_at=created,
            measurement_anchor=manifest.measurement_anchor,
        ).model_copy(update={"policies": manifest.policies})
    )


class _Layout(HTMLParser):
    """Ids, tags and parents of the longitudinal section in index.html."""

    VOID = frozenset({"input", "meta", "link", "br", "img"})

    def __init__(self) -> None:
        super().__init__()
        self.stack: list[tuple[str, str | None]] = []
        self.elements: list[dict] = []
        self.checkboxes: list[dict] = []
        self.order: list[str] = []
        self.inside = False
        self.buttons: list[dict] = []

    def _parent(self) -> str | None:
        for _, element_id in reversed(self.stack):
            if element_id is not None:
                return element_id
        return None

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        element_id = values.get("id")
        if element_id == "longitudinal":
            self.inside = True
        if self.inside:
            if element_id:
                self.elements.append(
                    {
                        "id": element_id,
                        "tag": tag,
                        "parent": self._parent()
                        if element_id != "longitudinal"
                        else None,
                        "hidden": "hidden" in values,
                        "disabled": "disabled" in values,
                        "attrs": values,
                    }
                )
                self.order.append(element_id)
            if tag == "input" and values.get("type") == "checkbox":
                self.checkboxes.append(
                    {"name": values["name"], "value": values["value"]}
                )
            if tag == "button":
                self.buttons.append(values)
        if tag not in self.VOID:
            self.stack.append((tag, element_id))

    def handle_endtag(self, tag):
        while self.stack:
            open_tag, element_id = self.stack.pop()
            if element_id == "longitudinal":
                self.inside = False
            if open_tag == tag:
                break


def _layout() -> _Layout:
    parser = _Layout()
    parser.feed((STATIC / "index.html").read_text())
    return parser


def _luminance(hex_color: str) -> float:
    value = hex_color.lstrip("#")
    channels = [int(value[i : i + 2], 16) / 255 for i in (0, 2, 4)]
    linear = [
        c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels
    ]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _contrast(foreground: str, background: str) -> float:
    high, low = sorted((_luminance(foreground), _luminance(background)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def _lg_rules() -> list[tuple[str, str]]:
    css = (STATIC / "styles.css").read_text()
    css = css[css.index("/* E12 longitudinal view.") :]
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)
    flat = re.sub(r"@media[^{]*\{", "", css)
    return [
        (sel.strip(), body) for sel, body in re.findall(r"([^{}]+)\{([^{}]*)\}", flat)
    ]


def _run_harness(tmp_path: Path, scenario: dict) -> dict:
    path = tmp_path / "scenario.json"
    path.write_text(json.dumps({"script": str(STATIC / "longitudinal.js"), **scenario}))
    result = subprocess.run(
        [NODE, str(HARNESS), str(path)],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _variant(workspace: dict, rows: list[dict], segments: list[dict]) -> dict:
    return {
        **workspace,
        "rows": rows,
        "segments": segments,
        "total_row_count": len(rows),
    }


def _comparison_row(
    base: dict, ordinal: int, timepoint: int, offset: int, value: float
) -> dict:
    comparison = dict(base["comparison"])
    comparison.update(
        {"member_value": value, "delta": value - comparison["anchor_value"]}
    )
    comparison["member_uncertainty_lower"] = value - 0.01
    comparison["member_uncertainty_upper"] = value + 0.01
    return {
        **base,
        "row_ordinal": ordinal,
        "timepoint_ordinal": timepoint,
        "offset_seconds": offset,
        "comparison": comparison,
    }


def _segment(a: dict, b: dict) -> dict:
    return {
        "from_row_ordinal": a["row_ordinal"],
        "to_row_ordinal": b["row_ordinal"],
        "from_timepoint_ordinal": a["timepoint_ordinal"],
        "to_timepoint_ordinal": b["timepoint_ordinal"],
        "from_comparison_sha256": a["comparison"]["comparison_sha256"]
        if a.get("comparison")
        else None,
        "to_comparison_sha256": b["comparison"]["comparison_sha256"],
        "from_delta": a["comparison"]["delta"] if a.get("comparison") else None,
        "to_delta": b["comparison"]["delta"],
        "anchor_relative": True,
    }


def longitudinal_action(outcome: str) -> str:
    return {
        "equivalent": "use_direct_comparison",
        "qualified_compatible": "use_qualified_comparison",
        "requires_reanalysis": "request_reanalysis",
        "registered_bridge": "review_registered_bridge",
        "incompatible": "start_separate_series",
        "unknown": "resolve_unknown_inputs",
    }[outcome]


def _controller_layout() -> dict:
    layout = _layout()
    return {
        "elements": [
            {k: item[k] for k in ("id", "tag", "parent", "hidden", "disabled")}
            for item in layout.elements
        ],
        "checkboxes": layout.checkboxes,
    }


def _journey_responses(env: Env) -> dict:
    step1 = _json(env.get("selectors")[1])
    step2 = _json(env.get("selectors", _cohort_query(env))[1])
    step3 = _json(
        env.get(
            "selectors",
            _cohort_query(
                env,
                anchor_policy_selector_id=env.world.request.anchor_policy_selector_id,
                anchor_policy_version="1",
            ),
        )[1]
    )
    diff = _json(env.get("diff", _cohort_query(env))[1])
    detail = _json(
        env.post("source", {"request": env.request_json, "row_ordinal": 3})[1]
    )
    return {
        "/api/v1/longitudinal/selectors": [
            {"status": 200, "payload": step1},
            {"status": 200, "payload": step2},
            {"status": 200, "payload": step3},
        ],
        "/api/v1/longitudinal/saved": [
            {"status": 200, "payload": {"records": [], "save": {"state": "available"}}}
        ],
        "/api/v1/longitudinal/diff": [{"status": 200, "payload": diff}],
        "/api/v1/longitudinal/workspace": [{"status": 200, "payload": _workspace(env)}],
        "/api/v1/longitudinal/source": [{"status": 200, "payload": detail}],
    }


def _journey_steps(env: Env, width: int) -> list[dict]:
    request = env.world.request
    return [
        {"do": "bind", "snapshot": "bound"},
        {"do": "select", "id": "lg-cohort", "value": f"{request.cohort_selector_id}|1"},
        {"do": "select", "id": "lg-measurement", "value": "0"},
        {
            "do": "select",
            "id": "lg-anchor-policy",
            "value": f"{request.anchor_policy_selector_id}|1",
            "snapshot": "selectors",
        },
        {"do": "select", "id": "lg-anchor", "value": request.anchor_selector_id},
        {
            "do": "select",
            "id": "lg-d09",
            "value": f"{request.d09_policy_selector_id}|1",
        },
        {"do": "submit", "snapshot": "submit-before-diff"},
        {"do": "click", "id": "lg-show-diff", "snapshot": "diff"},
        {"do": "submit", "snapshot": "results"},
        {"do": "width", "value": width},
        {"do": "details", "index": 2, "snapshot": "drawer"},
        {"do": "key", "key": "Tab", "focus": "lg-drawer-close", "snapshot": "tab"},
        {"do": "key", "key": "Escape", "snapshot": "closed"},
    ]
