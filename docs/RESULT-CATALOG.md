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

## Scope

This is research inspection infrastructure. It is not a frontend, network API,
clinical interpretation layer, donor registry, or plugin interface.

The adversarial test suite covers idempotence, conflicting identities, source
mutation, root replacement, traversal, symlinks, special and extra files,
oversize inputs, simulated pre-commit failure, unsupported catalog schemas, and
stale/revoked authority. A deterministic synthetic 10,000-result test requires
p95 filtered page latency at or below 250 ms.
