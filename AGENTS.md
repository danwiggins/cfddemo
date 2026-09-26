# AGENTS.md — Traceback

Guidance for Codex (and other coding agents) working in this project.

## What this project is

Traceback is a local-first Streamlit prototype for auditing scientific claims
against bounded deterministic evidence and cited source passages. It uses
Amazon Bedrock's OpenAI-compatible responses endpoint with SigV4 auth. The
original terminal chat remains available as a transport example.

## Layout

- `app.py` — Streamlit interface and evidence display.
- `evidence_inspector/` — strict contracts, BAM preparation, deterministic
  checks, case loading, review orchestration, and UI state.
- `bedrock_chat/responses.py` — stateless deadline-aware Bedrock adapter.
- `bedrock_chat/` — preserved terminal chat and shared transport helpers.
- `scripts/prepare_local_case.py` — bounded local BAM preparation.
- `evals/` — synthetic six-case semantic evaluation harness.
- `tests/` — fully offline pytest suite.

## Run it

```bash
uv sync
uv run pytest
uv run streamlit run app.py
```

Config comes from the environment: `BEDROCK_MODEL_ID`, `BEDROCK_REGION`
(falls back to `AWS_REGION` / `AWS_DEFAULT_REGION`), `BEDROCK_MAX_TOKENS`,
`BEDROCK_TEMPERATURE`, and optional `TRACEBACK_CASE_PATH`.

## Test

```bash
uv run pytest
uv run python -m evals.harness
```

Tests must never require live AWS access — keep new tests offline by injecting a
fake client or exercising pure helpers.

## Conventions for changes

- Never commit `data/local/`, BAMs, supplied documents, credentials, read IDs,
  absolute local paths, or live evaluation results.
- Keep raw sequence and identifiers out of model prompts; send only curated
  excerpts and aggregate evidence.
- Preserve explicit metric definitions, denominators, filters, units, sample
  linkage, trimming state, and partial-collection limits.
- Keep AWS-touching code isolated in `bedrock_chat/`; keep helpers pure and tested.
- `boto3` is imported lazily so the test suite runs without AWS setup — preserve that.
- Add a test with every behavior change; run `pytest` before finishing.
- Match the existing style: type hints, module docstrings, small focused functions.
