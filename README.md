# Traceback

Traceback is a local-first cfDNA evidence-inspection prototype. It connects a
source-backed claim to a bounded deterministic check, measured evidence, and a
cited AI assessment. The Streamlit application is under construction.

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
