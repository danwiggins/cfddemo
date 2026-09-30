# Durable record supersession

Status: D04 synthetic/local foundation. This contract does not authorize real
provider data, clinical interpretation, release, or export.

`RecordSupersessionStore` is a private append-only SQLite ledger bound to one
exact live `ProviderLinkageStore`. A record binds an opaque provider namespace,
analysis identity, immutable result and bundle digests, and the exact linkage
revision and activation receipt observed when it was registered. Public ledger
contracts accept no names, free text, paths, source identifiers, sequences, or
clinical claims.

## Supersession and reanalysis

An initial record must bind an active linkage whose technical lineage has no
reanalysis source. A reanalysis must bind a distinct active analysis whose
provider-local `reanalysis_of` token names the source analysis. The source and
replacement must retain the same exact biological lineage. The new record both
derives from and supersedes that source; it always carries
`biological_timepoint_contribution=false`. A technical reanalysis therefore
cannot manufacture another collection, draw, denominator, or timepoint.
The `primary_analysis` role identifies the first technical record in a chain;
it does not itself assert or create biological timepoint membership.

History is never edited. Each record can have at most one successor, sources
must already exist, and self-links, missing sources, branches, and cycles fail
closed. An exact retry returns the same content-addressed receipt without
advancing state. Active selection returns only chain leaves whose exact linkage
revision and immutable activation receipt remain active in the live authority
store. Superseded ancestors remain immutable history; a later correction or
tombstone of an ancestor does not hide a separately authorized current leaf.

`record_history_snapshot(*, cursor=None, limit=100)` is the protected
read contract for longitudinal consumers. It returns the global immutable
record ledger in canonical record-ID order with deterministic no-gap,
no-duplicate pagination. The opaque typed continuation cursor authenticates its
record position with a canonical HMAC-SHA-256 under a private 256-bit ledger key
and binds the exact ledger and linkage identities, versions, and heads;
continuation fails closed after any append or authority change instead of
silently omitting newly sorted history. The limit is closed to 1 through 1,000.
Every row includes the immutable record, its content digest, original activation
receipt, successor edge, and durable stale-comparison warnings. Rows are explicitly
`superseded`, `active`, or `authority_invalid`. A record with a successor is
always historical. A leaf is active exactly when its own current linkage and
activation receipt match live authority; a stale ancestor does not invalidate
a separately authorized current replacement.

The snapshot binds the ledger ID, epoch, storage identity, version and head and
the current linkage-store ID, epoch, storage identity, version and head, plus
the input cursor, page limit, and next cursor. The ledger transaction and
linkage authority fence remain held through full-ledger chain validation,
bounded page construction, detached canonical capture, and final state
validation. `replay_history_snapshot` rejects prior heads and caller-altered
values. Pages with more than 1,000 affected-comparison warnings fail closed.

Provider-store schema v3 persists the original state coordinates for every
activation receipt, so unrelated authority advances do not rewrite its proof.
A v2 supersession record carries a controlled reason and an Ed25519 approval
whose signed statement binds the result and bundle digests, source edge,
technical lineage, and exact linkage proof. Reviewer role, key, purpose, trust
snapshot, validity window, and issuer status are independently checked.

## Derived comparisons

A comparison can be registered only over a bounded, uniquely sorted set of
active record identities. Its v2 immutable bytes and reviewer signature bind
the provider, linkage-store identity, authority version and head, members, and
exact derived-artifact digest. The SQLite index columns must equal those signed
bytes. Superseding any member writes a durable
`record_superseded` invalidation. A later linkage correction, tombstone, or
authority advance is discovered through the live store and appended as a
durable invalidation before a snapshot or status is returned. Stale comparisons
remain inspectable as history but are never current comparison authority.

Snapshots and statuses are canonical, privacy-safe records. Snapshot replay
reruns live authority and rejects any prior ledger or linkage head. Replay is
bounded to 100,000 records and comparisons and 1,000 members per comparison.

## Durability and trust boundary

The ledger root and SQLite files are private, no-follow regular filesystem
objects bound to protected descriptors, owner/mode checks, and exact inode
identity. Each connection proves the database descriptor it opened. Schema,
canonical row bytes, content digests, complete supersession
history, row counts, state version, and state head are revalidated in every
transaction. SQLite uses foreign keys, WAL, full synchronous writes, immediate
writer transactions, and uniqueness constraints for cross-process conflicts.
Dependent commits hold a provider-store write fence through the ledger commit.

The same process-integrity and offline-storage limits as the provider linkage
store apply. Provider backup, rollback anchoring, encryption, OS access control,
and real provider qualification remain external requirements.

The cursor MAC key is generated once inside the private ledger database, bound
into the ledger state head, retained across reopen, and included in a complete
database backup or restore. Restoring an older complete database rejects cursors
issued by later ledger heads. Detecting rollback to an internally consistent
older database still requires the external rollback anchor described above.

Provider stores migrate v1/v2 state to v3 in place. D04 records and comparisons
use v2 signed contracts; pre-release v1 D04 rows are rejected rather than
silently upgraded into authority. Existing result-catalog, cohort-manifest, and
longitudinal contracts remain unchanged.
