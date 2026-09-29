# Deterministic fragment explorer

`evidence_inspector.fragment_explorer` is the framework-independent E07 view
model for comparing two explicitly selected synthetic fragment results. It is
not a frontend, result verifier, method recommender, or interpretation layer.

## Trust boundaries

- E02 verifies the signed aggregate bundle before
  `fragment_source_from_verified_bundle` is called. The adapter drops the local
  bundle path and retains the exact manifest, measurement, and chart contracts.
- `VerifiedFragmentSource` binds the canonical E02 measurement digest to the
  E05 result identity and the canonical manifest digest to the E05 bundle
  identity. Manifest content digests, chart rows, and measurement histogram
  rows must agree exactly.
- E05 makes the compatibility decision. E07 neither chooses a method nor
  weakens an `unknown`, `incompatible`, or `different_quantity` result.

Only synthetic aggregate fixtures are supported. Raw query sequence, read
identifiers, donor/sample/patient identifiers, absolute paths, URIs, and local
bundle locators are outside the contract.

## Quantities do not alias

The explorer recognizes three separate identities:

| Quantity | Registered quantity ID | Measurement definition |
| --- | --- | --- |
| Raw query length | `qty_fragment_raw_query_length` | `raw-query-length.v1` |
| Aligned query length | `qty_fragment_aligned_query_length` | `aligned-query-length.v1` |
| Aligned reference span | `qty_fragment_aligned_reference_span` | `aligned-reference-span.v1` |

The source contract requires an exact match among this identity, the E05
method/key quantity, the E02 measurement definition, and `unit_bp`. Different
quantities may appear side by side, but deltas and shared axes are withheld.

## Explicit state and filters

`FragmentExplorerState` contains two explicit `(result_id, method_ref)`
selections. There is no default, fallback, provider-primary inference, or
automatic replacement.

`ExplorerControls` uses a half-open bin-index window
`[bin_start_inclusive, bin_end_exclusive)` and an inclusive minimum-count
display threshold. Linked mode carries one control object. Unlinked mode
requires one object for each panel. These are display filters only:

- every row fraction keeps the signed `eligible_alignments` denominator;
- displayed plus outside-display counts reconcile to that denominator;
- exclusions remain separate and reconcile with `records_scanned`;
- an empty display uses a safe axis maximum of one, never a fabricated count.

Synchronized comparison, shared y-scale, crosshair eligibility, and exact
right-minus-left deltas require linked filters, complete panels, equal controls,
identical bin boundaries, and an E05 `comparable` outcome. Shared axes and
deltas additionally require their corresponding E05 policy permission. Equal
panel-local controls in unlinked mode remain independent. Otherwise the
synchronized features fail closed.

## Withholding and accessibility

Only `complete` sources expose numerical rows. `failed`, `insufficient`,
`revoked`, `unverified`, and `unavailable` are derived from the exact E05 state
and presence of verified E02 content; they expose a typed withholding code and
no rows, denominator, or numerical axis. Untraceable presentation-only labels
such as `loading`, `empty`, `partial`, and `unsupported` are rejected at this
canonical boundary rather than self-authorized by a view digest.

The accessible table is constructed from the same immutable `ExplorerBinRow`
objects as the chart series. The view validator rejects any table/chart drift.
Missing or withheld values are absent rather than represented as zero.

## Canonical replay

Use `build_fragment_explorer_state` and `build_fragment_explorer_view` to create
self-digested contracts. The exported view embeds its bounded request so
canonical parsing can replay the E05 decision and bind panel result, bundle,
method, quantity, unit, source state, and controls to exact verified sources.
`canonical_fragment_explorer_bytes` provides stable canonical JSON.
`fragment_explorer_from_canonical_bytes` rejects normalization drift, and
`replay_fragment_explorer_view` recomputes the full transformation from the
original request before accepting an exported view.
