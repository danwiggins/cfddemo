# Shared storage behaviour

Status: implemented for the D03-pattern registries. Code:
`evidence_inspector/registry_storage.py`. Tests: `tests/registry_storage_checks.py`,
wired into each registry's test module (`test_storage_*`).

## Scope

The registries that follow the D03 storage pattern (private `0700` root,
`0600` owner-only files, append-only hash-chained journal, optional
content-addressed `objects/` directory):

| Registry | Module |
| --- | --- |
| D05 cohort | `cohort_registry.py` |
| D03 decision | `longitudinal_decision_registry.py` |
| D07 comparison | `repeatability_comparison_registry.py` |
| D09 policy | `denominator_policy_registry.py` |
| D10 context | `covariate_context_registry.py` |
| E06 source | `result_view_source_registry.py` |
| Family source | `measurement_source_artifact_registry.py` |
| Anchor policy | `anchor_policy_registry.py` |
| Projection policy | `projection_policy_registry.py` |
| Reader authorization | `reader_authorization_registry.py` |
| Result trust | `result_trust_registry.py` |

The saved-comparison registry (`longitudinal_comparison_registry.py`) has its
own candidate-record recovery and is not covered here. Neither is the E04
result catalog.

Threat model: in-process code mutation and same-user filesystem races are out
of scope, as for the registries themselves. These behaviours handle crashes
and caught failures. They are not a defence against a hostile process running
as the same user.

## Behaviour

1. **Staged root creation.** A new root is built in a hidden sibling,
   `.<name>.staging-<32 hex>`. That covers the `objects/` directory, the lock,
   journal and metadata files, and the first metadata publication under the
   lock. The sibling is published with one `rename(2)` after the
   expected-identity checks pass. The final path therefore either does not
   exist or holds a complete registry. A retry after an interrupted creation
   creates a fresh registry, and no identity was ever handed out for the
   abandoned one. `rename(2)` keeps the inode, so registries whose head fence
   is keyed by root inode see the same identity. An existing final path is
   never staged over; it is opened through the normal checks.
2. **Staged restore.** `restore()` (and the reader registry's `create()`, which
   shares `_materialize`) writes into a staging sibling, publishes it with
   `rename(2)` only once it is complete, then reopens it through the normal
   checks. A failed reopen still removes the target, as before. An interrupted
   restore never leaves a partial target that blocks a retry at that path.
3. **Torn journal tail on reopen: fail closed, operator recovery.** Reopen
   never repairs a torn tail. It fails closed with "journal is incomplete".
   Each registry has an explicit maintenance classmethod:

   ```python
   removed = Registry.recover_torn_journal_tail(
       root,
       expected_registry_id=...,
       expected_registry_epoch_sha256=...,
       expected_state_head_sha256=...,
   )
   ```

   It takes the exclusive registry lock without waiting, so recovery cannot
   deadlock. Another thread holding the registry's process lock is refused
   ("in use"). The process lock is an RLock, so the caller's own fence
   re-enters it; the non-blocking `flock` then refuses it, as it refuses any
   live instance in another process. It checks the private root, lock,
   metadata and journal. The metadata identity must equal the retained
   identity, and every complete line must chain from the metadata genesis to
   exactly the retained head. Only then does it truncate the bytes after the
   last newline and fsync. It returns the number of bytes removed (`0` when
   there is no torn tail). A corrupt committed line, a wrong identity or a
   wrong head is refused, and nothing is truncated. Committed entries are
   newline-terminated and fsynced before any receipt, so this never removes a
   committed entry. Reopen with the same retained values afterwards. The
   normal open still runs every registry-specific check.

   Why option A (explicit) over automatic self-repair: the Fable review of E06
   ruled that self-repairing on reopen is a divergence the family adopts
   uniformly or not at all, and E06 was reverted to fail closed. Keeping reopen
   fail-closed preserves that ruling. A torn tail stays visible to the
   operator, who supplies the retained head the remaining chain must reach.
   That head is the same authority reopen already requires.
4. **Owned temporary names (D05 rule).** A `.tmp-<32 hex>` name inside a
   registry's private root or `objects/` directory is owned by the registry.
   It is unlinked unconditionally under the exclusive lock, at startup and
   before every write. `unlink(2)` never follows a symlink and never destroys
   data that has another link. A directory under that name makes `unlink`
   fail, so recovery fails closed ("recovery is unsafe"). The result-trust
   registry has no `objects/` directory and sweeps its root.
5. **Failed append truncates on any exception.** If the journal append raises
   anything, including `KeyboardInterrupt` between partial writes, the
   journal is truncated back to its pre-append size and fsynced. An `OSError`
   becomes the registry's "journal append failed" error. Any other exception
   is re-raised with its own type.
6. **Lock descriptor read under the process lock.** `_lock` reads the lock
   descriptor only after taking the process lock, which `close()` also holds.
   A waiter therefore cannot `flock` a descriptor number that a concurrent
   `close()` freed and the process reused.
7. **Instance seal compared under the instance process lock.** Accepting a
   new head assigns `_trusted_head_sha256` and then re-seals the instance,
   inside `_lock`, which holds the instance's `_process_lock`. `close()`
   clears descriptors under that same lock. The integrity check that every
   public method runs first compares the instance with its seal under it
   too. Without that, a reader on another thread could see the new head with
   the old seal (or a half-closed instance) and fail with "authority state
   changed" while a concurrent registration is in flight, for example two
   callers adopting the same bytes. The check takes the instance lock, not
   the module `_REGISTRY_PROCESS_LOCK`, so a finalizer closing an unreachable
   registry never waits on a lock another thread holds. Rollback and tamper
   detection are unchanged: the comparison is the same, and only its timing
   is serialized with the writer.

Items 5 and 6 port the result-trust registry's hardening to the family.
Item 7 replaces the result-trust registry's module-lock version of the same
guard and applies it to all 12 registries.

## Limits

- A hard crash during staging leaves a `.<name>.staging-<32 hex>` sibling in
  the parent directory. It is never adopted and never blocks a retry. An
  operator may delete it when no creation or restore is running.
- A failure or interrupt after the rename but before the constructor or
  restore returns, including one raised as the registry lock or the
  authority fence exits, is cleaned up by inode: the directory the root
  descriptor holds is removed under whichever name it now has. The
  constructor clears its cleanup handle only as the last statement, after
  the lock and every fence have exited. A new root's identity was
  never returned, so a retry creates a fresh one. Only a hard crash in that
  window leaves a complete registry whose identity the caller never received.
  That identity is in `registry-metadata.json`, and the head is the genesis
  (or, for restore, the backup's retained head).
- A half-built root left by code from before this change (a root without
  metadata) still fails closed and needs manual removal.
- `rename(2)` replaces an empty directory created at the final path in the
  instant between the absence check and the rename. That is a same-user race
  and out of scope.
- Constructors resolve a symlinked parent (for example macOS `/tmp`) before
  staging, as `mkdir` did before this change. `restore()` and the reader
  registry's `create()` still refuse a symlinked parent, as they did before;
  pass a resolved path.
