# Versioned cohort manifests

Status: D05 synthetic/local contract. This foundation defines cohort membership
and denominators. It does not authorize real data, provider operation, clinical
use, or scientific claims.

A `CohortManifest` v2 is immutable canonical JSON. It pins one declared analysis
unit (`subject`, `collection`, or `specimen`), separate technical-replicate and
reanalysis rules, a defined time axis, inclusion/exclusion/missingness policy
digests, and measurement-definition and anchor-authority digests. Opaque
provider-local tokens are the only identity fields; names, source identifiers,
paths, sequences, and free text are outside the contract.

Membership uses deterministic longitudinal order. Every member binds its exact
linkage revision and current committed receipt, a known run, an independently
pinned and signed `CollectionEventReference`, and a canonical time coordinate
under the manifest time-axis definition. A collection event carries provider, subject,
collection, exact collection time, and an Ed25519 provider proof over those
canonical semantics. The proof binds an independently pinned live provider
trust snapshot, issuer, key, nonce, validity window, explicit data-purpose
grant, and exact event digest. A linkage-creation grant does not authorize
collection events or subject/study origins. A separate signed data-grant
contract lists the exact permitted data purposes and is verified with a key
from the unchanged, independently pinned provider trust snapshot. Existing
provider trust bytes and linkage-purpose semantics remain unchanged. Grant and
proof bind the same provider, issuer, key, principal, role, and trust snapshot;
a grant must exist before its proof and cannot expire before that proof.
It never reuses `LinkageRevision.proposed_at`, which records an administrative
linkage action rather than a biological event.

The opaque `timepoint_<hex>` handle is derived from provider, subject and
collection identity. Collection-time coordinates come from `collected_at`;
relative axes additionally require an exact signed UTC origin and derive
seconds from that origin. Study-relative axes require every provider to
authorize the same origin. Subject-relative axes require one separately
authorized origin for every provider/subject pair, so a cohort-wide origin
cannot silently stand in for multiple subjects. The time axis commits the
canonical ordered origin-proof set. All members under one
provider/subject/collection, including
sibling specimens, corrections, technical reruns and reanalysis, therefore
share one timepoint handle and coordinate. Reanalysis and technical-replicate
source chains must remain within the same biological lineage and cannot contain
cycles.

A collection-time axis has no origin proof and must bind the canonical digest
of the empty origin set. An arbitrary unused origin digest, even with a
recomputed member time commitment, cannot manufacture a new cohort version.

Integer domains are finite. Linkage revisions use the upstream linkage
revision maximum, store authority versions use the same 10,000-record maximum
as the protected store's authoritative state semantics, and time coordinates use whole UTC seconds within Python's
representable year 1 through 9999 range. Oversized integers fail canonical,
history, and live-store validation even if a caller recomputes the time
commitment.

Each declared analysis unit contributes exactly one denominator. A subject
unit can contain multiple collections and specimens; a collection unit can
contain sibling specimens. Within one exact subject/collection/specimen
lineage, a technical rerun cannot become another biological draw or denominator
unit. The separate
replicate and reanalysis policies determine whether those records are excluded
or collapsed without changing the biological denominator.

`validate_manifest_against_linkage_store` accepts a live
`ProviderLinkageStore`, reads its current active snapshot, and recomputes the store
trust-pin digest from independently supplied provider pins. It requires the exact provider
trust snapshots matching those independent pins and verifies every event and
relative-origin signature against an active trusted issuer. A caller cannot
authorize changed time semantics by recomputing dependent digests, selecting a
new trust snapshot, or copying a proof to another event. The boundary requires
the exact concrete store type and rejects subclass, fake, instance-shadowed,
or class-shadowed authority callables. Every provider authority and receipt must
match the live store ID, epoch, storage identity, trust pins, state version,
state head, provider, linkage revision, and lineage. Detached snapshots,
closed stores, cross-store receipts, state advances, corrections, tombstones,
and caller-forged trust-pin digests fail closed.

Trust, grant, and proof validity are required at both immutable manifest
creation and the protected current time. The order is trust issuance, grant
issuance, proof issuance, manifest creation, and current evaluation; both
manifest creation and current evaluation must be strictly before proof and
grant expiry, whose nesting cannot exceed trust expiry. Caller-authored
creation time therefore cannot authorize historical as-of replay or a proof
issued after the manifest. Future-issued, exactly expired, post-expiry, and
backdated event or origin authority fail closed.

One store write fence covers canonical validation, independent pin capture,
the active snapshot, monotonic protected time, every authority and membership
check, final snapshot/time revalidation, and return. Normal commits cannot move
the live authority head between a successful final check and the result. The
time comes from the store's persisted authority-time floor, so an underlying
clock rollback cannot revive expired collection-event or relative-origin
authority.

Version one has no predecessor. Every later version must increment by one, bind
the prior manifest digest, use a strictly later creation time, and change
substantive membership or cohort policy. Authority refreshes and timestamps
alone cannot create a new cohort version. The canonical parser rejects unknown
fields, duplicate-key encodings, whitespace variants, and other noncanonical
JSON. Substantive comparison projects collection events and relative origins to
their authority-independent provider, lineage, time, axis, and target semantics;
proof IDs, nonces, signatures, proof-derived event digests, and proof-derived
member time commitments cannot make an otherwise identical version substantive.
Changed biological collection time, origin time, axis definition, membership,
or policy remains substantive. Identical controlled inputs reproduce identical
bytes regardless of caller input order.

Both live-store and history validation first walk the exact object graph without
calling caller-owned hooks. Only exact contract, enum, primitive, tuple, and
approved UTC timestamp types may reach canonical serialization. They then
serialize and canonically reparse the complete manifest. Unvalidated
`model_copy` mutations therefore cannot
bypass denominator, unit, role, dependency, ordering, member, or authority
checks. The iterative preflight detects cycles and enforces depth, node,
scalar, and declared per-field tuple limits before serialization, avoiding raw
recursion failures or unbounded traversal. Canonical byte input is bounded
before parsing; duplicate keys, oversized integer tokens, deep JSON, excessive
nodes or collection items, and oversized strings produce one sanitized
canonical-input rejection. Technical-replicate and reanalysis dependencies
share one acyclic graph even though their inclusion policies remain separate.

All opaque linkage identities remain provider-local. Analysis records,
declared-unit denominator groups, biological-lineage groups, and dependency
edges are keyed by provider namespace plus token. Equal opaque tokens from two
providers therefore remain distinct, and a replicate or reanalysis cannot
name a source analysis from another provider.

## Schema transition and persistence boundary

`traceback.cohort-manifest.v1` used linkage proposal timestamps as collection
coordinates. Those bytes are recognized as historical but cannot be opened as
live manifests, because the missing collection event cannot be reconstructed
honestly. Rebuild v2 from independently pinned provider collection-event
evidence; never translate the old timestamp automatically.

This module is the immutable manifest and live-validation contract. A protected
durable cohort-version registry, optimistic publication transaction, and
backup/restore rehearsal remain a separate D05 persistence follow-up. Until
that lands, v2 manifests can be materialized for bounded computation but cannot
support a claim that cohort publication is durably registered. The timepoint
handle is safe for a browser selector only after a protected read model
authorizes the record; this contract does not expose subject or collection
tokens to the UI.
