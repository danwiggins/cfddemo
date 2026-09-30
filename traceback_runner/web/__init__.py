"""Local-only operator web boundary.

The package is additive to the preserved Streamlit demo.  It exposes pure,
framework-independent contracts and authorization checks so a later packaged
server cannot silently weaken the loopback threat boundary.
"""

from .auth import (
    BootstrapBroker,
    BoundaryDenied,
    BrowserRequest,
    LocalWebBoundary,
    LoopbackServerConfig,
    SessionGrant,
    build_loopback_config,
)
from .contracts import (
    ActionKind,
    JobAction,
    JobProjection,
    ProblemDetail,
    ProblemOwner,
)
from .server import LocalWebServerError, RunningLocalWebService
from .source import JobStoreProjectionSource

__all__ = [
    "ActionKind",
    "BootstrapBroker",
    "BoundaryDenied",
    "BrowserRequest",
    "JobAction",
    "JobProjection",
    "JobStoreProjectionSource",
    "LocalWebBoundary",
    "LocalWebServerError",
    "LoopbackServerConfig",
    "ProblemDetail",
    "ProblemOwner",
    "RunningLocalWebService",
    "SessionGrant",
    "build_loopback_config",
]
