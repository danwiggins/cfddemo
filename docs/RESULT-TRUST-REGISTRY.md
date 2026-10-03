# Protected result trust registry

Status: local synthetic trust authority for development result keys. It does
not authorize real provider operation, clinical use, scientific claims,
export, or Epic D release, and it is not production key custody.

`ResultTrustRegistry` (`evidence_inspector/result_trust_registry.py`) is the
protected, forward-only authority for result trust that the E12 plan lists as
a blocker. Before it, D07 and E04 took a caller-supplied trust document plus a
pin. A revocation reached only instances opened with the new document, and an
old, internally consistent document and its pin could revive revoked
comparisons.

## Threat model

The process and user boundary is the trust boundary, as in the D03, D05, and
D07 registries. Seals detect accidental or naive replacement of methods,
pinned callables, result constructors, and instance authority state.
In-process code mutation and same-user filesystem races are out of scope.
Caller-supplied objects, rollback to an older journal or backup, interrupted
writes, and concurrent readers and writers are in scope.

Production key custody, external release keys, and signed administration are
out of scope.

## Schema versions and namespaces

A `v1` registry (`traceback.result-trust-registry-metadata.v1`, journal entry,
receipt, snapshot and backup all `v1`) holds `development-synthetic` result
keys only. Every registry created before the `development-local` namespace is
`v1`; it reopens with its retained ID, epoch and head and verifies v1/v2
bundles exactly as before (frozen fixture `tests/fixtures/result_trust_registry/v1/`).
A `v1` registry refuses `development-local` keys and `devlocal-result-…`
revocations; it keeps accepting synthetic keys as `v1` entries, which older
code still reads.

A `v2` registry is created only on request: `ResultTrustRegistry(root,
create_version=2)`. Its metadata, journal entries (`namespace` per key),
receipts, snapshot and backup are all `v2`, under their own hash-domain tags.
It accepts `development-synthetic` (`dev-result-…`) and `development-local`
(`devlocal-result-…`) result keys. The key ID is derived from the namespace,
so one key is valid for exactly one namespace. `ResultTrustSnapshotV2` carries
a `DevelopmentTrustDocumentV2` and `data_origin` (the sorted origins its keys
sign: `synthetic`, `local_unqualified`) in place of `synthetic_only`. A journal
line of the other version fails closed on load.

The E04 catalog accepts both snapshot versions and binds a v2 snapshot under
its own digest domain. D07, cohort import and the composite authority fence
still accept only v1 snapshots, so they fail closed on a v2 registry.

## Event model

The journal is append-only and hash-chained from a genesis digest over the
immutable metadata (registry ID, random epoch, namespace, purpose). Each entry
is one event:

- `add_key`: one public key with purpose `result`: a `PublicTrustedKey` in
  `development-synthetic`, or (v2 registries only) a `PublicTrustedKeyV2` in
  either development namespace. The key must be exact, not revoked, and its key ID must be the
  namespace- and purpose-bound ID of its public key (`trusted_key_id`). Release
  keys are rejected: this is a result trust authority, and D07 and E04 verify
  only `result` signatures.
- `revoke_key`: one `dev-result-…` key ID (v2 registries: also
  `devlocal-result-…`), with no public key.

Rules, enforced when appending and again when loading the journal:

- Revocation is permanent. A revoked key ID can never be re-added, and there
  is no un-revoke event.
- Revoking a key ID that was never added records a permanent tombstone, so the
  ID can never be added later.
- Re-adding an identical active key, or re-revoking a revoked ID, is a no-op
  that appends nothing (`applied=false` on the receipt). The journal never
  contains a re-add or a re-revoke; one that does fails closed on load.
- Any other entry for an existing key ID is rejected. Key IDs are derived from
  the public key, namespace, and purpose, so this can only happen through a
  forged or corrupted input.
- At most 32 keys (D07's result trust bound, revoked keys included), 192
  tombstones, and 256 events. Tombstones have their own bound so they can
  never use up the capacity reserved for revoking every added key; revoking an
  added key always fits.

The current trust is the fold of the journal: one `DevelopmentTrustDocument`
whose keys are sorted by ID, with revoked keys kept and marked `revoked`.

### Authorization

Additions are authorized by holding an open registry handle, which requires
the retained registry ID, epoch, and head. They are not signed by a separate
trust-administration key. Under this threat model, any code that can call
`add_key` runs in the same process as whoever would hold an administration
key, and development keys have no custody story, so a second key would add
ceremony without adding a boundary. A signed administration key becomes
meaningful with production key custody, which is out of scope; it is listed
as an open decision below. The open handle is the capability and there is no
read-only handle, so every D07 registry bound to a trust registry holds a
handle that can also add keys.

Revocation needs no extra authority. It only ever removes trust, and the
registry never restores it, so making revocation easy is the fail-safe
direction.

## Forward-only reads

- Opening an existing registry requires the independently retained registry
  ID, epoch, and current head. An older head is rejected. A new root refuses
  inherited expectations.
- A process-wide head fence, keyed by registry ID and epoch rather than by
  root inode (the sibling registries key by inode), rejects any journal that
  does not contain the newest head this process has seen for that registry.
  That covers a journal truncated in place and a restore of an older backup
  into another directory. Consequence, deliberate: two roots with the same
  registry identity in one process are held to one head. A same-identity copy
  that is behind the newest head this process has seen fails closed with
  `state rollback detected` even though it is internally consistent, and only
  one of two such roots can advance. A trust copy that is behind would present
  revoked keys as active, which is the hazard this registry exists to close,
  so this is required behaviour here, not the false rollback it would be for a
  non-trust registry. D07 binds its trust registry by the same (ID, epoch) key,
  so the fence and the binding agree on what 'the same trust registry' means.
- Across processes, the retained head is the defence, as in the sibling
  registries.

Readers bind the identity and head they read:

- `read_fence()` is a context manager that holds the shared registry lock for
  the caller's body and yields one `ResultTrustSnapshot` (registry ID, epoch,
  state version, head, document, and the SHA-256 of the document's
  `development_trust_document_bytes`). No trust event can commit until the body
  exits, so a value built inside the body is consistent with the yielded head.
- `current_trust()` returns the snapshot, built under the lock.
- `current_trust_store()` returns the snapshot and a fresh `TrustStore` built
  from it with `load_development_trust`, also under the lock.

The lock is not reentrant on a thread. `flock` converts a lock in place, so a
nested acquisition (for example a revoke inside a held read fence) would
silently upgrade or release the outer lock. The registry raises instead.

Lock order for consumers is: linkage authority fence, then the trust read
fence, then the consumer's own registry lock. Code that holds a trust read
fence must not open a linkage fence or write linkage, and must not call a D07
registry (which would re-enter the trust lock and raise).

## Storage

Storage follows the D03 decision registry at D05 seal parity: a private `0700`
root, `0600` owner-only files, descriptor-relative publication with fsync and
hard-link adoption, inode-bound control files (lock, metadata, journal), a
process-private instance seal, sealed class methods, pinned authority
callables (`trusted_key_id`, `development_trust_document_bytes`,
`load_development_trust`), and sealed result-constructor and helper aliases.
There is no whole-module namespace seal; a test pins that Python's
`__warningregistry__` does not disable the registry.

Events are under 1 KB, so they are stored inline in the journal; there is no
objects directory. The journal is the commit point. A failed append truncates
any torn suffix on any exception (an interrupt keeps its own type), and the
event can be retried. A journal with a torn tail from a crash fails closed on
reopen until an operator runs `recover_torn_journal_tail`. Creation and restore
are staged, and owned `.tmp-<32 hex>` names in the root are swept under the
exclusive lock. This registry is the source of the lock-descriptor ordering
and truncate-on-any-exception fixes the family now shares; see
`docs/REGISTRY-STORAGE.md`.

`backup_bytes()` returns canonical metadata, state, and journal. `restore()`
validates the chain and the event rules, requires the retained ID, epoch, and
head and an empty target, and removes the target if anything fails, including
the final open. Restore cleanup is best-effort, as in the sibling registries.

## D07 wiring

`RepeatabilityComparisonRegistry` accepts `result_trust_registry=` instead of
`result_trust_document=` and `expected_result_trust_sha256=`. See
`docs/REPEATABILITY-COMPARISON-REGISTRY.md` ("Result trust").

## E04 and runner wiring

`ResultCatalog` accepts `result_trust_registry=` in place of `trust_store=`
(exactly one of the two). See `docs/RESULT-CATALOG.md` ("Result trust"). Against
the follow-up analysis written with this registry:

1. Done. Each catalog verification builds a fresh `TrustStore` from the snapshot
   its read fence yields. The `TrustStore.resolve` pin stays.
2. Done, as a new contract version. `traceback.catalog-authority.v2` sets
   `trust_snapshot_sha256` to a digest of the registry ID, epoch, state version,
   head, and document digest. The TrustStore path keeps
   `traceback.catalog-authority.v1`. D06 rebuilds a retained authority from its
   digest across both versions (`CATALOG_AUTHORITY_SCHEMA_VERSIONS`).
3. Done, with the opposite lock order. `CatalogLiveReader` binds the registry
   instance and its (ID, epoch) instead of `id(_keys)` and the key tuple, and
   holds the trust fence through reverification and return. The order is the
   catalog `_connection_lock` first, then the trust read fence, not trust first:
   D06 already held the catalog connection lock before the TrustStore lock, and
   D06 and E06 call the catalog while holding that connection lock, so trust
   first would deadlock against them (a mutation test inverts the order and
   the D06/E04/D07 composition test hangs).
4. Done. Import, preparation, staging, adoption, and reference verification
   each hold one fence through return; a trust event between preparation and
   adoption fails the adoption.
5. Done for `verify`: `traceback verify BUNDLE --trust-registry ROOT
   --trust-registry-id ID --trust-registry-epoch EPOCH --trust-registry-head
   HEAD` verifies against the registry's current trust under its read fence,
   and `--trust-store` still works. A retained head older than the registry's
   is refused. The `assets` commands keep `--trust-store` only: they verify
   release-purpose signatures, and this registry holds result keys only and
   rejects release keys, so a registry option there could never verify.

Full lock order, as composed and tested: linkage fence, D05 lock, catalog
connection lock, result-trust read fence, D06 root, then E06's lock; D07 takes
linkage fence, result-trust read fence, then its own lock. A thread holding a
trust read fence taken directly from the registry must not call the catalog.

This change also fixes a race in the registry: a trust event updates the
instance's trusted head and then its instance seal, and the integrity check at
the start of a public call on another thread could read the new head with the
old seal and fail with `authority state changed`. The check now runs under the
process registry lock that trust events hold.

## Open decisions

- Signed trust administration (a pinned administration key for additions)
  once production key custody exists.
- Whether the TrustStore path in E04 and the fixed-document D07 path are
  removed now that both accept the registry.
- Whether a catalog root persists its trust-registry binding, so a catalog
  opened once on the registry path can never be reopened with a caller-held
  `TrustStore` or another registry (a catalog schema v4 migration).
