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

Integer domains are finite. Linkage revisions use the upstream linkage
revision maximum, store authority versions use the protected approval-ledger
capacity, and time coordinates use whole UTC seconds within Python's
representable year 1 through 9999 range. Oversized integers fail canonical,
history, and live-store validation even if a caller recomputes the time
commitment.

Each declared analysis unit contributes exactly one denominator. A subject
unit can contain multiple collections and specimens; a collection unit can
contain sibling specimens. Within one exact subject/collection/specimen
lineage, a technical rerun cannot become another biological draw. The separate
replicate and reanalysis policies determine whether those records are excluded
or collapsed without changing the biological denominator.

`validate_manifest_against_linkage_store` accepts a live
`ProviderLinkageStore`, reads its current active snapshot, recomputes the store
trust-pin digest from independently supplied provider pins, and invokes the
store's pinned current-receipt verification. The boundary requires the exact
concrete store type and rejects subclass, fake, instance-shadowed, or
class-shadowed authority callables. Every provider authority and receipt must
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

Both live-store and history validation first serialize and canonically reparse
the complete manifest. Unvalidated `model_copy` mutations therefore cannot
bypass denominator, unit, role, dependency, ordering, member, or authority
checks. Technical-replicate and reanalysis dependencies share one acyclic
graph even though their inclusion policies remain separate.
