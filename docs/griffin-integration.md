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
the official `Healthy_demo.GC_corrected.coverage.tsv` (one CTCF list, 132
15-bp bins) from upstream commit
`b624c7a2ea7b8758b6eebd424066c815bfeece61`.

`traceback_lab.adapters.griffin_run.run_upstream` drives that pinned upstream
checkout, rather than approximating the measurement. It runs Griffin's GC
correction and nucleosome-profiling Snakemake workflows on one Lab aligned
BAM **when the BAM is paired-end WGS**. Griffin's scripts require paired
reads with positive template length in both GC counting and coverage. The
adapter requires every BAM read group to declare `PL:ILLUMINA` and scans the
full BAM for paired and usable records before creating an attempt. It does
not infer library type from an early coordinate-sorted slice. Native ONT
single-end BAMs are rejected. A scientifically reviewed ONT fragment
adapter remains necessary for the Lab's primary input. The caller supplies
the registered hg38 FASTA and its SHA-256, Griffin's
reference assets (including mappability BigWig, exclusion BEDs and all
15–500 bp genome GC-frequency tables), and high-mappability TF-site lists.
It requires the 5M D21 gate, counted as eligible autosomal **pairs** on an
Illumina-declared BAM (one first mate per molecule), a clean upstream checkout at the pinned commit,
and a fresh per-input attempt directory. The attempt stores hashes for BAM,
reference, site lists and reference assets, and only marks its manifest
complete after Griffin returns a valid corrected TSV. Partial outputs remain
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

An end-to-end upstream run has not yet been completed in this environment:
tested Snakemake 5.19.2, pyBigWig, pybedtools, and the required mappability
BigWig are absent. Native ONT input is incompatible with the exact upstream
code. A scientifically reviewed ONT fragment adapter is still needed before
this method can run on the Lab's principal assay.

Griffin's source is BSD-3-Clause-Clear, which expressly grants no patent
rights. The Lab's existing `method-patents` callout applies, and no
freedom-to-operate review is represented here.

Primary sources: [Griffin source and workflow](https://github.com/adoebley/Griffin),
[Griffin methodology](https://doi.org/10.1038/s41467-022-35076-w).
