# Pinned-anchor longitudinal compatibility

`evidence_inspector.longitudinal_compatibility` is the additive D02 boundary
used by future E12 views. It does not change E05's pairwise API, execute a
bridge, run reanalysis, create cohort membership, or authorize provider
identity.

## Complete identity

Each `LongitudinalRecord` binds an exact E05 result ID/digest, bundle ID/digest,
E05 compatibility-key digest, E01 capability digest, registry identity and
authority head/revision to one proof-carrying D01 linkage revision and a
current `CommittedLinkageReceipt` from the protected transactional store plus a
`LongitudinalComparisonKey`. The linkage's protected measurement token and
source-projection token are derived from those exact E05/E01 identities, so an
unrelated authorized linkage cannot be attached to a result. The key carries
method, quantity and unit plus every required comparison dimension in canonical
order:

1. assay/protocol;
2. preanalytics policy;
3. reference;
4. canonical model;
5. modified-base model;
6. trimming policy;
7. measurement definition;
8. atlas/marker set;
9. filter/QC policy;
10. uncertainty method;
11. coordinate semantics;
12. denominator semantics; and
13. result schema.

Every component is either a complete versioned digest identity or explicit
`unknown`. The measurement-definition component is derived from and validated
against the exact method definition, quantity and unit; it cannot be relabeled
independently. Overlapping E05 reference, atlas/grid/panel, normalization,
coordinate, denominator and result-schema identities are recomputed and must
match their D02 dimensions exactly.

## One anchor, six outcomes

Every series pins one complete anchor-key digest and one exact policy digest.
Every member is evaluated directly against that anchor. Adjacent comparisons
are never used to extend a series.

Exact match is the default. A mismatch is permitted only when the anchor policy
names that exact member-component digest and binds evidence. The six outcomes
are:

- `equivalent`: exact complete-key match;
- `qualified_compatible`: every mismatch is in a named qualified envelope;
- `requires_reanalysis`: every mismatch requires a new derived analysis;
- `registered_bridge`: every mismatch has a named registered bridge;
- `incompatible`: an unregistered mismatch or mixed disposition; and
- `unknown`: missing identity, stale policy/anchor, invalid result state, or
  absent linkage authority.

Only `equivalent` and `qualified_compatible` permit numeric deltas or connecting
trend lines. `registered_bridge` reports the bridge reference but does not run
it, emit a translated value, or connect the source records. `requires_reanalysis`,
`registered_bridge`, `incompatible` and `unknown` all suppress deltas and trends.
Each decision binds the exact result, bundle, record, linkage, capability,
activation-receipt, anchor and policy identities and has canonical bytes and a SHA-256 digest. A
series retains the ordered digest of every member decision. Closed semantic
validation rejects forged combinations such as an `equivalent` outcome carrying
mismatch dimensions or evidence.

Longitudinal members must have the same explicit provider and subject linkage.
Distinct collections are biological timepoints. Multiple technical analyses may
bind the same collection without becoming additional biological timepoints.

Policy digests, current E01 authority-head digest and provider trust-snapshot
digests are caller-pinned external inputs. The decision engine also requires a
live `ProviderLinkageStore`; it invokes `verify_current_receipt` for both anchor
and member. The boundary requires the exact protected store implementation;
duck-typed accept-all objects and subclasses cannot authorize a comparison. A
serialized, cross-store, forged, stale or absent receipt, or an absent live
store, produces `unknown` and suppresses deltas/trends. Receipt digests bind
the exact provider, linkage, revision, store version and store head into each
decision. Scientific qualification and provider approval are not inferred from
a fixture, method label, matching digest, or passing test. The future durable
comparison-membership ledger must replay the live store and bind the exact D02
decisions before materializing a view.
