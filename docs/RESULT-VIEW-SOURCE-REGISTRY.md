# Protected E06 result-view-source registry

Status: local synthetic E06 persistence contract. It does not authorize real
provider operation, clinical use, scientific claims, export, or Epic D release.

`ResultViewSourceRegistry` is the protected E06 source discovery store that E12
requires (see `docs/E12-INTEGRATION-PLAN.md`). It replaces a caller-owned
`ResultViewSource` sequence with an opaque registry-scoped selector and version.
Every protected read re-verifies the stored source against live D06/E04
authority and replays the E05 decision and the E06 source construction.

## Authority analysis

This section records what "current E06 replay authority" can mean in the code
as it stands. It was written before the registry was built and constrains what
the registry claims.

A `ResultViewSource` is one `VerifiedMeasurementRecord`, one pairwise E05
`CompatibilityDecision` that binds that record to a second record, four filter
identities derived from those two, a `DenominatorLedger`, and two labels.

Live authority that exists today:

- **D06 `CohortRecordCatalog.record_status_authority_fence`.** It holds the D01
  linkage fence, a shared D05 registry lock, the E04 catalog connection lock,
  the E04 trust-store lock, and the D06 root lock, and yields a
  `CohortManifestRecordStatus` for one cohort selector and version. Every
  available member carries an immutable `CohortRecordBinding` whose
  `CatalogResultRef` D06 re-verified against the live E04 object store and
  trust store inside the fence. A revoked result key turns the member into
  `withheld`. A trust-store, catalog-storage, linkage, or registry change
  either changes the status digest or makes D06 fail closed.
- **E04 `CatalogResultRef`.** It pins result ID, bundle digest, method
  reference, method-definition digest, and the method capability that was
  current at import (registry digest and version, authority head and revision,
  scope, `as_of`, qualification, display role, research inspectability, and
  provider eligibility).

Authority that does not exist today:

- **No durable method-authority store.** `CatalogVerificationContext` and
  `replay_current_capability` take a caller-supplied `MethodRegistry`,
  `AuthorityHead`, and expected head digest. Nothing in the process holds the
  current head independently. The registry can therefore bind the capability
  only to the head pinned in the D06/E04 binding. It cannot prove that head is
  still current; a later method-authority advance is invisible until a method
  authority store exists.
- **No compatibility-policy authority.** E05 is pure. `decide_compatibility`
  and `replay_compatibility_decision` take a caller-supplied
  `CompatibilityPolicy` and policy pin, as D03 does. "E05 replay" can only mean
  that the stored decision is exactly what E05 derives from the stored
  inputs, with the authority-head pin taken from the D06/E04 binding rather
  than from the caller.
- **Nothing derives a `DenominatorLedger`.** No module computes the E06 ledger
  from authority; every producer builds it by hand. D09 v3 already dropped the
  E06 ledger for this reason (`build_registered_cohort_denominator_summary`
  accepts no caller-authored ledger). The registry stores the ledger
  immutably and binds its digest, so alteration is detected, but it does not
  and cannot verify its counts.
- **Several record fields have no authority source.** `result_sha256`,
  `bundle_id`, `information_state`, the compatibility key's result schema,
  assets, semantics, and registered policy reference, and
  `effective_approval_ref` when the result is provider-eligible exist only in
  the caller's record. E04 stores none of them. `CatalogResultRef` does carry
  a `bundle_record_id`, but it is a different identifier from the E05
  `bundle_id`, and nothing maps one to the other. The method-definition object
  is verified only through its digest, which the binding pins. The key's
  measurement family, quantity, and unit are bound through that digest, because
  `VerifiedMeasurementRecord` requires them to equal the method's; only the four
  sub-fields above are unverified, and `caller_asserted_fields` lists exactly
  those four for each record.
- **Labels** are presentation text, privacy-checked by E06 and otherwise
  caller-authored.

Field classification used by the registry:

| Field | Verified against | Status |
| --- | --- | --- |
| Member commitment for the record's result | Live D06 status under its fence; exactly one member binds the result | Live |
| D06 binding (cohort registry ID/epoch, selector, version, manifest, linkage snapshot, publication, reader) | Live binding digest equality | Live |
| E04 catalog authority (storage, trust snapshot, reader registry) | Live D06 status `catalog_authority_sha256` equality | Live |
| Result ID, bundle digest, method reference, method-definition digest | Live `CatalogResultRef` equality | Live |
| Capability (registry, head, revision, scope, `as_of`, qualification, role, inspectable, eligible) | `CatalogResultRef` fields pinned at import | Live binding, import-time head |
| Execution `complete`, trust `verified` | D06 `available` means E04 indexed a complete result and re-verified its signature | Live |
| Counterpart record | Same checks as above against a second, distinct live member of the same cohort | Live |
| Method definition object | Digest equals the pinned method-definition digest | Digest-bound |
| Compatibility decision | Exact E05 re-derivation from stored records, stored policy and pin, and the binding's authority head | Pure replay |
| E06 source and filter identities | Exact E06 re-construction from the replayed decision | Pure replay |
| Compatibility policy and policy pin | Caller input | Not verified |
| `result_sha256`, `bundle_id`, `information_state`, compatibility key result schema, assets, semantics, and policy reference, `effective_approval_ref` | Caller input | Not verified |
| Denominator ledger | Caller input; digest bound | Not verified |
| Accessible and QC labels | Caller input; E06 privacy checks | Not verified |

The core identity fields (member, result, bundle, method, capability as
pinned, trust) have live authority, so the registry is built. Each resolved
source carries the fixed list of unverified fields
(`caller_asserted_fields`) and the literals `denominator_verified=false` and
`method_authority_head_current_verified=false`, so a consumer cannot mistake a
bound ledger or an import-time method head for a verified one. The list covers
the counterpart record as well as the subject: the E05 decision depends on the
counterpart's information state, the four unmapped compatibility-key
sub-fields, and its other unmapped fields.

Decision (Dan, 2026-10-01): a denominator ledger is authoritative only when
the analysis pipeline computes it. Until a pipeline derivation exists, every
E06 ledger is operator-entered and unverified. E12 must either hide it or show
it explicitly labelled "operator-entered, unverified", and must never present
it next to the D09 v3 counts as equivalent to them. This registry keeps
`denominator_verified=false` and accepts the ledger as caller input on that
basis.

## Registration derives the source

`register_source` takes a cohort selector and version, the subject record, a
counterpart record, a `CompatibilityPolicy` with its pin, a `DenominatorLedger`,
and the two labels. It never accepts a `ResultViewSource`, a decision, a member
commitment, or an authority-head pin. It:

1. captures every caller contract as exact bounded canonical bytes and
   re-parses it, so subclasses, private or extra Pydantic state, and oversized
   graphs fail before any authority read;
2. enters the D06 record-status fence for the cohort selector and version;
3. finds the one live available member whose binding carries each record's
   result, and fails if a result is bound to zero or several members or if the
   two records resolve to the same member;
4. requires each record to match its live `CatalogResultRef` (result, bundle,
   method, method digest, every capability field E04 stores, `complete`,
   `verified`);
5. derives the E05 decision with the pinned `decide_compatibility`, using the
   subject binding's authority head as the trusted head;
6. builds the E06 source with the pinned `bind_result_view_source`;
7. stores the inputs, the live commitments (cohort registry ID and epoch,
   selector, version, manifest digest, D06 `catalog_authority_sha256`, both
   member commitments, and both binding digests), and the derived source as
   one canonical object; and
8. publishes it under the exclusive registry lock, appends one hash-chained
   journal entry, reloads the committed state, and returns a receipt, all
   while the D06 fence is still held.

The selector is opaque and names one member slot: it derives from the
registry epoch, the cohort registry ID, the cohort selector and version, and
the member commitment. `selector_for_member` computes it without I/O. Each
distinct registration for the same slot (for example a different ledger,
counterpart, or label) appends the next source version, up to 16. Exact
re-registration is idempotent. Within one cohort selector and version, one
result maps to one member and one member to one result; the journal loader
enforces the same rule, so tampered or restored state cannot break it.

The scope of that rule is one cohort selector and version, which is the scope
of one E12 workspace. Across cohort versions the same result may appear under
a different member commitment, because D06 deliberately lets one verified
record bind to a new manifest version whose member contents changed.
Decision (Dan, 2026-10-01): this stays allowed. Reusing a result across
cohort versions is normal for a revised cohort, and E12 compares within one
selected cohort version, so a result cannot be counted twice within one
comparison.

## Every read re-verifies

`resolve(selector_id, source_version, *, expected_member_sha256,
expected_result_id)` locates the object under a shared registry lock, releases
it, enters the D06 fence for that object's cohort, takes the shared registry
lock again, and re-reads the object. Objects are immutable and append-only, so
the second read must find the same digest. It then re-runs steps 3 to 7 against
the live status and requires the re-derived object to be byte-identical to the
stored one. Any change to the member binding, the E04 catalog storage, the
trust store (including a key addition), a result-key revocation, the linkage
snapshot, or the D05 selection, and any mismatch with the expected member or
result, raises without returning a source. D06 fail-closed errors become
`ResultViewSourceRegistryStale`. Both locks are held through construction of
the returned `RegisteredResultViewSource`, which binds the registry ID, epoch,
state version and head, selector, version, object digest, cohort commitments,
D06 status digest, catalog-authority digest, member and binding digests,
catalog-result digest, source, decision and ledger digests, and a source replay
digest over every other returned field, so no commitment in the returned
contract can be changed without failing validation. The digest is not a
signature: a holder outside this process can recompute it, so a serialized
copy is never authority, and consumers must call `resolve` again.

The result is an as-of read: it binds one D06 status. A consumer that composes
it with other authority must hold the composite E12 fence or call `resolve`
again inside it. This registry does not provide that fence.

## Selector page

`list_selectors(cohort_selector_id, cohort_version, ...)` is scoped to one
cohort so the whole page is evaluated under one D06 fence. It returns 1 to 100
rows ordered by selector and version. Each row has the selector, version,
object digest, source digest, ledger digest, compatibility outcome, and
`current|stale` state from re-verification under that fence. Rows carry no
labels, result IDs, member commitments, bundle digests, provider, subject,
collection, specimen, run, or analysis tokens, and the page does not echo the
cohort selector.

If the D06 fence itself cannot be entered (for example after a linkage
advance), the page raises `ResultViewSourceRegistryStale` instead of returning
rows.

## Fence composition and lock order

Measured on the current code:

- The D06 fence holds the D01 linkage fence. Entering
  `ProviderLinkageStore.authority_read_fence` inside it raises
  `linkage store authority fence requires an idle connection`; entering a
  second D06 fence inside it also fails. The registry therefore never calls a
  D01 or D06 read inside the fence and reads the linkage snapshot it needs for
  its metadata before taking any lock.
- The E04 catalog connection lock and the trust-store lock are reentrant
  locks held by the D06 fence; a same-thread `verify_reference` still works,
  and `TrustStore.revoke` on another thread blocks until the fence is released.
  The registry needs no separate E04 call, because D06 re-verifies every
  available member inside the fence.

Lock order inside this registry matches the E12 plan: D06 fence (which takes
D01, D05, E04 catalog, E04 trust, and the D06 root lock in that order), then the
E06 registry lock. The registry lock is never held while the D06 fence is
acquired; `resolve` releases its locating read first. Backup takes only the
registry lock.

## Storage and bounds

Storage follows the D03 decision registry: a private `0700` root, `0600`
owner-only files, descriptor-relative exclusive publication with fsync and
hard-link adoption, a journal chain from a genesis digest over immutable
metadata, required retained registry ID, epoch, and head on reopen, a
process-wide monotonic head fence against rollback, inode-bound control files,
and a process-private instance seal.

A failed journal append truncates any torn suffix back to the last committed
entry, so the chain stays readable and the registration can be retried. A
failed restore, including a failed final reopen of the restored registry,
removes the partial target it created, so a retry is possible. A crash, rather
than a caught failure, can still leave a torn journal tail or a partial restore
target that needs manual repair; that is the same shared follow-up as the D03
decision and D05 cohort registries.

The metadata binds the cohort registry ID and epoch, the linkage store ID, epoch,
and storage identity, the E04 catalog storage and reader-registry identities,
and the D06 record-catalog scope; reopening or restoring against a different
D06 catalog fails.

One object holds at most 1 MiB of canonical bytes (a two-record source is
about 15 KB). The registry holds at most 10,000 sources and 256 MiB of
committed object bytes, checked before publication, with 16 versions per
member slot. Object and backup parsing enforce byte, depth, node, collection,
and string budgets before validation and require an exact canonical round
trip. A backup contains protected records and is not an export artifact.

## Threat model and seals

In-process code mutation is outside the threat model: the process boundary is
the trust boundary, as in the D05 cohort and D03 decision registries. Seals
detect accidental or naive replacement of the registry's methods (class and
instance), the pinned D06 fence, linkage snapshot, E05 decide, and E06 bind
callables, the sealed result constructors and selector and digest helpers, and
the instance's authority state. There is no whole-module namespace seal; the
interpreter's `__warningregistry__` must not disable the registry.

## Known gaps

- Crash recovery for torn journal tails, partial restore targets, and partial
  first-time creation matches D03/D05 and is a shared follow-up.
- The rollback fence is per process. A fresh process trusts the retained head
  it is given.

## Not in scope

The composite E12 authority fence, a durable method-authority store, a
compatibility-policy authority, and an authority for denominator ledgers are
not provided here. All fixtures are synthetic and local, and outcomes describe
technical comparability only.
