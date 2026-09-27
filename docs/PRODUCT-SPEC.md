# Traceback MinION-to-Research-Record Specification

Status: `/autoplan` reviewed; synthetic contract implementation may start,
real-data execution remains gated  
Product boundary: research-use software, one qualified MinION workflow, hg38  
Primary input: complete MinKNOW run directory with retained POD5  
Fast-path input: compatible coordinate-sorted modBAM plus BAI/CSI

## 1. Objective

Traceback guides a sequencing operator from a completed qualified run to a
reproducible cfDNA research record. Its release-one promise is simple: select a
supported input and receive a signed aligned reference-span record, or one exact
remediation, without bioinformatics expertise.

The first release succeeds when a trained operator who is not a
bioinformatician can:

1. verify that the sample, MinION host, software, storage, flow cell, and run
   configuration are compatible;
2. select a completed MinKNOW run directory;
3. obtain one deterministic fragment-length research record without composing
   shell commands;
4. understand and correct any blocking failure from the product;
5. reproduce the same measurement JSON from the same input and workflow
   release; and
6. prove that no raw sequence or read identifiers crossed the configured local
   data boundary.

## 2. Users

### Primary: sequencing operator

A trained laboratory or sequencing-service operator who can perform the
approved wet-lab protocol and use MinKNOW but should not need to understand
Dorado, SAM tags, reference indexing, modkit, or workflow orchestration.

### Secondary: research customer

The owner of the sample who sees technical status, neutral measurements,
methods, limits, provenance, retention controls, and compatible longitudinal
comparisons. The customer does not configure the pipeline.

### Tertiary: support and workflow administrator

An authorized operator who qualifies protocol versions, publishes signed
workflow releases, inspects redacted support bundles, and retires incompatible
software or reference versions.

## 3. Product boundaries

### In scope

- One qualified, provider-performed cfDNA collection and native-library
  protocol.
- MinION Mk1D with a declared supported flow cell and sequencing kit.
- MinKNOW run-folder discovery and validation.
- POD5 retention, inventory, integrity, and local custody.
- Pinned Dorado canonical basecalling and hg38 alignment. POD5 is retained so a
  later qualified workflow can perform modified-base calling.
- Coordinate sorting, indexing, and modBAM validation.
- Resumable local execution with stable error codes.
- Aligned reference-span distribution as the only release-one measurement.
- Signed aggregate result bundles with no sequence, read ID, or local path.
- Public Protocol & Setup documentation generated from versioned source data.

### Explicitly out of scope for the first release

- Direct control of MinION hardware or starting/stopping a MinKNOW run.
- Untrained or self-administered phlebotomy.
- Arbitrary collection tubes, extraction methods, library kits, providers,
  references, flow cells, or basecalling models.
- Clinical interpretation, disease probability, screening, diagnosis,
  treatment advice, or reassurance.
- Real-time biological interpretation while sequencing.
- Sending POD5, FASTQ, BAM, modBAM, read IDs, or unsalted genomic hashes beyond
  the provider-controlled environment.
- CRAM, legacy FAST5 processing, multi-sample multiplexing, adaptive sampling,
  and references other than the registered hg38 release.
- Publishing cell-origin or dosage measurements before their separate
  analytical and claims gates pass.
- Hosted accounts, portals, uploads, subscriptions, billing, kits, logistics,
  watched directories, remote inboxes, and automatic workflow updates.
- Production support for more than one qualified workstation profile.

## 4. Source-of-truth artifacts

| Artifact | Required | Purpose | Retention rule |
|---|---:|---|---|
| MinKNOW sample sheet | Yes | Sample, kit, flow-cell, reference metadata | Retain with run |
| MinKNOW final summary | Yes | Run-level acquisition evidence | Retain with run |
| Sequencing summary | Yes when emitted | Per-read technical QC | Local only |
| Output hash file | Preferred | File-integrity evidence | Retain with run |
| POD5 | Yes for full path | Original electrical signal | Never delete automatically |
| Basecalled BAM/modBAM | Derived | Sequence, quality, provenance, modifications | Provider policy |
| Sorted aligned modBAM | Derived | Analysis-ready registered input | Provider policy |
| BAI or CSI | Derived | Indexed coordinate access | Retain with modBAM |
| FASTQ | Optional | Interoperability only | Never treated as methylation-capable |
| Workflow manifest | Yes | Exact tools, models, references, parameters | Immutable |
| Aggregate result bundle | Yes | Publishable research record | Cloud-eligible after validation |

POD5 is the reproducible raw source. FASTQ is never accepted as evidence that
modified-base probabilities exist. A BAM is methylation-eligible only when its
header declares the modified-base model and sampled records contain valid
`MM`, `ML`, and `MN` tags.

## 5. Operator journey

### 5.1 Prepare

The operator opens **Protocol & Setup** and selects the registered workflow.
Traceback shows:

- required and recommended equipment;
- provider-approved blood collection and processing SOP;
- consumables and controls;
- supported MinION, flow cell, kit, MinKNOW, Dorado, OS, GPU, memory, and disk;
- estimated local storage for the planned run;
- data-retention and privacy requirements; and
- a printable readiness checklist.

### 5.2 Check this computer

`traceback doctor` reports `PASS`, `WARN`, or `BLOCKED` for:

- supported OS and architecture;
- CPU features;
- memory;
- local SSD capacity and projected peak use;
- MinKNOW and Dorado versions;
- accelerator availability;
- samtools, modkit, and reference assets;
- write access and atomic rename support;
- clock sanity;
- network availability only where the selected workflow needs it; and
- local data-boundary configuration.

The command performs no AWS call and reads no sequence data.

### 5.3 Validate run configuration

Before collection or sequencing, the operator can import or generate a sample
sheet. Traceback validates:

- pseudonymous sample ID syntax;
- one sample and one supported protocol;
- flow-cell and kit compatibility;
- native-DNA preparation;
- POD5 retention;
- expected basecalling and modified-base model;
- registered hg38 reference;
- output directory and free-space reserve; and
- controls and required metadata.

This is advisory in release one. MinKNOW remains the system that controls the
device.

### 5.4 Import a completed run

The operator selects the MinKNOW base output directory. Traceback inventories
files without copying them, computes an immutable local run identity from
non-sensitive metadata plus locally scoped digests, and performs preflight.

No job starts until MinKNOW reports completion and the runner has committed an
immutable input snapshot. A watched live directory may display acquisition
progress, but it is never itself an analysis input.

### 5.5 Process

For POD5 input, the runner:

1. resolves a signed workflow release;
2. verifies the POD5 inventory;
3. runs the pinned Dorado canonical model;
4. aligns to the registered hg38 FASTA/index;
5. sorts and indexes the aligned modBAM;
6. validates alignment and header provenance;
7. runs technical QC;
8. performs a complete streaming aligned reference-span scan;
9. validates aggregate outputs;
10. writes and signs the local result bundle.

For compatible modBAM input, steps 3 through 5 are skipped and their provenance
is validated from the header and manifest.

### 5.6 Review and recover

The operator sees current stage, elapsed time, disk use, latest checkpoint, and
one primary action. An interrupted job resumes from the last verified
checkpoint. A changed input invalidates dependent checkpoints rather than
silently reusing them.

### 5.7 Complete

The local record becomes available only after:

- required technical QC passes;
- every numeric field validates against a registered aggregate artifact;
- the claims firewall passes;
- provenance and limitations are complete; and
- the bundle signature verifies.

## 6. Run-package contract

### 6.1 Discovery

The importer recognizes a MinKNOW directory by required metadata files and
format-specific subdirectories, not by its directory name alone. Pooling and
barcode layouts are rejected in release one unless the manifest declares the
single accepted sample unambiguously.

### 6.2 Local and export manifests

Traceback keeps two representations:

- `input-manifest.local.json` contains root-relative locators and ordinary
  SHA-256 digests for local integrity, restart, and deduplication.
- `input-provenance.json` contains provider-keyed HMAC commitments and no path,
  filename, read ID, sequence-file SHA-256, or other reusable genomic
  fingerprint.

Only the export representation may be deliberately saved outside the runner's
private data root.

```json
{
  "schema_version": "traceback.run-package.v1",
  "run_id": "local opaque identifier",
  "protocol_run_id": "MinKNOW UUID",
  "sample_id": "pseudonymous value",
  "device_id": "MinION identifier",
  "flow_cell_id": "flow-cell identifier",
  "flow_cell_product_code": "registered code",
  "kit": "registered code",
  "started_at": "ISO-8601",
  "completed_at": "ISO-8601 or null",
  "minknow_version": "exact version",
  "basecall_model": "exact model or null",
  "modified_base_models": [],
  "reference_id": "registered hg38 identifier or null",
  "artifacts": [
    {
      "role": "pod5",
      "relative_token": "non-reversible local token",
      "size_bytes": 0,
      "sha256_local": "local-only digest",
      "export_commitment": "provider-keyed HMAC",
      "stable": true
    }
  ]
}
```

Artifact ordering is deterministic. Absolute paths, symlinks that escape the
configured root, sockets, device files, raw paths, read IDs, sequences, and
globally reusable hashes are forbidden in export-eligible serialization.

### 6.3 Stability rule

An artifact is stable only when:

- its size and modification timestamp remain unchanged across the configured
  observation interval;
- it can be opened by the format validator;
- the run is complete, or an explicit offline-processing snapshot was created;
  and
- any declared MinKNOW output hash matches.

After hashing, the runner restats each accepted file. A changed size or
modification time invalidates the snapshot before it can be committed.

## 7. Workflow release contract

Each immutable signed release declares:

- supported MinION, chemistry, flow cell, kit, and protocol versions;
- MinKNOW input contract;
- container or executable digests;
- Dorado canonical and modified-base model identifiers;
- hg38 FASTA and index digests;
- samtools and modkit versions;
- ordered stages and dependencies;
- CPU, accelerator, memory, disk, and timeout requirements;
- QC and measurement eligibility rules;
- stable errors and remediation links;
- aggregate result schemas;
- checkpoint compatibility keys;
- claims-policy version; and
- bundle-signing key identifier.

The runner refuses unsigned, expired, revoked, or incompatible releases.

## 8. Local job state machine

```text
DISCOVERED
  → WAITING_FOR_FINALIZATION
  → SNAPSHOTTING
  → VALIDATING
  → READY
  → BASECALLING
  → ALIGNING
  → SORTING_INDEXING
  → TECHNICAL_QC
  → MEASURING
  → VALIDATING_OUTPUT
  → SIGNING
  → COMPLETE

Any running state → PAUSED | RETRYABLE_FAILURE | TERMINAL_FAILURE
COMPLETE → SUPERSEDED only by an explicit newer analysis
```

Every transition records job ID, stage attempt, workflow release, input
identity, timestamp, reason, and checkpoint outputs. Replaying the same request
with the same idempotency key returns the existing job.

## 9. Preflight and stable errors

| Code | State | Meaning | Required remediation |
|---|---|---|---|
| `TBX-RUN-001` | BLOCKED | Directory is not a recognized MinKNOW run | Select the base run directory |
| `TBX-RUN-002` | WAITING | Run is active or output is changing | Wait for automatic recheck |
| `TBX-RUN-003` | BLOCKED | Required run metadata is missing or contradictory | Restore metadata or obtain provider manifest |
| `TBX-POD5-001` | BLOCKED | No readable POD5 is available | Recover POD5; FASTQ alone cannot restore it |
| `TBX-POD5-002` | BLOCKED | POD5 inventory is corrupt or incomplete | Recopy affected batch and retry |
| `TBX-BAM-001` | BLOCKED | BAM is unreadable, truncated, unsorted, or unindexed | Regenerate/sort/index it |
| `TBX-BAM-002` | BLOCKED | Contigs do not match registered hg38 | Realign to the registered reference |
| `TBX-MOD-001` | FUTURE_INELIGIBLE | Modified-base provenance or `MM/ML/MN` tags are absent | Fragment processing may continue; later cell-origin work requires re-basecalling |
| `TBX-MOD-002` | FUTURE_INELIGIBLE | Modification tags fail structural validation | Fragment processing may continue; do not repair tags heuristically |
| `TBX-SYS-001` | BLOCKED | Peak disk reserve is insufficient | Free space or select approved storage |
| `TBX-SYS-002` | BLOCKED | Required accelerator/tool is unavailable | Install supported runtime or use qualified host |
| `TBX-JOB-001` | RETRYABLE | Tool exited or host restarted after checkpoint | Resume job |
| `TBX-JOB-002` | BLOCKED | Input changed after checkpoint | Start a new job identity |
| `TBX-OUT-001` | BLOCKED | Aggregate artifact violates schema or privacy policy | Quarantine output and inspect locally |
| `TBX-SIGN-001` | BLOCKED | Workflow or result signature does not verify | Refresh trusted release/key; never publish |

Every error shown to an operator includes problem, likely cause, exact fix,
documentation link, stage, job ID, retryability, and a copyable redacted
diagnostic.

## 10. Measurement eligibility

### Aligned reference-span distribution, release-one measurement

Requires a coordinate-aligned BAM/modBAM matching registered hg38. It is the
number of reference bases consumed by `M`, `D`, `N`, `=`, and `X` CIGAR
operations for eligible primary alignments at the registered mapping-quality
threshold. Release one performs a complete streaming scan; an ordered prefix,
record cap, or interrupted scan cannot produce the publishable measurement.
Computation, charting, documentation, and validation use this definition.

### Cell-origin methylation

Requires a validated aligned modBAM with compatible modified-base model,
`MM/ML/MN` tags, registered Loyfer resources, sufficient marker overlap, and
separate analytical-release approval. Reuse the bounded loaders, UXM
classification, count-weighted NNLS, and seeded bootstrap code.

### Broad chromosome dosage

Requires a validated aligned BAM and separate analytical-release approval.
The existing algorithm remains explicitly experimental and is not represented
as ichorCNA or a tumor-fraction estimate.

Failure of one optional measurement creates a partial technical record; it does
not erase independently valid measurements.

## 11. Result bundle

```text
traceback-result/
├── manifest.json
├── qc.json
├── measurements/
│   └── fragment-length.v1.json
├── provenance.json
├── limitations.json
├── charts/
│   └── fragment-length.v1.json
├── report.html
├── checksums.sha256
└── bundle.sig
```

Requirements:

- canonical JSON with finite numbers and versioned schemas;
- byte-identical measurement JSON for identical inputs and workflow release;
- no sequence, base qualities, read IDs, raw/local paths, or unsalted genomic
  hashes;
- complete denominator, filters, units, reference, model, tool, and asset
  provenance;
- deterministic chart data separate from visual rendering;
- claims-controlled text built from approved templates; and
- offline verification through `traceback verify`.

## 12. Public Protocol & Setup page

The page is generated from a versioned compatibility manifest and contains:

1. scope, trained-personnel boundary, and research-use limits;
2. provider-approved blood draw and preanalytical timeline;
3. materials with required/recommended/optional labels;
4. supported MinION, flow cell, kit, and host computer;
5. minimum and recommended acquisition/processing configurations;
6. MinKNOW run settings and sample-sheet example;
7. live-run technical checks;
8. expected files, sizes, retention, and privacy;
9. post-run processing and expected duration;
10. failure/recovery matrix;
11. printable checklist; and
12. source, protocol version, compatibility date, and change log.

Exact blood volume, tube, centrifugation, extraction, library-preparation, and
timing instructions remain `UNAPPROVED` until the scientific owner registers a
versioned SOP and its evidence. The published page fails closed rather than
substituting generic instructions.

## 13. Security and privacy

- The runner binds to localhost by default and authenticates privileged local
  operations.
- Tool arguments are argv arrays; no sequence-derived value enters a shell
  command.
- Input paths are canonicalized and restricted to configured roots.
- Symlinks escaping an input root and non-regular input files are rejected.
- Workflow releases and reference assets are verified before execution.
- Logs redact paths, read IDs, sample labels, command output, and environment
  secrets.
- Cloud upload uses an allowlist schema and rejects unknown fields.
- Model prompts receive only allowlisted aggregate evidence.
- Deletion is explicit, scoped, logged, and never includes POD5 automatically.
- Support bundles contain configuration, stable codes, versions, resource
  metrics, and redacted events only.

## 14. Performance and resource targets

- `traceback doctor`: under 10 seconds without downloading assets.
- Run-package inventory: streams metadata; memory stays below 512 MB.
- Preflight: under five minutes for a completed single-flow-cell run, excluding
  full cryptographic hashing when a verified MinKNOW hash exists.
- Runner: bounded concurrency with one heavy job per supported workstation by
  default.
- Disk preflight reserves input, intermediate, final, and 20% safety headroom.
- Interrupted stages resume without repeating a verified completed stage.
- UI status updates at least every 15 seconds from the local runner.
- `traceback demo`: completes on synthetic fixtures in under five minutes,
  offline and without an account.

Published hardware and duration estimates must be measured for one qualified
post-run workstation profile. Vendor minimums for acquisition are context, not
a Traceback processing-support promise.

## 15. CLI

```text
traceback doctor [--json]
traceback protocol show [--print]
traceback sample-sheet create --workflow <release>
traceback preflight <run-folder-or-modbam> [--json]
traceback run <run-folder-or-modbam> [--json]
traceback status <job-id> [--json]
traceback logs <job-id>
traceback pause <job-id>
traceback resume <job-id>
traceback retry <job-id>
traceback inspect <bundle>
traceback verify <bundle>
traceback support-bundle <job-id>
traceback demo
```

Human-readable output is the default. JSON output and exit codes are stable
interfaces. Commands never infer permission to upload or delete data.

## 16. Acceptance criteria

### End-to-end

- Synthetic fixtures exercise orchestration and failure states without claiming
  to qualify Dorado. A real, consented POD5 qualification dataset exercises the
  pinned toolchain on the supported workstation.
- A registered real run completes from run-folder selection without an operator
  entering a shell command.
- A compatible modBAM follows the fast path and records validated upstream
  provenance.
- The same analysis-ready input and workflow release produce byte-identical
  measurement JSON on the qualified workstation profile.

### Failure and recovery

- Active/changing run folders do not start accidentally.
- Missing POD5 blocks the raw-signal path. Missing or malformed modification
  tags never block the release-one aligned reference-span record.
- Wrong reference, corrupt batches, low disk, tool crash, restart, duplicate
  delivery, and changed input are covered by deterministic tests.
- Retrying never creates duplicate records or repeats verified work.

### Privacy

- Automated tests seed sequences, read IDs, paths, sample labels, and secrets
  into every upstream field and prove none appear in export-eligible bundles,
  logs, UI state, or support bundles.
- Raw genomic files remain inside the provider-controlled runner environment.
- Export rejects unknown fields and invalid signatures.

### Documentation

- A new operator finds required materials and computer requirements in under
  two minutes.
- Required, recommended, and optional items are distinguishable without color.
- The page and printable checklist are generated from the same manifest.
- Every exact wet-lab instruction has a protocol owner, version, source,
  approval state, and last-reviewed date.
- Unsupported combinations fail the compatibility checker with a specific fix.

### Claims

- Prohibited clinical terms fail publication tests across UI, reports, exports,
  and support templates.
- No technical state is represented as a health state.
- Every measurement displays its limits beside the chart.

## 17. Rollout and rollback

1. Run entirely offline on synthetic fixtures.
2. Shadow-process existing consented provider runs without publishing records.
3. Compare outputs and operational failures with manual processing.
4. Enable one qualified provider and one workflow release.
5. Publish fragment records only.
6. Add longitudinal comparison after repeatability gates.
7. Enable cell-origin and dosage independently after validation.

Workflow releases are immutable. Rollback revokes a release for new jobs,
restores the previous signed release, and leaves existing research records
visible with their original provenance. Reanalysis creates a new superseding
record; it never edits history.

## 18. Existing code leverage

| Need | Existing code | Decision |
|---|---|---|
| BAM streaming and bounded extraction | `evidence_inspector/preparation.py` | Reuse pure logic; wrap in runner stage |
| Aligned-span fragment computation | `evidence_inspector/fragmentomics.py` | Reuse with one locked definition |
| Modified-base extraction and validation | `evidence_inspector/cell_origin_pipeline.py` | Reuse after modBAM eligibility |
| UXM classification and NNLS | `uxm.py`, `deconvolution.py` | Reuse after analytical gate |
| Broad dosage calculation | `copy_number.py` | Keep experimental and separately gated |
| Strict evidence/result models | `models.py`, `cell_origin_models.py` | Extend with versioned runner contracts |
| Bounded AI review | `reviewer.py`, `checks.py` | Keep downstream of deterministic results |
| Demo UI | `app.py` | Preserve as demo; do not use as runner architecture |

## 19. `/autoplan` decision record

### Verdict

Conditional go for a bounded provider-side automation pilot. The operator is
the user; the initial buyer hypothesis is the sequencing provider or research
program. Consumer membership remains an unproven later hypothesis.

The 10-star product is a compiler for qualified sequencing runs: one explicit
start, fail-fast compatibility checks, crash-safe resume, exact remediation,
and a portable signed record whose measurements can be independently verified.

### MVP sequence

1. Prove the complete signed-record vertical slice with compatible modBAM.
2. Add completed MinKNOW/POD5 import, canonical basecalling, and alignment.
3. Qualify the two routes on one provider protocol and one workstation.
4. Run a paid provider pilot with assisted installation and manual billing.

The modBAM slice is the first implementation milestone, not completion of the
MinKNOW-to-record product.

### Local product information architecture

```text
Jobs
├── Needs attention
├── Processing
├── Queued
├── Completed records
└── New analysis

Protocol & Setup
├── Qualified workflow and approved checklist
├── Acquisition readiness
├── Processing readiness
└── Expected files and retention

System
├── Runner health and freshness
├── Storage and approved locations
├── Workflow release and assets
└── Redacted diagnostics
```

The jobs view prioritizes blocker, affected pseudonymous sample, and next
action; then running work and freshness; then completed history. It never sorts
or summarizes jobs by biological values.

### Interaction decisions

- Jobs start only after an explicit operator action. A FIFO queue rechecks
  resources immediately before execution.
- Runner freshness, job progress, and record validity are separate states.
- After three missed 15-second polls, status is stale and duplicate start is
  disabled.
- Progress is determinate only when backed by files, bytes, records, or fixed
  stages.
- A failure names what remains valid, what will repeat, owner, exact fix,
  retryability, error code, and redacted support action.
- No chart appears before the complete scan, validation, and signature pass.
- Zero eligible records means `Measurement unavailable`, never zero.
- Completion language is `Signed local record ready`; no upload is implied.

### Architecture decision

Build a separate `traceback_runner` package. Use SQLite WAL for local jobs,
immutable stage attempts and receipts, expiring leases, atomic artifact
publication, argv-only rootless OCI execution with networking disabled, and
separate release-signing and result-signing Ed25519 keys. Do not evolve
Streamlit session state into product infrastructure.

Each reusable stage receipt binds the workflow and stage digests, ordered input
digests, image and observed tool versions, parameters, reference assets,
outputs, and postcondition results. A stage is reused only after its receipt
and every output digest verify.

### Blocking engineering contracts

Before real genomic data is processed:

1. The runner copies, reflinks, or content-addresses accepted inputs into a
   runner-owned read-only snapshot, fsyncs and seals its manifest, and verifies
   the delivery did not change during capture. Stages never read mutable input.
2. Leases carry monotonically increasing fencing tokens. Artifact adoption and
   success commits reject stale tokens.
3. Every attempt writes privately, validates and fsyncs outputs, seals a
   receipt, atomically publishes, then commits success. Startup adopts an orphan
   exactly once from a verified receipt or quarantines it.
4. Rootless stages receive read-only input/reference mounts, one writable
   attempt mount, bounded resources, no host socket or inherited secrets, and
   zero network egress.
5. Release and result signatures use distinct Ed25519 keys. Generation,
   protected custody, public trust distribution, rotation, revocation,
   lost-key response, and offline verification are defined. Development keys
   can never verify pilot data.
6. A versioned executable measurement contract fixes CIGAR operations, flags,
   mapping quality, contigs, duplicates, pairing, overflow, binning, exclusions,
   denominator, and completion.
7. Database upgrades use backup plus expand/contract migration. Active jobs
   remain on their original release or pause at a declared safe boundary, and
   the prior runner remains available through rollback.

Identical analysis-ready BAM input must produce byte-identical measurement JSON.
POD5-to-record reproducibility is a separate qualification test on the exact
supported host, Dorado release, model, and reference assets.

### First-five-minutes contract

Assisted installation ends with `traceback doctor`, `traceback demo`, and
`traceback verify <synthetic-bundle>`. This path needs no account, genomic
input, cloud credential, or network, and completes within five minutes.

Documentation includes exact example modBAM and POD5 delivery packages,
versioned CLI/JSON/exit-code contracts, and remediations that perform one safe
action or name the escalation owner. Workflow developers receive a stage
template, synthetic fixture generator, development trust root, and one
`traceback workflow test` conformance command.

### Commercial and qualification gates

- Two qualified providers accept a priced pilot within six weeks; at least one
  pays.
- The scientific owner approves the protocol, measurement definition, QC, and
  repeatability thresholds.
- One workstation profile is qualified with real POD5 data.
- At least 19 of 20 consecutive eligible pilot runs finish without engineering
  intervention, counting every attempted delivery.
- Median operator processing/report-preparation time falls at least 50%.
- The pilot has positive contribution margin and at least one buyer purchases
  another processing block at the intended price.

If paid demand requires a health conclusion, or two qualification iterations
cannot meet the agreed cost, duration, repeatability, privacy, and integrity
limits, stop general product development and reassess the regulated lane.

### Review limitations

The requested Astra subagent endpoint was unavailable. An independent Astra
review was completed through the local Codex CLI instead, alongside the primary
reviewer and specialized product, design, engineering, and operator reviews.

### Final phase verdicts

| Review | Verdict |
|---|---|
| CEO | Conditional go for provider automation; reject a platform-scale MVP |
| Design | Go with the local job-centered information architecture |
| Engineering | Go for synthetic contracts; real data remains gated |
| Operator/developer experience | Pilot handoff waits for install, examples, conformance, and recovery gates |

## 20. Open approval gates

- Scientific approval of exact collection, plasma, extraction, and native
  library SOP.
- Exact supported flow cell, sequencing kit, and controls.
- Pinned MinKNOW, Dorado, modified-base model, modkit, samtools, and hg38 asset
  versions.
- Fragment measurement definition for release one.
- Minimum usable reads and technical QC thresholds.
- Signing implementation and key custody.
- Provider retention and deletion policy.
- Regulatory and claims review of the complete Protocol & Setup page.
