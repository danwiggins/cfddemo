"""B01 session binding to protected ``longitudinal_reader`` grants for E12.

A B01 session proves only local transport authentication.  E12 routes call
:meth:`ReaderSessionBinder.reader_authorization`, which resolves the session's
server-side reader binding against the live
:class:`~evidence_inspector.reader_authorization_registry.ReaderAuthorizationRegistry`
under its cross-process fence and raises
:class:`~evidence_inspector.reader_authorization_registry.ReaderAuthorizationDenied`
(``permission_denied``) before any other protected read.

The binding is created only by exchanging a separate one-use launch credential
that the server-side launcher minted for one grant selector.  The browser never
supplies a role, principal, grant, scope list or signature.  Existing B01 routes
are unchanged: they never consult the reader binding.

Threat model: the process/OS-user boundary is the trust boundary.  In-process
code mutation and same-user filesystem races are out of scope.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time
from collections import deque
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from evidence_inspector.reader_authorization_registry import (
    MeasurementScope,
    ReaderAuthorization,
    ReaderAuthorizationDenied,
    ReaderAuthorizationRegistry,
    ReaderAuthorizationRegistryError,
    ReaderDenialReason,
)

from .auth import (
    BootstrapBroker,
    BoundaryDenied,
    BrowserRequest,
    LocalWebBoundary,
    ReaderSessionBinding,
)

_GRANT_SELECTOR_PREFIX = "reader_grant_"


def _is_grant_selector(value: object) -> bool:
    return (
        type(value) is str
        and len(value) == len(_GRANT_SELECTOR_PREFIX) + 32
        and value.startswith(_GRANT_SELECTOR_PREFIX)
        and all(
            character in "0123456789abcdef"
            for character in value[len(_GRANT_SELECTOR_PREFIX) :]
        )
    )


@dataclass(frozen=True, slots=True)
class _PendingLaunch:
    grant_selector: str
    expires_at: float
    authority: str


class ReaderSessionBinder:
    """Mint one-use launch credentials and resolve bound sessions under the fence.

    ``registry=None`` means no protected reader registry is configured: E12 is
    disabled and every reader check is denied.
    """

    def __init__(
        self,
        *,
        boundary: LocalWebBoundary,
        registry: ReaderAuthorizationRegistry | None,
        now: Callable[[], float] = time.monotonic,
        launch_ttl_seconds: int = 60,
        max_pending_launches: int = 16,
        exchange_window_seconds: int = 60,
        max_exchange_attempts: int = 8,
    ) -> None:
        if type(boundary) is not LocalWebBoundary:
            raise TypeError("reader binder requires the exact B01 boundary")
        if registry is not None and type(registry) is not ReaderAuthorizationRegistry:
            raise TypeError("reader binder requires the exact reader registry")
        if not 1 <= launch_ttl_seconds <= 300:
            raise ValueError("launch TTL must be between 1 and 300 seconds")
        if not 1 <= max_pending_launches <= 64:
            raise ValueError("pending launch limit must be between 1 and 64")
        if not 1 <= exchange_window_seconds <= 300:
            raise ValueError("exchange window must be between 1 and 300 seconds")
        if not 1 <= max_exchange_attempts <= 64:
            raise ValueError("exchange attempt limit must be between 1 and 64")
        self._boundary = boundary
        self._registry = registry
        self._now = now
        self._launch_ttl = launch_ttl_seconds
        self._max_pending = max_pending_launches
        self._exchange_window = exchange_window_seconds
        self._exchange_attempts: deque[float] = deque(maxlen=max_exchange_attempts)
        self._max_exchange_attempts = max_exchange_attempts
        self._pending: dict[bytes, _PendingLaunch] = {}
        self._lock = threading.RLock()

    @property
    def enabled(self) -> bool:
        return self._registry is not None

    @staticmethod
    def _digest(value: str) -> bytes:
        return hashlib.sha256(value.encode("utf-8")).digest()

    def _broker(self) -> BootstrapBroker:
        return self._boundary.broker

    def issue_launch_credential(self, grant_selector: str) -> str:
        """Server-side launcher only: map a fresh one-use credential to a grant.

        No registry read happens here; exchange verifies the grant under the
        fence.  The credential is never written to disk or logged.
        """

        if not _is_grant_selector(grant_selector):
            raise ValueError("grant selector is invalid")
        with self._lock:
            now = self._now()
            for digest in [
                key for key, item in self._pending.items() if now > item.expires_at
            ]:
                self._pending.pop(digest, None)
            if len(self._pending) >= self._max_pending:
                raise RuntimeError("pending reader launch limit reached")
            for _ in range(4):
                token = secrets.token_urlsafe(32)
                digest = self._digest(token)
                if BootstrapBroker._strong_token(token) and digest not in self._pending:
                    break
            else:
                raise RuntimeError("strong unique credential issuance failed")
            self._pending[digest] = _PendingLaunch(
                grant_selector=grant_selector,
                expires_at=now + self._launch_ttl,
                authority=self._boundary.config.authority,
            )
            return token

    def _consume_launch(self, launch_credential: object) -> str:
        """Remove the credential before verification; any outcome consumes it."""

        with self._lock:
            now = self._now()
            while (
                self._exchange_attempts
                and now - self._exchange_attempts[0] >= self._exchange_window
            ):
                self._exchange_attempts.popleft()
            if len(self._exchange_attempts) >= self._max_exchange_attempts:
                self._pending.clear()
                raise ReaderAuthorizationDenied(
                    ReaderDenialReason.LAUNCH_CREDENTIAL_INVALID
                )
            self._exchange_attempts.append(now)
            if type(launch_credential) is not str or not BootstrapBroker._strong_token(
                launch_credential
            ):
                raise ReaderAuthorizationDenied(
                    ReaderDenialReason.LAUNCH_CREDENTIAL_INVALID
                )
            supplied = self._digest(launch_credential)
            matched: _PendingLaunch | None = None
            for digest in list(self._pending):
                if hmac.compare_digest(digest, supplied):
                    matched = self._pending.pop(digest)
            if (
                matched is None
                or now > matched.expires_at
                or matched.authority != self._boundary.config.authority
            ):
                raise ReaderAuthorizationDenied(
                    ReaderDenialReason.LAUNCH_CREDENTIAL_INVALID
                )
            return matched.grant_selector

    def exchange_launch_credential(
        self, request: BrowserRequest, launch_credential: str
    ) -> None:
        """Bind the request's B01 session to the credential's current grant.

        The B01 session, Host, Origin and CSRF checks run first.  The session
        then stores only the grant commitment and the registry head.
        """

        self._boundary.authorize(request)
        if request.method.upper() != "POST":
            raise BoundaryDenied(403, "TBX-AUTH-003")
        selector = self._consume_launch(launch_credential)
        registry = self._registry
        if registry is None:
            raise ReaderAuthorizationDenied(ReaderDenialReason.AUTHORITY_ABSENT)
        try:
            with registry.authority_read_fence():
                binding = registry.bind_grant_in_fence(selector)
                # Bind while the fence is held so a revocation cannot land
                # between the grant check and the stored binding.
                self._broker().bind_reader_session(
                    request.session_token,
                    authority=self._boundary.config.authority,
                    binding=ReaderSessionBinding(
                        grant_sha256=binding.grant_sha256,
                        registry_head_sha256=binding.state_head_sha256,
                    ),
                )
        except ReaderAuthorizationDenied:
            raise
        except BoundaryDenied as exc:
            if exc.code == "TBX-AUTH-006":
                raise ReaderAuthorizationDenied(
                    ReaderDenialReason.SESSION_ALREADY_BOUND
                ) from None
            raise
        except ReaderAuthorizationRegistryError:
            raise ReaderAuthorizationDenied(
                ReaderDenialReason.REGISTRY_UNAVAILABLE
            ) from None

    def _authorize_in_fence(
        self,
        registry: ReaderAuthorizationRegistry,
        request: BrowserRequest,
        *,
        cohort_registry_id: str,
        measurement_scope: MeasurementScope,
    ) -> ReaderAuthorization:
        binding = self._broker().reader_binding(
            request.session_token, authority=self._boundary.config.authority
        )
        if binding is None:
            raise ReaderAuthorizationDenied(ReaderDenialReason.SESSION_UNBOUND)
        return registry.authorize_reader_in_fence(
            binding.grant_sha256,
            expected_state_head_sha256=binding.registry_head_sha256,
            cohort_registry_id=cohort_registry_id,
            measurement_scope=measurement_scope,
        )

    @contextmanager
    def reader_authorization(
        self,
        request: BrowserRequest,
        *,
        cohort_registry_id: str,
        measurement_scope: MeasurementScope,
    ) -> Iterator[ReaderAuthorization]:
        """The boundary every E12 read or save calls before protected reads.

        B01 transport checks run first and keep their own errors.  Then, under
        the reader-registry fence, the session's bound grant must exist, be
        unrevoked, signed by an active trusted key, current, at the exact bound
        registry head, and in scope for the requested cohort registry and
        measurement.  The fence stays held while the caller builds its result
        and the authorization is re-resolved before the fence is released, so
        no grant add, revocation or key rotation can land in between.
        """

        self._boundary.authorize(request)
        registry = self._registry
        if registry is None:
            raise ReaderAuthorizationDenied(ReaderDenialReason.AUTHORITY_ABSENT)
        try:
            fence = registry.authority_read_fence()
            fence.__enter__()
        except ReaderAuthorizationRegistryError:
            raise ReaderAuthorizationDenied(
                ReaderDenialReason.REGISTRY_UNAVAILABLE
            ) from None
        try:
            authorization = self._checked(
                registry,
                request,
                cohort_registry_id=cohort_registry_id,
                measurement_scope=measurement_scope,
            )
            yield authorization
            final = self._checked(
                registry,
                request,
                cohort_registry_id=cohort_registry_id,
                measurement_scope=measurement_scope,
            )
            if final.model_copy(
                update={"evaluated_at": authorization.evaluated_at}
            ) != authorization:
                raise ReaderAuthorizationDenied(ReaderDenialReason.STALE_HEAD)
        except BaseException as exc:
            try:
                fence.__exit__(type(exc), exc, exc.__traceback__)
            except ReaderAuthorizationRegistryError:
                raise ReaderAuthorizationDenied(
                    ReaderDenialReason.REGISTRY_UNAVAILABLE
                ) from None
            raise
        try:
            fence.__exit__(None, None, None)
        except ReaderAuthorizationRegistryError:
            raise ReaderAuthorizationDenied(
                ReaderDenialReason.REGISTRY_UNAVAILABLE
            ) from None

    def _checked(
        self,
        registry: ReaderAuthorizationRegistry,
        request: BrowserRequest,
        *,
        cohort_registry_id: str,
        measurement_scope: MeasurementScope,
    ) -> ReaderAuthorization:
        try:
            return self._authorize_in_fence(
                registry,
                request,
                cohort_registry_id=cohort_registry_id,
                measurement_scope=measurement_scope,
            )
        except BoundaryDenied:
            raise ReaderAuthorizationDenied(
                ReaderDenialReason.SESSION_UNBOUND
            ) from None
        except ReaderAuthorizationRegistryError:
            raise ReaderAuthorizationDenied(
                ReaderDenialReason.REGISTRY_UNAVAILABLE
            ) from None


__all__ = ["ReaderSessionBinder"]
