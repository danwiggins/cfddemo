<!-- /autoplan restore point: /Users/danwiggins/.gstack/projects/danwiggins-cfddemo/main-autoplan-restore-20260926-162006.md -->
# Traceback Consumer Research Product Plan

> Historical strategy exploration. Where this document conflicts with
> `PRODUCT-SPEC.md`, the `/autoplan`-reviewed provider-side MVP in
> `PRODUCT-SPEC.md` and `EPICS.md` controls. Consumer membership, hosted
> records, payments, kits, logistics, and longitudinal comparison are deferred
> hypotheses, not release-one scope.

Status: reviewed product direction; not approved for implementation or launch.

## Decision

For the first commercial hypothesis, Traceback is consumer-first research
software whose core product begins when a sequencing provider delivers a BAM or
modBAM. A customer pays for automated, reproducible computation and a private
longitudinal research record. Collection materials and provider coordination
are optional integrations, not the product wedge.

Traceback does not provide a cancer test, health status, diagnosis, screening,
treatment recommendation, or reassurance. Independent providers perform blood
collection, laboratory processing, and sequencing. That separation is
operational, not a regulatory exemption: intended use is determined by the
product's functions, claims, labeling, reports, marketing, and support behavior.

This hypothesis remains provisional. It must pass demand, regulatory, and
analytical gates before broad consumer launch.

## Product thesis

The product is:

> Guided MinION-run-to-research-record software that validates acquisition
> settings, preserves the raw POD5 source, runs pinned cfDNA workflows, and
> publishes signed aggregate measurements with complete provenance while raw
> genomic data remains under provider control.

The product is not:

> A service that tells a customer whether they have cancer, are healthy, are
> normal, or should seek or avoid medical care.

The durable value is not one novel report. It is a trustworthy record across
multiple draws:

- one place to coordinate compatible sample workflows;
- repeatable measurements with stable definitions;
- explicit technical quality and comparison eligibility;
- clear visual change over time when methods are compatible;
- portable data and provenance;
- report-integrity checks that prevent unsupported language.

## Target customer and demand hypothesis

Initial customer: a technically sophisticated, high-income self-researcher who
already pays for quantified-self, longevity, or personal genomics products and
is willing to pay for a non-clinical result.

The demand hypothesis is weak until proven. Interest in cancer biology is not
evidence that people will repeatedly pay for non-actionable measurements.

Before building the full platform:

1. interview at least 20 target customers using neutral, non-disease product
   language;
2. show the actual research-only output and its limitations;
3. ask for a refundable deposit or paid concierge pilot;
4. test annual rather than monthly renewal;
5. record the question customers believe the product answers;
6. stop if the purchase depends on reassurance, detection, or medical action.

Commercial gate: at least five paid pilots, three completed second draws, and
two explicit annual-renewal commitments at a price that supports provider and
support costs.

## Business model

### Initial offer

Sell an annual Traceback membership, not a test result.

The membership may include:

- access to the private research workspace;
- processing of a defined number of compatible sequencing runs;
- longitudinal comparison using locked workflow versions;
- export of a self-contained research record and provenance bundle;
- provider-facing automation and status tracking.

Optional kits or provider referrals remain separate offers. Collection,
laboratory, sequencing, shipping, and taxes must be itemized separately,
including which party charges the customer and handles failures.

### Pricing experiments

Test three offers without implying clinical value:

| Offer | Hypothesis | Risk |
|---|---|---|
| Annual software membership plus pass-through provider costs | Recurring record and comparison drive renewal | Customer may see little value after the first report |
| Paid first research record, annual renewal for comparison | Lowers initial commitment | Looks more like a one-off test |
| Founding research cohort with two scheduled draws | Produces repeat-use evidence quickly | Must not be represented as IRB research unless it actually is |

Do not use a monthly subscription. The natural cadence is per draw or annual.
Do not hide the all-in expected cost behind a low software price.

### Revenue boundaries

- Traceback may sell or resell compatible collection materials after product,
  labeling, shipping, and state-law review.
- Traceback may receive disclosed coordination or referral revenue only after
  counsel reviews fee-splitting, advertising, and provider-independence issues.
- Traceback must not bill for clinical interpretation.
- Traceback must not compensate providers based on a biological result.
- Provider contracts must define custody, failure ownership, data delivery,
  deletion, breach handling, and customer support.

## Regulatory-clean operating model

“Regulatory clean” means the intended use is defensible in actual product
behavior. It does not mean regulation-free and cannot be created with a footer
disclaimer.

### Claims firewall

Release-blocking words in personalized outputs, marketing, support scripts,
notifications, and exports:

- cancer, tumor signal, detection, screening, risk, positive, negative;
- healthy, normal, abnormal, clean, clear, reassuring;
- early warning, disease-free, concerning, seek care, no action needed.

Allowed personalized language describes measurements and technical validity:

- “Fragment-length distribution peaked at 212 bp.”
- “0.7% of accepted reads exceeded 1 kb.”
- “Estimated cell-contributor profile.”
- “Zero chromosomes crossed the predefined research visualization threshold.”
- “This run passed the technical requirements for this measurement.”
- “These two runs cannot be compared because workflow versions differ.”

“Clinical meaning is not established” is a structured field displayed beside
every measurement, not a footer.

### Functional boundaries

The consumer product may:

- show raw and derived research measurements;
- explain algorithms and data quality;
- compare technically compatible runs;
- show method-matched research cohort distributions without labeling the
  individual as inside or outside a health range;
- identify computational inconsistencies and unsupported report language;
- export the customer's data and provenance.

The consumer product must not:

- calculate or imply disease probability;
- label a result as clinically normal or abnormal;
- recommend medical action or inaction;
- prioritize support based on a biological measurement;
- generate an overall red, amber, or green health status;
- let AI freely interpret biology or introduce medical language;
- let a provider-facing clinical conclusion leak into the consumer interface.

### Pre-launch counsel package

Obtain a written, function-by-function review covering:

- intended use and product classification;
- consumer marketing, onboarding, support scripts, and exports;
- kit components, labeling, fulfillment, and product liability;
- provider relationships, referrals, and payment flows;
- CLIA implications of patient-specific results;
- human-subject research and IRB boundaries;
- HIPAA status and the FTC Health Breach Notification Rule;
- state genetic privacy, consumer health data, laboratory, and telehealth laws;
- consent, secondary use, deletion, retention, and breach notification;
- terms, age eligibility, geographic restrictions, and adverse-event handling.

Launch gate: counsel approves the complete customer journey and exact claims,
not only the terms of service.

## Product experience

### First-screen contract

Within five seconds, an unfamiliar customer should understand:

1. Traceback creates a personal cfDNA research record.
2. It cannot establish or exclude disease.
3. The next step, owner, total expected cost, and timeline are clear.
4. The customer controls data retention and export.

### Information architecture

```text
Public explanation
  ├── What Traceback measures
  ├── What it cannot tell you
  ├── Process, providers, price, timeline, and failure ownership
  ├── Privacy and data flow
  └── Begin enrollment

Authenticated workspace
  ├── Home
  │   ├── One next action
  │   ├── Active sample timeline
  │   └── Latest completed research record
  ├── Samples
  │   └── Order → collect → ship → sequence → analyze → complete
  ├── Research record
  │   ├── Technical quality
  │   ├── Fragment measurements
  │   ├── Cell-contributor estimates
  │   ├── Chromosome-dosage measurements
  │   └── Change over time
  ├── Report integrity
  │   └── Deterministic checks + bounded AI evidence review
  └── Data, methods, privacy, consent, and export
```

The customer never handles BAM filenames, command lines, reference indexes, or
workflow configuration in the primary journey.

### Protocol and setup documentation

A public, versioned **Protocol & Setup** page is a required product deliverable,
not optional support content. It must let a new operator determine what they
need, what they must do, and whether their system can complete the workflow
before they purchase materials or collect a sample.

The page covers:

1. **End-to-end protocol:** provider-performed blood draw, sample labeling,
   transport, plasma separation, cfDNA extraction, native-DNA library
   preparation, MinION sequencing, local processing, and report generation.
2. **Blood collection requirements:** qualified operator, approved collection
   tube and volume, mixing and handling, temperature, processing-time window,
   rejection criteria, required controls, chain of custody, and the owner of
   each step. Exact instructions must come from a reviewed, versioned SOP; the
   consumer page must not improvise phlebotomy guidance.
3. **Sequencing equipment:** supported MinION model, flow cell, sequencing and
   expansion kits, consumables, pipettes, cold-chain and centrifugation
   equipment, concentration/QC instruments, and required software.
4. **Computer requirements:** separate **minimum** and **recommended**
   configurations for acquisition, basecalling with modified-base models,
   alignment, and downstream analysis. Include supported OS, CPU, GPU, memory,
   SSD capacity, USB connection, network needs, expected runtime, and expected
   storage by run size.
5. **Run configuration:** pseudonymous sample ID, kit and flow-cell metadata,
   POD5 retention, basecalling model, modified-base model, reference build,
   output formats, controls, and stop/completion criteria.
6. **What files are produced:** POD5, BAM/modBAM, FASTQ, sequencing summary,
   sample sheet, final report, hashes, indexes, and which files must be retained.
7. **Troubleshooting and recovery:** failed flow-cell check, inadequate input,
   low active-pore count, insufficient disk or GPU, missing methylation tags,
   interrupted basecalling, incomplete transfer, and when recollection is
   required.
8. **Limits and safety:** research-use-only scope, trained-personnel boundaries,
   biohazard and sharps handling, privacy guidance, and what the measurements
   cannot establish.

Every material and setting is labeled **Required**, **Recommended**, or
**Optional**, with a reason, compatible versions, estimated cost, supplier or
generic specification, and last-reviewed date. A printable checklist and
machine-readable compatibility manifest use the same versioned source data as
the web page.

For the current MinION Mk1D baseline, publish Oxford Nanopore's current host
guidance as the acquisition floor—16 GB memory and 1 TB SSD, or 24 GB unified
memory on a supported Apple system—and 32 GB memory and 2 TB SSD as the
recommended class. GPU/Apple Silicon details, supported operating systems, and
storage estimates must be pinned to the supported MinKNOW and Dorado release
rather than copied into evergreen marketing text.

### Measurement page contract

Every algorithm uses the same progressive structure:

1. **Measured:** one neutral sentence, one chart, and exact value.
2. **Technical quality:** whether the input was adequate for this computation.
3. **Context:** prior compatible draws or a clearly described research cohort.
4. **Uncertainty:** method-specific uncertainty without clinical confidence.
5. **Limits:** what the measurement cannot establish.
6. **Method:** an illustrated three-to-five-step explanation.
7. **Audit details:** denominator, filters, reference build, workflow version,
   parameters, input hashes, references, and verification status.

The default layer shows one sentence, one chart, one quality state, and one
limitation. Technical detail is available without competing with the main
reading path.

### Longitudinal experience

- A first draw establishes a research record but never implies a trend.
- A later draw is comparable only when preanalytical protocol, required metadata,
  reference build, workflow definition, and quality gates are compatible.
- Incompatible measurements remain visible but are not connected by a trend
  line or summarized as change.
- Technical and biological variation are shown separately when the validation
  data support that distinction.
- No increase or decrease receives a health interpretation.

### Report integrity

AI is a constrained reviewer, not a biological interpreter. It may:

- select from registered evidence checks;
- find definition drift, changed denominators, unsupported statements, and
  inconsistent reproduction instructions;
- cite exact measurement artifacts and source passages;
- draft neutral explanations from approved templates.

Deterministic code verifies all numeric claims. AI cannot change a measurement,
invent a clinical meaning, or publish unapproved vocabulary. If AI is
unavailable, the deterministic report remains complete.

## Required interaction states

Every major surface specifies empty, in-progress, failed, partial, and complete
states. A missing result never silently disappears.

| Area | Required behavior |
|---|---|
| Enrollment | Explain value, limits, full cost, consent status, and saved progress |
| Collection | Show appointment or shipping status, responsible party, and recovery |
| Sequencing | Show provider-owned stage without false precision |
| Data transfer | Verify package identity, completeness, and deletion policy |
| Analysis | Show step-level status, resumability, owner, and plain-language failure |
| Measurement | Show unavailable/partial status with the exact missing requirement |
| Comparison | Block interpretation when versions or quality are incompatible |
| AI review | Preserve deterministic output when model review fails |
| Export | Verify redaction, completeness, signature, and reproducibility manifest |
| Withdrawal | Stop future processing and explain retained versus deleted records |

## Accessibility and visual system

- Treat the product as an application, not a landing page followed by a
  scientific poster.
- Use persistent navigation and one primary action per screen.
- Use a restrained editorial-scientific visual system with one visual anchor,
  calm surfaces, and minimal card chrome.
- Define typography, spacing, color, charts, state labels, icons, and responsive
  behavior in `DESIGN.md` before implementation.
- Meet WCAG 2.2 AA.
- Provide visible keyboard focus and complete keyboard operation.
- Use at least 16 px body text and 44 px touch targets.
- Never communicate state by color alone.
- Give every chart a text summary and accessible data table.
- Announce asynchronous status and errors to assistive technology.
- Support 320 px, 768 px, and desktop layouts intentionally.
- Respect reduced-motion preferences.
- Expand scientific abbreviations on first use.

## Scientific product requirements

- Measurement definitions are explicit, immutable, and versioned.
- Every result includes denominator, filters, reference build, software
  versions, parameters, input hashes, and verification level.
- Controls, reference materials, and panels of normals are versioned assets.
- Uncertainty reflects the analytical method and is not presented as clinical
  confidence.
- Detection limits come from dilution series or reference materials, not one
  apparently healthy sample.
- Longitudinal claims require estimates of within-person, provider, lot,
  operator, transport, and computational variance.
- Provider eligibility requires a locked preanalytical SOP and qualification
  evidence. Arbitrary bring-your-own collection is not supported initially.
- Claims never exceed the validation status of the workflow.

## Architecture principles

- Computation follows the raw data. The initial Traceback Runner executes inside
  the qualified sequencing provider's environment. A consumer-local runner is a
  later option, not an onboarding requirement.
- Raw sequence data stays in provider-controlled storage by default.
- The hosted control plane receives only consented identity and order data,
  workflow state, signed aggregate result artifacts, and required provenance.
- Algorithms remain deterministic and independently testable.
- Workflow manifests are immutable and content-addressed.
- Longitudinal comparisons require explicit compatibility decisions.
- Consumer copy is generated from versioned, approved claim templates.
- AI receives bounded aggregate evidence, never raw sequence data or unrestricted
  personal records.
- Use managed commodity services for identity, billing, email, object storage,
  queues, and kit fulfillment. Build the workflow contracts, local runner,
  provenance, comparison logic, and claims firewall.

## Core automation contract

### Initial accepted input

Support one opinionated provider contract first:

- a complete MinKNOW run directory with retained POD5 and run metadata; or
- a coordinate-sorted BAM or modBAM plus BAI or CSI index as a validated
  fast-path input;
- hg38 only;
- one biological sample per job;
- stable pseudonymous sample ID delivered separately;
- `MM`, `ML`, and `MN` tags required for methylation workflows;
- declared sequencing chemistry, basecaller/model, library protocol, collection
  protocol, and processing timestamps.

POD5 is the reproducible source from which a pinned Dorado release can recreate
basecalls and modified-base calls. An ordinary BAM can support fragment and
coverage-based measurements. Cell origin requires a valid modBAM or POD5 that
can be re-basecalled with a compatible modified-base model. FASTQ alone is not
sufficient for cell-origin methylation. Defer CRAM, multiple reference builds,
direct device control, streaming biological analysis, and arbitrary workflows.

### Zero-touch flow

```text
MinKNOW run folder or validated modBAM delivered
  → acquisition and package preflight
  → pinned basecalling/alignment when POD5 is supplied
  → pinned computation
  → output validation
  → signed aggregate bundle
  → claims-controlled research record
  → longitudinal compatibility gate
```

Support a direct command for evaluation, a watched directory for small
providers, and an S3-compatible inbox for automated providers.

```bash
traceback doctor
traceback demo
traceback preflight /data/minknow/run-folder
traceback run /data/minknow/run-folder
traceback serve --config runner.yaml
```

`traceback demo` must execute the real workflow contract on a small synthetic
fixture without an account, cloud credentials, configuration, or reference
downloads. It should produce a signed local report in under five minutes.

### Preflight

Before expensive computation, inspect rather than trust:

- readability, index, coordinate sorting, contigs, and reference build;
- file truncation and record corruption;
- required modification tags;
- minimum usable reads and measurement eligibility;
- workflow and reference compatibility;
- disk, memory, tools, and output permissions;
- duplicate or previously completed delivery.

Each check is `PASS`, `WARN`, or `BLOCKED`. A blocked check provides a stable
error code, problem, cause, correction, documentation link, run ID, and
retryability.

### Runner commands

```text
traceback doctor
traceback demo
traceback preflight <input> [--json]
traceback run <input> [--json]
traceback status <run-id>
traceback logs <run-id>
traceback retry <run-id>
traceback inspect <bundle>
traceback verify <bundle>
traceback support-bundle <run-id>
traceback serve
traceback update check
```

Human-readable output is the default. Stable JSON and documented exit codes
make every command automatable.

### Signed output

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

No sequence, read ID, raw local path, or unsalted genomic hash may enter the
hosted bundle.

### Operator experience

Provider operators need an operational queue, not biological interpretation:

- jobs awaiting files;
- preflight failures and exact remediation;
- active stage and elapsed time;
- retryable failures;
- completed bundle and upload status;
- runner/workflow versions and storage pressure;
- redacted support-bundle generation.

Target metrics:

- demo under five minutes and production install under 30 minutes;
- 95% of valid inputs complete without engineering involvement;
- preflight catches 95% of terminal input problems;
- interrupted jobs resume without repeating completed stages;
- duplicate delivery never creates duplicate records;
- identical input and release produce byte-identical measurement JSON;
- raw genomic data never crosses the upload boundary;
- support resolves 90% of failures from the error code and support bundle.

### System shape

```text
Consumer browser
      |
      | identity, consent, order, status, aggregate research records
      v
+---------------- TRACEBACK CLOUD ----------------+
| consumer and provider portals                   |
| lifecycle state machines                        |
| claims-controlled report renderer               |
| longitudinal compatibility engine               |
| PostgreSQL + aggregate object storage            |
| identity, billing, fulfillment adapters          |
+----------------------^---------------------------+
                       |
               signed result bundle
               no BAM, FASTQ, read IDs, or paths
                       |
+----------------------|---------------------------+
| TRACEBACK RUNNER at qualified provider           |
| validate signed workflow release and inputs      |
| execute pinned containers                        |
| produce QC, measurements, and provenance         |
| sign and upload aggregate bundle                 |
+----------------------^---------------------------+
                       |
         provider-controlled encrypted storage
```

Start as a modular monolith with PostgreSQL, managed object storage, a small
container runner, and server-rendered application pages. Do not begin with
microservices, Kubernetes, a graph database, event sourcing, a separate SPA,
or a generic workflow marketplace.

The current Streamlit interface remains a demo. Reuse the tested Pydantic
contracts and pure scientific modules; do not turn session state and
filesystem-selected result files into product infrastructure.

### Workflow release contract

Each signed release contains:

- container image digests;
- reference, atlas, and panel digests;
- exact input contract;
- ordered steps and restart rules;
- QC rules and failure codes;
- result schema;
- claims-policy version;
- longitudinal compatibility-key definition.

Use an explicit sequential container runner first. Adopt a larger workflow
system only when actual provider workflows require branching, multiple compute
backends, or scheduling that the small runner cannot handle.

### State machines

Logistics, specimens, analyses, and records have separate states:

```text
ORDER
draft → awaiting_payment → paid → fulfillment_requested → shipped → delivered
      ↘ cancelled / refunded

SPECIMEN
not_collected → scheduled → collected → provider_received → sequencing → data_ready
              ↘ failed / recollection_required

ANALYSIS
not_ready → validating → queued → running → partial / failed / complete → superseded

RESEARCH RECORD
draft → deterministic_validation → claims_validation
      → optional_AI_integrity_review → approved → published → withdrawn
```

Every transition records actor, timestamp, reason, source event, and idempotency
key. Current state remains relational for simple queries.

### Core data model

```text
User
 └── Enrollment ── ConsentVersion
      ├── Order ── Payment ── Fulfillment
      └── Specimen ── CustodyEvent ── ProviderHandoff
           └── DataDelivery
                └── AnalysisRun ── WorkflowRelease
                     ├── ArtifactDescriptor
                     ├── QCResult
                     ├── Measurement
                     └── SignedResultBundle
                          ├── EvidenceBinding
                          ├── IntegrityReview
                          └── ResearchRecord
                               └── LongitudinalComparison
```

Cloud artifact descriptors record classification, size, digest scope,
retention, and location owner. They never contain raw local paths or unsalted
genomic hashes.

The compatibility key includes collection protocol, kit, processing delay,
sequencing chemistry, basecaller and modification model, reference build,
workflow release, measurement definition, atlas/reference version, filters,
denominator, and normalization method. Exact compatibility permits one series.
A mismatch creates separate series unless a validated bridge exists.

### Claims publication pipeline

```text
measurement
  → approved deterministic template
  → prohibited-language scan
  → numeric and evidence validation
  → optional bounded AI contradiction review
  → deterministic revalidation
  → publish or fail closed
```

Persist model ID, prompt version, input-bundle digest, output, citations,
validation outcome, and policy decision. No AI output bypasses deterministic
publication controls.

### Build versus buy

| Capability | Decision |
|---|---|
| Scientific algorithms, result contracts, and provenance | Build |
| Longitudinal compatibility and claims enforcement | Build |
| Provider runner and signed bundle | Build narrowly |
| Authentication | Proven managed service or established framework package |
| Payments, tax, and refunds | Stripe |
| Shipping and tracking | Established shipping API |
| Physical fulfillment | Qualified third-party logistics provider |
| Collection and sequencing | Contracted independent providers |
| Email and SMS | Managed provider |
| Object storage and database | Managed infrastructure |
| Audit history | Append-only relational records first |
| AI model | Bedrock behind the bounded existing adapter |

### Test strategy

- Unit-test every schema, state transition, compatibility rule, claims rule,
  canonical serialization, hash, and signature.
- Integration-test byte-stable aggregate bundles, interruption and resume, disk
  exhaustion, corrupt indexes, wrong reference builds, and missing methylation
  tags.
- Prove by test that raw sequence, read identifiers, local paths, and private
  source text cannot cross the cloud or model boundary.
- End-to-end test enrollment, payment, fulfillment, provider handoff, duplicate
  and out-of-order events, recollection, refund, withdrawal, deletion, and
  publication.
- Property-test historical immutability and comparison rejection for every
  compatibility-key mismatch.
- Evaluate AI against prohibited claims, invented numbers, bad citations,
  prompt injection, timeout, and malformed output.
- Use scientific golden datasets covering mixtures, dilution series,
  replicates, low coverage, contamination, partial inputs, and method changes.

## Delivery sequence and gates

### Phase 0: prove the business without building the platform

- Run interviews and paid concierge pilots.
- Use a reviewed static report with neutral measurement language.
- Map the complete provider, payment, custody, support, and failure journey.
- Obtain the first regulatory classification memo.
- Run usability tests for mistaken medical interpretation.

Gate: paid demand exists without a health promise; no participant interprets
the output as excluding cancer; the end-to-end service can be delivered
reliably by named providers.

### Phase 1: one complete research record

- Build enrollment, consent, order coordination, sample tracking, ingest,
  technical QC, one validated measurement, neutral explanation, provenance,
  privacy controls, and export.
- Publish the versioned Protocol & Setup page, printable operator checklist,
  materials list, and minimum/recommended compute matrix.
- Start with the most analytically defensible readout, not all three.
- Package the current deterministic contracts rather than rewriting them.
- Keep AI report integrity optional and subordinate.

Gate: at least 95% of valid pilot inputs complete without engineering
intervention; every number is reproducible; every failure has an owner and
recovery path; counsel approves the release candidate journey.

### Phase 2: longitudinal comparison

- Add second-draw ordering and method-compatibility rules.
- Estimate repeatability from technical replicates and repeated collections.
- Show comparable measurements without biological or clinical interpretation.
- Add explicit stale and superseded workflow handling.

Gate: predefined repeatability limits hold across qualified providers and lots;
customers understand the difference between technical change and unknown
biological meaning.

### Phase 3: additional algorithms

- Add cell-contributor and chromosome-dosage workflows only after separate
  analytical validation and claims review.
- Add versioned research context datasets.
- Expand report-integrity checks and portable audit packages.

Gate: each workflow has published input requirements, benchmark performance,
failure boundaries, and approved language.

### Phase 4: scale or change lanes

- Scale the consumer research model only if renewal and support economics work.
- If customer value consistently depends on health interpretation, stop
  stretching the research boundary and pursue a regulated clinical partner.
- Keep the same deterministic workflow and provenance foundation across lanes,
  but separate products, claims, access, and validation.

## Success metrics

Business:

- five paid pilots;
- at least three second draws;
- at least two annual-renewal commitments;
- positive contribution margin after provider failures and support;
- documented top purchase reason that does not depend on medical reassurance.

Product:

- 11 of 12 target consumers correctly explain that Traceback cannot establish
  or exclude disease after viewing the first screen;
- 10 of 12 find sample status and their next action within 30 seconds;
- 10 of 12 distinguish technical quality from health status;
- no participant interprets a result as “I do not have cancer”;
- total expected price and responsible providers are visible before purchase.

Analytical:

- a second machine reproduces aggregate outputs from the same registered inputs;
- technical replicates meet predefined concordance;
- invalid inputs fail before analysis with a specific fix;
- every displayed number resolves to one immutable result artifact;
- incompatible workflow versions never produce a trend interpretation.

Operational:

- every handoff has a named owner and service-level expectation;
- support can resolve common failures without engineering;
- raw sequence data leaves the controlled execution environment only through
  explicit, logged consent;
- deletion and export requests complete within published timelines.

## Explicitly out of scope

- diagnosis, screening, risk scoring, treatment, or reassurance;
- an overall health status;
- autonomous AI interpretation;
- arbitrary providers before protocol qualification;
- broad geographic launch before state-by-state review;
- pediatric use;
- insurance billing;
- upload of raw genomic data to the hosted control plane by default;
- a marketplace of unqualified providers;
- multi-omics expansion before cfDNA workflows are validated;
- claims that the consumer model is exempt from regulation.

## Known risks and kill criteria

1. **Demand risk:** customers may only value a medical answer.
   - Kill or change lanes if paid demand disappears when cancer language is
     removed.
2. **Regulatory risk:** personalized workflows may still be treated as testing.
   - Do not launch if counsel cannot support the intended-use position.
3. **Scientific risk:** preanalytical variance may overwhelm longitudinal change.
   - Do not show trends until repeatability criteria are met.
4. **Operational risk:** kit and provider failures may destroy unit economics.
   - Stop scaling if support and recollection costs prevent positive margin.
5. **Trust risk:** customers may infer reassurance from neutral charts.
   - Block release if usability testing reveals repeated medical interpretation.
6. **Moat risk:** a polished wrapper is copyable.
   - Invest in qualified protocols, validation datasets, compatibility models,
     provenance contracts, and accumulated run-performance data.

## Decisions recorded

- Consumer-first research software remains the working commercial hypothesis.
- The core product starts with provider-delivered BAM/modBAM and ends with a
  signed research record; kits and logistics are optional integrations.
- The product sells automation, a research workspace, and longitudinal records,
  not a cancer result.
- Provider separation is not treated as a regulatory exemption.
- Claims safety is enforced in product behavior and generated outputs.
- Raw sequence data is local/provider-controlled by default.
- AI reviews report integrity and cannot interpret personal health.
- One analytically validated workflow precedes the three-readout bundle.

## Unresolved decisions

- Exact first measurement to commercialize after analytical comparison.
- Whether Traceback is merchant of record for kits and provider services.
- Named launch states and qualified provider configuration.
- Retention defaults and secondary research-use policy.
- Annual membership price and included processing allowance.
- Whether a genuine IRB-governed study runs in parallel with commercial pilots.
