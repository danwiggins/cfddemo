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
not cataloged.

Object publication is an exclusive atomic rename after file and directory
flushes. The SQLite index uses WAL, full synchronous durability, and one
immediate transaction for the public reference plus its private aliases. A
failure before commit leaves no indexed partial result; a process-ending crash
can leave only an unreferenced content-addressed object, which a retry verifies
and safely adopts.

## Read model

`CatalogResultRef` contains immutable bundle, method, registry, and authority
identities. Its result ID is derived from the bundle digest and method-definition
digest. Execution, information, trust, and qualification are independent closed
states; consumers must not infer one from another.

Opaque display, run, and timepoint aliases live in a separate protected table.
They are accepted only in their controlled formats and can select results, but
the mapping is not returned in result references. No input path, bundle path,
run token, or protected-identifier mapping enters the public reference.

Queries normalize and deduplicate bounded method/state filters. They support
method, state, and exact opaque-alias selectors, deterministic result-ID order,
keyset cursors, and a maximum page size of 100. Empty pages distinguish an empty
catalog from a filtered query with no matches.

## Scope

This is research inspection infrastructure. It is not a frontend, network API,
clinical interpretation layer, donor registry, or plugin interface.

The adversarial test suite covers idempotence, conflicting identities, source
mutation, root replacement, traversal, symlinks, special and extra files,
oversize inputs, simulated pre-commit failure, unsupported catalog schemas, and
stale/revoked authority. A deterministic synthetic 10,000-result test requires
p95 filtered page latency at or below 250 ms.
