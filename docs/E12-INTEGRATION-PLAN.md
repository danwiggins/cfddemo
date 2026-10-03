# E12 cohort/timepoint integration plan

Status: implementation map at `7df3ca834217c06c99dfdcae3f99885e0d8d949d`.
This document is not qualification evidence and does not authorize release,
export, provider operation, or clinical interpretation.

## Product boundary

E12 is the protected D08 longitudinal read model plus its bounded public
projection in the local result explorer. The selector is an authorized D05
registry selector and cohort version. The output is an exact source-record
table first, followed by optional series whose points are grouped by biological
collection time. Technical replicates and reanalyses remain source rows at the
same biological timepoint and never create another draw or denominator unit.

Verified source measurements and D09 denominator counts remain visible whenever
their own source authority is valid. Comparative D07 delta, comparison
uncertainty, shared-series axis, and connecting segment are separate fields and
are suppressed unless current D01 linkage, D04 supersession, D05 membership,
D06 record availability, D03 anchor-relative compatibility, and D07
repeatability gates all pass for that exact comparison. D09 counts are shown
alongside as aggregate population context only: D09 is not a comparison gate,
and a D09 `included` count never implies comparison eligibility. This separation
allows incompatible records to remain inspectable as distinct unconnected
series without implying comparison. D10 context is displayed as a confounding
limitation and never changes source values, denominators, or eligibility.

## Requirement and authority map

| Requirement | Authoritative input | E12 check and projection | Acceptance evidence |
| --- | --- | --- | --- |
| Authorized cohort selection | Exact `CohortRegistry`; `resolve_history(selector_id, …)` result with registry ID, epoch, state version/head and manifest digest (the selector is the call's input, not a returned field) | Resolve under the live registry; reject caller-built manifest, browser alias, stale head, or changed linkage authority | Selector resolves current registered history; rollback, stale selector, caller manifest, and head race fail closed |
| Authorized reader | Prerequisite protected `ReaderAuthorizationRegistry` containing externally provider-authorized, signed and revocable `longitudinal_reader` grants; independently pinned registry ID, epoch, expected head and B01 session binding | Every selector, workspace and source-detail route resolves the opaque session credential to one current, unexpired reader grant under the live registry. The builder receives the live registry plus the session credential, never a caller-authored role, principal, grant, scope or approval digest | Missing, forged, expired, revoked, wrong-role, wrong-scope, stale-head, registry-replacement and grant-revocation races fail with `permission_denied` before protected selector or workspace reads; synthetic tests use only a checked-in synthetic provider authority |
| Subject/draw linkage | Live `ProviderLinkageStore` state; D05 members retain only `committed_receipt_sha256`, and D03 series decisions retain the receipts themselves | Never infer identity from result aliases, dates, method labels, or adjacency; revalidate through D05/D03 APIs | Wrong subject, corrected linkage, tombstone, stale receipt, cross-store receipt, and authority advance suppress all comparisons |
| Biological timepoint | D05 v2 signed collection event, `CohortManifest.time_axis`, and `CohortMember.biological_timepoint_id/time_coordinate` | Derive one public timepoint ordinal and a stable signed-seconds offset from the first biological coordinate; expose controlled axis kind, `seconds` unit, definition digest, and relative/absolute semantics, but never the protected handle, collection token, or absolute collection timestamp | Sibling specimens share a point only as policy permits; technical rerun/reanalysis remains at its source collection coordinate; unequal intervals retain unequal numeric spacing |
| Technical rerun and reanalysis distinction | D05 lineage role/source edge plus the prerequisite live D04 bounded record-history snapshot | Source table carries controlled `biological_draw`, `technical_replicate`, or `reanalysis` role; only a manifest denominator contributor counts as a biological unit | Reanalysis twice is idempotent; superseded source remains immutable/history-only; active replacement does not create a timepoint or denominator |
| Verified result availability | D06 registry-bound `CohortManifestRecordStatus` and bindings | Preserve `available`, `missing`, and `withheld`; no missing/withheld row contains result details or a zero value | Exact registry/catalog/linkage/status identities match; revocation, stale registry, missing record, malformed binding, and read race fail closed |
| Immutable source/result identity | Protected result-view-source registry resolution bound to D06/E04 identity plus current E06 `ResultViewSource` replay verification | Bind registry ID/epoch/head and selector/version, result, bundle, method/version, capability, compatibility, denominator ledger, and source replay digest | Caller-owned source objects, stale/rollback registry state, or alteration of any identity or denominator field rejects the workspace; one result cannot represent two members |
| One pinned anchor and six outcomes | Live-replayed D03 v3 series decision | Evaluate every member against the same pinned anchor; expose exact outcome/reasons/action | Property test every compatibility-key dimension; adjacency cannot bridge an anchor mismatch; old policy schema fails closed |
| Standalone source values | Closed versioned family adapter over an applicable replayed E07 `FragmentExplorerView`, E08 `CellOriginExplorerArtifact`, or E09 `CnaExplorerSnapshot`, bound through the exact E04 result and E06 source authority | Bind the requested D02 measurement definition, quantity, unit, and one exact family coordinate before projecting values. A missing adapter is an explicit prerequisite/unavailable source-value state; D07 observations or comparison inputs never substitute for it | Wrong family/panel/bin/statistic/contributor/chromosome/segment, ambiguous or multiple match, quantity/unit mismatch, table/chart drift, and value-ranked selection reject the row |
| Approved anchor and projection selection | Prerequisite protected `AnchorPolicyRegistry` and `ProjectionPolicyRegistry`, each with independently pinned registry ID, epoch, expected head and bounded selector/version | Resolve one registered D03 anchor policy/D07 envelope, one approved anchor within that policy's live candidate page, and one registered family/statistic/coordinate policy; the anchor/projection-selection portion of the browser request carries only those three selector/version pairs and cannot author or hash a policy or anchor identity | Caller-created anchor/policy/envelope or projection subsets, value-ranked policies, stale selectors, rollback, registry replacement, head race and unregistered coordinates fail closed before member or source-artifact reads |
| Delta and trend suppression | D03 outcome plus D07 comparison replay | Only `equivalent` or `qualified_compatible` with D07 `available` may expose D07 comparison values, comparison denominators, delta, comparison interval, shared axis, or connecting segment. Independently verified E07/E08/E09 standalone values and D09 counts remain visible in their own fields | `requires_reanalysis`, `registered_bridge`, `incompatible`, `unknown`, outside-envelope, missing, failed, insufficient, or stale comparison states contain no D07 numeric fields or segment; valid measurement-specific source rows remain visible as separate series |
| Repeatability uncertainty | D07 signed observations, result trust, registered envelope/evidence/protocol/authority | Copy D07 fields only from an available replayed D07 comparison; for every unavailable D07 result, preserve the contract's exact suppression of both comparison values, both comparison denominators, delta, and uncertainty | Same-value, noisy, missing, incompatible, unregistered factor transition, revoked key, and stale evidence cases remain distinct |
| Denominators and missingness | Registry-bound D09 v3 summary derived from D05/D06 only (no E06 ledger or comparability evidence) | Show declared/included/excluded/unavailable members and denominator units; preserve missing and withheld as states. An E06 ledger is shown only if labelled "operator-entered, unverified" (or hidden), never as equivalent to D09 counts | All counts reconcile for zero-included-unit, one-included-unit, partial, and full cohorts; filters never rewrite scientific counts |
| Batch/preanalytical context | D10 result derived from the exact D09 population and exact D03 decisions | Show aggregate group states and controlled limitation reasons; never silently correct or attribute change biologically | Protocol/timepoint alias, mixed context, and missing metadata remain visible; values and eligibility remain unchanged |
| Filters | E06 independent state axes plus E12 controlled cohort/timepoint/lineage filters | Filters select rows after authority construction; they cannot alter source contracts, denominators, compatibility, qualification, or role | Empty filter retains denominator ledger; timepoint filter includes all rows assigned to the selected biological collection; lineage filter cannot promote a technical row |
| Supersession and history | Live D04 snapshot/status plus immutable D05 version | Current comparisons use active leaves. Superseded source rows remain visible as immutable history with an affected-comparison warning and no current segment; historical cohort versions remain addressable but visibly stale when authority changed | Supersession invalidates derived comparisons; replacement and source are distinguishable; old records and cohort versions remain immutable and are never rewritten |
| Saved comparison | Prerequisite exact `LongitudinalComparisonRegistry` with independently pinned ID, epoch and expected head | Save only through transactional no-overwrite publication under final dependency fences; reopen immutable bytes by a privacy-safe selector and replay current authority before any current result | Exact retry, conflict/race/crash recovery, bounded reads, backup/restore, rollback detection and stale reopen pass; absent/unhealthy registry disables Save |
| Typed boundary failures | Exact registry, store, catalog, trust, and reader operations | Authority verification failure, tamper, unparseable trust authority, storage/process-integrity failure, and read race return controlled typed safe errors and no workspace payload. A stable verified authority response of `missing`, `withheld`/revoked, `incompatible`, or `insufficient` remains an ordinary record/scientific state | Each injected boundary failure maps to its exact error code/remediation and cannot be downgraded into an unavailable row; a stable revoked fixture remains a revoked row |
| Privacy | Protected model retains D01/D05/D06/D04 identifiers; public model exposes registry-scoped selector, ordinal, controlled aliases, digests, counts, and states only | Reject provider, subject, collection, specimen, run, analysis, raw sample/read, path/URI, sequence, and free-form labels at browser boundary | Seeded private identifiers and encodings cannot reach canonical public bytes, HTTP JSON, HTML, logs, screenshots, or portable export |
| Release behavior | Literal capability fields | `product_release_authorized=false`, `release_export_authorized=false`, `diagnostic_interpretation_allowed=false`; no API accepts a caller release decision | Forged booleans/extra fields reject; local research inspection can remain available without upgrading release state |
| Accessibility and exact values | D08 public projection and packaged E14 renderer | Native table is authoritative; chart is supplementary; text conveys state; source and mismatch details are keyboard reachable | Semantic table, headers, focus order, non-color state, reflow, keyboard and screen-reader checks; observed audits remain E14 evidence |
| Epic D first release cut | Installed package plus maintained operator, quickstart and rollback documents/evidence | Run an offline synthetic installed journey and rollback rehearsal without source checkout, network or donor data | Exact package/workflow/store identities, retained immutable records, capability disablement, backup/restore and pass/fail evidence are recorded; external qualification remains separate |

## Required implementation cut

Add `evidence_inspector/longitudinal_workspace.py` only after the remaining merge order below completes.
The module owns the protected-to-public boundary and contains these contracts:

- `LongitudinalWorkspaceRequest`: registry-scoped cohort selector/version,
  independently registry-scoped anchor-policy selector/version and
  approved-anchor selector/version, projection-policy selector/version,
  normalized controlled filters, the D09
  policy selector/version, and requested D02 measurement
  definition/quantity/unit. The approved-anchor selector resolves only within
  the chosen policy's bounded live candidate page. It carries no caller-chosen anchor record, D03
  policy ID/digest, D07 envelope ID/digest, family coordinate,
  projection-policy bytes or digest, manifest or linkage identifier, and never
  carries a release flag.
- The browser session credential and protected reader authorization are not
  fields of `LongitudinalWorkspaceRequest` and never enter its replay or public
  bytes. They are separate boundary inputs resolved through the live
  `ReaderAuthorizationRegistry`; no route or builder accepts a role name,
  principal, grant object, scope list, signature, or approval digest from the
  caller.
- `ProtectedLongitudinalRow`: exact manifest-member commitment, lineage role,
  denominator contribution, protected timepoint commitment and coordinate,
  D04 record-history identity, D06 binding/status, E06 source, the applicable
  replayed E07/E08/E09 measurement artifact, D03 decision, and D07 comparison.
  It never crosses the HTTP boundary.
- `LongitudinalSourceRow`: ordinal, public biological-timepoint ordinal,
  stable coordinate offset in signed seconds, controlled lineage role,
  current/superseded state, affected-comparison warning, record availability,
  public catalog aliases, exact result/method/source commitments,
  compatibility outcome/reasons/action, denominator state, and independently
  verified measurement-specific source values/uncertainty. It contains no
  protected member or timepoint digest. It contains no numeric source value
  when the applicable E07/E08/E09 projection is absent.
- `LongitudinalSegment`: adjacent public row ordinals and exact comparison
  digest plus comparative delta/interval fields. It exists only when both
  endpoint rows pass live D03 and D07 gates; rendering code never infers
  segments from adjacent source values.
- `LongitudinalWorkspace`: exact registry/D06/D09/D10/D03/D07/D04 authority
  commitments, reconciled population counts, rows, explicitly supplied
  segments, privacy-safe time-axis kind, `seconds` unit, definition digest,
  coordinate semantics and stable offset origin, normalized filters,
  controlled limitations, replay digest, and literal disabled
  release/interpretation fields.
- `LongitudinalWorkspaceProjection`: aggregate/public contract accepted by the
  loopback explorer. Protected contracts and provider-local identifiers are
  structurally absent.
- `LongitudinalWorkspaceBoundaryError`: a closed safe exception carrying one of
  `invalid_request`, `permission_denied`, `authority_stale`, `trust_revoked`,
  `integrity_failure`, `storage_failure`, or `read_conflict`, plus a controlled
  remediation code. It carries no nested exception text or protected identity.

### Closed source-value projection family

E12 adds one discriminated, versioned union with no generic numeric fallback:

- `FragmentLongitudinalValueProjectionV1` binds the exact E07 artifact and
  source panel, D02 measurement definition/quantity/unit, fragment quantity,
  bin index and exact half-open bin boundaries, controlled statistic
  (`count` or `fraction`), statistic unit, source/result/method/bundle digests,
  and projected value. The bin must exist exactly once in both the authoritative
  panel rows and exact table.
- `CellOriginLongitudinalValueProjectionV1` binds the exact E08 artifact, D02
  measurement definition/quantity/unit, registered contributor ID, controlled
  `estimated_fraction` statistic and fraction unit, point estimate, typed
  interval state/bounds, atlas/result/method/bundle digests, and the matching
  dot/table row. The contributor must exist exactly once.
- `CnaChromosomeLongitudinalValueProjectionV1` binds the exact E09 snapshot,
  D02 measurement definition/quantity/unit, `dosage_qc` family, coordinate-grid
  digest, chromosome, controlled chromosome statistic/unit, and the exact
  layer/table value. `CnaSegmentLongitudinalValueProjectionV1` instead binds
  `segmented_cna`, grid digest, chromosome, zero-based half-open start/end,
  controlled segment statistic/unit, segment identity, and the exact
  layer/table value. Dosage and segmented coordinates never alias.

Each adapter request is derived from a closed canonical policy resolved from a
protected `ProjectionPolicyRegistry` before artifact values are inspected. The
registry has an independently pinned ID, epoch and expected state head, a
bounded opaque selector plus version, append-only canonical policy objects,
rollback detection, bounded list/get reads and final-head revalidation. A
browser/caller cannot submit policy bytes, a policy digest, a family coordinate
or a subset. The resolved policy binds one and only one family and coordinate,
the exact D02 measurement-definition/quantity/unit tuple, D05 measurement
anchor, policy-registry identity/head and registered policy digest. The policy
contains the finite allowed family/statistic/coordinate set or an explicit
canonical-all-components rule.
It cannot select `top`, `largest`, `most_changed`, minimum/maximum, or any
value-ranked component. A request matching zero rows, multiple rows, another
family, another panel, an unregistered contributor, a changed bin/coordinate,
or a mismatched output unit fails closed. Where the approved policy is
canonical-all-components, the adapter emits the complete canonically ordered
bounded vector and rejects a caller-supplied subset.

Replay reparses the exact artifact, reruns its family-specific replay function,
rechecks E04/E06 current source authority, resolves the selected coordinate
from both chart/layer and exact table representations, and requires equality.
The public projection carries family, controlled statistic, unit, privacy-safe
coordinate, value state/value and uncertainty only where the source artifact
authorizes them. It does not accept a caller-created scalar.

The construction entry point should be one pinned unbound function:

```python
build_longitudinal_workspace(
    request,
    *,
    reader_authorization_registry,
    reader_session_credential,
    cohort_registry,
    cohort_record_catalog,
    supersession_store,
    anchor_policy_registry,
    projection_policy_registry,
    result_view_source_registry,
    measurement_source_artifact_registry,
    d03_decision_registry,
    d07_comparison_registry,
    d09_summary_registry,
    d10_context_registry,
) -> LongitudinalWorkspace
```

Every caller-owned contract is captured as exact bounded canonical bytes before
any authority operation. The function requires exact concrete store classes,
invokes captured unbound methods, and performs no caller callback. It resolves
the opaque session credential to one current externally provider-authorized
`longitudinal_reader` grant and exact allowed cohort/measurement scope through
the live reader registry before any protected selection or artifact read. It
then resolves D05 through the registry, resolves the approved anchor policy/D07 envelope and
the approved projection policy through their independently pinned registries,
derives the bounded anchor-candidate page from the registered policy and live
authority, resolves the request's explicit opaque approved-anchor selector from
that exact page, obtains D06 status for the cohort selector/version,
reads D04 active and bounded history state, obtains the D09 summary from its
live registry/catalog authority, replays each applicable E07/E08/E09 artifact
from durable family-source discovery against its exact E04/E06 source, replays
the registered D03 series decision and each D07 comparison against current
authority, and obtains D10 derived from that exact D09 population and replayed
D03 decision set. Construction and return require the composable authority-
fence prerequisite below; the builder was prohibited until that prerequisite
merged (it merged in #80 and #81; the D08 builder followed in #83). Under that protocol it acquires the live D01 linkage, D04 history, D05
cohort, reader-authorization, D06 record/catalog, E04 catalog, the protected E06 result-view-source
registry, D03 decision, D07
comparison, D09 summary, D10 context, family-source, anchor-policy and
projection-policy read fences in their fixed global order, captures every
ID/epoch/version/head and canonical input, builds only from those snapshots,
and revalidates every captured identity/head immediately before return while
the complete fence set remains held. No parser, family adapter, renderer,
callback or network operation runs while authority locks are held. Authority mismatch,
tamper, trust-source verification failure, or read race raises a typed safe
boundary error and returns no workspace payload. Those failures never
masquerade as scientific unavailability or produce a partially current chart.
A stable, successfully verified revoked/withheld status remains visible as that
exact state.

The first implementation should be limited to one measurement and one pinned
anchor per workspace. Multiple incompatible outcomes retain values from their
applicable verified E07/E08/E09 artifacts in separate nonconnected series
groups. If that measurement has no reviewed source projection, E12 names the
missing projection as a prerequisite and renders no standalone numeric value.
Registered bridges remain review-only and do not create translated values.
Cohort statistics beyond D09 and automatic batch correction remain out of scope.

### Protected reader-authorization prerequisite

B01 session possession proves only local transport authentication. Before any
E12 selector or builder route lands, add a durable bounded
`ReaderAuthorizationRegistry` whose immutable grants bind registry ID/epoch,
opaque grant selector, external provider-authority ID and key version, exact
`longitudinal_reader` role, allowed cohort-registry and measurement scopes,
issued/expiry times, revocation state, and a canonical provider signature.
There is no wildcard role or caller-selected scope. Production startup requires
an independently retained registry ID, epoch and expected head plus configured
provider trust authority; a missing registry or grant disables E12. Checked-in
synthetic keys and grants are accepted only when the entire installed run is in
the explicit synthetic profile and cannot authorize another profile.

Bootstrap uses a separate opaque one-use launch credential mapped server-side
to a grant selector. Successful exchange stores only the exact grant commitment
and reader-registry head in server-side session state. Every E12 read and save
resolves that commitment again under the registry fence and fails before other
protected reads if it is missing, expired, revoked, out of scope, signed by an
untrusted key, or no longer at the expected authority head. Grant add/revoke,
provider-key rotation and session resolution share the same cross-process
fence; revocation cannot land between final authorization revalidation and the
returned selector, workspace, source detail or save receipt.

The registry has closed exact schemas, bounded grant/session indexes, canonical
append-only grant and revocation records, rollback detection, descriptor-safe
storage, no-overwrite publication, backup/restore identity, and typed safe
errors. Tests cover forged grants and signatures, wrong role/scope/provider,
expiry edges, revocation and key rotation, stale/rollback heads, registry/root
replacement, class/instance/private-state hooks, oversized inputs, concurrent
bootstrap/read/save versus revoke, crash recovery, and absence of reader,
provider and scope identifiers from every public byte, error, log and route.

### D04 record-history dependency

Current main includes the reviewed protected bounded history API required by
D08:

```python
RecordSupersessionStore.record_history_snapshot(
    *, cursor: RecordHistoryCursor | None = None, limit: int = 100
) -> RecordHistorySnapshot
```

`limit` is closed to `1..1000`. The snapshot binds ledger ID/epoch/storage,
ledger state version/head, linkage-store version/head, cursor, and canonical
records. Each protected row carries the exact immutable `SupersedingRecord`,
record digest, `active|superseded|authority_invalid` state, successor record ID
when present, and controlled affected-comparison IDs/reasons. Canonical order is
record ID with deterministic no-gap/no-duplicate paging. Provider, analysis,
linkage, approval, and protected lineage fields remain inside this API and are
replaced by safe catalog aliases, digests, and controlled status in D08 public
rows.

One provider-linkage read fence and one D04 SQLite snapshot cover state
validation, full-chain/cycle/branch checks, bounded page construction, final
state revalidation, and return. The API requires the exact store class, invokes
captured unbound operations, accepts no callback or caller-authored snapshot,
and fails with typed safe errors on authority advance or storage race.

Focused D04 evidence covers acceptance of `limit=1,000`, rejection of
`limit=1,001` and other invalid limits, deterministic pagination,
changed/missing/duplicate rows, broken
source/successor edges, cycles, branches, stale linkage, tombstone, concurrent
supersession, schema/index/row tamper, root/database replacement, class and
instance shadows, Pydantic private/extra state, oversized graphs, non-forgeable
store-keyed cursor authentication, rollback/reopen/backup behavior, and safe
error handling. A populated exact 1,000-row page and seeded protected-token
absence from the future D08 public projection remain E12 acceptance tests; D04
does not itself expose that public projection. D08 must consume this API under
the composite fence below rather than treating a prior snapshot as current
authority.

### Composable authority-fence prerequisite

Built since this was written (#80, #81); see the note at the end of this
section. The text below is the plan as specified.

The existing D04 and D06 public reads internally acquire the D01 linkage fence,
whose non-reentrant cross-operation guard rejects nested entry, and E04 exposes
no composable read fence. D08 must not simulate an atomic snapshot by nesting
those public APIs or by reading and later comparing unlocked heads. Before the
workspace builder or saved-comparison publication can land, add reviewed
authority-fence adapters in prerequisite PRs for D01, D04, D05, the protected
reader-authorization registry, D06, E04, the
protected result-view-source registry with current E06 replay verification, D03,
D07, D09, D10, family-source, anchor-policy and projection-policy stores.

Each adapter exposes a pinned unbound acquire/release boundary plus an exact
bounded immutable snapshot operation that assumes its fence is already held.
Every mutation that can change eligibility, trust, values, policy or a returned
head shares that same fence. The adapters bind exact store type, instance, ID,
epoch, storage identity and lock identity; reject public/private/class shadows;
and never accept a caller snapshot or callback. D04 and D06 gain already-fenced
internal reads rather than reopening D01. E04 gains a catalog and trust-
authority fence shared with key add/revoke and catalog mutation.

One coordinator acquires all adapters in the fixed global order documented
below, constructs snapshots with already-fenced operations, performs only
bounded canonical validation while held, revalidates every head in reverse
order, constructs the immutable return value, and releases after return-value
construction. Expensive parsing, family replay and rendering use descriptor-
or content-pinned bytes prepared before the final fence, then their exact
digests are rechecked while held. Opposing mutation/read/save tests prove
termination without deadlock and prove no authority change can land between
final revalidation and the returned object. Until these APIs exist and their
lock order is independently reviewed, D08 and Save remain unavailable.

Built: the coordinator and store fences in
`evidence_inspector/composite_authority_fence.py` (#80,
`docs/COMPOSITE-AUTHORITY-FENCE.md`) and the E04 cross-process catalog-content
fence and content head (#81). The D08 read model (#83) and browser Save and
Reopen (#93) run under them.

### Durable saved-comparison registry prerequisite

Saving and reopening is not an in-memory D08 feature. Add a separate protected
`LongitudinalComparisonRegistry` in its own prerequisite PR. D08 must keep Save
disabled until this registry is installed and healthy.

The registry has a closed exact schema and these contracts:

- `LongitudinalComparisonRegistryMetadataV1`: registry ID, registry epoch,
  storage identity, schema version, bound cohort-registry ID/epoch,
  D04-ledger ID/epoch, reader-authorization registry ID/epoch, D06 catalog
  authority, E06 result-view-source registry ID/epoch, and creation digest;
- `SavedLongitudinalComparisonV1`: immutable canonical selection, exact family
  source-value projection request, cohort/manifest/policy/measurement/anchor,
  D03/D07/D09/D10/D04/source commitments, protected reader-grant commitment,
  exact E06 source-registry selector, version and state head, normalized
  filters, workspace replay digest, creation time, literal
  local/synthetic/nonrelease states, and content digest;
- `SavedComparisonJournalEntryV1`: sequence, predecessor head, safe opaque
  selector, saved-object digest, exact dependency-head vector, and entry digest;
- `SavedComparisonRegistrationReceiptV1`: registry ID/epoch, state version/head,
  safe selector, object digest, and dependency-head vector; and
- bounded `SavedComparisonSelectorPageV1` and `RegisteredSavedComparisonV1`
  read results. The browser selector is registry-scoped and opaque and reveals
  no cohort, provider, subject, collection, analysis, or record identity.

One registry contains at most 1,000 saved objects. Canonical saved-object input
and output are each capped at 512 KiB, a recovery record at 64 KiB, the journal
at 4 MiB, one selector page at 100 rows, and a complete backup at 520 MiB. The
registry tracks cumulative object bytes and rejects admission before writing
when the backup bound would be exceeded. Integer tokens, nesting depth, graph
nodes, strings and collections also use explicit pre-serialization limits.

Publication holds one exclusive registry lock plus final read fences for every
mutable identity persisted in the object or authorizing the operation: D01
linkage, D04 history, D05 cohort, reader authorization, D06 record/catalog, E04
catalog, E06 result-view-source registry, D03 decision,
D07 comparison, D09
summary, D10 context, family-source artifact, anchor-policy authority and
projection-policy authority.
It canonicalizes and bounds all input before opening a
transaction, writes the content-addressed object with descriptor-relative
no-follow/no-overwrite operations, fsyncs it, appends and fsyncs one hash-chained
journal entry, reloads the complete registry state, rechecks all dependency
heads inside the final fences, and only then returns a receipt. An exact retry
is idempotent. The same selector/version with different bytes, digest collision,
or changed authority is a conflict and never overwrites an object.

All dependent operations use one documented lock order:
`D01 linkage -> D04 history -> D05 cohort registry -> reader-authorization registry
-> D06 cohort record catalog
-> E04 catalog -> E06 result-view-source registry -> D03 decision -> D07 comparison -> D09 summary
-> D10 context -> family-source artifacts -> anchor-policy registry
-> projection-policy registry
-> saved-comparison registry`. No callback, renderer, parser hook or network
operation executes while those locks are held. Concurrency tests prove opposing
save/read/supersession/cohort-registration/import operations terminate without
deadlock and return either one exact committed head or one typed retry error.

Findings from the merged prerequisite registries that the coordinator must
resolve before this order is final:

- The D09 summary builder cannot run inside a held D01 linkage fence or a held
  D06 record-status fence, so the D09 policy registry (#59) takes its own lock
  first and the D05/D06 fences inside it. Either D09 moves ahead of D01 in this
  order, or D06 gains an entry point that builds inside an already-held fence.
- D07 (#60) adds `compare_repeatability_in_fence`, which requires the caller to
  hold this process and thread's `ProviderLinkageStore.authority_read_fence`
  (recorded by the store as `(pid, thread)`), so D07 composes inside D01.
- The E06 source registry takes the D06 record-status fence (which itself holds
  D01, the D05 lock, the E04 catalog and trust locks, and the D06 root) and then
  its own lock, never the reverse.
- The live D10 build (#64) fails inside held D01 or D06 fences and works inside
  held D09 or D03 registry locks, so the D10 context lock comes first:
  D10 → D09 → D01/D05/D06 and D10 → D01 → D03. That is acyclic but, like D09,
  conflicts with the order above.
- The anchor-policy registry (#66) takes the linkage fence, then the D05 shared
  lock, then its own lock, matching the order above. It calls the cohort
  registry's private in-fence read (`_lock` plus `_resolve_history_in_fence`);
  the coordinator should turn that into a reviewed public API.
- The result-trust store (#68) is read under its own shared lock between the D01
  fence and the D07 lock: D01 → result trust → D07. Never touch the linkage store
  or call D07 while holding a trust fence.
- The reader-authorization registry (#67) supplies one cross-process fence plus
  in-fence reads (`authorize_reader_in_fence`). It is first in the order above.

The private root is mode `0700`; metadata, journal, lock, recovery records and
objects are owner-only regular files with bound descriptors. Cross-process
locking serializes writers. A durable candidate/recovery record is written
before object publication. Startup recovery validates candidates independently
of replaceable temporary files, adopts only exact fully committed bytes, and
otherwise removes only the incomplete candidate's own object/journal intent.
Missing, truncated, substituted or extra files fail closed. A process-observed
head cannot move backward. Existing roots require independently retained
registry ID, epoch, and expected head; metadata deletion never bootstraps a new
identity.

`resolve()` and bounded selector pages (`limit 1..100`) hold a shared registry
snapshot plus current dependency authority fences through parse, replay, final
head check, and result construction. Reopen returns immutable saved bytes plus
current/stale status; current workspace values and segments require a fresh E12
replay. The registry never treats a saved digest or receipt as current
authority.

`backup_bytes()` captures exact metadata, journal, recovery-free state and every
committed object under one lock with fixed object/count/byte bounds. `restore()`
requires an empty private target plus independently supplied registry ID,
epoch, expected state head, dependency-store identities and current authority;
it verifies the whole chain before publication and reopens through normal
checks. Rollback or truncated backup is rejected. Application rollback may stop
new saves and retain old readable objects, but it never rewrites saved bytes or
blesses an older head.

Registry tests cover exact retry, conflicting publication, two-process race,
lock interruption, crash after candidate/object/journal/fsync boundaries,
orphan cleanup, no-overwrite, symlink/hardlink/FIFO/device/permission attacks,
root/metadata/object/journal replacement, schema/index/extra-file tamper,
rollback and peer-head advance, stale dependencies during final return,
bounded parse/read/page limits, backup/restore identity and truncation,
private/extra state and public method shadows, and seeded protected identifiers
in selector pages/errors. This registry is a required dependency and split PR;
the first D08 read-model PR may render an unsaved workspace but cannot claim
the selection/persistence journey complete.

## Browser integration

Extend `IntegratedExplorerSource` with one exact optional longitudinal source
adapter after the read model exists. Add bounded routes for selector listing,
workspace projection, and source-detail projection. Reuse B01 session, Host,
Origin, CSRF, and no-network controls only after extending B01 bootstrap/session
creation to bind each session to one live protected reader grant. A bare B01
session is transport authentication, not longitudinal authorization. Bootstrap
cannot mint the longitudinal capability from a caller role string: it resolves
an opaque one-use launch credential against the pinned
`ReaderAuthorizationRegistry`, verifies the external provider signature,
expiry, revocation and cohort/measurement scope, and seals the exact grant and
registry-head commitments into server-side session state. Every longitudinal
route replays that binding under the registry read fence before selector or
workspace access. The HTML order is:

1. comparison identity/version and authority state;
2. compatibility outcome, exact reasons, and permitted next action;
3. declared/included/excluded/unavailable denominator strip;
4. authoritative source-record table;
5. optional chart with explicit series breaks and uncertainty;
6. aggregate covariate limitation panel and source/provenance drawer.

The renderer must consume `LongitudinalSegment`; it may not draw one line over
all visible points. X positions use the stable signed-seconds offsets rather
than equal ordinal spacing. Filters are cohort version, public timepoint ordinal,
lineage role, record availability, compatibility outcome, and the existing E06
state axes. A filter digest and replay digest bind the visible rows while the
population denominator remains unchanged.

### Normative D08 selection and persistence journey

The journey is `Cohorts -> exact cohort version -> measurement -> approved
anchor -> version/policy diff -> results -> save`. The browser never accepts a
free-form cohort, record, or anchor identity.

1. The cohort selector comes from the authorized bounded D05 selector page.
2. The measurement selector contains only measurement definitions present in
   the exact D05 manifest and available through the exact D06/E04/E06 binding.
3. The request first resolves an opaque selector/version in the protected
   `AnchorPolicyRegistry`; it cannot submit an anchor identity, D03 policy or D07
   envelope. The derived anchor selector is capped at 1,000 entries and contains
   only candidates admitted by that exact registered D03 anchor policy and live
   linkage. It shows
   a safe alias, biological-timepoint ordinal/offset, method version, and exact
   eligibility state. There is no implicit first/latest/provider-primary anchor
   and no arbitrary record-ID field. The operator must choose one candidate; the
   request carries that registry-scoped opaque anchor selector/version and the
   builder resolves it against the same candidate page. Omission, injection,
   stale version or selection from another policy fails closed; there is no
   implicit first/latest/provider-primary choice.
4. Before any result table or chart, the operator sees a deterministic diff
   between the selected cohort version and its predecessor, or between a saved
   comparison's version and the version being reopened. The diff identifies
   added and removed member counts/commitments, unchanged count, inclusion,
   exclusion and missingness policy changes, unit-of-analysis/replicate/
   reanalysis rule changes, time-axis changes, measurement-anchor changes,
   authority-head changes, and D03 policy changes. Protected member identities
   remain inside the adapter. The public diff contains counts, digests, and
   controlled reason codes only. The operator selects the exact version after
   seeing this diff; the UI never silently upgrades it.
5. `Save comparison` calls the exact installed
   `LongitudinalComparisonRegistry.register()` boundary. The returned receipt
   is displayed only after transactional publication and the final authority
   fence succeed. Saving does not authorize export.
6. Reopen resolves the safe opaque registry selector, verifies the immutable
   object and registry head, resolves its exact cohort version, replays all
   current authorities, and presents the version/authority/policy diff before
   results. Historical saved bytes remain inspectable. If current authority
   differs, the comparison is visibly stale and has no current segments; saved
   bytes are never rewritten or relabeled as current.

Compatibility-specific actions are fixed by D03 and appear beside every row:

| Outcome | Required action | Comparison rendering |
| --- | --- | --- |
| `equivalent` | `use_direct_comparison` | Eligible for D07 comparison after all other gates pass |
| `qualified_compatible` | `use_qualified_comparison` | Eligible for D07 comparison after all other gates pass; show qualified evidence |
| `requires_reanalysis` | `request_reanalysis` | Separate source series; no D07 numeric comparison or segment |
| `registered_bridge` | `review_registered_bridge` | Separate source series; show bridge reference, never execute it |
| `incompatible` | `start_separate_series` | Separate source series; show exact mismatches |
| `unknown` | `resolve_unknown_inputs` | No comparison; show exact unknown dimensions |

### Normative D08 states and responsive behavior

| State | Required behavior |
| --- | --- |
| Loading | Show the exact local stage and elapsed time, no fake percentage, no stale chart relabeled as current, and a polite status announcement |
| Empty | State why no verified records or why fewer than two comparable draws exist; retain reconciled denominator counts and the next safe action |
| Error | Show a typed safe code, problem, remediation, and local retry action; expose no partial workspace or protected identifiers |
| Success | Show identity/version, eligibility and next action before the table and optional chart |
| Partial | Preserve available, missing, withheld, excluded, incompatible, and unknown rows distinctly; show separate series and exact reasons |
| Stale | Keep immutable historical rows visible with the changed authority and required refresh action; suppress current comparisons |
| Revoked | Keep the historical record identity/status visible, remove current values forbidden by its own trust contract, and suppress comparison |
| Permission denied | Return the same bounded denial shell regardless of record existence; no counts, selectors, or timing detail leak |
| Slow stage | Show stage and elapsed time, allow navigation away without duplicate work, and restore only after fetching a fresh revision |

Desktop keeps the source drawer beside the table/chart when space permits.
Tablet uses an overlaid drawer without obscuring the focused source row. Mobile
uses a full-width modal sheet with focus containment and a focus-restoring close
action; the exact values table precedes the chart in reading order. At 200%
zoom, all content reflows without horizontal page scrolling and every control,
table overflow region, reason, drawer action, and chart alternative remains
reachable. Text and meaningful non-text indicators meet at least 4.5:1
contrast, interactive targets are at least 44 by 44 CSS pixels, reduced-motion
preference disables nonessential transitions, and state never depends on
color, hover, animation, or pointer precision. These are implementation and
automated conformance requirements; observed keyboard, screen-reader, and 200%
audits remain E14 evidence.

## Tests required before merge

Create `tests/test_longitudinal_workspace.py` for pure and protected integration
tests and extend `tests/web/test_integrated_explorer.py` for the HTTP/DOM layer.

1. One biological draw plus technical rerun and reanalysis produces one public
   timepoint and one denominator contributor, while retaining three source rows.
2. Two distinct signed collection events produce two ordered timepoints even
   when their method/result labels are identical.
3. All six D03 outcomes are rendered distinctly; only `equivalent` and
   `qualified_compatible` can progress to D07 comparative numerics.
4. Every D07 unavailable state strips D07 `anchor_value` and `member_value`,
   both D07 comparison denominators, delta, comparison interval, shared-axis,
   and segment fields exactly as the D07 contract requires. Independently
   verified values from the applicable E07/E08/E09 artifact and D09 counts
   remain separate and visible.
5. An incompatible middle member cannot connect compatible endpoints; every
   segment is explicitly authorized anchor-relative.
6. Fixtures with collapsed technical replicates and reanalyses,
   policy-excluded members, D06 missing and withheld records, unqualified or
   provider-ineligible results, and zero- and one-included-unit cohorts yield
   reconciled D09 v3 aggregate counts and the matching summary state. The D09
   v3 summary serializes no per-member reasons, so each row's lineage,
   missing and withheld state comes from D05 lineage and D06 bindings on
   `LongitudinalSourceRow`. Failed or insufficient source results appear as
   source-row states from their own E06/E07/E08/E09 authority.
7. D04 supersession keeps the immutable source row with an affected-comparison
   warning while the active leaf alone controls current comparison. A stable
   verified result-key revocation renders an exact revoked/withheld row. Linkage
   correction/tombstone, method-authority advance, registry-head advance,
   trust-store mutation, and catalog mutation during construction return typed
   safe boundary errors rather than scientific unavailable states or mixed
   authority.
8. A caller-built manifest, status, E06 `ResultViewSource`, D09 summary, D03
   outcome/digest pair, D07 comparison, private/extra Pydantic state, subclass,
   proxy, mutable sequence, oversized graph, and instance/class method shadow
   fail before publication. Result-view-source registry wrong selector,
   cross-result/member binding, tamper, stale/rollback head and mutation during
   final return fail with a typed safe boundary error and no workspace payload.
9. Permuting caller input order yields byte-identical output; changing a filter,
   policy, member, decision, comparison, source, denominator, or covariate
   commitment changes the replay digest.
10. Source and timepoint filters never change declared population counts and
    never promote technical rows into biological units.
11. Seeded provider/subject/collection/specimen/run/analysis/sample/read tokens,
    paths, URIs, encoded variants, and sequence-like strings are absent from
    public bytes and browser output.
12. Browser tests cover loading, empty, error, success, partial, stale, revoked,
    permission-denied, and slow-stage states; native table semantics; controlled
    state text; separate SVG paths per authorized series; unequal x spacing for
    unequal time intervals; desktop/tablet/mobile drawer behavior; 4.5:1
    contrast; 44-pixel targets; reduced motion; 200% reflow/reachability; no
    external requests; and literal disabled release and export controls.
    They also prove a normal B01 session without a current externally authorized
    `longitudinal_reader` grant, a caller-injected role, wrong-scope grant,
    expired/revoked grant, stale registry head, registry replacement, and
    revocation during final return all yield `permission_denied` with no
    selector, protected read, workspace payload, log or partial response.
13. Public bytes retain controlled axis kind, `seconds` unit, definition digest,
    coordinate semantics, and stable signed offsets while excluding absolute
    collection timestamps and protected timepoint handles.
14. Language tests reject clinical meaning for increase, decrease, direction,
    magnitude, interval, or trend. The visible limitation states exactly that
    comparisons are descriptive technical differences with no causal or
    clinical interpretation.
15. An exact 1,000-member registered cohort completes deterministically within
    the declared graph and response bounds. A 1,001-member cohort is rejected
    immediately after the bounded registry selection check and before
    per-member authority traversal, serialization, or rendering with one typed
    bounded-input error.
16. The anchor selector contains only live policy-approved candidates, caps at
    1,000, requires an explicit choice, and rejects injected, stale, unapproved,
    over-bound, free-form, cross-policy and cross-registry record selectors.
    Candidate-page mutation between selection, build and final return fails
    closed rather than silently selecting a different anchor.
17. Saved comparison bytes are immutable, content-addressed, idempotent for an
    exact retry, and reopen only after exact live replay. Tamper, root swap,
    stale authority, and cohort-version advance preserve historical bytes and
    suppress current segments.
18. Added/removed membership and every authority/policy dimension in the
    version diff appear before results; no reopen silently advances cohort,
    policy, measurement, or anchor identity.
19. All six compatibility outcomes display the exact D03 action, and no action
    can execute a bridge, invent reanalysis, promote eligibility, or alter a
    source artifact.
20. Family-adapter tests independently cover fragment panel/bin/statistic,
    cell-origin contributor, CNA chromosome and CNA segment projections.
    Wrong-family, wrong-panel, shifted bin/segment, unregistered contributor,
    ambiguous/multiple coordinate, quantity/unit mismatch, chart/table drift,
    caller-created scalar, result-ranked selector, and incomplete
    canonical-all-components requests reject before a value is returned.
21. The saved-comparison registry passes transactional publication, exact retry,
    conflict, multiprocess race, every crash window, final-authority race,
    bounded paging, privacy, backup/restore and rollback tests listed in its
    prerequisite contract. D08 Save stays unavailable when the registry is
    absent, stale, corrupt, or over bound.

Run focused tests, the D01-D10 suites, the full offline suite, and
`python -m evals.harness`. An exact-head independent review must inspect the
authority fence, protected/public field split, delta suppression, denominator
preservation, graph bounds, and browser privacy before merge.

## Epic D first-release-cut completion evidence

D08 closes the local longitudinal-value cut D01-D08 only when implementation,
operator documentation, installed-package evidence and rollback evidence agree.
Add and review these maintained artifacts:

- `docs/LONGITUDINAL-WORKSPACE.md`: authority model, measurement-family
  projections, six compatibility outcomes/actions, denominator and missingness
  semantics, biological-timepoint versus rerun behavior, saved-comparison
  registry, privacy boundary, failure codes, limitations, and explicit
  descriptive-only/no-clinical-meaning language;
- `docs/quickstarts/LONGITUDINAL-SYNTHETIC.md`: one exact installed-package,
  offline, synthetic journey from local service launch through cohort/version,
  measurement component and approved-anchor selection, version diff, source
  table, separated series, save, reopen and verification. It uses actual
  packaged commands/UI, requires no source checkout, development server,
  network, donor data, or manually edited authority bytes, and records the
  installed application/workflow/schema identities; and
- `docs/rollback/LONGITUDINAL-REHEARSAL.md` plus a canonical local evidence
  record: starting and target application versions, registry/ledger/catalog
  identities and heads, backup digest, feature-capability transition, exact
  steps, expected observations, retained saved-comparison digest, post-rollback
  verification, recovery/forward path, owner, timestamp, and pass/fail result.

The installed quickstart test builds the distributable, installs it into a clean
temporary environment, denies network, runs only checked-in synthetic fixtures,
drives the real loopback session and browser routes, saves and reopens through
the durable registry, verifies no protected sentinel escaped, and compares its
canonical result to the documented expected state. Importing repository source
through the working directory or `PYTHONPATH` fails the test.

The rollback rehearsal backs up the exact stores, disables the longitudinal
capability, installs the prior supported signed application/workflow, verifies
that new comparison/save routes are unavailable, and verifies a retained saved
object and its source records offline without rewriting them or accepting an
older registry head as current. It then restores the supported forward version
and replays current authority. Crash recovery, backup/restore and application
rollback are separate checks and all three must pass. If the prior version
cannot read the new schema, the documented result is a controlled unavailable
state with preserved bytes, not an automatic downgrade migration.

The first cut remains local, synthetic and release-disabled unless its named
provider/scientific/governance gates separately pass. Documentation, a green
source checkout, or a successful synthetic quickstart cannot claim those gates.

## Remaining blockers

Historical list, written before #80. Since then the composite authority fence
(#80), the E04 content fence (#81), registry storage hardening (#82), the D08
read model (#83) and the browser routes, view, Save and Reopen (#93) have
merged. Resolved bullets below say so.

Before #80, implementing the builder would still have required accepting weaker
caller assertions and was therefore prohibited:

- Merged prerequisites: corrected D09 (#52), D10 (#54), the D03 decision
  registry (#56, #61), D10 resolving D03 from it (#57), the D05 stale-history
  read (#58), the D09 policy/summary registry (#59), the D07 comparison registry
  (#60), the E06 result-view-source registry (#62), D10 live D09 binding plus the
  d10_context_registry (#64), the projection-policy registry (#65), the
  anchor-policy registry (#66), the reader-authorization registry plus B01
  session binding (#67), the result-trust store wired into D07 (#68), the local
  operator reader authority (#73), E04 and `traceback verify` wired to the
  result-trust store (#74), the family-source artifact registry (#75), the
  closed source-value projection adapters (#76), the durable saved-comparison
  registry (#77), and a D04 concurrent-initialisation race fix (#78). Each
  registry derives its object itself, replays it against live authority on every
  read, and returns nothing when stale. Each result is valid as of one authority
  snapshot; composing them still needs the composite fence below.
- D09's registered v3 summary carries aggregate counts only; its protected member
  projection (`resolve_population`, #64) is for D10 and never public. Policy
  versions are not superseded: an older version that still rebuilds stays
  current. Rows on one D09 selector page may reflect different D06 snapshots.
- D10 `build_live_covariate_context` (#64) takes its population from the D09
  policy registry and its decisions from the D03 registry, joined on four keys.
  Covariate tokens (batch, protocol, preanalytics) are still caller-registered;
  no upstream authority for them exists. Decided (Dan, 2026-10-01): they are
  operator-entered, and E12 labels them "operator-entered, unverified", as for
  E06 ledgers.
- D05 `resolve_history_view` returns a `CohortHistoryView` that is always
  `presented_as_current=false` and labelled `current|stale`. History and reopen
  use it; current authority still comes only from `resolve_history`.
- D07 comparisons are evaluated at the linkage store's authority clock, and every
  read re-evaluates at live time. Use the returned `replayed_at`, not
  `evaluated_at`, as the as-of time.
- Result trust: the forward-only result-trust store (#68) closes the revocation
  gap for D07 registries opened with it. A revocation makes the affected
  comparisons stale on the next read, and old trust cannot revive revoked keys.
  E04 (`result_catalog`) and `traceback verify` are wired to it too (#74), with
  lock order catalog connection lock → trust fence; the `TrustStore` and D07
  fixed-document paths remain and could be retired later.
- E06: the source registry live-verifies member, D06 binding, E04 catalog and
  trust, and result/bundle/method identity. It cannot verify the denominator
  ledger, labels, compatibility-policy pin, or the fields in
  `CALLER_ASSERTED_FIELDS`; it marks them (`denominator_verified=false`).
  Capability is checked only against the head pinned at import
  (`method_authority_head_current_verified=false`) because no durable
  method-authority store exists. Decided (Dan, 2026-10-01): a denominator
  ledger is authoritative only when the pipeline computes it. Until then every
  E06 ledger is operator-entered, and E12 must hide it or label it
  "operator-entered, unverified", never presenting it as equivalent to D09
  counts. One result may represent members of different cohort versions; E12
  compares within one selected cohort version.
- Reader authorization (#67) works in the synthetic profile. Decided (Dan,
  2026-10-01, simplest operationally for now):
  - The provider authority is a local operator authority: one signing key
    generated on the workstation, held by the operator, with trust provisioned
    and rotated through a runner CLI command. There is no external provider yet.
  - The operator issues and revokes grants with the CLI. A grant is delivered by
    printing a one-use launch link to the terminal, as a notebook token is.
  - A bound session is ended only by changes to its own grant or signing key
    (revocation, expiry, scope, untrusted or rotated-out key), not by unrelated
    registry changes.
  Built in #73: `traceback reader authority|grant|launch`, a fragment-carried
  one-use launch link exchanged at `POST /api/v1/session/reader-launch`, and
  the own-grant session check.
- Standalone numeric values require the applicable E07, E08, or E09 replayable
  artifact and the closed family adapter above, bound through the exact E04/E06
  source and requested D02 identity/coordinate. A measurement without that
  reviewed projection remains explicitly unavailable in E12; D07 observations
  and comparison inputs are not a substitute source-value authority.
- The anchor-policy registry (#66) binds approved D03 policy and D07 envelope
  pairs and a live candidate page. A D03 v1 policy pins one complete anchor key,
  so each approval admits one anchor. Decided (Dan, 2026-10-01, simplest
  operationally for now): multiple anchors are offered as multiple approvals,
  with no D03 schema change; any record the policy admits may be an anchor,
  with no lineage-role restriction; and an expired envelope surfaces as D07
  `unavailable`, as it does today. E12 must bind each D07 comparison's
  envelope digest to the resolved anchor policy.
- The projection-policy registry (#65) holds closed per-family policies and
  replays no live authority. The E12 builder must call
  `require_projection_policy_binding` with its live D05 anchor and E04/E06
  source. E08 and E09 policies bind the registrant's exact D02 tuple, because
  those contracts pin no quantity or unit.
- Resolved (#80, #81): the composable cross-store fence required above exists
  as `evidence_inspector/composite_authority_fence.py`, and E04 has its
  cross-process catalog-content fence. Before those PRs, the D01/D04/D06/E04
  APIs did not expose it and builder work was blocked on it.
- Resolved: the saved-comparison registry (#77) publishes through a
  caller-supplied dependency fence. The former `LiveRegistryDependencyFence`
  (`direct_head_reread`) is retired and refused; `composite_authority_fence` is
  the only production fence kind, and browser Save (#93) publishes through it
  (`docs/LONGITUDINAL-COMPARISON-REGISTRY.md`, "Dependency-fence seam").
- The family-source registry (#75) derives E07 fragment artifacts only. E08 needs
  cell-origin bundle import and verification, and E09 needs a binding to E04/E06
  results plus authority from E04/E01. Per the one-measurement first
  implementation, E12 shows fragment standalone values and marks E08/E09 values
  explicitly unavailable. The projection adapters (#76) support all three
  families once artifacts exist; the E12 builder must resolve every projection
  policy from the registry, because the adapter cannot tell a valid unregistered
  policy from a registered one.
- Resolved (#82, `docs/REGISTRY-STORAGE.md`): across the D03, D05, D07, D09,
  D10, E06, family-source, anchor, projection, reader and result-trust
  registries, root creation and restore are staged and published by one
  `rename(2)`, owned temporary names are swept, a failed append truncates on any
  exception, and the result-trust lock-descriptor ordering is ported. A torn
  journal tail found on reopen still fails closed by design; the explicit
  `recover_torn_journal_tail` maintenance call repairs it. Still open: every read
  parses every committed object, which is fine at synthetic scale only.

The composable authority-fence adapters/coordinator (#80, #81), D08 read model
(#83) and browser integration (#93) have merged. What remains is installed
quickstart and rollback rehearsal evidence, and a production composition root:
no CLI command constructs a `LongitudinalExplorerSource` today, so
`traceback reader launch` serves no E12 route. E14 may automate renderer checks afterward;
observed keyboard, screen-reader, 200% zoom, approved-host performance, and
five-provider evidence remain separate gates.
