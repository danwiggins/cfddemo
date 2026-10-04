# Traceback quick guide for scientists

A short, practical guide for a collaborating scientist. It takes Nanopore BAMs
to a fragment-length record you can look at in your browser. For the full
reference, see [the operator guide](OPERATOR-GUIDE.md).

> Everything here is a research prototype. Records are **unqualified, local,
> and not for clinical use**. Nothing is uploaded. The measurement describes one
> file's fragment-length distribution; it makes no statement about health,
> tumour content or sample quality.

## What it does today

- **Measures one thing on real data:** the fragment-length distribution of an
  aligned BAM, counted as aligned reference span. The policy is locked: MAPQ ≥ 20,
  primary alignments only, chromosomes 1–22, X and Y, and 7 bins (0, 100, 150,
  200, 300, 500, 1000+ bp).
- **Seals every result as a signed record:** inputs are hashed and a copy is kept,
  so every number traces back to the exact file and method.
- **Shows records in a local web page:** a histogram, the denominator (how many
  reads were scanned and why some were excluded), and each record's status in
  plain words.

Cell-origin and copy-number are **not** in this pipeline yet. They exist only
as the separate demo scripts.

## One-time setup (macOS)

```bash
brew install samtools minimap2
git clone https://github.com/danwiggins/cfddemo.git traceback && cd traceback
uv sync
```

You also need a reference FASTA with its index, for example hg38 primary
(`samtools faidx hg38.primary.fa`). Building a minimap2 index once saves time
on every alignment:

```bash
minimap2 -x map-ont -d hg38.primary.mmi hg38.primary.fa
```

## From MinKNOW output to a record

MinKNOW and Dorado write **unaligned** BAMs. Traceback detects this and stops
with `TBX-BAM-003`, printing the command below. Align each barcode separately:
never merge different barcodes into one BAM.

```bash
R="$HOME/traceback-run1"            # one folder (ROOT) per experiment
FASTA=/path/to/hg38.primary.fa

# Once per ROOT
uv run traceback reference register --fasta "$FASTA" --id hg38 --root "$R"

# Per barcode: merge, align (keeping modification tags), sort, index
samtools cat -o barcode01.bam bam_pass/barcode01/*.bam
samtools fastq -T MM,ML,MN barcode01.bam \
  | minimap2 -ax map-ont -y hg38.primary.mmi - \
  | samtools sort -o barcode01.sorted.bam
samtools index barcode01.sorted.bam

# Measure, sign, and add to the catalog in one step
uv run traceback run barcode01.sorted.bam --reference hg38 --root "$R" \
  --label "barcode01 plasma draw 1" --import
```

Timings on an M-series Mac: aligning a 3 GB run takes about 9 minutes with
`-t 16`, mostly building the index if you skip the `.mmi` step. `run` takes
about 1 minute for a 2 GB BAM.

The operator guide has a ready-made loop for all barcodes
([Aligning MinKNOW output](OPERATOR-GUIDE.md#aligning-minknow-output)).

**Labels:** use a short operator note such as a barcode or draw number. Never
put names, dates of birth, MRNs or other donor identifiers in a label.

## Looking at results

```bash
uv run traceback catalog list --root "$R"   # every record, its label and counts
uv run traceback serve --root "$R"          # prints a one-use link; open it
```

The link works once, on this machine, for 60 seconds. Press Enter in the
terminal for a new one, and Ctrl-C to stop the server.

In the browser:

- **The records table** lists one row per record: label, eligible and scanned
  counts, preflight result, and import time.
- **A record page** shows the fragment-length histogram, a denominator strip,
  and a "What this record is" table.

How to read a record page:

| On the page | What it means |
|---|---|
| **Histogram** | Share of eligible alignments per bp in each bin. Bins have unequal widths, so bar *area* is the share. The open 1000+ bin is hatched. |
| **Eligible of scanned** | Reads that passed the locked policy, out of all records read. The strip shows why the rest were excluded: secondary, supplementary, MAPQ < 20, unmapped, contig outside the policy. |
| **Unqualified** | The method has not been qualified. Counts only, no interpretation. |
| **Development signature verified** | Signed and verified with this machine's development key. Proves the record wasn't altered; it is not a quality claim. |
| **Preflight: warn / partial** | Technical caveats about the input. For example, "basecall model not declared in the header" doesn't affect fragment length. |

As a reference point from the first real run: 3,435,813 of 5,822,296 records
were eligible (59%), and 63% of eligible fragments were 150–199 bp. That
describes that file only. It is not a reference range.

## Other useful commands

```bash
uv run traceback jobs --root "$R"                         # every run, newest first, with failures
uv run traceback status JOB_ID --root "$R"                # one job; a failed job shows code, cause and fix
uv run traceback label RECORD_ID "new note" --root "$R"   # add or change a label later
uv run traceback catalog export --csv out.csv --root "$R" # one row per record and bin, from signed counts
uv run traceback doctor --root "$R"                       # checks tools, disk and the reference
```

## When something goes wrong

Every refusal prints a **CODE**, a **CAUSE** and a **FIX**. The most common:

| Code | Meaning | Fix |
|---|---|---|
| `TBX-BAM-003` | Unaligned BAM (straight from MinKNOW or Dorado) | Align it with the commands above |
| `TBX-RUN-008` / `TBX-RUN-009` | BAM or its `.bai` index not found | Check the path; run `samtools index` |
| `TBX-BAM-002` | BAM contigs don't match the registered reference | The diff shows the first mismatches. If only the `chr` prefix differs, use `samtools reheader` |
| `TBX-JOB-002` | Another run on this ROOT is still working | Wait, or use a separate ROOT |

All codes are listed in the operator guide's
[troubleshooting table](OPERATOR-GUIDE.md#troubleshooting).

## What's coming, and where your input matters

- **Comparing two records side by side** (same settings, differences shown as
  descriptive only) is next.
- **Changing the analysis.** The plan is to measure once at 1-bp resolution, so
  bins, size windows and ratios can be explored on the page without re-running.
  A few named presets would be owned by the team. **Before this is built, it
  would help to know what you most want to change:** bin edges, a size-selection
  window, the MAPQ threshold, or a new metric such as a short-fragment ratio or
  10-bp periodicity.

## Two rules before sharing anything

1. Write down each BAM's provenance and consent before showing any record from
   it to anyone outside the team.
2. Keep donor identifiers out of labels, file names you share, and screenshots.
