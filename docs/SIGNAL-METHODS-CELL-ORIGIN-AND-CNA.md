# Signal methods: cell origin and copy number as signed local records

Status: draft spec, 2026-10-04. Verified against `origin/main` at `149799e`
(includes PR #109, the record view). Line numbers are from that commit.

This is a research prototype at a pre-seed company. Nothing here qualifies a
method, validates an assay or makes a record fit for clinical use. Every output
added here is **unqualified, local, not for clinical use and descriptive
only**, exactly like the fragment-length record.

## Decisions taken by the user (2026-10-04)

- Build **cell origin** (methylation, then the Loyfer atlas, then
  tissue-mixture fractions) into the real pipeline now. It runs in parallel
  with wave 1 of `docs/OPERATOR-USABILITY-AND-ANALYSIS.md`.
- **Copy number uses ichorCNA.** The in-house 5 Mb whole-chromosome screen is
  not promoted.
- Each method produces a real signed, cataloged and viewable record, the same
  as the fragment-length path.
- **Gate G1 (process, not code):** no tissue-mixture fraction or tumour-fraction
  number leaves the team until the scientist co-founder signs off the method.
  This spec records the gate. It builds no feature for it (see "Gate G1").

## 1. Problem, scope, non-goals

**Problem.** Two analyses exist only outside the signed path:

- **Cell origin** is a standalone pipeline in `evidence_inspector/`.
- **Copy number** is a parse-only ichorCNA adapter that never runs R.

The runner signs one thing: the fragment-length histogram. Its stages are
validate, measure and sign (`traceback_runner/cli.py:1290`, `cli.py:1471-1503`).
As a result:
- An operator cannot get a cell-origin or copy-number result that verifies,
  catalogs and renders the way a fragment record does.
- The site has nothing to show for these analyses (`site.js:547-577` renders
  fragment histograms only).

**Scope.** For each of cell origin and copy number:
- registered, content-addressed input assets;
- a locked method definition whose hash enters the job key;
- a run stage, a signed bundle, `verify`, `catalog import` and a record view;
- new problem codes, preflight checks and a canary extension.

**Non-goals:**
- No qualification, analytical validation, clinical claim, healthy/abnormal
  call or reference range in a record.
- No new UI framework. The site stays plain JS plus SVG (`site.js`,
  `chart.js`).
- No new trust model, signing key or catalog database.
- No automatic alignment or re-basecalling.
- No PoN construction.
- No hosted execution.
- No change to a signed byte of any existing fragment-length record.

## 2. Current state (verified in code)

### 2.1 The fragment-length path, end to end

| Step | Location |
|---|---|
| `run` refuses an unaligned or empty BAM before a job, ensures the authority, checks free space and builds the request | `cli.py:1797-1834` (`_refuse_unaligned_or_empty` `cli.py:1507`; `ensure_local_method_authority` `cli.py:1816`) |
| `JobRequest`: `sample_token="local-<ref>"`, `workflow_release_sha256=_local_workflow_sha256(_local_method_sha256(...))` | `cli.py:1826-1833` |
| Workflow hash = `sha256("local-unqualified-v0:" + method_definition_sha256)` | `cli.py:1066-1078`; `_LOCAL_WORKFLOW_ID` `cli.py:1043` |
| Method hash = `method_definition_sha256(local_method_definition(ref))` | `cli.py:1098-1105`; `method_registry.py:637-638` |
| Job key = sha256 of the canonical `JobRequest`; the store dedupes on it | `contracts.py:649-652`; `store.py:450-465` |
| Local request recognised by token prefix + non-synthetic workflow hash | `cli.py:1085-1095` |
| Preflight stage (BLOCKED raises `LocalStageRefusal`; internal error becomes TBX-INTERNAL-001) | `cli.py:1331-1380` |
| Measure stage (`scan_aligned_reference_spans`, `finalize_measurement`; no eligible denominator becomes TBX-RUN-005) | `cli.py:1382-1415` |
| Sign stage (`build_result_bundle(..., method=local_method_identity(...))`) | `cli.py:1417-1469` |
| Publish to `ROOT/records/<record_id>` | `cli.py:899-911` |

**Bundle v3:**
- **Fixed file names.** The `BundlePath` regex (`bundles.py:84-93`) and the
  path constants (`bundles.py:95-102`) hard-code
  `measurements/fragment-length.v1.json` and
  `charts/fragment-length.v1.json`.
- **Version rules.** The table maps v3 to `FragmentMeasurementV2` with
  `TrustNamespace.DEVELOPMENT_LOCAL` (`bundles.py:147-163`).
- **Record ID.** It is derived as
  `record-` + sha256(measurement sha, method identity, provenance sha)[:24]
  (`bundles.py:329-336`).
- **One measurement per bundle.** `verify_bundle` requires
  `measurement_schema_versions == (measurement.schema_version,)`
  (`bundles.py:551-552`).
- **Size caps.** 16 MiB per measurement or chart file, 36 MiB in total
  (`bundles.py:113-123`).

**Authority:**
- **One method per reference.** The store is
  `ROOT/authority/<ref>/{method-registry.json, authority-head.json, pins.json}`.
  It must **equal** the registry rebuilt from code (`local_authority.py:458-467`)
  and holds exactly one definition (`local_authority.py:281-323`).
- **No other entries.** `validate_local_method_authorities` refuses anything
  else under `ROOT/authority` (`local_authority.py:496-527`).

**Catalog:**
- **Peek.** `_peek_local_record` accepts only a v3 manifest with a
  `FragmentMeasurementV2` (`local_catalog.py:126-150`).
- **Result ID.** It is `sha256(bundle_sha256, method_definition_sha256)`
  (`result_catalog.py:707-718`).
- **Compatibility key.** The explorer artifact sets `grid_asset`, `atlas_asset`
  and `panel_asset` to `None` (`local_catalog.py:305-307`). Those slots already
  exist in `MeasurementCompatibilityKey` (`evidence_inspector/compatibility.py:210-212`).

**Record view (PR #109):**
- **Fragment-only.** `LocalRecordView` (`traceback.local-record-view.v1`,
  `web/records.py:134-168`) is fragment-shaped. `_verified_view` rejects
  anything that is not `FragmentMeasurementV2` (`web/records.py:540-543`).
- **State copy.** It lives in `web/state_copy.py` (tables from L34; `STATE_COPY`
  L216).
- **Rendering.** The histogram is rendered by `site.js:509-540` with
  `chart.js`.

**Method model.** It already has the families. `MethodFamily` has
`cell_origin` and `copy_number` (`method_registry.py:117-121`). A definition
carries generic `tools[]` and `assets[]` (`AssetReference(asset_id, version,
content_sha256)`, `method_registry.py:165-168`), and both enter the hash.
**No schema change is needed to put an atlas, wig or PoN into a method hash.**

**References and assets:**
- `reference register` is FASTA-specific: it checks the `.fai`, the per-contig
  M5 and refuses gzip (`references.py:262-301`, `references.py:333-375`).
- It is write-once and idempotent; a mismatch gives TBX-REF-002
  (`references.py:378-401`).
- The FASTA is referenced, not copied (`references.py:1-13`).
- The other asset store (`assets.py:275-300`) is synthetic-only:
  `AssetReference.synthetic_only: Literal[True]` (`release_evidence.py:116-123`).

**Problem codes:**
- Codes must match `^TBX-[A-Z]+-[0-9]{3}$` (`contracts.py:28`).
- The guide table is `| Code | Exit | Cause | Fix |` with an anchor per row
  (`docs/OPERATOR-GUIDE.md:367-395`).
- These families are unused: `ASSET`, `METH`, `CNA` and `TOOL`.

**Canary:**
- `scripts/canary/real_bam_canary.py` drives the CLI in a fresh ROOT per repeat.
- With `--repeat N` every repeat must agree (`real_bam_canary.py:536-566`).
- The baseline lives at `~/.config/traceback-canary/baseline.json`, mode 0600,
  never inside a Git work tree (`real_bam_canary.py:68-72`, `:510-511`, `:579`).
- The measurement path is hard-coded to fragment length (`real_bam_canary.py:49`).

### 2.2 Cell origin today (standalone; exists)

| Piece | Location | Notes |
|---|---|---|
| Pipeline and CLI | `evidence_inspector/cell_origin_pipeline.py` (1,935 lines); entry `scripts/regenerate_cell_origin.py` | Inputs: an aligned modBAM or a normalized extract TSV (`cell_origin_pipeline.py:1803-1860`). Output: `data/local/cell-origin/result.json` |
| modkit call | `cell_origin_pipeline.py:1462-1506` | `modkit extract calls --reference <hard-coded data/local/reference/hg38.primary.fa> --cpg --include-bed <regions> --mapped-only`. **The reference path is hard-coded (`:1477`)**. The version is probed but not pinned (`:1527-1535`). No explicit `--filter-threshold` |
| Pinned modkit 0.6.4 adapter (`extract full`, digest-bound) | `evidence_inspector/modkit_adapter.py:39`, `:164`; `scripts/adapt_modkit_064.py` | Separate research path; not used by `run_pipeline` |
| UXM classification | `cell_origin_models.py:67-69`, `:224-236` | Fragment-level: ≥4 CpGs; U if methylation < 0.251, M if ≥ 0.75, otherwise X |
| NNLS | `deconvolution.py:117` (active-set, `traceback.active-set-nnls.v1`, `:45`), `deconvolve_uxm_v2` `:340` | **The pipeline uses `NnlsRowScale.SQRT_COUNT` (`cell_origin_pipeline.py:1565`).** The enum's own docstring says `REFERENCE_COUNT` is the one that reproduces the reference implementation (`cell_origin_models.py:124-135`) |
| Normalization | `deconvolution.py:367` (weights normalised after the solve); validator `cell_origin_models.py:705-708` | Sum-to-1 is not a constraint in the solve: the NNLS weights are rescaled to sum to 1 afterwards. There is **no unassigned compartment**; `residual_l2` is a fit diagnostic on the row-scaled system (`cell_origin_models.py:698-708`) |
| Bootstrap | `bootstrap_uxm_v2` (`deconvolution.py:517`) | 200 replicates, seed 7 (`cell_origin_pipeline.py:81-82`). Binomial resampling of U and non-U counts per marker, not molecule-level resampling |
| Validation report | `cell_origin_pipeline.py:1632-1636` | **Every check is set to `passed=True` unconditionally.** A signed record must not carry this as it stands (CO3) |
| No alignment filters | `uxm.py`, `cell_origin_inputs.py` | No MAPQ, duplicate, secondary or supplementary filter anywhere in the path; modkit gets `--mapped-only` only |
| Read-ID hashing | `cell_origin_inputs.py` (`load_modkit_extract_calls`) | Requires the `TRACEBACK_FRAGMENT_HASH_SALT` env var. The output does not depend on the salt (verified below) |
| Doc drift | `docs/ALGORITHMS.md` §2 | Says U ≤ 0.25; the code uses U < 0.251 (already in `TODOS.md:3-5`) |
| Caps | `cell_origin_pipeline.py:78-80`; `uxm.py:36-39` | 1e6 calls, 1e5 groups. A cap hit sets `partial_input` (`:987-1045`) or refuses (`:1432`) |
| Healthy-range comparison (Table S8) | `cell_origin_pipeline.py:1073`, `:1269` | Reference-range framing; **excluded from records** (see §3.1) |
| Explorer view contract | `evidence_inspector/cell_origin_explorer.py`; `docs/CELL-ORIGIN-EXPLORER.md` | Label: "estimated fraction among registered atlas contributors". Carries marker and fragment denominators and solver diagnostics |

**Reproduction.**
- **2026-10-01 re-creation** (operator-approved; build log
  `~/scratch/crash-resume-2026-09-30/log-traceback-build.md`, entry 12:16):
  - installed modkit 0.6.4 from bioconda through micromamba;
  - re-aligned to hg38 (UCSC analysisSet);
  - confirmed that the Loyfer markers and regions BED match their recorded
    SHA-256s;
  - matched the older demo result for all 40 contributors within a small
    tolerance. The demo was made from an extract TSV with v1 schemas, so it is
    not byte-comparable.
- **2026-10-04 re-run.** `scripts/regenerate_cell_origin.py --aligned-modbam`
  was run twice on the local aligned BAM, in fresh scratch directories with
  different salts:
  - each run took about 5-7 minutes;
  - the two outputs are **byte-identical to each other and to the 2026-10-01
    output**;
  - the 229 tests in the seven cell-origin test files pass.

The numbers stay out of the repo.

**Tooling and BAM facts:**
- **modkit** is installed at `~/.local/opt/modkit-0.6.4/bin/modkit` (not on
  PATH). Its sha256 does **not** match the `extract full` adapter's pinned
  executable digest (`modkit_adapter.py:40-41`), so that adapter would reject
  the binary on this Mac.
- **`samtools` and `minimap2`** are in `/opt/homebrew/bin`.
- **Tags.** The aligned BAM carries MM/ML/MN.
- **Basecall model.** It is declared only in the *unaligned* BAM's `@RG`, and
  there the modbase field is a placeholder. Alignment drops `@RG`.

**Conclusion:** the code exists, it is deterministic, and it reproduces. It is
fit to promote. Four defects must be fixed in the promotion:
1. the hard-coded reference paths (`cell_origin_pipeline.py:459-460`, `:1477`);
2. the unpinned modkit and its implicit filter threshold;
3. the missing alignment filters;
4. the unconditional validation report.

### 2.3 Copy number today

**In-house screen (not promoted):**
- `evidence_inspector/copy_number.py` (schema `copy-number-screen.v1`, :26;
  5 Mb bins, MAPQ ≥ 20, :28-31).
- `copy_number_qc.py` (`copy-number-dosage-qc.v2`, :22).
- Rendered only in Streamlit (`app.py:1159-1345`).
- It says "not ichorCNA" (`copy_number.py:195-204`). E11 requires that it is
  never called ichorCNA (`docs/EPICS.md:343`). It stays as it is.

**ichorCNA adapter (parse and prepare only).** `evidence_inspector/ichor_adapter.py`
does not execute R (`:1-7`). It provides:
- **Pins:** `ICHOR_COMMIT 5bfc03ed…`, `HMMCOPY_COMMIT`, `HMMCOPY_UTILS_COMMIT`
  (`:30-32`). `RuntimeBinding` targets `preparation_only|local_r|oci`
  (`:115-141`).
- **Asset bindings:** wig grid, centromere table and PoN (`:237-335`). **The PoN
  binding declares `rdata_granges` (`:323`), but the argv passes
  `/assets/pon.rds` (`:782`).**
- **Request:** `CnvRunRequest` (`traceback.ichor-request.v1`, `:461-603`) with
  `pon_mode` set to `none_development` or `protocol_matched_frozen` (`:471`).
  It has **no mode for ichorCNA's bundled healthy-donor PoN**.
- **Parameters:** `IchorParameterSet`, autosomes 1-22 only (`:415-458`).
- **Exact argv:** `_expected_argv` (`:727-783`).
- **Parsers:** `.correctedDepth.txt`, `.seg`, `.cna.seg` and `.params.txt`
  (`:1199-1500`). Entry point: `validate_ichor_outputs` (`:1601-1729`).
- **Result:** `CnvDevelopmentResult` (`traceback.ichor-development-result.v1`,
  `:923-1081`). Tumour fraction is carried as `model_fraction` (`:905-921`).

**Explorer contract.**
- `evidence_inspector/cna_explorer.py` (`traceback.cna-explorer-snapshot.v1`,
  `:497`) has `ExplorerChart` with corrected depth plus segments (`:447-454`).
- **No renderer exists.** `app.js:59` dumps JSON. The doc is
  `docs/CNA-EXPLORER.md`.
- Naming caveat: "E09" in that code is a module label. `docs/EPICS.md` E9 is
  pilot operations, and the CNA epic is **E11** (`EPICS.md:328-348`).

**Fixtures.** `tests/fixtures/ichor/{arm_loss,native_na,neutral}/` hold
synthetic 1 Mb-bin outputs. `tests/fixtures/ichor/upstream/` holds pinned wig
and centromere excerpts.

**Tooling on this Mac (arm64), verified 2026-10-04:**
- Not installed: R, Rscript, readCounter.
- Installed: `micromamba` and `mamba`.
- A dry-run solve of `r-ichorcna hmmcopy` for `osx-arm64` resolves natively:
  - r-base 4.4.3;
  - bioconductor-hmmcopy 1.48.0;
  - hmmcopy 0.1.1 (readCounter);
  - r-ichorcna 0.5.1 (noarch);
  - about 359 MB in total.
- Neither Rosetta nor Docker is needed.
- **Version gap:** bioconda r-ichorcna 0.5.1 is a release tag. It is not known
  to equal the adapter's pinned commit `5bfc03ed…`.

## 3. Method definitions

Rule: **every parameter that can change a number enters the method definition**.
That covers:
- tool versions, through `tools[]`;
- asset digests, through `assets[]`;
- everything else, through `parameter_schema_sha256 = sha256(canonical
  parameter JSON)`.

That last field is the same mechanism as `local_authority.py:211`. Changing any
of these changes the method hash, so the job key and the record identity change
too.

### 3.1 Cell origin (`mth_cell_origin_loyfer_uxm`, family `cell_origin`)

**Inputs:**
- the sealed aligned BAM, carrying `MM`/`ML` (and optionally `MN`) tags;
- the registered reference FASTA (the one already used by `run`);
- three registered Loyfer assets.

**External tool:** modkit **0.6.4**. It is the version the 2026-10-01
re-creation used, and the existing adapter pins it (`modkit_adapter.py:39`).
The tool's identity enters `tools[]` and is recorded from three things:
- `modkit --version`;
- the sha256 of the binary;
- the conda lock line it came from.

At run time, a different version refuses with TBX-TOOL-001. modkit 0.6.4 is
available from bioconda for macOS arm64; the 2026-10-01 re-creation installed
it that way.

**Registered assets** (§3.3), all content-addressed and fed into `assets[]`:

| Asset kind | File (local, gitignored) | Asset ID |
|---|---|---|
| `loyfer-atlas` | `Atlas.U250.l4.hg38.full.tsv` | `asset_loyfer_atlas_u250_l4_hg38` |
| `loyfer-markers` | `Markers.U250.hg38.tsv` | `asset_loyfer_markers_u250_hg38` |
| `loyfer-regions` | `Regions.U250.l4.hg38.bed` | `asset_loyfer_regions_u250_l4_hg38` |

**Locked parameters** (`CellOriginParametersV1`, canonical JSON):

| Parameter | Value (default) | Source today |
|---|---|---|
| `modkit_subcommand` | `extract calls` with `--cpg --mapped-only --include-bed <regions>` | `cell_origin_pipeline.py:1472-1484` |
| `modkit_filter_threshold` | an explicit value (CO1 decides it). **Never modkit's automatic estimate**, which is not recorded in the result today. The automatic value from the reproduced run is the candidate default, so numbers stay comparable | not set today; defect |
| `modification_codes` | `m`, `h` collapsed to "methylated" | `uxm.py:31` |
| `uxm_min_cpgs` / `u_max_exclusive` / `m_min_inclusive` | 4 / 0.251 / 0.75 | `cell_origin_models.py:67-69` |
| `min_mapq`, flag exclusions | MAPQ 20; secondary, supplementary, QC-fail and duplicate excluded (same as fragment policy) | new (modkit `--mapped-only` only; CO3 adds a pysam pre-filter or modkit flags, decided in CO1) |
| `nnls_row_scale` | `sqrt_count` (today's behaviour), **flagged for sign-off** (Q3) | `cell_origin_pipeline.py:1565` |
| `nnls_tolerance`, `max_iterations`, solver ID | `1e-12`; `traceback.active-set-nnls.v1` | `deconvolution.py:44-45` |
| `bootstrap_replicates`, `random_seed` | 200, 7 | `cell_origin_pipeline.py:81-82` |
| `caps` (calls, groups, CpGs per group) | 1e6, 1e5, 1e4. **A cap hit is a refusal, not a partial record** | `cell_origin_pipeline.py:78-80` |
| `min_classified_fragments`, `min_observed_markers` | floors below which the record is refused (TBX-METH-004). Defaults are set in CO3 from the synthetic fixture and the one real BAM, then confirmed by the scientist | new |
| `atlas_contributor_set` | all atlas columns of the registered atlas (no relabeling or merging) | `cell_origin_explorer.py` contract |

**Excluded on purpose:**
- **Healthy-range comparison.** It is the Table S8 / `range_comparison` reference
  range (`cell_origin_pipeline.py:1073`). E10 forbids a healthy/abnormal
  classification (`EPICS.md:316-324`), and a reference range invites one.
- **Partial-input records.** A cap hit refuses the run instead.

**Output: `CellOriginMeasurementV1`** (`traceback.cell-origin-measurement.v1`).
It is a new contract in `traceback_runner/contracts.py` that reuses the
evidence models' validators instead of copying them. Fields:

```
schema_version, definition_id, approval_state="unapproved_local", reference_id,
modbase_model: {id, source: header|operator_declared},
denominators: {records_scanned, eligible_alignments, alignments_with_mod_tags,
  marker_overlapping_fragments, classified_fragments (U+M), mixed_fragments (X),
  excluded_fewer_than_4_cpgs, registered_markers, observed_markers},
cpg_calls: {inspected, retained, excluded_by_reason{...}},   # uxm.py:43-60 counters
marker_counts: [{marker_id, u, m, x}],                        # integers; the solver input
estimates: [{contributor_id, fraction, raw_nnls_weight,
  interval: {low, high} | null, interval_state}],             # sorted by contributor_id
solver: {converged, iterations, residual_l2, objective_value, row_scale}
```

**Validators:**
- the fractions sum to 1 (as `cell_origin_models.py:705-708`);
- the contributor set equals the atlas columns;
- `classified_fragments = sum(u + m)` over the markers.

**Floats** are serialized with the existing canonical JSON. Reproducibility is
checked on the canonical bytes (§6).

**Signed:** the measurement file, a chart file derived from it, provenance
(including the tool identity and `modbase_model`), limitations and the report.
The signing is the same `BundleSigningPayloadV3`-style payload
(`bundles.py:197-209`), extended in v4 (§4).

**Limitations text (fixed strings):**
- "Estimated fraction among registered atlas contributors (Loyfer U250 atlas).
  Fractions are forced to sum to 1; the method has no unassigned compartment."
- "Descriptive; not a diagnosis, not compared with any reference range."
- "Unqualified, local, not for clinical use."
- "ONT basecaller methylation calls; the atlas was built from WGBS."

**Preflight checks** (cell origin only; they reuse the `PreflightCheck` shape,
`contracts.py:377-387`):

| Check | Outcome | Code |
|---|---|---|
| MM/ML tags absent or contradictory in the sample | BLOCKED for cell origin. The fragment path keeps today's PARTIAL | TBX-METH-001 |
| Tags valid, no `@RG DS modbase_models=` declaration, and no `run --modbase-model ID` | BLOCKED for cell origin (the fragment path keeps WARN, `preflight.py:670-675`) | TBX-METH-002 |
| Reference contigs lack the Loyfer regions' contigs | BLOCKED | TBX-METH-003 |
| modkit missing or wrong version | BLOCKED before the job | TBX-TOOL-001 |

**Every aligned Dorado BAM fails TBX-METH-002**, because alignment drops
`@RG` (`docs/OPERATOR-GUIDE.md:166-168`). So `run --analysis cell-origin
--modbase-model ID` lets the operator declare the model:
- it is recorded as `source: operator_declared` and signed;
- the record view labels it "basecall model declared by the operator, not read
  from the file".

### 3.2 Copy number (`mth_copy_number_ichorcna`, family `copy_number`)

**Inputs:**
- the sealed aligned BAM and its index;
- the registered reference, for the contig check (ichorCNA uses UCSC naming,
  `--genomeStyle UCSC`, `ichor_adapter.py:727-783`);
- the registered ichorCNA assets.

**External tools:** an optional toolchain (§3.4).
- `readCounter` from `hmmcopy` 0.1.1;
- R 4.4.x, `bioconductor-hmmcopy` 1.48.0 and `r-ichorcna` (0.5.1 or the pinned
  commit, Q2);
- all from one micromamba lock file for `osx-arm64`.

`tools[]` records three things:
- the lock-file sha256;
- the `runIchorCNA.R` sha256;
- the readCounter binary sha256.

The adapter's `RuntimeBinding` (`ichor_adapter.py:115-141`) is re-pinned in CN1
to whatever Q2 decides.

**Registered assets:**

| Kind | Default | Note |
|---|---|---|
| `ichor-gc-wig` | `gc_hg38_1000kb.wig` from the installed package's `extdata` | Registered from the toolchain path by digest |
| `ichor-map-wig` | `map_hg38_1000kb.wig` | Same |
| `ichor-centromere` | `GRCh38.GCA_000001405.2_centromere_acen.txt` | Matches `tests/fixtures/ichor/upstream/PROVENANCE.md` |
| `ichor-pon` | **none** (`pon_mode=none_development`, `ichor_adapter.py:471`) by default | Q1 |

The asset file names come from knowledge of the ichorCNA package. CN2 verifies
them against the installed package before anything is pinned.

**Locked parameters** (`CopyNumberParametersV1`, which wraps
`IchorParameterSet` `ichor_adapter.py:415-458` plus the counting policy at
`:386-403`):

| Parameter | Default | Why |
|---|---|---|
| `bin_size_bp` | 1,000,000 | Matches the fixtures and the adapter tests (`tests/test_ichor_adapter.py:100-101`). 500 kb is the alternative (Q4) |
| readCounter `--quality` | 20 | Same MAPQ as the fragment policy |
| readCounter `--chromosome` | `chr1..chr22` | The adapter is autosomes-only (`ichor_adapter.py:415-458`). chrX and chrY are excluded and stated in the limitations |
| `normal_fraction_starts` | `0.95, 0.99, 0.995, 0.999` | ichorCNA's low-tumour-fraction recommendation; matches the adapter tests (`test_ichor_adapter.py:243-256`) |
| `ploidy_starts`, `max_copy_number` | `2`; `3` | Same |
| `include_subclonal_states` | false | Same |
| `transition_probability`, `transition_strength` | `0.9999`, `10000` | Same |
| `minimum_map_score`, `centromere_flank_bp` | `0.9`, `100000` | Same |
| `minimum_segment_bins`, `altered_fraction_threshold` | `50`, `0.05` | Same |
| `lambda_policy` | automatic | Same |
| `min_counted_reads` | floor (TBX-CNA-002). Default 1,000,000 reads at MAPQ ≥ 20 in chr1-22 (about 0.1x at about 300 bp, the ichorCNA ULP regime). CN3 confirms or adjusts it from the fixture | new |

**Output: `CopyNumberMeasurementV1`** (`traceback.copy-number-measurement.v1`).
It is built from `validate_ichor_outputs` (`ichor_adapter.py:1601-1729`), so the
parsers are reused, not rewritten. Fields:

```
schema_version, definition_id, approval_state, reference_id,
counts: {records_scanned, counted_reads, bins_total, bins_used, bins_masked},
bins: [{chr, start, end, log2_corrected | null}],            # .correctedDepth.txt, ~2,900 rows at 1 Mb
segments: [{chr, start, end, n_bins, median_log2, copy_number, call}],
solution: {model_fraction, ploidy, pon_mode, identifiable: bool},
stated_lower_limit: {value: 0.03, basis: "<citation string>"}  # fixed text in the definition
```

Notes on the fields:
- **Size.** At 1 Mb bins the file is far below the 16 MiB cap
  (`bundles.py:113-123`).
- **`model_fraction`.** It keeps the adapter's name (`ichor_adapter.py:905-921`).
  The view labels it as ichorCNA's tumour-fraction estimate.
- **The `.RData` output** is never deserialized or signed. Its digest goes into
  provenance, as the adapter already does (`:675-724`).

**Limitations text (fixed):**
- "ichorCNA tumour-fraction estimate from a model built for shallow short-read
  sequencing."
- "The published lower limit is about 3% at about 0.1x short-read coverage
  (Adalsteinsson et al., Nat Commun 2017). It is not established for this
  nanopore protocol."
- "No panel of normals: bin-level noise is not corrected against healthy
  samples." (This line appears only when `pon_mode` is none.)
- "Autosomes only."
- "Descriptive, unqualified, local, not for clinical use."

**ONT long reads.** cfDNA fragments are short (about 170 bp) whatever the
platform, so most reads are fragment-length molecules. The mismatch comes from
four other places:
1. GC and mappability bias differ between nanopore and Illumina chemistry, and
   ichorCNA's GC and map wigs and its default PoN were built from Illumina ULP
   data.
2. Nanopore error profiles shift MAPQ.
3. A minority of long or concatemer reads can span bin edges. readCounter counts
   one read once, by its position, so a long read adds to one bin, not to every
   bin it covers.
4. Depth is usually lower per run.

What this means for interpretation:
- **Bin size.** Use 1 Mb by default; 500 kb only when there are many more
  reads than the floor.
- **Tumour fraction.** Read it as a model output, with its lower limit always
  shown.
- **No PoN.** Expect noisier log2 than the published sWGS regime.
- **PoN later.** A protocol-matched PoN (the adapter's `protocol_matched_frozen`)
  is the real fix. It needs healthy ONT cfDNA samples, which is out of scope
  here.

**Checks:**

| Check | When | Code |
|---|---|---|
| Toolchain absent or lock digest mismatch | before the job (`run`) and in `doctor` (optional section) | TBX-TOOL-002 |
| Index-reported mapped reads on chr1-22 below the floor (cheap upper bound from the `.bai`) | preflight | TBX-CNA-001 (BLOCKED) |
| Counted reads (MAPQ ≥ 20, chr1-22) below the floor | measure stage | TBX-CNA-002 |
| BAM contigs not UCSC-style `chrN` | preflight | TBX-CNA-003 |
| ichorCNA exit non-zero, timeout or output fails the adapter's validation | measure stage | TBX-CNA-004 |
| Adapter reports the solution unidentifiable (force-zero rule, `ichor_adapter.py:1679-1690`) | measure stage: **record created** with `identifiable: false`; the view shows "no tumour-fraction estimate" | none (state, not failure) |

### 3.3 Asset registration (reuse, not a new registry)

Add `register_asset(root, kind, asset_id, path)` beside `register_reference` in
`references.py`. It reuses that module's patterns:
- the ID grammar (`references.py:35`);
- the write-once and idempotent `_existing` check (`references.py:378-401`);
- the same atomic no-replace write.

Storage is `ROOT/assets-local/<kind>/<id>/registered-asset.json`, holding:
- `{kind, asset_id, file_sha256, byte_size, parse_check}`;
- a `source.json` locator.

The file is referenced, never copied (same as FASTA).

`kind` is a closed enum:
- `loyfer-atlas`, `loyfer-markers`, `loyfer-regions`;
- `ichor-gc-wig`, `ichor-map-wig`, `ichor-centromere`, `ichor-pon`.

Each kind has a structural parse check at registration:
- the TSV header;
- for BED, the column count;
- for wig, `fixedStep` with span = step = bin size (the rule at
  `ichor_adapter.py:237-274`);
- for the centromere table, its columns (`:289-315`).

CLI: `traceback asset register --kind K --id ID --file PATH --root R`,
`traceback asset show ID`, and `asset register --from-toolchain ichor
--bin-size 1000000`. The last one finds the wig and centromere files inside the
installed package.

At `run` and at `doctor --deep`, the file is re-hashed:
- a digest mismatch gives TBX-ASSET-002;
- a missing file gives TBX-ASSET-003.

`assets.py` (synthetic, framed packages) is **not** reused. Its
`synthetic_only: Literal[True]` contract (`release_evidence.py:116-123`) would
have to be loosened, which is a larger trust change.

### 3.4 Optional toolchains (doctor, not a blocker)

`doctor` gains a section "Optional analyses" with one line per method:

```
cell origin   modkit 0.6.4 ......................... ready | missing (TBX-TOOL-001)
copy number   ichorCNA toolchain (lock <sha12>) ..... ready | missing (TBX-TOOL-002) | not installed (optional)
```

Missing optional toolchains never fail `doctor`. They refuse only
`run --analysis` for that method.

`traceback toolchain install ichor` creates `ROOT/toolchains/ichor/` from the
committed lock file `toolchains/ichor-osx-arm64.lock`:
- it runs `micromamba create --file <lock> --prefix ...`;
- it prints the command first and needs `--yes`.

Linux runners use a second lock, `toolchains/ichor-linux-64.lock`, for CI.

modkit is a single binary. Its toolchain is a second lock file, or a recorded
`--modkit PATH` checked by sha256.

## 4. One record per method (recommended)

**Recommendation.** One BAM gives **one signed record per method**: up to three
jobs, three bundles and three catalog rows. **Not** one record with several
measurements.

Why:
1. **Job key.** It is built from one `method_definition_sha256`
   (`cli.py:1066-1078`, `cli.py:1826-1833`). One record per method keeps the
   formula `sha256("local-unqualified-v0:" + method_definition_sha256)` without
   change. A combined record would need a composite "method set" hash, and any
   change to one method would re-key and re-run all three.
2. **Verification.** `verify_bundle` enforces one measurement schema per bundle
   (`bundles.py:551-552`). The record ID binds one measurement to one method
   identity (`bundles.py:329-336`), and the catalog result ID binds one bundle
   to one method hash (`result_catalog.py:707-718`). All of these stay true.
3. **Failure isolation.** A failure in one method does not block the others.
   Low depth or no R makes copy number fail without blocking the fragment or
   cell-origin record. A combined record would fail as one unit.
4. **Authority.** The authority store is one method per store
   (`local_authority.py:281-323`). Per-method stores reuse it as-is.
5. **Gate G1.** It is per method. A cell-origin record can be cleared while copy
   number is not.

**Mechanics:**

| Element | Value |
|---|---|
| CLI | `traceback run BAM --reference REF --analysis fragment,cell-origin,copy-number` (default `fragment`; today's behaviour is unchanged). Creates one job per analysis, in order, and prints one JOB_ID per line |
| Sample token | `local-<ref>` (fragment, unchanged); `local-<ref>:cell-origin`; `local-<ref>:copy-number`. `cell-origin` and `copy-number` are reserved policy IDs in B2a's grammar, so D2's `local-<ref>:<policy_id>` stays unambiguous |
| Workflow hash | the same `_local_workflow_sha256(method_definition_sha256)` (`cli.py:1066`) with each method's own definition |
| `_is_local_request` | unchanged (token prefix + non-synthetic hash, `cli.py:1085-1095`) |
| `resume` | parses the method from the token suffix, the way D2 parses a policy |
| Authority | `ROOT/method-authority/<ref>/<method_slug>/`, built by the same `_create_store` / `open_local_method_authority` (`local_authority.py:391-493`), parameterised by method. **The same parameterisation B2b needs for `research-authority`**: build it once (SH1) and let B2b reuse it |
| Bundle | **v4** (`traceback.result-bundle.v4`): the same file set, but the measurement and chart paths are chosen by the measurement schema (`measurements/cell-origin.v1.json`, `measurements/copy-number.v1.json`, ...). The v3 rules row and the fragment paths are untouched, so every existing record verifies byte for byte |
| Signing | the v3 payload shape, with the namespace still `development-local` |
| Input copy | each job seals its own copy (2x free space each, `cli.py:1553`). Accepted for now; sharing one sealed copy is a follow-up with a trigger (§10) |

## 5. Record view on the site

`LocalRecordView` stays `traceback.local-record-view.v1` for fragment records.
Two new view types are added:
- `traceback.local-cell-origin-view.v1`;
- `traceback.local-copy-number-view.v1`.

They are selected in `_verified_view` by the measurement type, which today
rejects anything else (`web/records.py:540-543`).

The checks do not change:
- authority-bound read;
- catalog `verify_reference`;
- a measurement sha match;
- any failure gives 503 TBX-WEB-503 (`web/records.py:602-622`).

`RecordSummary` gains `analysis: fragment|cell_origin|copy_number`, so that the
catalog table shows an "Analysis" column.

Every new view shows a fixed banner at the top: "Unqualified. Local
development record. Not for clinical use. Descriptive only."

**Cell origin (plain language):**
- **Heading:** "Estimated cell-type mixture (Loyfer atlas)".
- **Ranked horizontal bar:**
  - one bar per contributor, sorted by fraction;
  - the top 12 shown, with the rest summed as "Other atlas contributors (n)"
    (12 = `DEFAULT_TOP_COMPOSITION_ROWS`, `cell_origin_pipeline.py:83`);
  - a bootstrap interval whisker only where `interval_state` is available. A
    partial interval draws no whisker (the rule in `docs/CELL-ORIGIN-EXPLORER.md`);
  - a full table below the chart, holding every contributor.
- **Unassigned and residual, said plainly:** "Fractions are forced to add up to
  100%. This method has no 'unassigned' share. How well the atlas explains the
  data is shown as the fit residual: <residual_l2>." No fake unassigned bar is
  drawn.
- **Coverage denominator strip:**
  - "Based on <classified_fragments> fragments with ≥4 CpGs at <observed_markers>
    of <registered_markers> atlas markers";
  - then "<mixed_fragments> mixed fragments not used", then "<eligible_alignments>
    eligible alignments".
- **Basecall model:** the declared model and its source.
- **States copy (new rows in `state_copy.py`):**
  - `cell_origin.ready`: "Mixture estimated";
  - `cell_origin.low_coverage` (record refused, shown in jobs): "Too few marker
    fragments to estimate a mixture";
  - `modbase.operator_declared`: "Basecall model declared by the operator".

**Copy number:**
- **Heading:** "Copy-number profile (ichorCNA)".
- **Genome-wide log2 ratio plot (SVG):**
  - chromosomes 1-22 along x, alternating shading;
  - a corrected log2 dot per bin, with null bins omitted;
  - segment medians as horizontal lines coloured by `call`;
  - y fixed at −2..2, labelled "log2 ratio vs this sample's median";
  - no clinical threshold lines (`clinical_thresholds_present: False`, as in
    `cna_explorer.py:447-454`).
- **Tumour-fraction estimate:**
  - "ichorCNA tumour-fraction estimate: <model_fraction as %> (ploidy <p>)";
  - always followed by "Stated lower limit: about 3%, from short-read data; not
    established for this nanopore protocol. Values near or below this are not
    distinguishable from zero.";
  - when it is unidentifiable: "No tumour-fraction estimate: the model could not
    separate tumour from normal signal."
- **Denominators:** "<counted_reads> reads counted in <bins_used> of
  <bins_total> 1 Mb bins (MAPQ ≥ 20, chr1-22)"; also "No panel of normals" when
  that applies.
- **Segment table:** chr, start, end, bins, median log2, call. It is shown
  collapsed by default.
- **States:** `copy_number.ready`, `copy_number.no_estimate`; TBX-CNA-002 is
  shown in jobs as "Too few reads for copy-number analysis".

Accessibility and layout rules from C1 apply: an SVG plus a table, no
horizontal scroll at 390 px, and copy from one table so that the docs and the
site cannot drift.

## 6. Canary

`scripts/canary/real_bam_canary.py` gains `--analysis` (default `fragment`;
today's behaviour is unchanged).

**Measurement path.** It is read from the bundle manifest instead of the
constant `MEASUREMENT_RELATIVE` (`real_bam_canary.py:49`).

**Metrics per analysis:**
- **cell origin:** the canonical measurement sha256, the denominators, the
  contributor fractions and `residual_l2`.
- **copy number:** the canonical measurement sha256, `counted_reads`,
  `bins_used`, the segment count and `model_fraction`.

**Reproducibility** (`--repeat 2`): equal canonical measurement bytes across
two fresh ROOTs. Both methods are expected to be deterministic:
- NNLS and a seeded bootstrap;
- an EM run on fixed inputs.

If the copy-number method is not byte-reproducible, the canary fails and CN6
does not merge. A tolerance is never added silently. Any tolerance is an
explicit, scientist-approved method parameter.

**Baselines.** One per analysis: `baseline-cell-origin.json` and
`baseline-copy-number.json`. They sit beside the existing baseline under
`~/.config/traceback-canary/`, mode 0600, and are refused inside a Git work tree
(`real_bam_canary.py:510-511`).

**What stays out of the repo.** No real number, baseline or log enters it. The
synthetic canary (`tests/fixtures/canary/synthetic_baseline.json`) gains
synthetic cell-origin values only. Copy number stays out of the synthetic
nightly unless the CI toolchain lock exists (CN6).

**Nightly schedule.** The 03:30 launchd canary keeps running fragment only. An
analysis is added to it only after its baseline exists.

## 7. Build plan

Effort is given as human days / CC time. Each item is one PR. Acceptance tests
only that item's own behaviour.

**Test rules for every item:**
- **Synthetic fixtures are generated in tests.** That covers modBAMs with MM/ML
  through the existing `fixtures.py` generator, extended with known U/M
  patterns over a 3-marker mini-atlas. The mini-atlas, its wig files, the
  centromere file and the ichorCNA outputs are written to `tmp_path`.
- **Every guard gets a mutation check.** Remove or invert it and a named test
  fails. The PR lists the mutations it tried.
- **No real data and no real numbers** in fixtures, docs or PR text.

| # | Title | Priority | Effort | Depends on |
|---|---|---|---|---|
| SH1 | Method-parameterised authority store `ROOT/method-authority/<ref>/<slug>/` | Critical | 2 d / 1.5 h | none (B2b later reuses it) |
| SH2 | `asset register` / `asset show` in `references.py`; TBX-ASSET-001..003 | Critical | 1.5 d / 1 h | none |
| SH3 | Bundle v4: per-schema paths, rules row, verify, catalog peek and reader | Critical | 3 d / 2 h | none |
| SH4 | `run --analysis`, token suffix, resume, one job per method, doctor "Optional analyses", problem rows | Critical | 2 d / 1.5 h | SH1, SH3; wave-1 `cli.py` chain (rebase) |
| SH5 | Record view dispatch by measurement type; `analysis` column; banner | High | 1.5 d / 1 h | SH3, PR #109 (merged) |
| CO1 | modkit 0.6.4 pin, tool identity, explicit filter threshold, flag pre-filter decision; TBX-TOOL-001 | Critical | 1 d / 45 min | none |
| CO2 | `CellOriginParametersV1`, the locked definition and Loyfer asset kinds | Critical | 1.5 d / 1 h | SH2, CO1 |
| CO3 | Cell-origin measure stage over the registered reference (fixes `:459-460`, `:1477`); alignment pre-filter; real validation checks instead of `passed=True` (`:1632-1636`); `CellOriginMeasurementV1`; floors; preflight TBX-METH-001..004; `--modbase-model` | Critical | 3 d / 2.5 h | CO2, SH1, SH4 |
| CO4 | Sign, verify, import and catalog for cell origin; explorer artifact binds `atlas_asset` | High | 1.5 d / 1 h | CO3, SH3 |
| CO5 | Site: ranked bar, contributor table, denominator strip, state copy | High | 2 d / 1.5 h | CO4, SH5 |
| CO6 | Canary `--analysis cell-origin`; synthetic baseline values | Medium | 1 d / 45 min | CO4 |
| CN1 | ichorCNA toolchain lock files (osx-arm64, linux-64), `toolchain install ichor`, doctor line, pin reconciliation (Q2), PoN format fix; TBX-TOOL-002 | Critical | 2 d / 1.5 h | none |
| CN2 | ichorCNA asset kinds; `--from-toolchain` registration; wig and centromere parse checks | High | 1 d / 45 min | SH2, CN1 |
| CN3 | readCounter plus `runIchorCNA.R` stage via the adapter's `local_r` binding; `CopyNumberMeasurementV1` from `validate_ichor_outputs`; depth floor; TBX-CNA-001..004 | Critical | 3.5 d / 2.5 h | CN2, SH1, SH4 |
| CN4 | Sign, verify, import and catalog for copy number; `grid_asset` / `panel_asset` bound | High | 1.5 d / 1 h | CN3, SH3 |
| CN5 | Site: genome-wide log2 SVG, segments, tumour-fraction line with its lower limit, state copy | High | 2.5 d / 2 h | CN4, SH5 |
| CN6 | Canary `--analysis copy-number`; CI job behind the toolchain lock (macOS and ubuntu) | Medium | 1.5 d / 1 h | CN4 |
| DoD | Acceptance script plus one real-BAM run per method by the operator (manual table, no numbers) | Critical | 1 d / 45 min | all |

**Per-item notes and acceptance (abridged; each PR expands its own):**

- **SH1.**
  - `ROOT/authority/` stays byte-identical, and `validate_local_method_authorities`
    still refuses unknown names there.
  - A damaged method store hides only that method's records
    (TBX-AUTHORITY-002 per D15, or the current TBX-AUTH-LOCAL-002 if D15 has
    not landed).
  - Mutation: drop the equality check at `local_authority.py:464`, and a
    tampered method store must still be caught by a test.
  - +8 tests.
- **SH2.**
  - Write-once: re-registering the same bytes exits 0; different bytes give
    TBX-ASSET-001 (exit 3).
  - Each kind's parse check rejects a malformed file: a BED with 2 columns, a
    wig whose step differs from its span, and an atlas without a header.
  - Re-hashing at run time catches a 1-byte edit (TBX-ASSET-002).
  - +10 tests.
- **SH3.**
  - The golden test: every v3 fixture bundle verifies byte for byte.
  - A v4 bundle whose measurement path does not match its schema is refused.
  - A v4 bundle with two measurements is refused.
  - Mutation: allow a 2-tuple at `bundles.py:551`, and a test fails.
  - +10 tests.
- **SH4.**
  - Same BAM, `--analysis fragment,cell-origin` gives two job IDs. Re-running
    gives the same two (dedupe).
  - Resuming an interrupted cell-origin job resumes cell origin.
  - Without `--analysis`, the fragment `JobRequest` bytes equal `main`'s.
  - +8 tests.
- **CO3.**
  - Planted U/M fixture: the expected fragment classifications.
  - A known 2-contributor mixture is recovered within solver tolerance on the
    mini-atlas.
  - Byte-identical measurement across 2 runs.
  - Each preflight and floor guard is mutation-checked.
  - No `data/local` path appears in the code (grep test).
  - +14 tests.
- **CN3.**
  - Runs only when the toolchain fixture marker is present. Otherwise the stage
    is unit-tested with recorded synthetic adapter outputs from
    `tests/fixtures/ichor/`.
  - Depth floor, non-zero exit, timeout and the unidentifiable path are each
    tested and mutation-checked.
  - `.RData` is never opened (asserted).
  - +14 tests.
- **CO5 and CN5.**
  - DOM harness (`tests/web/app_dom_harness.js`) checks:
    - the bar count equals min(12, n) + 1 "Other";
    - no whisker for a partial interval;
    - the lower-limit sentence is always present next to the tumour-fraction
      value;
    - no `<pre>`.
  - Screenshots at 1280 and 390 px are attached to each PR.

**Dependency graph:**

```
SH2 ─┬─> CO2 ─> CO3 ─> CO4 ─┬─> CO5
     │   CO1 ─┘              └─> CO6
     └─> CN2 ─> CN3 ─> CN4 ─┬─> CN5
         CN1 ─┘              └─> CN6
SH1 ─┬─> CO3, CN3
SH3 ─┼─> SH4 ─> CO3, CN3 ;  SH3 ─> SH5 ─> CO5, CN5
     └─> CO4, CN4
DoD after all
```

**Lanes and collisions.** SH4 is the only item on the wave-1 `cli.py` serial
chain:
- it lands after A4c, or rebases onto it;
- if D10 (the `cli.py` split) is scheduled, SH4 lands after D10;
- SH1 must be agreed with B2b's owner before either starts.

Three lanes can run in parallel: CO, CN and SH.

**Estimate:**
- shared: 10 d;
- cell origin: 10 d;
- copy number: 12 d;
- DoD: 1 d.

That is about **33 human days**, or 3-4 calendar weeks with three lanes, and
about 25 h of CC build time plus review rounds. On this repo review has roughly
doubled build time, so **5-7 calendar weeks** is the realistic range.

## Gate G1: scientist sign-off (process)

Until the scientist co-founder signs off a method in writing:
- no tissue-mixture fraction or tumour-fraction number from that method leaves
  the team (no deck, email, partner demo, screenshot or shared report);
- `serve` is local and operator-only today, so no code feature is built for
  this.

The sign-off note records:
- the method definition sha256 being approved;
- the parameters in §3;
- the Q1-Q10 answers;
- the real-data reproducibility evidence.

It lives in Notion (the single source of truth), not in the repo.

A later change to the method hash needs a new sign-off. The DoD PR links the
note, or states "G1 not yet cleared".

## 8. Open questions (ranked, with recommended defaults)

1. **Q1. Panel of normals.**
   - **Default:** none (`pon_mode=none_development`), shown in the view.
   - ichorCNA's bundled healthy-donor PoN is short-read Illumina and is the
     wrong noise model for ONT. Using it would also need a third `pon_mode`, a
     contract change to `ichor_adapter.py:471`.
   - A protocol-matched ONT PoN needs healthy ONT cfDNA samples, which is a
     follow-up.
2. **Q2. ichorCNA version pin.**
   - **Default:** bioconda `r-ichorcna` 0.5.1 in a committed micromamba lock,
     with the adapter's `ICHOR_COMMIT` re-pinned to match.
   - CN1 checks whether tag v0.5.1 is commit `5bfc03ed…`. If it is not, CN1
     installs from GitHub at that commit into the same environment. Only one of
     the two can be the recorded pin.
3. **Q3. NNLS row scale.**
   - **Default:** keep `sqrt_count` (today's output, the 2026-10-01
     re-creation).
   - The code's own note says `reference_count` reproduces the reference
     implementation (`cell_origin_models.py:124-135`).
   - The scientist picks one before G1, and the choice changes the method hash.
4. **Q4. ONT read-length handling and bin size.**
   - **Default:** 1 Mb bins, counting by read position (readCounter's
     behaviour), with no length filter.
   - A size window (for example 90-150 bp to enrich tumour fragments) is a later
     method variant with its own hash. It is not v1.
   - 500 kb only when reads are above 2x the floor.
5. **Q5. Optional R toolchain.**
   - **Default:** yes. The ichorCNA toolchain is optional, installed by
     `traceback toolchain install ichor`, and reported by `doctor`.
   - Without it, the fragment and cell-origin paths are unaffected, and the base
     install gains no R.
6. **Q6. Atlas version.**
   - **Default:** Loyfer U250, level-4 hg38 (the local files, whose SHA-256s
     matched the recorded values on 2026-10-01).
   - U25 and coarser groupings are later variants.
   - The atlas sha enters the method hash, so a switch is a new method, not an
     edit.
7. **Q7. Basecall model declaration.**
   - **Default:** accept `run --modbase-model ID`, labelled
     "operator-declared". The guide tells the operator to copy the model from
     the *unaligned* BAM's `@RG`.
   - On the local BAM, that `@RG` names the basecall model, but its modbase
     field is a placeholder. So even the header is not proof of the modbase
     model.
   - Alternative: require the operator to re-insert `@RG` (stricter; every
     aligned BAM fails today).
8. **Q8. Unassigned share.**
   - **Default:** show `residual_l2` and the denominators. Do not show an
     "unassigned" bar.
   - An unassigned compartment (for example, unnormalised NNLS weights) would be
     a method change for the scientist to decide.
9. **Q9. modkit `extract calls` or pinned `extract full`?**
   - **Default:** `extract calls`, the path that reproduces today, with an
     explicit filter threshold and the binary digest recorded in `tools[]`.
   - The pinned `extract full` adapter (`modkit_adapter.py`) is stricter:
     - it pins the command;
     - it checks the CpG dyad against the FASTA;
     - it uses a combined m+h call threshold.
   - But its executable pin rejects the arm64 binary on this Mac, and its output
     has never been compared with the pipeline's.
   - Switching later is a new method hash.
10. **Q10. Flag filtering for modkit.**
   - **Default:** apply the fragment policy's exclusions (secondary,
     supplementary, QC-fail, duplicate, MAPQ < 20) before modkit, so the
     denominators agree across records of the same BAM.
   - This is a change from today's `--mapped-only` only.

## 9. Risks

| Risk | Mitigation |
|---|---|
| **Surface area.** The repo is about 187k lines of tracked Python (`traceback_runner` 24k, `evidence_inspector` 84k, tests 76k). This adds about 4-6k | Reuse: references.py patterns for assets (no new registry), the authority functions parameterised (no second store design), the bundle builder with a per-schema path table (no second bundle family), the existing evidence parsers and solvers imported, not copied. No new web framework. Each PR states its net line count |
| Cell-origin numbers drift with modkit's automatic threshold | The explicit threshold is locked in the definition (CO1); the canary checks bytes |
| ichorCNA not byte-reproducible (parallel EM, R RNG) | Single worker (`--cores 1` or equivalent) is locked; the canary requires equality; failure blocks CN6, not the other lanes |
| Tumour fraction over-read at low depth or without a PoN | The lower-limit sentence is mandatory beside the value (DOM test); the no-PoN line; G1 |
| Mixture fractions read as diagnosis | No reference range; "estimated fraction among registered atlas contributors"; banner; G1 |
| R toolchain breaks on a macOS or R update | Lock file; doctor digest check; optional, so it never blocks other analyses |
| SH1 conflicts with B2b's research authority | Build one parameterised store in SH1; B2b consumes it; agree before either starts |
| SH4 collides with the wave-1 `cli.py` chain | Land after A4c (or D10); keep SH4 small |
| Disk: one sealed input copy per analysis | Documented; trigger for sharing below |
| Atlas or PoN licensing on redistribution | Assets are registered by reference to local files; nothing is committed; the repo holds only digests |

## 10. Out of scope (follow-ups with triggers)

| Item | Trigger |
|---|---|
| Protocol-matched ONT PoN | At least 10 healthy ONT cfDNA samples available and Q1 revisited |
| Shared sealed input across analyses | `run --analysis` with 3 methods on a BAM above 5 GB, or a disk-full refusal |
| Cell-origin bootstrap intervals on the site by default | Scientist asks after G1 |
| Fragment-size-selected CNA variant | Q4 revisited after the first real copy-number records |
| Cross-record comparison of mixtures or profiles | C6 (pair compare) merged and the scientist asks |
| Promoting or retiring the in-house dosage screen | E11 decision revisited |
| `traceback align` | Unchanged from the wave-1 spec |

## 11. Review amendments (/autoplan, 2026-10-04)

**Direction.** Both strategy reviewers recommended a one-week feasibility check
before the build. The user chose to **build as specced** (D1). These items stay
in scope and are recorded only so the scientist can see them before G1:
- marker coverage at about 0.2x is unverified;
- the bootstrap resamples per marker, not per molecule, so intervals are likely
  too narrow;
- the 3% lower limit comes from Illumina data with a PoN.

**Positive control (D2).** Copy-number DoD requires one **public nanopore cfDNA
cancer dataset with a published tumour fraction**, run with no PoN on both that
dataset and the local BAM. If the two cannot be told apart, the record view
keeps the tumour-fraction line hidden behind "not established for this
protocol" until G1. CN1 finds and registers the dataset by digest; nothing from
it is committed.

The amendments below override the sections they name. Each was flagged by at
least two of the three review voices (engineering, Codex, design/operator).

### Architecture (overrides §3.3, §4)

1. **Authority stores are keyed by definition hash, append-only.** The store is
   `ROOT/method-authority/<ref>/<slug>/<method_definition_sha256>/`, validated
   against the inputs stored with it and never against current machine state.
   - A modkit reinstall or atlas re-registration creates a new hash directory.
   - Older records stay viewable. Test: reinstall the tool, then the old records
     still render.
   - The fragment store `ROOT/authority/` is unchanged.
2. **One validator covers both store trees.** `validate_local_method_authorities`
   gains a sibling for `ROOT/method-authority`.
   - `serve` and `run` call both.
   - A ROOT holding only new-method records is valid: a missing `ROOT/authority`
     is no longer an error if `method-authority` exists.
   - A damaged method store hides only its own records (B6).
3. **An orchestrator runs `run --analysis`.**
   - It runs inside one ROOT lock, with fragment first and each analysis
     independent.
   - Per-analysis errors are captured. Every job ID is printed as soon as the
     job is admitted.
   - The result is a per-analysis table in human output and an array in
     `--json`.
   - A terminal failure of one analysis never blocks another.
   - `_refuse_failed_job` is scoped to `(input, analysis)`.
   - Free space is checked once, for N × 2 × input size.
4. **The resolved configuration is part of the job identity.**
   - `--modbase-model` (cell origin) and every resolved per-analysis setting
     enter the `JobRequest` through the method definition or the sample token.
     A re-run with a new declaration is therefore a new job, never a dedupe onto
     a failed one.
   - `resume` parses the analysis with `partition(":")` against a closed enum.
   - `resume` refuses with **TBX-JOB-003** when the stored
     `workflow_release_sha256` differs from the current method hash.
5. **A missing tool at stage time is RETRYABLE**, never terminal. A deleted
   environment must not poison the input.
6. **Bundle v4 reader selection** is versioned by `(bundle_version,
   measurement_schema)`, not by version range alone. It covers:
   - `chart_for_measurement` and `render_bundle_report` in `export.py`, one per
     analysis;
   - the `BundlePath` regex and the per-path size map;
   - the twin-record peek (`cli.py:1657-1670`);
   - catalog `_RESULT_SCHEMA_ID`, chosen per schema;
   - the peek size limit, reconciled with verify.
   The existing `ExplorerArtifactRecord` `cell_origin` and `cna` slots
   (`web/explorer.py:103-104`) are reused. Golden test: every v1-v3 fixture
   still verifies and imports; a mixed-analysis ROOT imports.

### Execution safety (overrides §3.1, §3.2, §3.4)

7. **Assets are copied into the sealed job directory and hashed there.** The
   atlas, markers, regions, wigs and centromere files are all small. Only the
   FASTA stays referenced. At stage start, check its size, mtime and inode; the
   DoD runs `doctor --deep` for the full digest.
8. **Tools run by absolute pinned path.** The binary is hashed right before
   exec. There is no `shutil.which` and no bare `Rscript`.
9. **R runs isolated:**
   - `--vanilla` with a scrubbed environment: `R_LIBS_USER=`, `R_LIBS_SITE=`,
     `R_PROFILE_USER` and `R_ENVIRON_USER=/dev/null`;
   - an asserted `.libPaths()`;
   - BLAS/OpenMP threads set to 1;
   - an explicit RNG seed.
   The environment's `conda-meta` `paths_data` digest enters `tools[]` and is
   verified at run time.
10. **The lock file is `@EXPLICIT` with `#sha256` lines and pinned channel
    URLs.** micromamba runs by absolute path. `toolchain install` without
    `--yes` prints a dry run and exits 0, and it states that it needs the
    network.
11. **The `local_r` binding gets a real path contract.**
    - A private staging directory replaces `/input`, `/assets` and `/attempt`,
      with no symlinks.
    - The adapter's exact-argv test gains a local variant.
    - Logs are bounded, and the process group is killed on timeout, interrupt
      or lease loss. The same rule covers modkit.
12. **modkit output is bounded.**
    - Its temp files go under the job directory, never system `/tmp` (they hold
      read IDs).
    - The call cap becomes a locked parameter, checked while streaming or
      bounded by modkit options.
    - CO1 records the real BAM's row count against the cap.
13. **ichorCNA determinism is shown, not assumed.**
    - `.RData` stays out of signed provenance unless shown to be byte-stable.
    - CI keeps a synthetic baseline per platform.
    - If two local runs differ, CN6 does not merge.

### Record views and catalog (overrides §5)

14. **Evidence comes before estimates.**
    - **Cell origin:** a one-line basis under the identity ("Based on N
      fragments at M of R markers; model declared by operator"), then the bar.
    - **Copy number:** the tumour-fraction line, its lower-limit sentence, the
      reads and bins line and the PoN line come above the plot.
    - The operator label stays the h1, with the analysis line under it.
15. **"Other" never reads as a cell type.** The row is "28 other contributors
    combined (11 at 0%)":
    - always last, hatched and neutral;
    - no whisker;
    - non-zero values below 0.1% shown as "<0.1%";
    - display names from the copy table, with raw IDs in the full table.
    Whiskers are **off by default** (consistent with §10). The residual line
    states its basis and says it has no threshold.
16. **Copy-number states are neutral, not clinical.**
    - The palette is neutral and colour-blind-safe, with a legend: "ichorCNA
      model state (not a clinical call)".
    - Clipped bins get edge markers and a "k bins outside range" line.
    - At 390 px the plot draws a min/max band per pixel column, labels every
      other chromosome, and has a chromosome selector.
17. **New states:**
    - "Made under an earlier method version", distinct from tamper and never a
      503;
    - `cell_origin.not_converged`, which refuses the record;
    - `modbase.header`;
    - interval states;
    - refusals in jobs, with code and label per job, analysis on each job row,
      and analysis-neutral stage copy.
    All are added to `ENUM_SOURCES`.
18. **The catalog columns are common to every analysis:** Analysis, Record,
    Reference, Method version, Key n with its unit, Preflight and Imported, plus
    an Analysis filter.
    - Records from one input are grouped by a short sealed-input digest.
    - Compare accepts only same-analysis pairs.
    - **No mixture or tumour-fraction value appears in the table** (G1
      screenshot risk).

### Operator experience (overrides §3.3, §3.4, §4)

19. **The command is `traceback method-asset`**, not `asset`, which collides
    with the existing `traceback assets`.
    - `method-asset register --from-dir LOYFER_DIR` registers all three Loyfer
      files with IDs derived from the kind.
    - `--from-toolchain copy-number` derives the bin size from the locked
      method, with no `--bin-size` flag.
    - New code **TBX-ASSET-004 "not registered"**; its fix prints the exact
      command.
20. **Toolchains are cached per user** at `~/.cache/traceback/toolchains/<lock
    sha>/`, shared by every ROOT. A fresh ROOT per experiment then costs no
    359 MB reinstall. `toolchain install modkit` exists for symmetry, and
    `copy-number` is accepted as an alias of `ichor`.
21. **`preflight --analysis`** reports METH, CNA and TOOL readiness before a
    run. The TBX-MOD-001 row says "BLOCKED for cell origin: pass
    `--modbase-model`".
22. **Doctor reports readiness per analysis** (tool, assets, contigs) in one
    format: "not set up (optional); next: <command>".
23. **Each code has one guide row with a cause and one fix.** TOOL-001 and
    TOOL-002 are split into missing and wrong version or digest. CNA-001,
    CNA-003, METH-004 and ASSET-001 get full rows. The journey's
    `RECORD_ID="$(ls ...)"` changes to read the IDs printed by `run --json`.
24. **The token reservation** (`cell-origin` and `copy-number` are not usable as
    D2 policy IDs) gets a grammar test in whichever of D2 or SH4 lands second.

### Revised estimate (overrides §7)

| Item | Was | Now | Why |
|---|---|---|---|
| SH1 | 2 d | 3 d | hash-keyed append-only stores, second validator, serve preflight |
| SH3 | 3 d | 6 d | reader selection, export/report per analysis, twin peek, peek limit |
| SH4 | 2 d | 4 d | orchestrator, identity-bound config, resume checks, `cli.py` rebase |
| SH6 (new) | none | 2 d | execution sandbox: absolute paths, staged copies, process-group kill, scrubbed R env |
| CN1 | 2 d | 3 d | explicit lock, user cache, positive-control dataset |
| CO5 + CN5 | 4.5 d | 6 d | states, catalog columns, mobile band plot |

The total is about **45 human days**: 7-9 calendar weeks with three lanes, at
this repo's review rate. That is about 12 days more than §7. Most of the
increase is integration work that §7 did not count.
