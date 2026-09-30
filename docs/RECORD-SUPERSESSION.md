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
store. Every source in the chain must remain active.

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

Provider stores migrate v1/v2 state to v3 in place. D04 records and comparisons
use v2 signed contracts; pre-release v1 D04 rows are rejected rather than
silently upgraded into authority. Existing result-catalog, cohort-manifest, and
longitudinal contracts remain unchanged.
