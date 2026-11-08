- 2026-11-07: `lab run` no longer aborts with TBX-LAB-020 (`ResultOrderUnavailable`) when a
  cached result cannot be safely reselected because a sibling result for the
  same algorithm is unreadable (for example one written by an older Lab
  version). The step is recomputed and published as a fresh, ordered result,
  and the progress line says `cache not reusable (unreadable sibling result);
  recomputing`.
