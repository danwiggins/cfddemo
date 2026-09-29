# Cell-origin estimate explorer contract

`evidence_inspector.cell_origin_explorer` is a pure, versioned presentation
boundary over the reviewed cell-origin result/bundle schemas. It does not
implement a web UI, execute a solver, access files or networks, or authorize a
clinical interpretation.

## Display contract

The ready state exposes the same values twice:

- dot-and-interval rows for a visual renderer;
- exact table rows for inspection and accessible alternatives.

Both use the label **estimated fraction among registered atlas contributors**.
Contributor IDs are the registered atlas IDs, not free-text aliases. Estimates
are fractions in `[0, 1]`; a numeric zero remains zero. An interval is emitted
only when bootstrap v2 marks that contributor `available`. Partial or
insufficient uncertainty has null bounds, so a renderer cannot invent a
zero-width whisker.

The view also carries registered, usable, excluded, collapsed-duplicate, and
observed marker counts;
input-fragment, marker-overlap, classified fragment-marker, and short-fragment
exclusion denominators; atlas marker coverage; NNLS convergence, iterations,
residual, and objective; and controlled limitation IDs. Deconvolution v2 does
not report a condition number, so conditioning is explicitly `missing` with a
controlled reason instead of a fabricated value.

## Authority and identity

The request embeds one E06 `ResultViewRequest`, which in turn binds the E05
verified record, E01 method and current authority, compatibility decision,
normalized filters, and denominator ledger. A ready view requires complete
execution, sufficient information, verified trust, research inspection
authority, and inclusion by the exact normalized filter.

The source record must bind canonical SHA-256 digests of the exact cell-origin
result and bundle. Its compatibility atlas asset ID and digest must equal the
deconvolution v2 atlas ID and digest. Result ID, method definition, authority
head, atlas, bundle, and filter identities are copied into the view binding.

Raw bundle validation and canonical replay are deliberately separate. The raw
request verifies every composition chart field against the signed result and
bootstrap: contributor identity, deterministic rank, fraction, percent,
interval bounds and percentages, uncertainty status and availability, palette,
and default visibility. The canonical artifact then retains only the aggregate
result, resource counts, and bundle digest needed for replay. Chart aliases,
healthy-context presentation rows, and notices are not serialized.

`failed`, `not_run`, `insufficient_information`, and `missing` are distinct
states. They expose no numerical rows or derived QC values, reject attached
result bundles, and serialize a null replay source. A complete, sufficient,
verified source without its exact bundle is invalid rather than silently
displayed as missing.

## Validation and replay

Construction fails closed for duplicate or missing contributors, chart/result
disagreement, resource-count disagreement, unreconciled fragment-marker
denominators, atlas mismatch, v1 deconvolution/bootstrap input, excess
contributors, unbounded notices/provenance, or reserved private terms in
contributor IDs. Privacy checks reject concatenated raw-ID prefixes, home,
relative, absolute, URI, and encoded paths, sequence-like strings, secret-like
text, and protected field names. Output contracts are frozen, reject unknown
fields and non-finite values, and bound all collections and strings.

Canonical artifacts contain both request and view. Parsing recomputes the view
from the embedded source and requires exact equality, so changing values and
recomputing a self-hash is insufficient. Tests use only synthetic aggregate
fixtures and contain no donor, read, sample, sequence, credential, or local-path
data.

## Tradeoffs

Advantages:

- renderers receive one deterministic, framework-independent contract;
- identity and authority cannot drift away from visible values;
- exact tables and plots cannot disagree;
- unavailable information cannot masquerade as zero or precision.

Costs:

- embedding the replay request makes artifacts larger;
- strict digest binding means any legitimate upstream bundle change requires a
  new E05 record;
- the contract intentionally withholds all numeric content when authority or
  information state is not eligible;
- conditioning remains unavailable until an upstream reviewed schema reports a
  defined conditioning diagnostic.
