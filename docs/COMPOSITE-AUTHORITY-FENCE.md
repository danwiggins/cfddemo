# Composite authority fence

Status: local, synthetic, release-disabled prerequisite for E12 Save and the
future D08 workspace builder (`docs/E12-INTEGRATION-PLAN.md`, "Composable
authority-fence prerequisite"). It builds no workspace and does not enable
Save by itself. It authorizes no release, export, provider operation or
clinical interpretation.

Code: `evidence_inspector/composite_authority_fence.py`. Tests:
`tests/test_composite_authority_fence.py`.

## Threat model

The process/OS-user boundary is the trust boundary, as for every merged E12
store. In-process code mutation and same-user filesystem races are out of
scope. Pins and binding checks detect accidental or naive class and instance
replacement only. In scope: caller-supplied inputs, concurrent readers and
writers across threads and processes, stores wired to the wrong peers, and
authority that moves while an operation runs.

## Global lock order

Every multi-lock operation of the merged stores, and the composite path
itself, takes E12 store locks in this order (the saved-comparison registry
takes its own lock last, inside `CompositeAuthorityFence.hold()`):

reader_authorization -> d10_context -> d09_summary -> d01_linkage -> d04_history -> d05_cohort -> e04_catalog_trust -> d06_record_catalog -> e06_source -> d03_decision -> d07_comparison -> family_source -> anchor_policy -> projection_policy

| Step | Store | What is held | Head read inside the fence |
| --- | --- | --- | --- |
| `reader_authorization` | `ReaderAuthorizationRegistry` | public `authority_read_fence` (shared flock) | pinned `_load_state` head |
| `d10_context` | `CovariateContextRegistry` | pinned `_lock(shared)` | pinned `_load_state` head |
| `d09_summary` | `DenominatorPolicyRegistry` | pinned `_lock(shared)` | pinned `_load_state` head |
| `d01_linkage` | `ProviderLinkageStore` | public `authority_read_fence` (store RLock + SQLite `BEGIN IMMEDIATE`) | `active_snapshot` (nests as a SAVEPOINT) |
| `d04_history` | `RecordSupersessionStore` | nothing of its own: fenced by D01 | `active_snapshot` (nests in the D01 fence). During capture and revalidation it takes D04's module lock (`_SQLITE_OPEN_LOCK`) and a D04 SQLite `BEGIN IMMEDIATE` inside the held D01 fence, and may advance D04 (refreshing invalidations); every D04 writer needs D01 first, so this cannot deadlock and a first-read advance is stable for the rest of the hold. |
| `d05_cohort` | `CohortRegistry` | **new** public `authority_read_fence` (shared lock) | **new** `head_in_fence` |
| `e04_catalog_trust` | `ResultCatalog` + `ResultTrustRegistry` | public `trust_authority_fence` (**catalog-content lock shared**: in-process gate, catalog connection lock, `flock`; then trust read fence) | `authority_snapshot` + **new** `content_head_in_fence` (E04 head = `catalog_dependency_head_sha256`, saved-head v2); trust journal via pinned `_snapshot_locked` |
| `d06_record_catalog` | `CohortRecordCatalog` | **new** public `record_status_read_fence` (root shared flock) | **new** `record_status_in_fence(scope)` |
| `e06_source` | `ResultViewSourceRegistry` | pinned `_lock(shared)` | pinned `_load_state` head; identity from **new** `registry_identity` |
| `d03_decision` | `LongitudinalDecisionRegistry` | pinned `_lock(shared)` | pinned `_load_state` head |
| `d07_comparison` | `RepeatabilityComparisonRegistry` | pinned `_lock(shared)` | pinned `_load_state` head |
| `family_source` | `MeasurementSourceArtifactRegistry` | pinned `_lock(shared)` | pinned `_load_state` head |
| `anchor_policy` | `AnchorPolicyRegistry` | pinned `_lock(shared)` | pinned `_load_state` head |
| `projection_policy` | `ProjectionPolicyRegistry` | pinned `_lock(shared)` | pinned `_load_state` head |
| (last) | `LongitudinalComparisonRegistry` | its own lock, inside `hold()` | — |

Every mutation of each store needs that store's exclusive lock (or, for D01
and D04, the D01 SQLite write lock; for E04 catalog rows, the E04
catalog-content lock), so no writer in any process can land while the
composite hold is active. See "E04 catalog content" below.

### Edges the order is derived from

The order is a linear extension of the acquisition edges the merged stores
actually take (A -> B: B is acquired while A is held):

- D10 registry -> D09 resolve (D09 lock -> D01 ...) and D10 -> D03 resolve
  (D01 -> D03), via the live D10 build (#64).
- D09 registry lock -> D09 summary builder -> D06 status fence (D01 -> D05
  -> E04 connection -> E04 content -> trust -> D06 root) (#59).
- D06 status fence: D01 -> D05 -> E04 content (shared, with the connection
  lock) -> result trust (registry read fence) -> D06 root.
- E04: catalog-content lock (in-process per-inode gate, then the catalog
  connection lock, then the cross-process `flock`) -> trust read fence
  (`trust_authority_fence`; every E04 row writer takes the content lock
  exclusively in the same position, then `_SQLITE_OPEN_LOCK` on a first
  connect). Nothing waits for the content lock while holding a catalog
  connection lock.
- D06 import: D01 -> D05 -> E04 content (exclusive, with the E04 connection
  lock) -> trust -> D06 root (exclusive) -> nested E04 writes, which reuse
  the held content lock. D06 open-time recovery: E04 content (exclusive) ->
  D06 root. Neither takes E04 content after the D06 root.
- E06 and family-source: D06 status fence -> own lock; family-source never
  holds its lock while resolving E06 (#62, family-source registry).
- D07: D01 -> trust read fence -> D07 lock (#60, #68).
- D03: D01 -> D03 lock (#56).
- Anchor: D01 -> D05 -> anchor lock (#66).
- D04: every read and write takes D01 first, then the D04 SQLite lock.
- Reader authorization, result trust (alone) and projection policy take no
  other store's lock.

No merged store takes a lock that precedes its own in this order while
holding its own, and the composite path acquires in exactly this order (it
refuses entry when its thread already holds any store lock), so every
multi-lock operation of the merged stores and the composite path is
consistent with it and the order is acquirable.

### Forbidden edge: saved-comparison registry -> any dependency store

The saved-comparison registry reads dependency heads *inside* its own lock.
A fence that served those reads through the stores' public reads (the
retired `LiveRegistryDependencyFence`, kind `direct_head_reread`) took
every other store's lock after the saved registry's, the reverse of this
order, and could permanently deadlock a composite hold in-process. That
fence is deleted and the registry refuses the `direct_head_reread` kind on
every operation. `CompositeAuthorityFence.read_heads` takes no lock.

### Deviations from the plan's order

The plan's order was `D01 -> D04 -> D05 -> reader -> D06 -> E04 -> E06 -> D03
-> D07 -> D09 -> D10 -> family -> anchor -> projection -> saved`.

| Deviation | Reason |
| --- | --- |
| Reader authorization moved to first | It takes no other store's lock, and the plan's finding places it first; nothing holds another E12 lock while authorizing. |
| D10 and D09 moved ahead of D01 | The live D10 build and the D09 summary builder take D01/D05/D06 (and D03) while their registry locks are held and cannot run inside a held D01 or D06 fence. Taking D01 first would invert a merged edge and deadlock against D09/D10 reads. |
| D04 has no lock step of its own | Every D04 operation acquires D01 first, so the held D01 fence already excludes every D04 writer. Its snapshot nests inside the D01 fence. |
| E04 before D06, and the result-trust registry added between them | D06's own fence takes E04's connection lock and the trust read fence before the D06 root; the plan listed D06 before E04 and had no trust slot. |
| Trust is read under E04's fence, not separately between D01 and D07 | `trust_authority_fence` is the only composable way to hold E04 and trust together (the trust lock is not reentrant); it still sits between D01 and D07 as the D07 edge requires. |
| D09 and D10 no longer sit between D07 and family-source | Follows from moving them first. |

## Adapters

One adapter per step (`_ReaderAdapter`, `_RegistryLockAdapter`,
`_LinkageAdapter`, `_HistoryAdapter`, `_CohortAdapter`,
`_CatalogTrustAdapter`, `_RecordCatalogAdapter`, `_SourceAdapter`). Each has:

- `acquire(stack)`: enters the store's fence through a function captured at
  import; the coordinator releases the stack in reverse.
- `capture()`: reads the store's ID, epoch and head assuming the fence is
  held. No capture re-enters a non-reentrant fence: inside the hold only the
  stores' already-fenced reads run (D01/D04 snapshots nest as SAVEPOINTs on
  the held D01 transaction; E04 reuses the held trust snapshot; registry
  heads come from `_load_state` under the held lock).
- binding: exact store type, instance and registry ID/epoch at construction,
  re-checked at every hold and capture.

Before every acquire and capture an adapter checks that the class still
carries exactly the pinned functions, that the instance does not shadow them,
and runs the store module's own seal check (`_require_registry_integrity` /
`_assert_cohort_runtime`).

Lock-only registries (D03, D07, D09, D10, E06, family-source, anchor,
projection) and the reader/trust head reads expose no public in-fence read,
so their private `_lock` / `_load_state` / `_snapshot_locked` are pinned here,
as the merged stores already pin each other's (D06 and the anchor registry
pin D05; D07 reads D01's fence holder). No store's fence semantics changed.

## Public in-fence reads added

- `CohortRegistry.authority_read_fence()`: holds the D05 shared lock for a
  composing caller. Requires this thread to hold the bound D01
  `authority_read_fence`; records the holder (pid, thread); not reentrant.
- `CohortRegistry.resolve_history_in_fence(selector, version)` and
  `head_in_fence()`: the reviewed public form of the private
  `_resolve_history_in_fence`, plus the registry head; both require the read
  fence. The anchor registry now uses this public pair instead of D05's
  private `_lock` and `_resolve_history_in_fence`.
- `CohortRecordCatalog.record_status_read_fence()`: holds the D06 record root
  shared after checking that this thread holds D01, the D05 read fence, and
  this catalog's E04 `trust_authority_fence` on the result-trust-registry
  path. The TrustStore path has no cross-process trust fence and is refused.
- `CohortRecordCatalog.record_status_in_fence(selector, version)`: the same
  derivation and final registry/linkage recheck as
  `record_status_for_manifest`, reusing the held fences, so several cohort
  versions can be read in one hold.
- `ResultViewSourceRegistry.registry_identity()`: lock-free immutable
  registry ID/epoch and bound cohort registry, replacing reads of E06's
  private `_metadata`.

The existing public `record_status_authority_fence`,
`record_status_for_manifest`, `resolve_history` and every other merged read
are unchanged.

## Coordinator protocol

`CompositeAuthorityCoordinator(**stores)` requires the 15 exact store types
and checks they are wired to each other (one D01 store, one D05 registry, one
D06 catalog over one E04 catalog and one result-trust registry shared with
D07, E06 over that D06, family-source over that E06, D10 over that D09 and
D03, and so on).

`snapshot(scope) -> CompositeAuthoritySnapshotV1`:

1. Capture `scope` as exact bounded canonical bytes and re-parse it, before
   any lock. Only the exact `SavedComparisonDependencyScopeV1` type is
   accepted; there is no callback parameter.
2. Acquire every adapter in `GLOBAL_LOCK_ORDER`.
3. Capture every store's ID, epoch and head in order, then the scoped D06
   status head.
4. While held, run only bounded canonical reads (the head vector and
   bindings).
5. Revalidate every head in reverse order; any difference raises
   `CompositeAuthorityRetry` and nothing is returned.
6. Construct the immutable `CompositeAuthoritySnapshotV1` (fence kind, lock
   order, scope, head vector, bindings).
7. Revalidate again on hold exit, then release in reverse order.

Entry rule: the coordinator must be the first lock its thread takes. Entry
is refused with `CompositeAuthorityUnsafe` before any acquisition if this
thread already owns any store's in-process lock (every store module's
process lock, the D01 store lock, the E04 connection lock, or the
result-trust lock depth). Entering under, for example, E04's public
`trust_authority_fence` would hold E04 while waiting for D01, the reverse of
D06's own order.

Error typing: a head that differs on revalidation raises
`CompositeAuthorityRetry`; a store integrity failure (every merged store's
`...Unsafe` class, D06/E04 filesystem errors, E04 unsupported schema) raises
`CompositeAuthorityUnsafe`; any other store failure inside the fence (not
current, conflict, busy) raises `CompositeAuthorityStale`; a type, pin,
binding or hold misuse raises `CompositeAuthorityUnsafe`. A hold is single-thread: another thread's use of
it, use after exit, and a nested hold on the same coordinator fail closed.
Holds of one coordinator from different threads serialize.

The snapshot is a point-in-time record, not a lease.

## `CompositeAuthorityFence`

Implements `SavedComparisonDependencyFence` with `fence_kind =
composite_authority_fence`. `hold()` runs the same acquire/capture as above,
yields a held object whose `read_heads(scope)` and `read_bindings()` serve
the heads captured under the held fences (D06 status is captured on first use
of each scope), and on exit revalidates every head, including each scoped D06
head, in reverse order before releasing anything. The saved-comparison
registry takes its own lock inside the hold, builds its receipt, and the
receipt is returned only if that final revalidation passes. Composite errors
surface as `LongitudinalComparisonRegistryStale` / `...Unsafe`.

`hold()` is the seam for the saved-comparison registry only. The D08 builder
must use `snapshot`-style construction (no caller code inside the fence).

## Tests

`tests/test_composite_authority_fence.py` builds one coherent world over the
real merged stores (E04 on a `ResultTrustRegistry`) and covers: the exact
order (and that this document states it); heads equal each store's public
read; acquire order, reverse revalidation, construction before the first
release, and release in reverse; no callback and inputs captured before any
lock; a head moving under the hold for several steps (and the scoped D06
head) raising a typed retry and releasing everything; in-process writers
(trust revoke, D01 commit, projection registration) and a separate-process
trust writer blocking until release; saved-comparison publication and reopen
under the fence, including a writer started during publication landing only
after the receipt; 17 opposing store operations (composite snapshot, save,
D06 status, live D09 and D10 builds, E06, family, D07, D03, anchor and D05
pages, D04 history, E04 authority, reader identity, trust add, D01 commit,
projection register) running concurrently to termination; the new public
in-fence reads refusing without their fences; public store reads failing
inside the hold without breaking it; entry under any store fence refused
(including the E04-then-D01 inversion with a concurrent D06 read); a store
integrity failure typed unsafe and fully released; and type, wiring, class
and instance shadow failures. E04 content: a separate-process E04 import and
a separate-process publication recovery each block until release (heads
unchanged during the hold, E04 head advanced after); an E04 row changed
between capture and revalidation raises `CompositeAuthorityRetry`; the D06
step refuses a trust fence held without the content lock; a shared content
hold is never upgraded; and the opposing-operations test adds an E04 import
through a second catalog instance and E04 content reads.

## E04 catalog content

E04 rows are fenced across processes (`docs/RESULT-CATALOG.md`,
"Catalog-content lock and content head"):

- A cross-process content lock (`flock` on `catalog-content.lock` in the
  catalog root) is taken **exclusively** by every E04 row writer: import,
  preparation, staging, adoption, finish, compensation, discard, coordinated
  candidate register/finish, publication recovery and schema initialization.
- The composite E04 step holds it **shared** through
  `trust_authority_fence` (connection lock -> content lock -> trust), as do
  D06 status and binding reads. D06 imports and D06 open-time recovery take
  it exclusively before the D06 root.
- The E04 head is `catalog_dependency_head_sha256(authority, content)`:
  catalog authority (storage, trust, reader registry) plus a digest of every
  committed catalog row, read under the held lock and revalidated in reverse
  order like every other head. Its change needed saved-head schema v2
  (`traceback.saved-comparison-dependency-heads.v2`); v1 vectors still read
  and reopen stale at `e04_catalog`.

So a second process importing into, staging, finishing, compensating or
recovering the E04 catalog blocks until the hold releases, and any committed
row change between holds changes the captured E04 head.

## D08 rule for E04 rows

D08 still consumes E04 rows through D06 status (rows bound to the cohort and
re-verified inside the held D06 root fence) and never runs caller code inside
the hold. Unbound E04 rows may be surfaced only when the read that produced
them is bound to an E04 head captured by a composite snapshot (an E04 row
read outside the hold is authority only if the E04 head is unchanged when it
is checked again); Save may claim E04 content is fenced only with a v2 head
vector.

## Open items

- Lock-only registries still expose no public in-fence read; the adapters
  pin their private `_lock` / `_load_state`. Promote a uniform
  `authority_read_fence` / `head_in_fence` pair if reviewers prefer.
- D06's existing fence still uses D05's private `_lock` and
  `_resolve_history_in_fence`; only the anchor registry was moved to the new
  public D05 API.
- The `family_source` saved-head slot prefix (`familysrc_registry_`) is not
  yet pinned in `_SLOT_ID_PREFIXES`.
- Whole-store heads are conservative: any advance of a captured dependency
  head marks every saved comparison stale (unchanged from the
  saved-comparison registry). In v2 any E04 catalog-row change marks every
  save stale; the E04 content digest is linear in catalog size.
- The D08 workspace builder is not built here.
