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

Only the `development-synthetic` namespace exists. Production key custody,
external release keys, and signed administration are out of scope.

## Event model

The journal is append-only and hash-chained from a genesis digest over the
immutable metadata (registry ID, random epoch, namespace, purpose). Each entry
is one event:

- `add_key`: one `PublicTrustedKey` in `development-synthetic` with purpose
  `result`. The key must be exact, not revoked, and its key ID must be the
  namespace- and purpose-bound ID of its public key (`trusted_key_id`). Release
  keys are rejected: this is a result trust authority, and D07 and E04 verify
  only `result` signatures.
- `revoke_key`: one `dev-result-…` key ID, with no public key.

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
any torn suffix, and the event can be retried. A journal with a torn tail
from a crash fails closed.

`backup_bytes()` returns canonical metadata, state, and journal. `restore()`
validates the chain and the event rules, requires the retained ID, epoch, and
head and an empty target, and removes the target if anything fails, including
the final open. Restore cleanup is best-effort, as in the sibling registries.

## D07 wiring

`RepeatabilityComparisonRegistry` accepts `result_trust_registry=` instead of
`result_trust_document=` and `expected_result_trust_sha256=`. See
`docs/REPEATABILITY-COMPARISON-REGISTRY.md` ("Result trust").

## E04 follow-up

E04 (`evidence_inspector/result_catalog.py`) is not rewired here. It holds a
live `TrustStore`, pins `TrustStore.resolve`, reads `trust_store._lock` and
`_keys` for its authority snapshot, and `CatalogLiveReader` binds the store's
identity and a snapshot of `_keys`. Wiring it would mean:

1. `ResultCatalog` takes a `ResultTrustRegistry` in place of the store, and
   verification runs on a fresh `TrustStore` from `read_fence` (or
   `current_trust_store`) per operation. The `TrustStore.resolve` pin stays.
2. `CatalogAuthoritySnapshot.trust_snapshot_sha256` binds the trust registry
   ID, epoch, and head (or the snapshot's document digest) instead of hashing
   `_keys`, so a revocation changes the catalog authority digest.
3. `CatalogLiveReader` binds the trust registry instance and its identity
   instead of `id(_keys)` and the `_keys` tuple, and each verified read holds
   the trust read fence through the reverification and return. Lock order:
   trust read fence, then the catalog `_connection_lock`.
4. Import and publication reverify under the same fence, so a key revoked
   mid-import cannot be published.
5. The CLI and runner paths that load a trust file (`traceback_runner/cli.py`)
   keep the document path or gain a registry option; that is a product choice.

That touches the catalog's authority snapshot, the reader seal, and several
tests that build a `TrustStore` directly, so it is a separate change.

## Open decisions

- Signed trust administration (a pinned administration key for additions)
  once production key custody exists.
- Whether E04 and the runner CLI move to the registry, and whether the
  fixed-document D07 path is then removed.
