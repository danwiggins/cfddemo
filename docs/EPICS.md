# Traceback Product Epics

Status: `/autoplan`-reviewed implementation sequence  
Delivery rule: preserve the existing public demo while product code is built
behind separate entry points.

## Dependency map

```text
E0 Protocol qualification
E1 Contracts, trust roots, and fixtures
  → E2 Fenced runner and immutable artifacts
    → E3 modBAM import and preflight
      → E5 analysis-ready modBAM
        → E6 complete-scan fragment record
          → E8 signed local record and privacy boundary
            → E7 operator experience and docs
              → E4 completed POD5 route
                → E9 paid pilot qualification

Post-MVP: E10 Cell origin → E11 Dosage → E12 Longitudinal comparison
```

## MVP cut

Release one is a local provider runner on one qualified workstation. It has no
hosted portal or upload. The modBAM route is the first vertical slice; completed
POD5 support is required before calling the MinKNOW-to-record MVP complete.
E10 through E12 remain demo-only or deferred.

## E0. Qualify one end-to-end protocol

**Outcome:** one exact provider-performed workflow can be documented, checked,
and versioned without generic wet-lab assumptions.

Deliverables:

- approved collection-to-library SOP;
- bill of materials with part numbers, substitutes, quantity, storage, shelf
  life, cost range, and owner;
- supported MinION, flow cell, kit, controls, and sample acceptance criteria;
- preanalytical timestamps and rejection rules;
- protocol evidence register and change-control owner;
- research-use, biosafety, privacy, and claims review.

Acceptance:

- every imperative wet-lab instruction has an owner, source, version, and
  approval state;
- an unsupported or unapproved item cannot render as an instruction;
- the same protocol version appears in sample sheet, runner manifest, record,
  and documentation.

Depends on: none.  
Blocks: E3, E7, pilot launch.

## E1. Define product contracts and synthetic fixtures

**Outcome:** every later component builds against stable schemas rather than
passing paths and loose dictionaries.

Deliverables:

- `run-package.v1`, `workflow-release.v1`, `job.v1`, `preflight.v1`,
  `result-bundle.v1`, and `compatibility-manifest.v1` Pydantic contracts;
- canonical serialization and schema-version rules;
- privacy classifications and cloud allowlist;
- synthetic MinKNOW run-folder, POD5 metadata, ordinary BAM, valid modBAM,
  corrupt input, wrong-reference, and missing-tag fixtures;
- golden aggregate outputs.

Acceptance:

- strict models reject unknown fields and non-finite numbers;
- fixtures contain no real genomic or identifying data;
- canonical serialization is byte-stable;
- migration policy is documented before any v2 schema exists.

Depends on: none.  
Blocks: E2 through E9.

## E2. Build the local runner foundation

**Outcome:** an operator has one reliable CLI and resumable job engine.

Deliverables:

- `traceback` console entry point;
- `doctor`, `demo`, `preflight`, `run`, `status`, `logs`, `retry`, `inspect`,
  `verify`, and `support-bundle`;
- SQLite-backed local job state and transition audit;
- SQLite WAL, stage attempts, receipts, expiring leases, and heartbeat;
- immutable stage checkpoints and idempotency keys;
- structured events, stable exit codes, cancellation, restart, and recovery;
- configured input/output roots and storage accounting.
- runner-owned sealed byte snapshots and no mutable stage inputs;
- fenced leases preventing stale-worker publication;
- sealed receipts, crash reconciliation, and orphan adoption/quarantine;
- rootless zero-egress execution with bounded resources;
- backup, expand/contract migration, and active-job rollback policy.

Acceptance:

- `traceback demo` runs offline in under five minutes;
- process termination during every stage resumes without duplicating verified
  work;
- identical requests return the same job;
- paths outside configured roots are rejected;
- every error contains problem, cause, fix, docs link, job ID, and retryability.

Depends on: E1.  
Blocks: E3 through E9.

## E3. Import and preflight MinKNOW run packages

**Outcome:** selecting a run folder either yields a trustworthy normalized
manifest or one exact remediation.

Deliverables:

- MinKNOW output-structure discovery;
- sample-sheet, final-summary, sequencing-summary, hash, POD5, BAM, and index
  inventory;
- stable-file detection;
- immutable snapshot commit with post-hash restat;
- single-sample and supported-protocol enforcement;
- local SHA-256 identities plus export-safe provider-keyed commitments;
- technical resource estimate and disk reserve;
- modBAM fast-path validator;
- `PASS`, `WARN`, `PARTIAL`, and `BLOCKED` report.

Acceptance:

- active runs cannot be mistaken for completed runs;
- duplicate directories map to one local run identity;
- symlink escapes, sockets, and device files are rejected;
- corrupt/missing batches and contradictory metadata are identified;
- ordinary BAM and FASTQ never qualify for methylation;
- no raw path, read ID, or reusable genomic digest enters serialized output.

Depends on: E0, E1, E2.  
Blocks: E5 through E9.

## E4. Add pinned POD5 basecalling and alignment

**Outcome:** retained POD5 can be converted reproducibly into a registered
aligned modBAM without operator-authored commands.

Deliverables:

- signed workflow release resolver;
- pinned Dorado canonical model execution;
- registered hg38 FASTA/index verification;
- samtools sorting, merging where required, and indexing;
- stdout/stderr redaction and bounded capture;
- one qualified Linux x86-64/NVIDIA processing profile;
- checkpoints, progress, timeout, and disk-pressure handling.

Acceptance:

- argv execution only; no shell interpolation;
- tool/model/reference digests are recorded;
- missing GPU or insufficient disk fails before expensive work;
- restart resumes from the last verified artifact;
- the qualified workstation produces a validated analysis input;
  reproducibility is measured as a qualification result rather than assumed.

Depends on: E1, E2, E3, E5, E6, E8.  
Blocks: E9 and declaration of the complete MinKNOW-to-record MVP.

## E5. Validate the analysis-ready modBAM

**Outcome:** downstream algorithms never receive an ambiguous alignment file.

Deliverables:

- truncation, sort order, index, contig, length, read-group, program-group, and
  hg38 checks;
- basecaller and modified-base model provenance extraction;
- sampled and bounded `MM/ML/MN` structural validation;
- measurement eligibility report;
- fast-path compatibility decision for provider-delivered modBAM.

Acceptance:

- wrong reference blocks every coordinate-dependent measurement;
- missing tags block cell origin only;
- invalid modification tags are never heuristically repaired;
- every eligibility decision names the requirement and supporting artifact.

Depends on: E3 for the modBAM slice; E4 only for POD5-produced input.  
Blocks: E6, E10, E11.

## E6. Publish one deterministic fragment research record

**Outcome:** the first product record proves the complete system using the most
defensible existing measurement.

Deliverables:

- one locked aligned reference-span definition;
- complete streaming computation; capped or interrupted scans cannot publish;
- technical QC and denominator reconciliation;
- deterministic chart JSON and accessible table;
- neutral explanation, limitations, and provenance;
- golden tests against existing module behavior;
- explicit longitudinal compatibility key, even before trends are enabled.

Acceptance:

- computation, chart, method text, and AI review use the same definition;
- every value resolves to one immutable artifact;
- identical inputs and release produce byte-identical measurement JSON;
- no clinical or health-status language passes publication.

Depends on: E5.  
Blocks: E7 through E9.

## E7. Ship the operator experience and Protocol & Setup documentation

**Outcome:** a new trained operator can prepare and process a run without
bioinformatics support.

Deliverables:

- local operator queue and job detail;
- readiness, import, preflight, progress, partial, failure, recovery, complete,
  and superseded states;
- public Protocol & Setup page;
- required/recommended/optional materials table;
- minimum/recommended compute matrix;
- run-configuration guide and sample-sheet generator;
- file-format/retention guide;
- printable checklist and change log;
- accessibility conformance and responsive behavior.

Acceptance:

- five representative operators complete the synthetic workflow without shell
  commands;
- four of five recover from seeded low-disk, missing-POD5, and missing-tag
  failures without support;
- required materials and compute specs are found within two minutes;
- page and printable checklist derive from one compatibility manifest;
- no unapproved wet-lab instruction can publish.

Depends on: E0 through E6.  
Blocks: E9.

## E8. Enforce signed local records and the raw-data boundary

**Outcome:** only verified aggregate records can be deliberately exported from
the local application; no hosted ingestion exists in release one.

Deliverables:

- canonical result bundle builder;
- checksums and asymmetric signature verification;
- separate Ed25519 release/result keys with protected custody, trust
  distribution, rotation, revocation, and lost-key response;
- export allowlist serializer;
- claims firewall across UI, report, and export;
- redacted logs and support bundles;
- explicit local save/export.

Acceptance:

- adversarial fixtures prove sequence, read ID, path, sample label, environment
  secret, and unsalted genomic hash cannot cross the boundary;
- unknown fields and invalid signatures fail closed;
- the deterministic record remains complete when AI is unavailable;
- no upload or deletion occurs implicitly.

Depends on: E1, E2, E6.  
Blocks: E9.

## E9. Paid pilot operations, validation, and release controls

**Outcome:** one provider can run the product repeatedly with known support,
cost, failure, and rollback behavior.

Deliverables:

- workflow signing and revocation;
- installer, upgrade check, compatibility notice, and rollback;
- operator metrics and redacted diagnostics;
- shadow-processing protocol;
- real-run concordance and repeatability report;
- support, custody, retention, deletion, recollection, and incident runbooks;
- release checklist and go/no-go dashboard.
- two priced provider commitments, one paid;
- operator labor baseline and contribution-margin measurement.

Acceptance:

- 95% of valid pilot inputs complete without engineering intervention;
- preflight catches 95% of terminal input failures before heavy compute;
- every handoff and failure has an owner;
- previous signed workflow can be restored without changing historical records;
- counsel and scientific owners approve the exact release journey.

Depends on: E0 through E8.  
Blocks: paid pilot expansion.

## E10. Add cell-origin methylation

**Outcome:** eligible modBAMs produce a separately validated cell-contributor
research measurement.

Deliverables:

- productionized modkit extraction;
- registered Loyfer marker and atlas assets;
- existing UXM, NNLS, and bootstrap integration;
- coverage/overlap eligibility and uncertainty;
- neutral chart, limits, provenance, and validation report.

Acceptance:

- analytical validation defines repeatability and failure boundaries;
- low marker coverage creates unavailable/partial state;
- no healthy/abnormal classification appears in personalized output;
- existing bounded privacy contracts remain intact.

Depends on: E5, E8, separate analytical approval.  
MVP: deferred.

## E11. Add broad chromosome dosage

**Outcome:** eligible BAMs produce the existing broad dosage visualization under
an explicitly experimental release.

Deliverables:

- runner integration and QC;
- panel/normalization strategy decision;
- technical validation;
- neutral visualization and limitations;
- decision on whether to retain, replace with ichorCNA, or remove.

Acceptance:

- it is never called ichorCNA unless it actually uses that method;
- visualization thresholds are not presented as clinical cutoffs;
- focal/subclonal and tumor-fraction limits remain visible.

Depends on: E5, E8, separate analytical approval.  
MVP: deferred.

## E12. Add longitudinal comparison

**Outcome:** compatible repeated draws can be compared without hiding method or
preanalytical changes.

Deliverables:

- compatibility-key engine;
- repeatability ranges;
- separate series for incompatible versions;
- supersession and reanalysis behavior;
- change visualization with technical uncertainty.

Acceptance:

- every compatibility-key mismatch is property-tested;
- incompatible records are never connected by a trend line;
- no increase/decrease receives clinical meaning;
- historical records remain immutable.

Depends on: E6 or later measurements plus repeatability evidence.  
MVP: deferred.

## Cross-epic test suites

| Suite | Scope |
|---|---|
| Contract | Strict schemas, canonical JSON, migrations, unknown fields |
| Run-package | MinKNOW layouts, changing files, corrupt batches, duplicates |
| Tool orchestration | argv safety, timeouts, progress, resume, disk exhaustion |
| Scientific | golden outputs, denominators, filters, asset/model compatibility |
| Privacy | seeded identifiers and sequence cannot reach allowed sinks |
| Claims | prohibited language and numeric/evidence binding |
| UX | all states, keyboard, screen reader, mobile/tablet/desktop |
| Operations | install, upgrade, revoke, rollback, support bundle, deletion |
| End-to-end | synthetic POD5 and modBAM fast path through signed record |

## Implementation rule

No epic may bypass a missing upstream contract by hard-coding a demo path.
Incomplete measurements must become explicit partial/unavailable states. They
must never disappear or inherit a value from a fixture.
