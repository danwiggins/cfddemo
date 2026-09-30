# Protected provider linkage store

This D01/D04 boundary turns a verified provider approval proof into active local
linkage only through one SQLite transaction. The store is separate from public
result artifacts and contains protected opaque subject, collection, specimen,
aliquot, run, analysis and measurement tokens.

## Commit boundary

`ProviderLinkageStore.commit_authorized_revision` begins an immediate
transaction, replays the external signatures against the store's independently
provisioned provider trust pins and package-owned authority-time source, extends exactly one immutable
revision chain, consumes every approval ID and nonce, validates the complete
history, advances the state version/head and commits. A conflict rolls the whole
transaction back. An exact retry returns the existing receipt without advancing
state. Approval IDs and nonces occupy one global replay namespace, so reuse for
different bytes or a different provider is rejected by database constraints,
including across processes and restarts.

Arbitrary clock callbacks are not authority inputs and are never executed. The
store accepts only the exact package-owned `AuthorityTimeSource`; subclasses are
rejected before use. Each operation captures one whole-second UTC value under
the store lock, advances a persisted monotonic authority-time floor in a
serialized transaction, then requires those exact canonical floor bytes in the
record transaction. Replacing the instance source, rolling fixed source state
backward, reopening with an earlier source, or poisoning parser, serializer,
registry, floor, or validator aliases cannot revive expired approval. The
captured validator and internal storage call chain do not dynamically dispatch
through caller-shadowable instance methods.

## Process-integrity precondition

Installed package bytes, imported module globals and Python function objects
must be protected by the operating system and deployment environment. Arbitrary
in-process code execution, debugger access, or mutation of installed module
bytecode/globals is outside this store's threat model and invalidates the
process. A principal with that capability can rewrite both an implementation
and any Python-resident fingerprint or expected baseline, so such fingerprints
are not an authorization or trust root.

The package exposes a best-effort loaded-process integrity diagnostic. When it
observes mutation, the longitudinal boundary fails explicitly before using the
store. This is useful corruption/tamper detection, but a matching or newly
resealed diagnostic never proves that a compromised process is trustworthy.
Deployment must establish code signing or immutable package provenance, OS
access control and a clean process start outside the interpreter.

Untrusted canonical input bytes, Pydantic model-copy/object-shape attacks,
caller-controlled store objects, and storage/path/database races remain in
scope and fail closed. This authority path has no eval, executable plugin, or
unsafe object-deserialization hook through which serialized data can mutate
loaded code.

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
that can mutate the running program or replace all provider-managed storage
while the application is stopped.
Provider encryption, OS access control, consistent backup/restore and an
external rollback anchor remain deployment requirements. Until those are
qualified, this work is research-only and does not claim Epic D evidence or
provider production readiness.

The additive D04 record ledger is documented in
`docs/RECORD-SUPERSESSION.md`. It binds this store's exact live projection;
supersession state never edits linkage history or public result artifacts.

No free-text identities, donor data, paths, read IDs, sequences or clinical
claims are accepted by or emitted from the public contracts.
