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
   revision projection, and fail-closed comparison-linkage eligibility.
2. **D02 compatibility-key/anchor:** add the complete Epic D measurement policy
   key and six outcomes. Every member is evaluated against one pinned anchor;
   `unknown` and invalid decisions suppress deltas and connecting trends.
3. **D04/D05 ledger and immutable membership:** durable no-follow local store,
   correction/supersession chain, cycle/idempotency/concurrency protection,
   immutable member ordering, unit-of-analysis and technical-replicate rules.
4. **D09 denominator/missingness:** bind declared denominator, inclusion,
   missingness and unavailable-record policy to the comparison identity and
   reconcile it with E06 ledgers.
5. **E12 integration:** add protected cohort/timepoint selectors and a pure read
   model that separates biological collections from technical reruns. It emits
   no delta or connected trend unless linkage, membership, compatibility and
   denominator gates all pass.

External provider approvals and scientific qualification remain inputs. No
fixture, signature, test result, or locally generated key is allowed to claim
those gates passed.
