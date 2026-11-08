# Algorithms reference

This repository has two sets of methods:

- **Traceback Lab** (`traceback_lab/`): 35 registered cfDNA algorithms plus
  two hidden intermediates (`read-summary`, `meth-calls`). This is the main
  analysis surface. Commands and setup are in the [Lab guide](LAB-GUIDE.md);
  a plain-language description of each method is in the
  [explainer](LAB-ALGORITHM-EXPLAINER.md).
- **Historical hackathon demo** (`app.py`, `evidence_inspector/`): an early
  proof-of-concept with three fixed readouts, kept at the end of this page
  for reference only. The separate signed `traceback run` path has its own
  fragment-length, cell-origin and ichorCNA copy-number analyses; see the
  [operator guide](OPERATOR-GUIDE.md).

Research use only. No method here is clinically validated.

## Lab algorithms

`traceback lab algorithms` prints the live list. The table below is taken
from the registry (`traceback_lab/registry.py`, one module per algorithm in
`traceback_lab/algorithms/`) and the read-time interpretation library
(`traceback_lab/interpretation/library.yaml`). "Gate" is the eligible-read
floor the Lab enforces (eligible = primary, mapped, MAPQ ≥ 20, not
duplicate or QC-fail, on chr1–22/X/Y); `--below-depth` runs past most of
these gates and marks the result underpowered. Every method requires an hg38
reference. Platform is ONT unless stated.

| Algorithm | Family | Tier | Gate | Also needs |
|---|---|---|---|---|
| `bam-qc` | qc | PROVEN | none | ONT or Illumina |
| `depth` | qc | PROVEN | none | ONT or Illumina |
| `fragment-size` | fragmentomics | NEWER | none | |
| `long-tail` | qc | NEWER | none | |
| `sex-check` | qc | PROVEN | 500k | chrX and chrY in the reference |
| `mtdna` | fragmentomics | SPECULATIVE | none | chrM in the reference |
| `identity` | qc | SPECULATIVE | 10M | Somalier on `PATH`, registered sites VCF |
| `copy-number` | copy-number | NEWER | 2M (and 1M counted reads) | ichorCNA toolchain and assets; a built panel of normals for an interpretable result |
| `instability` | copy-number | SPECULATIVE | 2M | a `copy-number` result |
| `arm-summary` | copy-number | SPECULATIVE | 2M | `copy-number` with a selected panel of normals (`lab cohort build` from ≥ 10 Lab-aligned samples, each ≥ 2M counted reads) |
| `end-motifs` | fragmentomics | SPECULATIVE | 500k | registered FASTA |
| `breakpoint-motifs` | fragmentomics | SPECULATIVE | 1M | registered FASTA |
| `delfi-ratio` | fragmentomics | SPECULATIVE | 1M | |
| `tss-profile` | fragmentomics | NEWER | 5M autosomal | GENCODE v46 basic GTF |
| `wps` | fragmentomics | SPECULATIVE | 10x depth | free disk for the exact track |
| `aligned-end-structure` | fragmentomics | SPECULATIVE | 10x depth | two hg38 end catalogues |
| `griffin` | fragmentomics | SPECULATIVE | 5M (ONT reads; Illumina pairs) | pyBigWig, `griffin-register` assets; Illumina also Snakemake 5.19.2, bedtools, pybedtools |
| `finaletoolkit-xcheck` | fragmentomics | SPECULATIVE | 1M | FinaleToolkit 1.1.0; ONT or Illumina; about 8 GiB memory on real data |
| `lionheart` | fragmentomics | SPECULATIVE | 5M | LIONHEART 2.2.0, modified mosdepth, Zenodo v003 resources ([Lab guide](LAB-GUIDE.md#algorithms)) |
| `short-read-fragment-size` | fragmentomics | SPECULATIVE | none | Illumina only: prealigned paired-end BAM |
| `modbase-qc` | qc | NEWER | none | MM/ML tags |
| `global-5mc` | methylation | SPECULATIVE | none | MM/ML tags |
| `global-5hmc` | methylation | SPECULATIVE | none | MM/ML tags with a `C+h` channel |
| `hypometh-reads` | methylation | SPECULATIVE | 500k | MM/ML tags |
| `frag-x-meth` | methylation | SPECULATIVE | 1M | MM/ML tags |
| `pmd-score` | methylation | SPECULATIVE | 500k | MM/ML tags, Zhou 2018 PMD BED.gz |
| `cell-origin` | methylation | NEWER | 2.5M | MM/ML tags, Loyfer U250 atlas |
| `epi-clock` | methylation | SPECULATIVE | 5M | MM/ML tags, Weidner hg38 map |
| `meth-tf` | methylation | SPECULATIVE | 2.5M | MM/ML tags, operator marker panel |
| `methylbert` | methylation | SPECULATIVE | 2.5M (not bypassable) | trusted fine-tuned bundle, CUDA GPU |
| `viral-reads` | non-human | SPECULATIVE | none | viral FASTA and targets TSV |
| `microbial` | non-human | SPECULATIVE | none | Kraken2, Bracken and a database (not supported for now) |
| `fetal-fraction` | copy-number | SPECULATIVE | 2M (not bypassable) | Rscript with NIPTeR 1.0.2, input-scoped prenatal control. Reports chr13/18/21 z-scores only: its `fetal_fraction` metric is always null, because NIPTeR does not estimate fetal fraction |
| `fetal-fraction-chry` | copy-number | SPECULATIVE | 2M and calibrated depth (not bypassable) | input-scoped male-fetus calibration |
| `dd-cfdna-chry` | copy-number | SPECULATIVE | calibrated depth (not bypassable) | input-scoped transplant calibration |

Tiers (PROVEN, NEWER, SPECULATIVE) are the interpretation library's
evidence labels; a tier does not validate this ONT implementation. Which
methods run on a real ONT kit today is in the Lab guide's
[status table](LAB-GUIDE.md#algorithm-status-on-real-data).

**How the registry works.** An algorithm declares measurement identity only:
id, version, family, requirements, what it consumes, parameters, tool pins
and upstream steps. Those declarations key its stored results. Tier,
citations, reference policy and footnotes live in the interpretation library
and are joined at read time, so editing them never re-keys a result. Registry
order is execution order. Adding an algorithm is one module, one registry
line, library entries and tests ([Lab guide](LAB-GUIDE.md#adding-an-algorithm)).

**Comparisons.** The default `healthy-v1` cohort is a read-side comparison of
compatible, source-excluded members. A panel of normals applies only to
copy-number and its dependents. Depth curves, split halves and synthetic CNA
spikes are software checks, not clinical validation. See
[Lab interpretation evidence](LAB-INTERPRETATION-EVIDENCE.md).

## Historical: hackathon demo methods

No longer maintained. Kept for reference.

## 1. Fragment length

The pipeline streams alignment records, keeps primary records, deduplicates by
read ID across ordered inputs, requires a positive query length, and caps
accepted lengths at 1,000,000 bp. Every displayed statistic uses the same
denominator.

```text
BAM records
  → primary-record filter
  → first eligible record per read ID
  → raw query-sequence length
  → histogram + mode + median + fraction above 1 kb
```

The public chart uses raw query-sequence length. It does not subtract a fixed
adapter length. That definition matters: an aligned reference span and a raw
length minus 45 bp are different measurements.

## 2. Cell-origin deconvolution

```text
aligned modBAM
  → CpG methylation calls
  → overlap with Loyfer U250 marker regions
  → classify each informative fragment as U, X, or M
  → count-weighted marker methylation vector
  → non-negative least squares against the cell atlas
  → normalize mixture weights
  → seeded bootstrap confidence interval
```

For a marker with informative calls:

- **U:** methylated fraction ≤ 0.25
- **M:** methylated fraction ≥ 0.75
- **X:** intermediate

The observed marker value is the methylated contribution divided by informative
calls. NNLS finds non-negative cell-type weights whose atlas profile best
reconstructs that observed vector. Bootstrap resampling gives a stability
interval; it is not a clinical confidence interval.

The comparison chart uses the observed 23-donor healthy-plasma distribution
from the registered Loyfer reference: min–max, IQR, median, sample estimate, and
bootstrap interval.

The additive v2 bootstrap contract reports every requested resample as
successful, failed, or degenerate. Its resampling unit is an independently
sampled classified U/non-U fragment call within each marker; this does not
preserve molecule linkage across markers and is not molecule-level resampling.
Interval eligibility requires two expected observations in each percentile
tail (80 successful resamples for a 95% interval), no solver failures, and
no undefined draws (failed or degenerate), and positive observed width for that
cell type. A zero-width or unavailable interval is reported as
`insufficient_information`, not as precise uncertainty. The historical v1
bootstrap contract and estimator remain available for compatibility.

## 3. Experimental chromosome dosage

```text
aligned BAM
  → primary mapped non-duplicate reads with MAPQ ≥20
  → read-start counts in complete 5 Mb autosomal bins
  → remove bins below 55% of each chromosome median
  → compare chromosome medians with the genome-wide median
  → flag only shifts beyond ±0.20 log₂
```

This is a conservative whole-chromosome screen. It is not ichorCNA: there is no
panel of normals, segmentation model, or tumor-fraction inference. It cannot
resolve focal or subclonal events. The threshold is a visualization boundary,
not a validated clinical cutoff.

## 4. Bounded AI evidence review

The model does not calculate fragment or methylation results. It receives:

- one claim;
- allowlisted tool capabilities;
- bounded source excerpts;
- aggregate registered results.

It first selects one permitted check. Deterministic code executes or retrieves
the check. The model then returns a strict structured assessment with status,
citations, revised wording, assumptions, and missing validation.

Validation rejects unknown evidence IDs, invented numeric fields, changed
values, mismatched units, changed denominators, changed filters, and stale claim
bindings.

## Regenerating the demo readouts

Private inputs belong under ignored `data/local/` paths (hg38 FASTA and
minimap2 index; the Loyfer U250 regions, markers, atlas and healthy-plasma
workbook; an aligned modBAM with valid `MM`, `ML` and `MN` tags, or a
normalized `modkit extract calls` TSV).

```bash
TRACEBACK_FRAGMENT_HASH_SALT='local-private-value' \
  uv run python scripts/regenerate_cell_origin.py \
  --extract-tsv data/local/cell-origin/calls.normalized.tsv
uv run python -m scripts.regenerate_copy_number \
  --bam data/local/cell-origin/full.hg38.sorted.bam
```

The cell-origin result is written to `data/local/cell-origin/result.json` and
takes precedence over the public aggregate bundle.

## Known limitations of the demo

- One research sample does not establish diagnostic performance.
- The fragment bundle is a registered subset with unverified sample linkage.
- Cell-origin estimates depend on marker coverage and atlas assumptions.
- The dosage screen uses sample-internal normalization and tests only broad
  whole-chromosome shifts.
- Shallow whole-genome sequencing may miss low tumor fractions.
- The report contains a known reproduction mismatch between its updated
  fragment method and older fixed-subtraction instructions.
