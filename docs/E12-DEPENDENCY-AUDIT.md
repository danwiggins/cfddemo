# E12 dependency audit

Audit baseline: `origin/main` at `cd988a795a4eb2f3237da4f0a1c6d239701a146d`.
This is an implementation inventory, not evidence that Epic D's provider,
scientific, governance, or usability gates have passed.

## Merged foundations

| Dependency | What is reusable | What it does not supply for E12 |
| --- | --- | --- |
| E01 method registry | Exact method definitions, append-only method authority, current authority-head replay | Provider identity authority, distinct human principals, subject/draw linkage |
| E04 result catalog | Verified immutable bundle indexing, protected opaque selectors, deterministic paging | Biological lineage, linkage correction history, comparison membership |
| E05 compatibility | Exact anchor-based pair decisions; invalid or unknown decisions forbid deltas/shared axes | Epic D's six-outcome longitudinal policy, provider linkage, cohort membership |
| E06 result filters | Independent state axes and reconciled denominator ledgers | Cohort/timepoint semantics, replicate policy, immutable membership |
| E10 provenance drawer | Exact result/method/asset/count/filter/limitation lineage and replay | Protected identity data or permission to connect records into a series |

The foundations are compatible but insufficient. E04 aliases cannot be promoted
into subject identity, and E05 pairwise compatibility cannot establish a
longitudinal series. E12 must consume protected provider-local linkage and an
immutable comparison anchor rather than infer either from aliases, dates,
method labels, or adjacency.

## Delivery sequence

1. **D01 linkage/authority foundation (this PR):** closed provider-local
   biological and technical lineage contracts, independently pinned provider
   trust, exact signed approvals, dual-principal correction, append-only
   history validation, and fail-closed activation/comparison eligibility. D01
   deliberately cannot consume approvals or project active linkage.
2. **D01/D04 protected transactional store (stack base):** atomically consume approval IDs
   and nonces with immutable revisions, preserve full-history identity and
   parent constraints, and expose active linkage only from committed state.
3. **D02 compatibility-key/anchor (this PR):** complete Epic D measurement
   policy key and six outcomes. Every member is evaluated against one pinned
   anchor; `unknown` and invalid decisions suppress deltas and connecting
   trends.
4. **D04/D05 supersession and immutable membership:** correction/supersession
   chain, cycle/idempotency/concurrency protection,
   immutable member ordering, unit-of-analysis and technical-replicate rules.
   D05 v2 now derives biological timepoints from independently pinned
   collection-event authority rather than linkage proposal time. A protected
   durable cohort-version registry and authorized browser alias projection
   remain an explicit persistence follow-up; immutable bytes alone do not make
   a cohort discoverable or published.
5. **D09 denominator/missingness:** bind declared denominator, inclusion,
   missingness and unavailable-record policy to the comparison identity from
   D05/D06 authority only. E06 ledger reconciliation and E05/D07 comparison
   eligibility move to E12's protected result-view and D03/D07 registries.
6. **E12 integration:** add protected cohort/timepoint selectors and a pure read
   model that separates biological collections from technical reruns. It emits
   no delta or connected trend unless linkage, membership, compatibility and
   denominator gates all pass.

External provider approvals and scientific qualification remain inputs. No
fixture, signature, test result, or locally generated key is allowed to claim
those gates passed.
