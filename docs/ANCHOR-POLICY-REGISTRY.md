# Protected anchor-policy registry

Status: local synthetic E12 prerequisite. It does not authorize real provider
operation, clinical use, scientific claims, export, or Epic D release.

`AnchorPolicyRegistry` (`evidence_inspector/anchor_policy_registry.py`) is the
protected anchor-policy store that E12 requires as `anchor_policy_registry`
(see `docs/E12-INTEGRATION-PLAN.md`, "Approved anchor and projection
selection" and step 3 of the D08 selection journey). It replaces a
caller-provided D03 policy, policy digest, D07 envelope or anchor identity with
two registry-scoped opaque selections: an anchor-policy selector/version and an
approved-anchor selector resolved against a live candidate page.

## Registration is the approval

`register_policy(cohort_selector_id, cohort_version, policy, envelope,
anchor_candidates, *, approval_version, expected_cohort_manifest_sha256,
expected_policy_sha256, expected_envelope_sha256,
expected_authority_head_sha256)` is the protected approval act. It accepts:

- one exact D05 selection (selector, version, expected manifest digest);
- one `LongitudinalAnchorPolicy` and one `RepeatabilityEnvelope` with
  independent digest pins;
- the registrant-approved anchor-candidate `LongitudinalRecord`s (1 to 1,000,
  an exact tuple); and
- the E01 authority-head pin D03 evaluates against.

It never accepts a candidate page, a selection, or a decision. The stored
`RegisteredAnchorPolicyObject` validator requires:

- the policy and envelope digests to equal their pins;
- every candidate's comparison-key digest to equal `policy.anchor_key_sha256`;
- every candidate to carry its activation receipt, uniquely sorted by record
  digest, with at most one candidate per linkage revision (one D05 member), so
  two snapshots of one member's record cannot both be offered; and
- the envelope to bind the anchor key: the same method reference, method
  definition, quantity and unit, and the key's known uncertainty-method and
  denominator-semantics dimensions equal to the envelope's. This is the
  anchor-side D07 measurement-identity gate, applied at approval time.

Under the authority fence (below), every candidate must also be admitted live
(next section). One non-admitted candidate rejects the whole approval rather
than being dropped. Then the object is published and journalled.

The selector `anchor_policy_…` derives from the registry epoch, the D05
registry ID, D05 selector, D05 version and `policy_id`. The version is the
explicit `approval_version`. Versions for one selector must be committed as
1, 2, … N. Re-registering identical bytes is idempotent. The same version with
other content is a conflict. Versions are not superseded: each stays
independently resolvable.

## The live candidate page

`derive_candidate_page(selector_id, approval_version)` re-derives the page on
every call. It never uses a cache. A stored candidate is admitted only when all
of these hold:

1. **Live D05 membership.** The bound D05 selector/version resolves as
   current with the stored manifest digest. The candidate is exactly one
   member of it, matched on provider, linkage ID, linkage revision, linkage
   revision digest, and committed receipt digest.
2. **Admitted by that exact D03 policy under live linkage.** The pinned
   `decide_longitudinal_member` runs with the candidate as both anchor and
   member, the stored policy and pins, and the live linkage store. The decision
   must bind the candidate's record digest, the policy digest and the policy's
   anchor key. `equivalent` with only `exact_match` is `eligible`. `unknown`
   with only `result_state_invalid` (an incomplete, insufficient or unverified
   result) is shown as `result_state_ineligible`. Any other result excludes the
   candidate: policy identity, anchor identity, linkage authority, the D03
   invalid-input sentinel, or a wrong E01 head pin (which D03 rejects as
   invalid input).

A page with no admitted candidate raises `AnchorPolicyRegistryStale`.

Each `AnchorCandidate` row carries only:

- an opaque `anchor_candidate_…` selector;
- a `candidate_…` alias derived from that selector;
- the biological-timepoint ordinal: the rank of the member's
  `(time_coordinate, biological_timepoint_id)` among the manifest's distinct
  timepoints;
- the signed-seconds offset from the manifest's first biological coordinate;
- the method version; and
- the eligibility state.

The page adds the approval, policy, envelope, anchor-key and manifest digests,
`explicit_selection_required=true`, and a `candidate_page_sha256` over its
content. The page digest excludes registry state heads, so an unrelated
registration does not invalidate an outstanding selection. There is no
implicit first, latest or provider-primary choice.

### D03 v1 consequence

A v1 `LongitudinalAnchorPolicy` pins one complete `anchor_key_sha256`, and the
comparison key binds one result (`result_id`, `result_sha256`, bundle and E01
authority). Every admitted candidate under one approval therefore shares that
exact result. With at most one candidate per linkage revision, a page holds one
candidate per live linkage of that result, which is normally one. The 1,000
bound and the page protocol still apply, and nothing here assumes one. Offering several
distinct anchors for one cohort means registering several approvals: one per
anchor, each with its own policy and envelope. See "Open decisions".

## Explicit selection

The E12 request's two selector/version pairs map to this registry as:

- anchor-policy selector/version = `(selector_id, approval_version)`; and
- approved-anchor selector/version = `(anchor_selector_id,
  candidate_page_sha256)`. The anchor "version" is the content digest of the
  page the operator chose from, not a counter. A counter could not detect a
  changed page that keeps the same selector.

`resolve_anchor(selector_id, approval_version, anchor_selector_id, *,
expected_candidate_page_sha256)` has no defaults. Under the authority fence it:

1. re-derives the page;
2. requires its digest to equal the page the operator chose from;
3. requires the anchor selector to be on it; and
4. requires the candidate to be `eligible`.

It returns a protected `ResolvedApprovedAnchor` with:

- registry identity and head;
- the D05 binding;
- the exact policy and envelope and their digests;
- the E01 head pin;
- the page digest and the candidate row; and
- the anchor record and its digest.

Its validator re-derives the anchor selector from the epoch, the approval
object digest and the record digest.

These fail closed:

- an injected or free-form selector (a result ID, linkage token or record
  digest);
- a selector from another approval or version;
- a selector from another registry (selectors are epoch-derived);
- an unknown policy selector or version;
- an ineligible candidate; and
- any change to the page between selection and use.

A changed page raises `AnchorPolicyRegistryStale`. The registry never selects
a different anchor.

E12 binds its other inputs to the resolved approval:

- the D03 series decision's `policy_sha256` and `anchor_record_sha256` must
  equal the resolved policy digest and anchor record digest; and
- each D07 comparison's envelope digest must equal `envelope_sha256`.

That closes the D07 registry's documented gap: until now, envelope and policy
pins came from the registering caller.

## Fence composition (measured)

A probe against merged D01/D05/D03 code showed:

| Call made while holding `ProviderLinkageStore.authority_read_fence` | Result |
| --- | --- |
| `CohortRegistry.resolve_history` | fails: "linkage store authority fence requires an idle connection" |
| `CohortRegistry._lock(exclusive=False)` + `_resolve_history_in_fence` | succeeds |
| `decide_longitudinal_member` (snapshot uses a savepoint) | succeeds |

Every read and registration therefore takes, in order:

1. the D01 linkage fence;
2. the D05 integrity check and shared D05 lock, with the D05 already-fenced
   history read; and
3. this registry's lock (shared for reads, exclusive for registration).

This is the plan's global order (D01 → D05 → … → anchor-policy). No D05 or
D01 code takes this registry's lock, so the order is acyclic. The fence is held
through construction of the returned value. A test starts a linkage writer
inside `resolve_anchor` and observes it blocked at return. Concurrent readers
interleaved with D05 public reads terminate.

The D05 already-fenced read is a private method, pinned unbound at import and
covered by this registry's authority seal. The composable authority-fence
adapter prerequisite should make it a reviewed public in-fence API. Merged D05
code is unchanged.

A linkage commit stales every approval. The D05 manifest binds the linkage
head, and D03 requires the candidate's receipt at the current head. A
re-approval is then required, as for the D03, D07 and D09 registries.

## Selector projection

`list_selectors(after_selector_id=None, after_approval_version=None,
limit=50)` returns a bounded page (1 to 100 rows) ordered by
`(selector, version)`. Each row carries:

- the object, policy, envelope, anchor-key and manifest digests;
- `current|stale`; and
- for a `current` row only, the candidate count, eligible count and page
  digest. The validator enforces this.

## Privacy

Public contracts (`AnchorCandidatePage`, `AnchorPolicySelectorPage`, the
receipt) carry only opaque selectors, aliases, ordinals, offsets, a method
version, controlled states, counts and digests. They never carry result,
bundle, provider, subject, collection, specimen, analysis, run or linkage
tokens, the D05 selector, the policy ID, method IDs, absolute coordinates,
timepoint handles, paths or free text. Tests seed these values and check their
absence. `ResolvedApprovedAnchor` is protected (`protected_only=true`) and must
not cross the browser boundary.

## Storage and bounds

Storage follows the D03 decision registry
(`docs/LONGITUDINAL-DECISION-REGISTRY.md`):

- a private `0700` root with `0600` owner-only files;
- descriptor-relative exclusive publication with fsync and hard-link adoption;
- a journal chain from a genesis digest over immutable metadata;
- the retained registry ID, epoch and head, required on reopen;
- a process-wide monotonic head fence against rollback; and
- inode-bound control files.

The metadata binds the linkage store identity and trust pins, and the D05
registry ID and epoch. Reopening or restoring against another linkage store or
cohort registry fails closed. The supplied cohort registry must be bound to
the same linkage store and trust pins.

The journal is the commit point. A failed journal append truncates any torn
suffix. Reads tolerate at most one uncommitted object, which the next
registration removes unless it holds the exact bytes being registered. A
failed restore removes the target it created, including when the final reopen
fails. Approval history is validated in journal order on load and on restore.

Bounds:

| Item | Bound |
| --- | --- |
| Candidates per approval (and per page) | 1,000 |
| Canonical bytes per object | 32 MiB |
| Approvals per registry | 10,000 |
| Committed object bytes per registry | 256 MiB, checked before publication |
| Selector page | 100 rows |

Object and backup parsing enforce byte, depth, node, collection and string
budgets before validation, and require an exact canonical round trip. A backup
contains protected records and is not an export artifact.

## Threat model

The process boundary is the trust boundary, as in the D03, D07 and D09
registries.

Seals detect accidental or naive replacement of:

- the registry methods;
- the pinned authority callables: the linkage fence and snapshot, D03
  `decide_longitudinal_member`, the D05 `list_selectors`, `_lock`,
  already-fenced history read and integrity check;
- the result-constructor and helper aliases; and
- the instance authority state, including the bound linkage store, cohort
  registry and trust pins.

There is no whole-module namespace seal (`__warningregistry__`; a test pins
this).

Out of scope:

- in-process code mutation, such as seal tables, function code or the module
  dictionary; and
- same-user filesystem races.

In scope:

- filesystem tampering;
- rollback;
- interrupted writes;
- concurrent linkage writers; and
- caller-supplied objects.

## Open decisions and limits

- **Multiple anchors per approval.** D03 v1 policies are anchor-specific (see
  above). The plan describes one policy whose page lists several candidate
  anchors. That needs either an anchor-agnostic D03 policy schema, or an
  approval that binds a set of (policy, envelope) pairs, one per anchor, under
  one selector. Both are product and contract decisions. This registry keeps
  one pair per selector/version.
- **One envelope per approval.** A D07 envelope binds exact anchor and member
  condition digests per factor. One registered envelope therefore covers only
  members whose conditions match it. Other members' D07 comparisons stay
  unavailable. Per-member envelopes would be a D07 contract change.
- **Lineage role.** Candidates are not filtered by D05 lineage role. A
  technical replicate or reanalysis can be approved as an anchor if the
  registrant supplies it. Whether only `biological_draw` members may anchor is
  a product decision.
- **D05 measurement anchor.** `CohortManifest.measurement_anchor` is not
  compared with the D03 key. Its digests are not defined in D03 terms.
- **Envelope validity window.** Validity is not gated here. D07 reports expiry
  as an unavailable comparison state.
- **D05 in-fence API.** The private D05 in-fence read should become a reviewed
  public API in the authority-fence adapter PR.
- **Recovery.** The staged-creation/restore follow-up shared with the D03, D05,
  D07 and D09 registries applies here too. Every read parses every committed
  object, which is fine at synthetic scale only.

All fixtures are synthetic/local. Candidate eligibility describes technical
D03 admissibility only and carries no clinical interpretation.
