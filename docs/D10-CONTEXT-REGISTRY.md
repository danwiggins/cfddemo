# Protected D10 context registry

Status: local synthetic D10 persistence contract. It does not authorize real
provider operation, clinical use, scientific claims, export, or Epic D release.

`CovariateContextRegistry` (`evidence_inspector/covariate_context_registry.py`)
is the protected D10 store that E12 requires as `d10_context_registry` (see
`docs/E12-INTEGRATION-PLAN.md`). It stores the inputs of one live D10 context
under an opaque registry-scoped selector. Every read rebuilds the context from
the live D09 policy registry and the live D03 decision registry. Callers never
supply a context.

## Why a registry and not only derive-on-demand

The live builder (`build_live_covariate_context`, see
`docs/COVARIATE-CONTEXT.md`) already derives the population and decisions from
authority. The registry adds three things the E12 plan needs:

- the batch, protocol, and preanalytical tokens are the only caller-owned D10
  input. The E12 builder accepts no caller-created contract, so those tokens
  need a protected store;
- the saved-comparison registry persists a D10 commitment and replays it on
  reopen, which needs a stable selector; and
- the plan's fence order names a D10 context read fence. This registry's lock
  is that fence.

## Registration derives the context

`register_context(covariates, *, d09_selector_id, d09_policy_version,
d03_series_selector_id, expected_d02_anchor_policy_sha256)` accepts the opaque
covariate tokens and the D09/D03 selections. Under the exclusive D10 lock it:

1. captures the covariates as exact bounded `LiveCovariateMemberValues`,
   sorted by member digest, and one canonical
   `RegisteredCovariateContextObject` that also binds the D09 and D03 registry
   IDs and epochs;
2. derives one live context through the pinned builder. If the inputs do not
   cover exactly the live D09 included set, the D03 series does not cover it,
   the pin is wrong, or the two reads saw different linkage, registration is
   rejected and nothing is written; and
3. publishes the object, appends one hash-chained journal entry, reloads the
   committed state, and returns a receipt with the context digest.

The selector `d10_context_…` is derived from the registry epoch and the object
digest. Exact re-registration is idempotent. Different covariates or
selections produce a new selector.

## Every read rebuilds

`resolve(selector_id)` holds the shared D10 lock, loads the object, and calls
the pinned live builder with the registry's bound D09 and D03 registries. It
returns `RegisteredLiveCovariateContext`, which binds D10 registry identity,
state version and head, selector, object digest, context digest, and the
`LiveCovariateContext`, with `rebuilt_against_live_authority=true`. If D09 or
D03 is stale, the population changed (for example a revoked result key), or
linkage advanced, `resolve` raises `CovariateContextRegistryStale` and returns
no context. Integrity failures from D09 or D03 (`…Unsafe`) propagate unchanged
rather than being reported as stale.

`list_selectors(after_selector_id=None, limit=50)` returns a bounded page (1 to
100 rows) ordered by selector. A row carries the selector, object digest,
`current|stale` state and, for a current row only, the context digest,
classification, included-member count, and group count. Rows never contain
member or result digests, D09/D03 selectors, covariate tokens, timepoints, or
crosswalk rows.

## Fence composition (measured)

Probes in `tests/test_covariate_context_live.py` and
`tests/test_covariate_context_live_registry.py`:

| Call | Made while holding | Result |
| --- | --- | --- |
| live build, registry `resolve` | nothing | succeeds |
| live build, registry `resolve` | `ProviderLinkageStore.authority_read_fence` | fails (D09 `…Stale` / D10 `…Stale`) |
| live build | `CohortRecordCatalog.record_status_authority_fence` | fails (D09 `…Stale`) |
| live build | the D09 registry's shared lock | succeeds |
| live build | the D03 registry's shared lock | succeeds |

The live build reads D09 first (D09 lock, then D01, D05, D06, result trust) and
then D03 (D01, then the D03 lock). Neither read can nest inside a held D01 or
D06 fence, and neither nests inside the other, so they run sequentially. The
equality of the D09 summary's and D03 decision's linkage-snapshot digests
proves both reads observed one D01 state. A concurrent linkage writer makes the
build fail; it never yields a mixed snapshot.

The D10 lock is therefore the outermost lock, ahead of D09 and D03. No D01,
D05, D06, D09, or D03 code takes the D10 lock, so the order is acyclic. The E12
plan lists D10 after D09 in its global fence order. As with D09, the
composable authority-fence adapter must reconcile this. This registry does not
change the plan.

The returned context is as-of one snapshot. A consumer that needs D01, D05,
D06, D09, and D03 to stay unchanged through a later step needs the composite
fence adapter.

## Storage and threat model

Storage, recovery, bounds, backup, and restore follow the D03 decision registry
(`docs/LONGITUDINAL-DECISION-REGISTRY.md`) and the D09 policy registry: private
`0700` root, `0600` files, descriptor-relative exclusive publication, a
genesis-anchored journal chain as the commit point, retained ID/epoch/head on
reopen, a process-wide monotonic head fence, and at most one uncommitted
object remnant. The metadata binds the D09 and D03 registry IDs and epochs.
Construction, reopen, and restore require exact `DenominatorPolicyRegistry` and
`LongitudinalDecisionRegistry` instances whose identities match and which read
one shared linkage store.
Crash recovery follows the shared storage behaviour in
`docs/REGISTRY-STORAGE.md`. Creation and restore are staged in a hidden
sibling and published with one rename. A torn journal tail fails closed on
reopen until an operator runs `recover_torn_journal_tail` with the retained
identity and head. Owned `.tmp-<32 hex>` names are swept under the
exclusive lock (the D05 rule). A failed append truncates on any exception.

One object holds at most 4 MiB and 1,000 members. The registry holds at most
10,000 objects and 256 MiB of committed object bytes. A backup contains
protected covariate tokens and member digests. It is not an export artifact.

The process boundary is the trust boundary, as in the D03, D05, and D09
registries. Method, instance, pinned-authority, and alias seals detect naive
replacement. In-process code mutation and same-user filesystem races are out
of scope. There is deliberately no whole-module-namespace seal.

## Not in scope

The composable E12 authority-fence adapter and the E12 builder are separate
prerequisites. Covariate tokens are registered by the caller. No upstream
authority for batch, protocol, or preanalytical metadata exists yet. All
fixtures are synthetic/local.
