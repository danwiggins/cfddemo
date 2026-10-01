# Repeatability-aware comparisons

D07 produces descriptive technical comparisons only. It does not infer cause,
clinical meaning, treatment effect, or biological change, and it never applies
automatic batch or preanalytical correction.

## Authorization boundary

A numeric value, delta, uncertainty interval, denominator, or connecting trend
is emitted only when all of these checks pass:

1. The stored D03 decision replays exactly from the pinned anchor policy,
   current method authority, current provider-linkage trust, and the live D04
   linkage-store receipt. D07 holds a SQLite authority fence across the final
   live replay and artifact construction, so a linkage writer cannot interleave
   with a numeric return. The immutable result is valid as of that serialized
   read; a consumer must replay it after any later linkage commit before use.
2. D03 classifies the member as `equivalent` or `qualified_compatible`.
3. Both observations are canonical result-authority-signed evidence receipts
   binding the exact D02 record, E05 result and bundle bytes, value, uncertainty
   method and bounds, and reconciled total/included/excluded denominator counts.
   The domain-separated signature also binds the receipt ID and evidence digest.
   Both measurements must be complete and sufficient.
4. Result signatures replay against an independently pinned canonical trust
   document. The document is bounded to 32 sorted unique public keys; key
   purpose, namespace, revocation state, trust digest, and verified key IDs are
   checked and bound into the comparison.
5. The repeatability envelope matches the independently pinned envelope,
   evidence, protocol, and authority digests and is valid at evaluation time.
6. Method reference and digest, quantity, unit, uncertainty method, and
   denominator semantics match both records exactly.
7. The absolute anchor-to-member delta is within the inclusive preapproved
   combined envelope.

The envelope must explicitly cover between-day, operator, lot, and
preanalytical factors in canonical order. Each signed observation binds its
actual condition and condition-policy identity for all four factors. Each
anchor-to-member transition must exactly match the corresponding registered
factor transition before the approved combined absolute-delta rule can be used.
D07 does not infer a missing condition or invent a transition/composition rule.

`compare_repeatability_in_fence` is the already-fenced variant for a caller
that holds the store's authority fence in the calling thread, such as the
protected D07 comparison registry (`docs/REPEATABILITY-COMPARISON-REGISTRY.md`).
SQLite cannot open the nested fence `compare_repeatability` would take. The
variant checks that this thread holds the fence at entry and before the final
replay, and otherwise raises before returning any result; its gates and
contract are identical.

## Unavailable states

`outside_envelope`, `missing_draw`, `failed_measurement`,
`insufficient_measurement`, `incompatible`, `unknown`, `requires_reanalysis`,
`registered_bridge`, and `evidence_unavailable` remain distinct. Every
unavailable result suppresses both source values, the delta, uncertainty,
denominators, envelope magnitude, and trend permission. Missing draws are never
represented as zero.

Every comparison is anchored directly to the same policy and anchor record.
Adjacent within-envelope differences cannot be chained to admit a member whose
anchor-relative difference is outside the envelope.

## Privacy and replay

Contracts are immutable, closed, versioned, bounded, canonically replayed, and
then hashed. Exact no-hook collection, primitive, model-state, and global graph
depth/node preflights reject oversized forged graphs, repeated container aliases,
and cycles before serialization.
Canonical replay occurs at envelope, observation, evidence-receipt, comparison
evaluation, and comparison digest boundaries, so unchecked `model_copy`
mutations cannot create authority.
Controlled identifiers reject path-like and identity-bearing vocabulary. The
comparison artifact binds the exact records, anchor policy, replayed D03
decision, repeatability envelope, and evaluation time without carrying local
paths, raw sequence data, or provider identity labels.
