"""One-use bootstrap and loopback browser authorization boundary."""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, model_validator

from traceback_runner.contracts import RunnerContract

_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


class BoundaryDenied(PermissionError):
    """Sanitized denial with no object existence or credential detail."""

    def __init__(self, status_code: Literal[401, 403], code: str) -> None:
        super().__init__("local request denied")
        self.status_code = status_code
        self.code = code


class LoopbackServerConfig(RunnerContract):
    schema_version: Literal["traceback.local-web-config.v1"] = (
        "traceback.local-web-config.v1"
    )
    bind_host: str
    port: int = Field(ge=1024, le=65535)
    allowed_host_headers: tuple[str, ...] = Field(min_length=1, max_length=3)
    allowed_origins: tuple[str, ...] = Field(min_length=1, max_length=3)
    cors_enabled: Literal[False] = False
    outbound_network_enabled: Literal[False] = False

    @model_validator(mode="after")
    def loopback_only(self) -> LoopbackServerConfig:
        try:
            address = ipaddress.ip_address(self.bind_host)
        except ValueError as exc:
            raise ValueError("bind host must be a literal loopback address") from exc
        if not address.is_loopback:
            raise ValueError("local web service must bind only to loopback")
        if self.allowed_host_headers != tuple(sorted(set(self.allowed_host_headers))):
            raise ValueError("allowed Host values must be uniquely sorted")
        if self.allowed_origins != tuple(sorted(set(self.allowed_origins))):
            raise ValueError("allowed origins must be uniquely sorted")
        expected_suffix = f":{self.port}"
        if any(not item.endswith(expected_suffix) for item in self.allowed_host_headers):
            raise ValueError("allowed Host values must bind the configured port")
        for origin in self.allowed_origins:
            parsed = urlsplit(origin)
            if parsed.scheme != "http" or parsed.path not in {"", "/"}:
                raise ValueError("local origin must be a plain loopback HTTP origin")
            if parsed.hostname is None:
                raise ValueError("local origin requires a host")
            try:
                if not ipaddress.ip_address(parsed.hostname).is_loopback:
                    raise ValueError("allowed origin must resolve to literal loopback")
            except ValueError as exc:
                raise ValueError("allowed origin must use a literal loopback address") from exc
            if parsed.port != self.port:
                raise ValueError("allowed origin must bind the configured port")
        return self


def build_loopback_config(*, port: int, ipv6: bool = False) -> LoopbackServerConfig:
    host = "::1" if ipv6 else "127.0.0.1"
    host_header = f"[::1]:{port}" if ipv6 else f"127.0.0.1:{port}"
    origin_host = "[::1]" if ipv6 else "127.0.0.1"
    return LoopbackServerConfig(
        bind_host=host,
        port=port,
        allowed_host_headers=(host_header,),
        allowed_origins=(f"http://{origin_host}:{port}",),
    )


@dataclass(frozen=True, slots=True)
class BrowserRequest:
    method: str
    path: str
    host: str
    origin: str | None = None
    session_token: str | None = None
    csrf_token: str | None = None


@dataclass(frozen=True, slots=True)
class SessionGrant:
    session_token: str
    csrf_token: str
    cookie_name: Literal["traceback_session"] = "traceback_session"
    cookie_http_only: Literal[True] = True
    cookie_same_site: Literal["Strict"] = "Strict"
    cookie_secure: Literal[False] = False


@dataclass(frozen=True, slots=True)
class _SessionRecord:
    csrf_sha256: bytes
    expires_at: float


class BootstrapBroker:
    """In-memory, restart-rotated secrets for one authenticated OS user."""

    def __init__(
        self,
        *,
        now: Callable[[], float] = time.monotonic,
        token_factory: Callable[[], str] = lambda: secrets.token_urlsafe(32),
        bootstrap_ttl_seconds: int = 60,
        session_ttl_seconds: int = 8 * 60 * 60,
    ) -> None:
        self._now = now
        self._token_factory = token_factory
        self._bootstrap_ttl = bootstrap_ttl_seconds
        self._session_ttl = session_ttl_seconds
        self._bootstrap_sha256: bytes | None = None
        self._bootstrap_expires_at = 0.0
        self._sessions: dict[bytes, _SessionRecord] = {}

    @staticmethod
    def _digest(value: str) -> bytes:
        return hashlib.sha256(value.encode("utf-8")).digest()

    def issue_bootstrap(self) -> str:
        code = self._token_factory()
        self._bootstrap_sha256 = self._digest(code)
        self._bootstrap_expires_at = self._now() + self._bootstrap_ttl
        return code

    @staticmethod
    def launch_fragment(code: str) -> str:
        return f"#bootstrap={code}"

    def exchange(self, code: str) -> SessionGrant:
        supplied = self._digest(code)
        expected = self._bootstrap_sha256
        valid = (
            expected is not None
            and self._now() <= self._bootstrap_expires_at
            and hmac.compare_digest(supplied, expected)
        )
        self._bootstrap_sha256 = None
        self._bootstrap_expires_at = 0.0
        if not valid:
            raise BoundaryDenied(401, "TBX-AUTH-001")
        session_token = self._token_factory()
        csrf_token = self._token_factory()
        self._sessions[self._digest(session_token)] = _SessionRecord(
            csrf_sha256=self._digest(csrf_token),
            expires_at=self._now() + self._session_ttl,
        )
        return SessionGrant(session_token=session_token, csrf_token=csrf_token)

    def require_session(self, session_token: str | None) -> _SessionRecord:
        if session_token is None:
            raise BoundaryDenied(401, "TBX-AUTH-001")
        digest = self._digest(session_token)
        record = self._sessions.get(digest)
        if record is None or self._now() > record.expires_at:
            self._sessions.pop(digest, None)
            raise BoundaryDenied(401, "TBX-AUTH-001")
        return record

    def require_csrf(self, record: _SessionRecord, csrf_token: str | None) -> None:
        if csrf_token is None or not hmac.compare_digest(
            self._digest(csrf_token), record.csrf_sha256
        ):
            raise BoundaryDenied(403, "TBX-AUTH-002")


class LocalWebBoundary:
    def __init__(self, config: LoopbackServerConfig, broker: BootstrapBroker) -> None:
        self.config = config
        self.broker = broker

    def _require_host(self, request: BrowserRequest) -> None:
        if request.host not in self.config.allowed_host_headers:
            raise BoundaryDenied(403, "TBX-AUTH-003")

    def _require_origin(self, request: BrowserRequest) -> None:
        if request.origin not in self.config.allowed_origins:
            raise BoundaryDenied(403, "TBX-AUTH-003")

    def exchange_bootstrap(self, request: BrowserRequest, code: str) -> SessionGrant:
        self._require_host(request)
        self._require_origin(request)
        if request.method != "POST" or request.path != "/api/v1/session/bootstrap":
            raise BoundaryDenied(403, "TBX-AUTH-003")
        if "?" in request.path:
            raise BoundaryDenied(403, "TBX-AUTH-003")
        return self.broker.exchange(code)

    def authorize(self, request: BrowserRequest) -> None:
        self._require_host(request)
        session = self.broker.require_session(request.session_token)
        method = request.method.upper()
        if method not in _SAFE_METHODS:
            self._require_origin(request)
            self.broker.require_csrf(session, request.csrf_token)
