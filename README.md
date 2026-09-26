# Traceback

Traceback is a local-first cfDNA evidence-inspection prototype. It connects a
source-backed claim to a bounded deterministic check, measured evidence, and a
cited AI assessment. It includes algorithmically regenerated fragment-length
and cell-origin charts.

The existing `bedrock_chat` terminal client remains available as the Bedrock
responses-API transport example.

## Setup

Requires Python 3.11 and
[uv](https://docs.astral.sh/uv/).

```bash
uv sync
uv run pytest
```

Run the app:

```bash
uv run streamlit run app.py
```

The public deployment uses de-identified aggregate demo bundles under
`data/demo/`. They contain binned measurements, cell-mixture estimates, and
provenance metadata only—never BAMs, read IDs, local paths, or source documents.
Private local results under `data/local/` take precedence when present.

## Cell-origin regeneration

The local pipeline implements:

```text
aligned modBAM → CpG extraction → Loyfer marker overlap → fragment UXM
→ count-weighted NNLS → seeded bootstrap → healthy-plasma range chart
```

Required local inputs are intentionally ignored by Git:

- hg38 primary FASTA and minimap2 index under `data/local/reference/`
- `Regions.U250.l4.hg38.bed`, `Markers.U250.hg38.tsv`, and
  `Atlas.U250.l4.hg38.full.tsv` under `data/local/loyfer/`
- official Loyfer supplementary workbook, sheet `Table S8`
- either an aligned modBAM with valid `MM`, `ML`, and `MN` tags, or a
  normalized `modkit extract calls` TSV

For a normalized extract:

```bash
TRACEBACK_FRAGMENT_HASH_SALT='local-private-value' \
  uv run python scripts/regenerate_cell_origin.py \
  --extract-tsv data/local/cell-origin/calls.normalized.tsv
```

The result is atomically validated at
`data/local/cell-origin/result.json`. The app plots the highest estimated
contributors and a horizontal comparison against the observed 23-donor
Loyfer plasma distribution: min–max, IQR, median, sample estimate, and
bootstrap interval. The output is analytical reconstruction, not diagnosis.

The existing terminal chat can still be run with:

```bash
uv run python -m bedrock_chat
```

## Evaluation

Run the six synthetic semantic cases offline:

```bash
uv run python -m evals.harness
```

After model and Region access are verified, run the same cases live with
`uv run python -m evals.harness --live`. Metadata-only live results are written
under ignored `evals/results/`; never commit live evaluation records.

## Bedrock configuration

AWS credentials use the standard SDK credential chain. Runtime settings are
environment-driven:

| Variable | Default |
|---|---|
| `BEDROCK_MODEL_ID` | `openai.gpt-5.5` |
| `BEDROCK_REGION` | `us-east-2` |
| `BEDROCK_MAX_TOKENS` | `1024` |
| `BEDROCK_TEMPERATURE` | unset |

Model and Region access must be verified locally before a live evaluation.

## Privacy boundary

Place BAMs, supplied reports, curated private excerpts, manifests containing
local paths, and generated evaluation records under `data/local/` or another
ignored private-input directory. Never commit `.env*`, sequence files, source
documents, credentials, read IDs, absolute local paths, or patient/sample
identifiers. Only synthetic fixtures and de-identified evaluation definitions
belong in Git.

Tests must remain offline. Raw reads stay local; model requests may contain only
bounded curated excerpts and aggregate check results.
