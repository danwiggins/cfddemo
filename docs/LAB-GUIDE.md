# Traceback Lab guide

Traceback Lab runs every cfDNA algorithm a kit BAM can support from one
command, and a local page explains each result. It is a separate, **unsigned**
subsystem: results carry a provenance stamp (tool, version, parameters hash,
input hash, timestamp) and nothing else. The signed `traceback run` path is
unchanged.

**Research use only. Not CLIA-validated. Not a diagnosis.**

This guide grows with the Lab. Sections for `lab run`, `lab serve`,
`lab doctor` and `lab run --example` arrive with the commands themselves.

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
traceback lab algorithms [--json]          registered algorithms (id, family, cost, inputs)
traceback lab runs [RUN_ID] [--step S]     the run log, or one run's steps
```

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

### TBX-LAB-012

Another Lab writer is running (exit 3, retryable). Only one writer (`lab run`,
`lab simulate`, `lab cohort add`, `lab cohort build`) may hold `ROOT/lab/.lock`
at a time. The cause names the holder's command, run id, pid, step and start
time. Wait for it (`traceback lab runs`), or stop that process.

### TBX-LAB-017

A run id, sample `id12` or `latest` was not found, or a prefix is ambiguous
(exit 4). `traceback lab runs` lists runs. Pass the same `--root` (or
`TRACEBACK_ROOT`) you ran with.

### TBX-LAB-018

`ROOT/lab/STORE_VERSION` is newer than this checkout (exit 3). Update the
checkout (`git pull && uv sync`). The store is never downgraded.

### TBX-LAB-020

Unexpected internal error (exit 7). Run `traceback lab runs RUN_ID`, then
report the code and the command.
