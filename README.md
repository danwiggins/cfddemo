# Traceback

Bring your cfDNA sequencing data, run it against every publicly available
cfDNA algorithm we have wrapped, get charts, and look for aberrations.

**Traceback Lab** takes a BAM, an unaligned BAM, or a MinKNOW chunk
directory, aligns it if needed, and runs 35 cfDNA methods on it, most from published tools:
fragment size, copy number, methylation, cell and tissue origin, end
motifs, nucleosome profiles, QC and more. Each method either reports a
result with its charts or says exactly why it did not run. A local web page
and a one-file HTML report show everything, with provenance.

> **Research use only. Not CLIA-validated. Not a diagnosis.** Nothing here is
> a validated clinical assay. The Lab runs locally and uploads nothing.

## Quick start (synthetic data)

Requires macOS or Linux, Python 3.11, [uv](https://docs.astral.sh/uv/),
samtools ≥ 1.20 and minimap2 ≥ 2.28 (`brew install samtools minimap2`).

```bash
uv sync
uv run traceback lab doctor
uv run traceback lab run --example
uv run traceback lab serve --open
```

`--example` generates a small synthetic sample at runtime and runs the real
pipeline on it. Methods that need hg38-scale assets skip with a reason.

## Real data

```bash
export TRACEBACK_ROOT=<lab-root>
uv run traceback reference register --fasta <hg38.fa> --id hg38
uv run traceback lab doctor
uv run traceback lab inventory <chunks-dir>
uv run traceback lab run <chunks-dir> --below-depth --memory-cap 8GiB
uv run traceback lab serve --open
```

Most methods need extra tools or operator-supplied assets (ichorCNA, the
Loyfer atlas, GENCODE v46, a Somalier sites file, Griffin references and
others). The [Lab guide: Running on real data](docs/LAB-GUIDE.md#running-on-real-data)
lists each one and how to register it, and
[Algorithm status on real data](docs/LAB-GUIDE.md#algorithm-status-on-real-data)
shows which methods run on a real ONT kit today and what blocks the rest.

## Documentation

- [Lab guide](docs/LAB-GUIDE.md): commands, real-data setup, every algorithm,
  error codes.
- [Lab quick guide for scientists](docs/SCIENTIST-QUICK-GUIDE.md): the short
  version.
- [Algorithm explainer](docs/LAB-ALGORITHM-EXPLAINER.md): what each method
  measures and what it cannot tell you.
- [Algorithms reference](docs/ALGORITHMS.md) and
  [Lab assets](docs/ASSETS.md).
- [All documentation](docs/README.md), including the signed-record path and
  historical design documents.

## Other surfaces in this repository

- **Signed local records** (`traceback run`): an older, narrower path that
  turns one aligned BAM into a development-signed fragment-length (and
  optional cell-origin or ichorCNA copy-number) record. See the
  [operator guide](docs/OPERATOR-GUIDE.md).
- **Historical: hackathon demo** (`app.py`, Streamlit): an early
  proof-of-concept with three fixed readouts and an AI review step. It is no
  longer maintained or relevant to the Lab; see [DEMO.md](docs/DEMO.md) only
  if you need it.

## Tests

```bash
uv run pytest
```

Tests are offline and use synthetic fixtures only.

## Privacy boundary

Never commit `.env` files, credentials, sequence files, BAMs, read IDs, source
reports, local paths, or patient or sample identifiers. Lab results, assets
and run logs stay under your local ROOT. The historical demo sends only bounded excerpts and aggregate check
results to its model.
