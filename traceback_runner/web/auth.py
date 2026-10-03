"""One-use bootstrap and loopback browser authorization boundary."""

from __future__ import annotations

import dataclasses
import hashlib
import hmac
import ipaddress
import secrets
import string
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final, Literal
from urllib.parse import urlsplit

from pydantic import Field, model_validator

from traceback_runner.contracts import RunnerContract

_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

SessionKind = Literal["operator", "reader"]
OPERATOR_SESSION: Final = "operator"
READER_SESSION: Final = "reader"
#: Every session kind; the default for checks that need only a live session.
ANY_SESSION: Final[frozenset[str]] = frozenset({OPERATOR_SESSION, READER_SESSION})
OPERATOR_ONLY: Final[frozenset[str]] = frozenset({OPERATOR_SESSION})
READER_ONLY: Final[frozenset[str]] = frozenset({READER_SESSION})
#: A session unused for longer than this ends (H1 idle timeout).
IDLE_TIMEOUT_SECONDS: Final = 20 * 60


class BoundaryDenied(PermissionError):
    """Sanitized denial with no object existence or credential detail."""

    def __init__(self, status_code: Literal[401, 403, 429], code: str) -> None:
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
    kind: SessionKind = "operator"
    cookie_name: Literal["traceback_session"] = "traceback_session"
    cookie_http_only: Literal[True] = True
    cookie_same_site: Literal["Strict"] = "Strict"
    cookie_secure: Literal[False] = False


def _is_sha256_hex(value: object) -> bool:
    return (
        type(value) is str
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


@dataclass(frozen=True, slots=True)
class ReaderSessionBinding:
    """Server-side E12 reader binding: grant commitment and registry head only.

    A bare B01 session is transport authentication.  This binding is added only
    by ``traceback_runner.web.reader_session`` after a one-use launch credential
    resolved to a current grant under the reader-registry fence; it never
    leaves the server and is re-resolved on every E12 read or save.
    """

    grant_sha256: str
    registry_head_sha256: str

    def __post_init__(self) -> None:
        if not _is_sha256_hex(self.grant_sha256) or not _is_sha256_hex(
            self.registry_head_sha256
        ):
            raise ValueError("reader session binding is invalid")


@dataclass(frozen=True, slots=True)
class _SessionRecord:
    """One live session.

    ``kind`` is fixed at bootstrap exchange: a session created from a reader
    launch link is a reader session from birth and carries the digest of the
    one launch credential issued in the same link.
    """

    csrf_sha256: bytes
    expires_at: float
    authority: str
    kind: SessionKind = "operator"
    last_seen_at: float = 0.0
    launch_credential_sha256: bytes | None = None
    reader_binding: ReaderSessionBinding | None = None


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
        exchange_window_seconds: int = 60,
        max_exchange_attempts: int = 8,
        idle_timeout_seconds: int = IDLE_TIMEOUT_SECONDS,
    ) -> None:
        if not 1 <= bootstrap_ttl_seconds <= 300:
            raise ValueError("bootstrap TTL must be between 1 and 300 seconds")
        if not 60 <= session_ttl_seconds <= 86_400:
            raise ValueError("session TTL must be between 60 and 86400 seconds")
        if not 1 <= max_active_sessions <= 256:
            raise ValueError("active session limit must be between 1 and 256")
        if not 1 <= token_attempt_limit <= 16:
            raise ValueError("token attempt limit must be between 1 and 16")
        if not 1 <= exchange_window_seconds <= 300:
            raise ValueError("exchange window must be between 1 and 300 seconds")
        if not 1 <= max_exchange_attempts <= 64:
            raise ValueError("exchange attempt limit must be between 1 and 64")
        if not 60 <= idle_timeout_seconds <= session_ttl_seconds:
            raise ValueError(
                "idle timeout must be between 60 seconds and the session TTL"
            )
        self._now = now
        self._idle_timeout = idle_timeout_seconds
        self._bootstrap_ttl = bootstrap_ttl_seconds
        self._session_ttl = session_ttl_seconds
        self._max_active_sessions = max_active_sessions
        self._token_attempt_limit = token_attempt_limit
        self._exchange_window_seconds = exchange_window_seconds
        self._max_exchange_attempts = max_exchange_attempts
        self._exchange_attempts: deque[float] = deque(maxlen=max_exchange_attempts)
        self._bootstrap_sha256: bytes | None = None
        self._bootstrap_expires_at = 0.0
        self._bootstrap_authority: str | None = None
        self._bootstrap_kind: SessionKind = OPERATOR_SESSION
        self._bootstrap_launch_sha256: bytes | None = None
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

    def _ended(self, record: _SessionRecord, now: float) -> bool:
        return (
            now > record.expires_at or now - record.last_seen_at > self._idle_timeout
        )

    def _prune_expired_sessions(self, now: float) -> None:
        expired = [
            digest
            for digest, record in self._sessions.items()
            if self._ended(record, now)
        ]
        for digest in expired:
            self._sessions.pop(digest, None)

    def _active_credential_digests(self) -> set[bytes]:
        digests = set(self._sessions)
        digests.update(record.csrf_sha256 for record in self._sessions.values())
        if self._bootstrap_sha256 is not None:
            digests.add(self._bootstrap_sha256)
        return digests

    def _clear_bootstrap(self) -> None:
        self._bootstrap_sha256 = None
        self._bootstrap_expires_at = 0.0
        self._bootstrap_authority = None
        self._bootstrap_kind = OPERATOR_SESSION
        self._bootstrap_launch_sha256 = None

    def issue_bootstrap(
        self,
        *,
        authority: str,
        kind: SessionKind = OPERATOR_SESSION,
        launch_credential_sha256: bytes | None = None,
    ) -> str:
        """Fill the single bootstrap slot, replacing any unexchanged code.

        A ``reader`` bootstrap carries the digest of the launch credential
        issued in the same link; exchanging it creates a reader session bound
        to that one credential.  An operator bootstrap carries none.
        """

        if kind == READER_SESSION:
            if (
                type(launch_credential_sha256) is not bytes
                or len(launch_credential_sha256) != 32
            ):
                raise ValueError("a reader bootstrap needs its launch digest")
        elif kind == OPERATOR_SESSION:
            if launch_credential_sha256 is not None:
                raise ValueError("an operator bootstrap carries no launch digest")
        else:
            raise ValueError("session kind is invalid")
        with self._lock:
            code, digest = self._issue_unique_token(self._active_credential_digests())
            self._bootstrap_sha256 = digest
            self._bootstrap_expires_at = self._now() + self._bootstrap_ttl
            self._bootstrap_authority = authority
            self._bootstrap_kind = kind
            self._bootstrap_launch_sha256 = launch_credential_sha256
            return code

    @staticmethod
    def launch_fragment(code: str) -> str:
        if not BootstrapBroker._strong_token(code):
            raise ValueError("bootstrap credential does not meet strength policy")
        return f"#bootstrap={code}"

    def exchange(self, code: str, *, authority: str) -> SessionGrant:
        """Exchange the pending bootstrap code for a session.

        Only the correct code consumes the slot (H1).  A wrong or malformed
        code counts against the per-window attempt limit and answers 401; once
        the limit is reached, further wrong codes answer 429.  Neither clears
        the pending code, so a local process cannot burn the operator's link.
        An expired code is cleared.
        """

        with self._lock:
            supplied = self._digest(code)
            expected = self._bootstrap_sha256
            now = self._now()
            while (
                self._exchange_attempts
                and now - self._exchange_attempts[0] >= self._exchange_window_seconds
            ):
                self._exchange_attempts.popleft()
            if expected is not None and now > self._bootstrap_expires_at:
                self._clear_bootstrap()
                expected = None
            valid = (
                expected is not None
                and self._bootstrap_authority == authority
                and hmac.compare_digest(supplied, expected)
            )
            if not valid:
                if len(self._exchange_attempts) >= self._max_exchange_attempts:
                    raise BoundaryDenied(429, "TBX-AUTH-005")
                self._exchange_attempts.append(now)
                raise BoundaryDenied(401, "TBX-AUTH-001")
            kind = self._bootstrap_kind
            launch_sha256 = self._bootstrap_launch_sha256
            self._clear_bootstrap()
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
                kind=kind,
                last_seen_at=now,
                launch_credential_sha256=launch_sha256,
            )
            return SessionGrant(
                session_token=session_token, csrf_token=csrf_token, kind=kind
            )

    def _live_record(
        self, session_token: str | None, *, authority: str, now: float
    ) -> tuple[bytes, _SessionRecord]:
        """Caller holds the lock.  An expired or idle record is removed."""

        if session_token is None:
            raise BoundaryDenied(401, "TBX-AUTH-001")
        digest = self._digest(session_token)
        record = self._sessions.get(digest)
        if record is not None and self._ended(record, now):
            self._sessions.pop(digest, None)
            record = None
        if record is None or record.authority != authority:
            raise BoundaryDenied(401, "TBX-AUTH-001")
        return digest, record

    def require_session(
        self, session_token: str | None, *, authority: str
    ) -> _SessionRecord:
        """A live session (not expired, not idle); never extends it."""

        with self._lock:
            return self._live_record(
                session_token, authority=authority, now=self._now()
            )[1]

    def authorize_session(
        self,
        session_token: str | None,
        *,
        authority: str,
        kinds: frozenset[str],
        csrf_token: str | None,
        mutation: bool,
        origin_allowed: bool,
    ) -> _SessionRecord:
        """The one locked session check for a request (H1).

        Expiry, idle timeout and authority (401); then, for a mutation, Origin
        and CSRF (403); then the route's session kinds (403 ``TBX-AUTH-007``).
        Only a request that passes every check refreshes ``last_seen_at``, and
        the refresh happens under the same lock, so a concurrent logout or
        ``end_session`` can never be undone by it.
        """

        if not kinds or not kinds <= ANY_SESSION:
            raise ValueError("route session kinds are invalid")
        with self._lock:
            now = self._now()
            digest, record = self._live_record(
                session_token, authority=authority, now=now
            )
            if mutation:
                if not origin_allowed:
                    raise BoundaryDenied(403, "TBX-AUTH-003")
                self.require_csrf(record, csrf_token)
            if record.kind not in kinds:
                raise BoundaryDenied(403, "TBX-AUTH-007")
            refreshed = dataclasses.replace(record, last_seen_at=now)
            self._sessions[digest] = refreshed
            return refreshed

    def end_session(self, session_token: str | None) -> bool:
        """Remove one session and its reader binding; ``False`` if absent."""

        if session_token is None:
            return False
        with self._lock:
            return self._sessions.pop(self._digest(session_token), None) is not None

    def require_reader_launch(
        self,
        session_token: str | None,
        *,
        authority: str,
        launch_credential_sha256: bytes,
    ) -> None:
        """Deny unless this live session is a reader session born from the
        same link as the presented launch credential (H1, T5)."""

        with self._lock:
            _, record = self._live_record(
                session_token, authority=authority, now=self._now()
            )
            expected = record.launch_credential_sha256
            if (
                record.kind != READER_SESSION
                or expected is None
                or not hmac.compare_digest(expected, launch_credential_sha256)
            ):
                raise BoundaryDenied(403, "TBX-AUTH-007")

    def bind_reader_session(
        self,
        session_token: str | None,
        *,
        authority: str,
        binding: ReaderSessionBinding,
    ) -> None:
        """Attach one reader binding to a live session; never rebinds."""

        if type(binding) is not ReaderSessionBinding:
            raise TypeError("reader session binding has the wrong type")
        with self._lock:
            digest, record = self._live_record(
                session_token, authority=authority, now=self._now()
            )
            if record.kind != READER_SESSION:
                raise BoundaryDenied(403, "TBX-AUTH-007")
            if record.reader_binding is not None:
                raise BoundaryDenied(403, "TBX-AUTH-006")
            self._sessions[digest] = dataclasses.replace(
                record, reader_binding=binding
            )

    def reader_binding(
        self, session_token: str | None, *, authority: str
    ) -> ReaderSessionBinding | None:
        return self.require_session(session_token, authority=authority).reader_binding

    def require_csrf(self, record: _SessionRecord, csrf_token: str | None) -> None:
        if csrf_token is None or not hmac.compare_digest(
            self._digest(csrf_token), record.csrf_sha256
        ):
            raise BoundaryDenied(403, "TBX-AUTH-002")


class LocalWebBoundary:
    def __init__(self, config: LoopbackServerConfig, broker: BootstrapBroker) -> None:
        self.config = config
        self.broker = broker

    def issue_bootstrap(
        self,
        *,
        kind: SessionKind = OPERATOR_SESSION,
        launch_credential_sha256: bytes | None = None,
    ) -> str:
        return self.broker.issue_bootstrap(
            authority=self.config.authority,
            kind=kind,
            launch_credential_sha256=launch_credential_sha256,
        )

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
        self.authorize_bootstrap_request(request)
        return self.broker.exchange(code, authority=self.config.authority)

    def authorize_bootstrap_request(self, request: BrowserRequest) -> None:
        """Reject untrusted authorities before parsing a request body."""

        self._require_host(request)
        self._require_origin(request)
        if request.method != "POST" or request.path != "/api/v1/session/bootstrap":
            raise BoundaryDenied(403, "TBX-AUTH-003")
        if "?" in request.path:
            raise BoundaryDenied(403, "TBX-AUTH-003")

    def authorize_public_asset(self, request: BrowserRequest) -> None:
        """Apply DNS-rebinding defenses before serving even non-sensitive assets."""

        self._require_host(request)

    def authorize(
        self, request: BrowserRequest, *, kinds: frozenset[str] = ANY_SESSION
    ) -> None:
        """Host, then one locked session check
        (:meth:`BootstrapBroker.authorize_session`).

        The server passes each route's kinds from its route table.  The
        default admits any live session; it is used only for re-checks inside
        a route the table already admitted.
        """

        self._require_host(request)
        self.broker.authorize_session(
            request.session_token,
            authority=self.config.authority,
            kinds=kinds,
            csrf_token=request.csrf_token,
            mutation=request.method.upper() not in _SAFE_METHODS,
            origin_allowed=request.origin in self.config.allowed_origins,
        )
