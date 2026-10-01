# Batch and preanalytical context (D10)

D10 is a synthetic/local descriptive contract. It exposes opaque batch,
protocol, and preanalytical context for a declared D09 population. It does not
alter measurement values, change D02/D03 eligibility, correct batch effects,
or assign biological or clinical meaning.

## D09 adapter boundary

The registered D09 v3 summary (`build_registered_cohort_denominator_summary`)
derives aggregate counts from D05/D06 authority only and deliberately carries no
D03 member decisions or per-member identities. D10 needs both, so it cannot
consume that summary directly. Binding D10 to live D09, D03, and D07 authority
is E12 integration work (see `docs/E12-DEPENDENCY-AUDIT.md`), not part of this
contract. Until then, `D09PopulationDigestInput` is a versioned digest-only
adapter that binds:

- the exact cohort-manifest digest;
- the declared future D09 status and population digests;
- the D02 anchor-policy digest; and
- the uniquely sorted SHA-256 identity of every included member.

The adapter is permanently marked `declared_digest_only`,
`live_d09_registry_verified=false`, `synthetic_only=true`, and
`clinical_use_authorized=false`. This implementation therefore does not claim
that D09 registry evidence was read or verified. E12 must replace the adapter
at this explicit boundary and recheck live D09 authority.

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

D10 does not establish D03 custody. A `LongitudinalMemberDecision` carries no
signature or self-binding between its member-result digest and its bundle,
record, and linkage digests, so a caller can supply a canonical decision that
D03 never produced, including a real decision re-pointed at another member.
D10 only checks that each supplied decision is canonical, matches the D02
anchor policy, and covers the declared population exactly. Both the protected
result and the aggregate are therefore permanently marked
`d03_authority_verified=false`. E12 must bind member decisions to D03 replay
against live linkage and record authority before any consumer relies on them.

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
