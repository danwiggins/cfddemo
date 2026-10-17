# Griffin pinned Snakemake synthetic workflow proof

This is a synthetic execution receipt for the Griffin foundation, not a qualified
human hg38 or 5-million-pair Lab result. The data were 500 generated
paired-end fragments, a 30 kb toy chr1 FASTA, one TF-like site, synthetic
GC-frequency tables and a constant-one mappability track. The input setup
is `generate_toy.py`; the workflow configuration is
`setup_workflows.py`, both beside this note. Both scripts accept `--base-dir`
and default to a `cfdna-griffin` directory under the system temporary directory.
The recorded run used an ephemeral scratch directory; its absolute location
was not part of the scientific qualification.

The POSIX checkout was at exact upstream commit
`b624c7a2ea7b8758b6eebd424066c815bfeece61`. Both original upstream
Snakefiles ran under Snakemake 5.19.2. Python 3.11 requires an attempt-local
`sitecustomize.py` shim for Snakemake's old `ratelimiter`
dependency (`asyncio.coroutine`); no production code or upstream checkout
was patched for that. The shim is first on `PYTHONPATH` and can shadow another
environment's `sitecustomize.py`; it is not a general Python compatibility proof.
`setup_workflows.py` copies complete workflow directories and caps the
synthetic rule pools at one CPU in its copied cluster configs.
These were the two workflow invocations, shown with a configurable proof base.
The proof virtual environment supplied `snakemake`, and the copied compatibility
shim was first on `PYTHONPATH`:

```sh
PROOF_BASE="$(mktemp -d)"
"$PROOF_PYTHON" docs/proofs/griffin/generate_toy.py --base-dir "$PROOF_BASE"
"$PROOF_PYTHON" docs/proofs/griffin/setup_workflows.py --base-dir "$PROOF_BASE" --upstream "$UPSTREAM"
export PYTHONPATH="$PROOF_BASE/compat${PYTHONPATH:+:$PYTHONPATH}"
"$SNAKEMAKE" -s "$PROOF_BASE/snakemake-proof/griffin_GC_and_mappability_correction/griffin_GC_and_mappability_correction.snakefile" --directory "$PROOF_BASE/snakemake-proof/griffin_GC_and_mappability_correction" --cores 1
"$SNAKEMAKE" -s "$PROOF_BASE/snakemake-proof/griffin_nucleosome_profiling/griffin_nucleosome_profiling.snakefile" --directory "$PROOF_BASE/snakemake-proof/griffin_nucleosome_profiling" --cores 1
```

Set `UPSTREAM` to a clean checkout of the pinned commit, `PROOF_PYTHON` to the
proof environment's Python, and `SNAKEMAKE` to its Snakemake 5.19.2 executable
before these commands.

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
bytecode into the pinned checkout. The Lab adapter copies the Snakefiles,
scripts and pinned cluster settings into a fresh attempt, caps cluster rule
CPUs to the requested cores, and sets `PYTHONDONTWRITEBYTECODE=1` for workflow
subprocesses.

## Adapter-generated configuration proof

`adapter_smoke.py` exercised the actual `run_upstream` entry point on the same
30 kb toy chr1 fixture and clean pinned upstream checkout. It added a declared
Illumina read group to the 500-pair BAM, supplied synthetic GC-frequency files
for every length 15–500 and an inert toy exclusion interval, and set only the
in-process minimum-pair constant to 500; the production gate remains five
million. The adapter generated and hashed seven YAML files, including both
cluster configs capped at one core.
Its `chroms` setting came from the validated chr1 chrom-sizes input. Reproduce
in the isolated Linux environment with `generate_toy.py` run first, a clean
checkout of commit `b624c7a2ea7b8758b6eebd424066c815bfeece61`,
Snakemake 5.19.2 and the required bioinformatics tools. The variables below
refer to that environment's Python, Snakemake and tool directories:

```sh
PYTHONPATH="$PWD" "$PROOF_PYTHON" \
  docs/proofs/griffin/adapter_smoke.py \
  --proof-dir "$PROOF_BASE/proof" \
  --upstream "$UPSTREAM" \
  --snakemake "$SNAKEMAKE" \
  --tool-dir "$BIO_TOOL_DIR" \
  --work-parent "$PROOF_BASE"
```

The recorded attempt was generated beneath an ephemeral scratch directory by
the checked-in proof script above.
`provenance.json` has status `complete`, pinned upstream commit, 500 verified
D21 pairs, one core, the Python 3.11.17 / Snakemake 5.19.2 runtime and exact
input, asset, script and generated-config fingerprints. Both original pinned
workflow logs report 4/4 jobs complete and exit zero. The adapter's strict
parser returned one `ToySite` profile with central coverage 2.92846. The
generated corrected TSV SHA-256 is
`8a72d21d3c0dcb3f90eeb94c08bb47d3efa97ed12daf616ff22ec3551209d594`;
the final provenance SHA-256 is
`d3114e6b01bc173bbfec26f7b1fa1e9811e788b2f6cd59d7be151947eb963621`.
The adapter output and process-pool behavior now have a bounded mechanics
proof; no human hg38 or full-depth result is implied.

The complete human hg38 asset registration and 5-million-pair normal Lab
run remain open qualification steps.
In a normal Lab run, Griffin's full BAM pairing, window and D21 pair gates
execute in the timed worker after the lightweight preflight. A derived
downsample or split selection is counted from its temporary selected BAM;
insufficient pairs yield a depth skip. This synthetic proof does not measure
the runtime or memory of a full-depth gate.
