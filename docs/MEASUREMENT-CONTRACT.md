# Synthetic BAM preflight and aligned reference-span contract

Status: first-wave synthetic implementation. Nothing in this contract qualifies
real genomic data, hg38 assets, Dorado, hardware, a scientific threshold, or a
wet-lab protocol. The public Streamlit demo is unchanged.

## Boundary

The S3 APIs accept local paths only as runtime arguments. Those paths must name
BAM and index bytes already captured under a sealed, runner-owned S2 snapshot.
Mutable delivery paths are not valid stage inputs. Paths, filenames, read IDs,
sequence, qualities, and reusable raw genomic hashes never enter preflight or
measurement results.

## BAM preflight

`validate_bam_snapshot(bam_path, index_path, registered_reference, policy)`
returns the shared privacy-safe `PreflightReport` contract.

A BAM is eligible for the aligned reference-span scan only when all of these
conditions hold:

- samtools quickcheck reaches a valid BAM end-of-file marker;
- the header declares coordinate sort and a complete record scan proves
  nondecreasing `(reference_id, reference_start)` order, with unmapped records
  last;
- the supplied BAI/CSI opens, reports an index, and its mapped/unmapped totals
  reconcile with the complete BAM scan;
- ordered `@SQ` names and lengths exactly match the registered reference; and
- every `@SQ` row carries the registered assembly ID (`AS`) and lowercase MD5
  provenance (`M5`).

Failure of any item is blocking (`TBX-BAM-001` or `TBX-BAM-002`). Library error
text is not propagated because it can contain a local path.

Methylation readiness is independent. The synthetic validator requires the
registered model declaration in `@PG DS` and structurally consistent `MM`,
`ML`, and `MN` tags in the bounded primary-record sample. Missing provenance or
tags reports `TBX-MOD-001`; contradictory tags report `TBX-MOD-002`. Both are
future methylation ineligibility and do not block the aligned reference-span
measurement. Tags are never repaired heuristically.

## Executable measurement definition

The first-wave policy is explicitly `synthetic-only`:

- a span is the sum of CIGAR operations `M`, `D`, `N`, `=`, and `X`;
- each eligible primary alignment is one denominator unit; paired alignments
  are not merged into a molecule and are not excluded merely for being paired;
- records are tested in this fixed order: unmapped, secondary, supplementary,
  QC-failed, duplicate, unregistered contig, MAPQ below 20, missing CIGAR, zero
  reference span, invalid/overflowing span;
- MAPQ 20 is a synthetic workflow value, not a scientifically approved release
  threshold;
- every inspected record contributes to exactly one accepted span or one
  exclusion reason;
- histogram bins are ordered, contiguous, half-open `[lower, upper)`, with an
  explicitly unbounded final bin; and
- histogram counts and the eligible denominator must reconcile exactly.

The implementation streams records once and retains only integer aggregate
counts. It does not retain alignments, identifiers, or sequences. Aggregate and
chart order is canonical, so the same validated bytes and policy produce the
same canonical JSON bytes.

## Completion and publication

Complete end-of-file traversal with at least one eligible alignment is required
for publication. The scan result records `complete`, `capped`, `interrupted`,
or `failed`. A zero-eligible, capped, interrupted, or failed `MeasurementScan`
remains useful for local recovery diagnostics, but `finalize_measurement`,
`canonical_measurement_bytes`, and `chart_data` reject it with
`MeasurementUnavailableError`. Only `finalize_measurement` constructs the
shared `FragmentMeasurement` publication contract. No partial prefix can be
mislabeled as the research record.

## Remaining gates

This synthetic slice does not establish real-data or protocol qualification.
Before real processing, owners must approve the exact hg38 asset and digest,
workflow/tool/model versions, measurement and QC thresholds, paired-input
semantics if the supported route changes, real POD5 processing, OCI isolation,
workstation repeatability, signing custody, claims review, and operator study.
