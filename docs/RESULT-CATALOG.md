# Local result catalog

`ResultCatalog` is a local-only index over verified deterministic result bundles.
It does not discover files, contact a service, or expose donor/run identifiers.

## Import boundary

Callers configure at most eight named import roots and provide one exact relative
bundle directory. The importer accepts at most four path components and opens
each component relative to an already-open directory descriptor with symlink
following disabled. It accepts only the eight files in the result-bundle schema,
with per-file and total byte limits. Extra entries, special files, traversal,
source mutation, and import-root replacement fail closed.

The importer copies the bundle into catalog-owned storage, rechecks source
identities, and invokes the existing bundle verifier against that immutable
snapshot. It also replays the supplied current E01 method capability against the
supplied registry and trusted authority head. A stale or revoked capability is
not cataloged. The verified v2 bundle must bind the same method ID, version, and
method-definition digest as that replayed capability; v1 or mismatched bundles
are rejected rather than relabeled.

Object publication is an exclusive atomic rename after file and directory
flushes. The SQLite index uses WAL, full synchronous durability, and one
immediate transaction for the public reference plus its private aliases. A
failure before commit leaves no indexed partial result. Publication is
monotonic: rollback never unlinks a published content-addressed object because
another importer may already have adopted it. A failure can therefore leave an
unreferenced object, which a retry verifies and safely adopts. Garbage
collection is deliberately absent until it can coordinate reference proof with
all importers under the same catalog lock.

Coordinators that must publish a second local index use the prepared-import
protocol. Preparation verifies and retains the immutable object without adding
a visible row. Staging adds a durable `pending` row that queries and reference
verification exclude. Adoption verifies package-owned catalog authority and
changes that exact publication to `adopted` inside one immediate transaction;
it accepts and executes no caller callback. A coordinating catalog performs its
own authority checks on both sides while holding its publication lock.
Compensation removes only the exact publication's index
references and never removes the content-addressed object. A v1 catalog is
expanded transactionally to this v2 publication table after its original schema
has been verified exactly.

Recovery can enumerate bounded hidden publications for one digest-bound
coordinator scope and resolve one publication from the exact SQLite schema
without trusting a filesystem journal. This lets a
coordinator compensate a pending row after journal loss or corruption and keeps
retry idempotent while retaining the immutable shared object.

The catalog root, object directory, and database inode are retained and
revalidated on operations. Staging creation and publication are relative to the
bound object-directory descriptor, so path replacement cannot redirect accepted
bytes. SQLite uses one lock-serialized retained connection, and connection
establishment is serialized across catalog instances in the process. At open,
the catalog snapshots each open descriptor's type/device/inode identity and
proves the newly opened SQLite descriptor matches the no-follow database
binding. Descriptor-number reuse is accepted only when the identity changed,
and catalog close is serialized with connection proof. A transient pathname
swap cannot substitute a database and hide the swap before the post-open check.

Fresh schema initialization holds that process-local critical section through
completion and uses one SQLite `BEGIN EXCLUSIVE` transaction. The SQLite lock is
the cross-process coordination boundary: one constructor creates the complete
schema and commits it atomically; concurrent constructors then validate the
exact committed table/index DDL and schema marker, including primary keys,
nullability, uniqueness, foreign keys, and ordered index columns. Existing
partial, altered, or extra schemas fail closed and are never repaired
implicitly.

## Result trust

A catalog is opened with exactly one result trust authority:

- `result_trust_registry=` (the protected path): a
  `ResultTrustRegistry` (`docs/RESULT-TRUST-REGISTRY.md`). Every verification
  reads the registry's current trust under its read fence and builds a fresh
  `TrustStore` from that head, so a key revoked in the registry is rejected by
  the next import, reference verification, prepared adoption, or live read on
  the same open catalog and reader, without reopening anything. The registry
  is forward-only, and the catalog also keeps a high-water mark of the
  (state version, head) it has seen and rejects any older or forked head, so
  old trust cannot come back through this catalog.
- `trust_store=` (the earlier path, kept for callers that build one): a
  caller-held `TrustStore`. Revocation reaches the catalog only through that
  same instance. The live reader freezes its key map at bind time.

Each operation holds one trust head from verification through return. On the
registry path an import, a preparation, staging, adoption, reference
verification, and a live read each hold the catalog `_connection_lock` and
then the registry read fence until they return, so no trust event can commit
between verification and the indexed or returned result. A preparation binds
the trust head into its authority snapshot; a trust event before staging or
adoption makes that preparation fail with `catalog authority changed during
import`, so a key revoked mid-import is never published.

`trust_authority_fence()` exposes the same fence to composing callers (D06),
and also holds the catalog-content lock shared (below). Catalog calls made
inside it on the same thread reuse the held head, because the registry lock
is not reentrant. The body must not open a linkage fence, call a D07
registry, mutate the trust registry, or write this catalog unless the caller
took the content lock exclusively first; a trust mutation on the
holding thread raises instead of deadlocking. The TrustStore path yields
`None` and holds nothing.

Lock order: the catalog-content lock (in-process gate, then the catalog
`_connection_lock`, then `flock`), then the result-trust read fence. The
connection lock still precedes trust, as D06 already used for the TrustStore
lock (catalog connection lock, then the trust lock), so D06's fence order is:
linkage fence, D05 lock, catalog content (gate, connection lock, flock),
result trust, D06 root; E06 takes the D06 fence and
then its own lock; D07 takes linkage fence, result trust, then its own lock.
Code holding a trust read fence taken directly from the registry (as D07 does)
must not call the catalog: the catalog would wait for its connection lock while
holding trust, the reverse of D06.

`CatalogAuthoritySnapshot` is versioned. `traceback.catalog-authority.v1` hashes
the keys of a caller-held `TrustStore`. `traceback.catalog-authority.v2` (the
registry path) sets `trust_snapshot_sha256` to a digest of the registry ID,
epoch, state version, head, and current document digest, so every trust event
changes the catalog authority digest, and a D06 or E06 artifact that binds it
becomes stale. The field set is unchanged; the version is inside the authority
digest, so `bound_catalog_authority` rebuilds a retained authority from its
digest unambiguously, and `CATALOG_AUTHORITY_SCHEMA_VERSIONS` lists every
version a retained digest may name. A D06 binding made under a v1 catalog keeps
replaying after that catalog is reopened on the registry path and is
re-verified against the registry.

## Catalog-content lock and content head

The SQLite transaction is not held between operations, so it cannot fence
catalog rows for a composing reader. A separate cross-process content lock
does: `flock` on `catalog-content.lock` (private, `0600`, owned by the
effective user, inode bound at open and rechecked on every acquisition) in the
catalog root.

- Every catalog-row writer holds it **exclusively**: `import_bundle`,
  `prepare_bundle_import`, `stage_prepared_import`, `adopt_prepared_import`,
  `finish_prepared_import`, `compensate_prepared_import`,
  `discard_prepared_import`, `register_coordinated_candidate`,
  `finish_coordinated_candidate`, `recover_pending_publication`, and schema
  initialization at open. Both trust paths (registry and TrustStore) take it.
- `trust_authority_fence()` holds it **shared** for its whole body, so no
  row write commits, from any process, while a composing reader (the
  composite fence's E04 step, D06's status and binding reads) holds it.
- `content_authority_fence(exclusive=...)` exposes it to a composing caller.
  D06 imports take it exclusively, and D06's open-time recovery takes it
  exclusively, before the D06 root; D06 reads take it shared.
- Plain reads (`query`, `verify_reference`, recovery enumeration, the live
  reader) take no content lock.

Lock order: the content lock, then the result-trust read fence (and
`_SQLITE_OPEN_LOCK` on a first connect). The content lock itself is taken in
three parts: an in-process gate per lock-file inode (a reader/writer gate
shared by every catalog instance on that root), then the instance's
`_connection_lock`, then the `flock` that excludes other processes. So no
thread waits for the content lock while holding a connection lock: a writer
of instance B that waits behind a holder of instance A holds nothing the
holder may need, and the holder can still read through B. The connection
lock is held through the body, so one thread per instance owns the lock;
nested entries on that thread reuse the held mode, and an exclusive request
under a held shared lock raises `CatalogConflict` (never upgraded). A thread
holding a root's content lock through one instance that asks for it through
another instance on the same root gets `CatalogConflict` instead of waiting
for itself. A caller must not hold a catalog's connection lock when it asks
for that catalog's content lock (D06 takes the content fence first), and
D06's fence entry (`_registered_authority_fence`, behind imports, binding
reads and record status) refuses a thread that already holds any content
lock, since D06 needs D01 first. The gate is reader-preferring: a steady
stream of shared holders (composite reads) can delay an in-process writer
indefinitely; that is a liveness limit, not a deadlock. A forked child resets
the gate (`os.register_at_fork`); it inherits no parent holders.

`content_head_in_fence()` (requires this thread's content lock) and
`content_snapshot()` (takes it shared) return `CatalogContentSnapshot`: a
digest over every row of every catalog table (metadata, results, aliases,
publications, coordinated rows, candidates) in primary-key order, read in one
SQLite read transaction. Any committed row change changes it; reads do not.
`catalog_dependency_head_sha256(authority, content)` is the E04 head of saved
dependency-head schema v2. The digest is linear in catalog size.

The binding of a catalog to its trust registry is not persisted in the catalog
database: reopening a catalog root with a different authority is the caller's
choice (see `docs/RESULT-TRUST-REGISTRY.md`, open decisions).

## Read model

`CatalogResultRef` contains immutable bundle, method, registry, and authority
identities. Its result ID is derived from the bundle digest and method-definition
digest. Execution, information, trust, and qualification are independent closed
states; consumers must not infer one from another.

Opaque display, run, and timepoint aliases live in a separate protected table.
They are accepted only in their controlled formats and can select results, but
the mapping and private predicates are not returned in result references or
serialized result pages. No input path, bundle path, run token, or
protected-identifier mapping enters the public reference.

Queries normalize and deduplicate bounded method/state filters. They support
method, state, and exact opaque-alias selectors, deterministic result-ID order,
keyset cursors, and a maximum page size of 100. Empty pages distinguish an empty
catalog from a filtered query with no matches using a bounded existence probe,
not a full-table count.

Exact-ID live reads use the same visibility rule as catalog pages: a
coordinator-staged result is absent until its publication is adopted. Lookup,
bundle and authority verification, and the final exact-row visibility check run
under one SQLite writer fence, so recovery cannot remove adoption between
verification and return. Pending, recovered, and unknown IDs all return the
same unavailable outcome without exposing hidden row existence.

## Scope

This is research inspection infrastructure. It is not a frontend, network API,
clinical interpretation layer, donor registry, or plugin interface.

The adversarial test suite covers idempotence, conflicting identities, source
mutation, root replacement, traversal, symlinks, special and extra files,
oversize inputs, simulated pre-commit failure, unsupported catalog schemas, and
stale/revoked authority. `tests/test_result_catalog_trust_registry.py` covers
the registry path: revocation without reopen, refusal of old trust, the trust
fence held through return, composing fences, and concurrent trust events. A deterministic synthetic 10,000-result test requires
p95 filtered page latency at or below 250 ms.
