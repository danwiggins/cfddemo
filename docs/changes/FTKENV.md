- 2026-11-07: `finaletoolkit-xcheck` no longer fails with "FinaleToolkit tool
  changed since execution planning" inside `lab run`: FinaleToolkit's probe and
  commands now run without the Lab worker's `PYTHONPATH`/`PYTHONHOME`, so its
  own interpreter does not import the Lab's packages. On a full-size input it
  can need more than the 2 GiB default memory cap; rerun with
  `--memory-cap 8GiB` as the failure's FIX line says.
