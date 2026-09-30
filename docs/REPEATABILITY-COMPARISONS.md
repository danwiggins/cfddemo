# Repeatability-aware comparisons

D07 produces descriptive technical comparisons only. It does not infer cause,
clinical meaning, treatment effect, or biological change, and it never applies
automatic batch or preanalytical correction.

## Authorization boundary

A numeric value, delta, uncertainty interval, denominator, or connecting trend
is emitted only when all of these checks pass:

1. The stored D03 decision replays exactly from the pinned anchor policy,
   current method authority, current provider-linkage trust, and the live D04
   linkage-store receipt.
2. D03 classifies the member as `equivalent` or `qualified_compatible`.
3. Both observations are canonical result-authority-signed evidence receipts
   binding the exact D02 record, E05 result and bundle bytes, value, uncertainty
   method and bounds, and reconciled total/included/excluded denominator counts.
   Both measurements must be complete and sufficient.
4. The repeatability envelope matches the independently pinned envelope,
   evidence, protocol, and authority digests and is valid at evaluation time.
5. Method reference and digest, quantity, unit, uncertainty method, and
   denominator semantics match both records exactly.
6. The absolute anchor-to-member delta is within the inclusive preapproved
   combined envelope.

The envelope must explicitly cover between-day, operator, lot, and
preanalytical factors in canonical order. Each signed observation binds its
actual condition and condition-policy identity for all four factors. Each
anchor-to-member transition must exactly match the corresponding registered
factor transition before the approved combined absolute-delta rule can be used.
D07 does not infer a missing condition or invent a transition/composition rule.

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
then hashed. Canonical replay occurs at envelope, observation, evidence-receipt,
comparison evaluation, and comparison digest boundaries, so unchecked
`model_copy` mutations cannot create authority.
Controlled identifiers reject path-like and identity-bearing vocabulary. The
comparison artifact binds the exact records, anchor policy, replayed D03
decision, repeatability envelope, and evaluation time without carrying local
paths, raw sequence data, or provider identity labels.
