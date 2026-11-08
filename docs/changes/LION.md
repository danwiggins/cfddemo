- 2026-11-08: `lionheart` runs end to end on macOS. The mosdepth library
  directory may hold `libhts.dylib` as well as `libhts.so`, and the guide gives
  the macOS build of the modified mosdepth against Homebrew htslib. The
  LIONHEART CLI no longer inherits the Lab worker's `PYTHONPATH` (which broke a
  LIONHEART environment on another Python version), and extraction passes
  `--n_jobs 4` because upstream's `--n_jobs 1` hands `unpigz -p 0`, which
  fails; its joblib feature workers run one at a time
  (`JOBLIB_MULTIPROCESSING=0`), and its default memory cap starts at a
  24 GiB floor (measured ~15 GiB peak). The guide notes LIONHEART needs SciPy
  below 1.14 and that `--memory-cap 8GiB` stops it.
  `lab run --below-depth` runs it below the 5M eligible-read gate as an
  underpowered result, as for other depth-floored methods.
