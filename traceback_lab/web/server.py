"""The Lab's loopback web server (D12, D22, D34).

Separate from the signed ``traceback serve``; it reuses the signed session
machinery in full: ``build_loopback_config``, ``BootstrapBroker`` and
``LocalWebBoundary``.  ``lab serve`` prints a one-time
``http://127.0.0.1:<port>/#bootstrap=<code>`` link; the page exchanges the
code at ``POST /api/v1/session/bootstrap`` for an HttpOnly SameSite=Strict
cookie.  Every ``/api/*`` request goes through ``boundary.authorize`` (Host,
then session); static assets through ``authorize_public_asset``.  The API is
GET-only apart from the bootstrap exchange, so it has no CSRF surface.

The server never touches the writer lock: ``/api/status`` reads
``ROOT/lab/generation`` and ``ROOT/lab/active.json`` only.  Requests never
carry paths: ids are matched against regexes and resolved through the store.
"""

from __future__ import annotations

import json
import mimetypes
import re
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from traceback_runner.web.auth import (
    BootstrapBroker,
    BoundaryDenied,
    BrowserRequest,
    LocalWebBoundary,
    build_loopback_config,
)

from ..store import LabStore
from . import api

STATIC = Path(__file__).resolve().parent / "static"
BASE_CSS = Path(__file__).resolve().parents[2] / "traceback_runner" / "web" / "static" / "styles.css"
CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
)
SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": CSP,
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
}
_COOKIE_NAME = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
_COOKIE_VALUE = re.compile(r"^[A-Za-z0-9_-]{0,256}$")
ID12 = r"[0-9a-f]{12,64}"
ALG = r"[a-z0-9-]{1,40}"
PANEL = r"[a-z0-9-]{1,64}"
DERIVED = r"(?:full|[0-9.e-]{1,20}|split-[AB]|cna-[0-9.e-]{1,20})"
_ROUTES = (
    ("status", re.compile(r"^/api/status$")),
    ("samples", re.compile(r"^/api/samples$")),
    ("sample", re.compile(rf"^/api/samples/({ID12})$")),
    ("artifact", re.compile(rf"^/api/samples/({ID12})/artifact/({DERIVED})/({ALG})/({PANEL})$")),
)
MAX_BODY = 4096


def _assets() -> dict[str, tuple[str, bytes]]:
    assets: dict[str, tuple[str, bytes]] = {}
    for path in sorted(STATIC.rglob("*")):
        if not path.is_file() or path.name.startswith("."):
            continue
        relative = path.relative_to(STATIC).as_posix()
        kind = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if kind.startswith("text/") or kind in ("application/javascript",):
            kind += "; charset=utf-8"
        assets[f"/assets/{relative}"] = (kind, path.read_bytes())
    assets["/assets/base.css"] = ("text/css; charset=utf-8", BASE_CSS.read_bytes())
    assets["/"] = ("text/html; charset=utf-8", (STATIC / "index.html").read_bytes())
    return assets


@dataclass
class LabWebApp:
    store: LabStore
    boundary: LocalWebBoundary
    assets: Mapping[str, tuple[str, bytes]]
    api: api.LabApi


class _Handler(BaseHTTPRequestHandler):
    server_version = "TracebackLab"
    sys_version = ""
    app: LabWebApp

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - silence default logging
        return

    # -- helpers ---------------------------------------------------------------
    def _single(self, name: str) -> str | None:
        values = self.headers.get_all(name, failobj=[])
        return values[0] if len(values) == 1 else None

    def _session_token(self) -> str | None:
        values = self.headers.get_all("Cookie", failobj=[])
        if len(values) != 1:
            return None
        cookies: dict[str, str] = {}
        for segment in values[0].split(";"):
            item = segment.strip()
            if not item or item.count("=") != 1:
                return None
            name, value = item.split("=", 1)
            if not _COOKIE_NAME.fullmatch(name) or not _COOKIE_VALUE.fullmatch(value) or name in cookies:
                return None
            cookies[name] = value
        token = cookies.get("traceback_session")
        if token is None or not 43 <= len(token) <= 128:
            return None
        return token

    def _request(self, path: str) -> BrowserRequest:
        forwarded = tuple(
            sorted(name for name in self.headers if name.casefold() == "forwarded" or name.casefold().startswith("x-forwarded-"))
        )
        return BrowserRequest(
            method=self.command,
            path=path,
            host=self._single("Host") or "",
            origin=self._single("Origin"),
            session_token=self._session_token(),
            csrf_token=self._single("X-Traceback-CSRF"),
            forwarded_headers=forwarded,
        )

    def _send(self, status: int, content_type: str, body: bytes, extra: Mapping[str, str] | None = None) -> None:
        try:
            self.send_response(status)
            for name, value in SECURITY_HEADERS.items():
                self.send_header(name, value)
            for name, value in (extra or {}).items():
                self.send_header(name, value)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, OSError):
            self.close_connection = True

    def _json(self, status: int, payload: Any, extra: Mapping[str, str] | None = None) -> None:
        body = json.dumps(payload, separators=(",", ":"), allow_nan=False).encode("utf-8")
        self._send(status, "application/json; charset=utf-8", body, extra)

    # -- verbs -------------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802 - http.server API
        raw = self.path
        parsed = urlsplit(raw)
        path = parsed.path
        request = self._request(path)
        try:
            if path in self.app.assets and not path.startswith("/api/"):
                self.app.boundary.authorize_public_asset(request)
                if parsed.query:
                    raise BoundaryDenied(403, "TBX-AUTH-003")
                kind, body = self.app.assets[path]
                self._send(200, kind, body)
                return
            if not path.startswith("/api/"):
                self.app.boundary.authorize_public_asset(request)
                self._json(404, {"error": {"code": "TBX-LAB-WEB-404"}})
                return
            self.app.boundary.authorize(request)
            if parsed.query:
                # No query string ever becomes a parameter, let alone a path (D34).
                self._json(400, {"error": {"code": "TBX-LAB-WEB-400"}})
                return
            for name, pattern in _ROUTES:
                match = pattern.fullmatch(path)
                if match is None:
                    continue
                if name == "status":
                    self._json(200, self.app.api.status())
                elif name == "samples":
                    self._json(200, self.app.api.samples())
                elif name == "sample":
                    payload = self.app.api.sample(match.group(1))
                    self._json(200 if payload.get("found") else 404, payload)
                else:
                    found = self.app.api.artifact(*match.groups())
                    if found is None:
                        self._json(404, {"error": {"code": "TBX-LAB-WEB-ARTIFACT"}})
                    else:
                        self._send(200, "application/json; charset=utf-8", found)
                return
            self._json(404, {"error": {"code": "TBX-LAB-WEB-404"}})
        except BoundaryDenied as denied:
            self._json(denied.status_code, {"error": {"code": denied.code}})
        except Exception:  # noqa: BLE001 - never leak internals
            self._json(500, {"error": {"code": "TBX-LAB-WEB-500"}})

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def do_POST(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        request = self._request(self.path)
        try:
            if path != "/api/v1/session/bootstrap":
                self.app.boundary.authorize(request)
                self._json(405, {"error": {"code": "TBX-LAB-WEB-405"}})
                return
            self.app.boundary.authorize_bootstrap_request(request)
            if self.headers.get_all("Transfer-Encoding", failobj=[]):
                raise ValueError("transfer encoding is not accepted")
            if self._single("Content-Type") != "application/json":
                raise ValueError("content type must be application/json")
            length_text = self._single("Content-Length") or ""
            if not length_text.isdigit() or int(length_text) > MAX_BODY:
                raise ValueError("bad content length")
            payload = json.loads(self.rfile.read(int(length_text)) or b"{}")
            if not isinstance(payload, dict) or set(payload) != {"bootstrap"} or not isinstance(payload["bootstrap"], str):
                raise ValueError("bootstrap request shape is invalid")
            grant = self.app.boundary.broker.exchange(payload["bootstrap"], authority=self.app.boundary.config.authority)
            self._json(
                200,
                {"csrf_token": grant.csrf_token, "session_kind": grant.kind},
                {"Set-Cookie": f"{grant.cookie_name}={grant.session_token}; Path=/; HttpOnly; SameSite=Strict"},
            )
        except BoundaryDenied as denied:
            self._json(denied.status_code, {"error": {"code": denied.code}})
        except ValueError:
            self._json(400, {"error": {"code": "TBX-LAB-WEB-400"}})

    def do_PUT(self) -> None:  # noqa: N802
        self._json(405, {"error": {"code": "TBX-LAB-WEB-405"}})

    do_DELETE = do_PUT
    do_PATCH = do_PUT


class RunningLabServer:
    """A started server: ``base_url``, ``issue_link()``, ``close()``."""

    def __init__(self, store: LabStore, *, port: int = 0) -> None:
        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
        self.httpd.daemon_threads = True
        actual = self.httpd.server_address[1]
        boundary = LocalWebBoundary(build_loopback_config(port=actual), BootstrapBroker())
        app = LabWebApp(store=store, boundary=boundary, assets=_assets(), api=api.LabApi(store))
        handler = type("LabHandler", (_Handler,), {"app": app})
        self.httpd.RequestHandlerClass = handler
        self.app = app
        self.port = actual
        self.base_url = f"http://127.0.0.1:{actual}"
        self._thread = threading.Thread(target=self.httpd.serve_forever, name="lab-serve", daemon=True)
        self._thread.start()

    def issue_link(self) -> str:
        code = self.app.boundary.issue_bootstrap()
        return f"{self.base_url}/{BootstrapBroker.launch_fragment(code)}"

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self._thread.join(timeout=5)

    def __enter__(self) -> RunningLabServer:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()



__all__ = ["CSP", "RunningLabServer", "SECURITY_HEADERS"]
