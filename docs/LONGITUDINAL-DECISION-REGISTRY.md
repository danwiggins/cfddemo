# Protected D03 decision registry

Status: local synthetic D03 persistence contract. It does not authorize real
provider operation, clinical use, scientific claims, export, or Epic D release.

`LongitudinalDecisionRegistry` is the protected D03 decision discovery store
that E12 requires (see `docs/E12-INTEGRATION-PLAN.md`). It replaces
caller-owned D03 decisions, digest/outcome pairs, and dictionaries with one
registry-scoped selector whose decision is replayed against live authority on
every read.

## Registration derives the decision

`register_series` accepts exact anchor and member `LongitudinalRecord`s, the
`LongitudinalAnchorPolicy`, and the expected policy and authority-head pins. It
never accepts a decision. While one `ProviderLinkageStore` authority fence is
held, it:

1. evaluates the series with the pinned `decide_longitudinal_series`;
2. rejects the D03 invalid-input sentinel;
3. captures the inputs and the derived decision as one canonical
   `RegisteredSeriesObject`, whose validator binds the decision's anchor,
   ordered membership, and policy digest to the stored inputs;
4. replays the stored decision once to prove it is replayable; and
5. publishes the object under the exclusive registry lock, appends one
   hash-chained journal entry, reloads the committed state, and returns a
   receipt.

A caller therefore cannot register a decision that D03 did not produce for
those inputs, and cannot re-point a real decision at another member. Exact
re-registration of the same bytes is idempotent. Registration under a later
linkage snapshot produces different decision bytes and a new selector.

## Every read replays

`resolve(selector_id)` holds the linkage authority fence and a shared registry
lock, loads the committed object, and calls the pinned
`replay_longitudinal_series_decision` against the live store with the
registry's trust pins. Only an exact replay returns a
`RegisteredLongitudinalSeriesDecision`, which binds registry identity, state
version and head, selector, object digest, decision digest, and the decision,
with `replayed_against_live_linkage=true`. Any linkage advance, correction,
tombstone, trust change, or changed record makes replay fail, and `resolve`
raises `LongitudinalDecisionRegistryStale` without returning a decision. The
fence is held through construction of the returned value, so no linkage
mutation lands between replay and return.

The replayed decision is still an as-of decision: it binds one linkage snapshot.
A consumer that emits a delta or trend after `resolve` returns must hold the
composite authority fence described in the E12 plan, or call `resolve` again
inside it. This registry does not provide that composite fence.

## Selector projection

`list_selectors` returns a bounded page (1 to 100 rows) of opaque
`d03_series_…` selectors, ordered by selector. Each row carries only the object,
decision, policy, and anchor-key digests; member count; per-outcome counts in
canonical outcome order; the delta-allowed count; and `current|stale` live
authority state from a replay under the same fence. Rows never contain result
IDs, provider, subject, collection, specimen, run, analysis, linkage tokens,
paths, or free text.

## Storage and bounds

Storage follows the D05 cohort registry: a private `0700` root, `0600`
owner-only files, descriptor-relative exclusive publication with fsync and
hard-link adoption, a journal chain that starts at a genesis digest over the
immutable metadata, required retained registry ID, epoch, and head on reopen,
a process-wide monotonic head fence against rollback, inode-bound control files,
a process-private instance seal, and sealed class and module callables,
including the pinned D03 decide and replay functions.

The journal is the commit point. An object without a journal entry is the
remnant of an interrupted registration. Reads tolerate at most one. The next
registration removes it unless it holds the exact bytes being registered, which
are adopted. More than one uncommitted object, a missing committed object, a
non-object file, or a digest mismatch fails closed.

One object holds at most 1,000 members (the D03 series bound) and 32 MiB of
canonical bytes; at a measured 19.7 KB per member, a full 1,000-member series is
about 19 MiB. The registry holds
at most 10,000 series and 256 MiB of committed object bytes, checked before
publication. Object and backup parsing enforce byte, depth, node, collection, and
string budgets before validation and require an exact canonical round trip.

`backup_bytes` and `restore` follow the cohort registry. Restore requires the
independently retained registry ID, epoch, and state head, the same linkage
store identity and trust pins, and an empty target. A backup contains protected
records and is not an export artifact.

## Not in scope

D07 comparison discovery, D10 live integration, and the composable E12
authority-fence adapter are separate prerequisites in the E12 merge order.
All fixtures are synthetic/local, and outcomes describe technical
comparability only.
