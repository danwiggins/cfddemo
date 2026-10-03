# Longitudinal browser integration (E12 routes, view, Save and Reopen)

Status: local, synthetic, release-disabled. This is the browser half of
`docs/E12-INTEGRATION-PLAN.md` ("Browser integration", "Normative D08
selection and persistence journey", "Normative D08 states and responsive
behavior"). It authorizes no release, export, provider operation or clinical
interpretation. Saving never authorizes export.

Code: `traceback_runner/web/longitudinal.py` (routes, adapter, Save/Reopen),
`traceback_runner/web/static/longitudinal.js` (view), the longitudinal section
of `static/index.html` and `static/styles.css`. Tests:
`tests/web/test_longitudinal_browser.py`, `tests/web/test_longitudinal_routes.py`
(shared helpers `tests/web/longitudinal_env.py`, DOM harness
`tests/web/longitudinal_dom_harness.js`).

## Threat model

The process/OS-user boundary is the trust boundary. In-process code mutation
and same-user filesystem races are out of scope. Pins and type checks detect
accidental or naive class and instance replacement only. In scope: browser
input, sessions without a current grant, grant revocation/expiry/scope, and
authority that moves while a route runs.

## Installation

`LongitudinalExplorerSource(stores=..., measurement_scopes=..., comparison_registry=...)`
takes the exact 15-store set the D08 builder needs (exact classes, wired to
each other: it constructs a `CompositeAuthorityCoordinator`, which checks the
wiring), 1..8 operator-configured D02 measurement scopes the selector step
offers, and the optional installed `LongitudinalComparisonRegistry`. It is the
one optional adapter of `IntegratedExplorerSource(..., longitudinal=...)` and
must be bound to that explorer's own E04 catalog. `RunningLocalWebService.start`
refuses a longitudinal adapter unless `reader_registry` is the adapter's own
reader registry. Without the adapter every E12 route answers not found.

## Routes

All under `/api/v1/longitudinal/`. GET routes take a bounded query
(`max_num_fields=12`); POST routes take a JSON body of at most 4,096 bytes and
need the B01 Origin and CSRF checks.

| Route | Input | Output |
| --- | --- | --- |
| `GET selectors` | none, or cohort selector/version + D02 scope, optionally + anchor-policy selector/version | `LongitudinalSelectorCatalog`: cohort versions (bounded page); for one version, the measurement options (projection policies bound to the manifest's measurement anchor and the scope), anchor-policy approvals and D09 policies for that manifest; the explicit anchor candidate page |
| `GET diff` | cohort selector/version + D02 scope | `LongitudinalVersionDiffResponse` (D08 `derive_version_diff`: counts, set digests, controlled reasons) |
| `GET saved` | optional cursor | `LongitudinalSavedPage`: opaque saved selectors with `current`/`stale` and stale slots; a grant covering only some configured scopes gets an empty page (`listing_state=requires_every_configured_scope`), no grant the denial shell |
| `POST workspace` | `{"request": LongitudinalWorkspaceRequest}` | `LongitudinalWorkspaceResponse`: the D08 `LongitudinalWorkspaceProjection`, Save availability, literal disabled release/export |
| `POST source` | `{"request", "row_ordinal"}` | `LongitudinalSourceDetail`: one row the request's filters make visible, the visible segments that touch it, authority digests, a fresh replay digest |
| `POST save` | `{"request"}` | `PublicSaveReceipt` (below) |
| `POST reopen` | `{"saved_selector_id", "comparison_version", "stage": "diff" \| "results"}` | `LongitudinalReopenResponse` (below) |

Every route renders only closed public models. `validate_longitudinal_public`
runs on every payload: controlled key names only, no protected field name
(member, linkage, reader authorization, dependency heads, saved-object bytes,
grant commitment and others) at any depth, and every string either a typed
64-hex digest or passes the shared public-text validator (paths, URIs,
credentials, sequences).

### Reader authorization

1. B01 Host/session (and for POST Origin/CSRF) checks run first and keep their
   own errors.
2. Before any route runs or parses its input, `handle_longitudinal_route`
   builds one `_AuthorizedView`: `configured` (the operator's scopes) and
   `granted` (`configured` intersected with the session's own grant, each
   checked through `ReaderSessionBinder.reader_authorization`); an empty
   `granted` denies. Routes read authority only through `view.gate(scope)`
   (deny unless the scope is in `granted`, then the live reader gate) and
   `view.saved_gate()` (deny unless `granted == configured`); no route
   derives scope itself. A missing binding, missing/forged/revoked/expired/wrong-scope grant,
   untrusted key, stale bound head, or an unavailable/replaced registry all
   answer exactly `403 {"error":{"code":"permission_denied"}}`: no counts,
   selectors or detail. Only the lock-free E06 identity binding (the cohort
   registry the grant must cover) is read before the gate. A requested
   measurement that is not one of the operator-configured scopes gets the
   same denial even when the grant covers it.
3. Selector and diff reads run while the gate's reader fence is held (the
   reader registry is first in the global lock order).
4. Workspace, source detail, save and reopen release the gate and call
   `build_longitudinal_workspace` with the session's sealed binding
   (`ReaderSessionBinder.session_credential`); the builder re-authorizes under
   its own fence before and after its composite snapshots.
5. Before returning, the route re-enters the gate and requires the same grant
   and scope. A revocation that lands before this check yields the denial
   shell and no payload. The response is a point-in-time record, not a lease.

Malformed transport input (non-JSON body, unparseable query) is rejected by the
server with `TBX-WEB-400` before the reader gate; it reveals no record state.

### Errors

D08 boundary codes map to `400 invalid_request`, `403 permission_denied`,
`409 authority_stale | trust_revoked | read_conflict`, `500 integrity_failure`,
`503 storage_failure`, each with its controlled remediation. Save also has
`409 save_unavailable` with the Save state as remediation. Unknown saved
selectors answer like a denial (no existence oracle).

## Save

1. Gate, then require Save availability: `registry_absent` (no registry),
   `registry_unhealthy` (identity read fails: closed, corrupt, rolled back or
   stale root), `registry_full` (1,000 objects).
2. Build the workspace from live authority.
3. `SavedLongitudinalComparisonV1` from the workspace: the canonical selection
   (filters must map onto D03 outcomes; `anchor`/`not_evaluated` filters cannot
   be saved), the family projection request derived from the resolved
   projection policy (bound to the workspace's policy digest and projection
   head), commitments (`saved_commitments`: manifest, D03 policy, D07 envelope,
   anchor record, D03 series, per-row D07 comparisons, D09 summary, D10
   context, D04 history, E06 sources, and the reader-grant commitment), the
   E06 registry head and source selectors, `dependency_heads =
   workspace.dependency_heads`, the workspace replay digest and a whole-second
   UTC creation time.
4. Exact retry: if the latest committed version of this selection already has
   this replay digest, head vector and selection, its exact bytes are
   republished and the registry returns its idempotent receipt
   (`applied=false`). Otherwise the next version is published.
5. `LongitudinalComparisonRegistry.register(saved,
   dependency_fence=CompositeAuthorityFence(coordinator))`. The registry
   requires the held head vector to equal the saved vector (so equal to
   `workspace.dependency_heads`) and rechecks it after commit. The route
   requires the receipt to carry the same vector and
   `composite_authority_fence`.
6. Final fence: one more composite hold re-reads the vector (it must still
   equal the receipt's) and re-authorizes the reader inside the hold
   (`ReaderSessionBinder.reader_authorization_in_held_fence`; the composite
   holds the reader fence). Only then is `PublicSaveReceipt` built: saved
   selector, version, object digest, registry state version, replay digest,
   `applied`, fence kind, `final_fence_passed=true`,
   `saving_authorizes_export=false`.

If the final fence fails the object stays committed (it reopens stale) but no
receipt is shown: `authority_stale` or `permission_denied`.

## Reopen

`stage=diff` and `stage=results` run the same steps; only `results` returns
rows, so the view always shows the diff first.

1. Gate for every configured scope (the saved page and Reopen are only for a
   reader whose own grant covers all of them, so no saved selector or object
   is read under a grant for another measurement), resolve the opaque
   selector through `registry.resolve` under a `CompositeAuthorityFence`,
   require the saved scope to be a configured one, and gate again for it.
2. Under that gate: re-derive the live anchor candidate page for the saved
   anchor-policy approval (its digest is the request's anchor version), the
   D05 version diff of the saved version, and the selector's versions.
3. Rebuild with exactly the saved selectors and versions; nothing is advanced.
4. Diff: registry state and stale slots, newer cohort version (shown, never
   applied), changed commitments (`CommitmentChange`), rebuild error, and
   `comparison_state`. `current` only when the registry reports current, no
   commitment or head changed, the publication was composite-fenced, and the
   rebuilt replay digest equals the saved one.
5. Results: `current` returns the rebuilt projection only after a final
   composite hold re-reads the head vector (it must equal the saved one) and
   re-authorizes the reader inside the hold; otherwise `authority_stale` or
   the denial shell and no result. `stale` returns only the saved object's
   immutable commitments (`historical_commitments`, without the reader-grant
   commitment): the saved bytes hold no rows, and rows rebuilt from current
   authority would relabel current state as the saved comparison. No values,
   comparison numbers or segments (`stale_segments=[]`), plus the refresh
   action `start_new_comparison_at_current_authority`.

Saved bytes are never rewritten; the response carries their digest only.

## View

The longitudinal section appears after a reader launch is exchanged
(`app.js` dispatches `traceback:longitudinal` with the CSRF token, kept in
page memory). Journey: cohort version → authorized measurement scope (the
configured scopes the grant covers) → measurement → anchor-policy approval
→ explicit anchor candidate (no default) → D09 policy → filters → "Show
version diff" → "Show results" (enabled only for the exact selection whose
diff was shown).

Filters are the D08 `LongitudinalWorkspaceFilters` axes: lineage role,
record availability, compatibility outcome (including `anchor` and
`not_evaluated`, which cannot be saved) and public timepoint ordinals. The
D08 filter contract carries no E06 state axes, so the view offers none.

Any denial, error or new reopen first clears every result surface (identity,
outcomes, counts, table, chart, covariates, drawer, receipt) so nothing from an
earlier result survives beside it. Returning to the tab refetches the last
workspace or reopened result as a fresh revision.

Result order (HTML and DOM): identity/version/authority; outcome, reasons and
permitted next action; denominator strip (D09 counts, "not a comparison gate");
the native source table (caption, `scope="col"` headers, `scope="row"` row
headers, a keyboard-reachable overflow region); the optional SVG chart; the
aggregate covariate panel ("operator-entered, unverified") and the provenance
drawer (E06 ledger digest labelled "operator-entered, unverified"). E08/E09
policies show standalone values "unavailable" with the prerequisite named.
Release and export buttons are literally `disabled`.

Chart: x is linear in the signed-seconds offset (unequal intervals stay
unequal); y is the D07 member value (anchor value for the anchor) with its
uncertainty interval. Lines come only from `LongitudinalSegment`: consecutive
segments form one series, each series is its own `<path>`, and points with no
segment are square "unconnected" markers. No segment, no line.

States (`data-state` on the section, always also written as text): `loading`
(stage + elapsed seconds, no percentage, `aria-busy`), `slow-stage` (after 2 s;
navigation away aborts the request; returning refetches a fresh revision),
`empty` (no visible rows, or fewer than two comparable draws; counts kept),
`error` (code, problem, remediation, Retry repeats the whole step),
`success`, `partial`, `stale` (reopen; comparisons and segments suppressed),
`revoked` (a `result_key_revoked` withheld row), `permission-denied` (fixed
text, no counts, no Retry).

Drawer: ≥64rem beside the table (sticky); 42–64rem overlaid at the right with
the main column padded so the focused row stays visible; <42rem a full-width
modal sheet (`aria-modal=true`) with Tab/Shift+Tab containment, Escape to
close, and focus restored to the row's Details button.

Accessibility: text and indicator colours in the `.lg` rules are at least
4.5:1 (tested), controls and checkbox labels are at least 44×44 CSS px,
`prefers-reduced-motion` disables transitions, and layouts use
`minmax(0, 1fr)` grids with no fixed pixel widths. Observed keyboard,
screen-reader and 200% zoom audits remain E14 evidence.

## Plan test items

- 12: `test_longitudinal_browser.py` (denial before any read for bare B01
  sessions, injected roles, wrong scope; selectors/diff/workspace/source;
  privacy; DOM harness: nine states, table semantics, separate SVG paths,
  unequal spacing, drawer modes, focus containment, controller journey at
  1440/800/375 px) and `test_longitudinal_routes.py` (revoked, expired, stale
  bound head, registry replacement, revocation during final return; static
  order, contrast, targets, reduced motion, reflow, no external requests,
  disabled release controls).
- 17: save idempotence and immutable bytes, current reopen, stale reopen after
  authority advance, tamper and root swap.
- 18: version diff route, cohort advance diffed and never applied on reopen,
  diff stage before results in the routes and the view.
- 19: six outcomes with their exact actions in the view; no route accepts an
  action, decision or segment; bridges stay `not_executed`.
- 21 (route half): Save unavailable when the registry is absent, corrupt or
  full; receipt only after the final fence.

## Open items

- `traceback reader launch` starts the service with the reader registry only;
  it does not yet open the 15 stores and the saved registry, so the installed
  quickstart (`docs/quickstarts/LONGITUDINAL-SYNTHETIC.md`) and the rollback
  rehearsal (`docs/rollback/LONGITUDINAL-REHEARSAL.md`) still need a packaged
  store-installation command and its pinned identities.
- The selector step authorizes per configured scope; the saved page and Reopen
  require a grant covering every configured scope (with one configured scope,
  the default, this is the same grant). Finer per-scope listing needs a
  registry read that maps selectors to scopes without opening objects (Reopen
  enforces the saved object's own scope).
- Each workspace, source-detail, save and reopen request rebuilds the
  workspace (about 5 s on the synthetic world); there is no cache.
- Browser-level evidence (real Chromium, screen reader, 200% zoom) is not
  produced here; the DOM harness checks the packaged script's behaviour only.
- Follow-up (structural review): restructure the client to render from one
  state object instead of mutating panels per step; the current code clears
  every surface on denial, error and reopen, which is correct but fragile.
- Follow-up (S2): `GET saved` lists rows whose saved scope is no longer
  configured; only Reopen refuses them. Filtering needs a registry read that
  maps a selector to its scope without opening the object.
- Product decision needed: the saved commitments include
  `reader_grant_sha256`, so a reopen under any other grant (another reader,
  or a re-issued grant) reports `reader_grant_changed` and is stale. With
  more than one reader a current reopen is practically unreachable; decide
  whether the grant belongs in the commitments or only in the publication
  record.
