# Cohort denominators and missingness

Status: D09 synthetic/local contract. This is not evidence that Epic D,
scientific qualification, provider authorization, or clinical-use gates passed.

`CohortDenominatorSummary` binds one immutable D05 manifest digest to one
versioned denominator policy. The policy repeats the manifest inclusion,
exclusion, and missingness digests and adds an explicit caller-declared denominator-definition
digest. Its only supported basis is the manifest's declared denominator
contributors. Unavailable contributors remain in the declared denominator and
missing values remain typed unavailable; neither can be rewritten as zero.

`CohortDispositionPolicy` supplies the closed member commitments evaluated by
the inclusion and exclusion policies. Each rule set is independently hashed and
must equal both the D05 manifest policy digest and the D09 denominator-policy
digest. D09 derives policy-exclusion reasons from those sets; callers cannot
select a reason label. Overlap is invalid rather than assigned implicit
precedence.

Every manifest member has exactly one assessment. Assessments are normalized
back to canonical manifest order, so caller order cannot change summary bytes.
The output reconciles both record counts and biological denominator-unit
counts across `included`, `excluded`, and `unavailable`. Technical replicates
and reanalyses remain visible only as their matching typed collapsed exclusions;
they cannot be reclassified as included or unavailable, and cannot inflate the
biological denominator established by D05.

In the protected v1 builder only, an included record must bind the same exact
result in an E04 catalog reference and E06 result-view source; the registered
path below derives rows from D05/D06 authority instead. The result must be complete, sufficient, verified,
qualified, currently provider-eligible, E05-comparable, and have a completely
observed reconciled E06 denominator ledger. Failed, not-run, insufficient,
untrusted, unqualified, non-comparable, missing, and withheld cases use closed
reason codes. D06 result-key withholding remains distinct from an E06 result
whose trust state is revoked. E05 `different_quantity`, `incompatible`, and
`unknown` remain distinct. A zero-included-unit summary and a one-included-unit summary have
separate explicit states; neither implies that a trend can be drawn.

Summary rows contain only canonical member commitments, controlled lineage
roles, result artifact IDs/digests, ledger digests, and typed dispositions.
They do not serialize provider, subject, collection, specimen, run, or analysis
tokens; accessible labels and other free text from E06 inputs are also omitted.
The population ID is derived from the complete canonical summary content. This
row-bearing v1 contract is a protected derivation artifact, not a public
aggregate projection.

`build_registered_cohort_denominator_summary` obtains the exact manifest history
through the protected D05 registry and derives each row inside D06's supported
composite authority fence. The fence holds live linkage, D05 registry, D06
catalog/root, result-catalog connection, and shared result-trust authority
through canonical capture of the returned object. Its v3 output binds those
registry, linkage, catalog, policy, manifest, and protected-population digests,
but embeds only reconciled aggregate counts. Per-member commitments, result IDs,
provider tokens, and other record lineage remain outside that aggregate boundary.

The registered path accepts no caller-authored result, compatibility, ledger, or
comparison evidence. D09 depends only on D05 and D06, so every registered row is
derived from fenced authority in fixed precedence: collapsed technical
replicates and reanalyses; members selected by the inclusion or exclusion policy;
D06 `missing` (`no_verified_catalog_result`); D06 `withheld` for a revoked result
key; then, for an available D06 binding, the qualification and provider
eligibility recorded on its `CatalogResultRef`. D06 indexes only complete,
development-signature-verified results whose method definition equals the
manifest measurement definition, and it re-verifies each bundle against live
trust inside the fence. An available, qualified, provider-eligible member is
`included`. Qualification and eligibility are those D06 bound at import
(`capability_as_of`), not a fresh capability lookup.

E05 cross-result comparability, E06 denominator-ledger completeness, and
D02/D03/D07 comparison eligibility are not evaluated in the registered path.
Nothing in the current system derives a result digest or compatibility key from a
verified bundle, so caller-supplied evidence of that kind could choose the
counts. E12 must obtain that evidence through protected result-view and D03/D07
discovery registries and must not treat a D09 `included` count as comparison
eligibility.

The row-bearing v1 builder remains a protected derivation over supplied member
evidence. It requires the exact disposition policy. Lineage and policy selections
mandate their collapsed or policy-exclusion reason, so a selected member cannot
be reported as included or unavailable. A policy-exclusion label without the
matching selection is rejected.

All object boundaries use bounded, zero-hook graph capture before authority
work. Byte parsers impose byte, depth, node, collection, string, and integer
bounds before validation and require an exact canonical round trip.

This D09 integration does not authorize rendering a delta or connecting a trend.
