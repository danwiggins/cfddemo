# Registered sensitivity and downsampling comparison

`evidence_inspector.sensitivity_comparison` is the pure E11 contract for
synthetic/local sensitivity inspection. It registers a complete study before
results exist, binds every result back to that registration, and emits a
framework-independent comparison view. It does not select molecules, execute
cell-origin analysis, choose a preferred method, or authorize clinical use.

## Exact source boundary

`SensitivitySource` combines:

- the E04 catalog reference;
- the ready E08 cell-origin explorer artifact;
- its canonical digest;
- the E05 verified record and E01 method/authority transitively bound by E08.

Construction requires the E04 bundle, method, registry, authority, scope, and
research-inspection identities to match the E08/E05 source exactly. The source
result, bundle, atlas, filter, method-definition, and input-molecule denominator
are carried into the registration and final view.

## Preregistered study grid

The subset family declares one immutable source molecule count, whole-molecule
selection, deterministic SHA-256 seeded ranking, nested subsets, one explicit
half-open edge-inclusion policy, sorted fraction levels, and sorted unique
replicate seeds. Fractions are integer parts per million; target counts use
registered floor rounding with a minimum of one. The full-molecule level is
mandatory. Subset IDs and fractions are independently unique.

Every level and replicate also has a preregistered membership commitment. The
commitment hashes a sorted, unique sequence of privacy-safe whole-molecule
content digests; raw molecule identifiers are not retained. Its derived subset
receipt binds the source explorer, result, bundle, method, atlas, filter,
family, edge policy, fraction, target, seed, and exact membership commitment.
The full-molecule commitment must be identical across seeds because sampling
cannot change membership at 100 percent.

Parameter sets bind the exact E01 method definition, atlas asset, and canonical
parameter digest. The parameters include CpG minimum, UXM thresholds, NNLS row
scale, solver tolerance, and maximum iterations. Every parameter set must use
the source method and atlas. Distinct parameter IDs cannot alias the same
parameter digest.

The run grid must equal the complete Cartesian product:

```text
subset levels × replicate seeds × parameter sets
```

The registration digest covers that complete grid. A caller cannot omit a cell,
reorder parameter sets, add a post-hoc run, or submit only a favorable result.

## Run evidence and attrition

Every registered cell has exactly one outcome: `complete`, `failed`, or
`insufficient_information`. Failed and insufficient runs remain visible and
cannot contain numerical estimates. Their controlled failure codes are counted
in the study attrition ledger.

Each run reconciles:

```text
selected molecules = accepted molecules
                   + edge-policy exclusions
                   + method exclusions
```

The selected count must equal the preregistered target. All parameter runs for
one subset level and replicate must share the same subset digest, proving they
used the same whole-molecule selection. Complete outputs bind exact result,
bundle, method, atlas, filter, seed, subset, and parameter identities.
The result digest is recomputed from the run key, attrition, subset, parameters,
and every numeric estimate; the bundle digest is recomputed from that result
and its complete execution binding. Swapping values while retaining result or
bundle identity therefore fails closed.

## Two different uncertainty concepts

Sampling uncertainty and method sensitivity are separate contract types:

- `SamplingEstimate` carries one run's bootstrap status and optional interval.
  Unavailable uncertainty has null bounds and never becomes a fake whisker.
- `MethodSensitivityRow` carries every registered parameter result for one
  subset, replicate, and contributor. Its min/max is labeled
  `registered_parameter_range_not_sampling_interval`; failed and insufficient
  parameter cells remain in the value list.

The compatibility block permits comparison only inside the registered
sensitivity grid, with the exact source quantity, unit, method, and atlas. It
explicitly does not infer longitudinal compatibility because downsampling
changes the denominator by design.

## Replay, privacy, and bounds

The final artifact embeds the complete registered bundle and derived view.
Canonical parsing rebuilds the view and requires exact equality. Contracts are
closed, immutable, finite-number safe, and bounded: at most 16 subset levels,
32 replicate seeds, 32 parameter sets, 4,096 run cells, and 512 contributors.
Canonical input/output is capped at eight MiB before JSON parsing.

Identifiers use controlled syntax and reject private identifier stems. No raw
molecule IDs, reads, sequences, paths, credentials, presentation aliases, or
network references are accepted or emitted. Tests use synthetic aggregate
digests and deterministic local fixtures only.

## Tradeoffs

- Whole-molecule selection is represented by a digest receipt rather than raw
  molecule identities; execution must produce that receipt outside this view
  layer.
- Full-grid retention makes artifacts larger but prevents silent omission and
  post-hoc cherry-picking.
- A registered parameter range is descriptive sensitivity evidence, not a
  confidence interval or validation claim.
- Failed runs reduce availability rather than being imputed or reported as
  zero.
