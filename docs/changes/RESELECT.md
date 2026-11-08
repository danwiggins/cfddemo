- 2026-11-07: `lab run` reuses a cached result that is already the newest
  selection even when an older sibling result for the same algorithm is
  unreadable (for example one written by an older Lab version); before, the
  second run on such a store aborted with TBX-LAB-020 (`ResultOrderUnavailable`).
  When a cached result would need re-selecting and that is unsafe, the step is
  recomputed (`cache not reusable (<reason>); recomputing`) instead of aborting.
  The Lab page and report still withhold that algorithm while the unreadable
  older selection remains on disk, because it cannot be ordered against the
  new result; move that identity directory out of `ROOT/lab/results` to show it.
