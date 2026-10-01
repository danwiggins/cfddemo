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
  the caller's record. E04 stores none of them. The method-definition object is
  verified only through its digest, which the binding pins.
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
| `result_sha256`, `bundle_id`, `information_state`, compatibility key details, `effective_approval_ref` | Caller input | Not verified |
| Denominator ledger | Caller input; digest bound | Not verified |
| Accessible and QC labels | Caller input; E06 privacy checks | Not verified |

The core identity fields (member, result, bundle, method, capability as
pinned, trust) have live authority, so the registry is built. Each resolved
source carries the fixed list of unverified fields
(`caller_asserted_fields`) and the literal `denominator_verified=false`, so a
consumer cannot mistake a bound ledger for a verified one.

Who may author an E06 denominator ledger, and whether E12 should show one at
all next to the D09 v3 counts, is a product decision this registry does not
make.
