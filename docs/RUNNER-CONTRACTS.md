# Traceback runner contracts

Status: first-wave synthetic contract, unapproved for real data.

These contracts support local development and orchestration tests only. They do
not qualify a protocol, MinION hardware, Dorado, hg38 assets, scientific
thresholds, or processing of real genomic data.

## Boundary rules

- Every persisted model has a fixed `traceback.*.v1` schema literal.
- Models reject unknown fields, non-finite numbers, mutable nested mappings,
  unsafe relative paths, and inconsistent aggregate totals.
- `canonical_json_bytes` emits UTF-8 JSON with sorted keys and fixed compact
  separators. `canonical_model_from_bytes` rejects noncanonical encodings.
- Local run packages may contain root-relative paths and ordinary SHA-256
  digests. Export provenance contains only opaque tokens and provider-keyed HMAC
  commitments. These two representations are not interchangeable.
- Result/export models contain no extension dictionary. New export content
  requires a schema revision and privacy review.

## Public interface

`traceback_runner.contracts` provides:

- run input: `LocalArtifact`, `LocalRunPackage`, `ArtifactCommitment`, and
  `ExportRunProvenance`;
- reference/release: `ReferenceContig`, `RegisteredReference`,
  `WorkflowStage`, and `WorkflowRelease`;
- runner: `JobRequest`, `ExecutionOptions`, `JobRecord`, `JobState`,
  `StageName`, `ArtifactDigest`, and `StageReceipt`;
- preflight: `PreflightCheck`, `PreflightReport`, and `PreflightOutcome`;
- measurement: `FragmentMeasurementPolicy`, `HistogramBin`,
  `ExclusionCounts`, `HistogramCount`, and `FragmentMeasurement`;
- export: `ResultBundleManifest`, `BundleContent`, and typed export provenance;
- operator source data: `CompatibilityManifest` and `CompatibilityItem`.

`traceback_runner.fixtures` generates temporary synthetic BAM/index files and
fake MinKNOW metadata. It never writes a checked-in sequence or signal file.

## Development report bundles

`traceback_runner.report_bundles` packages already-validated aggregate result,
plot-data, plot-spec, provenance, and accessible-table bytes. The v1 contract
is fixed to `copy-number-dosage-qc.v2`, method
`sample-internal-whole-chromosome-dosage-qc`, and qualification state
`development_unqualified`; it never authorizes product release.

The builder performs no plotting, scientific interpretation, runtime probing,
or network access. It writes canonical JSON artifacts in a fixed inventory and
atomically publishes a new directory without overwriting an existing one. The
manifest commits to each artifact's exact byte size and SHA-256 digest, and its
deterministic report identity binds the result to the plot data, plot spec,
provenance, and accessible table. Verification and replay reject unknown schema
identities, identity drift, extra or missing entries, symlinks, noncanonical
JSON, and any digest or size mismatch.

## Synthetic aligned reference-span policy

The v1 synthetic policy counts reference bases consumed by CIGAR operations
`M`, `D`, `N`, `=`, and `X`. It requires MAPQ 20, restricts measurement to the
policy's registered contigs, and excludes unmapped, secondary, supplementary,
QC-failed, and duplicate records. Each eligible primary alignment is counted
independently; paired records are not combined into a physical-fragment length.
Accordingly, output labels must say **aligned reference span per primary
alignment**, not physical fragment length.

Bins are contiguous half-open integer intervals. The final bin has
`upper_exclusive=null` and captures every remaining value. Eligible plus named
exclusion counts must equal scanned records, and bin counts must equal eligible
alignments. Interrupted, capped, and zero-eligible scans are unavailable and
cannot instantiate a publishable `FragmentMeasurement`.

Missing `MM`, `ML`, or `MN` tags makes future methylation work ineligible. It
does not block aligned reference-span measurement.

## Schema migration policy

There is no implicit coercion between schema versions and no in-place rewrite
of signed or receipt-bound artifacts. Readers accept only versions they name
explicitly. A future v2 requires:

1. a new model and schema literal, leaving v1 readable;
2. an explicit pure `v1 -> v2` migration with golden canonical-byte tests;
3. privacy and claims review for any exported field;
4. database backup plus expand/contract rollout;
5. active jobs remaining on their original workflow release or pausing at a
   declared safe boundary; and
6. new receipts/results after migration rather than mutation of history.

Unknown versions fail closed. Removing v1 read support is a separate release
decision and cannot occur while retained records or active jobs depend on it.
