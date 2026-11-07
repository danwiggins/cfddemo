# AGENTS.md — Traceback

Guidance for Codex (and other coding agents) working in this project.

## What this project is

Traceback has three local surfaces: the public Streamlit evidence-review demo,
the signed but development-unqualified `traceback` runner, and the separate,
unsigned research `traceback lab` workflow. The demo can use Amazon Bedrock's
OpenAI-compatible responses endpoint with SigV4 auth or replay checked-in
assessments. Neither local workflow grants clinical or release qualification.

## Layout

- `app.py` — Streamlit interface and evidence display.
- `evidence_inspector/` — strict contracts, BAM preparation, deterministic
  checks, case loading, review orchestration, and UI state.
- `bedrock_chat/responses.py` — stateless deadline-aware Bedrock adapter.
- `bedrock_chat/` — preserved terminal chat and shared transport helpers.
- `scripts/prepare_local_case.py` — bounded local BAM preparation.
- `evals/` — synthetic six-case semantic evaluation harness.
- `tests/` — fully offline pytest suite.
- `traceback_runner/` — local signed development runner, contracts, receipts,
  trust, result explorer, and CLI.
- `traceback_lab/` — unsigned cfDNA algorithm registry, local store, worker,
  cohort/PoN handling, and Lab CLI/web view.
- `docs/LAB-GUIDE.md` — Lab operations and research limitations.

## Run it

```bash
uv sync
uv run pytest
uv run streamlit run app.py
uv run traceback lab doctor
uv run traceback lab run --example
```

The Streamlit demo's live review config comes from the environment:
`BEDROCK_MODEL_ID`, `BEDROCK_REGION`
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
- Cover behavior changes with meaningful offline tests. Run the relevant
  focused suite and the applicable CI checks before finishing.
- Match the existing style: type hints, module docstrings, small focused functions.

## Current CI boundary

`.github/workflows/ci.yml` runs fast offline tests on macOS and Ubuntu with
Python 3.11. It installs `samtools` and `minimap2` on both hosts, runs `uv sync`,
checks that `uv.lock` remains unchanged, then runs Ruff and the non-slow pytest
suite with xdist. The separate `slow` and `canary` workflows have their own
triggers; CI fixtures and a green build do not establish real-input,
workstation, protocol, or clinical qualification.
