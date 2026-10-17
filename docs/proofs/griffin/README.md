# Griffin pinned Snakemake synthetic workflow proof

This is a synthetic execution receipt for draft PR #194, not a qualified
human hg38 or 5-million-pair Lab result. The data were 500 generated
paired-end fragments, a 30 kb toy chr1 FASTA, one TF-like site, synthetic
GC-frequency tables and a constant-one mappability track. The input setup
is `generate_toy.py`; the workflow configuration is
`setup_workflows.py`, both beside this note. These scripts use the
`/var/tmp/cfdna-griffin` layout of the recorded run; that path can be
changed before replaying them.

The POSIX checkout was at exact upstream commit
`b624c7a2ea7b8758b6eebd424066c815bfeece61`. Both original upstream
Snakefiles ran under Snakemake 5.19.2. Python 3.11 requires the isolated
`sitecustomize.py` shim for Snakemake's old `ratelimiter`
dependency (`asyncio.coroutine`); no production code or upstream checkout
was patched for that. The synthetic configs capped rule pools at one CPU.
These were the two invocations, each with the isolated virtual environment
and `PYTHONPATH=/var/tmp/cfdna-griffin/compat`:

```
snakemake -s /var/tmp/cfdna-griffin/snakemake-proof/griffin_GC_and_mappability_correction/griffin_GC_and_mappability_correction.snakefile --directory /var/tmp/cfdna-griffin/snakemake-proof/griffin_GC_and_mappability_correction --cores 1
snakemake -s /var/tmp/cfdna-griffin/snakemake-proof/griffin_nucleosome_profiling/griffin_nucleosome_profiling.snakefile --directory /var/tmp/cfdna-griffin/snakemake-proof/griffin_nucleosome_profiling --cores 1
```

The GC workflow completed 4/4 jobs; the profiling workflow completed 4/4
jobs, including coverage, site merging and plotting. The Lab's strict
corrected-profile parser accepted the output with one `ToySite` profile,
132 bins and central coverage 2.92846. The corrected TSV SHA-256 is
`80119c9756431fce599f87103248b07aef993b6263c0ce54013674b8488d6180`.
The resulting bias table, corrected TSV and plot are in the sibling
`griffin-snakemake-proof/` folder. Contention on the shared WSL host made
these toy jobs unusually slow; the receipt is an execution proof, not a
production runtime estimate.

This run exposed two adapter concerns: upstream cluster settings requested
eight worker processes despite `--cores 1`, so this manual proof capped
the copied rule settings at one CPU; and a standalone run wrote Python
bytecode into the pinned checkout. The Lab adapter copies only the needed
Snakefiles and scripts into a fresh attempt and sets
`PYTHONDONTWRITEBYTECODE=1` for workflow subprocesses.
The complete human hg38 asset registration and 5-million-pair normal Lab
run remain open qualification steps.
