# Longitudinal workspace (D08 read model)

Status: local, synthetic, release-disabled. This is the protected D08 read
model and its public projection from `docs/E12-INTEGRATION-PLAN.md`
("Required implementation cut"). It authorizes no release, export, provider
operation or clinical interpretation. Save/Reopen, browser routes and HTML are
later PRs.

Code: `evidence_inspector/longitudinal_workspace.py`. Tests:
`tests/test_longitudinal_workspace.py` (world:
`tests/longitudinal_workspace_world.py`).

## Threat model

The process/OS-user boundary is the trust boundary, as for every merged E12
store. In-process code mutation and same-user filesystem races are out of
scope. Pins and type checks detect accidental or naive class and instance
replacement only. In scope: caller-supplied inputs, concurrent readers and
writers across threads and processes, and authority that moves while a build
runs.

## Entry point

```python
build_longitudinal_workspace(
    request,                       # LongitudinalWorkspaceRequest
    *,
    reader_authorization_registry,
    reader_session_credential,     # ReaderGrantBinding sealed in the B01 session
    linkage_store, cohort_registry, cohort_record_catalog,
    result_catalog, result_trust_registry, supersession_store,
    anchor_policy_registry, projection_policy_registry,
    result_view_source_registry, measurement_source_artifact_registry,
    d03_decision_registry, d07_comparison_registry,
    d09_summary_registry, d10_context_registry,
) -> LongitudinalWorkspace
```

Deviation from the plan's signature: `linkage_store`, `result_catalog` and
`result_trust_registry` are added. They are never read directly; they complete
the exact store set `CompositeAuthorityCoordinator` requires.

`project_longitudinal_workspace(workspace)` returns the public
`LongitudinalWorkspaceProjection`; `longitudinal_projection_bytes` its
canonical bytes.

### Request

`LongitudinalWorkspaceRequest` carries only opaque selectors and versions:
cohort selector/version, anchor-policy selector/approval version,
approved-anchor selector plus its version (the digest of the live candidate
page the operator chose from), projection-policy selector/version, D09 policy
selector/version, the requested D02 measurement (family, quantity, unit,
measurement-definition digest) and normalized filters. It has no field for a
manifest, status, source, decision, comparison, policy, anchor record,
coordinate, value, role, grant or release flag. The request is captured as
exact bounded canonical bytes (exact type, no extra/private state, bounded
graph) and re-parsed before any authority operation; filter order is
canonicalized. The session credential is a `ReaderGrantBinding` (grant
commitment plus bound registry head) held server-side; it never enters the
replay digest or public bytes.

## Authority model

Every authority comes from the merged registries' public live-replaying reads:

| Input | Store and read |
| --- | --- |
| Reader | `ReaderAuthorizationRegistry.authority_read_fence` + `authorize_reader_in_fence` (scope: cohort registry and D02 family/quantity/unit) |
| Cohort bound | `CohortRegistry.list_selectors` (one row, `member_count`) |
| Cohort | `CohortRegistry.resolve_history` (current registered history) |
| Record status | `CohortRecordCatalog.record_status_for_manifest` |
| E04 rows | only through D06 status bindings |
| History | `RecordSupersessionStore.record_history_snapshot` (all pages, limit 1,000) |
| Anchor | `AnchorPolicyRegistry.resolve_anchor` (page digest from the request) |
| Projection | `ProjectionPolicyRegistry.resolve` |
| Sources | `ResultViewSourceRegistry.list_selectors` / `selector_for_member` / `resolve` |
| Values | `MeasurementSourceArtifactRegistry.list_selectors` / `selector_for_e06_source` / `resolve`, then `project_source_values` |
| D03 | `LongitudinalDecisionRegistry.list_selectors` / `resolve` |
| D07 | `RepeatabilityComparisonRegistry.list_selectors` / `resolve` |
| D09 | `DenominatorPolicyRegistry.resolve` |
| D10 | `CovariateContextRegistry.list_selectors` / `resolve` |

Every store must be the exact merged class; every method is a function
captured at import and re-checked against the class and the instance before
each call.

### Bracketed composite snapshot

The composite coordinator holds every fence in the global order but runs no
caller code inside the hold, and the stores' public reads cannot run inside it.
The builder therefore brackets the reads with two composite snapshots:

1. capture request and credential;
2. authorize the reader (`A1`), before any other protected read;
3. read the bounded D05 selector row; more than 1,000 members is rejected here,
   before any snapshot or per-member traversal;
4. `H1 = coordinator.snapshot(scope)`: every store's ID/epoch/head plus the
   scoped D06 status head, captured and revalidated under the full fence set;
5. run the public reads; every head a read returns must equal its `H1` head
   (D05, D06 status digest, D01 linkage head inside D06 and D04, E06, family,
   D03, D07 and the trust head it binds, D09, D10, anchor, projection, reader);
6. `H2 = coordinator.snapshot(scope)` and re-authorize (`A2`);
7. require `H2 == H1` and `A2 == A1` (except `evaluated_at`), then derive.

Chained heads (append-only journals with rollback detection) are covered by
`H2 == H1`: no committed mutation of those stores landed between the two
holds. Two heads are content digests rather than chains: the D06 scoped head
(the status digest of the selected cohort version, derived from E04 rows that
can be removed and re-added) and the E04 head, which since #81 includes a
digest of every committed catalog row under the cross-process content lock. A
remove-and-re-add between the holds can restore an equal digest, so every
D06-derived read (D06 status, E06 sources, family artifacts, D09 summary, D10
context) is also bound to the `H1` status digest. A read that observed the
intermediate state either fails that binding (`read_conflict`) or fails its
own replay and surfaces as `authority_stale`; a retry resolves it. If any read fails, the
builder re-snapshots: if authority moved, the failure is reported as
`read_conflict`, never as stale or scientific unavailability. The workspace is
a point-in-time record of `H1`, not a lease.

Known limits of this protocol:

- E04 catalog rows are fenced by the composite since #81 (content lock plus
  an E04 head over row content). D08 still consumes E04 rows only through D06
  bindings, which D06 re-verifies against E04 inside its own fence; the
  content-digest ABA case is described above.
- Time-dependent state (reader grant expiry, D07 envelope validity) is
  evaluated at each read. D07 `replayed_at` is the as-of time of a comparison;
  it stays in the protected row (an absolute timestamp never reaches public
  bytes).
- The family adapter, request capture and derivation run outside every store
  fence, between the two snapshots.

## Rows

One row per D05 manifest member, in canonical manifest order.
`ProtectedLongitudinalRow` keeps the exact `CohortMember`, member commitment,
D06 member status and binding, D04 history state, E06 source commitments,
family projection, D03 member decision fields and D07 comparison fields. It
never crosses the HTTP boundary. `LongitudinalSourceRow` is the public row.

### Biological timepoint versus rerun

Public timepoints are the distinct `(time_coordinate, biological_timepoint_id)`
pairs of the manifest, in order, with 1-based ordinals and a signed offset in
seconds from the first biological coordinate (the same derivation as the
anchor-policy candidate page). Technical replicates and reanalyses share their
source collection's handle and coordinate, so they stay source rows at the same
timepoint and never create a timepoint. Unequal intervals keep unequal
offsets. Only a manifest denominator contributor counts as a biological unit
(`denominator_contributor`). The public axis carries `kind`, `unit=seconds`,
the axis `definition_sha256`, coordinate semantics
(`absolute_collection_time`, `subject_relative`, `study_relative`) and
`offset_origin=first_biological_coordinate`; never a timestamp, coordinate or
timepoint handle.

### Record availability and history

`available`, `missing` and `withheld` come from D06 bindings. A missing or
withheld row carries no result commitment, method, source, value or
comparison (structurally enforced). A stable verified key revocation is a
`withheld` row, not an error. D04 state is `active`, `superseded`,
`authority_invalid`, `not_recorded` or `binding_mismatch` (record matched by
provider, analysis record and result, then checked against the member's
linkage revision and the binding's bundle). Superseded rows and rows with
affected D04 comparisons carry `affected_comparison_warning`.

### Measurement-family projections

The projection policy is resolved from `ProjectionPolicyRegistry` and must bind
the D05 measurement anchor and the requested D02 tuple. In this cut only E07
fragment artifacts exist (family-source registry). For each available row with
a verified E06 source and a registered E07 artifact, `project_source_values`
runs and the public row carries family, statistic, unit, panel, bin index and
half-open bounds, the exact count or rational fraction, and artifact/chart
digests. Adapter rejections are `projection_rejected` with a controlled
reason; an unregistered artifact is `artifact_not_registered`. An E08 or E09
policy yields `family_prerequisite_missing` naming
`e08_cell_origin_artifact_binding` or `e09_cna_artifact_binding` and no value.
D07 observations never substitute for a source value.

E06 denominator ledgers are operator-entered: the public row carries only the
ledger digest, always labelled `operator-entered, unverified`, never counts.

### Six compatibility outcomes and actions

The row copies the exact D03 member decision for the pinned anchor: outcome,
reason codes, mismatch and unknown dimensions, next action, bridge reference
count and `bridge_execution_state=not_executed`.

| Outcome | Action | Rendering |
| --- | --- | --- |
| `equivalent` | `use_direct_comparison` | D07 numbers only after every other gate passes |
| `qualified_compatible` | `use_qualified_comparison` | same |
| `requires_reanalysis` | `request_reanalysis` | separate source series, no D07 numbers or segment |
| `registered_bridge` | `review_registered_bridge` | separate series; bridge never executed |
| `incompatible` | `start_separate_series` | separate series with exact mismatches |
| `unknown` | `resolve_unknown_inputs` | no comparison; exact unknown dimensions |

The anchor row is `anchor`; members the D03 series does not decide are
`not_evaluated`. The D03 series is found by policy digest, anchor key and the
resolved anchor record; zero or several matches leave every member
`not_evaluated` with a series state. A not-current series with that policy and
anchor key, with no current match, is `authority_stale` (it cannot be proven
unrelated to this anchor).

### Comparisons and segments

D07 numbers (anchor/member values, both uncertainty intervals, both
comparison denominators, delta, maximum absolute delta, classification) appear
only when every gate passes for that exact comparison
(`comparison_suppression`):

- D06: member and anchor rows are `available`;
- D04: member and anchor histories are `active`;
- D03: decided, outcome `equivalent` or `qualified_compatible`, `delta_allowed`,
  member and anchor D01 receipts equal the D05 members' committed receipts,
  the decision binds the resolved policy and anchor record, and its member
  result is the D06 binding's result;
- D07: exactly one current registered comparison for that decision, under the
  resolved policy and the resolved anchor envelope digest, replayed `available`
  and bound to the same decision, member record, policy, anchor record and
  envelope. An expired envelope surfaces as D07 unavailable.

D09 is never a gate. Otherwise the row is `suppressed` with every failed gate
named. `LongitudinalSegment` exists only between adjacent public timepoints
whose single biological-draw row is the anchor or carries an available
comparison; a timepoint with an ineligible or a second draw breaks the series,
technical rows are never endpoints, and each segment names the D07
comparison digest(s) and deltas that authorize it. The workspace contract
rejects any segment that does not match its endpoint rows.

## Denominators and missingness

Population counts come only from the requested registered D09 v3 summary
(`CohortPopulationProjection`): declared, included, excluded and unavailable
members and denominator units, and the summary state. The builder checks the
summary binds the selected cohort version, manifest, D05 head and D06 status
digest, and that declared counts equal the manifest. Rows carry lineage,
missing and withheld from D05/D06, not from D09. Filters never change counts.

## Covariate context

D10 is found by the D09 selector/version, the D03 series selector and the D03
policy digest; the public context is the aggregate projection
(classification, reasons, group states and counts), labelled
`operator-entered, unverified`, with `values_changed`, `eligibility_changed`
and `biological_attribution_allowed` literally false. Absent, ambiguous or
stale contexts are named states.

D10's live build requires every D09-included member to have a D03 member
decision, and a D03 series never decides its own anchor, so a D10 context
exists only when the anchor member is outside the D09 included set (the test
world excludes it by D09 policy). This is an upstream constraint, recorded as
an open item.

## Filters and replay

Filters (timepoint ordinals, lineage roles, record availability,
compatibility states) select visible rows after construction. The projection
keeps `total_row_count`, the full population and only segments whose
endpoints are both visible. `filters_sha256` binds the normalized filters.
`replay_sha256` binds the request, every authority commitment (the public
head vector omits the reader-registry head, which is normally the session's
bound head), the time axis,
population, covariate context, version diff, anchor record digest, every
protected and public row and every segment; the reader authorization is
excluded, and each protected row's D07 `replayed_at` is replaced by a fixed
placeholder before hashing (it is the live authority clock at the read, so it
would change the digest of unchanged authority).

## Version diff

`derive_version_diff` compares the selected manifest with its predecessor from
the same registered history: added/removed/unchanged member counts, set
digests, and controlled reasons for membership, inclusion, exclusion,
missingness, unit of analysis, replicate and reanalysis rules, time axis,
measurement anchor and provider authority changes. D03 policy change is
`not_comparable` (it needs the prior saved comparison; Reopen is later).
`silent_upgrade` is literally false.

## Privacy boundary

The public projection's schema has no protected-row, member, reader or
credential field. Public rows use a domain-separated digest alias `source_<16 hex>`, ordinals,
offsets, controlled states, method references and digests of results,
sources, decisions and comparisons. Tests seed provider, subject, collection,
specimen, run, analysis, linkage, result, bundle, receipt, timepoint, path and
time-coordinate values and check that none of them, or their upper-case, hex
or base64 forms, appear in canonical public bytes.

## Failure codes

`LongitudinalWorkspaceBoundaryError` carries one code and one remediation and
is raised outside any `except` block, so it has no cause, context or nested
text.

| Code | When |
| --- | --- |
| `invalid_request` | request capture fails; unknown/invalid selector; anchor, projection or D09 policy for another cohort or measurement; cohort over 1,000 members |
| `permission_denied` | missing, forged, revoked, expired, wrong-scope or stale-head grant; registry unavailable |
| `authority_stale` | a stable store read reports not-current authority (cohort, anchor page, D09 policy, a member's latest E06 source or its E07 artifact, an unmatched D03 series) |
| `trust_revoked` | trust authority failure raised by a read |
| `integrity_failure` | wrong store type, class/instance shadow, store `Unsafe`, incoherent results |
| `storage_failure` | filesystem errors |
| `read_conflict` | a read does not bind `H1`, `H2 != H1`, or any read failed after authority moved |

Stable verified `missing`, `withheld`, `incompatible`, `unknown` and D07
`unavailable` states remain ordinary row states. Two not-current states stay
row/context states by decision of the plan: a stale D07 comparison (the plan
lists "stale comparison states" among the suppressed D07 states, and an
expired envelope is D07 `unavailable`), and a stale D10 context (context only;
it never changes values or eligibility, and a stale context cannot be proven
to belong to this workspace). Known limit: a stale D10 page row skips the
exact D09/D06/D03 bindings, so a D06 remove-and-re-add between the snapshots
can show a transient `stale` covariate context instead of `read_conflict`;
values, counts, gates and segments are unaffected, and a rebuild shows the
current context.

## Limitations

Every workspace lists its limitation codes and the exact statement:
"Comparisons are descriptive technical differences with no causal or clinical
interpretation." D07 values carry
`interpretation=descriptive_technical_difference_only_no_causal_or_clinical_meaning`.
Release, export and diagnostic interpretation fields are literally false.

- One measurement and one pinned anchor per workspace; 1,000 members maximum.
- Standalone values: E07 fragment only. E08 needs cell-origin bundle import
  and verification; E09 needs a binding to E04/E06 results.
- E06 cannot verify capability against a current method-authority head
  (`method_authority_head_not_current_verified`).

## Saved comparison (not wired)

`LongitudinalComparisonRegistry` exists and accepts only
`composite_authority_fence` publications. Wiring Save needs:

- a `SavedLongitudinalComparisonV1` built from `LongitudinalWorkspace`:
  normalized request, projection request, authority commitments and replay
  digest, plus the reader grant commitment from `reader_authorization`;
- publication through `CompositeAuthorityFence(coordinator)` with a dependency
  scope of the workspace's cohort selector/version, requiring the
  publication's head vector to equal `workspace.dependency_heads` (the
  protected `H1` vector; the public `authority.heads` omits the reader-registry
  head), so a save never records heads the workspace was not built from;
- reader re-authorization under the same final fences as publication (the
  composite hold includes the reader fence, but the registry exposes no
  in-hold authorize call for the saved-registry path);
- Reopen: resolve the saved selector, show `derive_version_diff` against the
  current version, rebuild with `build_longitudinal_workspace` and compare
  replay digests; a different digest is `stale` with no current segments.

## Open items

- A real exactly-1,000-member registered cohort is not built in tests: D01
  linkage commits are O(n) each (200 commits took about 110 s), so the bound is
  tested by doctoring the selector row (1,001 rejected before any snapshot,
  1,000 passes) and by deriving an exact 1,000-row workspace.
- Builds re-resolve E06 and family sources per member, and each E06/family
  resolve recomputes the D06 status: O(n^2) at 1,000 members. Fine at
  synthetic scale; needs a batched in-fence read.
- D10 requires the anchor to be outside the D09 included set (above).
- D05 committed receipts (current at manifest time) and D04 activation
  receipts differ; D08 matches D04 records by linkage revision, not receipt.
