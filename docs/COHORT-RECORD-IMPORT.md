# Verified cohort record import (D06)

D06 adds a protected local binding index between D05 cohort members and the
existing immutable aggregate-result catalog. It accepts only the exact signed
result-bundle v2 schema with fragment-measurement v1 through an explicit reader
registry. A bundle is copied through descriptor-relative, no-follow opens,
bounded by the result catalog's fixed eight-file inventory and 36 MiB total
limit, and verified against independently provisioned offline result-key trust.
The signature purpose, current key revocation state, manifest/content digests,
derived chart/report, method identity, and current method authority are checked
before a result can become visible in the index.

The cohort layer pins the exact result-catalog object, storage identity, trust
authority (the catalog's caller-held `TrustStore` or its protected
`ResultTrustRegistry`), reader authority, live D05 registry, and linkage store.
Its authority fence holds, in order, the linkage fence, the D05 lock, the
catalog connection lock, result trust (the TrustStore lock, or the registry
read fence through the catalog's `trust_authority_fence`), and the binding
root; catalog calls inside it reuse the held trust head. A binding's retained
catalog authority is rebuilt from its digest across catalog-authority v1 and
v2, so bindings made before a catalog moved to the registry keep replaying. Import and read
APIs accept only a registry selector and cohort version; they resolve the exact
registered history under one linkage/registry authority fence and never accept
a caller-built manifest sequence. Each verified result is bound to one exact
registered member, the registry ID/epoch/state head, the current linkage
snapshot, and the inclusion, exclusion, missingness, and record-status policy
digests. The manifest is rechecked against independently pinned provider trust
before and after bundle verification. The bundle method definition must equal
the cohort's measurement anchor. Opaque result-catalog aliases are
deterministically derived from protected provider, analysis, run, and
collection tokens so the same result can be reused across immutable cohort
versions without exposing those tokens in the public result reference.

Bindings carry the digest-bound catalog storage, trust snapshot, reader
authority, and publication identity. They are canonical, append-only files in a
mode-0700 local directory; each file is mode 0600 and capped at 128 KiB. The
catalog supports at most 100,000
bindings. Duplicate exact imports are idempotent, while a different result for
the same member and manifest or the same result assigned to another member in
that manifest is a conflict. Reads recheck the live D05 authority, the exact
binding bytes, the configured reader-registry digest, the stored immutable
bundle, and current result-key trust. A later linkage change or key revocation
therefore withholds the binding rather than turning stale evidence into an
available longitudinal record.

`record_status_for_manifest(selector_id, cohort_version)` returns a bounded
canonical status artifact with exactly one item in D05 member order. Each item
is `available` with a live verified binding, `missing`, or `withheld` with a
typed safe reason and no result details. An available binding is the protected,
authority-fenced bridge from the D05 member commitment to the exact E04
`CatalogResultRef`; those two digests are distinct and must never be equated.
The artifact binds the registry identity/state head, cohort/version/manifest
digest, current linkage snapshot, current catalog/trust authority, complete
member coverage, and its own canonical digest. Structural corruption, a wrong
member identity, or an unverifiable catalog still fails the whole read.
`bindings_for_manifest(selector_id, cohort_version)` remains strict for callers
that require every selected record to verify.

Publication is coordinated across SQLite and the binding directory. The result
row is first durable but hidden in `pending` state. A same-directory mode-0600
journal is flushed, hard-linked to the final name without overwrite, and the
directory is flushed. D05 linkage, result trust, catalog storage, and exact
binding bytes are revalidated immediately before and after the atomic catalog
visibility transaction while the binding root remains exclusively locked. The
catalog transaction executes no coordinator or caller callback. Any failure
compensates the exact publication row and binding before releasing that lock,
while retaining the shared content-addressed object. A durable SQLite candidate
row is created before binding publication. It binds a unique operation identity
to the exact publication, result, recovery scope, manifest, binding and final
filename plus binding and marker digests. A private canonical rollback marker
is then flushed before catalog visibility and remains until the binding and
ownership commit completes. The database candidate is the recovery authority;
replaceable marker or journal bytes never authorize deleting a peer binding.
If marker creation or later cleanup fails, restart recovery uses the candidate
row to remove only that incomplete binding and exact ownership.
Startup recovery
enumerates hidden SQLite publications for the binding root's digest-bound
recovery scope independently of journal parsing. A retained pending journal is
an incomplete import and defaults to rollback even if SQLite reached `adopted`.
Missing, truncated, or substituted journals compensate the
exact durable publication and are removed with any linked final file; an empty
or partial final binding is never accepted.

Every binding root also owns a durable, scope-digest-bound reference to an
adopted coordinated result, including when the result bytes and visible catalog
row already existed. Recovery removes only that root's ownership row. A shared
result remains visible while any other root retains an adopted owner; the final
owner cleanup removes only the catalog reference and retains the immutable
content-addressed object bytes. Direct, non-coordinated catalog imports are not
deleted by binding-root recovery.

Caller-supplied method registry, authority head, and current capability are
captured with exact-type, bounded, zero-hook traversal before any catalog or
publication operation. Subclasses, instance shadows, private or extra state,
cycles, aliases, excessive depth, and oversized graphs fail without executing a
caller serializer or producing a catalog side effect. Import and read paths
invoke captured unbound authority functions and verify the
entire reachable catalog, trust-store, linkage-store, verifier, storage, class,
instance, and module-alias call chain. Replacing a validator cannot turn stale
D05 membership or a revoked result key into accepted evidence, including a
replacement that attempts to restore itself when called.
Python function code, defaults, keyword defaults, and closure cells are also
fingerprinted to diagnose in-process monkeypatching. These checks assume a
trusted package and process image. Arbitrary code execution that can rewrite
both a verifier and its expected fingerprint makes that process untrusted and
must be handled by package integrity, process isolation, and restart rather
than treating a recursive Python self-check as an authorization root. No data,
bundle-reader, plugin, or evaluation input receives a code-mutation capability.
Production and recovery paths do not execute caller callbacks. Fault-window
tests use an exact, non-subclassable package controller with immutable
configuration and fixed raise, exit, or synchronization actions. Catalog
instances pin its exact identity, primitive configuration, lock, and event
identities; every member is type-checked without caller dispatch before the
package invokes pinned unbound operations.

This implementation remains synthetic and local. The binding index contains
protected analysis and provider identifiers and must stay inside provider
managed encrypted storage and backup policy. It contains no raw sequence,
read-level data, free-text identity, network transport, clinical status, or
diagnostic interpretation. Development trust cannot establish provider
qualification; the downstream longitudinal capability remains disabled until
its separate dependency and qualification gates pass.

## Failure behavior and rollback

- Tampered, revoked, wrong-purpose, malformed, over-bound, symlinked, and
  unsupported-version bundles fail before result indexing.
- Invalid/stale D05 histories, nonmembers, wrong measurement anchors, and
  shadowed catalog/reader authority fail before cohort binding.
- Index corruption, unexpected files, root replacement, stale linkage, or
  current trust failure blocks reads with a sanitized error. Root identity is
  rechecked immediately before and after final publication.
- Rollback stops new D06 imports and returns to the prior application release.
  Existing immutable result objects and binding bytes remain retained for
  offline verification; they are not rewritten to make rollback succeed.

Focused evidence lives in `tests/test_cohort_import.py`; the pre-existing
result-catalog adversarial and 10,000-record query tests continue to exercise
bounded filesystem import, exact SQLite schema, concurrency, and pagination.
