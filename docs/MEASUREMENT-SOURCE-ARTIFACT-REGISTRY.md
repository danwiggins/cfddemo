# Protected E12 family-source artifact registry

Status: local synthetic persistence contract. It does not authorize real
provider operation, clinical use, scientific claims, export, or Epic D release.

`MeasurementSourceArtifactRegistry`
(`evidence_inspector/measurement_source_artifact_registry.py`) is the durable
family-source artifact discovery store that E12 calls
`measurement_source_artifact_registry` (see `docs/E12-INTEGRATION-PLAN.md`).
The E12 family adapters project standalone values from an E07, E08 or E09
artifact. This registry locates that artifact for one E06 source and proves,
on every read, that it still derives from live authority. It does not
implement the family adapters, and it never selects a coordinate or a value.

Only the E07 fragment family is built. E08 and E09 stop at a product decision;
see "Families that cannot bind today".

## Authority analysis

This section answers four questions from the code as it stands. It was written
before the registry and constrains what the registry claims.

### What builds each artifact

| Family | Builder | Inputs | Live authority | Caller-supplied |
| --- | --- | --- | --- | --- |
| E07 `FragmentExplorerView` | `build_fragment_explorer_view(FragmentExplorerRequest)` | two `VerifiedFragmentSource` (E05 record, quantity, state, E02 manifest, measurement, chart), `CompatibilityPolicy` and policy pin, authority-head pin, `FragmentExplorerState` (two selections, controls) | E02 manifest, measurement and chart: E04 `ResultCatalog.verify_reference` re-reads and re-verifies the signed bundle | records, policy, pins, selections, controls |
| E08 `CellOriginExplorerArtifact` | `build_cell_origin_explorer_artifact(CellOriginExplorerRequest)` | one E06 `ResultViewRequest` (exactly one `ResultViewSource` plus normalized filters) and an optional `CellOriginResultBundle` | none: E04 indexes only `traceback.result-bundle` v2 with `traceback.fragment-measurement.v1`, so no store holds a cell-origin bundle | the E06 request, filters and bundle |
| E09 `CnaExplorerSnapshot` | `build_cna_explorer_snapshot(dosage, segmented, dosage_authority, segmented_authority)` | a dosage-QC v2 result, an ichor development result, and two `ExplorerInputAuthority` (execution, trust, qualification, inspectable) | none | everything, including the authority states |

### Can the registry derive the artifact itself?

E07: yes. Given one E06 source, every E07 input is either live authority or
pinned by the E06 source:

- The subject record is the E06 record. E06 verified it against the live D06
  binding and E04 `CatalogResultRef` under the D06 fence.
- The counterpart record and the policy are not returned by E06 `resolve`, so
  the caller supplies them. They are accepted only if
  `replay_compatibility_decision` reproduces the E06 decision byte for byte.
  The decision binds both records' identities (result, digests, bundle ID,
  method, capability digest, compatibility-key digest), the policy digest and
  the trusted pins, so the caller has no freedom beyond the counterpart fields
  E06 already lists as caller-asserted.
- The two E02 bundles come from `verify_reference` on the two live
  `CatalogResultRef`s, inside the D06 fence.
- Selections, panel order and controls are fixed: subject in panel A, the E06
  counterpart in panel B, unlinked filters, each panel's window
  `[0, len(chart rows))`, threshold zero. Every bin is displayed, so the
  adapter can resolve a bin from both the chart and the table.
- Quantity and source state are not chosen: of all `FragmentQuantity` and
  `ExplorerSourceState` pairs, exactly one validates under E07 for the record
  and bundle, and the registry requires exactly one.

So `register_fragment_artifact` never accepts an artifact, view, request,
state, controls, decision or bundle.

E08 and E09: no; see below.

### What binds an artifact to one E04 result and one E06 source

- The selector derives from the registry epoch, the family, the E06 registry
  ID, and the E06 selector and version. One E06 source version has at most one
  artifact; a different derivation for the same slot is a conflict.
- The stored object carries the E06 object and source digests, the cohort
  commitments, both member and binding digests, and both `CatalogResultRef`
  digests.
- The E07 view embeds both E05 records, both E02 contents, the policy and
  pins, and its own compatibility decision. Its comparison semantics (outcome,
  mismatch keys, missing fields, delta and shared-axis permission, remediation)
  must equal the E06 decision's.

One identity difference is forced by the two existing contracts. E06 requires
the E05 `bundle_sha256` to equal the E04 bundle-tree digest
(`CatalogResultRef.bundle_sha256`). E07 requires it to equal the canonical E02
manifest digest. Both are E04-pinned: `CatalogResultRef.bundle_manifest_sha256`
is exactly that manifest digest, and `verify_reference` checks it. The registry
builds the E07 record from the E06 record with `bundle_sha256` replaced by
`bundle_manifest_sha256` and nothing else changed. It also requires the E06
record's `result_sha256`, which E06 cannot verify, to equal the canonical E04
measurement digest, because E07 requires that. An E06 source whose
`result_sha256` is anything else is `MeasurementSourceArtifactNotApplicable`.
The resolved contract states both facts as literals
(`e07_bundle_sha256_source="e04_bundle_manifest_sha256"`,
`e06_result_sha256_matches_e04_measurement=true`).

### Is the artifact deterministic and replayable?

E07: yes. `build_fragment_explorer_view` is pure; the view embeds its request,
and `replay_fragment_explorer_view` rebuilds it from that request. Two
registries deriving the same E06 source produce byte-identical artifacts (test
`test_artifact_derivation_is_deterministic_across_registries`).

E08 is pure and replays through its artifact validator; E09 replays through
`replay_cna_explorer_snapshot`. Determinism is not what blocks them.

## Families that cannot bind today

Building these needs a product decision, so the registry's family vocabulary is
closed at `fragment`.

- **E08 cell origin.** `CellOriginExplorerRequest` requires the E05 record's
  `bundle_sha256` to equal the digest of the `CellOriginResultBundle` and its
  `result_sha256` to equal the digest of the `CellOriginResult`. Any record
  the E06 registry admits has `bundle_sha256` equal to an E04 bundle-tree
  digest, and E04 indexes only fragment-measurement result bundles. No E06
  source can therefore yield a ready E08 artifact, and no durable store holds
  a cell-origin bundle to re-verify. Open decision: where cell-origin bundles
  are imported and verified (an E04 reader for them, or another store), and
  which digest the E05 `bundle_sha256` means for that family.
- **E09 CNA.** The snapshot takes two upstream results and two caller-authored
  authority objects. It has no E05 record, result ID or method digest, so
  nothing ties it to an E04 result or an E06 source. Open decisions: which E04
  result and E06 source represent a snapshot built from two results, where the
  dosage-QC and ichor results are stored and verified, and how the explorer's
  execution, trust, qualification and inspection states derive from E04/E01
  authority instead of the caller.

## Registration

`register_fragment_artifact(*, e06_selector_id, e06_source_version,
expected_member_sha256, expected_result_id, counterpart_record, policy)`:

1. captures the counterpart record and policy as exact bounded canonical
   bytes;
2. resolves the E06 source through the pinned `ResultViewSourceRegistry.resolve`,
   which takes the D06 fence and the E06 lock and releases both;
3. enters the D06 record-status fence for that source's cohort and requires the
   status digest to equal the one E06 verified, so no D01, D05, D06 or E04
   change landed in between;
4. finds both member bindings by the digests E06 returned;
5. replays the E05 decision from the E06 record, counterpart record and policy;
6. re-verifies both E04 bundles with the pinned `verify_reference`;
7. builds the two E07 sources and the view with the fixed parameters and checks
   the comparison semantics against E06; and
8. publishes the object under the exclusive registry lock, appends one
   hash-chained journal entry, reloads the committed state and returns a
   receipt, with the D06 fence still held.

Exact re-registration is idempotent. A new E06 source version gets its own
selector.

## Every read re-derives

`resolve(selector_id, *, expected_e06_selector_id,
expected_e06_source_version, expected_member_sha256, expected_result_id)`
locates the object under a shared lock and releases it, requires the
expectations to match, resolves the E06 source, enters the D06 fence (which must
observe E06's status digest), re-runs steps 4 to 7 from the stored counterpart
record and policy, takes the shared lock again, re-reads the object, and
requires the derived object to be byte-identical to the stored one and the
stored view to pass `replay_fragment_explorer_view`. Any failure, including an
E06 stale or missing source, returns nothing and raises
`MeasurementSourceArtifactRegistryStale`. The D06 fence and the registry lock
are held through construction of `RegisteredFragmentSourceArtifact`, whose
`artifact_replay_sha256` covers every returned commitment.

The digest is not a signature, and the result is an as-of read bound to one D06
status and one E06 state head. A consumer composing it with other authority
must hold the composite E12 fence or call `resolve` again inside it.

## Selector page

`list_selectors(cohort_selector_id, cohort_version, *, after_selector_id=None,
limit=50)` returns 1 to 100 rows for one cohort, ordered by selector. A row
carries the selector, family, object digest, artifact digest, E06 source digest,
and `current|stale`. It carries no result, member, bundle, cohort, provider,
subject, collection, run or analysis identity. Each row's E06 source is
resolved first; then one D06 fence must observe the status digest of every
current E06 row (otherwise the page raises stale and the caller retries), each
row is re-derived under it, and the selection must be unchanged under the final
registry lock.

## Fence composition and lock order

Measured on the current code (`test_fence_composition_and_lock_order`):

- Inside a held D06 fence, `ResultCatalog.verify_reference` works on the same
  thread (the E04 locks the fence holds are reentrant).
- Inside a held D06 fence, `ProviderLinkageStore.authority_read_fence` raises
  `linkage store authority fence requires an idle connection`, and E06
  `resolve` and this registry's `resolve` both fail closed as stale, because
  each enters D06 itself.
- E06 `resolve` therefore runs before the D06 fence, never inside it, and the
  status-digest equality joins the two.

Order inside this registry: E06 resolve (D06 fence, then E06 lock, both
released) -> D06 fence (D01, D05, E04 catalog, E04 trust, D06 root) -> this
registry's lock. The registry lock is never held while E06 or D06 is acquired.
That matches the E12 plan's position for family-source artifacts after D06, E04
and E06. This registry does not provide the composite E12 fence; joining its
lock to that fence is left to the authority-fence prerequisite.

## Storage, bounds and seals

Storage is at D03 parity and follows the E06 registry: a private `0700` root
with `0600` files; descriptor-relative publication with fsync and hard-link
adoption; a hash-chained journal from a genesis digest over immutable metadata;
reopen requires the retained registry ID, epoch and head; a process-wide head
fence plus a per-instance trusted head detect rollback; inode-bound control
files; the journal is the commit point and at most one uncommitted object is
tolerated; a failed journal append truncates its torn suffix; `backup_bytes`
and `restore` into a new root, where a failed restore, including a failed final
reopen, removes its target.

The metadata binds the E06 registry ID, epoch and metadata digest; the E06
metadata binds the cohort registry, D01 linkage store, E04 catalog storage and
reader registry, and D06 scope. Reopening or restoring against another E06
registry fails.

One object holds at most 8 MiB (a four-bin fixture is about 19 KB; E07 caps a
chart at 4,096 bins). The registry holds at most 10,000 artifacts and 256 MiB
of object bytes, checked before publication.

Seals are at D05 parity: class and instance method seals, an alias seal over
the sealed constructors, selector and digest helpers, an authority seal over the
pinned E06 resolve, D06 fence, E04 `verify_reference`, E05 decision replay and
E07 source, state, view and replay functions, and an instance-state seal. There
is no whole-module namespace seal, so the interpreter's `__warningregistry__`
does not disable the registry.

Threat model: in-process code mutation and same-user filesystem races are out
of scope. The seals and storage checks fail closed on accidental drift; they do
not defend against a hostile process running as the same user.

## Known gaps and open decisions

- E08 and E09 artifacts cannot be bound to an E06 source; see above.
- The E06 counterpart record and policy are not exposed by E06 `resolve`, so
  this registry stores the caller's copies, pinned by exact decision replay.
  When the E06 decision is not `comparable`, the counterpart's
  `information_state` is only partly pinned (E06 lists it as caller-asserted),
  and it can change panel B's state. Panel A, the subject, is fully derived.
- The method-authority head is the import-time head pinned by E04, as in E06
  (`method_authority_head_current_verified=false`).
- The crash-recovery gaps shared by every registry (interrupted root creation,
  torn tails found on reopen, staged restore) and parse-everything reads apply
  here too.
- The rollback fence is per process. A fresh process trusts the retained head
  it is given.
