# Synthetic CNA explorer contract

`evidence_inspector.cna_explorer` is the E09 deterministic data contract for a
future copy-number explorer. It is not a UI framework, renderer, analysis
runtime, or clinical interpretation layer.

The builder consumes two already-validated upstream contracts:

- `copy-number-dosage-qc.v2`, the sample-internal whole-chromosome dosage-QC
  result union;
- `traceback.ichor-development-result.v1`, the segmented-CNA development
  result.

Those results remain separate measurements. Every grid, bin, method, asset,
insufficiency record, and presentation row carries `dosage_qc` or
`segmented_cna`; the contract never joins their values or puts them on an
implied common quantitative scale.

## Authority and failure behavior

Each input has independent execution, trust, qualification, and research-
inspection authority. A snapshot is available only when both inputs are
complete, verified, qualified, research-inspectable, and upstream-complete.
Unknown, incomplete, revoked, unqualified, non-inspectable, or upstream-
insufficient input yields one unavailable snapshot with explicit reason codes,
no data layers, and empty chart/table contracts.

The upstream development qualification remains visible even when independent
current authority permits the synthetic explorer fixture. E09 never upgrades
either source artifact, authorizes product release, adds diagnostic thresholds,
or permits clinical interpretation.

## Exact layers

An available snapshot binds and exposes:

- independent zero-based half-open coordinate/grid identities;
- exact upstream asset digests without local paths;
- source and current authority method states;
- dosage bins and chromosome summaries;
- segmented bin status, corrected depth, and prespecified masks;
- segments and the complete upstream candidate grid;
- explicit insufficiency state for each source;
- path-free provenance binding each source result and authority digest.

Native missing corrected-depth values remain `null` with
`native_missing`; they are never converted to zero. Candidate model fields are
preserved as upstream development outputs and explicitly are not tumor or
clinical estimates.

## Determinism and presentation

Chart and table contracts duplicate only values from the exact typed layers.
Snapshot validation requires every chart and table tuple to equal its source
layer, while `replay_cna_explorer_snapshot` re-derives the entire snapshot from
the two upstream results and authority inputs. Canonical UTF-8 JSON bytes and a
SHA-256 digest are provided for immutable binding.

All models are frozen, closed to unknown fields, finite-number-only, and
bounded. Canonical loading rejects altered whitespace, unknown fields,
non-finite values, private identifiers, absolute local paths, secret-like
text, and sequence-like text. Tests use only existing synthetic fixtures; the
module performs no native execution or network access.
