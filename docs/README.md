# Documentation index

Traceback is research-use software. Nothing here is a validated clinical
assay.

## Start here: Traceback Lab

The Lab runs every registered cfDNA algorithm on a nanopore kit. It is the
main surface of this repository.

- [Lab guide](LAB-GUIDE.md): commands, [running on real data](LAB-GUIDE.md#running-on-real-data),
  [algorithm status](LAB-GUIDE.md#algorithm-status-on-real-data), error codes
- [Quick guide for scientists](SCIENTIST-QUICK-GUIDE.md)
- [Algorithm explainer](LAB-ALGORITHM-EXPLAINER.md): what each measurement may and may not tell you
- [Algorithms reference](ALGORITHMS.md): gates and requirements per algorithm
- [Assets](ASSETS.md): what to register for each algorithm, and how

## Lab method notes

- [TSS coverage profile](LAB-TSS-PROFILE.md)
- [Griffin TF-site profile integration](griffin-integration.md) and its
  [synthetic workflow proof](proofs/griffin/README.md)
- [Exploratory epi-clock adapter](epi-clock.md) and its
  [coordinate provenance](epi-clock-coordinate-provenance.md)
- [MethylBERT model bundle contract](METHYLBERT-BUNDLE.md)
- [Prenatal NIPTeR adapter](fetal-nipter.md)
- [Male-fetus chrY fetal-fraction workflow](fetal-chry.md)
- [Exploratory male-donor chrY cfDNA adapter](dd-cfdna-chry.md)
- [Lab interpretation evidence matrix](LAB-INTERPRETATION-EVIDENCE.md)
- [ADR 0002: reference-state labels are a Lab-only surface](adr/0002-e10-reference-labels-lab-only.md)

## Signed local records

The older `traceback run` path: one aligned BAM to a development-signed
record. The code is still in the repository and tested.

- [Operator guide](OPERATOR-GUIDE.md): every signed command, error code and recovery step
- [Golden-path canaries](CANARIES.md)
- [Runner contracts](RUNNER-CONTRACTS.md) and [runner recovery](RUNNER-RECOVERY.md)
- [Synthetic BAM preflight and aligned reference-span contract](MEASUREMENT-CONTRACT.md)
- [Development record signing and export boundary](SIGNING.md)
- [Local operator web boundary](LOCAL-WEB-BOUNDARY.md)
- [Local result catalog](RESULT-CATALOG.md) and [integrated result explorer](RESULT-EXPLORER.md)
- [Versioned compatibility decisions](COMPATIBILITY.md)
- [Protocol & Setup](PROTOCOL-SETUP.md)

## Public evidence demo

- [Three-minute demo](DEMO.md)
- [75-second recording script](RECORDING-SCRIPT.md)
- [Railway deployment](DEPLOYMENT.md)

## Development

- [L0 development control plane](DEVELOPMENT-CONTROL.md) (the CI section is
  current; the rest is historical)
- `docs/changes/<ID>.md`: one dated bullet per pull request, to be folded
  into the Lab guide's Changes list

## Historical: plans and specs

These describe plans from the signing, registry and governance phase
(September–October 2026). Each carries a "Historical" banner. They are not
evidence that a capability shipped; check the guides above.

- [Traceback Consumer Research Product Plan](PRODUCT-PLAN.md)
- [Traceback MinION-to-Research-Record Specification](PRODUCT-SPEC.md)
- [Traceback Product Epics](EPICS.md)
- [Traceback Release-One Test Plan](TEST-PLAN.md)
- [Golden-path MVP slice and engineering hygiene](GOLDEN-PATH-MVP-SLICE.md)
- [Operator usability, changeable analysis and an interpretable site](OPERATOR-USABILITY-AND-ANALYSIS.md)
- [Signal methods: cell origin and copy number as signed local records](SIGNAL-METHODS-CELL-ORIGIN-AND-CNA.md)
- [Pilot security hardening](PILOT-SECURITY-HARDENING.md)
- [E12 cohort/timepoint integration plan](E12-INTEGRATION-PLAN.md)
- [E12 dependency audit](E12-DEPENDENCY-AUDIT.md)
- [ADR 0001: provisional execution-substrate direction](adr/0001-execution-substrate-comparison.md)
- [Method comparison explorer: states and interactions](designs/method-comparison-explorer-notes.md)

## Historical: signed-record and governance contracts

Contracts for `evidence_inspector` and `traceback_runner` modules built in the
same phase. The code they describe is still in the repository and tested,
but none of it is part of the Lab workflow. Each page states its own
synthetic/local status.

- [Protected anchor-policy registry](ANCHOR-POLICY-REGISTRY.md)
- [Cell-origin estimate explorer contract](CELL-ORIGIN-EXPLORER.md)
- [Synthetic CNA explorer contract](CNA-EXPLORER.md)
- [Cohort denominators and missingness](COHORT-DENOMINATORS.md)
- [Versioned cohort manifests](COHORT-MANIFESTS.md)
- [Verified cohort record import (D06)](COHORT-RECORD-IMPORT.md)
- [Protected cohort registry](COHORT-REGISTRY.md)
- [Composite authority fence](COMPOSITE-AUTHORITY-FENCE.md)
- [Batch and preanalytical context (D10)](COVARIATE-CONTEXT.md)
- [Protected D09 policy/summary registry](D09-POLICY-REGISTRY.md)
- [Protected D10 context registry](D10-CONTEXT-REGISTRY.md)
- [Deterministic fragment explorer](FRAGMENT-EXPLORER.md)
- [Longitudinal browser integration](LONGITUDINAL-BROWSER.md)
- [Durable saved-comparison registry](LONGITUDINAL-COMPARISON-REGISTRY.md)
- [Pinned-anchor longitudinal compatibility](LONGITUDINAL-COMPATIBILITY.md)
- [D03 longitudinal compatibility decision engine](LONGITUDINAL-DECISION-ENGINE.md)
- [Protected D03 decision registry](LONGITUDINAL-DECISION-REGISTRY.md)
- [Longitudinal workspace (D08 read model)](LONGITUDINAL-WORKSPACE.md)
- [Protected E12 family-source artifact registry](MEASUREMENT-SOURCE-ARTIFACT-REGISTRY.md)
- [Versioned method registry](METHOD-REGISTRY.md)
- [Portable local view contract (E13)](PORTABLE-LOCAL-VIEW.md)
- [Product gate evidence foundation](PRODUCT-GATES.md)
- [Protected E12 projection-policy registry](PROJECTION-POLICY-REGISTRY.md)
- [Provenance and method-difference drawer](PROVENANCE-DRAWER.md)
- [Protected provider linkage store](PROVIDER-LINKAGE-STORE.md)
- [Protected provider-local linkage](PROVIDER-LINKAGE.md)
- [Protected reader-authorization registry and B01 session binding](READER-AUTHORIZATION-REGISTRY.md)
- [Durable record supersession](RECORD-SUPERSESSION.md)
- [Shared storage behaviour](REGISTRY-STORAGE.md)
- [Synthetic release and qualification evidence](RELEASE-EVIDENCE.md)
- [Protected D07 comparison registry](REPEATABILITY-COMPARISON-REGISTRY.md)
- [Repeatability-aware comparisons](REPEATABILITY-COMPARISONS.md)
- [Protected result trust registry](RESULT-TRUST-REGISTRY.md)
- [Result status, QC, and evidence filters](RESULT-VIEW-FILTERS.md)
- [Protected E06 result-view-source registry](RESULT-VIEW-SOURCE-REGISTRY.md)
- [Registered sensitivity and downsampling comparison](SENSITIVITY-DOWNSAMPLING.md)
- [E12 source-value projection adapters](SOURCE-VALUE-PROJECTION.md)
