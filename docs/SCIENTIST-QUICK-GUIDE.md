# Traceback quick guide for scientists

The short version of the [Lab guide](LAB-GUIDE.md): take a Nanopore kit
from MinKNOW output to a local page of cfDNA measurements.

> Research prototype. **Research use only. Not CLIA-validated. Not a
> diagnosis.** Everything stays on your machine; nothing is uploaded.

## What you get

`traceback lab run` aligns your chunks once and runs 35 registered
algorithms: QC and depth, fragment size and end patterns, copy number,
methylation and cell origin, nucleosome profiles, and a few exploratory
signals. Each one either reports a result or says exactly why it did not run
(too little depth, missing asset, wrong platform). Results below a method's
depth floor can be run with `--below-depth`; they are then marked
**underpowered** and are never shown as a call.

Which methods run on a real ONT kit today, and what blocks the others, is in
[Algorithm status on real data](LAB-GUIDE.md#algorithm-status-on-real-data).
What each method measures is in the
[algorithm explainer](LAB-ALGORITHM-EXPLAINER.md).

## One-time setup (macOS)

```bash
brew install samtools minimap2
git clone https://github.com/danwiggins/cfddemo.git traceback && cd traceback
uv sync
export TRACEBACK_ROOT="$HOME/traceback-lab"     # one store for everything
uv run traceback reference register --fasta <hg38.fa> --id hg38
uv run traceback lab doctor
```

`lab doctor` lists anything missing with the command that fixes it. Try the
synthetic example first if you like: `uv run traceback lab run --example`.

Most methods need an extra tool or a reference file you supply (ichorCNA for
copy number, the Loyfer atlas for cell origin, GENCODE for the TSS profile,
and so on). The table in
[Running on real data](LAB-GUIDE.md#running-on-real-data) lists each one with
its registration command. Skip the ones you do not need; their methods just
report `missing_asset`.

## From MinKNOW output to results

MinKNOW and Dorado write unaligned BAM chunks. The Lab aligns them for you;
point it at one barcode's folder, `bam_pass/barcodeNN/` (or `bam_pass/` for
an unbarcoded run). Never mix barcodes in one run.

```bash
uv run traceback lab inventory <chunks-dir>                  # is the run complete?
uv run traceback lab run <chunks-dir> --below-depth --memory-cap 8GiB
uv run traceback lab serve --open                            # opens the local page
```

The first run builds a minimap2 index of hg38 (about 20–40 minutes and 8 GB,
once) and aligns the chunks; later runs reuse both. If you stop a run, run
the same command again: finished results are kept. `traceback lab run latest`
reruns on the last sample without the chunk paths.

On the page, start with **Check reliability** (QC, depth, split-half
repeatability) before **Explore signals**. To share one sample as a file:

```bash
uv run traceback lab report --html <id12>
```

## When something goes wrong

Every refusal prints **CODE**, **CAUSE**, **FIX** and a link into the
[Lab guide troubleshooting list](LAB-GUIDE.md#troubleshooting). Common ones:

| Code | Meaning | Fix |
|---|---|---|
| `TBX-LAB-010` | Not enough free disk for the run | Free space, or use a ROOT on a larger volume |
| `TBX-LAB-012` | Another Lab command holds the writer lock | Wait for it, then rerun |
| `TBX-LAB-016` | The run finished but some algorithms failed | Read the `FAILED` lines; rerun the same command after fixing |
| `TBX-LAB-030` | An algorithm hit its memory cap | Rerun with the `--memory-cap` value the FIX line prints |

`traceback lab support-bundle <run_id>` writes a scrubbed archive of one run
for asking for help.

## Signed records (separate, older path)

`traceback run` turns one aligned BAM into a development-signed
fragment-length record (optionally cell-origin and ichorCNA copy number) with
its own browser view. It is narrower than the Lab and documented in the
[operator guide](OPERATOR-GUIDE.md), including
[aligning MinKNOW output by hand](OPERATOR-GUIDE.md#aligning-minknow-output).

## Two rules before sharing anything

1. Write down each sample's provenance and consent before showing any result
   from it to anyone outside the team.
2. Keep donor identifiers out of labels, file names you share, and
   screenshots. Lab reports and support bundles are scrubbed of paths, but you
   are responsible for what you send.
