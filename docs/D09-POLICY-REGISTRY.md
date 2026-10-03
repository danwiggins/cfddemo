# Protected D09 policy/summary registry

Status: local synthetic D09 persistence contract. It does not authorize real
provider operation, clinical use, scientific claims, export, or Epic D release.

`DenominatorPolicyRegistry` (`evidence_inspector/denominator_policy_registry.py`)
is the protected D09 policy/summary store that E12 requires as
`d09_summary_registry` (see `docs/E12-INTEGRATION-PLAN.md`). It replaces
caller-supplied `CohortDenominatorPolicy` and `CohortDispositionPolicy` objects
with one registry-scoped selector and version. Every read rebuilds the summary
from live D05/D06 authority. Callers never supply a summary.

## Registration binds a policy pair to one D05 selection

`register_policy(cohort_selector_id, cohort_version, policy,
disposition_policy, *, expected_cohort_manifest_sha256)` accepts the D05
selection, the exact policy pair, and the manifest digest the caller expects.
It never accepts a summary. Under the exclusive D09 registry lock it:

1. captures both policies as exact bounded canonical bytes, rejecting hooks,
   private state, and non-exact types;
2. captures one canonical `RegisteredDenominatorPolicyObject`. Its validator
   requires the policy's inclusion, exclusion, and missingness digests to equal
   the disposition policy's rule sets. The object also binds the D05 registry
   ID/epoch, selector, version, and expected manifest digest;
3. derives one summary through the pinned
   `build_registered_cohort_denominator_summary`. The summary must bind the same
   D05 registry, selector, version, manifest digest, and policy digest. A pair
   that the D05 manifest does not bind, or a wrong expected digest, is rejected;
4. enforces the selector's version history: version `N` requires versions
   `1..N-1` and must not reuse a version number with other content; and
5. publishes the object, appends one hash-chained journal entry, reloads the
   committed state, and returns a receipt.

Re-registering the same bytes is idempotent.

The D09 selector `d09_policy_…` is derived from the registry epoch, D05
registry ID, D05 selector, D05 version, and `policy_id`. The policy version is
`CohortDenominatorPolicy.version`. A new policy version for the same cohort
selection extends the same selector. Each registered version stays
independently resolvable.

## Every read rebuilds

`resolve(selector_id, policy_version)` holds a shared D09 registry lock, loads
the committed object, and calls the pinned builder with the registry's bound
D05 `CohortRegistry` and D06 `CohortRecordCatalog`. The builder holds D06's
composite authority fence (linkage, D05 registry, D06 catalog/root, result
catalog connection, and result trust) through construction of its summary. The
result is `RegisteredDenominatorPolicySummary`, which binds D09 registry
identity, state version and head, selector, policy version, object digest,
policy digests, and the summary, with `rebuilt_against_live_authority=true`.
Its validator re-derives the selector from the summary's D05 identity and
policy ID, and checks the policy digest.

D06 changes, such as a newly imported result or a revoked result key, are live
data. They change the counts on the next read without changing D09 history. If
the builder fails (stale linkage, a correction, a tombstone, changed trust, or a
D05 selection that is no longer current), or if its output no longer matches the
stored binding, `resolve` raises `DenominatorPolicyRegistryStale` and returns
no summary.

### Fence composition (measured, not assumed)

A probe against the merged D05/D06 code showed:

| Builder call made while holding | Result |
| --- | --- |
| nothing | succeeds |
| `ProviderLinkageStore.authority_read_fence` | fails (`CohortImportError`) |
| `CohortRecordCatalog.record_status_authority_fence` | fails (`CohortImportError`) |
| `CohortRegistry._lock(exclusive=False)` | succeeds |
| an independent flock plus `threading.RLock` | succeeds |

A linkage writer blocks while the D06 fence is held and completes after release.
After a linkage correction, the builder fails.

The builder acquires the D06 fence itself, and that fence cannot nest inside a
held linkage fence. The D09 lock is therefore the outer lock: the order is D09,
then the D06 fence order (D01 linkage, D05, D06 catalog, result trust). No D05
or D06 code acquires the D09 lock, so the order is acyclic. Merged D05/D06 code
is unchanged.

Consequences:

- The D09 lock is held through construction of the returned value, so no D09
  registration lands between the rebuild and the return.
- The D05/D06 fence is released when the builder returns. The summary is an
  as-of summary for one linkage/catalog snapshot, which its digests identify.
  `resolve` cannot be called inside a held linkage or D06 fence: the builder
  tries to re-enter it and fails, and `resolve` raises
  `DenominatorPolicyRegistryStale`. A test pins this. A consumer that needs
  D05/D06 to stay unchanged through a later step needs the composite fence
  adapter or an in-fence builder entry point. This registry provides neither.
- The E12 plan lists D09 after D01/D05/D06 in its global fence order. This
  registry has to take its lock before them, because the builder cannot run
  inside them. The composable authority-fence adapter must reconcile this. For
  example, it could take the D09 lock first, or D06 could expose an in-fence
  builder entry point. Both change other layers and are left to that
  prerequisite. Resolved in #80: the global lock order in
  `docs/COMPOSITE-AUTHORITY-FENCE.md` takes D10 and D09 before D01, D05 and
  D06.

## Selector projection

`list_selectors(after_selector_id=None, after_policy_version=None, limit=50)`
returns a bounded page (1 to 100 rows) ordered by `(selector, version)`. Each
row carries:

- the object, policy, disposition-policy, and D05 manifest digests;
- `current|stale` authority state; and
- for a `current` row only, the live summary digest, summary state, and
  reconciled aggregate member and denominator-unit counts. A `stale` row carries
  no counts; the validator enforces this.

Rows never contain member commitments, result IDs, provider, subject,
collection, specimen, run, or analysis tokens, the D05 selector, the policy ID,
paths, or free text. Each row is rebuilt through its own D05/D06 fence while the
D09 shared lock is held, so rows in one page can reflect different D06
snapshots.

## Protected population read

`resolve_population(selector_id, policy_version)` is the protected read D10
uses (see `docs/COVARIATE-CONTEXT.md`). It takes the same shared D09 lock and
the same D05/D06 fence as `resolve`, and it applies the same checks. It calls
the pinned `build_registered_cohort_population_members`, which runs the
identical derivation and also returns the row-bearing population plus each
included row's exact D05 `CohortMember` and D06 `CatalogResultRef`, captured
inside that fence. The result, `RegisteredDenominatorPolicyPopulation`, holds:

- `summary`: the same `RegisteredDenominatorPolicySummary` that `resolve`
  returns for that snapshot; and
- `members`: `RegisteredCohortPopulationMembers`, whose validator requires its
  population digest and projection to equal the v3 summary's, and each included
  member's D05 and catalog digests to equal its row.

This read is protected and local only. Its member data never reaches the v3
summary, selector rows, or any aggregate projection. `resolve` and
`list_selectors` are unchanged. It cannot run inside a held linkage fence,
like `resolve`. A test pins this.

## Storage and bounds

Storage follows the D03 decision registry (`docs/LONGITUDINAL-DECISION-REGISTRY.md`),
which mirrors the D05 cohort registry:

- a private `0700` root with `0600` owner-only files;
- descriptor-relative exclusive publication with fsync and hard-link adoption;
- a journal chain that starts at a genesis digest over the immutable metadata;
- on reopen, the retained registry ID, epoch, and head are required;
- a process-wide monotonic head fence against rollback, and inode-bound control
  files.

The metadata binds the D05 registry ID and epoch, the D06 result-catalog
storage identity, and the D06 record-catalog scope digest. Reopening or
restoring against another D05 registry or D06 catalog fails closed. The
supplied catalog must be bound to the supplied D05 registry and its linkage
store.

The journal is the commit point. An object without a journal entry is the
remnant of an interrupted registration. Reads tolerate at most one. The next
registration removes it, unless it holds the exact bytes being registered, in
which case it is adopted. A failed journal append truncates any torn suffix. A
failed restore removes the partial target it created. That includes a target
whose final reopen fails. Policy history is validated in journal order: each
selector's versions must be committed as 1, 2, … N. A gap or a reordered
history fails closed on load and on restore.
Crash recovery follows the shared storage behaviour in
`docs/REGISTRY-STORAGE.md`. Creation and restore are staged in a hidden
sibling and published with one rename. A torn journal tail fails closed on
reopen until an operator runs `recover_torn_journal_tail` with the retained
identity and head. Owned `.tmp-<32 hex>` names are swept under the
exclusive lock (the D05 rule). A failed append truncates on any exception.

One object holds at most 32 MiB of canonical bytes. Each disposition rule set
holds at most 100,000 member commitments. The registry holds at most 10,000
policy objects and 256 MiB of committed object bytes, checked before
publication. Object and backup parsing enforce byte, depth, node, collection,
and string budgets before validation, and require an exact canonical round
trip.

`backup_bytes` and `restore` follow the D03 registry. Restore requires the
independently retained registry ID, epoch, and state head, the same D05 registry
and D06 catalog, and an empty target. A backup contains protected disposition
member commitments. It is not an export artifact.

## Threat model

The process boundary is the trust boundary, as in the D05 cohort registry and
D03 decision registry. Seals cover:

- the registry's methods (the method seal);
- the instance's authority state (the instance seal);
- the pinned authority callables: the D09 summary and population builders,
  summary and population bytes, policy and member-set digests, D05
  `list_selectors`, and the D05 integrity check; and
- the sealed result-constructor and helper aliases.

They detect accidental or naive replacement of those objects. In-process code
mutation is outside the threat model: monkeypatching module globals, the seal
tables, function code, the module dictionary, the standard library, or
pydantic. There is deliberately no whole-module-namespace seal. Python writes
`__warningregistry__` into module globals, and that seal bricked the D03
registry until it was reverted.

## Not in scope

The composable E12 authority-fence adapter and the E12 builder are separate
prerequisites in the E12 merge order. D10 consumes this registry through
`resolve_population` (`docs/COVARIATE-CONTEXT.md`,
`docs/D10-CONTEXT-REGISTRY.md`). All fixtures
are synthetic/local. A D09 `included` count never implies comparison
eligibility.
