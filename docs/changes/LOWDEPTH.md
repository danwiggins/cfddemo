- 2026-11-07: `lab run --below-depth` (exploratory) runs methods below their
  depth floor instead of skipping with reason `depth`. Each such result is
  marked `underpowered` (floor, observed, kind) in its result JSON, the run
  line, `--json`, the run log flags, the Lab page and `lab report --html`, and
  its metrics are `not_interpretable (depth)`. Prenatal/transplant calibration
  floors and MethylBERT's qualified minimum still skip. Default runs are
  unchanged.
