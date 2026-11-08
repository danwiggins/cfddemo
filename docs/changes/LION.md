- 2026-11-08: `lionheart` runs end to end on macOS. On macOS the mosdepth
  library directory must hold `libhts.dylib` (`libhts.so` elsewhere), and the
  guide gives the macOS build of the modified mosdepth against Homebrew
  htslib. The LIONHEART CLI gets the operator's `PYTHONPATH` back instead of
  the Lab worker's (which broke a LIONHEART environment on another Python
  version). Extraction passes `--n_jobs 4` because upstream's `--n_jobs 1`
  hands `unpigz -p 0`, which fails, and runs upstream's joblib feature tasks
  in-process one at a time (`JOBLIB_MULTIPROCESSING=0`). Its default memory
  cap starts at a 24 GiB floor (14.8 GiB measured peak). The guide notes
  LIONHEART needs SciPy below 1.14 and that `--memory-cap 8GiB` or a machine
  with under about 19 GiB RAM stops it. A new test covers existing behaviour:
  `lab run --below-depth` already ran it below the 5M eligible-read gate as an
  underpowered result.
