# Durable saved-comparison registry

Status: local, synthetic, release-disabled prerequisite for the E12 Save and
Reopen journey (`docs/E12-INTEGRATION-PLAN.md`, "Durable saved-comparison
registry prerequisite", journey steps 5–6, test items 17 and 21). It does not
authorize release, export, provider operation or clinical interpretation.
Saving never authorizes export.

`LongitudinalComparisonRegistry`
(`evidence_inspector/longitudinal_comparison_registry.py`) stores immutable
saved comparisons. D08 must keep Save disabled until this registry is
installed and healthy **and** publications carry a
`composite_authority_fence` (see "Dependency-fence seam").

## Threat model

The process and user boundary is the trust boundary, as for the D03, D05,
D07 and result-trust registries. In-process code mutation and same-user
filesystem races are out of scope. Seals detect accidental or naive
replacement of public methods (instance and class), sealed module aliases and
result constructors, and instance authority state. There is no whole-module
namespace seal (D05 parity). In scope: caller-supplied objects and fences,
interrupted writes and process crashes, concurrent readers and writers across
processes, tampered, truncated, substituted, linked or extra files, rollback
to an older journal or backup, and stale dependency authority.

## Contracts

- `LongitudinalComparisonRegistryMetadataV1`: registry ID, random epoch,
  creation-time storage identity, saved-object schema version, bound
  cohort-registry ID/epoch, D04 ledger ID/epoch, reader-authorization
  registry ID/epoch, D06 catalog authority (the E04 storage identity the D06
  catalog binds), E06 source-registry ID/epoch, and a creation digest over all
  of them.
- `SavedLongitudinalComparisonV1`: the canonical selection
  (`SavedComparisonSelectionV1`: cohort, anchor-policy, approved-anchor,
  projection-policy and D09-policy selectors/versions, requested D02
  measurement, normalized filters), comparison version, the exact family
  source-value projection request (`SavedFamilyProjectionRequestV1`: family,
  selection rule, family-checked statistic and unit, projection-policy
  selector/version/digest, component count; the digest pins the immutable
  registered policy and so every family coordinate), commitments
  (`SavedComparisonCommitmentsV1`: manifest, D03 policy, D07 envelope,
  approved anchor, D03, D07, D09, D10, D04, source and reader-grant
  digests), exact E06 source-registry
  ID/epoch/state head and source selectors/versions, the full dependency-head
  vector it was built against, workspace replay digest, whole-second UTC
  creation time, literal `local_only`, `synthetic_only` and disabled
  release/export/interpretation fields, and a content digest.
- `SavedComparisonJournalEntryV1`: sequence, predecessor head, opaque
  selector, comparison version, object digest and size, the exact
  dependency-head vector, the fence kind, and the entry digest.
- `SavedComparisonRegistrationReceiptV1`: registry ID/epoch, state
  version/head, selector, version, object digest, dependency-head vector,
  fence kind, `applied`, and `saving_authorizes_export=false`.
- `SavedComparisonSelectorPageV1` / `SavedComparisonSelectorRecordV1` and
  `RegisteredSavedComparisonV1`: bounded read results (below).
- `SavedComparisonRecoveryRecordV1`: the durable publication intent.

The selector is `saved_comparison_` + 40 hex of a digest over the registry
epoch and the canonical selection. It is registry-scoped and opaque: selector
pages and errors carry no cohort, provider, subject, collection, analysis or
record identity.

`build_saved_longitudinal_comparison(**fields)` derives the content digest.

### Dependency-head vector

`SavedComparisonDependencyHeadsV1` is closed: one `DependencyHeadV1(id,
epoch, head)` per slot, in the plan's lock order. Each slot's ID must carry
its store's own prefix.

| Slot | Source (merged store) | ID / epoch / head |
| --- | --- | --- |
| `d01_linkage` | `ProviderLinkageStore.active_snapshot()` | store ID, store epoch, state head |
| `d04_history` | `RecordSupersessionStore.active_snapshot()` | ledger ID, ledger epoch, state head |
| `d05_cohort` | `CohortRegistry.list_selectors(limit=1)` | registry ID, epoch, state head |
| `reader_authorization` | `ReaderAuthorizationRegistry.identity()` (#67) | registry ID, epoch, state head |
| `d06_record_catalog` | `CohortRecordCatalog.record_status_for_manifest(scope)` | D05 registry ID/epoch, cohort status digest |
| `e04_catalog` | `ResultCatalog.authority_snapshot()` | `e04_catalog_`+storage prefix, storage identity, `catalog_authority_sha256` |
| `result_trust` | `ResultTrustRegistry.current_trust()` (#68) | registry ID, epoch, state head |
| `e06_source` | `ResultViewSourceRegistry.list_selectors(scope, limit=1)` (#62) | registry ID, epoch, state head |
| `d03_decision` | `LongitudinalDecisionRegistry.list_selectors(limit=1)` (#56) | registry ID, epoch, state head |
| `d07_comparison` | `RepeatabilityComparisonRegistry.list_selectors(limit=1)` (#60) | registry ID, epoch, state head |
| `d09_summary` | `DenominatorPolicyRegistry.list_selectors(limit=1)` (#59) | registry ID, epoch, state head |
| `d10_context` | `CovariateContextRegistry.list_selectors(limit=1)` (#64) | registry ID, epoch, state head |
| `family_source` | not merged | **optional slot, `None` in v1** |
| `anchor_policy` | `AnchorPolicyRegistry.list_selectors(limit=1)` (#66) | registry ID, epoch, state head |
| `projection_policy` | `ProjectionPolicyRegistry.list_selectors(limit=1)` (#65) | registry ID, epoch, state head |

D06 and E04 expose no registry ID/epoch/head of their own; the table shows
what stands in for them. D06 status and the E06 page are scoped to one cohort
version (`SavedComparisonDependencyScopeV1`), taken from the saved selection.

The family-source registry is being built in parallel. Its slot is optional
in schema v1 and accepts any `<prefix>_<32 hex>` ID. When that registry
merges, the live fence fills the slot (and its prefix is pinned); every
vector saved before then compares unequal, so those saves reopen `stale`.
No schema bump is needed.

Staleness is conservative: slots are whole-store heads, so any advance of a
dependency store (for example, a grant issued to another reader, or another
cohort's D09 policy) marks every saved comparison `stale`. That never shows a
stale comparison as current; current values come only from a fresh E12
replay in any case.

## Dependency-fence seam

Every operation that depends on other stores takes a caller-supplied
`SavedComparisonDependencyFence`. `hold()` yields a
`HeldSavedComparisonDependencies` with `fence_kind`, `read_heads(scope)` and
`read_bindings()`. The registry acquires the fence first and its own lock
second (the saved-comparison registry is last in the global order), calls
`read_heads` several times per operation, and re-validates every returned
value from canonical bytes; a fence that returns a wrong type fails with
`integrity_failure`, and one that raises fails with `authority_stale`.

- `LiveRegistryDependencyFence` is the interim implementation. It takes
  exact instances of the 14 merged stores and reads each head through that
  store's own public bounded read under that store's own lock. It holds **no
  cross-store lock**, so two reads can straddle an authority change; the
  registry's pre-commit and final rechecks then fail closed, but the window
  between the final recheck and the caller's use of the receipt is not
  closed. Its `fence_kind` is `direct_head_reread`. The startup binding of
  the E06 registry ID/epoch is read from E06's private `_metadata`, because
  E06 has no public unscoped identity read.
- The composite coordinator (not built here) will implement the same
  interface: `hold()` acquires every store's read fence in the global order,
  `read_heads` reads already-fenced snapshots, and `fence_kind` is
  `composite_authority_fence`. The registry does not change when it lands.

Every journal entry, receipt, page and reopen records the fence kind. D08
must refuse Save, and must not present a publication as fenced, unless the
kind is `composite_authority_fence`.

Because the live fence reads other stores while this registry's lock is
held, it acquires those stores' locks after this one. No store ever takes
this registry's lock, so this cannot deadlock; the coordinator removes the
inversion by acquiring everything first.

## Publication

`register(saved, dependency_fence=…)`:

1. Canonicalize and bound the input (exact type, closed graph, 512 KiB)
   before any fence or lock.
2. Hold the dependency fence; read the heads. They must equal the object's
   own vector (otherwise `authority_stale`), and their store identities must
   equal the metadata bindings (otherwise `invalid_request`).
3. Take the exclusive registry lock; recover any interrupted publication;
   load the committed index.
4. Exact retry (same object digest): re-read the heads; if they still equal
   the committed vector, return a receipt with `applied=false`; otherwise
   `authority_stale`. Nothing is written.
5. Same selector/version with other bytes: `invalid_request`. A new version
   must be exactly the next version for its selector.
6. Admission: object count (1,000), journal bytes (4 MiB) and the exact
   projected backup size (520 MiB, cumulative bytes) are checked before any
   write.
7. Write the recovery record, then the content-addressed object (private
   temporary file, fsync, no-follow `link` that never overwrites, directory
   fsync).
8. Re-read the heads (pre-commit check); then append and fsync one journal
   entry — the commit point. A failed append truncates its own suffix on any
   exception.
9. Remove the recovery record, reload the complete state, verify the exact
   committed entry and bytes, and re-read the heads a final time. Only then
   return the receipt.

If the final recheck fails, the entry is already committed: no receipt is
returned (`authority_stale`), the object reopens `stale`, and an exact retry
is a conflict. Saved bytes are never rewritten.

## Crash recovery

The plan requires a durable candidate/recovery record written before object
publication. This differs from the sibling registries, which fail closed on
interrupted writes (listed as a shared follow-up in the plan); this registry
follows its own spec.

`publication-candidate.json` (≤ 64 KiB, owner-only, single link) holds the
base state version, head and journal byte length, and the exact intended
journal entry. Recovery runs under the exclusive lock at startup, at the
start of every publication, and before any read that observes pending state.
It never uses temporary files as evidence. It deletes a `.tmp-<32 hex>`
file only when it is exactly what an interrupted private write leaves (an
owner-only, single-link regular file within the object bound); a link,
directory, FIFO or other file under that name fails closed.

| State found | Action |
| --- | --- |
| Journal = base + exact intended entry | Committed: verify the object bytes, fsync the journal and objects, then delete the record |
| Journal = base, or base + a strict prefix of the entry | Uncommitted: truncate to base, delete the candidate's object only if its bytes hash to the candidate digest, delete the record |
| Anything else, or an invalid, linked, FIFO or foreign record | Fail closed (`integrity_failure`) |

A substituted candidate object is never deleted or adopted.

A publication can commit without returning a receipt: a crash after the
journal fsync, or a failed final dependency recheck. The operator then holds
the predecessor head. Startup accepts a retained head that is exactly one
committed entry behind the verified chain head (forward movement, never a
rollback); it does not depend on the recovery record, which may already be
consumed. Any older head is rejected.

## Reads

`resolve(selector, version, dependency_fence=…)` (alias `reopen`) holds the
fence and a shared registry lock through object read, digest and canonical
parse, the live head read, the final head re-read and result construction.
`RegisteredSavedComparisonV1` returns the immutable bytes, the parsed object,
the live vector, `current` or `stale`, and the exact stale slots. Its
validator makes a stale result impossible to construct as current.
`saved_digest_is_current_authority=false` and
`current_values_require_fresh_replay=true` are literals.

`list_selectors(dependency_fence=…, after_selector_id, after_version,
limit=1..100)` returns a privacy-safe page ordered by (selector, version),
with per-row `current`/`stale` and a final head re-read for every scope it
used. A head change mid-read raises `read_conflict`.

Every read verifies the full journal chain, the exact object set (no
missing, extra, linked or wrong-size files) and the forward-only head.

## Backup and restore

`backup_bytes()` captures, under one shared lock and after recovery, the
magic line, a canonical header (metadata, state version/head, journal, object
index) and the raw committed objects in journal order. It is bounded at
520 MiB.

`restore(root, content, dependency_fence=…, expected_registry_id,
expected_registry_epoch_sha256, expected_state_head_sha256)` verifies the
whole chain, every object digest, canonical form and journal binding, and
the backup's dependency-store bindings against the live fence before
publishing into a new private target (the target must not exist). The
restored root reopens through the normal constructor. A truncated or altered
backup, a wrong expected identity or head, other dependency stores, an
existing target, or a backup older than a head this process has seen is
rejected. Application rollback may stop new saves but never rewrites saved
bytes or blesses an older head.

## Startup

A new root mints a random registry ID and epoch and binds the live
dependency-store identities. An existing root requires the independently
retained registry ID, epoch and head (or the head one committed entry
behind it, see "Crash recovery"); missing metadata never bootstraps a
new identity, and changed dependency-store identities fail closed. The root
is `0700`; metadata, journal, lock, record and objects are `0600`,
single-link regular files opened through bound descriptors.

## Errors

| Exception | `code` |
| --- | --- |
| `LongitudinalComparisonRegistryConflict` | `invalid_request` |
| `LongitudinalComparisonRegistryStale` | `authority_stale` |
| `LongitudinalComparisonRegistryReadConflict` | `read_conflict` |
| `LongitudinalComparisonRegistryUnsafe` | `integrity_failure` |

Messages are fixed strings and never contain nested exception text or
protected identity.

## Open items

- The composite authority-fence coordinator and its adapters. Until it
  exists, publications are `direct_head_reread` and D08 Save stays disabled.
- The family-source registry slot (optional in v1; see above).
- A public unscoped identity read on the E06 source registry, to replace the
  private `_metadata` read in `LiveRegistryDependencyFence.read_bindings`.
- Root-creation crash recovery: like the siblings, an interrupted root
  creation (root without metadata) fails closed.
- Reads parse one object per selected row; full-state parsing happens only in
  backup.
