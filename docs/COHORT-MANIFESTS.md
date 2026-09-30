# Versioned cohort manifests

Status: D05 synthetic/local contract. This foundation defines cohort membership
and denominators. It does not authorize real data, provider operation, clinical
use, or scientific claims.

A `CohortManifest` is immutable canonical JSON. It pins one declared analysis
unit (`subject`, `collection`, or `specimen`), an explicit technical-replicate
and reanalysis rule, a defined time axis, inclusion/exclusion/missingness policy
digests, and measurement-definition and anchor-authority digests. Membership is
sorted by provider, linkage identifier, and exact linkage revision. Opaque
provider-local tokens are the only identity fields; names, source identifiers,
paths, sequences, and free text are outside the contract.

Each biological analysis unit contributes exactly one denominator. Additional
records for that unit must be marked technical replicates or reanalyses and
cannot contribute another biological draw. Cross-unit lineage changes,
duplicate analysis records, unknown reanalysis sources, and mismatched declared
unit keys fail validation.

`validate_manifest_against_linkage_snapshot` rechecks every member against one
current protected-store snapshot. It requires independently pinned provider
trust digests and binds the store identity, epoch, trust pins, state version,
state head, exact linkage-revision digest, and committed receipt. Missing,
stale, corrected, tombstoned, wrong-provider, or wrong-lineage records fail
closed.

Version one has no predecessor. Every later version must increment by one, bind
the prior manifest digest, and change canonical membership or policy semantics.
Reusing a version for changed bytes, skipping a version, mutating in place, or
creating a metadata-only version fails history validation. Identical controlled
inputs reproduce identical bytes regardless of caller input order.
