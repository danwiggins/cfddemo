# Traceback Lab guide

Traceback Lab attempts its registered cfDNA algorithms from one run command;
each method either reports a result or a specific skip or failure when the
input, platform, depth, tools, or assets do not qualify. A local page explains
the available results. Lab is a separate, **unsigned** subsystem: its results
carry execution provenance and immutable attempts, not a development
signature. The signed `traceback run` path has its own methods and records.

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
4. `traceback lab doctor --root R` shows setup checks and recovery commands;
   inspect readiness for the methods you intend to run. Other methods have
   additional optional tools, assets, or input-scoped calibrations below.

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
    [--reference ID] [--profile all|fast] [--only a,b] [--cohort ID]
    [--downsample 0.5,0.25,0.1 | --no-downsample]
    [--controls | --no-controls] [--tf 0.03,0.10,0.30]
    [--skip-bad-chunks] [--sex M|F] [--force] [--timeout-s 3600]
    [--threads N] [--sort-mem 1G] [--mmi PATH]
    [--dry-run] [--verbose] [--json] [--root R]
traceback lab simulate cna <id12|latest> [--tf 0.03,0.10,0.30] [--cohort ID] [--verbose]   synthetic CNA spike, copy-number recovery
traceback lab simulate split <id12|latest> [--cohort ID] [--verbose]                         split-half pseudo-replicate
traceback lab algorithms [--json]          registered algorithms (id, tier, min depth, licenses, footnote)
traceback lab doctor [--json]              setup checks, each with a fix command
traceback lab serve [--open]               the local page (loopback only)
traceback lab runs [RUN_ID] [--step S]     the run log, or one run's steps
traceback lab identity sites add --file SITES.vcf.gz --sha256 SHA256 --source-label LABEL [--root R]
traceback lab identity sites inspect [--root R]
traceback lab identity compare SAMPLE_A SAMPLE_B [--force] [--json] [--root R]
traceback lab cohort add|remove ID12 --cohort ID [--root R]
traceback lab cohort build --cohort ID [--root R]
traceback lab arm-zscores ID12 --cohort ID [--root R]
traceback lab asset add|inspect KIND [--root R]
traceback lab asset register-clock-map|inspect-clock-map [options] [--root R]
traceback lab asset griffin-register|griffin-inspect [options] [--root R]
traceback lab asset end-catalogues-register|end-catalogues-inspect [options] [--root R]
traceback lab microbial db add|inspect [options] [--root R]
traceback lab fetal register|inspect [options] [--root R]
traceback lab fetal chry-register|chry-inspect [options] [--root R]
traceback lab donor register|inspect [options] [--root R]
```

The registration lines are a map, not complete copy-paste syntax. Their
input files and required flags are specified in the method sections and
linked guides below; `traceback lab COMMAND --help` shows exact options.

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
Its concordance value is Somalier's `hom_concordance` column, not a general
sequence-concordance estimate.
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
cached results. Recording a new assertion advances the local page's change
counter, including on a fully cached run; repeating the same assertion does
not. Trust/QC compares a definite male/female call with the assertion at read
time as a pre-analytic check. An absent assertion, unavailable measured call
(including a skipped or failed check), and a measured indeterminate call have
distinct no-verdict explanations. The page marks the check as privacy-sensitive
and does not present it as a diagnostic sex or gender determination. A conflicting
assertion is refused, not silently substituted. If the stored assertion metadata
is malformed, measurements remain visible, while Trust/QC shows an explicit
no-verdict repair notice (TBX-LAB-023). Reruns without `--sex` preserve that
metadata and can continue; an explicit `--sex` write is refused until repaired.
The assertion timestamp must be a valid UTC second, in `Z` or `+00:00` form.

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
needed or reused, the ingest/alignment disk budget against free space and the
plan. For a stored sample, a selected read-summary can show eligible depth and
minimum-depth gates; cached reuse and other adapter checks remain decisions for
the run.
The stored-sample zero budget covers reused ingest and alignment only; it is
not a sufficiency check for analysis scratch. Nonempty excluded-chunk lists
appear in both human and JSON output. With `--force`, a prior read-summary does not supply
depth predictions for the new run.
With an explicit `--cohort`, it also checks that a built internal panel exists.
Protocol and asset compatibility are checked during the run, after alignment
when alignment is needed; the preview labels that decision as deferred.
When detected physical RAM is below 16 GiB, both dry run and execution show
an advisory warning in human and JSON output. This does not block a run; a
full hg38 alignment may be slow or fail for lack of memory.

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

`traceback lab algorithms` is the live catalog of registered IDs, versions,
tiers, license notes, and minimum depths. This table explains their outputs.
Run a subset with `--only ID[,ID...]`; a listed method can still skip for a
particular input. For a scientist-friendly explanation of what each measurement
may reveal, including epigenetic age, read the
[Lab algorithm explainer](LAB-ALGORITHM-EXPLAINER.md).

- **Both supported platforms:** read summary, depth, BAM QC, and selected
  common counters run where their input requirements permit. The ONT
  `fragment-size` aligned-span measurement and Illumina
  `short-read-fragment-size` TLEN measurement have different denominators.
- **ONT and modified-base methods:** methylation, cell-origin, end patterns,
  WPS, and exploratory tumour or microbial measures often need extra depth,
  pinned references, software, or an operator-provided model. Missing inputs
  are reported as unavailable, not zero.
- **Illumina-specific checks:** Griffin and the FinaleToolkit cross-check
  require a declared paired-end Illumina input. Neither validates equivalence
  to an ONT result.
- **Input-scoped calibrations:** prenatal and transplant fraction methods
  require separate operator assertions and matched controls. Their software
  gates do not establish clinical accuracy.

| id | What it reports |
|---|---|
| `read-summary` | intermediate: one pass over the aligned BAM into memmapped columns (no read names); every pysam algorithm and every downsample or split-half view reads it |
| `bam-qc` | primaries, mapped and MAPQ ≥ 20 shares, supplementary share, mean read Q, share of sampled genome covered ≥ 1x, GC-bias slope, duplicate share (only when marked); supplementary share as a chimera proxy and mean read Q interpretation are ONT-specific |
| `depth` | eligible reads (primary, MAPQ ≥ 20, chr1–22/X/Y), mean aligned span per eligible read, approximate aligned-base depth; the index count is shown only as an upper bound, and Illumina aligned span is not template length (`abs(TLEN)`) |
| `fragment-size` | 1-bp aligned-span histogram, mode, short (< 150 bp) share, mono/di peaks, 10 bp periodicity |
| `lionheart` | opt-in upstream LIONHEART v2.2.0 extraction of 898 raw sample-standardized chromatin-accessibility correlation scores from the selected hg38 BAM; needs ≥5M eligible reads, the modified mosdepth and Zenodo v003 resources; ONT single-end mode is experimental, and no cancer prediction is run |
| `short-read-fragment-size` | separate exploratory paired-end Illumina histogram and size summaries from absolute BAM TLEN; requires a prealigned BAM with every `@RG PL:ILLUMINA` |
| `copy-number` | tumour fraction, ploidy, genome log2 track and segments through the installed ichorCNA toolchain; a built protocol-matched cohort supplies a verified technical panel, but no clinical calibration or interpretation; the unbuilt default runs without a panel and remains not interpretable; needs 2M eligible reads, `traceback toolchain install ichor` and `traceback method-asset register --from-toolchain copy-number --root R` |
| `instability` | bin-weighted median absolute deviation of the selected copy-number result's segment log2 values (`tmad`), with used-bin and null-estimate coverage counts; needs 2M eligible reads and a successful copy-number result for the same input; exploratory and cohort-relative, with no ONT cutoff |
| `arm-summary` | medians of finite, unmasked PoN-corrected copy-number bins wholly inside each autosomal p/q arm; requires a selected verified PoN, the registered GRCh38 centromere asset, and 2M eligible reads. The separate `lab arm-zscores` command compares these summaries with compatible cohort controls; neither output supplies a clinical threshold |
| `modbase-qc` | mod codes in the MM tags, MM-tagged share, mean ML at aligned CpG calls (read from `meth-calls`), share of those calls below 0.7 confidence, tagged reads with unusable MM/ML; skipped (`missing_tags`) when no read carries MM |
| `long-tail` | shares of molecules > 300 bp, 1–4 kb (delayed processing) and > 7.5 kb (lysis); a supplementary-split read counts at its read length |
| `sex-check` | chrY/chrX read density per Mb of non-N, non-PAR reference and a male / female / indeterminate call; needs 500,000 eligible reads |
| `identity` | opt-in Somalier fingerprint from a full aligned hg38 input with at least 10M eligible reads and operator-pinned sites; `lab identity compare` relates two current selected fingerprints. Exploratory and privacy-sensitive, with no validated ONT kinship threshold |
| `dd-cfdna-chry` | exploratory male-donor/female-recipient transplant fraction from chrY non-PAR reads and an input-scoped, operator-verified female-background/full-male calibration; depth minimum comes from that calibration ([registration and limits](dd-cfdna-chry.md)) |
| `fetal-fraction` | exploratory NIPTeR 1.0.2 chromosome 13/18/21 z-scores against an operator-attested, input-scoped matched control RDS; its fetal-fraction field is always null because NIPTeR does not estimate it. No trisomy call or ONT clinical cutoff ([registration and limits](fetal-nipter.md)) |
| `fetal-fraction-chry` | exploratory chrY-based fraction for an asserted male fetus and female maternal background, with separate input-scoped protocol-matched calibration; returns null outside its calibrated interval and makes no aneuploidy call ([registration and limits](fetal-chry.md)) |
| `mtdna` | chrM reads (MAPQ ≥ 20) as a share of all primary reads, and the chrM fragment mode |
| `microbial` | exploratory Kraken2/Bracken species profile from primary, QC-passing, non-duplicate human-unmapped ONT reads; needs operator-pinned tools and database; no infection diagnosis |
| `viral-reads` | EBV, HPV and HBV reads per million human-unmapped primary reads from a separate minimap2 ≥ 2.28 alignment; needs an operator-registered viral FASTA and is exploratory for ONT cfDNA |
| `cell-origin` | share of cfDNA from each of 40 Loyfer U250 cell types with bootstrap intervals; the Lab decodes MM/ML CpG calls in-house and uses the registered Loyfer atlas with UXM and NNLS. It needs 2.5M eligible reads and `traceback method-asset register --from-dir LOYFER_DIR --root R`; minor cell types are not interpretable below 5x. Missing or uncertain calls stay unknown; confident 5hmC and 5mC both enter the UXM methylated (M) state. Every run writes a private prefiltered BAM in worker scratch; downsample and split runs first select their reads into a second transient BAM. Both are deleted after normal run completion, and neither is published. The signed cell-origin method retains its separate modkit path |
| `meth-tf` | slow-profile exploratory tumour share of a two-component, nonnegative methylation-marker fit; needs 2.5M eligible reads, C+m calls, and an operator-registered reference with matching genome and modified-base model; no clinical calibration or threshold |
| `methylbert` | speculative ONT tumour/normal deconvolution from an operator-trusted, fine-tuned MethylBERT bundle and pinned installed code; needs modified-base input, at least 2.5M eligible reads, and CUDA. No model ships with the Lab, and a generic pretrained encoder is insufficient ([bundle contract](METHYLBERT-BUNDLE.md)) |
| `end-motifs` | reference 4-mers at both ends of each molecule (5′ forward end and the reverse complement of the other end), motif diversity score and CCCA share; needs 500,000 eligible reads and the registered FASTA |
| `griffin` | pinned Griffin GC-corrected TF-site nucleosome profile on a registered hg38 site list; needs an Illumina-declared paired-end BAM, at least 5 million eligible autosomal pairs, the registered reference assets, Snakemake 5.19.2 importable by the Lab Python interpreter, and bedtools, samtools, pyBigWig and pybedtools. Native ONT skips; derived Illumina downsample and split selections run when the selected reads meet the 5-million-pair gate and the other requirements. Site profiles are descriptive, not a tissue-origin or clinical call |
| `delfi-ratio` | short (100–150 bp) / long (151–220 bp) ratio per 5 Mb bin on chr1–22, median-centred, and its SD; no GC correction, cohort-relative only; needs 1,000,000 eligible reads |
| `tss-profile` | strand-oriented molecule-overlap coverage in 50 bp bins around pinned GENCODE v46 autosomal protein-coding transcription start sites, with a central-to-flank ratio; needs 5M autosomal eligible reads and the registered annotation. It does not infer gene expression |
| `wps` | long-fragment windowed protection score from D21-eligible 120–180 bp ONT aligned spans: spanning 120 bp windows minus molecules with either aligned-span endpoint inside them. Coordinates are 0-based and half-open: a span starting exactly at a window's left edge and reaching its right edge counts as spanning and has its start endpoint inside the window, for a net contribution of zero; aligned endpoints are `start` and `end - 1`, and a molecule with both endpoints inside is subtracted once. Requires hg38 with `chr1`–`chr22` autosome names and known approximate depth ≥ 10x for each input, including derived controls. No minimum qualifying-fragment depth has been validated; its measured depth and fragment count are reported as support, not a biological WPS score. The full input also needs free lab-volume space for its exact track (four bytes per autosomal base, 10% overhead, plus 2 GiB margin); insufficient space yields a `disk` skip. The full result publishes exact per-base scores in compressed 1 Mb chunks indexed by `wps_exact_index.json`. Derived downsample and split controls that meet the 10x gate publish a separate mean-per-1 kb-bin preview in `wps_track.npz`, but do not retain another exact per-base track. The preview smooths away per-base detail and cannot recover the exact track. A genome-wide or 1 kb average of raw WPS is dominated by qualifying-fragment coverage and is not reported as an interpreted scalar. No nucleosome calls, validated stability tolerance, or clinical cutoff. |
| `finaletoolkit-xcheck` | FinaleToolkit **1.1.0** cross-check on a verified prealigned, indexed, paired-end Illumina hg38 BAM with at least 1M eligible reads. Set `TRACEBACK_FINALETOOLKIT_BIN` to that release's executable (or place it on `PATH`); an `env`-style launcher or Windows wrapper can also use `TRACEBACK_FINALETOOLKIT_PYTHON` to run the pinned package module under that interpreter, so the attested runtime performs the measurement. The full input only is checked; ONT and derived inputs receive labelled skips. The upstream 4-mer 5′-end frequencies and uncorrected, unmerged 100-kb DELFI short/long counts (MAPQ ≥20) are compared with an independent paired-end counter on the same BAM and registered FASTA. The artifact gives Pearson correlations, matched-bin count, exact count agreement and the fixed 0.95/0.90 engineering thresholds; concordance also requires exact DELFI short/long count agreement. It retains the upstream and reference TSVs. A sparse or constant track has no correlation verdict. Its 1.1.0 end-motif region worker accepts but does not apply the declared minimum-length option, so the comparator follows the actual output and records that behavior. These settings intentionally differ from the published GC-corrected, arm-merged DELFI classifier and from the Lab's single-end ONT `end-motifs` and `delfi-ratio`; no clinical or cross-platform equivalence is implied. FinaleToolkit 1.1.0 produced no usable single-end ONT fragments in the D13 spike. |
| `meth-calls` | intermediate: decodes MM/ML once into site-level CpG call chunks (read row, forward-strand C position, strand, 5mC/5hmC/canonical ML); derived inputs reuse it through their row mask |
| `global-5mc` | share of CpG site calls whose most likely state is 5mC |
| `global-5hmc` | share of CpG site calls (with a 5hmC channel) whose most likely state is 5hmC; skipped (`missing_channel`) when the MM tags carry no `C+h` |
| `epi-clock` | exploratory three-CpG Weidner age arithmetic transferred to ONT cfDNA; needs 5M eligible reads, MM-tagged CpG calls, and an operator-verified hg38 assay-site map; **SPECULATIVE**, without a clinical age range or ONT validation |
| `pmd-score` | 5mC share inside vs outside the Zhou 2018 common PMDs and their difference; needs 500,000 eligible reads and the PMD asset (below) |
| `hypometh-reads` | among reads with ≥ 4 CpG calls, the share under 25% methylated, and U/X/M counts (≤ 25% / between / ≥ 75%); needs 500,000 eligible reads |
| `frag-x-meth` | descriptive molecule-count matrix of aligned-size bins by per-read 5mC fraction (at least four distinct CpG sites/read); the scalar is long (151–220 bp) mean minus short (100–150 bp) mean, null if either group is empty; no normal range |
| `breakpoint-motifs` | 4-mer counts at aligned-reference-span start and end−1, stratified by CpG proximity; the scalar is the fraction of ends near a CpG. These are not proven physical cleavage positions, and the methylation link is unavailable without a supported joint input |
| `aligned-end-structure` | exploratory ONT aligned-span U/D end asymmetry around operator-pinned hg38 open-chromatin centers and exact support for separately pinned U/D preferred-end coordinates; needs a native ONT input or its downsample/split subset at ≥10x observed eligible depth and both catalogues registered below. No tissue-origin or clinical calibration |

The Lab's cell-origin v2 execution identity pins normalized source digests of
its MM/ML decoder and reused UXM, NNLS, bootstrap, model, and prefilter code,
along with the NumPy, pysam, and htslib versions. A changed implementation
therefore needs a new result rather than reusing a prior cached estimate.
After a ready cell-origin worker examines the marker regions, too few passing
alignments, confident CpG calls, classified fragments, or observed markers
publish a `skipped/low_support` result with no composition metrics or artifact.
Parser errors, resource caps, non-convergence, and validation failures still
publish failed attempts for investigation.

The FinaleToolkit verdict uses the D13 Pearson correlation thresholds and
exact DELFI counts. Pearson compares 4-mer frequency patterns: even a value
of 1 can coexist with different absolute frequencies. The artifact and Signals
show their maximum absolute frequency difference as a diagnostic, without an
additional acceptance cutoff.

**Exploratory epi-clock.** Verify the three hg38 assay-site coordinates, then
follow the [epi-clock map instructions](epi-clock.md) to prepare the JSON map and
its SHA-256. Register and inspect it with:

```bash
traceback lab asset register-clock-map --file MAP.json --sha256 HEX_SHA256 --root ROOT
traceback lab asset inspect-clock-map --root ROOT
```

A numerical age requires at least five eligible calls at each CpG, but that is
only a completeness gate:
with five PDE4C calls, changing one call's modified state shifts the fitted
arithmetic by 32.94 years. Treat even a non-null result as exploratory until
matched-platform validation is available.

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

When downsample and split controls are selected, LIONHEART extracts each
eligible derived input from a temporary BAM containing only that fraction's
or half's reads. The same opt-in, ONT platform, resources, and measured
5-million-eligible-read gate apply independently to each input. A derived
result below that gate is labelled skipped; it is not replaced by the full
input's score.

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

**Short-read TLEN slice.** For a prealigned paired-end Illumina BAM against
the registered hg38 reference, run the normal Lab command to include its
separate TLEN measurement:

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

**Aligned-end catalogues.** `aligned-end-structure` requires two local,
operator-supplied hg38 files. The center file is sorted BED3
(`chrN<TAB>start<TAB>end`, 0-based half-open); the center is the integer
midpoint, and centers must be more than 200 bp apart so ±100 bp profiles do
not overlap. The preferred-end file is sorted BED4 with 0-based half-open
one-base intervals
and `U` or `D` in column four. `U` means the lower-coordinate aligned end;
`D` means the higher-coordinate aligned end. Both files use chr1–chr22 and
numeric chromosome order, contain no header or extra columns, and must each
be at most 64 MiB. Filter raw peak files to these columns and autosomes.
Sort centers by chromosome and their integer midpoint, and preferred ends by
chromosome and start:

```bash
awk 'BEGIN { OFS="\t" } { print $1, $2, $3, int(($2+$3)/2) }' centers.bed |
  sort -k1.4,1n -k4,4n | cut -f1-3 > centers.sorted.bed
sort -k1.4,1n -k2,2n preferred-ends.bed > preferred-ends.sorted.bed
```

Thin centers separately until consecutive midpoints on each chromosome are
more than 200 bp apart; sorting alone does not remove overlapping profiles.
The Lab does not silently discard overlapping peaks or convert genome builds.
Confirm the source build and convert any 1-based source positions before
registration:
a wrong build or one-base shift can produce a plausible but incorrect score.
A U and D site may share
one coordinate, but duplicate sites in the same direction are rejected. Both files
must identify their provenance outside the file, including source accession,
release, assembly, coordinate convention and terms. The Lab validates and
rehashes their exact bytes. It neither downloads nor redistributes them.

```bash
traceback lab asset end-catalogues-register \
  --centers YOUR_SORTED_hg38_centers.bed \
  --preferred YOUR_SORTED_hg38_preferred_U_D_sites.bed \
  --source-label YOUR_SHORT_DESCRIPTION \
  --centers-source YOUR_CENTER_ACCESSION_AND_RELEASE \
  --preferred-source YOUR_END_SITE_ACCESSION_AND_RELEASE \
  --terms unknown --root R
traceback lab asset end-catalogues-inspect --root R
```

The result measures raw `D−U` end counts at integer offsets [−70, −50] bp
and `U−D` at [50, 70] bp around pinned centers. These bounds follow the
published OCF summation at peaks −60 and +60 with a ±10 offset. The paper
calls them 20-bp windows, while summing both integer endpoints includes 21
positions. This implementation follows the displayed summation literally;
the paper does not resolve that counting convention. It also reports the
fraction of valid aligned ends exactly matching the pinned U/D site catalogue;
this is a support count, not de novo site discovery or enrichment against a calibrated
background. The raw count difference scales with depth, eligible-region count
and end coverage; it is null when no aligned ends fall in its measurement
windows or when either side of the center lacks window observations.
The proxy does not impose the short-fragment length constraint of the
published cfDNA method. Its artifact reports aggregate aligned-span counts
in five length bins so that this difference can be assessed for each run.
The artifact also records the strongest left-side D and right-side U peak
offsets as clipping diagnostics; these are descriptive and do not validate
the score's biological origin.
Downsample and split results use the same selected read-summary rows as other
Lab measurements. The 10x gate is recalculated from those selected rows;
CNA-derived inputs remain unsupported. The reported 10x minimum is an input
gate, not a validated adequacy threshold.
The raw count score changes with retained depth, so a downsampled score should
not be read as a biological difference from the full-input score; split halves
are descriptive repeatability checks only.
The 201-position raw U/D profiles and exact preferred-site support are
sample-level derivatives. Treat the artifact as sample-identifiable data,
especially when registering a small or targeted catalogue.
The score has no validated stability tolerance or biological acceptance
interval. Its headline placement is for Lab navigation and does not imply
clinical or tissue-origin validity. Both are ONT aligned-span proxies, not confirmed physical
cleavage positions. The [OCF study](https://pubmed.ncbi.nlm.nih.gov/30808726/)
used tissue-specific short-read open-chromatin signals, while the
[size-tagged preferred-end study](https://www.pnas.org/doi/10.1073/pnas.1804134115)
mined sites from roughly 470x pooled paired-end data. A substituted hg38
ATAC peak set is not the original tissue-specific DNase-seq catalogue.
Neither a 10x ONT result nor an unverified site set supports a tissue-origin,
fetal-fraction, cancer or clinical call.

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

**Microbial profile.** Install Kraken2 and Bracken from their upstream source
layouts through your approved local process: the Kraken2 launcher must sit
beside `classify` and `kraken2lib.pm`, and the Bracken launcher must sit beside
`src/est_abundance.py`. The Lab pins those executed helpers and the Python
interpreter used by Bracken. A packaged layout without those files reports
`missing_toolchain` with an unsupported-layout explanation rather than silently
omitting helper hashes. Build
a Kraken2 database and its Bracken read-length distribution, and
register its exact bytes under the run ROOT. The database remains in its
operator-owned directory; the Lab stores its path only in a local manifest and
publishes the bundle SHA-256 and source label. The registration requires
`hash.k2d`, `opts.k2d`, `taxo.k2d`, and
`database<READ_LENGTH>mers.kmer_distrib`. The Bracken read length must be
75–500 bases. An altered or missing file makes the measurement unavailable.
Registration records a bounded file-stat receipt for the sample page. The page
and execution planning check that receipt without streaming the large database;
the worker verifies complete file bytes before and after classification.
If the database must remain at a new path, register it as a new Lab root; this
registration will not silently switch to another directory with identical bytes.
An older registration without a page receipt can be refreshed in place with
the same `lab microbial db add` command: it verifies all bytes before adding
the receipt. The sample page reports that legacy registration as unavailable
until refreshed. If the sample page or execution planning reports changed
registered files after a byte-identical restore or a file-stat-only change,
repeat `lab microbial db add` with the original absolute directory, read
length, and source label recorded locally in
`ROOT/lab/assets/microbial-db/manifest.json`. It rehashes the required files
and refreshes the receipt only when the bundle (file hashes and read length),
directory, and source label match the existing registration. A refresh reads
the required files twice, so allow time for a large database. If the read
length or label was mistyped, retry with its original value. If the bytes or
directory changed unintentionally, restore the original bytes at the registered
path and retry. To keep different database bytes, Bracken read length, source
label, or directory, use a new Lab root.

```bash
traceback lab microbial db add --directory KRAKEN_DB --read-length 75 \
  --source-label operator-built-kraken-bracken --root R
traceback lab microbial db inspect --root R
traceback lab run ID12 --only microbial --root R
```

Only human-unmapped primary reads that pass QC and are not marked duplicate
are classified. Reads shorter than the registered Bracken length are counted
as excluded; longer reads are clipped to that exact length before Kraken2.
The rate denominator is **all** primary QC-passing non-duplicate reads,
including aligned and short unmapped reads. Bracken species estimates under
Bacteria, Archaea, Viruses, and Fungi are shown as estimated reads per million
denominator reads; human and other non-microbial taxa are excluded. At most
256 species are displayed, with an omitted-taxon count. Read names and Kraken
per-read assignments remain transient and are not published.
The headline microbial estimate includes accepted species beyond the 256
displayed rows, so it need not equal the sum of the visible table.
The worker briefly writes candidate unmapped sequence in a private Lab scratch
directory, with a 512 MiB candidate FASTA limit per run. Larger candidate sets
are sent in full to Kraken2 through a pipe; the partial staged file is removed
first. Kraken's per-read assignments are suppressed, while its aggregate report feeds
Bracken. Normal completion removes scratch; a crash or abrupt power loss can
leave files for an operator to inspect and remove under the Lab root.
An input with no primary QC-passing non-duplicate reads has no microbial rate
or cohort comparison. If unmapped reads exist but all are too short or lack
sequence, the rate and cohort comparison are also unavailable. An input with
only confidently human-mapped reads has a measured zero candidate burden.
Kraken classifications above species level without any species row yield a zero
Bracken species estimate; that does not mean the candidate read is biologically
non-microbial. The QC panel keeps the classified-read count visible.
The QC panel counts missing-sequence unmapped reads separately from short reads.

The profile is speculative and marked **PRIVACY**. A compatible healthy-v1
cohort of at least ten *other* samples supplies a per-taxon and overall 97.5th
percentile only when the aligner, exact tool and database identities, analysis
parameters, and selected inputs match. Build that cohort as below. Missing
taxon counts are treated as zero only when the cohort profile was complete;
otherwise they are excluded. An absent or incompatible cohort leaves the
percentile explicitly unavailable. A high candidate signal can reflect
contamination, residual human sequence, database content, or misclassification;
it is not an infection diagnosis or an ONT clinical threshold.

**Methylation tumour-fraction reference.** `meth-tf` needs an operator-supplied
JSON marker panel. The repository does not bundle or download tumour/healthy
marker fractions. The operator must establish the panel's source and use rights,
then register its exact bytes under the same ROOT as the run:

```bash
traceback lab asset add meth-tf-reference --file YOUR_LOCAL_MARKER_REFERENCE.json \
  --source-label YOUR_PANEL_DESCRIPTION --terms unknown --root R
traceback lab asset inspect meth-tf-reference --root R
```

The JSON has schema `traceback.lab.meth-tf-reference.v1`, the exact genome
`reference_sha256`, the BAM-header `modbase_model` ID, and mandatory
`call_definition: "C+m_argmax_5mC_over_canonical_5mC_5hmC"`. Its sorted
`markers` array contains autosomal `chrom`, zero-based CpG-C `pos0`, and
`healthy` and `tumour` fractions between 0 and 1. These fractions must be
measured as the share of observed C+m-channel CpG calls whose largest
canonical/5mC/5hmC probability is 5mC, under a protocol compatible with the
sample's modified-base model and filtering. Generic bisulfite beta values do
not automatically meet that definition. For example,
`{"chrom":"chr1","pos0":101,"healthy":0.25,"tumour":0.75}` illustrates the
format only; these numbers are synthetic and are not a usable reference.
The BAM must declare exactly one distinct modified-base model ID across its
read groups. Meth-TF skips a BAM declaring multiple models rather than choosing
one; register a reference for a single-model input.
Registration is immutable within one Lab root: a different panel needs a
separate root. Skipped, refused, or numerically unidentifiable results are
unavailable rather than zero; a sparse but identifiable fit can estimate zero.
The result has no detection limit, calibrated
interval, or clinical cutoff. If an existing registration is damaged, `lab
asset add` will not overwrite it: restore that registration's exact files from
a known-good backup, or use a new ROOT. If planning could not pin a readable
registration, retry the run after checking the asset's local storage.
`identifiable` means only that the observed,
count-weighted reference columns have numerical rank and a nonzero fit. The
reported `reference_condition_number` describes how sensitive the numerical
fit is to small changes; a large value warns of nearly collinear references,
without implying a validated reliability cutoff.
The `markers_covered` and `cpg_calls_used` diagnostics show the support for
the numerical fit, not biological confidence. With exactly two covered
markers, a full-rank two-weight fit is exactly determined: an interior feasible
fit has zero residual up to numerical error, so a near-zero `fit_rms` is not
evidence of goodness of fit. A boundary-clamped fit may retain mismatch, but
the residual alone does not identify whether the solution lies on a boundary.
`fit_rms` is the count-weighted residual of the unnormalized, nonnegative
reference-weight fit, before its weights are normalized into a tumour share;
it is not the residual of a sum-to-one mixture fit. It has no goodness-of-fit
threshold or biological confidence claim.

The Agreement lens pairs PoN-qualified copy-number tumour fraction with the
methylation estimate. Its other plots show raw fragment-size mode and mtDNA
fraction against copy-number tumour fraction, with compatible paired cohort
points from one pinned generation when available. The target source is excluded
from those points; they are descriptive and have no minimum-size gate or
reference-interval claim. The lens computes no combined score, warns that the
signals are not independent, and notes the whole-genome-doubling caveat for
copy-number tumour fraction. The methylation pair shows its fit support and
explicit unavailability reasons.

**Viral reference.** `viral-reads` leaves the registered hg38 human reference
unchanged. It takes human-unmapped primary reads from the aligned BAM, including
duplicate-flagged and QC-fail primaries but excluding secondary and
supplementary records, then runs
minimap2 separately against an operator-supplied viral FASTA, and reports
EBV/HPV/HBV aligned reads per million human-unmapped primaries. The count is
null if that denominator is zero.
The microbial panel's viral taxa use Bracken estimates from QC-passing,
non-duplicate unmapped reads long enough for its registered read length and
divide by all QC-passing, non-duplicate primaries. `viral-reads` instead uses
the side-reference alignments and divides by human-unmapped primaries,
including QC-fail and duplicate-flagged reads. Their rates can differ or one
can be zero while the other is nonzero; do not compare them as matching assays.
A read with reported primary or secondary alignments to more than one virus
group is excluded from all groups; multiple
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
  When either sample has selected panel-of-normals copy-number, the two-sample
  A/B view keeps independently pinned metrics and lists copy-number and its
  dependents as unpaired. It never pools different panels as one reference.
- **Trust/QC**: depth against each algorithm's minimum, the run table with
  scrubbed stderr for failures and a link to a matching numbered run-log line,
  provisional QC bands, library health. The link searches up to 64 recently updated
  runs and reveals only allowlisted status fields. A line must name the selected
  immutable result attempt; legacy logs without that ID, older runs, or cached
  steps may have no matching line. A latest-run in-progress or interrupted note
  appears only when that run names the same source and full input; the selected
  result status remains visible beside it. Unreadable or ambiguous selections
  appear as separate run-table rows without a fabricated duration. If the step published no result yet,
  a sample-scoped run notice appears instead of a result row. If the latest
  analysis or simulation run log is unreadable, unsafe, malformed, or outside
  the 1 MiB / 4,096-line limit, the status banner reports an unavailable
  status without inferring whether the run ended normally or was interrupted.
- **Stability**: one small multiple per metric: deviation from full depth at
  each fraction, the stability band, and split halves A/B at 50%. Each half
  shows its own status when a result is skipped, failed, or lacks a numeric
  metric; a split-half verdict appears only when both values are available
  and the stability comparison is interpretable. When both halves have numeric
  usable values, their raw values and relative difference remain visible without a
  verdict. When a half is unavailable, its status remains visible. A selected
  half with parameters that differ from the current full-depth result
  is named unavailable and excluded from the comparison. An extra
  unrecognized split label is disclosed even when A and B are both usable. REPEATABILITY's
  median and worst-three list include every numeric split comparison; only
  metrics with a validated verdict enter its stability count. Home separately
  counts unavailable metric comparisons, even when no pair is comparable.
  Home also counts unavailable full-depth algorithm results, with their names
  and reasons on Stability. Damaged downsample selections carry their reason
  into the depth points. A measurement usable only at an isolated lower depth
  is labelled as such rather than claiming stability through missing higher-depth
  evidence. If a full-depth selected result is unreadable or ambiguous, Stability names that
  algorithm without inventing a metric-level panel from damaged data.
  When a metric lacks a stability rule or numeric values, the downsample cell
  says no stability verdict instead of calling it unstable.

## Building a Lab cohort

Use the same `--root R` as the source runs. The sample ID12 is printed by
`lab run` and listed on the Lab landing page. Only stored, full-input,
non-EXAMPLE samples can be added. A repeat add of the same source is
idempotent; rerunning an analysis does not create a new member.

```bash
traceback lab cohort add ID12 --cohort healthy-v1 --root R
# Repeat add for the other eligible cohort samples.
traceback lab cohort build --cohort healthy-v1 --root R
traceback lab serve --root R --open
```

`healthy-v1` is the default cohort used by sample lenses and the implicit ONT
panel; another cohort must be selected explicitly.

`build` publishes an immutable generation and changes the current pointer
atomically. Open **Compare** and choose that cohort; the page names the exact
generation used for the whole response. Build again after a member or its
results change. `cohort remove` removes one member from the pending roster by
ID12 or full source ID, even if its stored source record is damaged. It does
not delete that sample's input or results, or change the published generation;
run `cohort build` to publish the revised membership. Different aligners or
incompatible algorithm parameters are
excluded per metric, with counts shown. Pre-aligned inputs whose processing
provenance is unknown are isolated by their input identity; they are not
pooled as protocol-matched references. Distinct source IDs are not proof of
independent donors. The separate PoN denominator does not become a
percentile denominator.

For an internal cohort with at least ten Lab-aligned members, `cohort build`
attempts panel construction. A panel source must pass the builder's gate of
at least 2M counted reads after filtering; fewer than ten passing sources
blocks the build. This panel-source gate is stricter than the 1M counted-read
floor for measuring a target. The build creates one full panel and one
leave-one-out panel per passing source, then recomputes those members'
copy-number, instability, and arm-summary results against their own panels
before publishing the generation. This build requires the
installed ichorCNA toolchain and registered copy-number method assets. If
panel construction or member recomputation fails, no new generation is
selected and any previously selected generation remains current. The
read-side fallback that retains independent comparisons when an existing
panel later becomes unavailable does not apply to building a generation.
Member selector changes are protected by a durable cohort selection journal.
If a build process is killed, readers continue to see the old selections while
the old generation is current. The next Lab writer restores those selections
before starting new work, or confirms the new generation if its pointer was
already published. Immutable result attempts remain available for inspection.
If the journal is damaged, selection reads fail closed and writers stop until
the damaged storage is repaired from a known-good backup.
`pon_n` counts all panel sources; a selected member panel uses `pon_n - 1`
sources. A nonmember uses the full panel. Both copy-number and instability
still require 2M eligible reads on the target input. Copy-number also enforces
the signed 1M counted-read floor after filtering, with or without a panel.
The corrected copy-number method version does not reuse older copy-number or
dependent instability attempts. Rerun affected inputs and rebuild existing
cohorts to restore current-version comparisons. The sample view marks selected
older copy-number and dependent instability results as `superseded_method`
until rerun; their immutable attempts remain available for inspection.

`arm-summary` summarizes PoN-corrected copy-number bins on autosomal p/q arms.
It derives boundaries from the locally registered ichorCNA GRCh38 centromere
asset and input reference lengths. A bin contributes only if finite, unmasked,
and wholly inside an arm; the arm value is the median of contributing bins.
It requires at least 2 million eligible reads and a selected PoN. The pinned
arm-definition bytes enter both result identity and cohort compatibility.

`traceback lab arm-zscores ID12 --cohort ID --root R` compares the selected
arm summaries against one published cohort generation. It excludes the target
source and requires ten remaining controls with the same protocol and arm
definition. A member of an exactly-ten-source cohort has nine controls and
receives `no_reference`. The JSON reports n, the cohort mean and population
SD, z, or a named unavailable state. Each arm also reports the target's bin
count and the smallest bin count among matched controls when the pinned
generation recorded them; older generations show `null` (or `?` in the human
view). The human view labels the control count separately from bin support.
This is exploratory: no abnormality
cutoff, donor-independence claim, or clinical reference is assigned.
The JSON response names the target panel binding and source count, whether
the target was excluded, and whether a selected arm result exists under that
binding. A missing result leaves the panel binding visible for rerun planning;
it does not claim the target was measured with that panel. A nonmember uses the full panel while cohort members
use leave-one-out panels, so their correction baselines differ; the z score
is a descriptive cohort comparison, not a calibrated test statistic.

`lab run` and `lab simulate` use a built `healthy-v1` cohort by default when
one exists for a real ONT input. Illumina and EXAMPLE inputs do not select
that implicit ONT panel. An explicit ONT cohort refuses those inputs. Pass
`--cohort ID` to pin another built cohort; an explicit cohort
with no compatible verified panel fails rather than falling back. If the
implicit `healthy-v1` is unbuilt, copy-number keeps its no-PoN baseline.
If a built implicit panel's toolchain or registered method assets are missing,
copy-number records `missing_toolchain` or `missing_asset`, its dependents
skip upstream, and independent algorithms continue. An older uncorrected
copy-number result is never reused. If the built implicit panel's protocol
does not match this input (including a prealigned ONT input with unknown
alignment provenance or an input realigned after a tool upgrade), copy-number
records `protocol_mismatch`, its dependents skip upstream, and independent
algorithms continue. The no-PoN copy-number baseline is never substituted.
Damaged panel bytes, an invalid leave-one-out binding, or other panel
verification failures stop the run.
An explicit `--cohort` is checked even when `--only` omits copy-number.

If a selected cohort cannot be verified, **Compare** lists it as unavailable
with a problem code and a next action. Other verified generations remain
selectable. Follow that action, then reload the page; an unavailable cohort or
target sample has no comparison values, and the Lab does not fall back to an
older generation. A cohort with members but no published generation needs
`traceback lab cohort build --cohort ID --root R` before it can be selected.
If only the selected panel's ichor toolchain or registered method assets are
missing, Compare keeps independent metric comparisons from the pinned
generation. It withholds copy-number and every dependent metric, names the
missing panel dependency, and never substitutes an uncorrected result.

## Positive controls

`traceback lab simulate cna <id12|latest>` requires an ONT sample and builds a synthetic copy-number
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

Another Lab writer is running, or cohort selection changed repeatedly during
a stored dry-run preview (exit 3, retryable). Run, simulation, cohort changes,
identity operations, and asset or calibration registration share the single
`ROOT/lab/.lock`. For a lock conflict, the cause names the holder's command,
run id, pid, step and start time. Wait for it to finish, or stop that process.
A dry run takes no lock; if cohort selection changes repeatedly during its
selected-summary read, retry the preview after the cohort update finishes.

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
(exit 4). For `cohort remove`, use a unique member ID12 or full source ID from
the pending roster; damaged source records do not prevent removal.
`traceback lab runs` lists runs. Pass the same `--root` (or `TRACEBACK_ROOT`)
you ran with.

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
Malformed transaction metadata or member selectors, and missing, symlinked or
non-directory required selector ancestors, stop Lab writers with this code
before changing run or cohort pointers. Inspect
`ROOT/lab/pending-cohort-selections.json`, `ROOT/lab/cohort-selection-epoch`,
the matching cohort pointer and generation, and the affected member result
selectors; restore damaged files from a known-good backup before retrying.
Unexpected filesystem I/O failures may still report an internal error. Do not
discard the pending journal without first establishing which generation was
committed.
Finder's `.DS_Store` file is ignored; other unexpected generation entries
must be inspected and moved aside manually after preserving them. If only the
current pointer is missing and the member roster is intact, `lab cohort build`
publishes a fresh generation from that roster while retaining earlier
generation directories.

### TBX-LAB-023

An operator-supplied `--sex` conflicts with an assertion already recorded for
the same sample, or the local sex metadata is malformed (exit 3). Reuse the
recorded assertion or omit the flag. A rerun without `--sex` can continue and
preserves malformed metadata; inspect and restore it from a known-good local
backup before setting `--sex` again. No measured result is changed.

### TBX-LAB-024

An input has missing, mixed, or unrecognized platform declarations, an
unaligned Illumina input would need the ONT-only aligner, or a stored input's
platform disagrees with its BAM header (exit 3). Use a BAM whose read groups
unanimously declare `PL:ONT` or `PL:ILLUMINA`; align Illumina reads externally
before supplying the BAM. For a damaged or mismatched stored input, restore
that input from a known-good backup or re-ingest the original BAM under a fresh
ROOT. Reusing the same ROOT does not replace an existing input manifest.
Rewriting a BAM header changes its source identity. Use
`traceback lab cohort remove ID12 --cohort ID --root R` for an unrecoverable member, then rebuild
that cohort; the old published generation stays selected until the rebuild.

### TBX-LAB-025

An advisory warning (exit 0): physical RAM is below the 16 GiB recommendation
for full hg38 alignment. The run continues, but alignment may fail or be slow.
Use a machine with at least 16 GiB RAM for a full alignment. If physical RAM
cannot be detected, the Lab omits this advisory rather than blocking the run.

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

- 2026-10-17: documented the expanded registered catalog, platform-specific
  methods, input-scoped calibrations, and current Lab writer commands.
- 2026-10-15: first slice: `lab run`, `lab runs`, `lab algorithms`,
  `lab simulate cna|split`, `lab serve`, `lab doctor`, `lab run --example`.
