# Griffin TF-site profile integration

The approved L2 `griffin` row means a Griffin nucleosome-protection profile
over transcription-factor binding sites, with a minimum of 5,000,000 D21
eligible autosomal molecules. It is a research signal, not a TF expression
call or a cancer classifier. A site-density histogram or an uncorrected
coverage plot is not a Griffin result.

`traceback_lab.adapters.griffin.read_corrected_profiles` reads Griffin's
upstream `*.GC_corrected.coverage.tsv` format. It keeps the original bin-start
coordinates, GC-corrected coverage, central coverage, amplitude, site-list
name/count, smoothing and CNA-normalization state. It rejects uncorrected,
non-finite, malformed, or wrong-sample files. The parser was checked against
the vendored upstream `Healthy_demo.GC_corrected.coverage.tsv` fixture (one CTCF list, 132
15-bp bins) from upstream commit
`b624c7a2ea7b8758b6eebd424066c815bfeece61`.

`traceback_lab.adapters.griffin_run.run_upstream` drives that pinned upstream
checkout, rather than approximating the measurement. It runs Griffin's GC
correction and nucleosome-profiling Snakemake workflows on one Lab aligned
BAM **when the BAM is paired-end WGS**. Griffin's scripts require paired
reads with positive template length in both GC counting and coverage. The
adapter requires at least one BAM read group and every group to declare
`PL:ILLUMINA`; it scans all primary BAM records for pairing and the 100–200 bp
profile-window gate before creating an attempt. It does
not infer library type from an early coordinate-sorted slice. Native ONT
single-end BAMs are rejected. It compares the BAM's `@SQ` names and lengths
and the workflow chromosome sizes with the registered FASTA index; this checks
coordinate-space compatibility, not exact sequence identity. A scientifically reviewed ONT fragment
adapter remains necessary for the Lab's primary input. The caller supplies
the registered hg38 FASTA and its SHA-256, Griffin's
reference assets (including mappability BigWig, exclusion BEDs and all
15–500 bp genome GC-frequency tables), and high-mappability TF-site lists.
It requires the 5M D21 gate, a clean upstream checkout at the pinned commit,
and a fresh per-input attempt directory. The adapter's `eligible_autosomal_pairs`
input is a count of eligible autosomal **pairs**. The adapter independently
recounts first mates of proper, primary, mapped, same-contig, nonduplicate,
non-QC-failed autosomal pairs at MAPQ ≥ 20. This is the paired subset of the
Lab's D21 eligible-record rule; the proper-pair bit depends on the aligner.
The adapter refuses a mismatch before creating an attempt. The attempt
records the verified count, exact counting predicate, sample ID, gate, and hashes for
BAM, BAM index, reference, reference index, site lists and reference assets.
It copies the pinned scripts and two Snakefiles into the attempt without
cached outputs or Python bytecode, records their digests and the upstream
default-config digests, disables bytecode writes during execution, and hashes
all five generated workflow YAML files. It requires Snakemake 5.19.2, records
the resolved Snakemake, Python, samtools, and bedtools executable digests and
the numerical Python package versions, and checks the runtime again after
execution. The workflow PATH uses the Lab Python environment so the upstream
scripts' `#!/usr/bin/env python` resolves to that recorded interpreter. The generated config sets the pinned
upstream default `mappability_correction: false`, so the accepted output is
GC-corrected only. The attempt only marks its manifest complete after Griffin returns
a valid corrected TSV with exactly the requested site-list names. Partial outputs remain
inspectable after failure. Griffin's upstream shell rules interpolate paths
without quoting, so the adapter refuses paths containing shell-special
characters. The runner is POSIX-only, matching upstream's environment.

The Lab now registers the exact upstream checkout, Griffin reference inputs,
and operator-supplied TF-site lists with:

```text
traceback lab asset griffin-register --upstream-root GRIFFIN_CHECKOUT \
  --ref-assets GRIFFIN_REF --site-list CTCF=FILTERED_CTCF_SITES \
  --primary-site CTCF --source-label operator-supplied --terms unknown --root ROOT
```

`traceback lab asset griffin-inspect --root ROOT` rehashes the registered
inputs. The registration digest enters the algorithm's execution identity,
while the Lab's input ID and declared platform keep cached results sample-specific. The algorithm
requires the 5M D21 paired-molecule gate and a paired-end hg38 BAM, runs the exact workflow,
and saves the validated corrected TSV and input-bound provenance alongside
the profile panel. The primary registered TF list supplies the scalar
`central_coverage`; all registered lists remain visible in the panel. Native
ONT, example, and derived inputs skip explicitly. No arbitrary TF activity or
diagnostic threshold is asserted.

Both complete pinned Snakemake workflows have run in an isolated synthetic
toolchain on 500 toy paired-end fragments, with a generated reference, site
list, and mappability track. The corrected output passed the Lab parser. That
proof used a compatibility shim for Snakemake 5.19.2 under Python 3.11 and
does not satisfy the Lab's 5M eligible-pair gate or validate a production hg38
sample. The commands, fixture generators, and output digest are in
[`docs/proofs/griffin/README.md`](proofs/griffin/README.md). A production run still needs registered hg38 assets and a compatible
toolchain. Native ONT input is incompatible with the exact upstream code; a
scientifically reviewed ONT fragment adapter would be needed for the Lab's
principal assay.

Griffin's source is BSD-3-Clause-Clear, which expressly grants no patent
rights. The Lab's existing `method-patents` callout applies, and no
freedom-to-operate review is represented here.

Primary sources: [Griffin source and workflow](https://github.com/adoebley/Griffin),
[Griffin methodology](https://doi.org/10.1038/s41467-022-35076-w).
