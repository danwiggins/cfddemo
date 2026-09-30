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
revision remains active in the live authority store.

## Derived comparisons

A comparison can be registered only over a bounded, uniquely sorted set of
active record identities. It pins the current linkage authority head and the
exact derived-artifact digest. Superseding any member writes a durable
`record_superseded` invalidation. A later linkage correction, tombstone, or
authority advance is discovered through the live store and appended as a
durable invalidation before a snapshot or status is returned. Stale comparisons
remain inspectable as history but are never current comparison authority.

Snapshots and statuses are canonical, privacy-safe records. Snapshot replay
reruns live authority and rejects any prior ledger or linkage head. Replay is
bounded to 100,000 records and comparisons and 1,000 members per comparison.

## Durability and trust boundary

The ledger root and SQLite files are private, no-follow regular filesystem
objects. Schema, canonical row bytes, content digests, complete supersession
history, row counts, state version, and state head are revalidated in every
transaction. SQLite uses foreign keys, WAL, full synchronous writes, immediate
writer transactions, and uniqueness constraints for cross-process conflicts.

The same process-integrity and offline-storage limits as the provider linkage
store apply. Provider backup, rollback anchoring, encryption, OS access control,
and real provider qualification remain external requirements.

This module is additive. Existing result-catalog, linkage, cohort-manifest, and
longitudinal contract schemas and serialized bytes remain unchanged; consumers
adopt the ledger explicitly when they need durable supersession authority.
