# Protected provider linkage store

This D01/D04 boundary turns a verified provider approval proof into active local
linkage only through one SQLite transaction. The store is separate from public
result artifacts and contains protected opaque subject, collection, specimen,
aliquot, run, analysis and measurement tokens.

## Commit boundary

`ProviderLinkageStore.commit_authorized_revision` begins an immediate
transaction, replays the external signatures against the store's independently
provisioned provider trust pins and construction-pinned clock, extends exactly one immutable
revision chain, consumes every approval ID and nonce, validates the complete
history, advances the state version/head and commits. A conflict rolls the whole
transaction back. An exact retry returns the existing receipt without advancing
state. Approval IDs and nonces occupy one global replay namespace, so reuse for
different bytes or a different provider is rejected by database constraints,
including across processes and restarts.

The clock callable identity is retained outside caller-mutable store attributes.
Each operation captures one whole-second UTC value under the store lock, advances
a persisted monotonic authority-time floor in a serialized transaction, then
requires that exact floor in the record transaction. Replacing the instance
clock, rolling the same callback backward, reopening with an earlier clock, or
mutating authority methods during the callback cannot revive expired approval.
The captured validator and internal storage call chain do not dynamically
dispatch through caller-shadowable instance methods.

The store validates the full append-only history, not only active rows:

- collection tokens retain one subject parent;
- specimen tokens retain one subject/collection parent;
- known aliquot tokens retain one subject/collection/specimen parent;
- analysis and measurement identities cannot be reassigned after correction or
  tombstone;
- sibling specimens/aliquots and distinct technical reruns remain valid;
- correction chains are consecutive and content-addressed.

## Read boundary

`active_snapshot` runs from one database snapshot, revalidates schema, exact
canonical authorized-record bytes and digests, signed history, state head,
current trust/approval time and tombstones, then returns
only latest active revisions in canonical order. A `CommittedLinkageReceipt` is
not authority by itself. `verify_current_receipt` must recheck it against the
live store; any correction, tombstone or unrelated state change makes an older
receipt stale. Receipts also bind the store ID, store epoch, root/database
storage identity, exact trust-pin set and exact authorized-record digest. A
receipt from another independently initialized store is rejected even when both
stores contain byte-identical linkage revisions.

## Storage boundary

The root is absolute, private (`0700`) and opened no-follow. The database is
regular, private (`0600`), descriptor-bound to the SQLite connection and
revalidated before and after operations. Existing permissive modes are rejected,
not repaired; WAL/SHM sidecars are also private, owned, regular and no-follow.
Initialization uses an exact committed schema; partial, extra or altered
tables/indexes fail closed. Schema v2 adds the canonical authority-time floor;
an exact v1 store is migrated once under the exclusive initialization
transaction, while malformed or extra metadata is rejected. Trust pins are
persisted on first initialization and every reopen must supply the exact same
set. SQLite uses WAL, full synchronous writes, foreign keys and bounded busy
waits.

This is a local application integrity boundary, not protection from a principal
that can replace all provider-managed storage while the application is stopped.
Provider encryption, OS access control, consistent backup/restore and an
external rollback anchor remain deployment requirements. Until those are
qualified, this work is research-only and does not claim Epic D evidence or
provider production readiness.

No free-text identities, donor data, paths, read IDs, sequences or clinical
claims are accepted by or emitted from the public contracts.
