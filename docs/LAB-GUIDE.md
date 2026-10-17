# Traceback Lab guide

Traceback Lab runs every cfDNA algorithm a kit BAM can support from one
command, and a local page explains each result. It is a separate, **unsigned**
subsystem: results carry a provenance stamp (tool, version, parameters hash,
input hash, timestamp) and nothing else. The signed `traceback run` path is
unchanged.

**Research use only. Not CLIA-validated. Not a diagnosis.**

## First five minutes

On a Mac with only `uv`, samtools and minimap2 (`brew install samtools
minimap2`), this reaches the Lab page with a synthetic sample:

<!-- lab-journey -->
```bash
uv sync
uv run traceback lab doctor
uv run traceback lab run --example
uv run traceback lab serve --open
```

`lab doctor` lists anything missing, each with the one command that fixes it,
and the missing setup steps in order. `lab run --example` generates a small
synthetic reference and three unaligned chunks (about 200k reads, a planted
167 bp size mode, 5mC calls and chrY reads), registers the reference as
`lab-example` in ROOT and runs the real ingest, alignment, read summary,
algorithms, depth curve and split halves. It takes about 45 seconds on an
M-series Mac. Copy-number is skipped (`example_reference`): it needs
hg38-scale assets. The sample carries an **EXAMPLE (synthetic)** pill.
`lab serve --open` prints the one-use link and opens it.

Measured time to the page on a clean Mac by a teammate: not yet measured
(target ≤ 5 minutes).

## One-time setup for real kits

1. `brew install samtools minimap2` (samtools ≥ 1.20, minimap2 ≥ 2.28).
2. Register hg38: `traceback reference register --fasta hg38.fa --id hg38 --root R`.
3. For copy-number: `traceback toolchain install ichor`, then
   `traceback method-asset register --from-toolchain copy-number --root R`.
4. `traceback lab doctor --root R` should then show no blocked checks.

## Duration and disk for a real run

For a full ~400-chunk run (~44 GB of uBAM), the disk check asks for about
2.2x the chunk bytes plus the minimap2 index the first time (hg38: ~8 GB)
plus 2 GB. Measured on a 30-chunk partial run (3.55 GB): the hg38 index
built in about 1.5 minutes, alignment took about 4.5 minutes at 8 threads
(peak RSS ~10 GB for minimap2), the aligned BAM kept 0.65x of the chunk
bytes, and every algorithm then ran in under a minute.

## ROOT

Every Lab command takes `--root R`. Without it the Lab uses
`$TRACEBACK_ROOT`, else `.traceback` in the current directory (the signed
default). The first line every command prints is the resolved absolute
`ROOT  /…`; `--json` carries it as `data.root`. Set `TRACEBACK_ROOT` once so
commands started in different directories share one store.

Lab data lives under `ROOT/lab/` and never touches signed records:

| Path | What |
|---|---|
| `ROOT/lab/STORE_VERSION` | layout version; a newer store is refused |
| `ROOT/lab/runs/<run_id>.jsonl` | one line per step event; hashes only, never paths |
| `ROOT/lab/inputs/<input_id>/` | an aligned input and its intermediates |
| `ROOT/lab/results/<input_id>/<algorithm>/<parameters_sha256>/` | immutable attempts plus `selected.json` |

## Commands

```
traceback lab run (<BAM|uBAM|chunk dir>... | <id12> | latest | --example)
    [--reference ID] [--profile all|fast] [--only a,b]
    [--downsample 0.5,0.25,0.1 | --no-downsample]
    [--controls | --no-controls] [--tf 0.03,0.10,0.30]
    [--skip-bad-chunks] [--sex M|F] [--force] [--timeout-s 3600]
    [--threads N] [--sort-mem 1G] [--mmi PATH]
    [--dry-run] [--verbose] [--json] [--root R]
traceback lab simulate cna <id12|latest> [--tf 0.03,0.10,0.30] [--verbose]   synthetic CNA spike, copy-number recovery
traceback lab simulate split <id12|latest> [--verbose]                         split-half pseudo-replicate
traceback lab algorithms [--json]          registered algorithms (id, tier, min depth, licenses, footnote)
traceback lab doctor [--json]              setup checks, each with a fix command
traceback lab serve [--open]               the local page (loopback only)
traceback lab runs [RUN_ID] [--step S]     the run log, or one run's steps
traceback lab identity sites add --file SITES.vcf.gz --sha256 SHA256 --source-label LABEL [--root R]
traceback lab identity sites inspect [--root R]
traceback lab identity compare SAMPLE_A SAMPLE_B [--force] [--json] [--root R]
```

## Running on a kit

Identity is an opt-in, exploratory Somalier measurement for full aligned hg38
inputs with at least 10 million eligible reads. An operator must first register
the exact local gzip VCF of hg38 fingerprint sites and its independently
obtained SHA-256 using `lab identity sites add`. The file is copied into the
Lab ROOT and checked against its pinned bytes each time it is used. Install
Somalier separately, then run `traceback lab run ID12 --only identity --root R`
for each of two chosen samples. `lab identity compare A B` relates only those
two current, selected fingerprints; `--force` stores a new immutable attempt.
The result is bound to the source, input, selected extraction attempt, sites,
reference, tool version, and upstream read summary. When any of those change,
rerun identity and the comparison. The local Trust/QC view shows current pairs.
Fingerprints and relatedness can reveal family relationships (FLAG: PRIVACY).
Keep the Lab ROOT private. There is no validated ONT duplicate or kinship
threshold here, and this command makes no diagnostic claim. No real donor data
is required for the synthetic checks in this repository.
An altered or unreadable sites registration is blocked, not silently replaced.
Preserve the affected ROOT for review; the safe repeat path is a fresh ROOT,
the verified original VCF and SHA-256, and new extraction/comparison runs.
Any in-place repair requires an offline backup and operator review first.

`--sex M|F` records an operator assertion in versioned local sample metadata.
It does not change the measured `sex-check` ratio, source or input identity, or
cached results. Recording a new assertion advances the local page's change counter,
including on a fully cached run; repeating the same assertion does not. Trust/QC
compares a definite male/female call with the assertion
at read time as a pre-analytic check. An absent assertion or indeterminate call
has no mismatch verdict. The page marks the check as privacy-sensitive and does
not present it as a diagnostic sex or gender determination. A later conflicting
assertion is refused, not silently substituted. If the stored assertion metadata
is malformed, measurements remain visible, while Trust/QC shows an explicit
no-verdict repair notice (TBX-LAB-023). Reruns without `--sex` preserve that
metadata and can continue; an explicit `--sex` write is refused until repaired.

`traceback lab run <chunk dir>` takes one file, several files or a directory
of MinKNOW chunks (`*.bam`, not recursive). It:

1. refuses symlinks, non-regular and unreadable files, truncated chunks
   (`samtools quickcheck -u`), duplicate chunks and mixed basecall models;
2. orders chunks by the `_<n>.bam` index, hashes them (cached by path, size
   and mtime) and derives the sample identity (`id12`, the first 12+ hex of
   the chunk-set digest);
3. checks free space against the disk budget (about 2.2x the chunk bytes,
   plus the minimap2 index the first time, plus 2 GB);
4. aligns unaligned chunks in one process group:
   `samtools cat | samtools fastq -T MM,ML,MN | minimap2 -ax map-ont -y -R <@RG> | samtools sort --write-index`,
   keeping the `@RG` line (so the modified-base model is still declared) and
   the MM/ML/MN tags;
5. checks that every uBAM record came back (primary + unmapped), then
   publishes the aligned BAM and its index together;
6. runs every registered algorithm, each in its own process group with a
   timeout (`--timeout-s`, default 3600 s);
7. by default, reruns the summary algorithms and copy-number on nested
   downsamples (0.5, 0.25, 0.1 of the reads) for the depth curve, and runs
   the positive controls (synthetic CNA spike, split halves). These are row
   masks over the one read summary plus small counting BAMs, never copies
   of the BAM. `--no-downsample` and `--no-controls` turn them off.

Alignment needs samtools ≥ 1.20 and minimap2 ≥ 2.28 (`brew install samtools
minimap2`). The minimap2 index of the reference is built once under
`ROOT/lab/refs/` (hg38: about 20–40 minutes and 8 GB); `--mmi PATH` reuses one
you already have. The measured alignment speed is 2.8–5.5 minutes per GB of
uBAM.

The reference is the one registered under ROOT (`traceback reference
register`); with several, pass `--reference ID`.

**Reruns.** Rerunning the same command recomputes missing or failed results
and a dependent result when a selected upstream attempt has changed;
unchanged chunks are not re-hashed and the alignment is reused.
`traceback lab run latest` or `traceback lab run <id12>` reruns on the stored
sample without the chunk paths. `--force` recomputes every result.
`--only a,b` runs just those algorithms (overriding `--profile`), plus the
intermediates they need, labelled "(upstream)".

A result is reused only when its selected attempt matches the input, algorithm,
and execution identity. Dependents also record the exact upstream attempts
they consumed, including a parent-input summary reused by a derived control.
Successful attempts record the effective execution-identity inputs in internal
metadata. Cohort reads verify this record against the selected result, input,
reference, upstream identities and consumed upstream attempts, then compare
using the producer's recorded tools and parameters. They do not probe the
reader's numerical runtime to reconstruct a historical result.
An older dependent attempt without that record is recomputed once; leaf results
remain cacheable. New attempts record artifact file sizes; older attempts
remain readable, with known upstream layouts checked against their metadata.
Missing or truncated required artifacts are recomputed. A completed failed
forced rerun is shown as failed; the prior successful attempt remains on disk.
If the current upstream step fails or skips, its dependent step also skips;
an older successful dependent result is not reused for that run.
Artifact sizes check completeness, not byte-level integrity.

**Dry run.** `--dry-run` writes nothing and takes no lock. It prints the
resolved ROOT and reference, chunk count and bytes, whether alignment is
needed or reused, the disk budget against free space and the plan.

**Interruption.** Ctrl-C (or SIGTERM) stops the current step, kills its tools
and exits 6. Completed results are kept; rerun the same command.

**Output.** The run ends with a summary block (`ROOT`, `RUN`, `SAMPLE`,
`DONE`, any `FAILED` lines, `NEXT`). With `--json`, stdout is one
`traceback.cli-result.v2` object and progress goes to stderr. `--verbose`
streams each tool's raw stderr to your terminal; only scrubbed stderr (paths,
ROOT, home, login and chunk names redacted) is ever stored.

Each executed algorithm reports the direct worker's `wait4` peak RSS,
the sampled peak sum of live worker-group and descendant RSS, and sampled peak
scratch-disk bytes separately in its step result and run log. The worker's
`wait4` value can include descendants it waited for; the sampled sum can count
shared pages more than once. Either memory number may be larger. Disk includes
temporary worker files under that step's scratch directory. The disk and
process-tree numbers are sampled observations, not resource ceilings: sampling
can miss brief peaks. A short step may have no tree sample. A disk scan that
cannot finish within the bounded reporting time leaves only earlier observations,
if any. Step duration ends when direct workers are reaped; group cleanup and
telemetry reporting can continue afterward. Cached and skipped steps have no
new measurement.

## Algorithms

`traceback lab algorithms` lists what is registered. The slice ships:

| id | What it reports |
|---|---|
| `read-summary` | intermediate: one pass over the aligned BAM into memmapped columns (no read names); every pysam algorithm and every downsample or split-half view reads it |
| `bam-qc` | primaries, mapped and MAPQ ≥ 20 shares, supplementary share, mean read Q, share of sampled genome covered ≥ 1x, GC-bias slope, duplicate share (only when marked); supplementary share as a chimera proxy and mean read Q interpretation are ONT-specific |
| `depth` | eligible reads (primary, MAPQ ≥ 20, chr1–22/X/Y), mean aligned span per eligible read, approximate aligned-base depth; the index count is shown only as an upper bound, and Illumina aligned span is not template length (`abs(TLEN)`) |
| `fragment-size` | 1-bp aligned-span histogram, mode, short (< 150 bp) share, mono/di peaks, 10 bp periodicity |
| `lionheart` | opt-in upstream LIONHEART v2.2.0 extraction of 898 raw sample-standardized chromatin-accessibility correlation scores from the selected hg38 BAM; needs ≥5M eligible reads, the modified mosdepth and Zenodo v003 resources; ONT single-end mode is experimental, and no cancer prediction is run |
| `short-read-fragment-size` | separate exploratory paired-end Illumina histogram and size summaries from absolute BAM TLEN; requires a prealigned BAM with every `@RG PL:ILLUMINA` |
| `copy-number` | tumour fraction, ploidy, genome log2 track and segments through the installed ichorCNA toolchain (no panel of normals yet, so read as not interpretable); needs 2M eligible reads, `traceback toolchain install ichor` and `traceback method-asset register --from-toolchain copy-number --root R` |
| `instability` | bin-weighted median absolute deviation of the selected copy-number result's segment log2 values (`tmad`), with used-bin and null-estimate coverage counts; needs 2M eligible reads and a successful copy-number result for the same input; exploratory and cohort-relative, with no ONT cutoff |
| `modbase-qc` | mod codes in the MM tags, MM-tagged share, mean ML at aligned CpG calls (read from `meth-calls`), share of those calls below 0.7 confidence, tagged reads with unusable MM/ML; skipped (`missing_tags`) when no read carries MM |
| `long-tail` | shares of molecules > 300 bp, 1–4 kb (delayed processing) and > 7.5 kb (lysis); a supplementary-split read counts at its read length |
| `sex-check` | chrY/chrX read density per Mb of non-N, non-PAR reference and a male / female / indeterminate call; needs 500,000 eligible reads |
| `mtdna` | chrM reads (MAPQ ≥ 20) as a share of all primary reads, and the chrM fragment mode |
| `viral-reads` | EBV, HPV and HBV reads per million human-unmapped primary reads from a separate minimap2 ≥ 2.28 alignment; needs an operator-registered viral FASTA and is exploratory for ONT cfDNA |
| `cell-origin` | share of cfDNA from each of 40 Loyfer U250 cell types with bootstrap intervals, through the signed method's own `compute_measurement` (modkit, UXM, NNLS); needs 2.5M eligible reads, `traceback toolchain install modkit` and `traceback method-asset register --from-dir LOYFER_DIR --root R`; minor cell types are not interpretable below 5x. Downsample and split runs read a transient filtered BAM that is deleted after publish |
| `end-motifs` | reference 4-mers at both ends of each molecule (5′ forward end and the reverse complement of the other end), motif diversity score and CCCA share; needs 500,000 eligible reads and the registered FASTA |
| `griffin` | pinned Griffin GC-corrected TF-site nucleosome profile on a registered hg38 site list; needs an Illumina-declared paired-end BAM, at least 5 million eligible autosomal pairs, the registered reference assets, Snakemake 5.19.2 importable by the Lab Python interpreter, and bedtools, samtools, pyBigWig and pybedtools. Native ONT and derived inputs skip; site profiles are descriptive, not a tissue-origin or clinical call |
| `delfi-ratio` | short (100–150 bp) / long (151–220 bp) ratio per 5 Mb bin on chr1–22, median-centred, and its SD; no GC correction, cohort-relative only; needs 1,000,000 eligible reads |
| `meth-calls` | intermediate: decodes MM/ML once into site-level CpG call chunks (read row, forward-strand C position, strand, 5mC/5hmC/canonical ML); derived inputs reuse it through their row mask |
| `global-5mc` | share of CpG site calls whose most likely state is 5mC |
| `global-5hmc` | share of CpG site calls (with a 5hmC channel) whose most likely state is 5hmC; skipped (`missing_channel`) when the MM tags carry no `C+h` |
| `pmd-score` | 5mC share inside vs outside the Zhou 2018 common PMDs and their difference; needs 500,000 eligible reads and the PMD asset (below) |
| `hypometh-reads` | among reads with ≥ 4 CpG calls, the share under 25% methylated, and U/X/M counts (≤ 25% / between / ≥ 75%); needs 500,000 eligible reads |
| `frag-x-meth` | descriptive molecule-count matrix of aligned-size bins by per-read 5mC fraction (at least four distinct CpG sites/read); the scalar is long (151–220 bp) mean minus short (100–150 bp) mean, null if either group is empty; no normal range |
| `breakpoint-motifs` | 4-mer counts at aligned-reference-span start and end−1, stratified by CpG proximity; the scalar is the fraction of ends near a CpG. These are not proven physical cleavage positions, and the methylation link is unavailable without a supported joint input |

**Experimental LIONHEART scores.** The [upstream v2.2.0 source](https://github.com/BesenbacherLab/lionheart/tree/v2.2.0)
is pinned at commit `f49de07ce0d9a72ee3c883429085cfacfc54c1d2` (MIT code).
Its `extract_features` command needs the
[modified mosdepth fork](https://github.com/LudvigOlsen/mosdepth/tree/f61263176b62677dc0b7ad4692c53b8ca2b8365c)
with `--single-end-length`. Download the 2.3 GB
[v003 resource archive](https://zenodo.org/records/15747531) through an
approved local process and verify MD5 `7ab368995ad5123ab665042e6ecc4410`.
Keep both the archive and its extracted `inference_resources_v003` directory
under `ROOT/lab/assets/lionheart/` (or set the paths below). The archive is
checked against that published MD5, every extracted file is compared with the
archive, and the verified resource bytes enter the result identity. Extraction
uses a separate verified scratch copy, so a changed installed resource cannot
silently enter a result. The CLI, its installed Python package code, and the
modified mosdepth binary are hashed into the result identity; the installed
package must report version 2.2.0. The selected mosdepth library directory
must contain `libhts.so`; its loaded bytes and the `samtools` and `unpigz`
executables are also pinned. Neither the pack nor scores are bundled in this repo;
Zenodo says its shared scores and models derive from non-commercial,
research-purpose-only data.

```bash
export TRACEBACK_LIONHEART_BIN=/path/to/pinned-lionheart-2.2.0/bin/lionheart
export TRACEBACK_LIONHEART_MOSDEPTH_BIN=/path/to/modified-mosdepth
export TRACEBACK_LIONHEART_LD_LIBRARY_PATH=/path/to/mosdepth-library-directory
export TRACEBACK_LIONHEART_ARCHIVE=ROOT/lab/assets/lionheart/inference_resources_v003.tar.gz
export TRACEBACK_LIONHEART_RESOURCES=ROOT/lab/assets/lionheart/inference_resources_v003
export TRACEBACK_LIONHEART_ENABLE_ONT=1
traceback lab run SAMPLE_ID12 --only lionheart --timeout-s 7200 --root ROOT
```

The binary's `--version` must report Lionheart 2.2.0; the modified mosdepth must expose
`--single-end-length`. Install `samtools` and `unpigz` on `PATH` for upstream's
BAM validation and coverage decompression. The library-directory setting supplies
upstream's required `--ld_library_path` argument; it defaults to the mosdepth
binary's directory when unset.
Set it to the directory containing mosdepth's shared libraries when needed.
Without the explicit ONT opt-in, or when a dependency,
resource or hg38 input is missing, this algorithm skips with a reason and
leaves other Lab results available. Signals shows the 898 raw scores with
their ATAC/DNase labels and a research-only callout. The upstream single-end
mode is experimental and its paired-end-trained cancer model is **not** run;
these values are neither cell fractions nor a validated diagnostic.

**Short-read support.** For a prealigned paired-end Illumina BAM against the
registered hg38 reference, run the normal Lab command:

```bash
traceback lab run YOUR_LOCAL_aligned_illumina.bam \
  --reference hg38 --root R
```

Every BAM read group must declare `PL:ILLUMINA`. This declaration is recorded
in the input manifest and run summary; it describes the BAM header, not
instrument attestation. A prealigned BAM with missing, mixed, or unrecognized
platform declarations is refused with TBX-LAB-024. Unaligned input must
unanimously declare `PL:ONT` to use the Lab's
`map-ont` alignment path. For short reads, align externally and supply a
prealigned BAM. Each qualifying proper first mate counts one
template: it must pass the common primary, mapped, nonduplicate, QC-pass,
MAPQ ≥ 20 and canonical nuclear contig filter (names with or without `chr`),
have a mapped mate on the same contig,
and carry nonzero TLEN. The reported length is `abs(TLEN)`, even when the
first mate's TLEN is negative. Downsample and split selections use the same
read-name predicate as other Lab full-BAM algorithms. The histogram and
metrics are keyed to `short-read-fragment-size`; `fragment-size` remains the
ONT aligned-span measurement. The normal Illumina run also reports read
summary, aligned-base depth, and BAM QC, including downsample and split views.
Split halves are read-name pseudo-replicates, not independent molecules. On
Illumina, unmarked PCR duplicates can place copies of one molecule in both
halves and make agreement look stronger; inspect duplicate marking and do not
treat split agreement as independent validation.
ONT-only algorithms skip with reason `platform`; the CNA spike is omitted for
Illumina. Result and cohort compatibility identities distinguish platforms
while retaining the historical ONT identity encoding, so an Illumina
measurement cannot reuse an ONT result or reference range. No clinical
cutoff, short-read healthy range, or cross-platform comparison is supplied.
If an Illumina BAM was ingested by the earlier opt-in TLEN path, run it again
from the original BAM to create its platform-specific input identity. The old
stored result remains available but does not enter new platform-aware cohorts.
For a legacy prealigned BAM without a platform declaration, verify its
instrument provenance and regenerate a BAM with the correct `@RG PL` value
from the trusted source before ingesting it. The Lab does not infer ONT from
an absent value. Previously stored Lab-aligned inputs with absent or
unrecognized platform tags remain readable only when their manifest exactly
pins the historical `map-ont` recipe for the BAM's read groups and no read
group explicitly declares Illumina. A new input still needs an explicit
platform declaration.
Stored prealigned manifests without a platform field still read the BAM's
unanimous platform declaration. Re-ingest short reads to record an Illumina
input identity for new results and cohorts. Before rerun, historical ONT-only
results on such an Illumina input remain stored but appear as platform skips
in sample views and the local API; rerun records those skips in the store.
The same read-side mask applies when an old prealigned input cannot verify ONT
provenance from its BAM.

**PMD asset.** `pmd-score` needs an operator-supplied Zhou 2018 hg38
common-PMD BED.gz file. The repository does not ship or download it; its
redistribution terms remain unknown. Obtain the file through your approved
local process, then register its exact bytes and provenance under the same
ROOT as the run:

```bash
traceback lab asset add pmd --file YOUR_LOCAL_PMD_coordinates_hg38.bed.gz \
  --source-label Zhou-2018-operator-supplied --terms unknown --root R
traceback lab asset inspect pmd --root R
```

`inspect` rehashes the pinned file; absent or altered bytes block
`pmd-score` with an asset-specific recovery command. Registering a PMD
asset does not establish redistribution rights or make a skipped result zero.

**Viral reference.** `viral-reads` leaves the registered hg38 human reference
unchanged. It takes human-unmapped primary reads from the aligned BAM, including
duplicate-flagged and QC-fail primaries but excluding secondary and
supplementary records, then runs
minimap2 separately against an operator-supplied viral FASTA, and reports
EBV/HPV/HBV aligned reads per million human-unmapped primaries. The count is
null if that denominator is zero. A read with reported primary or secondary
alignments to more than one virus group is excluded from all groups; multiple
primary alignments within one group count once. The side alignment explicitly
retains up to 100 secondary alignments per read at minimap2's 0.8 score ratio;
bundles with many near-identical contigs can still hide cross-group alternatives.
If any human-unmapped primary lacks stored sequence, the anonymized query
FASTA exceeds its 512 MiB staging budget, or the side-alignment process exits
nonzero or exceeds its 3600-second or 1 GiB PAF budget, all three RPM values
are null; the aggregate
artifact records the reason and read counts, with `hits: null` when no complete
alignment was counted. Donor sequences are staged in private temporary FASTA
and removed after the step; an abrupt host failure may leave that scratch for
operator cleanup. There is no MAPQ or aligned-length threshold, so
weak or short matches can contribute. No detection threshold or clinical
interpretation is established for this ONT cfDNA measurement; inspect the
denominator `n` before using an exploratory RPM value. Malformed PAF or an
unexpected query/contig is an integrity failure and stops the algorithm rather
than publishing a possibly wrong count.

The FASTA must contain at least one nonempty IUPAC sequence for each group.
Provide a TSV with exactly `contig`, `virus`, `accession` columns, one row per
FASTA contig in FASTA order; `virus` must be `EBV`, `HPV` or `HBV` and the
accession should include its version. Registration pins the exact bytes of
both files and the declared source terms. It neither downloads nor
redistributes the sequences. Registrations are write-once under a Lab ROOT;
to change accession coverage or recover a corrupted registration, create a new
ROOT and register the corrected bundle there:

```bash
traceback lab asset add viral-fasta --file YOUR_LOCAL_viral.fasta \
  --targets YOUR_LOCAL_targets.tsv --source-label your-reference-bundle \
  --terms unknown --root R
traceback lab asset inspect viral-fasta --root R
```

Choose accession versions and HPV type coverage for the intended evaluation
before registering a bundle. Until one is registered under ROOT, the
algorithm reports an explicit `missing_asset` skip. Registering a FASTA
does not validate sensitivity, specificity, a reference choice, or rights
to redistribute source sequences.


Fragment length is the aligned reference span, the same definition as the
signed fragment metric; the Lab's 1-bp counter is parity-tested against it.

## Opening the page

`traceback lab serve [--open] [--root R]` starts a server on 127.0.0.1 only
and prints ROOT, then a one-use link
(`http://127.0.0.1:<port>/#bootstrap=<code>`). Open it within 60 seconds on
this machine; `--open` opens it for you. Press Enter for a fresh link;
Ctrl-C stops the server. Pass the same `--root` (or `TRACEBACK_ROOT`) you
used for `lab run`; on an empty ROOT the server says so.

The server never takes the writer lock, so a `lab run` can proceed while the
page is open: the page polls every 15 seconds and shows "A Lab run is in
progress", then "New results are available. [Reload]" (it never swaps
content under you). The page is strict: same-origin scripts only, no inline
script or style, no outside requests.

The run-status banner follows the latest Lab writer; a later cohort update or
other writer can supersede an earlier interrupted-run banner. It does not
serve as a history of every interrupted run.

## What the page shows

- **Landing (`#/`)**: the CLIA explainer (short version, "Read the full
  explainer" expands in place, full text at `#/about/clia`), one computed
  headline sentence, the **defended results** (headline metrics that are
  usable at half depth, stable across split halves, PROVEN or NEWER,
  citation-checked and, for copy-number, whose synthetic spike at TF 0.10 was
  recovered), the **depth → utility curve** (one row per algorithm; cells at
  10/25/50/100% of the reads; literature minimums drawn as "literature, not
  measured"), the **positive controls** (SYNTHETIC, REPEATABILITY,
  KNOWN-STATUS), the samples table and the license callouts.
- **Findings**: only above/below-reference results are cards (sorted by tier,
  then severity); not-interpretable, no-reference and in-range metrics are
  counted, collapsible rows. Before the kit cohort exists (n < 10) most
  metrics read "no reference".
- **Compare**: choose a published Lab cohort generation and read finite scalar
  metrics against compatible, status-ok members. This is an exploratory
  selected-cohort distribution, not a healthy or clinical reference. The
  original library reference remains visible and unchanged. The page names the immutable version,
  target aligner, eligible and excluded counts and aligners. If the target is a
  member, its own sample is excluded before deciding whether n is sufficient.
  At n < 10 there is no reference; n = 10–29 shows an exploratory min–max
  without percentile or confidence-interval claims; n ≥ 30 enables the
  interpretation library's percentile and interval. A provisional external
  generation is labelled as such. Missing or failed metrics are never zero.
  The synthetic EXAMPLE cannot be scored against a real kit cohort.
- **Trust/QC**: depth against each algorithm's minimum, the run table with
  scrubbed stderr for failures and a link to a matching numbered run-log line,
  provisional QC bands, library health. The link searches up to 64 recently updated
  runs and reveals only allowlisted status fields. A line must name the selected
  immutable result attempt; legacy logs without that ID, older runs, or cached
  steps may have no matching line. A latest-run in-progress or interrupted note
  appears only when that run names the same source and full input; the selected
  result status remains visible beside it. If the step published no result yet,
  a sample-scoped run notice appears instead of a result row. If the latest
  analysis or simulation run log is unreadable, unsafe, malformed, or outside
  the 1 MiB / 4,096-line limit, the status banner reports an unavailable
  status without inferring whether the run ended normally or was interrupted.
- **Stability**: one small multiple per metric: deviation from full depth at
  each fraction, the stability band, and split halves A/B at 50%.

## Building a Lab cohort

Use the same `--root R` as the source runs. The sample ID12 is printed by
`lab run` and listed on the Lab landing page. Only stored, full-input,
non-EXAMPLE samples can be added. A repeat add of the same source is
idempotent; rerunning an analysis does not create a new member.

```bash
traceback lab cohort add ID12 --cohort healthy --root R
traceback lab cohort build --cohort healthy --root R
traceback lab serve --root R --open
```

`build` publishes an immutable generation and changes the current pointer
atomically. Open **Compare** and choose that cohort; the page names the exact
generation used for the whole response. Build again after a member or its
results change. Different aligners or incompatible algorithm parameters are
excluded per metric, with counts shown. Pre-aligned inputs whose processing
provenance is unknown are isolated by their input identity; they are not
pooled as protocol-matched references. Distinct source IDs are not proof of
independent donors. The separate PoN denominator does not become a
percentile denominator.

If a selected cohort cannot be verified, **Compare** lists it as unavailable
with a problem code and a next action. Other verified generations remain
selectable. Follow that action, then reload the page; an unavailable cohort or
target sample has no comparison values, and the Lab does not fall back to an
older generation. A cohort with members but no published generation needs
`traceback lab cohort build --cohort ID --root R` before it can be selected.

## Positive controls

`traceback lab simulate cna <id12|latest>` builds a synthetic copy-number
spike from the healthy aligned input, with no external data: one-copy gains
on 1q and 8q and losses on 13q and 17p at tumour fraction TF (default 0.03,
0.10 and 0.30). Each read in a gain arm is duplicated with probability TF/2
and each read in a loss arm removed with probability TF/2, drawn from a
salted hash of the read name, so the spike is deterministic. The spike is a
stripped counting BAM (positions only, about 1-2% of the aligned BAM).
Copy-number then runs on each, and the output shows expected against
recovered TF (recovered means within ±0.05).

`traceback lab simulate split <id12|latest>` splits the reads into halves A
and B by a second salted hash and runs every summary algorithm and
copy-number on each. It measures sampling noise only, not a known answer.

Both take the writer lock and rerun only what is missing.

## Exit codes

| Code | Meaning |
|---:|---|
| 0 | OK (skipped algorithms are fine) |
| 2 | Usage error |
| 3 | Blocked (each cause has its own `TBX-LAB-*` code below) |
| 4 | Not found |
| 6 | Interrupted or retryable; rerun the same command |
| 7 | Internal error |
| 8 | The run completed but one or more algorithms failed |

## Troubleshooting

Every refusal prints `CODE`, `CAUSE`, `FIX`, `RETRYABLE` and `DOCS` (the
anchor below); `--json` carries the same fields under `data`.

### TBX-LAB-001

Unknown algorithm id in `--only` (exit 2). The fix line suggests the closest
id. `traceback lab algorithms` lists every id.

### TBX-LAB-002

A `--tf` (or `--downsample`) value is not a comma list of fractions with
0 < f < 1 (exit 2). Example: `--tf 0.03,0.10,0.30`.

### TBX-LAB-003

samtools or minimap2 is missing or older than 1.20 / 2.28, and the input is unaligned (exit 3). `brew install samtools minimap2` (or `brew upgrade …`), then `traceback lab doctor`. An already-aligned BAM needs neither.

### TBX-LAB-004

No reference is registered (exit 3), or several are and `--reference` was not given (exit 2, listing the IDs). Register one with `traceback reference register --fasta hg38.fa --id hg38 --root R`, or add `--reference ID`. The `lab-example` reference is never picked by default for real input.

### TBX-LAB-005

An input is a symlink, not a regular file, missing or unreadable (exit 3). The data names the argument position and kind. Pass the real file (`realpath`) or fix its permissions.

### TBX-LAB-006

A chunk fails `samtools quickcheck -u`: it is truncated (exit 3). Re-download the named chunk indexes, or rerun with `--skip-bad-chunks`; the excluded indexes are recorded and the results are marked provisional.

### TBX-LAB-007

Two chunks have identical content, or one chunk index appears twice with different content (exit 3). Remove the duplicate download.

### TBX-LAB-008

Chunks declare different `@RG` basecall models (exit 3). Run each model's chunks as a separate sample.

### TBX-LAB-009

An aligned BAM's `@SQ` lines differ from the registered reference, the inputs mix aligned and unaligned BAMs, or more than one aligned BAM is supplied (exit 3). The cause lists the first three reference differences when applicable. Pass the unaligned chunks and let the Lab align, or register the FASTA the BAM was aligned to; supply one prealigned BAM at a time.

### TBX-LAB-010

Not enough free space for the step (exit 3, retryable). The data carries the step, the required bytes by part (aligned BAM, sort temp, minimap2 index, margin) and the free bytes. Free space, or use `--root` on a larger volume; `--dry-run` shows the budget first.

### TBX-LAB-011

The volume filled up during a step (exit 3, retryable). The staging directory was removed. Free space as for TBX-LAB-010, then rerun.

### TBX-LAB-012

Another Lab writer is running (exit 3, retryable). Only one writer (`lab run`,
`lab simulate`, `lab cohort add`, `lab cohort build`) may hold `ROOT/lab/.lock`
at a time. The cause names the holder's command, run id, pid, step and start
time. Wait for it (`traceback lab runs`), or stop that process.

### TBX-LAB-013

After alignment, the uBAM record total did not equal the aligned primary-plus-unmapped count (exit 3). Nothing was published. Not retryable as-is: run `traceback lab runs RUN_ID --step align`, then report.

### TBX-LAB-014

An alignment process failed or timed out (exit 3, retryable). The cause names the process and its first scrubbed stderr line. Rerun with `--verbose` to see live stderr; check `traceback lab doctor`.

### TBX-LAB-015

The run was interrupted by Ctrl-C or SIGTERM (exit 6, retryable). Completed results are kept. Rerun the same command; only missing results recompute.

### TBX-LAB-016

The run completed and one or more algorithms failed (exit 8). The summary's `FAILED` lines name them with their first scrubbed stderr line. `traceback lab runs RUN_ID --step ALG` shows the step; a rerun recomputes only the failed results.

### TBX-LAB-017

A run id, sample `id12` or `latest` was not found, or a prefix is ambiguous
(exit 4). `traceback lab runs` lists runs. Pass the same `--root` (or
`TRACEBACK_ROOT`) you ran with.

### TBX-LAB-018

`ROOT/lab/STORE_VERSION` is newer than this checkout (exit 3). Update the
checkout (`git pull && uv sync`). The store is never downgraded.

### TBX-LAB-019

A warning (exit 0): the samtools or minimap2 version changed since this sample was last aligned, so it is realigned under a new input identity (results from the old alignment are never reused). `--dry-run` shows it first. Pin the tools to avoid it.

### TBX-LAB-020

Unexpected internal error (exit 7). Run `traceback lab runs RUN_ID`, then
report the code and the command. For `cohort add` or `cohort build`, this code
also identifies a missing, malformed or unsafe stored source/input record.
Restore the named regular JSON file from a known-good backup before retrying;
the cohort's published generation is left unchanged.

### TBX-LAB-021

An EXAMPLE synthetic sample was offered to `lab cohort add` (exit 3). Add
a stored real kit sample instead. EXAMPLE results remain labelled synthetic
and are not scored against a real cohort in Compare.

### TBX-LAB-022

Stored cohort metadata or an immutable generation is missing or malformed
(exit 3). The reported cohort and field identify what to inspect. Restore the
matching file from a known-good backup, then retry. The Lab does not replace
the current pointer or repair a generation automatically. The backend listing
reports each affected cohort separately so callers can retain intact cohorts.
Finder's `.DS_Store` file is ignored; other unexpected generation entries
must be inspected and moved aside manually after preserving them. If only the
current pointer is missing and the member roster is intact, `lab cohort build`
publishes a fresh generation from that roster while retaining earlier
generation directories.

### TBX-LAB-023

An operator-supplied `--sex` conflicts with an assertion already recorded for
the same sample, or the local sex metadata is malformed (exit 3). Reuse the
recorded assertion or omit the flag. Inspect and restore malformed metadata
from a known-good local backup before rerunning; no measured result is changed.

### TBX-LAB-024

An input has missing, mixed, or unrecognized platform declarations, an
unaligned Illumina input would need the ONT-only aligner, or a stored input's
platform disagrees with its BAM header (exit 3). Use a BAM whose read groups
unanimously declare `PL:ONT` or `PL:ILLUMINA`; align Illumina reads externally
before supplying the BAM. For a damaged or mismatched stored input, restore
that input from a known-good backup or re-ingest the original BAM under a fresh
ROOT. Reusing the same ROOT does not replace an existing input manifest.
Rewriting a BAM header changes its source identity; remove an unrecoverable
member from its cohort before rebuilding that cohort.

## Adding an algorithm

1. Add a module under `traceback_lab/algorithms/` exposing an instance of the
   plug-in Protocol (`id`, `version`, `family`, `requires`, `consumes`,
   `cost`, `parameters`, `tools`, `upstream`, `preflight`, `run`). Declare
   measurement identity only: tier, footnote, license, reference and next
   steps belong in the library.
2. Add one line to `ALGORITHMS` in `traceback_lab/registry.py` (registry
   order is execution order; upstream entries come first).
3. Add `traceback_lab/interpretation/library.yaml` entries for every metric,
   with exactly one `headline: true` and a citation check (or the entry
   shows UNVERIFIED).
4. Add tests under `tests/lab/` on synthetic fixtures. No other shared file
   changes.

## Changes

- 2026-10-15: first slice: `lab run`, `lab runs`, `lab algorithms`,
  `lab simulate cna|split`, `lab serve`, `lab doctor`, `lab run --example`.
