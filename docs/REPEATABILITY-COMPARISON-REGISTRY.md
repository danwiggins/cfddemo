# Protected D07 comparison registry

Status: local synthetic D07 persistence contract. It does not authorize real
provider operation, clinical use, scientific claims, export, or Epic D release.

`RepeatabilityComparisonRegistry` is the protected D07 comparison discovery
store that E12 requires as `d07_comparison_registry` (see
`docs/E12-INTEGRATION-PLAN.md`). It replaces caller-owned D07 comparisons and
values with one registry-scoped selector whose comparison is re-derived
against live authority on every read.

## Threat model

The process boundary is the trust boundary, as in the D05 cohort registry and
the D03 decision registry. Seals detect accidental or naive replacement of the
registry's methods, pinned authority callables, result constructors, and
instance authority state. In-process code mutation (seal tables, function code,
the module dictionary) is out of scope. Filesystem tampering, rollback,
interrupted writes, concurrent linkage writers, and caller-supplied objects are
in scope.

## Registration derives the comparison

`register_comparison` accepts exact anchor and member `LongitudinalRecord`s, the
`LongitudinalAnchorPolicy`, both signed `ComparisonObservation`s, the
`RepeatabilityEnvelope`, and the policy, authority-head, envelope, evidence,
protocol, and repeatability-authority pins. It never accepts a comparison, a
D03 decision, an evaluation time, a linkage store, or a result trust document.
While one `ProviderLinkageStore` authority fence is held, it:

1. advances and reads the linkage store's authority clock, which becomes the
   comparison's `evaluated_at`;
2. derives the D03 member decision with the pinned `decide_longitudinal_member`
   and rejects the D03 invalid-input sentinel;
3. derives the comparison with the pinned `compare_repeatability_in_fence`,
   using the registry's linkage trust pins and its configured result trust
   document;
4. captures inputs, derived decision, and comparison as one canonical
   `RegisteredComparisonObject`, whose validator binds the comparison's record,
   policy, decision, and envelope digests to the stored inputs;
5. replays the stored bytes once (below) before publication; and
6. publishes the object under the exclusive registry lock, appends one
   hash-chained journal entry, reloads the committed state, and returns a
   receipt with the comparison digest, availability, and classification.

Unavailable comparisons (`outside_envelope`, D03 non-eligible outcomes,
signature or identity failures, expired evidence) are registered too, so E12
can show the exact suppressed state. The envelope is required; a missing
envelope is represented by the absence of a registration, not by an
`evidence_missing` row. Exact re-registration of the same bytes is idempotent.

### Why the D03 decision is derived, not taken from the D03 registry

`compare_repeatability` already replays the D03 member decision against live
linkage on every evaluation, so a forged or stale caller decision cannot
produce a comparison. Deriving it inside the registry removes the caller
object entirely. Requiring a D03 registry selector would couple two registries'
locks and fences for no added authority, and the D03 registry has no
member-level selector. E12 binds the two by digest: every D07 comparison and
selector row carries `d03_decision_sha256`, which equals the digest of the
matching member decision in a resolved D03 series under the same linkage state
(tested).

## Every read replays

`resolve(selector_id)` holds the linkage authority fence and a shared registry
lock, loads the committed object, reads the live authority time, and runs the
pinned `compare_repeatability_in_fence` once on the stored inputs at that live
time. The output, with only `evaluated_at` set back to the stored registration
time, must be byte-identical to the stored comparison. Otherwise `resolve`
raises `RepeatabilityComparisonRegistryStale` and returns nothing. Linkage
store `Unsafe` and schema errors that reach the registry keep their own types
and are not reported as stale; a store failure that D03 itself absorbs into its
closed decision surfaces as a non-replaying decision, and so as stale.

A comparison becomes stale on any linkage commit (the D03 decision binds the
linkage snapshot head), linkage approval expiry, result trust change or key
revocation, an envelope that expires after registration, an envelope that was
not yet valid at registration and has since opened, or a live time earlier
than registration.

The returned `RegisteredRepeatabilityComparison` binds registry identity, state
version and head, selector, object digest, comparison digest, the stored
comparison, and `replayed_at`, with `replayed_against_live_linkage=true`.

### Evaluation time

`comparison.evaluated_at` is the linkage store's authority time at
registration, never a caller value: a caller-chosen time could be backdated
into an expired envelope's window. On replay, the stored time is used only for
byte identity; the gates run at the live authority time, so currency is always
judged now. `replayed_at` is the as-of instant of each read, and E12 should use
it, not `evaluated_at`, as the comparison's current time. The authority clock
has one-second resolution and never moves backwards.

## Fence composition

A probe confirmed that `compare_repeatability` cannot run inside a held linkage
fence on its available path: its own `authority_read_fence` raises
"linkage store authority fence requires an idle connection". Its unavailable
paths do not open the fence and run fine inside one.

D07 therefore gains `compare_repeatability_in_fence`, an additive
already-fenced variant with the same contract and gates. It requires that the
calling thread holds the store's `authority_read_fence`, checked at entry and
again before the final replay and construction; otherwise it raises
`LongitudinalDecisionReplayError` before any result. `authority_read_fence`
now records its holder thread for exactly its body, so another thread's fence
and other store transactions such as `fenced_active_snapshot` do not satisfy
the check. `compare_repeatability` is unchanged.

The registry holds one fence across authority-time capture, D03 derivation,
D07 evaluation, pre-publication replay, the registry lock, publication, journal
append, reload, and receipt construction. `resolve` and `list_selectors` hold
one fence across load, replay, and construction of the returned value. A
linkage writer cannot interleave with a registry commit or return; tests start
a writer inside the fence and observe it still blocked when the receipt or
resolved value is built.

The returned comparison is still an as-of artifact. A consumer that emits a
delta or trend after `resolve` returns must hold the composite E12 authority
fence or call `resolve` again inside it. This registry does not provide that
composite fence.

## Selector projection

`list_selectors` returns a bounded page (1 to 100 rows) of opaque
`d07_comparison_…` selectors ordered by selector. Each row carries only the
object, comparison, D03 decision, anchor-policy, and envelope digests; the D03
outcome; the D07 availability and classification; and `current|stale` from a
replay under the same fence. Rows never contain values, deltas, uncertainty,
denominators, result IDs, signing key IDs, provider, subject, collection,
specimen, run, or linkage tokens, paths, or free text. If the live linkage
authority is not current, every row is stale.

## Result trust

The result trust document and its pin are registry configuration supplied at
open and restore, not stored per object and not bound in metadata. Replacing
it, for example to revoke a key, is therefore possible by reopening the
registry, and makes every comparison whose output changes stale. The instance
seal re-hashes the configured document on every call.

## Storage and bounds

Storage follows the D03 decision registry: a private `0700` root, `0600`
owner-only files, descriptor-relative exclusive publication with fsync and
hard-link adoption, a journal chain from a genesis digest over immutable
metadata, required retained registry ID, epoch, and head on reopen, a
process-wide monotonic head fence against rollback, inode-bound control files,
a process-private instance seal (including result trust), and sealed class
methods, pinned authority callables (linkage fence, snapshot, authority time,
D03 member decide, invalid-input sentinel, D07 in-fence evaluator, comparison
digest, result trust digest), and sealed result-constructor and helper
aliases. There is no whole-module namespace seal; Python writes
`__warningregistry__` into module globals, and a test pins that this does not
disable the registry.

The journal is the commit point. A failed journal append truncates any torn
suffix; reads tolerate at most one uncommitted object, which the next
registration removes unless it holds the exact bytes being registered. A failed
restore removes the partial target it created.

One object is about 43 KB for a fixture pair and is bounded to 1 MiB of
canonical bytes; the registry holds at most 10,000 comparisons and 256 MiB of
committed object bytes, checked before publication. At fixture size the byte
bound binds first, near 6,000 comparisons. Object and backup parsing enforce
byte, depth, node, collection, and string budgets before validation and
require an exact canonical round trip.

`backup_bytes` and `restore` follow the D03 registry. Restore requires the
independently retained registry ID, epoch, and state head, the same linkage
store identity and trust pins, a matching result trust document and pin, and an
empty target. A backup contains protected records, signed observations, and
values, and is not an export artifact.

## Not in scope

The protected `AnchorPolicyRegistry` that would make envelope and policy pins
independent of the registering caller, D10 live integration, and the composable
E12 authority-fence adapter are separate prerequisites in the E12 merge order.
Until the policy registry exists, the envelope pins are registration inputs;
E12 must bind each comparison's envelope digest to the policy registry's
resolved envelope. All fixtures are synthetic/local, and outcomes describe
technical comparability only.
