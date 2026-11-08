- 2026-11-07: `lab run --below-depth` (exploratory) runs methods below their
  depth floor instead of skipping with reason `depth`. Each such result, and
  each result built on an underpowered upstream, is marked `underpowered`
  (floor, observed, kind) in its result JSON, the run line, `--json`, the run
  log flags, the Lab page and the report exports; its metrics are
  `not_interpretable (depth)`, and it never feeds cohorts, stability verdicts
  or spike recovery verdicts. Prenatal/transplant calibration floors and
  MethylBERT's qualified minimum still skip. Default runs are unchanged.
  Forward-only: the optional `underpowered` field is added to
  `traceback.lab.result.v1` without a version bump, so an older Lab rejects
  marked results (`extra="forbid"`); unmarked results keep their bytes.
