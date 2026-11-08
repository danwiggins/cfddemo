# AGENTS.md — Traceback

Guidance for Codex (and other coding agents) working in this project.

## What this project is

Traceback is a local cfDNA research workbench. The main surface is the
unsigned `traceback lab` workflow: bring a BAM, uBAM or MinKNOW chunk
directory, run every registered public cfDNA method on it, and read the
results and charts on a loopback web page or in a one-file HTML report. The
older signed but development-unqualified `traceback run` path is still in the
repository and tested. Neither workflow grants clinical or release
qualification.

## Layout

- `traceback_lab/` — unsigned cfDNA algorithm registry, ingest and alignment,
  local store, worker, cohort/PoN handling, and Lab CLI/web view.
- `traceback_runner/` — local signed development runner, contracts, receipts,
  trust, result explorer, and CLI.
- `evidence_inspector/` — strict contracts, method adapters and protected
  registries shared by the runner and the Lab.
- `scripts/` — fixture regeneration, canaries and validation helpers.
- `tests/` — fully offline pytest suite.
- `docs/LAB-GUIDE.md` — Lab operations and research limitations.

## Run it

```bash
uv sync
uv run traceback lab doctor
uv run traceback lab run --example
uv run traceback lab serve --open
```

## Test

```bash
uv run pytest
```

Tests are offline and use synthetic fixtures only.

## Conventions for changes

- Never commit `data/local/`, BAMs, supplied documents, credentials, read IDs,
  or absolute local paths.
- Preserve explicit metric definitions, denominators, filters, units, sample
  linkage, trimming state, and partial-collection limits.
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
