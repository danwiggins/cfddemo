# D03 longitudinal compatibility decision engine

`evidence_inspector.longitudinal_compatibility` remains the single evaluator
for D02 and D03. D03 extends the pinned-anchor decision with exact explanations,
controlled safe actions, and replay; it does not introduce a second comparison
algorithm.

Current member and series decisions use v3 schema identities and bind the exact
retained linkage snapshot. Historical v2 envelopes remain parseable through
explicit legacy models but cannot satisfy current replay.
Policy, member-decision, and series-decision `schema_version` fields are
required on the wire. `bridge_execution_state` is also required; deserialization
never supplies these authority-bearing fields from defaults.

## Requirement matrix

| Requirement | D02 authority reused | D03 addition |
|---|---|---|
| Exact mismatch explanation and safe next action | mismatch and unknown dimension sets | one cryptographic explanation per dimension and one outcome-derived action |
| Exact-match default | unlisted digest transitions are incompatible | adversarial retention tests |
| Old or missing policy fields | closed versioned policy schema | explicit rejection tests; no legacy fallback |
| Six outcomes | complete D02 outcome enum and evaluator | one exact safe action for every outcome |
| One pinned series anchor | every member evaluated against the anchor | explicit nontransitivity regression |
| Bridge explicit and not implicit | registered bridge references | immutable `not_executed` state and review-only action |
| Delta and trend eligibility | equivalent and qualified-compatible only | all-six-outcome regression |
| E05, E01, and lineage replay | exact records, capabilities, activated linkage receipts, policy and pins | replay helper plus live protected-store verification |

## Exact dimension explanations

Every decision contains all 13 dimensions in canonical order. Each explanation
binds:

- the dimension;
- anchor and member known/unknown states;
- canonical anchor and member value digests;
- exact match, unknown, disallowed mismatch, qualified envelope, required
  reanalysis, or registered bridge disposition;
- the registered evidence reference and digest when an allowance exists; and
- the bridge reference only for a registered-bridge disposition.

The decision's mismatch, unknown, evidence, and bridge summaries must be exactly
derivable from those explanations. They cannot be edited independently.

## Controlled next actions

| Outcome | Safe next action | Delta/trend |
|---|---|---:|
| `equivalent` | `use_direct_comparison` | allowed |
| `qualified_compatible` | `use_qualified_comparison` | allowed |
| `requires_reanalysis` | `request_reanalysis` | blocked |
| `registered_bridge` | `review_registered_bridge` | blocked |
| `incompatible` | `start_separate_series` | blocked |
| `unknown` | `resolve_unknown_inputs` | blocked |

Actions are controlled workflow states, not commands. A bridge decision always
records `bridge_execution_state=not_executed`; the engine does not run a bridge,
translate a value, start reanalysis, or mutate a record.

## Exact reason sets

Reason codes cannot be mixed across outcomes:

- `equivalent`: exactly `exact_match`;
- `qualified_compatible`: exactly `qualified_envelope`;
- `requires_reanalysis`: exactly `reanalysis_required`;
- `registered_bridge`: exactly `bridge_available`;
- `incompatible`: exactly one of `disallowed_mismatch`, `mixed_dispositions`,
  or `subject_linkage_mismatch`; and
- `unknown`: a nonempty subset of the controlled unknown-state reasons only.

The unknown-state reasons are `unknown_dimension`, `linkage_authority_invalid`,
`policy_identity_invalid`, `anchor_identity_invalid`, and
`result_state_invalid`. Compatibility reasons cannot be added to an unknown
decision.

## Default, anchor, and replay rules

An exact complete-key match remains the only default-compatible path. A policy
must use the current closed schema and contain every required field and every
dimension rule. Missing fields, old schema versions, stale pins, missing
authority, or unknown dimensions never fall back to compatibility.

One series has one pinned anchor and policy. Every member is compared directly
with that anchor. Compatibility between adjacent members cannot be inherited by
a later member.

Replay recomputes the decision from the exact E05 records, E01 capabilities and
authority head, D01 authorized linkage revisions, D04 committed activation
receipts, provider trust snapshots, anchor policy, and external pins. Both the
anchor and member receipts must be exact members of one retained
`ProviderLinkageStore.active_snapshot`. Decisions bind that immutable as-of
snapshot's version, head, and digest; they do not claim the mutable store remains
current after return. A consumer must replay the member or series against the
live store immediately before emitting a delta or trend. A serialized receipt or
self-consistent stored digest is not enough; an absent live store or any changed
record, authority, lineage, receipt, explanation, action, or policy causes replay
failure.

All fixtures and tests are synthetic/local. Outcomes describe technical
comparability only and carry no clinical interpretation.
