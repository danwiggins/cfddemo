# Griffin TF-site profile integration

The approved L2 `griffin` row means a Griffin nucleosome-protection profile
over transcription-factor binding sites. The paired-end implementation requires
5,000,000 D21 eligible autosomal pairs, a stricter subset of the broader
eligible-molecule cohort count. It is a research signal, not a TF expression
call or a cancer classifier. A site-density histogram or an uncorrected
coverage plot is not a Griffin result.

`traceback_lab.adapters.griffin.read_corrected_profiles` reads Griffin's
upstream `*.GC_corrected.coverage.tsv` format. It keeps the original bin-start
coordinates, GC-corrected coverage, central coverage, amplitude, site-list
name/count, smoothing and CNA-normalization state. It rejects uncorrected,
non-finite, malformed, or wrong-sample files. The parser was checked against
the vendored upstream `Healthy_demo.GC_corrected.coverage.tsv` fixture (one CTCF list, 132
15-bp bins) from upstream commit
`b624c7a2ea7b8758b6eebd424066c815bfeece61`; its exact-byte SHA-256 is
`4e7b430bc98d67d704e506ee41d2650b76bad3cc0a55be919f7884d0137ecc88`.

`traceback_lab.adapters.griffin_run.run_upstream` drives that pinned upstream
checkout, rather than approximating the measurement. It runs Griffin's GC
correction and nucleosome-profiling Snakemake workflows on one Lab aligned
BAM **when the BAM is paired-end WGS**. Griffin's scripts require paired
reads with positive template length in both GC counting and coverage. Native
ONT single-end BAMs have no such records; the adapter checks all primary
records and unanimous `@RG PL:ILLUMINA`, then rejects incompatible input
before creating an attempt. It compares the BAM's `@SQ` names and lengths
and the workflow chromosome sizes with the registered FASTA index. Every
autosome in that index must appear in the workflow sizes, so the coverage
space cannot omit autosomes included in the depth count. This checks
coordinate-space compatibility, not exact sequence identity. A scientifically
reviewed ONT fragment
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
all seven generated workflow YAML files, including capped cluster settings.
It requires Snakemake 5.19.2, records
the resolved Snakemake, Python, samtools, and bedtools executable digests and
the numerical Python package versions, and checks the runtime again after
execution. The attempt contains a `bin/python` launcher for the recorded Lab interpreter
and a hashed compatibility shim for Snakemake 5.19.2 on Python 3.11+. The
adapter invokes Snakemake through that interpreter; PATH and PYTHONPATH make
upstream scripts' `#!/usr/bin/env python` resolve to it too. The generated config sets the pinned
upstream default `mappability_correction: false`, so the accepted output is
GC-corrected only. The attempt only marks its manifest complete after Griffin returns
a valid corrected TSV with exactly the requested site-list names. Partial outputs remain
inspectable after failure. Griffin's upstream shell rules interpolate paths
without quoting, so the adapter refuses paths containing shell-special
characters. The runner is POSIX-only, matching upstream's environment.

The current Lab does **not** register or display Griffin as an available
algorithm. The adapter is an invocation boundary. A toy paired-end run through
both pinned upstream Snakemake workflows completed on a 30 kb synthetic
reference. A second bounded run completed through `run_upstream` itself, using
its generated configuration and corrected-TSV parser. It lowered only the
five-million-pair input gate to 500 for this proof and used a chr1-only toy
reference. Neither run validates biological signal or production resource
needs. The commands, fixture generators, and output digests are in
[`docs/proofs/griffin/README.md`](proofs/griffin/README.md). No real-sample
end-to-end run has been completed. Native ONT
input is incompatible with the exact upstream code. There is no
registered TF-site set yet. Before publication, the Lab must bind the
per-input attempt manifest, verified corrected TSV, asset digests and
upstream commit to its execution identity and result cache. Runner identity
currently receives only `ROOT`, not `input_id`; a static global TSV asset
would risk assigning one sample's profile to another. Derived and example
inputs need explicit same-input upstream execution or must skip.

Griffin's source is BSD-3-Clause-Clear, which expressly grants no patent
rights. The Lab's existing `method-patents` callout applies, and no
freedom-to-operate review is represented here.

Primary sources: [Griffin source and workflow](https://github.com/adoebley/Griffin),
[Griffin methodology](https://doi.org/10.1038/s41467-022-35076-w).
