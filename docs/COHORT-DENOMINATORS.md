# Cohort denominators and missingness

Status: D09 synthetic/local contract. This is not evidence that Epic D,
scientific qualification, provider authorization, or clinical-use gates passed.

`CohortDenominatorSummary` binds one immutable D05 manifest digest to one
versioned denominator policy. The policy repeats the manifest inclusion,
exclusion, and missingness digests and adds an explicit denominator-definition
digest. Its only supported basis is the manifest's declared denominator
contributors. Unavailable contributors remain in the declared denominator and
missing values remain typed unavailable; neither can be rewritten as zero.

Every manifest member has exactly one assessment. Assessments are normalized
back to canonical manifest order, so caller order cannot change summary bytes.
The output reconciles both record counts and biological denominator-unit
counts across `included`, `excluded`, and `unavailable`. Technical replicates
and reanalyses remain visible only as their matching typed collapsed exclusions;
they cannot be reclassified as included or unavailable, and cannot inflate the
biological denominator established by D05.

An included record must bind the same exact result in an E04 catalog reference
and E06 result-view source. The result must be complete, sufficient, verified,
qualified, currently provider-eligible, E05-comparable, and have a completely
observed reconciled E06 denominator ledger. Failed, not-run, insufficient,
untrusted, unqualified, non-comparable, missing, and withheld cases use closed
reason codes. A zero-included-unit summary and a one-included-unit summary have
separate explicit states; neither implies that a trend can be drawn.

Summary rows contain only canonical member commitments, controlled lineage
roles, result artifact IDs/digests, ledger digests, and typed dispositions.
They do not serialize provider, subject, collection, specimen, run, or analysis
tokens; accessible labels and other free text from E06 inputs are also omitted.
The population ID is derived from the complete canonical summary content. This
row-bearing v1 contract is a protected derivation artifact, not a public
aggregate projection.

`build_registered_cohort_denominator_summary` obtains the exact manifest history
through the protected D05 registry, derives each record state from D06, and
rechecks both snapshots after derivation. Its v2 output binds those registry,
linkage, catalog, policy, manifest, and protected-population digests, but embeds
only reconciled aggregate counts. Per-member commitments, result IDs, provider
tokens, and other record lineage remain outside that aggregate boundary.
Every supplied E06 source must be consumed exactly once by an eligible selected
member; sources for collapsed, withheld, missing, or unrelated records are
rejected instead of being silently omitted from output identity.

This D09 integration still does not authorize a delta or connecting trend. E12
must additionally bind the selected comparison anchor and reject stale,
unknown, or incompatible comparison evidence before rendering either.
