"""Packaged, loopback-only HTTP adapter for the local operator projection."""

from __future__ import annotations

import http.server
import json
import os
import re
import secrets
import socket
import stat
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from http.cookies import SimpleCookie
from importlib.resources import files
from pathlib import Path
from types import TracebackType
from typing import Any, Self
from urllib.parse import urlsplit

from traceback_runner.serialization import canonical_json_bytes
from traceback_runner.store import JobStore

from .api import ApiProblem, LocalApiKernel
from .auth import (
    BootstrapBroker,
    BoundaryDenied,
    BrowserRequest,
    LocalWebBoundary,
    build_loopback_config,
)
from .contracts import ProblemDetail, ProblemOwner
from .source import JobStoreProjectionSource

MAX_REQUEST_BYTES = 4096
STATE_DIRECTORY_MODE = 0o700
STATE_FILE_MODE = 0o600
_JOB_ROUTE = re.compile(r"^/api/v1/jobs/(job_[0-9a-f]{32})$")
_SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": (
        "default-src 'self'; connect-src 'self'; img-src 'self' data:; "
        "script-src 'self'; style-src 'self'; object-src 'none'; "
        "base-uri 'none'; frame-ancestors 'none'"
    ),
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
}


class LocalWebServerError(RuntimeError):
    """The packaged local server could not establish its security boundary."""


def _open_state_directory(path: Path) -> int:
    try:
        path.mkdir(mode=STATE_DIRECTORY_MODE, parents=True, exist_ok=True)
        metadata = path.lstat()
    except OSError as exc:
        raise LocalWebServerError("local web state directory is unavailable") from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != STATE_DIRECTORY_MODE
        or metadata.st_uid != os.geteuid()
    ):
        raise LocalWebServerError(
            "local web state directory must be owner-only mode 0700"
        )
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise LocalWebServerError(
            "local web state directory could not be pinned"
        ) from exc
    pinned = os.fstat(descriptor)
    if (pinned.st_dev, pinned.st_ino) != (metadata.st_dev, metadata.st_ino):
        os.close(descriptor)
        raise LocalWebServerError("local web state directory changed during open")
    return descriptor


def _write_instance_state(directory_fd: int, payload: dict[str, object]) -> None:
    temporary = f".instance-{secrets.token_hex(8)}.tmp"
    content = canonical_json_bytes(payload) + b"\n"
    descriptor: int | None = None
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            STATE_FILE_MODE,
            dir_fd=directory_fd,
        )
        os.fchmod(descriptor, STATE_FILE_MODE)
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(content)
            stream.flush()
            os.fsync(descriptor)
        os.replace(
            temporary,
            "instance.json",
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        metadata = os.stat("instance.json", dir_fd=directory_fd, follow_symlinks=False)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != STATE_FILE_MODE
            or metadata.st_uid != os.geteuid()
            or metadata.st_nlink != 1
        ):
            raise LocalWebServerError("local web state file is not owner-only")
        os.fsync(directory_fd)
    except OSError as exc:
        raise LocalWebServerError("local web state could not be published") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            os.unlink(temporary, dir_fd=directory_fd)
        except FileNotFoundError:
            pass


def _packaged_assets() -> dict[str, tuple[str, bytes]]:
    root = files("traceback_runner.web").joinpath("static")
    assets = {
        "/": ("text/html; charset=utf-8", root.joinpath("index.html").read_bytes()),
        "/assets/app.js": (
            "text/javascript; charset=utf-8",
            root.joinpath("app.js").read_bytes(),
        ),
        "/assets/styles.css": (
            "text/css; charset=utf-8",
            root.joinpath("styles.css").read_bytes(),
        ),
    }
    for _, content in assets.values():
        lowered = content.lower()
        if b"http://" in lowered or b"https://" in lowered or b"//cdn" in lowered:
            raise LocalWebServerError("packaged web assets reference external content")
    return assets


@dataclass(frozen=True, slots=True)
class _Application:
    kernel: LocalApiKernel
    boundary: LocalWebBoundary
    assets: dict[str, tuple[str, bytes]]


class _LoopbackHttpServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False
    application: _Application


class _LoopbackHttpServerV6(_LoopbackHttpServer):
    address_family = socket.AF_INET6


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "TracebackLocal"
    sys_version = ""

    @property
    def application(self) -> _Application:
        return self.server.application  # type: ignore[attr-defined,no-any-return]

    def log_message(self, format: str, *args: object) -> None:
        del format, args

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(5)

    def _single_header(self, name: str) -> str | None:
        values = self.headers.get_all(name, failobj=[])
        return values[0] if len(values) == 1 else None

    def _send(self, status_code: int, content_type: str, content: bytes) -> None:
        self.send_response(status_code)
        for name, value in _SECURITY_HEADERS.items():
            self.send_header(name, value)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(content)

    def _json(self, status_code: int, payload: object) -> None:
        self._send(
            status_code,
            "application/json; charset=utf-8",
            canonical_json_bytes(payload) + b"\n",
        )

    def _deny(self, error: BoundaryDenied) -> None:
        self._json(error.status_code, {"error": {"code": error.code}})

    def _session_token(self) -> str | None:
        values = self.headers.get_all("Cookie", failobj=[])
        if len(values) != 1:
            return None
        cookie = SimpleCookie()
        try:
            cookie.load(values[0])
        except Exception:
            return None
        morsel = cookie.get("traceback_session")
        return morsel.value if morsel is not None else None

    def _request(self, path: str) -> BrowserRequest:
        forwarded = tuple(
            sorted(
                name
                for name in self.headers.keys()
                if name.casefold() == "forwarded"
                or name.casefold().startswith("x-forwarded-")
            )
        )
        return BrowserRequest(
            method=self.command,
            path=path,
            host=self._single_header("Host") or "",
            origin=self._single_header("Origin"),
            session_token=self._session_token(),
            csrf_token=self._single_header("X-Traceback-CSRF"),
            forwarded_headers=forwarded,
        )

    def _body_json(self) -> dict[str, Any]:
        if self.headers.get_all("Transfer-Encoding", failobj=[]):
            raise ValueError("transfer encoding is not accepted")
        if self._single_header("Content-Type") != "application/json":
            raise ValueError("request content type must be application/json")
        raw_length = self._single_header("Content-Length") or ""
        if not raw_length.isascii() or not raw_length.isdigit():
            raise ValueError("request content length is required")
        length = int(raw_length)
        if not 1 <= length <= MAX_REQUEST_BYTES:
            raise ValueError("request body exceeds the local API bound")
        payload = json.loads(self.rfile.read(length))
        if not isinstance(payload, dict):
            raise ValueError("request body must be an object")
        return payload

    def do_HEAD(self) -> None:
        self.do_GET()

    def do_GET(self) -> None:
        parsed = urlsplit(self.path)
        if parsed.query or parsed.fragment:
            self._json(404, {"error": {"code": "TBX-WEB-404"}})
            return
        asset = self.application.assets.get(parsed.path)
        if asset is not None:
            try:
                self.application.boundary.authorize_public_asset(
                    self._request(parsed.path)
                )
            except BoundaryDenied as exc:
                self._deny(exc)
                return
            self._send(200, asset[0], asset[1])
            return
        request = self._request(parsed.path)
        try:
            if parsed.path == "/api/v1/jobs":
                jobs = self.application.kernel.list_jobs(request)
                self._json(
                    200, {"jobs": [item.model_dump(mode="json") for item in jobs]}
                )
                return
            match = _JOB_ROUTE.fullmatch(parsed.path)
            if match is not None:
                job = self.application.kernel.get_job(request, match.group(1))
                self._json(200, job.model_dump(mode="json"))
                return
        except BoundaryDenied as exc:
            self._deny(exc)
            return
        except ApiProblem as exc:
            self._json(exc.status_code, exc.problem.model_dump(mode="json"))
            return
        self._json(404, {"error": {"code": "TBX-WEB-404"}})

    def do_POST(self) -> None:
        parsed = urlsplit(self.path)
        if parsed.query or parsed.fragment:
            self._json(404, {"error": {"code": "TBX-WEB-404"}})
            return
        request = self._request(parsed.path)
        try:
            if parsed.path == "/api/v1/session/bootstrap":
                payload = self._body_json()
                if set(payload) != {"bootstrap"} or not isinstance(
                    payload["bootstrap"], str
                ):
                    raise ValueError("bootstrap request shape is invalid")
                grant = self.application.boundary.exchange_bootstrap(
                    request, payload["bootstrap"]
                )
                content = canonical_json_bytes({"csrf_token": grant.csrf_token}) + b"\n"
                self.send_response(200)
                for name, value in _SECURITY_HEADERS.items():
                    self.send_header(name, value)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(content)))
                self.send_header(
                    "Set-Cookie",
                    f"{grant.cookie_name}={grant.session_token}; Path=/; HttpOnly; SameSite=Strict",
                )
                self.end_headers()
                self.wfile.write(content)
                return
            if parsed.path == "/api/v1/session/validate":
                self.application.boundary.authorize(request)
                self._json(200, {"authorized": True})
                return
        except BoundaryDenied as exc:
            self._deny(exc)
            return
        except (ValueError, json.JSONDecodeError):
            self._json(400, {"error": {"code": "TBX-WEB-400"}})
            return
        self._json(404, {"error": {"code": "TBX-WEB-404"}})


@dataclass(slots=True)
class RunningLocalWebService:
    """A started local service with restart-scoped in-memory credentials."""

    server: _LoopbackHttpServer
    thread: threading.Thread
    boundary: LocalWebBoundary
    bootstrap_code: str
    state_directory_fd: int
    instance_id: str

    @classmethod
    def start(
        cls,
        *,
        store: JobStore,
        state_directory: Path,
        ipv6: bool = False,
    ) -> Self:
        host = "::1" if ipv6 else "127.0.0.1"
        server_type = _LoopbackHttpServerV6 if ipv6 else _LoopbackHttpServer
        try:
            server = server_type((host, 0), _Handler)
        except OSError as exc:
            raise LocalWebServerError(
                "literal loopback listener is unavailable"
            ) from exc
        port = int(server.server_address[1])
        config = build_loopback_config(port=port, ipv6=ipv6)
        broker = BootstrapBroker()
        boundary = LocalWebBoundary(config, broker)
        problem = ProblemDetail(
            code="TBX-WEB-404",
            problem="Requested local object is unavailable",
            cause="The object is unavailable in this local session",
            fix="Refresh the local queue",
            docs_path="docs/OPERATOR-GUIDE.md",
            owner=ProblemOwner.OPERATOR,
            retryable=False,
            correlation_id="cor_0000000000000000",
            preserved_work="Existing verified work is unchanged",
            repeated_work="No work was repeated",
        )
        source = JobStoreProjectionSource(store)
        kernel = LocalApiKernel(
            boundary=boundary,
            source=source,
            not_found_problem=problem,
        )
        server.application = _Application(kernel, boundary, _packaged_assets())
        state_fd: int | None = None
        try:
            state_fd = _open_state_directory(state_directory)
            instance_id = f"instance_{secrets.token_hex(16)}"
            _write_instance_state(
                state_fd,
                {
                    "bind_host": config.bind_host,
                    "capability_enabled": False,
                    "instance_id": instance_id,
                    "port": config.port,
                    "schema_version": "traceback.local-web-instance.v1",
                    "started_at": datetime.now(UTC).isoformat(timespec="seconds"),
                },
            )
            bootstrap_code = boundary.issue_bootstrap()
            thread = threading.Thread(
                target=server.serve_forever,
                name="traceback-local-web",
                daemon=True,
            )
            thread.start()
            return cls(
                server=server,
                thread=thread,
                boundary=boundary,
                bootstrap_code=bootstrap_code,
                state_directory_fd=state_fd,
                instance_id=instance_id,
            )
        except BaseException:
            server.server_close()
            if state_fd is not None:
                os.close(state_fd)
            raise

    @property
    def base_url(self) -> str:
        return self.boundary.config.allowed_origins[0]

    @property
    def launch_url(self) -> str:
        return f"{self.base_url}/{self.boundary.broker.launch_fragment(self.bootstrap_code)}"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        os.close(self.state_directory_fd)

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc, traceback
        self.close()


__all__ = ["LocalWebServerError", "RunningLocalWebService"]
