"""Packaged, loopback-only HTTP adapter for the local operator projection."""

from __future__ import annotations

import errno
import fcntl
import hashlib
import http.server
import ipaddress
import json
import os
import re
import secrets
import socket
import stat
import threading
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path
from types import TracebackType
from typing import Any, Self
from urllib.parse import parse_qs, urlsplit

from evidence_inspector.reader_authorization_registry import (
    ReaderAuthorizationDenied,
    ReaderAuthorizationRegistry,
)
from evidence_inspector.result_catalog import CatalogQuery
from traceback_runner.serialization import canonical_json_bytes
from traceback_runner.store import JobStore

from .api import ApiProblem, LocalApiKernel
from .auth import (
    BootstrapBroker,
    BoundaryDenied,
    BrowserRequest,
    LocalWebBoundary,
    LoopbackServerConfig,
    build_loopback_config,
)
from .contracts import ProblemDetail, ProblemOwner, validate_public_projection
from .explorer import (
    IntegratedExplorerSource,
    prepare_explorer_comparison_response,
    prepare_explorer_document_response,
)
from .longitudinal import (
    GET_ROUTE_PATHS as _LONGITUDINAL_GET_ROUTES,
)
from .longitudinal import (
    POST_ROUTE_PATHS as _LONGITUDINAL_POST_ROUTES,
)
from .longitudinal import (
    handle_longitudinal_route,
)
from .reader_session import ReaderSessionBinder
from .source import JobStoreProjectionSource

MAX_REQUEST_BYTES = 4096
MAX_REQUEST_HEADERS = 32
MAX_REQUEST_HEADER_BYTES = 16 * 1024
MAX_HTTP_WORKERS = 16
REQUEST_TIMEOUT_SECONDS = 2
STATE_DIRECTORY_MODE = 0o700
STATE_FILE_MODE = 0o600
_STABLE_LOCK_ROOT = Path("/tmp").resolve(strict=True)
_JOB_ROUTE = re.compile(r"^/api/v1/jobs/(job_[0-9a-f]{32})$")
_EXPLORER_RESULT_ROUTE = re.compile(r"^/api/v1/explorer/results/(result_[0-9a-f]{40})$")
_EXPLORER_COMPARE_ROUTE = "/api/v1/explorer/compare"
_READER_LAUNCH_ROUTE = "/api/v1/session/reader-launch"
_COOKIE_NAME = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
_COOKIE_VALUE = re.compile(r"^[A-Za-z0-9_-]{0,256}$")
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


def _build_explorer_dispatch(
    source_type: type[IntegratedExplorerSource],
    query_method: Callable[..., object],
    get_method: Callable[..., object],
    compare_method: Callable[..., object],
) -> tuple[Callable[..., object], Callable[..., object], Callable[..., object]]:
    """Capture exact source methods for the installed HTTP boundary."""

    expected = {
        "compare": compare_method,
        "get": get_method,
        "query": query_method,
    }

    def checked(source: IntegratedExplorerSource) -> None:
        if type(source) is not source_type or any(
            source_type.__dict__.get(name) is not method
            for name, method in expected.items()
        ):
            raise TypeError("integrated explorer source class changed")

    def query(source: IntegratedExplorerSource, value: object) -> object:
        checked(source)
        return query_method(source, value)

    def get(source: IntegratedExplorerSource, result_id: str) -> object:
        checked(source)
        return get_method(source, result_id)

    def compare(source: IntegratedExplorerSource, left: str, right: str) -> object:
        checked(source)
        return compare_method(source, left, right)

    return query, get, compare


_EXPLORER_DISPATCH = _build_explorer_dispatch(
    IntegratedExplorerSource,
    IntegratedExplorerSource.query,
    IntegratedExplorerSource.get,
    IntegratedExplorerSource.compare,
)


class LocalWebServerError(RuntimeError):
    """The packaged local server could not establish its security boundary."""


@dataclass(frozen=True, slots=True)
class _StartupAnchor:
    parent_path: Path
    parent_fd: int
    parent_mode: int
    state_parent_path: Path
    state_parent_fd: int
    state_parent_mode: int
    name: str
    descriptor: int


def _require_startup_anchor(anchor: _StartupAnchor) -> None:
    try:
        named_parent = anchor.parent_path.lstat()
        pinned_parent = os.fstat(anchor.parent_fd)
        named_anchor = os.stat(
            anchor.name, dir_fd=anchor.parent_fd, follow_symlinks=False
        )
        pinned_anchor = os.fstat(anchor.descriptor)
        named_state_parent = anchor.state_parent_path.lstat()
        pinned_state_parent = os.fstat(anchor.state_parent_fd)
    except OSError as exc:
        raise LocalWebServerError("local web startup anchor is unavailable") from exc
    if (
        not stat.S_ISDIR(named_parent.st_mode)
        or named_parent.st_uid != 0
        or stat.S_IMODE(named_parent.st_mode) != anchor.parent_mode
        or (named_parent.st_dev, named_parent.st_ino)
        != (pinned_parent.st_dev, pinned_parent.st_ino)
    ):
        raise LocalWebServerError("local web startup parent identity changed")
    if (
        not stat.S_ISREG(named_anchor.st_mode)
        or stat.S_IMODE(named_anchor.st_mode) != STATE_FILE_MODE
        or named_anchor.st_uid != os.geteuid()
        or named_anchor.st_nlink != 1
        or (named_anchor.st_dev, named_anchor.st_ino)
        != (pinned_anchor.st_dev, pinned_anchor.st_ino)
    ):
        raise LocalWebServerError("local web startup anchor identity changed")
    if (
        not stat.S_ISDIR(named_state_parent.st_mode)
        or named_state_parent.st_uid != os.geteuid()
        or stat.S_IMODE(named_state_parent.st_mode) != anchor.state_parent_mode
        or (named_state_parent.st_dev, named_state_parent.st_ino)
        != (pinned_state_parent.st_dev, pinned_state_parent.st_ino)
    ):
        raise LocalWebServerError("local web state parent identity changed")


def _open_startup_anchor(state_directory: Path) -> _StartupAnchor:
    parent_path = _STABLE_LOCK_ROOT
    parent_fd: int | None = None
    state_parent_fd: int | None = None
    descriptor: int | None = None
    parent_locked = False
    anchor_locked = False
    try:
        parent_metadata = parent_path.lstat()
        if (
            not stat.S_ISDIR(parent_metadata.st_mode)
            or parent_metadata.st_uid != 0
            or not parent_metadata.st_mode & stat.S_ISVTX
        ):
            raise LocalWebServerError(
                "local web stable lock root must be a root-owned sticky directory"
            )
        parent_fd = os.open(
            parent_path,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        pinned_parent = os.fstat(parent_fd)
        if (pinned_parent.st_dev, pinned_parent.st_ino) != (
            parent_metadata.st_dev,
            parent_metadata.st_ino,
        ):
            raise LocalWebServerError("local web startup parent changed during open")
        fcntl.flock(parent_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        parent_locked = True
        name_digest = hashlib.sha256(os.fsencode(state_directory)).hexdigest()
        anchor_name = f".traceback-web-{os.geteuid()}-{name_digest[:32]}.lock"
        descriptor = os.open(
            anchor_name,
            os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
            STATE_FILE_MODE,
            dir_fd=parent_fd,
        )
        anchor_metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(anchor_metadata.st_mode)
            or anchor_metadata.st_uid != os.geteuid()
            or anchor_metadata.st_nlink != 1
        ):
            raise LocalWebServerError("local web startup anchor is not private")
        os.fchmod(descriptor, STATE_FILE_MODE)
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        anchor_locked = True
        state_parent_path = state_directory.parent
        state_parent_path.mkdir(mode=STATE_DIRECTORY_MODE, parents=True, exist_ok=True)
        state_parent_metadata = state_parent_path.lstat()
        if (
            not stat.S_ISDIR(state_parent_metadata.st_mode)
            or state_parent_metadata.st_uid != os.geteuid()
            or stat.S_IMODE(state_parent_metadata.st_mode) & 0o022
        ):
            raise LocalWebServerError(
                "local web state parent must be user-owned and not writable by others"
            )
        state_parent_fd = os.open(
            state_parent_path,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        pinned_state_parent = os.fstat(state_parent_fd)
        if (pinned_state_parent.st_dev, pinned_state_parent.st_ino) != (
            state_parent_metadata.st_dev,
            state_parent_metadata.st_ino,
        ):
            raise LocalWebServerError("local web state parent changed during open")
        anchor = _StartupAnchor(
            parent_path=parent_path,
            parent_fd=parent_fd,
            parent_mode=stat.S_IMODE(parent_metadata.st_mode),
            state_parent_path=state_parent_path,
            state_parent_fd=state_parent_fd,
            state_parent_mode=stat.S_IMODE(state_parent_metadata.st_mode),
            name=anchor_name,
            descriptor=descriptor,
        )
        _require_startup_anchor(anchor)
        return anchor
    except BaseException as exc:
        if state_parent_fd is not None:
            os.close(state_parent_fd)
        if anchor_locked and descriptor is not None:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        if descriptor is not None:
            os.close(descriptor)
        if parent_locked and parent_fd is not None:
            fcntl.flock(parent_fd, fcntl.LOCK_UN)
        if parent_fd is not None:
            os.close(parent_fd)
        if isinstance(exc, LocalWebServerError):
            raise
        if isinstance(exc, OSError) and exc.errno in {errno.EACCES, errno.EAGAIN}:
            raise LocalWebServerError("local web service is already running") from exc
        raise LocalWebServerError("local web startup anchor is unavailable") from exc


def _close_startup_anchor(anchor: _StartupAnchor) -> None:
    try:
        try:
            try:
                named = os.stat(
                    anchor.name,
                    dir_fd=anchor.parent_fd,
                    follow_symlinks=False,
                )
                pinned = os.fstat(anchor.descriptor)
                if (named.st_dev, named.st_ino) == (pinned.st_dev, pinned.st_ino):
                    os.unlink(anchor.name, dir_fd=anchor.parent_fd)
                    os.fsync(anchor.parent_fd)
            except FileNotFoundError:
                pass
        finally:
            os.close(anchor.state_parent_fd)
    finally:
        try:
            try:
                fcntl.flock(anchor.descriptor, fcntl.LOCK_UN)
            finally:
                os.close(anchor.descriptor)
        finally:
            try:
                fcntl.flock(anchor.parent_fd, fcntl.LOCK_UN)
            finally:
                os.close(anchor.parent_fd)


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


def _require_named_state_directory(path: Path, directory_fd: int) -> None:
    try:
        named = path.lstat()
        pinned = os.fstat(directory_fd)
    except OSError as exc:
        raise LocalWebServerError("local web state directory is unavailable") from exc
    if (
        not stat.S_ISDIR(named.st_mode)
        or stat.S_IMODE(named.st_mode) != STATE_DIRECTORY_MODE
        or named.st_uid != os.geteuid()
        or (named.st_dev, named.st_ino) != (pinned.st_dev, pinned.st_ino)
    ):
        raise LocalWebServerError("local web state directory identity changed")


def _acquire_instance_lease(directory_fd: int) -> int:
    descriptor: int | None = None
    directory_locked = False
    try:
        fcntl.flock(directory_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        directory_locked = True
        descriptor = os.open(
            "instance.lock",
            os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
            STATE_FILE_MODE,
            dir_fd=directory_fd,
        )
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_nlink != 1
        ):
            os.close(descriptor)
            descriptor = None
            raise LocalWebServerError("local web lease file is not private")
        os.fchmod(descriptor, STATE_FILE_MODE)
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return descriptor
    except OSError as exc:
        if descriptor is not None:
            os.close(descriptor)
        if directory_locked:
            fcntl.flock(directory_fd, fcntl.LOCK_UN)
        if exc.errno in {errno.EACCES, errno.EAGAIN}:
            raise LocalWebServerError("local web service is already running") from exc
        raise LocalWebServerError("local web lease could not be acquired") from exc


def _require_instance_lease(directory_fd: int, lease_fd: int) -> None:
    try:
        named = os.stat("instance.lock", dir_fd=directory_fd, follow_symlinks=False)
        pinned = os.fstat(lease_fd)
    except OSError as exc:
        raise LocalWebServerError("local web lease identity is unavailable") from exc
    if (
        not stat.S_ISREG(named.st_mode)
        or stat.S_IMODE(named.st_mode) != STATE_FILE_MODE
        or named.st_uid != os.geteuid()
        or named.st_nlink != 1
        or (named.st_dev, named.st_ino) != (pinned.st_dev, pinned.st_ino)
    ):
        raise LocalWebServerError("local web lease identity changed")


def _unlink_instance_state(
    directory_fd: int, *, expected_instance_id: str | None = None
) -> None:
    try:
        descriptor = os.open(
            "instance.json",
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=directory_fd,
        )
    except FileNotFoundError:
        return
    except OSError as exc:
        raise LocalWebServerError("local web state file is unsafe") from exc
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != STATE_FILE_MODE
            or metadata.st_uid != os.geteuid()
            or metadata.st_nlink != 1
            or metadata.st_size > MAX_REQUEST_BYTES
        ):
            raise LocalWebServerError("local web state file is unsafe")
        if expected_instance_id is not None:
            payload = json.loads(os.read(descriptor, MAX_REQUEST_BYTES + 1))
            if (
                not isinstance(payload, dict)
                or payload.get("instance_id") != expected_instance_id
            ):
                return
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise LocalWebServerError("local web state file is unsafe") from exc
    finally:
        os.close(descriptor)
    try:
        os.unlink("instance.json", dir_fd=directory_fd)
        os.fsync(directory_fd)
    except FileNotFoundError:
        pass


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
        "/assets/longitudinal.js": (
            "text/javascript; charset=utf-8",
            root.joinpath("longitudinal.js").read_bytes(),
        ),
    }
    for _, content in assets.values():
        lowered = content.lower()
        if b"http://" in lowered or b"https://" in lowered or b"//cdn" in lowered:
            raise LocalWebServerError("packaged web assets reference external content")
    return assets


@dataclass(frozen=True, slots=True)
class _CallableIdentity:
    target: Callable[..., object]
    code: object
    defaults: object
    kwdefaults: object
    closure_values: tuple[object, ...]

    @classmethod
    def capture(cls, target: Callable[..., object]) -> _CallableIdentity:
        closure = getattr(target, "__closure__", None) or ()
        return cls(
            target=target,
            code=getattr(target, "__code__", None),
            defaults=getattr(target, "__defaults__", None),
            kwdefaults=getattr(target, "__kwdefaults__", None),
            closure_values=tuple(cell.cell_contents for cell in closure),
        )

    def assert_intact(self) -> None:
        closure = getattr(self.target, "__closure__", None) or ()
        if (
            getattr(self.target, "__code__", None) is not self.code
            or getattr(self.target, "__defaults__", None) is not self.defaults
            or getattr(self.target, "__kwdefaults__", None) is not self.kwdefaults
            or len(closure) != len(self.closure_values)
            or any(
                cell.cell_contents is not expected
                for cell, expected in zip(closure, self.closure_values, strict=True)
            )
        ):
            raise LocalWebServerError("installed HTTP callable changed")


@dataclass(frozen=True, slots=True)
class _ExplorerHttpBoundary:
    dispatch: tuple[Callable[..., object], Callable[..., object], Callable[..., object]]
    prepare_document: Callable[..., dict[str, object]]
    prepare_comparison: Callable[..., dict[str, object]]
    validate_public: Callable[..., None]
    canonicalize: Callable[[object], bytes]
    longitudinal: Callable[..., tuple[int, dict[str, object]]]
    identities: tuple[_CallableIdentity, ...]

    def assert_intact(self) -> None:
        for identity in self.identities:
            identity.assert_intact()

    def encode(self, payload: object) -> bytes:
        self.assert_intact()
        content = self.canonicalize(payload) + b"\n"
        self.assert_intact()
        return content

    def encode_public(self, payload: object) -> bytes:
        self.assert_intact()
        self.validate_public(payload)
        return self.encode(payload)


_INSTALLED_EXPLORER_HTTP_DEPENDENCIES = (
    _EXPLORER_DISPATCH,
    prepare_explorer_document_response,
    prepare_explorer_comparison_response,
    validate_public_projection,
    canonical_json_bytes,
    handle_longitudinal_route,
)


@dataclass(frozen=True, slots=True)
class _Application:
    kernel: LocalApiKernel
    boundary: LocalWebBoundary
    assets: dict[str, tuple[str, bytes]]
    explorer: IntegratedExplorerSource | None = None
    explorer_http: _ExplorerHttpBoundary | None = None
    reader: ReaderSessionBinder | None = None


class _LoopbackHttpServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False
    request_queue_size = MAX_HTTP_WORKERS
    application: _Application
    security_validator: Callable[[], None]
    security_failed: threading.Event

    def __setattr__(self, name: str, value: object) -> None:
        if name == "RequestHandlerClass" and hasattr(self, "RequestHandlerClass"):
            raise TypeError("installed request handler is sealed")
        super().__setattr__(name, value)

    def __init__(self, *args: object, **kwargs: object) -> None:
        self._worker_slots = threading.BoundedSemaphore(MAX_HTTP_WORKERS)
        self._worker_count = 0
        self._worker_count_lock = threading.Lock()
        self.security_failed = threading.Event()
        super().__init__(*args, **kwargs)

    @property
    def active_workers(self) -> int:
        with self._worker_count_lock:
            return self._worker_count

    def verify_request(self, request: socket.socket, client_address: object) -> bool:
        del request
        try:
            return bool(
                self.RequestHandlerClass is _Handler
                and client_address
                and isinstance(client_address, tuple)
                and ipaddress.ip_address(client_address[0]).is_loopback
            )
        except (ValueError, TypeError):
            return False

    def process_request(self, request: socket.socket, client_address: object) -> None:
        if not self._worker_slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        with self._worker_count_lock:
            self._worker_count += 1
        try:
            super().process_request(request, client_address)
        except BaseException:
            with self._worker_count_lock:
                self._worker_count -= 1
            self._worker_slots.release()
            raise

    def process_request_thread(
        self, request: socket.socket, client_address: object
    ) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            with self._worker_count_lock:
                self._worker_count -= 1
            self._worker_slots.release()

    def handle_error(self, request: object, client_address: object) -> None:
        del request, client_address


class _LoopbackHttpServerV6(_LoopbackHttpServer):
    address_family = socket.AF_INET6


class _SealedHandlerType(type):
    def __setattr__(cls, name: str, value: object) -> None:
        raise TypeError("installed HTTP handler class is sealed")

    def __delattr__(cls, name: str) -> None:
        raise TypeError("installed HTTP handler class is sealed")


class _Handler(http.server.BaseHTTPRequestHandler, metaclass=_SealedHandlerType):
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
        self.connection.settimeout(REQUEST_TIMEOUT_SECONDS)

    def _headers_within_bounds(self) -> bool:
        items = list(self.headers.items())
        return (
            len(items) <= MAX_REQUEST_HEADERS
            and sum(len(name) + len(value) + 4 for name, value in items)
            <= MAX_REQUEST_HEADER_BYTES
        )

    def _security_boundary_intact(self) -> bool:
        server = self.server  # type: ignore[assignment]
        if server.security_failed.is_set():  # type: ignore[attr-defined]
            return False
        try:
            server.security_validator()  # type: ignore[attr-defined]
        except LocalWebServerError:
            server.security_failed.set()  # type: ignore[attr-defined]
            return False
        return True

    def _single_header(self, name: str) -> str | None:
        values = self.headers.get_all(name, failobj=[])
        return values[0] if len(values) == 1 else None

    def _send(self, status_code: int, content_type: str, content: bytes) -> None:
        try:
            self.send_response(status_code)
            for name, value in _SECURITY_HEADERS.items():
                self.send_header(name, value)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(content)
        except (BrokenPipeError, ConnectionResetError, OSError):
            self.close_connection = True

    def _json(self, status_code: int, payload: object) -> None:
        boundary = self.application.explorer_http
        if boundary is None or not self._security_boundary_intact():
            self.close_connection = True
            self._send(
                503,
                "application/json; charset=utf-8",
                b'{"error":{"code":"TBX-WEB-503"}}\n',
            )
            return
        self._send(
            status_code,
            "application/json; charset=utf-8",
            boundary.encode(payload),
        )

    def _public_json(self, status_code: int, payload: object) -> None:
        boundary = self.application.explorer_http
        if boundary is None or not self._security_boundary_intact():
            self.close_connection = True
            self._send(
                503,
                "application/json; charset=utf-8",
                b'{"error":{"code":"TBX-WEB-503"}}\n',
            )
            return
        self._send(
            status_code,
            "application/json; charset=utf-8",
            boundary.encode_public(payload),
        )

    def _deny(self, error: BoundaryDenied) -> None:
        self._json(error.status_code, {"error": {"code": error.code}})

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
            if (
                not _COOKIE_NAME.fullmatch(name)
                or not _COOKIE_VALUE.fullmatch(value)
                or name in cookies
            ):
                return None
            cookies[name] = value
        token = cookies.get("traceback_session")
        if token is None or not 43 <= len(token) <= 128:
            return None
        return token

    def _request(self, path: str) -> BrowserRequest:
        forwarded = tuple(
            sorted(
                name
                for name in self.headers
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
            raise TypeError("request body must be an object")
        return payload

    def _longitudinal(
        self, path: str, query: str, request: BrowserRequest, *, post: bool
    ) -> None:
        """E12 routes: B01 checks, then the reader gate inside the handler."""

        try:
            self.application.boundary.authorize(request)
            explorer = self.application.explorer
            source = None if explorer is None else explorer.longitudinal_source()
            if source is None:
                raise ApiProblem(404, self.application.kernel.not_found_problem)
            explorer_http = self.application.explorer_http
            if explorer_http is None:
                raise TypeError("explorer HTTP boundary is unavailable")
            explorer_http.assert_intact()
            body: object = None
            params: dict[str, list[str]] = {}
            if post:
                body = self._body_json()
            elif query:
                params = parse_qs(
                    query,
                    keep_blank_values=False,
                    strict_parsing=True,
                    max_num_fields=12,
                )
            status, payload = explorer_http.longitudinal(
                "POST" if post else "GET",
                path,
                binder=self.application.reader,
                request=request,
                source=source,
                params=params,
                body=body,
            )
        except BoundaryDenied as exc:
            self._deny(exc)
            return
        except ApiProblem as exc:
            self._json(exc.status_code, exc.problem.model_dump(mode="json"))
            return
        except (TypeError, ValueError, json.JSONDecodeError):
            self._json(400, {"error": {"code": "TBX-WEB-400"}})
            return
        self._json(status, payload)

    def do_HEAD(self) -> None:
        self.do_GET()

    def do_GET(self) -> None:
        if not self._security_boundary_intact():
            self.close_connection = True
            self._json(503, {"error": {"code": "TBX-WEB-503"}})
            return
        if not self._headers_within_bounds():
            self.close_connection = True
            self._json(431, {"error": {"code": "TBX-WEB-431"}})
            return
        parsed = urlsplit(self.path)
        if parsed.fragment:
            self._json(404, {"error": {"code": "TBX-WEB-404"}})
            return
        if parsed.query and parsed.path not in {
            "/api/v1/explorer/catalog",
            _EXPLORER_COMPARE_ROUTE,
            *_LONGITUDINAL_GET_ROUTES,
        }:
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
        if parsed.path in _LONGITUDINAL_GET_ROUTES:
            self._longitudinal(parsed.path, parsed.query, request, post=False)
            return
        try:
            if parsed.path == "/api/v1/jobs":
                jobs = self.application.kernel.list_jobs(request)
                self._json(
                    200, {"jobs": [item.model_dump(mode="json") for item in jobs]}
                )
                return
            if parsed.path == "/api/v1/explorer/catalog":
                self.application.boundary.authorize(request)
                if self.application.explorer is None:
                    raise ApiProblem(404, self.application.kernel.not_found_problem)
                parameters = parse_qs(
                    parsed.query,
                    keep_blank_values=False,
                    strict_parsing=True,
                    max_num_fields=8,
                )
                if set(parameters) - {"method_id", "method_version", "cursor", "limit"}:
                    raise ValueError("unsupported explorer query")
                if any(len(values) != 1 for values in parameters.values()):
                    raise ValueError("explorer query values must be singular")
                method_ids = parameters.get("method_id", [])
                method_versions = parameters.get("method_version", [])
                if (
                    bool(method_ids) != bool(method_versions)
                    or len(method_ids) > 1
                    or len(method_versions) > 1
                ):
                    raise ValueError("method filters must be paired")
                query_payload: dict[str, object] = {
                    "limit": int(parameters.get("limit", ["50"])[0]),
                }
                if method_ids:
                    query_payload["method_refs"] = (
                        {"method_id": method_ids[0], "version": method_versions[0]},
                    )
                if "cursor" in parameters:
                    query_payload["cursor"] = parameters["cursor"][0]
                explorer_http = self.application.explorer_http
                if explorer_http is None:
                    raise TypeError("explorer HTTP boundary is unavailable")
                explorer_http.assert_intact()
                explorer_query = explorer_http.dispatch[0]
                query = CatalogQuery(**query_payload)
                page = explorer_query(self.application.explorer, query)
                if page.query != query:
                    raise ValueError("catalog response query identity changed")
                payload = page.model_dump(mode="json")
                if payload.get("query") != query.model_dump(mode="json"):
                    raise ValueError("catalog response query encoding changed")
                self._public_json(200, payload)
                return
            if parsed.path == _EXPLORER_COMPARE_ROUTE:
                self.application.boundary.authorize(request)
                if self.application.explorer is None:
                    raise ApiProblem(404, self.application.kernel.not_found_problem)
                parameters = parse_qs(
                    parsed.query,
                    keep_blank_values=False,
                    strict_parsing=True,
                    max_num_fields=2,
                )
                if set(parameters) != {"left", "right"} or any(
                    len(values) != 1 for values in parameters.values()
                ):
                    raise ValueError("comparison requires singular left and right")
                result_pattern = re.compile(r"^result_[0-9a-f]{40}$")
                left = parameters["left"][0]
                right = parameters["right"][0]
                if not result_pattern.fullmatch(left) or not result_pattern.fullmatch(
                    right
                ):
                    raise ValueError("comparison result identity is invalid")
                explorer_http = self.application.explorer_http
                if explorer_http is None:
                    raise TypeError("explorer HTTP boundary is unavailable")
                explorer_http.assert_intact()
                explorer_compare = explorer_http.dispatch[2]
                comparison = explorer_compare(self.application.explorer, left, right)
                if (
                    comparison.left_result_id != left
                    or comparison.right_result_id != right
                ):
                    raise ValueError("comparison response identity changed")
                payload = explorer_http.prepare_comparison(
                    self.application.explorer, comparison
                )
                if (
                    payload.get("left_result_id") != left
                    or payload.get("right_result_id") != right
                ):
                    raise ValueError("comparison response encoding changed")
                self._public_json(200, payload)
                return
            explorer_match = _EXPLORER_RESULT_ROUTE.fullmatch(parsed.path)
            if explorer_match is not None:
                self.application.boundary.authorize(request)
                if self.application.explorer is None:
                    raise ApiProblem(404, self.application.kernel.not_found_problem)
                requested_result_id = explorer_match.group(1)
                explorer_http = self.application.explorer_http
                if explorer_http is None:
                    raise TypeError("explorer HTTP boundary is unavailable")
                explorer_http.assert_intact()
                explorer_get = explorer_http.dispatch[1]
                try:
                    document = explorer_get(
                        self.application.explorer, requested_result_id
                    )
                except KeyError as exc:
                    raise ApiProblem(
                        404, self.application.kernel.not_found_problem
                    ) from exc
                if document.models.catalog_ref.result_id != requested_result_id:
                    raise ValueError("detail response identity changed")
                payload = explorer_http.prepare_document(
                    self.application.explorer, document
                )
                models = payload.get("models")
                catalog_ref = (
                    models.get("catalog_ref") if isinstance(models, dict) else None
                )
                if (
                    not isinstance(catalog_ref, dict)
                    or catalog_ref.get("result_id") != requested_result_id
                ):
                    raise ValueError("detail response encoding changed")
                self._public_json(200, payload)
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
        except (TypeError, ValueError):
            self._json(400, {"error": {"code": "TBX-WEB-400"}})
            return
        self._json(404, {"error": {"code": "TBX-WEB-404"}})

    def do_POST(self) -> None:
        if not self._security_boundary_intact():
            self.close_connection = True
            self._json(503, {"error": {"code": "TBX-WEB-503"}})
            return
        if not self._headers_within_bounds():
            self.close_connection = True
            self._json(431, {"error": {"code": "TBX-WEB-431"}})
            return
        parsed = urlsplit(self.path)
        if parsed.query or parsed.fragment:
            self._json(404, {"error": {"code": "TBX-WEB-404"}})
            return
        request = self._request(parsed.path)
        if parsed.path in _LONGITUDINAL_POST_ROUTES:
            self._longitudinal(parsed.path, "", request, post=True)
            return
        try:
            if parsed.path == "/api/v1/session/bootstrap":
                self.application.boundary.authorize_bootstrap_request(request)
                payload = self._body_json()
                if set(payload) != {"bootstrap"} or not isinstance(
                    payload["bootstrap"], str
                ):
                    raise ValueError("bootstrap request shape is invalid")
                grant = self.application.boundary.broker.exchange(
                    payload["bootstrap"],
                    authority=self.application.boundary.config.authority,
                )
                explorer_http = self.application.explorer_http
                if explorer_http is None:
                    raise TypeError("explorer HTTP boundary is unavailable")
                content = explorer_http.encode({"csrf_token": grant.csrf_token})
                try:
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
                except (BrokenPipeError, ConnectionResetError, OSError):
                    self.close_connection = True
                return
            if parsed.path == "/api/v1/session/validate":
                self.application.boundary.authorize(request)
                self._json(200, {"authorized": True})
                return
            if parsed.path == _READER_LAUNCH_ROUTE:
                # Full B01 mutation checks (Host, session, Origin, CSRF) run
                # before the body is read; the binder repeats them and requires
                # POST.  The credential arrives only in this JSON body.
                self.application.boundary.authorize(request)
                reader = self.application.reader
                if reader is None:
                    raise ApiProblem(404, self.application.kernel.not_found_problem)
                payload = self._body_json()
                if set(payload) != {"launch"} or not isinstance(
                    payload["launch"], str
                ):
                    raise ValueError("reader launch request shape is invalid")
                reader.exchange_launch_credential(request, payload["launch"])
                self._json(200, {"reader_bound": True})
                return
        except BoundaryDenied as exc:
            self._deny(exc)
            return
        except ReaderAuthorizationDenied:
            self._json(403, {"error": {"code": "permission_denied"}})
            return
        except ApiProblem as exc:
            self._json(exc.status_code, exc.problem.model_dump(mode="json"))
            return
        except (TypeError, ValueError, json.JSONDecodeError):
            self._json(400, {"error": {"code": "TBX-WEB-400"}})
            return
        self._json(404, {"error": {"code": "TBX-WEB-404"}})


@dataclass(frozen=True, slots=True)
class _EventStatus:
    value: bool

    def is_set(self) -> bool:
        return self.value


@dataclass(frozen=True, slots=True)
class _ServerStatus:
    security_failed_value: bool
    active_workers: int

    @property
    def security_failed(self) -> _EventStatus:
        return _EventStatus(self.security_failed_value)


@dataclass(slots=True)
class _RunningLocalWebRuntime:
    server: _LoopbackHttpServer
    thread: threading.Thread
    boundary: LocalWebBoundary
    startup_anchor: _StartupAnchor
    state_directory: Path
    state_directory_fd: int
    lease_fd: int
    instance_id: str
    watchdog_stop: threading.Event
    watchdog_thread: threading.Thread
    reader: ReaderSessionBinder | None = None
    # Keeps the job store's WAL/SHM sidecars (and their pinned identities)
    # stable while runner processes open and close the same database.
    journal: ExitStack | None = None


_RUNTIME_LOCK = threading.Lock()
_RUNTIMES: dict[str, _RunningLocalWebRuntime] = {}


@dataclass(frozen=True, slots=True)
class RunningLocalWebService:
    """An immutable handle to a package-owned local web runtime."""

    _runtime_id: str
    config: LoopbackServerConfig
    bootstrap_code: str
    instance_id: str
    anchor_path: Path
    _launch_url: str

    @property
    def server(self) -> _ServerStatus:
        """Return an inert status snapshot without runtime capabilities."""

        with _RUNTIME_LOCK:
            runtime = _RUNTIMES.get(self._runtime_id)
            if runtime is None:
                return _ServerStatus(True, 0)
            return _ServerStatus(
                runtime.server.security_failed.is_set(),
                runtime.server.active_workers,
            )

    @property
    def is_running(self) -> bool:
        with _RUNTIME_LOCK:
            runtime = _RUNTIMES.get(self._runtime_id)
            return runtime is not None and runtime.thread.is_alive()

    @classmethod
    def start(
        cls,
        *,
        store: JobStore,
        state_directory: Path,
        ipv6: bool = False,
        explorer: IntegratedExplorerSource | None = None,
        reader_registry: ReaderAuthorizationRegistry | None = None,
    ) -> Self:
        """Start the loopback service.

        ``reader_registry`` enables the E12 reader launch exchange route for
        that protected registry; without it the route answers not found.
        """

        state_directory = state_directory.absolute()
        startup_anchor: _StartupAnchor | None = None
        state_fd: int | None = None
        lease_fd: int | None = None
        server: _LoopbackHttpServer | None = None
        instance_id: str | None = None
        thread: threading.Thread | None = None
        watchdog_stop: threading.Event | None = None
        watchdog_thread: threading.Thread | None = None
        journal = ExitStack()
        try:
            startup_anchor = _open_startup_anchor(state_directory)
            _require_startup_anchor(startup_anchor)
            state_fd = _open_state_directory(state_directory)
            _require_startup_anchor(startup_anchor)
            _require_named_state_directory(state_directory, state_fd)
            lease_fd = _acquire_instance_lease(state_fd)
            _require_instance_lease(state_fd, lease_fd)
            _unlink_instance_state(state_fd)
            _require_named_state_directory(state_directory, state_fd)
            _require_instance_lease(state_fd, lease_fd)

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
            (
                explorer_dispatch,
                prepare_document,
                prepare_comparison,
                validate_public,
                canonicalize,
                longitudinal_dispatch,
            ) = _INSTALLED_EXPLORER_HTTP_DEPENDENCIES
            tracked_callables = (
                *explorer_dispatch,
                prepare_document,
                prepare_comparison,
                validate_public,
                canonicalize,
                longitudinal_dispatch,
                _Handler.do_GET,
                _Handler.do_POST,
                _Handler._longitudinal,
                _Handler._json,
                _Handler._public_json,
            )
            explorer_http = _ExplorerHttpBoundary(
                dispatch=explorer_dispatch,
                prepare_document=prepare_document,
                prepare_comparison=prepare_comparison,
                validate_public=validate_public,
                canonicalize=canonicalize,
                longitudinal=longitudinal_dispatch,
                identities=tuple(
                    _CallableIdentity.capture(item) for item in tracked_callables
                ),
            )
            explorer_http.assert_intact()
            # A replaced or foreign explorer is not an E12 installation; its
            # own routes still fail closed per request as before.
            longitudinal_source = None
            if type(explorer) is IntegratedExplorerSource:
                try:
                    longitudinal_source = explorer.longitudinal_source()
                except TypeError:
                    longitudinal_source = None
            if longitudinal_source is not None and (
                reader_registry is None
                or longitudinal_source.reader_registry is not reader_registry
            ):
                raise LocalWebServerError(
                    "longitudinal routes require their own reader registry"
                )
            reader = (
                None
                if reader_registry is None
                else ReaderSessionBinder(boundary=boundary, registry=reader_registry)
            )
            application = _Application(
                kernel,
                boundary,
                _packaged_assets(),
                explorer,
                explorer_http,
                reader,
            )
            server.application = application

            def validate_security_boundary() -> None:
                _require_startup_anchor(startup_anchor)
                _require_named_state_directory(state_directory, state_fd)
                _require_instance_lease(state_fd, lease_fd)
                if (
                    server.application is not application
                    or application.boundary is not boundary
                    or application.kernel is not kernel
                    or application.explorer_http is not explorer_http
                    or application.reader is not reader
                ):
                    raise LocalWebServerError("installed HTTP application changed")
                if server.RequestHandlerClass is not _Handler:
                    raise LocalWebServerError("installed request handler changed")
                explorer_http.assert_intact()

            server.security_validator = validate_security_boundary
            instance_id = f"instance_{secrets.token_hex(16)}"
            _require_startup_anchor(startup_anchor)
            _require_named_state_directory(state_directory, state_fd)
            _require_instance_lease(state_fd, lease_fd)
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
            _require_startup_anchor(startup_anchor)
            _require_named_state_directory(state_directory, state_fd)
            _require_instance_lease(state_fd, lease_fd)
            bootstrap_code = boundary.issue_bootstrap()
            thread = threading.Thread(
                target=server.serve_forever,
                name="traceback-local-web",
                daemon=True,
            )
            thread.start()
            watchdog_stop = threading.Event()

            def watch_security_boundary() -> None:
                while not watchdog_stop.wait(0.05):
                    try:
                        server.security_validator()
                    except LocalWebServerError:
                        server.security_failed.set()
                    if server.security_failed.is_set():
                        server.shutdown()
                        server.server_close()
                        return

            watchdog_thread = threading.Thread(
                target=watch_security_boundary,
                name="traceback-local-web-security",
                daemon=True,
            )
            watchdog_thread.start()
            runtime_id = secrets.token_hex(32)
            runtime = _RunningLocalWebRuntime(
                server=server,
                thread=thread,
                boundary=boundary,
                startup_anchor=startup_anchor,
                state_directory=state_directory,
                state_directory_fd=state_fd,
                lease_fd=lease_fd,
                instance_id=instance_id,
                watchdog_stop=watchdog_stop,
                watchdog_thread=watchdog_thread,
                reader=reader,
                journal=journal,
            )
            with _RUNTIME_LOCK:
                _RUNTIMES[runtime_id] = runtime
            launch_url = (
                f"{config.allowed_origins[0]}/"
                f"{boundary.broker.launch_fragment(bootstrap_code)}"
            )
            return cls(
                _runtime_id=runtime_id,
                config=config,
                bootstrap_code=bootstrap_code,
                instance_id=instance_id,
                anchor_path=startup_anchor.parent_path / startup_anchor.name,
                _launch_url=launch_url,
            )
        except BaseException:
            try:
                if watchdog_stop is not None:
                    watchdog_stop.set()
                if server is not None:
                    if thread is not None and thread.is_alive():
                        server.shutdown()
                    server.server_close()
                if thread is not None and thread.ident is not None:
                    thread.join(timeout=5)
                if watchdog_thread is not None and watchdog_thread.ident is not None:
                    watchdog_thread.join(timeout=5)
                if state_fd is not None and instance_id is not None:
                    try:
                        _unlink_instance_state(
                            state_fd, expected_instance_id=instance_id
                        )
                    except LocalWebServerError:
                        pass
                if lease_fd is not None:
                    fcntl.flock(lease_fd, fcntl.LOCK_UN)
                    os.close(lease_fd)
                if state_fd is not None:
                    fcntl.flock(state_fd, fcntl.LOCK_UN)
                    os.close(state_fd)
                if startup_anchor is not None:
                    _close_startup_anchor(startup_anchor)
            finally:
                # Release the job-store anchor even when earlier cleanup fails.
                journal.close()
            raise

    @property
    def base_url(self) -> str:
        return self.config.allowed_origins[0]

    @property
    def launch_url(self) -> str:
        return self._launch_url

    def issue_bootstrap(self) -> str:
        with _RUNTIME_LOCK:
            runtime = _RUNTIMES.get(self._runtime_id)
            if runtime is None:
                raise LocalWebServerError("local web service is closed")
            return runtime.boundary.issue_bootstrap()

    def issue_reader_launch_url(self, grant_selector: str) -> str:
        """Return a one-use launch link binding a new session to one grant.

        The link carries a fresh B01 bootstrap code and a fresh one-use reader
        launch credential only in the URL fragment, which browsers never send
        to the server or in a Referer.  The packaged page clears the fragment,
        exchanges the bootstrap, then POSTs the credential with Origin and
        CSRF to the reader launch route.  The link is for the operator's
        terminal only; it is never logged or written to disk here.
        """

        with _RUNTIME_LOCK:
            runtime = _RUNTIMES.get(self._runtime_id)
            if runtime is None:
                raise LocalWebServerError("local web service is closed")
            if runtime.reader is None:
                raise LocalWebServerError("reader authorization is not configured")
            credential = runtime.reader.issue_launch_credential(grant_selector)
            bootstrap = runtime.boundary.issue_bootstrap()
        fragment = runtime.boundary.broker.launch_fragment(bootstrap)
        if not BootstrapBroker._strong_token(credential):
            raise LocalWebServerError("reader launch credential is invalid")
        return f"{self.config.allowed_origins[0]}/{fragment}&reader_launch={credential}"

    def close(self) -> None:
        with _RUNTIME_LOCK:
            runtime = _RUNTIMES.pop(self._runtime_id, None)
        if runtime is None:
            return
        runtime.watchdog_stop.set()
        try:
            try:
                runtime.server.shutdown()
            finally:
                runtime.server.server_close()
                runtime.thread.join(timeout=5)
                runtime.watchdog_thread.join(timeout=5)
        finally:
            try:
                try:
                    _unlink_instance_state(
                        runtime.state_directory_fd,
                        expected_instance_id=runtime.instance_id,
                    )
                finally:
                    fcntl.flock(runtime.lease_fd, fcntl.LOCK_UN)
                    os.close(runtime.lease_fd)
            finally:
                try:
                    fcntl.flock(runtime.state_directory_fd, fcntl.LOCK_UN)
                    os.close(runtime.state_directory_fd)
                finally:
                    try:
                        _close_startup_anchor(runtime.startup_anchor)
                    finally:
                        if runtime.journal is not None:
                            runtime.journal.close()

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
