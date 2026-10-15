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
    [--skip-bad-chunks] [--force] [--timeout-s 3600]
    [--threads N] [--sort-mem 1G] [--mmi PATH]
    [--dry-run] [--verbose] [--json] [--root R]
traceback lab simulate cna <id12|latest> [--tf 0.03,0.10,0.30]   synthetic CNA spike, copy-number recovery
traceback lab simulate split <id12|latest>                         split-half pseudo-replicate
traceback lab algorithms [--json]          registered algorithms (id, tier, min depth, licenses, footnote)
traceback lab doctor [--json]              setup checks, each with a fix command
traceback lab serve [--open]               the local page (loopback only)
traceback lab runs [RUN_ID] [--step S]     the run log, or one run's steps
```

## Running on a kit

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

**Reruns.** Rerunning the same command recomputes only missing or failed
results; unchanged chunks are not re-hashed and the alignment is reused.
`traceback lab run latest` or `traceback lab run <id12>` reruns on the stored
sample without the chunk paths. `--force` recomputes every result.
`--only a,b` runs just those algorithms (overriding `--profile`), plus the
intermediates they need, labelled "(upstream)".

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

## Algorithms

`traceback lab algorithms` lists what is registered. The slice ships:

| id | What it reports |
|---|---|
| `read-summary` | intermediate: one pass over the aligned BAM into memmapped columns (no read names); every pysam algorithm and every downsample or split-half view reads it |
| `bam-qc` | primaries, mapped and MAPQ ≥ 20 shares, supplementary share (chimera proxy), mean read Q, share of sampled genome covered ≥ 1x, GC-bias slope, duplicate share (only when marked) |
| `depth` | eligible reads (primary, MAPQ ≥ 20, chr1–22/X/Y), mean span, approximate depth; the index count is shown only as an upper bound |
| `fragment-size` | 1-bp aligned-span histogram, mode, short (< 150 bp) share, mono/di peaks, 10 bp periodicity |
| `copy-number` | tumour fraction, ploidy, genome log2 track and segments through the installed ichorCNA toolchain (no panel of normals yet, so read as not interpretable); needs 2M eligible reads, `traceback toolchain install ichor` and `traceback method-asset register --from-toolchain copy-number --root R` |
| `modbase-qc` | mod codes in the MM tags, MM-tagged share, mean ML at CpG calls, share of CpG calls below 0.7 confidence, tagged reads with unusable MM/ML; skipped (`missing_tags`) when no read carries MM |
| `long-tail` | shares of molecules > 300 bp, 1–4 kb (delayed processing) and > 7.5 kb (lysis); a supplementary-split read counts at its read length |
| `sex-check` | chrY/chrX read density per Mb of non-N, non-PAR reference and a male / female / indeterminate call; needs 500,000 eligible reads |
| `mtdna` | chrM reads (MAPQ ≥ 20) as a share of all primary reads, and the chrM fragment mode |

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
- **Trust/QC**: depth against each algorithm's minimum, the run table with
  scrubbed stderr for failures, provisional QC bands, library health.
- **Stability**: one small multiple per metric: deviation from full depth at
  each fraction, the stability band, and split halves A/B at 50%.

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

An aligned BAM's `@SQ` lines differ from the registered reference, or the inputs mix aligned and unaligned BAMs (exit 3). The cause lists the first three differences. Pass the unaligned chunks and let the Lab align, or register the FASTA the BAM was aligned to.

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
report the code and the command.

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
