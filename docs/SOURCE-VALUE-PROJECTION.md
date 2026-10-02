# E12 source-value projection adapters

Status: local synthetic contract. It does not authorize real provider
operation, clinical use, scientific claims, export, or Epic D release.

`evidence_inspector/source_value_projection.py` implements the closed
source-value projection family from `docs/E12-INTEGRATION-PLAN.md` ("Closed
source-value projection family"). It is a set of pure functions with no
registry, no I/O and no generic numeric fallback. A value leaves an E07, E08 or
E09 artifact only through one of four versioned projection contracts, and only
for a coordinate and statistic named by a `ResolvedProjectionPolicy` from the
protected projection-policy registry (`docs/PROJECTION-POLICY-REGISTRY.md`).

## Caller obligations (the E12 builder)

The adapter takes an artifact and a policy. It does not find artifacts and does
not re-verify live source authority. The E12 builder must:

- pass a policy it obtained from `ProjectionPolicyRegistry.resolve` under the
  composite fence. The adapter revalidates the policy contract but cannot prove
  registry membership: every digest in `ResolvedProjectionPolicy` is unkeyed, so
  a validly constructed policy that was never registered is indistinguishable.
  Provenance belongs to the registry and the builder, as for artifacts;
- pass an artifact it resolved from the family-source registry under the
  composite authority fence, after E04/E06 current-authority re-verification;
- pass the D05 `MeasurementAnchor` of its live-resolved manifest;
- pass a `SourceMeasurementIdentity` (method reference, E01 method digest,
  quantity ID, unit) taken from the live E04/E06 source, not from the policy;
- for a CNA policy, pass `CnaReplayInputs`: the exact E09 upstream dosage and
  segmented results and both `ExplorerInputAuthority` values;
- use only the output of `project_source_values`, or check a stored projection
  with `verify_source_value_projection`.

## Entry points

```python
project_source_values(
    policy, artifact, *, measurement_anchor, source_measurement, cna_inputs=None
) -> SourceValueProjectionSetV1

verify_source_value_projection(
    projection, policy, artifact, *, measurement_anchor, source_measurement,
    cna_inputs=None,
) -> SourceValueProjectionSetV1
```

Neither takes a value, subset, ordering or selector. Every coordinate comes
from the resolved policy or, for `canonical_all_components`, from the complete
artifact.

`project_source_values`:

1. captures the policy as bounded exact bytes (`exact_model_bytes`) and
   reparses it, so a `model_construct` forgery, subclass, foreign nested object,
   or a rule or statistic outside the closed vocabulary (for example `top`) is
   rejected;
2. captures the anchor and source identity the same way, then calls
   `require_projection_policy_binding` with them;
3. requires `cna_inputs` exactly when the policy family is CNA;
4. rejects an artifact or CNA input whose object graph holds a foreign or
   subclassed node (`exact_model_bytes` over the family's type graph), then
   captures the artifact as canonical bytes, reparses it with the family
   parser, and reruns the family replay function (E07
   `replay_fragment_explorer_view`; E08 `_build_replay_view`, the function the
   artifact validator itself uses; E09 `replay_cna_explorer_snapshot` over
   reparsed inputs). Only the reparsed artifact is read after that;
5. resolves each coordinate from both the chart/layer and the exact table, and
   requires each to match exactly once and to agree;
6. returns a `SourceValueProjectionSetV1`, ordered by coordinate then statistic.

`verify_source_value_projection` reparses a projection set, reruns
`project_source_values`, and requires byte-identical canonical output. A
caller-created scalar, an edited value, a reordered or truncated vector, or a
projection moved onto another artifact or policy fails with
`SourceValueProjectionForged`.

## Contracts

All are closed (`extra="forbid"`), frozen and versioned. Every projection
carries a `ProjectionPolicyBindingV1` (registry ID and epoch, the exact
registry state version and head it was resolved under, selector, version,
object and policy digests, rule) and a `ProjectionMeasurementV1` (D02
method reference, E01 method digest, quantity, unit, D02 measurement-definition
digest, D05 anchor), plus the SHA-256 of the canonical artifact bytes. The
statistic unit must be the registry's controlled unit for the statistic.

| Contract | Coordinate | Value | Uncertainty |
| --- | --- | --- | --- |
| `FragmentLongitudinalValueProjectionV1` | panel, fragment quantity, chart `bin_index` and exact half-open bounds | `count`: exact integer; `fraction`: exact `fraction_numerator`/`fraction_denominator` over the eligible-alignment denominator (never a float) | none; E07 has none |
| `CellOriginLongitudinalValueProjectionV1` | atlas ID/digest and registered `contributor_id` | `point_estimate` | `interval_state`; bounds only when the state is `available` |
| `CnaChromosomeLongitudinalValueProjectionV1` | dosage grid digest, `chr1`..`chr22` | `integer_value` for `accepted_read_count`, else `real_value` | none |
| `CnaSegmentLongitudinalValueProjectionV1` | segmented grid digest, segment index and zero-based half-open interval | `integer_value` for copy number and bin counts, `real_value` for `median_log2` | none |

Family bindings: E07 carries the view digest, result and bundle IDs and digests
and the E02 chart digest. E08 carries the request digest, result, bundle,
cell-origin method, atlas and E06 authority-head digests, and the digest of the
exact table row. E09 carries the grid digest and the source's input result and
authority digests from the snapshot provenance.

## Per-family resolution

Fragment (E07). The panel is `left` for `a` and `right` for `b`; its source is
the one request source with the panel's result ID. Panel and source quantity
must be the policy's. The embedded E05 record must equal the builder's source
identity. A non-complete panel is `SourceValueWithheld`. A bin is resolved
from the verified E02 chart by index; the chart row's bounds must equal the
policy's (a shifted bin fails), the E02 measurement histogram row must agree,
and the bounds must appear exactly once in the chart, the panel rows and the
panel's accessible-table rows. Count, numerator and denominator must agree
across all of them and with the eligible-alignment denominator. Because E07
panel rows are filtered by display controls, a bin hidden by the bin window or
count threshold matches zero panel rows and fails; canonical-all therefore
requires a view that displays every bin.

Cell origin (E08). The artifact must be `ready`, the atlas must equal the
policy's in the view binding and the deconvolution, and the embedded record
must equal the builder's source identity. A contributor must appear exactly
once among the deconvolution estimates (otherwise unregistered or ambiguous),
the dot/interval rows and the exact table rows. The dot and table rows must
agree, and both must equal the estimate and bootstrap interval in the result.
Canonical-all requires the estimates, dot rows and table rows to name the same
contributors and orders them by contributor ID, not by the view's estimate
rank.

CNA (E09). The snapshot must be `available`. The artifact's grid for the
family's source must equal the policy grid and its digest. A dosage chromosome
must be declared by the grid and appear exactly once in the chart, the layers
and the upstream dosage chromosome table, all equal. A segment is found by
index in the chart, the table and the layers; its interval must equal the
policy's (a shifted segment fails), the interval must appear exactly once in
the chart and table, all three must be equal, and the upstream segmented result
must carry the same interval and values. Dosage and segment projections are
different contracts on different grids and never alias.

## Errors

All are `SourceValueProjectionError` (a `ValueError`) subclasses and are raised
before any value is returned:

| Error | Cause |
| --- | --- |
| `SourceValuePolicyRejected` | not an exact, valid resolved policy |
| `SourceValueFamilyMismatch` | wrong artifact family, panel identity, atlas, grid, or CNA inputs for a non-CNA family |
| `SourceValueMeasurementMismatch` | D05 anchor, D02 tuple, quantity or unit mismatch; the artifact's record is not the builder's source |
| `SourceValueCoordinateUnresolved` | zero or several matches, shifted bin or segment, unregistered contributor, undeclared contig |
| `SourceValueRepresentationDrift` | chart/layer and table representations disagree |
| `SourceValueWithheld` | the artifact withholds values for the source |
| `SourceValueReplayRejected` | the artifact or CNA inputs do not reparse or replay |
| `SourceValueProjectionForged` | a projection is not the adapter's exact output |

## Threat model

In-process code mutation is out of scope. Caller-built objects are not trusted:
artifacts, policies, inputs and projection sets are checked for exact object
graphs, reparsed and replayed before use. A caller that builds a fully valid
policy or artifact from invented content is not detectable here; resolving
both from their protected registries is the builder's obligation. The
adapter checks only what the artifact and policy carry; live authority belongs
to the caller.

## Tests

`tests/test_source_value_projection.py` uses the real E07/E08/E09 fixture
artifacts from the explorer test modules and policies registered in and
resolved from a real `ProjectionPolicyRegistry`. Drift and duplicate cases are
checked twice: with replay on (the family replay rejects them) and with the
family replay disabled by monkeypatch, so that the adapter's own chart/table and
exactly-once checks are exercised. Mutation checks were run against chart/table
equality per family, exactly-once matching, canonical-all subset emission per
family, display-rank ordering, shifted bin and segment checks, policy capture
and verification equality.

## Open decisions

- E09 carries no E01/D02 identity. The CNA D02 binding is only between the
  policy and the builder's source identity; the snapshot's method layer is not
  linked to an E01 method.
- Canonical-all dosage emits `chr1`..`chr22` and requires each to be declared
  by the dosage grid, so a grid missing an autosome cannot produce a complete
  vector.
- Canonical-all fragment requires an E07 view whose controls display every
  bin. Whether E12 should build a dedicated full-window view, or project from
  the E02 chart alone, is open.
- E08 replay uses the private `_build_replay_view`; E08 exports no public
  replay function.
- The projection binds the registry state head, so the same policy resolved
  after an unrelated registration yields a different projection. E12 must
  decide whether to compare projections across heads by policy digest only.
- A projection set can hold up to `MAX_PROJECTED_COMPONENTS` (every E09
  segment times every segment statistic). No smaller E12 bound is set yet.
