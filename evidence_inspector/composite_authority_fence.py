"""Composite authority fence over every E12 dependency store.

E12 Save and the future D08 workspace builder need one atomic view of fifteen
independently locked stores.  Nesting their public reads cannot provide it:
the D01 linkage fence is not reentrant, and D04, D05, D06, D07, D03 and the
anchor registry all open it themselves.  Reading heads one store at a time
and comparing them later leaves a window between the last check and the
returned value.

This module adds one adapter per store and one coordinator:

- Each adapter has a pinned acquire step that enters the store's own read
  fence (or shared lock) and a pinned capture step that reads the store's
  identity and head *assuming the fence is already held*.  No capture step
  re-enters a non-reentrant fence: inside the composite hold, only the
  stores' already-fenced reads run.
- ``CompositeAuthorityCoordinator`` captures the caller's canonical inputs as
  exact bytes before any lock, acquires every adapter in the global order
  (``GLOBAL_LOCK_ORDER``), captures every store's ID, epoch and head, runs
  only bounded canonical reads while the fences are held, revalidates every
  head in reverse order, builds the immutable return value, and only then
  releases.  It accepts no caller callback.
- ``CompositeAuthorityFence`` implements the saved-comparison registry's
  ``SavedComparisonDependencyFence`` on top of the same hold, with
  ``fence_kind = composite_authority_fence``.

Global lock order (see ``docs/COMPOSITE-AUTHORITY-FENCE.md`` for the edges it
is derived from and every deviation from the integration plan)::

    reader authorization -> D10 context -> D09 summary -> D01 linkage
    (D04 history is fenced by D01) -> D05 cohort registry
    -> E04 catalog content (shared, with the connection lock) + result trust
    -> D06 record root
    -> E06 source -> D03 decision -> D07 comparison -> family-source
    -> anchor policy -> projection policy -> saved-comparison registry

Threat model: the process/OS-user boundary is the trust boundary.  In-process
code mutation and same-user filesystem races are out of scope.  Pins and
binding checks detect accidental or naive class/instance replacement only.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from enum import StrEnum
from types import MappingProxyType
from typing import Literal

from pydantic import Field

import evidence_inspector.anchor_policy_registry as anchor_module
import evidence_inspector.cohort_import as d06_module
import evidence_inspector.cohort_registry as d05_module
import evidence_inspector.covariate_context_registry as d10_module
import evidence_inspector.denominator_policy_registry as d09_module
import evidence_inspector.longitudinal_decision_registry as d03_module
import evidence_inspector.measurement_source_artifact_registry as family_module
import evidence_inspector.projection_policy_registry as projection_module
import evidence_inspector.reader_authorization_registry as reader_module
import evidence_inspector.repeatability_comparison_registry as d07_module
import evidence_inspector.result_trust_registry as trust_module
import evidence_inspector.longitudinal_comparison_registry as saved_module
import evidence_inspector.provider_linkage_store as d01_module
import evidence_inspector.record_supersession_store as d04_module
import evidence_inspector.result_catalog as e04_module
import evidence_inspector.result_view_source_registry as e06_module
from evidence_inspector.anchor_policy_registry import AnchorPolicyRegistry
from evidence_inspector.cohort_import import (
    CohortManifestRecordStatus,
    CohortRecordCatalog,
)
from evidence_inspector.cohort_registry import CohortRegistry, CohortRegistryHead
from evidence_inspector.covariate_context_registry import CovariateContextRegistry
from evidence_inspector.denominator_policy_registry import DenominatorPolicyRegistry
from evidence_inspector.longitudinal_comparison_registry import (
    DependencyFenceKind,
    DependencyHeadV1,
    DependencySlot,
    HeldSavedComparisonDependencies,
    LongitudinalComparisonRegistryStale,
    LongitudinalComparisonRegistryUnsafe,
    SAVED_DEPENDENCY_HEADS_SCHEMA_V2,
    SavedComparisonDependencyFence,
    SavedComparisonDependencyHeadsV1,
    SavedComparisonDependencyScopeV1,
    SavedComparisonRegistryBindingsV1,
)
from evidence_inspector.longitudinal_decision_registry import (
    LongitudinalDecisionRegistry,
)
from evidence_inspector.measurement_source_artifact_registry import (
    MeasurementSourceArtifactRegistry,
)
from evidence_inspector.method_registry import (
    RegistryContract,
    canonical_contract_bytes,
    contract_from_canonical_bytes,
)
from evidence_inspector.projection_policy_registry import ProjectionPolicyRegistry
from evidence_inspector.provider_linkage_store import (
    ActiveLinkageSnapshot,
    ProviderLinkageStore,
)
from evidence_inspector.reader_authorization_registry import (
    ReaderAuthorizationRegistry,
)
from evidence_inspector.record_supersession_store import (
    ActiveRecordSnapshot,
    RecordSupersessionStore,
)
from evidence_inspector.repeatability_comparison_registry import (
    RepeatabilityComparisonRegistry,
)
from evidence_inspector.result_catalog import (
    CatalogAuthoritySnapshot,
    CatalogContentSnapshot,
    ResultCatalog,
    catalog_dependency_head_sha256,
)
from evidence_inspector.result_trust_registry import (
    ResultTrustRegistry,
    ResultTrustSnapshot,
)
from evidence_inspector.result_view_source_registry import (
    ResultViewSourceRegistry,
    ResultViewSourceRegistryIdentity,
)
from evidence_inspector.safe_ingress import contract_type_graph, exact_model_bytes

# --- errors ---------------------------------------------------------------------


class CompositeAuthorityError(RuntimeError):
    """Sanitized composite-fence failure; messages carry no store identity."""

    code = "composite_authority_error"


class CompositeAuthorityUnsafe(CompositeAuthorityError):
    """A store type, binding, pin or hold is not what the fence was built over."""

    code = "integrity_failure"


class CompositeAuthorityStale(CompositeAuthorityError):
    """A store could not produce current authority inside the fence."""

    code = "authority_stale"


class CompositeAuthorityRetry(CompositeAuthorityStale):
    """A captured head did not revalidate; no value was returned.  Retry."""

    code = "authority_retry"


# --- global order ----------------------------------------------------------------


class CompositeLockStep(StrEnum):
    """One acquisition step, in the global lock order."""

    READER_AUTHORIZATION = "reader_authorization"
    D10_CONTEXT = "d10_context"
    D09_SUMMARY = "d09_summary"
    D01_LINKAGE = "d01_linkage"
    D04_HISTORY = "d04_history"
    D05_COHORT = "d05_cohort"
    E04_CATALOG_TRUST = "e04_catalog_trust"
    D06_RECORD_CATALOG = "d06_record_catalog"
    E06_SOURCE = "e06_source"
    D03_DECISION = "d03_decision"
    D07_COMPARISON = "d07_comparison"
    FAMILY_SOURCE = "family_source"
    ANCHOR_POLICY = "anchor_policy"
    PROJECTION_POLICY = "projection_policy"


GLOBAL_LOCK_ORDER: tuple[CompositeLockStep, ...] = tuple(CompositeLockStep)

# Head slots each step captures, in capture order.  D06 is scoped to one
# cohort version, so its slot is captured per scope, not with the step.
_STEP_SLOTS: MappingProxyType[CompositeLockStep, tuple[DependencySlot, ...]] = (
    MappingProxyType(
        {
            CompositeLockStep.READER_AUTHORIZATION: (
                DependencySlot.READER_AUTHORIZATION,
            ),
            CompositeLockStep.D10_CONTEXT: (DependencySlot.D10_CONTEXT,),
            CompositeLockStep.D09_SUMMARY: (DependencySlot.D09_SUMMARY,),
            CompositeLockStep.D01_LINKAGE: (DependencySlot.D01_LINKAGE,),
            CompositeLockStep.D04_HISTORY: (DependencySlot.D04_HISTORY,),
            CompositeLockStep.D05_COHORT: (DependencySlot.D05_COHORT,),
            CompositeLockStep.E04_CATALOG_TRUST: (
                DependencySlot.E04_CATALOG,
                DependencySlot.RESULT_TRUST,
            ),
            CompositeLockStep.D06_RECORD_CATALOG: (),
            CompositeLockStep.E06_SOURCE: (DependencySlot.E06_SOURCE,),
            CompositeLockStep.D03_DECISION: (DependencySlot.D03_DECISION,),
            CompositeLockStep.D07_COMPARISON: (DependencySlot.D07_COMPARISON,),
            CompositeLockStep.FAMILY_SOURCE: (DependencySlot.FAMILY_SOURCE,),
            CompositeLockStep.ANCHOR_POLICY: (DependencySlot.ANCHOR_POLICY,),
            CompositeLockStep.PROJECTION_POLICY: (DependencySlot.PROJECTION_POLICY,),
        }
    )
)


# --- pinned store operations -----------------------------------------------------
#
# Captured once at import.  Every acquisition re-checks that each class still
# carries exactly these functions and that the instance does not shadow them,
# then invokes the captured unbound function.  Lock-only registries expose no
# public in-fence read, so their private ``_lock`` and ``_load_state`` are
# pinned here, as the merged stores already pin one another's (D06 and the
# anchor registry pin D05; D07 pins D01's fence state).

_PINNED_LINKAGE_FENCE = ProviderLinkageStore.authority_read_fence
_PINNED_LINKAGE_SNAPSHOT = ProviderLinkageStore.active_snapshot
_PINNED_HISTORY_SNAPSHOT = RecordSupersessionStore.active_snapshot
_PINNED_COHORT_FENCE = CohortRegistry.authority_read_fence
_PINNED_COHORT_HEAD = CohortRegistry.head_in_fence
_PINNED_READER_FENCE = ReaderAuthorizationRegistry.authority_read_fence
_PINNED_READER_LOAD_STATE = ReaderAuthorizationRegistry._load_state
_PINNED_CATALOG_TRUST_FENCE = ResultCatalog.trust_authority_fence
_PINNED_CATALOG_AUTHORITY = ResultCatalog.authority_snapshot
_PINNED_CATALOG_CONTENT_HEAD = ResultCatalog.content_head_in_fence
_PINNED_TRUST_SNAPSHOT_LOCKED = ResultTrustRegistry._snapshot_locked
_PINNED_STATUS_FENCE = CohortRecordCatalog.record_status_read_fence
_PINNED_STATUS_IN_FENCE = CohortRecordCatalog.record_status_in_fence
_PINNED_E06_IDENTITY = ResultViewSourceRegistry.registry_identity

_CLASS_PINS: MappingProxyType[type, MappingProxyType[str, object]] = MappingProxyType(
    {
        ProviderLinkageStore: MappingProxyType(
            {
                "authority_read_fence": _PINNED_LINKAGE_FENCE,
                "active_snapshot": _PINNED_LINKAGE_SNAPSHOT,
            }
        ),
        RecordSupersessionStore: MappingProxyType(
            {"active_snapshot": _PINNED_HISTORY_SNAPSHOT}
        ),
        CohortRegistry: MappingProxyType(
            {
                "authority_read_fence": _PINNED_COHORT_FENCE,
                "head_in_fence": _PINNED_COHORT_HEAD,
            }
        ),
        ReaderAuthorizationRegistry: MappingProxyType(
            {
                "authority_read_fence": _PINNED_READER_FENCE,
                "_load_state": _PINNED_READER_LOAD_STATE,
            }
        ),
        ResultCatalog: MappingProxyType(
            {
                "trust_authority_fence": _PINNED_CATALOG_TRUST_FENCE,
                "authority_snapshot": _PINNED_CATALOG_AUTHORITY,
                "content_head_in_fence": _PINNED_CATALOG_CONTENT_HEAD,
            }
        ),
        ResultTrustRegistry: MappingProxyType(
            {"_snapshot_locked": _PINNED_TRUST_SNAPSHOT_LOCKED}
        ),
        CohortRecordCatalog: MappingProxyType(
            {
                "record_status_read_fence": _PINNED_STATUS_FENCE,
                "record_status_in_fence": _PINNED_STATUS_IN_FENCE,
            }
        ),
        **{
            cls: MappingProxyType(
                {"_lock": cls.__dict__["_lock"], "_load_state": cls.__dict__["_load_state"]}
            )
            for cls in (
                CovariateContextRegistry,
                DenominatorPolicyRegistry,
                LongitudinalDecisionRegistry,
                RepeatabilityComparisonRegistry,
                MeasurementSourceArtifactRegistry,
                AnchorPolicyRegistry,
                ProjectionPolicyRegistry,
            )
        },
        ResultViewSourceRegistry: MappingProxyType(
            {
                "_lock": ResultViewSourceRegistry.__dict__["_lock"],
                "_load_state": ResultViewSourceRegistry.__dict__["_load_state"],
                "registry_identity": _PINNED_E06_IDENTITY,
            }
        ),
    }
)

# Each store module's own seal check (class, instance and alias seals).
_INTEGRITY: MappingProxyType[type, Callable[[object], None] | None] = MappingProxyType(
    {
        ProviderLinkageStore: None,
        RecordSupersessionStore: None,
        CohortRegistry: d05_module._require_registry_integrity,
        ReaderAuthorizationRegistry: reader_module._require_registry_integrity,
        ResultCatalog: None,
        ResultTrustRegistry: trust_module._require_registry_integrity,
        CohortRecordCatalog: d06_module._assert_cohort_runtime,
        CovariateContextRegistry: d10_module._require_registry_integrity,
        DenominatorPolicyRegistry: d09_module._require_registry_integrity,
        LongitudinalDecisionRegistry: d03_module._require_registry_integrity,
        RepeatabilityComparisonRegistry: d07_module._require_registry_integrity,
        MeasurementSourceArtifactRegistry: family_module._require_registry_integrity,
        AnchorPolicyRegistry: anchor_module._require_registry_integrity,
        ProjectionPolicyRegistry: projection_module._require_registry_integrity,
        ResultViewSourceRegistry: e06_module._require_registry_integrity,
    }
)


def _instance_state(store: object) -> dict[str, object]:
    state = object.__getattribute__(store, "__dict__")
    if type(state) is not dict:
        raise CompositeAuthorityUnsafe("dependency store state is invalid")
    return state


def _require_pinned(store: object, cls: type) -> None:
    """Exact type, unshadowed pinned callables, and the store's own seals."""

    if type(store) is not cls:
        raise CompositeAuthorityUnsafe("dependency store type changed")
    state = _instance_state(store)
    for name, pinned in _CLASS_PINS[cls].items():
        if cls.__dict__.get(name) is not pinned or name in state:
            raise CompositeAuthorityUnsafe("dependency store callable changed")
    integrity = _INTEGRITY[cls]
    if integrity is not None:
        try:
            integrity(store)
        except Exception:
            raise CompositeAuthorityUnsafe(
                "dependency store integrity check failed"
            ) from None


def _registry_identity(store: object) -> tuple[str, str]:
    metadata = _instance_state(store).get("_metadata")
    registry_id = getattr(metadata, "registry_id", None)
    epoch = getattr(metadata, "registry_epoch_sha256", None)
    if type(registry_id) is not str or type(epoch) is not str:
        raise CompositeAuthorityUnsafe("dependency store identity is invalid")
    return registry_id, epoch


def _head(registry_id: object, epoch: object, head: object) -> DependencyHeadV1:
    try:
        return DependencyHeadV1(id=registry_id, epoch=epoch, head=head)
    except Exception:
        raise CompositeAuthorityUnsafe("dependency store head is invalid") from None


# --- adapters ---------------------------------------------------------------------


class _Adapter:
    """One store's pinned acquire step and already-fenced capture step.

    ``acquire`` enters the store's fence on ``stack`` (released in reverse by
    the coordinator).  ``capture`` assumes the fence is held, re-enters no
    fence, and returns this step's head slots.  ``bind`` returns the
    immutable identity the coordinator binds at construction.
    """

    step: CompositeLockStep

    def __init__(self, store: object, cls: type) -> None:
        _require_pinned(store, cls)
        self.store = store
        self.cls = cls

    def acquire(self, stack: ExitStack) -> None:
        raise NotImplementedError

    def capture(self) -> dict[DependencySlot, DependencyHeadV1]:
        raise NotImplementedError

    def bind(self) -> tuple[object, ...]:
        return (self.cls, id(self.store))


class _RegistryLockAdapter(_Adapter):
    """Lock-only registry: shared ``_lock``, head from ``_load_state``."""

    def __init__(
        self, step: CompositeLockStep, store: object, cls: type, slot: DependencySlot
    ) -> None:
        super().__init__(store, cls)
        self.step = step
        self.slot = slot
        self._identity = self.identity()

    def identity(self) -> tuple[str, str]:
        return _registry_identity(self.store)

    def bind(self) -> tuple[object, ...]:
        return (self.cls, id(self.store), self._identity)

    def acquire(self, stack: ExitStack) -> None:
        _require_pinned(self.store, self.cls)
        stack.enter_context(_CLASS_PINS[self.cls]["_lock"](self.store, exclusive=False))

    def capture(self) -> dict[DependencySlot, DependencyHeadV1]:
        _require_pinned(self.store, self.cls)
        result = _CLASS_PINS[self.cls]["_load_state"](self.store)
        if type(result) is not tuple or len(result) != 2:
            raise CompositeAuthorityUnsafe("dependency store state is invalid")
        identity = self.identity()
        if identity != self._identity:
            raise CompositeAuthorityUnsafe("dependency store identity changed")
        return {self.slot: _head(*identity, result[1])}


class _SourceAdapter(_RegistryLockAdapter):
    """E06: identity from the public lock-free ``registry_identity``."""

    def identity(self) -> tuple[str, str]:
        value = _PINNED_E06_IDENTITY(self.store)
        if type(value) is not ResultViewSourceRegistryIdentity:
            raise CompositeAuthorityUnsafe("E06 source registry identity is invalid")
        return value.registry_id, value.registry_epoch_sha256

    def cohort_binding(self) -> tuple[str, str]:
        value = _PINNED_E06_IDENTITY(self.store)
        if type(value) is not ResultViewSourceRegistryIdentity:
            raise CompositeAuthorityUnsafe("E06 source registry identity is invalid")
        return value.cohort_registry_id, value.cohort_registry_epoch_sha256


class _ReaderAdapter(_Adapter):
    step = CompositeLockStep.READER_AUTHORIZATION

    def __init__(self, store: ReaderAuthorizationRegistry) -> None:
        super().__init__(store, ReaderAuthorizationRegistry)
        self._identity = _registry_identity(store)

    def bind(self) -> tuple[object, ...]:
        return (self.cls, id(self.store), self._identity)

    def acquire(self, stack: ExitStack) -> None:
        _require_pinned(self.store, self.cls)
        stack.enter_context(_PINNED_READER_FENCE(self.store))

    def capture(self) -> dict[DependencySlot, DependencyHeadV1]:
        _require_pinned(self.store, self.cls)
        state = _PINNED_READER_LOAD_STATE(self.store)
        if _registry_identity(self.store) != self._identity:
            raise CompositeAuthorityUnsafe("reader registry identity changed")
        return {
            DependencySlot.READER_AUTHORIZATION: _head(
                *self._identity, getattr(state, "head", None)
            )
        }


class _LinkageAdapter(_Adapter):
    step = CompositeLockStep.D01_LINKAGE

    def __init__(self, store: ProviderLinkageStore) -> None:
        super().__init__(store, ProviderLinkageStore)

    def acquire(self, stack: ExitStack) -> None:
        _require_pinned(self.store, self.cls)
        stack.enter_context(_PINNED_LINKAGE_FENCE(self.store))

    def snapshot(self) -> ActiveLinkageSnapshot:
        # Inside the held fence this nests as a SAVEPOINT on the fence's own
        # transaction; it acquires no new lock.
        _require_pinned(self.store, self.cls)
        snapshot = _PINNED_LINKAGE_SNAPSHOT(self.store)
        if type(snapshot) is not ActiveLinkageSnapshot:
            raise CompositeAuthorityUnsafe("linkage snapshot is invalid")
        return snapshot

    def capture(self) -> dict[DependencySlot, DependencyHeadV1]:
        snapshot = self.snapshot()
        return {
            DependencySlot.D01_LINKAGE: _head(
                snapshot.store_id,
                snapshot.store_epoch_sha256,
                snapshot.state_head_sha256,
            )
        }


class _HistoryAdapter(_Adapter):
    """D04 has no lock of its own in the order: every D04 read and write takes
    the D01 linkage fence first, so the held D01 fence already fences it."""

    step = CompositeLockStep.D04_HISTORY

    def __init__(self, store: RecordSupersessionStore, linkage: _LinkageAdapter) -> None:
        super().__init__(store, RecordSupersessionStore)
        self.linkage = linkage

    def acquire(self, stack: ExitStack) -> None:
        _require_pinned(self.store, self.cls)

    def capture(self) -> dict[DependencySlot, DependencyHeadV1]:
        _require_pinned(self.store, self.cls)
        snapshot = _PINNED_HISTORY_SNAPSHOT(self.store)
        if type(snapshot) is not ActiveRecordSnapshot:
            raise CompositeAuthorityUnsafe("record history snapshot is invalid")
        linkage = self.linkage.snapshot()
        if (
            snapshot.linkage_store_id,
            snapshot.linkage_store_epoch_sha256,
            snapshot.linkage_state_head_sha256,
        ) != (linkage.store_id, linkage.store_epoch_sha256, linkage.state_head_sha256):
            # D01 is held, so the two cannot legitimately diverge here.
            raise CompositeAuthorityUnsafe("record history is not bound to live linkage")
        return {
            DependencySlot.D04_HISTORY: _head(
                snapshot.ledger_id,
                snapshot.ledger_epoch_sha256,
                snapshot.state_head_sha256,
            )
        }


class _CohortAdapter(_Adapter):
    step = CompositeLockStep.D05_COHORT

    def __init__(self, store: CohortRegistry) -> None:
        super().__init__(store, CohortRegistry)
        self._identity = _registry_identity(store)

    def bind(self) -> tuple[object, ...]:
        return (self.cls, id(self.store), self._identity)

    def acquire(self, stack: ExitStack) -> None:
        _require_pinned(self.store, self.cls)
        stack.enter_context(_PINNED_COHORT_FENCE(self.store))

    def capture(self) -> dict[DependencySlot, DependencyHeadV1]:
        _require_pinned(self.store, self.cls)
        value = _PINNED_COHORT_HEAD(self.store)
        if type(value) is not CohortRegistryHead or (
            value.registry_id,
            value.registry_epoch_sha256,
        ) != self._identity:
            raise CompositeAuthorityUnsafe("cohort registry head is invalid")
        return {
            DependencySlot.D05_COHORT: _head(
                value.registry_id, value.registry_epoch_sha256, value.state_head_sha256
            )
        }


class _CatalogTrustAdapter(_Adapter):
    """E04 catalog-content lock (shared; it takes the E04 connection lock),
    then result trust.

    ``ResultCatalog.trust_authority_fence`` takes both, in that order,
    and marks this thread so every catalog verification inside reuses the
    one held trust snapshot.  Trust add/revoke, and every E04 catalog-row
    writer (import, staging, adoption, finish, compensation, discard,
    candidates, recovery), in any process, block on it.  The E04 head is the
    saved-head v2 definition: catalog authority plus catalog content.
    """

    step = CompositeLockStep.E04_CATALOG_TRUST

    def __init__(self, store: ResultCatalog, trust: ResultTrustRegistry) -> None:
        super().__init__(store, ResultCatalog)
        _require_pinned(trust, ResultTrustRegistry)
        self.trust = trust
        self._trust_identity = _registry_identity(trust)

    def bind(self) -> tuple[object, ...]:
        return (self.cls, id(self.store), id(self.trust), self._trust_identity)

    def acquire(self, stack: ExitStack) -> None:
        _require_pinned(self.store, self.cls)
        _require_pinned(self.trust, ResultTrustRegistry)
        snapshot = stack.enter_context(_PINNED_CATALOG_TRUST_FENCE(self.store))
        if (
            type(snapshot) is not ResultTrustSnapshot
            or (snapshot.registry_id, snapshot.registry_epoch_sha256)
            != self._trust_identity
        ):
            raise CompositeAuthorityUnsafe("catalog result trust is not bound")

    def capture(self) -> dict[DependencySlot, DependencyHeadV1]:
        _require_pinned(self.store, self.cls)
        _require_pinned(self.trust, ResultTrustRegistry)
        authority = _PINNED_CATALOG_AUTHORITY(self.store)
        if type(authority) is not CatalogAuthoritySnapshot:
            raise CompositeAuthorityUnsafe("catalog authority is invalid")
        # Read under the held shared content lock; takes no lock of its own.
        content = _PINNED_CATALOG_CONTENT_HEAD(self.store)
        if type(content) is not CatalogContentSnapshot:
            raise CompositeAuthorityUnsafe("catalog content head is invalid")
        # Re-read the trust journal directly: the shared trust lock is held
        # by this thread and is not reentrant.
        trust = _PINNED_TRUST_SNAPSHOT_LOCKED(self.trust)
        if type(trust) is not ResultTrustSnapshot or (
            trust.registry_id,
            trust.registry_epoch_sha256,
        ) != self._trust_identity:
            raise CompositeAuthorityUnsafe("result trust snapshot is invalid")
        storage = authority.storage_identity_sha256
        return {
            DependencySlot.E04_CATALOG: _head(
                "e04_catalog_" + storage[:32],
                storage,
                catalog_dependency_head_sha256(authority, content),
            ),
            DependencySlot.RESULT_TRUST: _head(
                trust.registry_id, trust.registry_epoch_sha256, trust.state_head_sha256
            ),
        }


class _RecordCatalogAdapter(_Adapter):
    """D06 record root; status is scoped, so it is captured per cohort version."""

    step = CompositeLockStep.D06_RECORD_CATALOG

    def __init__(self, store: CohortRecordCatalog) -> None:
        super().__init__(store, CohortRecordCatalog)

    def acquire(self, stack: ExitStack) -> None:
        _require_pinned(self.store, self.cls)
        stack.enter_context(_PINNED_STATUS_FENCE(self.store))

    def capture(self) -> dict[DependencySlot, DependencyHeadV1]:
        return {}

    def capture_scope(self, scope: SavedComparisonDependencyScopeV1) -> DependencyHeadV1:
        _require_pinned(self.store, self.cls)
        status = _PINNED_STATUS_IN_FENCE(
            self.store, scope.cohort_selector_id, scope.cohort_version
        )
        if type(status) is not CohortManifestRecordStatus:
            raise CompositeAuthorityUnsafe("record status is invalid")
        return _head(status.registry_id, status.registry_epoch_sha256, status.status_sha256)


# --- immutable results ----------------------------------------------------------


class CompositeAuthoritySnapshotV1(RegistryContract):
    """Every dependency head and binding captured under one composite hold.

    It was built after every head revalidated in reverse order and before any
    fence was released.  It is a point-in-time record, not a lease: current
    authority needs a new snapshot.
    """

    schema_version: Literal["traceback.composite-authority-snapshot.v1"] = (
        "traceback.composite-authority-snapshot.v1"
    )
    fence_kind: Literal[DependencyFenceKind.COMPOSITE_AUTHORITY_FENCE] = (
        DependencyFenceKind.COMPOSITE_AUTHORITY_FENCE
    )
    lock_order: tuple[CompositeLockStep, ...] = Field(
        min_length=len(GLOBAL_LOCK_ORDER), max_length=len(GLOBAL_LOCK_ORDER)
    )
    scope: SavedComparisonDependencyScopeV1
    heads: SavedComparisonDependencyHeadsV1
    bindings: SavedComparisonRegistryBindingsV1


_SNAPSHOT = CompositeAuthoritySnapshotV1
_SCOPE_MODEL_TYPES, _SCOPE_ENUM_TYPES = contract_type_graph(
    SavedComparisonDependencyScopeV1
)


def _capture_scope(scope: object) -> SavedComparisonDependencyScopeV1:
    """Exact bounded bytes of a caller scope, re-parsed; never the caller object."""

    if type(scope) is not SavedComparisonDependencyScopeV1:
        raise CompositeAuthorityUnsafe("dependency scope is not the exact contract")
    try:
        content = exact_model_bytes(
            scope,
            SavedComparisonDependencyScopeV1,
            model_types=_SCOPE_MODEL_TYPES,
            enum_types=_SCOPE_ENUM_TYPES,
            max_bytes=1024,
            max_nodes=32,
            max_depth=4,
            max_collection_items=4,
            max_string_bytes=128,
        )
        return contract_from_canonical_bytes(SavedComparisonDependencyScopeV1, content)
    except Exception:
        raise CompositeAuthorityUnsafe("dependency scope is invalid") from None


# --- coordinator ------------------------------------------------------------------


class _CompositeHold:
    """State of one active composite hold, usable only on its owning thread."""

    def __init__(self, coordinator: CompositeAuthorityCoordinator) -> None:
        self._coordinator = coordinator
        self._owner = (os.getpid(), threading.get_ident())
        self._active = True
        self._base: dict[DependencySlot, DependencyHeadV1] = {}
        self._scoped: dict[bytes, tuple[SavedComparisonDependencyScopeV1, DependencyHeadV1]] = {}

    def require_active(self) -> None:
        if not self._active or self._owner != (os.getpid(), threading.get_ident()):
            raise CompositeAuthorityUnsafe("composite authority hold is not active")

    def scoped_head(self, scope: SavedComparisonDependencyScopeV1) -> DependencyHeadV1:
        self.require_active()
        key = canonical_contract_bytes(scope)
        known = self._scoped.get(key)
        if known is not None:
            return known[1]
        head = _guard(lambda: self._coordinator._records.capture_scope(scope))
        self._scoped[key] = (scope, head)
        return head

    def heads(self, scope: SavedComparisonDependencyScopeV1) -> SavedComparisonDependencyHeadsV1:
        self.require_active()
        d06 = self.scoped_head(scope)
        base = self._base
        try:
            return SavedComparisonDependencyHeadsV1(
                schema_version=SAVED_DEPENDENCY_HEADS_SCHEMA_V2,
                d01_linkage=base[DependencySlot.D01_LINKAGE],
                d04_history=base[DependencySlot.D04_HISTORY],
                d05_cohort=base[DependencySlot.D05_COHORT],
                reader_authorization=base[DependencySlot.READER_AUTHORIZATION],
                d06_record_catalog=d06,
                e04_catalog=base[DependencySlot.E04_CATALOG],
                result_trust=base[DependencySlot.RESULT_TRUST],
                e06_source=base[DependencySlot.E06_SOURCE],
                d03_decision=base[DependencySlot.D03_DECISION],
                d07_comparison=base[DependencySlot.D07_COMPARISON],
                d09_summary=base[DependencySlot.D09_SUMMARY],
                d10_context=base[DependencySlot.D10_CONTEXT],
                family_source=base[DependencySlot.FAMILY_SOURCE],
                anchor_policy=base[DependencySlot.ANCHOR_POLICY],
                projection_policy=base[DependencySlot.PROJECTION_POLICY],
            )
        except Exception:
            raise CompositeAuthorityUnsafe("dependency heads are not coherent") from None

    def bindings(self) -> SavedComparisonRegistryBindingsV1:
        self.require_active()
        base = self._base
        e06 = base[DependencySlot.E06_SOURCE]
        if self._coordinator._sources.cohort_binding() != (
            base[DependencySlot.D05_COHORT].id,
            base[DependencySlot.D05_COHORT].epoch,
        ):
            raise CompositeAuthorityUnsafe("E06 is bound to another cohort registry")
        try:
            return SavedComparisonRegistryBindingsV1(
                cohort_registry_id=base[DependencySlot.D05_COHORT].id,
                cohort_registry_epoch_sha256=base[DependencySlot.D05_COHORT].epoch,
                d04_ledger_id=base[DependencySlot.D04_HISTORY].id,
                d04_ledger_epoch_sha256=base[DependencySlot.D04_HISTORY].epoch,
                reader_registry_id=base[DependencySlot.READER_AUTHORIZATION].id,
                reader_registry_epoch_sha256=base[
                    DependencySlot.READER_AUTHORIZATION
                ].epoch,
                d06_catalog_storage_identity_sha256=base[DependencySlot.E04_CATALOG].epoch,
                e06_registry_id=e06.id,
                e06_registry_epoch_sha256=e06.epoch,
            )
        except Exception:
            raise CompositeAuthorityUnsafe("dependency bindings are invalid") from None

    def revalidate(self) -> None:
        """Re-read every captured head in reverse lock order; any change fails.

        Runs while the complete fence set is still held.  Scoped D06 heads are
        rechecked in reverse capture order at the D06 step.
        """

        self.require_active()
        coordinator = self._coordinator
        for adapter in reversed(coordinator._adapters):
            if adapter.step is CompositeLockStep.D06_RECORD_CATALOG:
                for scope, head in reversed(tuple(self._scoped.values())):
                    current = _guard(lambda: coordinator._records.capture_scope(scope))
                    if current != head:
                        raise CompositeAuthorityRetry(
                            "dependency authority changed under the composite fence"
                        )
                continue
            current = _guard(adapter.capture)
            for slot in _STEP_SLOTS[adapter.step]:
                if current.get(slot) != self._base.get(slot):
                    raise CompositeAuthorityRetry(
                        "dependency authority changed under the composite fence"
                    )


# Store failures that mean tamper, corruption or a broken fence rather than
# authority that is merely not current.  They must not be retried as stale.
_UNSAFE_STORE_ERRORS: tuple[type[BaseException], ...] = (
    d01_module.ProviderLinkageStoreUnsafe,
    d04_module.RecordSupersessionUnsafe,
    d05_module.CohortRegistryUnsafe,
    reader_module.ReaderAuthorizationRegistryUnsafe,
    d06_module.CohortImportFilesystemError,
    e04_module.CatalogFilesystemError,
    e04_module.CatalogUnsupportedSchema,
    trust_module.ResultTrustRegistryUnsafe,
    e06_module.ResultViewSourceRegistryUnsafe,
    d03_module.LongitudinalDecisionRegistryUnsafe,
    d07_module.RepeatabilityComparisonRegistryUnsafe,
    d09_module.DenominatorPolicyRegistryUnsafe,
    d10_module.CovariateContextRegistryUnsafe,
    family_module.MeasurementSourceArtifactRegistryUnsafe,
    anchor_module.AnchorPolicyRegistryUnsafe,
    projection_module.ProjectionPolicyRegistryUnsafe,
)


def _guard(operation: Callable[[], object]) -> object:
    """Map a store failure inside the fence to a sanitized composite error.

    Integrity failures become ``CompositeAuthorityUnsafe``; any other store
    failure (not current, conflict, busy) becomes ``CompositeAuthorityStale``.
    ``CompositeAuthorityRetry`` is reserved for an observed head mismatch.
    """

    try:
        return operation()
    except CompositeAuthorityError:
        raise
    except _UNSAFE_STORE_ERRORS:
        raise CompositeAuthorityUnsafe("dependency store integrity failed") from None
    except Exception:
        raise CompositeAuthorityStale("dependency authority is unavailable") from None


# Module-level process locks every store fence of these modules takes first.
_MODULE_PROCESS_LOCKS = (
    reader_module._REGISTRY_PROCESS_LOCK,
    d10_module._REGISTRY_PROCESS_LOCK,
    d09_module._REGISTRY_PROCESS_LOCK,
    d04_module._SQLITE_OPEN_LOCK,
    d05_module._REGISTRY_PROCESS_LOCK,
    trust_module._REGISTRY_PROCESS_LOCK,
    d06_module._PROCESS_LOCK,
    e06_module._REGISTRY_PROCESS_LOCK,
    d03_module._REGISTRY_PROCESS_LOCK,
    d07_module._REGISTRY_PROCESS_LOCK,
    family_module._REGISTRY_PROCESS_LOCK,
    anchor_module._REGISTRY_PROCESS_LOCK,
    projection_module._REGISTRY_PROCESS_LOCK,
    saved_module._REGISTRY_PROCESS_LOCK,
)


class CompositeAuthorityCoordinator:
    """Acquire every dependency fence in the global order and read under it.

    Construction requires the exact merged store classes and checks that they
    are wired to one another (one D01 store, one D05 registry, one D06
    catalog over one E04 catalog and one result-trust registry, and so on).
    Those bindings are re-checked at every hold.
    """

    def __init__(
        self,
        *,
        linkage_store: ProviderLinkageStore,
        record_history_store: RecordSupersessionStore,
        cohort_registry: CohortRegistry,
        reader_registry: ReaderAuthorizationRegistry,
        record_catalog: CohortRecordCatalog,
        result_catalog: ResultCatalog,
        result_trust_registry: ResultTrustRegistry,
        source_registry: ResultViewSourceRegistry,
        decision_registry: LongitudinalDecisionRegistry,
        comparison_registry: RepeatabilityComparisonRegistry,
        d09_registry: DenominatorPolicyRegistry,
        d10_registry: CovariateContextRegistry,
        family_source_registry: MeasurementSourceArtifactRegistry,
        anchor_registry: AnchorPolicyRegistry,
        projection_registry: ProjectionPolicyRegistry,
    ) -> None:
        expected = (
            (linkage_store, ProviderLinkageStore),
            (record_history_store, RecordSupersessionStore),
            (cohort_registry, CohortRegistry),
            (reader_registry, ReaderAuthorizationRegistry),
            (record_catalog, CohortRecordCatalog),
            (result_catalog, ResultCatalog),
            (result_trust_registry, ResultTrustRegistry),
            (source_registry, ResultViewSourceRegistry),
            (decision_registry, LongitudinalDecisionRegistry),
            (comparison_registry, RepeatabilityComparisonRegistry),
            (d09_registry, DenominatorPolicyRegistry),
            (d10_registry, CovariateContextRegistry),
            (family_source_registry, MeasurementSourceArtifactRegistry),
            (anchor_registry, AnchorPolicyRegistry),
            (projection_registry, ProjectionPolicyRegistry),
        )
        if any(type(value) is not kind for value, kind in expected):
            raise TypeError("composite fence requires the exact merged store types")
        self._linkage = _LinkageAdapter(linkage_store)
        self._records = _RecordCatalogAdapter(record_catalog)
        self._sources = _SourceAdapter(
            CompositeLockStep.E06_SOURCE,
            source_registry,
            ResultViewSourceRegistry,
            DependencySlot.E06_SOURCE,
        )
        adapters: tuple[_Adapter, ...] = (
            _ReaderAdapter(reader_registry),
            _RegistryLockAdapter(
                CompositeLockStep.D10_CONTEXT,
                d10_registry,
                CovariateContextRegistry,
                DependencySlot.D10_CONTEXT,
            ),
            _RegistryLockAdapter(
                CompositeLockStep.D09_SUMMARY,
                d09_registry,
                DenominatorPolicyRegistry,
                DependencySlot.D09_SUMMARY,
            ),
            self._linkage,
            _HistoryAdapter(record_history_store, self._linkage),
            _CohortAdapter(cohort_registry),
            _CatalogTrustAdapter(result_catalog, result_trust_registry),
            self._records,
            self._sources,
            _RegistryLockAdapter(
                CompositeLockStep.D03_DECISION,
                decision_registry,
                LongitudinalDecisionRegistry,
                DependencySlot.D03_DECISION,
            ),
            _RegistryLockAdapter(
                CompositeLockStep.D07_COMPARISON,
                comparison_registry,
                RepeatabilityComparisonRegistry,
                DependencySlot.D07_COMPARISON,
            ),
            _RegistryLockAdapter(
                CompositeLockStep.FAMILY_SOURCE,
                family_source_registry,
                MeasurementSourceArtifactRegistry,
                DependencySlot.FAMILY_SOURCE,
            ),
            _RegistryLockAdapter(
                CompositeLockStep.ANCHOR_POLICY,
                anchor_registry,
                AnchorPolicyRegistry,
                DependencySlot.ANCHOR_POLICY,
            ),
            _RegistryLockAdapter(
                CompositeLockStep.PROJECTION_POLICY,
                projection_registry,
                ProjectionPolicyRegistry,
                DependencySlot.PROJECTION_POLICY,
            ),
        )
        if tuple(adapter.step for adapter in adapters) != GLOBAL_LOCK_ORDER:
            raise CompositeAuthorityUnsafe("composite fence order is invalid")
        self._adapters = adapters
        self._stores = MappingProxyType(
            {
                "linkage": linkage_store,
                "history": record_history_store,
                "cohort": cohort_registry,
                "records": record_catalog,
                "results": result_catalog,
                "trust": result_trust_registry,
                "sources": source_registry,
                "d03": decision_registry,
                "d07": comparison_registry,
                "d09": d09_registry,
                "d10": d10_registry,
                "family": family_source_registry,
                "anchor": anchor_registry,
            }
        )
        self._binding = self._bindings_now()
        # Serializes holds of this coordinator; it is taken before any store
        # fence, so it sits ahead of the whole global order.
        self._hold_lock = threading.Lock()
        self._hold_owner: int | None = None

    def _bindings_now(self) -> tuple[object, ...]:
        """Cross-store wiring plus every adapter's bound identity."""

        stores = self._stores

        def attr(name: str, field: str) -> object:
            return _instance_state(stores[name]).get(field)

        linkage = stores["linkage"]
        cohort = stores["cohort"]
        records = stores["records"]
        results = stores["results"]
        trust = stores["trust"]
        wiring = (
            attr("history", "linkage_store") is linkage,
            attr("cohort", "_linkage_store") is linkage,
            attr("records", "_linkage_store") is linkage,
            attr("records", "_cohort_registry") is cohort,
            attr("records", "_result_catalog") is results,
            attr("records", "_result_trust_registry") is trust,
            attr("results", "result_trust_registry") is trust,
            attr("sources", "_record_catalog") is records,
            attr("d03", "_linkage_store") is linkage,
            attr("d07", "_linkage_store") is linkage,
            attr("d07", "_result_trust_registry") is trust,
            attr("d09", "_cohort_registry") is cohort,
            attr("d09", "_record_catalog") is records,
            attr("d10", "_d09_registry") is stores["d09"],
            attr("d10", "_decision_registry") is stores["d03"],
            attr("family", "_e06_registry") is stores["sources"],
            attr("anchor", "_linkage_store") is linkage,
            attr("anchor", "_cohort_registry") is cohort,
        )
        if not all(wiring):
            raise CompositeAuthorityUnsafe("dependency stores are not bound to each other")
        if self._sources.cohort_binding() != _registry_identity(cohort):
            raise CompositeAuthorityUnsafe("E06 is bound to another cohort registry")
        return tuple(adapter.bind() for adapter in self._adapters)

    def _require_no_store_lock_held(self) -> None:
        """Refuse entry while this thread already holds any store lock.

        The coordinator must be the first lock any thread takes: entering it
        under, say, E04's public ``trust_authority_fence`` would hold E04
        while waiting for D01, the reverse of D06's own order.  Every store
        fence takes one of these in-process locks first, so "this thread owns
        none of them" means "this thread holds no store fence".
        """

        stores = self._stores
        owned = (
            *(lock._is_owned() for lock in _MODULE_PROCESS_LOCKS),
            _instance_state(stores["linkage"]).get("_lock")._is_owned(),  # type: ignore[union-attr]
            _instance_state(stores["results"]).get("_connection_lock")._is_owned(),  # type: ignore[union-attr]
            bool(getattr(trust_module._LOCK_DEPTH, "value", 0)),
            # Any E04 content lock, through any catalog instance on any root:
            # held through another instance on this root it would wait for
            # this thread's own D01 successors.
            e04_module.content_lock_held_by_current_thread(),
        )
        if any(owned):
            raise CompositeAuthorityUnsafe(
                "composite authority must be entered before any store fence"
            )

    @contextmanager
    def _hold(self) -> Iterator[_CompositeHold]:
        """Acquire in order, capture, yield, revalidate in reverse, release.

        The body is the coordinator's own bounded reads, or the
        saved-comparison registry's publication through
        ``CompositeAuthorityFence``.  Revalidation runs after the body and
        before any release; a body exception releases without revalidating
        and propagates unchanged.
        """

        thread = threading.get_ident()
        if self._hold_owner == thread:
            # A nested hold would re-enter non-reentrant store fences.
            raise CompositeAuthorityUnsafe("composite authority hold is already active")
        self._require_no_store_lock_held()
        with self._hold_lock:
            self._hold_owner = thread
            body_failed = False
            try:
                if self._bindings_now() != self._binding:
                    raise CompositeAuthorityUnsafe("dependency store bindings changed")
                hold = _CompositeHold(self)
                with ExitStack() as stack:
                    for adapter in self._adapters:
                        _guard(lambda adapter=adapter: adapter.acquire(stack))
                    for adapter in self._adapters:
                        hold._base.update(_guard(adapter.capture))
                    try:
                        try:
                            yield hold
                        except BaseException:
                            body_failed = True
                            raise
                        hold.revalidate()
                    finally:
                        hold._active = False
            except CompositeAuthorityError:
                raise
            except Exception:
                if body_failed:
                    raise
                # A store fence failed on release; nothing is returned.
                raise CompositeAuthorityStale(
                    "dependency authority could not be released cleanly"
                ) from None
            finally:
                self._hold_owner = None

    def snapshot(self, scope: SavedComparisonDependencyScopeV1) -> CompositeAuthoritySnapshotV1:
        """Return every dependency head for one cohort version under one hold.

        The scope is captured as exact bytes before any lock.  While held:
        capture, bounded reads, reverse revalidation, construction; release
        happens only after the immutable result exists.
        """

        captured = _capture_scope(scope)
        with self._hold() as hold:
            heads = hold.heads(captured)
            bindings = hold.bindings()
            hold.revalidate()
            result = _SNAPSHOT(
                lock_order=GLOBAL_LOCK_ORDER,
                scope=captured,
                heads=heads,
                bindings=bindings,
            )
        return result


# --- saved-comparison fence -------------------------------------------------------


class _CompositeHeld(HeldSavedComparisonDependencies):
    def __init__(self, hold: _CompositeHold) -> None:
        self._hold = hold

    @property
    def fence_kind(self) -> DependencyFenceKind:
        return DependencyFenceKind.COMPOSITE_AUTHORITY_FENCE

    def read_heads(
        self, scope: SavedComparisonDependencyScopeV1
    ) -> SavedComparisonDependencyHeadsV1:
        captured = _translate(lambda: _capture_scope(scope))
        return _translate(lambda: self._hold.heads(captured))

    def read_bindings(self) -> SavedComparisonRegistryBindingsV1:
        return _translate(self._hold.bindings)


def _translate(operation: Callable[[], object]) -> object:
    try:
        return operation()
    except CompositeAuthorityUnsafe:
        raise LongitudinalComparisonRegistryUnsafe(
            "composite dependency fence integrity failed"
        ) from None
    except CompositeAuthorityError:
        raise LongitudinalComparisonRegistryStale(
            "dependency authority changed under the composite fence"
        ) from None


class CompositeAuthorityFence(SavedComparisonDependencyFence):
    """``SavedComparisonDependencyFence`` backed by the composite coordinator.

    ``hold()`` acquires every dependency fence in the global order before the
    saved-comparison registry takes its own lock (it is last in the order),
    serves ``read_heads``/``read_bindings`` from heads captured under the
    held fences, and on exit revalidates every head in reverse order before
    releasing anything.  A publication through this fence records
    ``composite_authority_fence``.
    """

    def __init__(self, coordinator: CompositeAuthorityCoordinator) -> None:
        if type(coordinator) is not CompositeAuthorityCoordinator:
            raise TypeError("a CompositeAuthorityCoordinator is required")
        self._coordinator = coordinator

    @contextmanager
    def hold(self) -> Iterator[HeldSavedComparisonDependencies]:
        try:
            with self._coordinator._hold() as hold:
                yield _CompositeHeld(hold)
        except CompositeAuthorityUnsafe:
            raise LongitudinalComparisonRegistryUnsafe(
                "composite dependency fence integrity failed"
            ) from None
        except CompositeAuthorityError:
            raise LongitudinalComparisonRegistryStale(
                "dependency authority changed under the composite fence"
            ) from None


__all__ = [
    "GLOBAL_LOCK_ORDER",
    "CompositeAuthorityCoordinator",
    "CompositeAuthorityError",
    "CompositeAuthorityFence",
    "CompositeAuthorityRetry",
    "CompositeAuthoritySnapshotV1",
    "CompositeAuthorityStale",
    "CompositeAuthorityUnsafe",
    "CompositeLockStep",
]
