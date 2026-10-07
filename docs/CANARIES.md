# Golden-path canaries

Two canaries exercise the Milestone 1 golden path (`traceback reference
register` → `preflight --reference` → `run` → `verify` → `report.html`) on a
schedule, so a regression shows up the next morning, not at the next manual
run. Every record they make is unqualified, local, not for clinical use and
signed with a development key only. Neither one qualifies anything.

| Canary | Where | Inputs | Baseline |
|---|---|---|---|
| Real-data | Operator's macOS workstation, launchd, daily 03:30 local | A real FASTA + BAM | Local only, `~/.config/traceback-canary/baseline.json` |
| Synthetic | GitHub Actions `canary` workflow, nightly, macOS + ubuntu | Generated 2-contig FASTA + 1,000-read BAM | Recorded in the job, plus the committed `tests/fixtures/canary/synthetic_baseline.json` |

## What each run checks

`scripts/canary/real_bam_canary.py` runs these steps, each repeat in a fresh
temporary root:

1. `traceback reference register` (the FASTA, ID `ref`);
2. `traceback preflight BAM --reference ref`: exit 0, outcome not `blocked`, fragment-measurement eligible;
3. `traceback run BAM --reference ref`: exit 0, `data_origin: local_unqualified`, published at `records/<record_id>`;
4. `traceback verify RECORD --trust-store …`: exit 0, `verified: true`;
5. `report.html` carries the local banner and never says "synthetic";
6. locator check: neither input path, nor its directory, appears in the record, the report or any command output (including `logs` and `status`).

It collects these deterministic metrics:

- exit codes per step;
- the preflight outcome and each check's code, outcome and artifact role;
- `records_scanned`, `eligible_alignments` and every exclusion count;
- the count in each histogram bin;
- the SHA-256 of the canonical measurement JSON
  (`measurements/fragment-length.v1.json`). That file has no per-run field (no
  timestamp, job, key or record ID; two fresh roots give byte-identical files),
  so it is hashed whole. Record and job IDs differ per root and are not part of
  the metrics.

It also records the wall time of each step.

With `--repeat N` (N > 1), every repeat must give identical deterministic
metrics. That is the reproducibility check.

Then it compares against the baseline:

- **Fail** on any difference in counts, codes, check outcomes, histogram bins or
  the measurement digest, or when the inputs' size or SHA-256 changed. A
  readable per-field diff is printed, e.g.
  `drift from baseline: measurement.eligible_alignments: expected 867, got 866`.
- **Warn** (exit 0) when a step takes more than 2x its baseline wall time.
- **Fail** when no baseline exists (unless recording one).

Exit codes: `0` pass (warnings allowed), `1` fail, `2` refused (bad usage, an
existing baseline without `--force`, a `--work-dir` under an input's directory,
or a baseline being recorded, a log directory or a `--work-dir` inside any Git
work tree).

## Record a baseline

Run once by hand, after a run you have checked:

```bash
uv run python scripts/canary/real_bam_canary.py \
  --fasta /path/to/ref.fa --bam /path/to/sample.sorted.bam \
  --record-baseline --repeat 2
```

The baseline is written to `~/.config/traceback-canary/baseline.json` (or
`--baseline PATH`, or `$TRACEBACK_CANARY_BASELINE`) with mode 0600. It is only
written when every check passed and, with `--repeat`, the runs agreed. An
existing baseline is never overwritten unless you add `--force`; do that only
after an intended change (new inputs, a deliberate measurement-contract change)
and say why in the PR or log.

Other options: `--log-dir`, `--work-dir` (parent of the temporary roots; it
needs about twice the BAM size free), `--keep` (keep the temporary roots for
inspection), `--step-timeout SECONDS`, `--no-notify`.

## Cell origin (`--analysis cell-origin`)

`--analysis cell-origin` (default `fragment`, unchanged) makes a cell-origin
record instead of the fragment record. `--loyfer-dir DIR` (required) names the
directory with the three Loyfer files, registered in each fresh root. Pass
`--modbase-model ID` when the BAM header does not declare the modified-base
model. modkit must be installed (`traceback toolchain install modkit`).

```bash
uv run python scripts/canary/real_bam_canary.py \
  --fasta /path/to/ref.fa --bam /path/to/sample.sorted.bam \
  --analysis cell-origin --loyfer-dir /path/to/loyfer --modbase-model MODEL \
  --record-baseline --repeat 2
```

- The measurement path is read from the record's bundle manifest.
- Metrics: the canonical measurement SHA-256, the denominators, every
  contributor fraction and `residual_l2`. Fractions are compared exactly; a
  tolerance would be a scientist-approved method parameter, never a canary
  setting.
- The baseline is `baseline-cell-origin.json` beside `baseline.json`, mode 0600,
  refused inside a Git work tree like the fragment baseline.
- No fraction is printed to the console (gate G1); only counts are.
- The nightly launchd canary stays fragment-only until a cell-origin baseline
  exists. The synthetic CI canary does not run cell origin yet: it needs modkit
  in CI.

## Install, uninstall, status (launchd)

```bash
scripts/canary/install_canary.sh install --fasta /path/to/ref.fa --bam /path/to/sample.sorted.bam
scripts/canary/install_canary.sh status
scripts/canary/install_canary.sh uninstall
```

`install` refuses unless the baseline exists, renders
`scripts/canary/com.traceback.real-bam-canary.plist.template` with the `uv`
binary, the repository path, the absolute input paths, the baseline and log
paths and `--repeat 2`, writes it to
`~/Library/LaunchAgents/com.traceback.real-bam-canary.plist` and loads it with
`launchctl bootstrap gui/$(id -u)`. The agent runs daily at 03:30 local time
(if the Mac is asleep then, launchd runs it on wake). `install` accepts `--baseline`,
`--log-dir` and `--repeat`. `render` prints the plist without installing it.

`status` shows whether the agent is loaded, launchd's last exit code, and the
last result summary. `uninstall` runs `launchctl bootout` and removes the plist;
it keeps the baseline and logs.

The canary runs from the repository checkout, so it tests whatever is checked
out there at 03:30.

## Where the logs live

- `~/Library/Logs/traceback-canary/<UTC timestamp>.json`: one result per run
  (mode 0600), plus `latest.json`.
- `~/Library/Logs/traceback-canary/launchd.out.log` / `launchd.err.log`: the
  console output of scheduled runs.

A result holds the status, the inputs' size and SHA-256, each run's metrics,
wall times and failures, the measurement digests, `reproducible`, the baseline
diff, warnings and failures. It never holds an absolute input path.

## When it fails

A failed run exits 1, writes the result and posts a macOS notification
(best effort; skipped without `osascript`). Read `status` or `latest.json`:

| Failure | Meaning | What to do |
|---|---|---|
| `step X exited N` | A CLI step failed | Rerun by hand with `--keep`; read the step's output in the kept root's `out/` |
| `drift from baseline: …` | A count, code or the measurement digest changed for the same inputs | Find the commit that changed it (`git log` since the last pass). If the change is intended, re-record with `--force`; if not, it is a regression |
| `inputs changed since the baseline` | The FASTA or BAM is not the file the baseline was recorded from | Restore the inputs, or re-record the baseline for the new ones |
| `not reproducible: run 1 vs run 2` | The same inputs gave different metrics in two fresh roots | A determinism bug; treat as a regression |
| `report.html lacks the local banner` / `mentions synthetic` | Report labelling regressed | Regression in report rendering |
| `an input path appears in …` | A locator leaked into the record or output | Privacy regression; fix before sharing any record |
| `no baseline` | Nothing to compare with | Record one (above) |

A slow-step warning alone is not a failure; look at it if it repeats.

The synthetic CI canary fails the `canary` workflow run in GitHub Actions. If
the committed synthetic baseline drifts because of an intended change, record a
fresh one from the generated fixture and commit it with `inputs` and
`wall_seconds` set to `null` (so it compares across macOS and Linux):

```bash
uv run python -c "from pathlib import Path; from traceback_runner.fixtures import create_local_golden_path_inputs as c; c(Path('/tmp/canary-in'))"
uv run python scripts/canary/real_bam_canary.py --fasta /tmp/canary-in/golden-reference.fa \
  --bam /tmp/canary-in/golden-aligned.bam --baseline /tmp/canary-baseline.json \
  --log-dir /tmp/canary-logs --record-baseline --repeat 2
# copy /tmp/canary-baseline.json to tests/fixtures/canary/synthetic_baseline.json,
# set "inputs" and "wall_seconds" to null, and explain the change in the PR.
```

`tests/test_canary.py` also checks the committed synthetic baseline on every PR.

## Privacy

The repository is public. The real-data baseline and the logs hold counts from
a real sample, so they stay on the workstation (`~/.config/traceback-canary/`,
`~/Library/Logs/traceback-canary/`, both private to the user) and are never
committed or uploaded. Results identify inputs by size and SHA-256 only, and
console output replaces input paths with `<FASTA>` / `<BAM>`. The rendered
launchd plist does hold the absolute input paths; it stays in
`~/Library/LaunchAgents`. Only synthetic numbers are committed.

The canary and the installer refuse to write a baseline, logs, temporary roots
or the plist inside any Git work tree (paths are fully resolved, so symlinks and
`..` do not get around it).

On the 2.1 GB development BAM one pass takes about 3-5 minutes (input hashing,
register, preflight, run); the scheduled `--repeat 2` run about twice that.
