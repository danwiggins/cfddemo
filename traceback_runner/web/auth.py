"""One-use bootstrap and loopback browser authorization boundary."""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import secrets
import string
import threading
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
        authority = (
            f"[{address.compressed}]:{self.port}"
            if address.version == 6
            else f"{address.compressed}:{self.port}"
        )
        expected_hosts = (authority,)
        expected_origins = (f"http://{authority}",)
        if self.allowed_host_headers != expected_hosts:
            raise ValueError("allowed Host must exactly match the bind authority")
        if self.allowed_origins != expected_origins:
            raise ValueError("allowed Origin must exactly match the bind authority")
        parsed = urlsplit(self.allowed_origins[0])
        if (
            parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise ValueError("local origin must be an exact authority without userinfo")
        return self

    @property
    def authority(self) -> str:
        return self.allowed_host_headers[0]


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
    forwarded_headers: tuple[str, ...] = ()


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
    authority: str


class BootstrapBroker:
    """In-memory, restart-rotated secrets for one authenticated OS user."""

    def __init__(
        self,
        *,
        now: Callable[[], float] = time.monotonic,
        bootstrap_ttl_seconds: int = 60,
        session_ttl_seconds: int = 8 * 60 * 60,
        max_active_sessions: int = 32,
        token_attempt_limit: int = 4,
    ) -> None:
        if not 1 <= bootstrap_ttl_seconds <= 300:
            raise ValueError("bootstrap TTL must be between 1 and 300 seconds")
        if not 60 <= session_ttl_seconds <= 86_400:
            raise ValueError("session TTL must be between 60 and 86400 seconds")
        if not 1 <= max_active_sessions <= 256:
            raise ValueError("active session limit must be between 1 and 256")
        if not 1 <= token_attempt_limit <= 16:
            raise ValueError("token attempt limit must be between 1 and 16")
        self._now = now
        self._bootstrap_ttl = bootstrap_ttl_seconds
        self._session_ttl = session_ttl_seconds
        self._max_active_sessions = max_active_sessions
        self._token_attempt_limit = token_attempt_limit
        self._bootstrap_sha256: bytes | None = None
        self._bootstrap_expires_at = 0.0
        self._bootstrap_authority: str | None = None
        self._sessions: dict[bytes, _SessionRecord] = {}
        self._lock = threading.RLock()

    @staticmethod
    def _digest(value: str) -> bytes:
        return hashlib.sha256(value.encode("utf-8")).digest()

    @staticmethod
    def _strong_token(value: str) -> bool:
        alphabet = string.ascii_letters + string.digits + "-_"
        return 43 <= len(value) <= 128 and all(
            character in alphabet for character in value
        )

    def _issue_unique_token(self, forbidden: set[bytes]) -> tuple[str, bytes]:
        for _ in range(self._token_attempt_limit):
            token = secrets.token_urlsafe(32)
            if not self._strong_token(token):
                continue
            digest = self._digest(token)
            if digest not in forbidden:
                return token, digest
        raise RuntimeError("strong unique credential issuance failed")

    def _prune_expired_sessions(self, now: float) -> None:
        expired = [
            digest
            for digest, record in self._sessions.items()
            if now > record.expires_at
        ]
        for digest in expired:
            self._sessions.pop(digest, None)

    def _active_credential_digests(self) -> set[bytes]:
        digests = set(self._sessions)
        digests.update(record.csrf_sha256 for record in self._sessions.values())
        if self._bootstrap_sha256 is not None:
            digests.add(self._bootstrap_sha256)
        return digests

    def issue_bootstrap(self, *, authority: str) -> str:
        with self._lock:
            code, digest = self._issue_unique_token(self._active_credential_digests())
            self._bootstrap_sha256 = digest
            self._bootstrap_expires_at = self._now() + self._bootstrap_ttl
            self._bootstrap_authority = authority
            return code

    @staticmethod
    def launch_fragment(code: str) -> str:
        if not BootstrapBroker._strong_token(code):
            raise ValueError("bootstrap credential does not meet strength policy")
        return f"#bootstrap={code}"

    def exchange(self, code: str, *, authority: str) -> SessionGrant:
        with self._lock:
            supplied = self._digest(code)
            expected = self._bootstrap_sha256
            now = self._now()
            valid = (
                expected is not None
                and now <= self._bootstrap_expires_at
                and self._bootstrap_authority == authority
                and hmac.compare_digest(supplied, expected)
            )
            self._bootstrap_sha256 = None
            self._bootstrap_expires_at = 0.0
            self._bootstrap_authority = None
            if not valid:
                raise BoundaryDenied(401, "TBX-AUTH-001")
            self._prune_expired_sessions(now)
            if len(self._sessions) >= self._max_active_sessions:
                raise BoundaryDenied(403, "TBX-AUTH-004")
            forbidden = self._active_credential_digests()
            forbidden.add(supplied)
            session_token, session_digest = self._issue_unique_token(forbidden)
            forbidden.add(session_digest)
            csrf_token, csrf_digest = self._issue_unique_token(forbidden)
            self._sessions[session_digest] = _SessionRecord(
                csrf_sha256=csrf_digest,
                expires_at=now + self._session_ttl,
                authority=authority,
            )
            return SessionGrant(session_token=session_token, csrf_token=csrf_token)

    def require_session(
        self, session_token: str | None, *, authority: str
    ) -> _SessionRecord:
        if session_token is None:
            raise BoundaryDenied(401, "TBX-AUTH-001")
        digest = self._digest(session_token)
        with self._lock:
            record = self._sessions.get(digest)
            now = self._now()
            if (
                record is None
                or now > record.expires_at
                or record.authority != authority
            ):
                if record is not None and now > record.expires_at:
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

    def issue_bootstrap(self) -> str:
        return self.broker.issue_bootstrap(authority=self.config.authority)

    @staticmethod
    def _reject_forwarded(request: BrowserRequest) -> None:
        if request.forwarded_headers:
            raise BoundaryDenied(403, "TBX-AUTH-003")

    def _require_host(self, request: BrowserRequest) -> None:
        self._reject_forwarded(request)
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
        return self.broker.exchange(code, authority=self.config.authority)

    def authorize(self, request: BrowserRequest) -> None:
        self._require_host(request)
        session = self.broker.require_session(
            request.session_token, authority=self.config.authority
        )
        method = request.method.upper()
        if method not in _SAFE_METHODS:
            self._require_origin(request)
            self.broker.require_csrf(session, request.csrf_token)
