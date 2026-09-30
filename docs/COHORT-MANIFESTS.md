# Versioned cohort manifests

Status: D05 synthetic/local contract. This foundation defines cohort membership
and denominators. It does not authorize real data, provider operation, clinical
use, or scientific claims.

A `CohortManifest` is immutable canonical JSON. It pins one declared analysis
unit (`subject`, `collection`, or `specimen`), separate technical-replicate and
reanalysis rules, a defined time axis, inclusion/exclusion/missingness policy
digests, and measurement-definition and anchor-authority digests. Opaque
provider-local tokens are the only identity fields; names, source identifiers,
paths, sequences, and free text are outside the contract.

Membership uses deterministic longitudinal order. Every member binds its exact
linkage revision and current committed receipt, a known run, the provider
linkage event, and a canonical time coordinate under the manifest time-axis
definition. Collection-time coordinates are re-derived from the authoritative
linkage event during live validation. Reanalysis and technical-replicate source
chains must remain within the same biological lineage and cannot contain
cycles.

Each declared analysis unit contributes exactly one denominator. A subject
unit can contain multiple collections and specimens; a collection unit can
contain sibling specimens. Within one exact subject/collection/specimen
lineage, a technical rerun cannot become another biological draw. The separate
replicate and reanalysis policies determine whether those records are excluded
or collapsed without changing the biological denominator.

`validate_manifest_against_linkage_store` accepts a live
`ProviderLinkageStore`, reads its current active snapshot, recomputes the store
trust-pin digest from independently supplied provider pins, and invokes the
store's current-receipt verification. Every provider authority and receipt must
match the live store ID, epoch, storage identity, trust pins, state version,
state head, provider, linkage revision, and lineage. Detached snapshots,
closed stores, cross-store receipts, state advances, corrections, tombstones,
and caller-forged trust-pin digests fail closed.

Version one has no predecessor. Every later version must increment by one, bind
the prior manifest digest, use a strictly later creation time, and change
substantive membership or cohort policy. Authority refreshes and timestamps
alone cannot create a new cohort version. The canonical parser rejects unknown
fields, duplicate-key encodings, whitespace variants, and other noncanonical
JSON. Identical controlled inputs reproduce identical bytes regardless of
caller input order.
