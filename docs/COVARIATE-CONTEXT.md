# Batch and preanalytical context (D10)

D10 is a synthetic/local descriptive contract. It exposes opaque batch,
protocol, and preanalytical context for a declared D09 population. It does not
alter measurement values, change D02/D03 eligibility, correct batch effects,
or assign biological or clinical meaning.

## D09 input: two paths

The registered D09 v3 summary (`build_registered_cohort_denominator_summary`)
derives aggregate counts from D05/D06 authority only and deliberately carries no
D03 member decisions or per-member identities. D10 needs the exact included
member set, so it cannot consume that summary directly. There are two paths.

The live path (`build_live_covariate_context`, below) derives the population
from the D09 policy registry and is marked `live_d09_registry_verified=true`.
The v1 path is unchanged: `D09PopulationDigestInput` is a versioned digest-only
adapter that binds:

- the exact cohort-manifest digest;
- the declared future D09 status and population digests;
- the D02 anchor-policy digest; and
- the uniquely sorted SHA-256 identity of every included member.

The adapter is permanently marked `declared_digest_only`,
`live_d09_registry_verified=false`, `synthetic_only=true`, and
`clinical_use_authorized=false`. That path does not claim that D09 registry
evidence was read or verified. E12 must use the live path or the D10 context
registry instead of this adapter.

## Covariates and classification

Every included member has exactly one ordered batch, protocol, and
preanalytical value. Values are either `known` with a controlled opaque token
or `unknown` with no token. Free text, paths, provider labels, subject labels,
and measurement values are not accepted.

The result uses four explicit classifications:

- `clear`: all metadata is known and the complete covariate context is constant;
- `aliased`: protocol is perfectly aligned one-to-one with two or more
  biological timepoints, so protocol and time cannot be attributed separately;
- `missing_metadata`: at least one required value is unknown, including missing
  batch, or the declared population has no included members; and
- `mixed`: metadata is complete and varies without the exact protocol/timepoint
  alias condition.

Classification is descriptive. Even `clear` does not establish comparability,
causality, or qualification. The output fixes measurement changes, eligibility
changes, correction, biological attribution, and clinical interpretation to
`false`. Each member's D03 decision digest and outcome remain bound into the
input and per-member digest. The builder consumes exact canonical D03 member
decision artifacts, derives their digests and outcomes after replay, binds each
artifact's member-result digest to the included member, and rejects duplicate
member or decision identities. No caller-declared digest/outcome pair is
accepted, so a supplied decision's outcome cannot be relabelled or collapsed
inside D10.

## D03 decisions: two paths

`build_covariate_context` takes caller-supplied decisions and does not establish
D03 custody. A `LongitudinalMemberDecision` carries no signature or self-binding
between its member-result digest and its bundle, record, and linkage digests, so
a caller can supply a canonical decision that D03 never produced, including a
real decision re-pointed at another member. That builder only checks that each
supplied decision is canonical, matches the D02 anchor policy, and covers the
declared population exactly. Its `CovariateContextResult` and the aggregate are
therefore marked `d03_authority_verified=false`.

`build_registered_covariate_context` takes no decisions. It takes a
`LongitudinalDecisionRegistry` (`docs/LONGITUDINAL-DECISION-REGISTRY.md`) and a
series selector, resolves the series (the registry replays it against the live
linkage store and raises `LongitudinalDecisionRegistryStale` rather than return
a stale decision), requires the series policy to equal the population's D02
anchor policy, and uses the registry's decision for each included member. The
population may be a subset of the series but not exceed it. Each member's
declared decision digest and outcome must equal the registry's. It returns a
`RegisteredCovariateContext` that wraps the unchanged v1 result with a binding to
the registry ID and epoch, state version and head, selector, object digest,
series decision digest, and the decision's linkage snapshot, and is marked
`d03_authority_verified=true`.

That binding is as-of one linkage snapshot, and a stored wrapper is not
authority by itself. `verify_registered_covariate_context` re-resolves the
selector, requires the same registry identity, object, series decision, and
snapshot, rebuilds the context from the registry's decisions, and requires it to
be identical. Unrelated registrations may advance the registry head; the
returned wrapper carries the current head. Any linkage change makes both build
and verification fail. The aggregate projection is unchanged and still reports
`d03_authority_verified=false`, because it cannot prove its source on its own.

## Live D09 path

`build_live_covariate_context(covariates, *, d09_registry, d09_selector_id,
d09_policy_version, decision_registry, series_selector_id,
expected_d02_anchor_policy_sha256)` takes no population and no decisions. The
caller supplies only `LiveCovariateMemberValues`: a D03 member-result digest
and its three ordered covariate values.

### Where the included set comes from

The D09 v3 summary is aggregate-only, but its `population_sha256` is the digest
of the protected row-bearing `CohortDenominatorSummary`, so v3 already commits
to every member row. `DenominatorPolicyRegistry.resolve_population` returns
`RegisteredDenominatorPolicyPopulation`. This is the same live summary
`resolve` returns, plus `RegisteredCohortPopulationMembers`: the row-bearing
population and, for each included row, its exact D05 `CohortMember` and D06
`CatalogResultRef`. Both come from one derivation inside one D05/D06 fence
(`build_registered_cohort_population_members`). The contract's validator
re-checks the population digest, the projection, and each included member's
D05 and catalog digests against its row. It is protected and local only. It
never enters the v3 summary, D09 selector rows, or D10 aggregate bytes, and the
v3 contract is unchanged.

Options considered: re-deriving the included set in D10 from the D05 manifest,
D06 status, and disposition policy would duplicate D09's disposition precedence.
The disposition policy is also only stored inside the D09 registry. Checking a
declared set against D09's aggregate counts would not prove which members were
included. Two different sets can have the same counts. Both were rejected.

### Matching D09 members to D03 decisions

D09 identifies a member by the digest of its D05 `CohortMember`. D03 and D10
identify a member by the E05 result digest. No existing contract maps one to
the other. The live builder joins each included member to exactly one D03
decision. All four keys must be equal:

- result ID (`CatalogResultRef.result_id` = `member_result_id`);
- result bundle (`bundle_sha256` = `member_bundle_sha256`);
- D01 linkage revision (`CohortMember.linkage_revision_sha256` =
  `member_linkage_revision_sha256`); and
- committed linkage receipt (`committed_receipt_sha256` =
  `member_linkage_receipt_sha256`, which must be present).

A missing or ambiguous match fails. D03 series members that D09 did not include
are ignored. The covariates must cover every and only the matched members.

The D09 rebuild and the D03 replay each take the D01 linkage fence themselves,
so neither can run inside the other. They run one after the other. The D09
summary's `linkage_snapshot_sha256` and the D03 series decision's
`linkage_snapshot_sha256` are the same digest of the active linkage snapshot.
The builder requires them to be equal, which proves both reads saw one D01
state. A D03 series without a linkage snapshot is rejected.

### What the live result binds

The result is `LiveCovariateContext`. It wraps the unchanged v1
`CovariateContextResult` with the #57 `RegisteredD03SeriesBinding` and a new
`LiveD09PopulationBinding`. The binding holds D09 registry identity, selector,
policy version, object and policy digests, D05 selection, manifest, v3 summary,
population, record-status, catalog-authority, and linkage-snapshot digests. It
also holds one crosswalk row per included member (D09 member digest, catalog
result digest, result ID, D03 member-result digest, D03 decision digest).
The wrapper is marked `live_d09_registry_verified=true` and
`d03_authority_verified=true`. The inner v1 result still reports both as
`false`, because it cannot prove its sources on its own.

On this path the v1 fields are filled from authority:

- `d09_status_sha256` is the D09 registered policy object digest;
- `d09_population_sha256` is the v3 `population_sha256`;
- `cohort_manifest_sha256` comes from the v3 summary; and
- each member's `biological_timepoint_sha256` is
  `sha256("traceback-d10-biological-timepoint-v1\0" + biological_timepoint_id)`
  of its D05 member, not a caller value.

`verify_live_covariate_context` re-resolves D09 and D03 by the bound selectors,
rebuilds from the stored covariate values, and requires an identical v1
result, the same D03 binding fields as #57, and the same D09 registry identity,
selector, version, object, policy and manifest digests, population digest,
linkage snapshot, and crosswalk. The D09 state head, v3 summary digest (it binds
the D05 registry head), record-status digest, and catalog-authority digest are
as-of values. Unrelated activity may advance them, and the returned context
carries the current ones. A D06 change to the population (for example a
revoked result key) or any linkage change makes verification fail.

The protected `d10_context_registry` that stores the covariate tokens and
rebuilds on every read is documented in `docs/D10-CONTEXT-REGISTRY.md`.

## Protected and aggregate outputs

`CovariateContextResult` is a protected, local-only artifact because it contains
member digests, biological-timepoint digests, and opaque covariate tokens. It
must not be used as public, support, or export payload bytes.

`project_aggregate_covariate_summary` is the only export projection. It binds
the protected result by digest but exposes only classification, reason codes,
known/unknown state patterns, and group/member counts. Aggregate groups are
ordered by their exported state pattern and member count, not by the protected
token-derived group IDs, so aggregate bytes reveal nothing about token values.
Its canonical bytes omit
member identities, timepoint identities, covariate tokens, and the declared D09
status/population digests. The protected result is still required to verify how
that aggregate was derived. Canonical aggregate bytes and hashes require that
protected result and reject any summary that does not exactly equal its derived
projection, including a forged `mixed`/`aliased` relabel.

## Determinism and ingress

Member input order is normalized before hashing. Groups and member identities
use canonical order, and conflicting reuse of one opaque token across dimensions
is rejected. Exact authority pins are required for the D09 status, D09
population, and D02 anchor policy.

All contracts are closed, frozen, versioned, canonically replayed, and bounded
to 1,000 members. The shared safe-ingress path performs exact no-hook
collection, primitive, state, alias, graph-node, and graph-depth checks and
rejects forged `model_copy`, private, or extra state before serialization.
Ingress budgets scale from the public population bound: 1,032 fixed graph nodes
plus 64 nodes per member, and 1 MiB fixed canonical JSON space plus 4 KiB per
member. This admits the maximum valid 1,000-member/1,000-group protected result
and aggregate projection with headroom while rejecting expanded hostile graphs
before serialization. D03 member decisions are captured one at a time under
the D03 per-decision bounds and share one 16 MiB canonical byte budget across
the whole collection, about twice the size of a valid 1,000-member input.
No raw sequence, read-level data, local path, provider identity, or subject
identity belongs in these artifacts.
