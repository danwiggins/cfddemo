# Lab interpretation evidence matrix

This is a code-and-spec audit of the Lab interpretation boundary.
It is not assay validation. The Lab interpretation library is joined at read
time; its version is reported without changing stored result bytes or
execution identities (`traceback_lab/interpretation/engine.py`).

Inventory at library v0.6.5: 188 entries across 35 displayed algorithms.
Of those entries, 125 declare no reference, 59 permit an `internal_cohort`
comparison, and four show published means without intervals. Those four are
fragment-size mode/mono/di-nucleosome values from platform-unspecified or
Illumina background literature, and mitochondrial share from Illumina WGS.
Seventy-nine entries carry at least one citation, 136 carry at least one
license callout, and 183 have a `citation_checked` record. These counts come
from `library.yaml` metadata; they are not independent verification of the
citations, applicability, or licenses. The registry also has two hidden
producers, `read-summary` and `meth-calls`, making 37 execution algorithms.

| Boundary | Current behavior | Evidence gap | Safe current state and downstream action |
| --- | --- | --- | --- |
| Cohort membership and circularity | `classify` accepts a prefiltered `Cohort` and gates on its supplied length: `<10` no reference, `10–29` exploratory min–max, `>=30` percentile and bootstrap interval. `project_metric` excludes the target by source identity before those gates and removes incompatible or missing member results. | `classify` alone cannot detect self-inclusion if a different caller supplies values directly. | Keep read-side scoring routed through `project_metric`; a ten-source cohort containing the target has nine comparison sources. A PoN protocol mismatch can retain independent metrics with a warning, but copy-number-dependent results stay excluded. |
| UI defended versus full-input completeness | `defended.exclusion` checks status, half-depth usability, split stability, tier, citation, and positive control where applicable. The UI can say “result is defended” within those checks; it receives no full-kit completeness flag. | A partial source cannot support a full-run scientific claim merely because one UI row is defended. | Treat the UI label as a software evidence state, and report source completeness separately. Do not infer clinical or assay qualification from it or from a synthetic example. |
| Actual modified-base model | `cell_origin.header_model` accepts a single string from BAM `@RG`, including an unverified placeholder, and `run` forwards it to signed measurement parameters. | A single declared string can be a placeholder rather than the actual vendor model. The software currently cannot derive the real model from it. | Keep model identity unknown for scientific interpretation. The owner must supply verified model provenance before a biological claim or a versioned code policy. S1 compatibility must not mistake the placeholder for known matching chemistry. |
| PMD input terms | `licenses.yaml` labels Zhou PMD data terms unknown; the PMD file is operator supplied and digest pinned. | A citation request is not a distribution license. | Retain `zhou-pmd-terms-unknown`; S2 asset handling must record terms without vendoring or downloading it implicitly. Owner resolves rights and PoN method. |
| Existing library metadata | `library.yaml` uses tier, origin tier/platform, depth, reference kind, performance or missing reason, stability, citations and `citation_checked`; `licenses.yaml` resolves license callouts. | These fields do not by themselves validate source applicability to this ONT kit. Existing published figures include other platforms and populations. | Keep published means as `no_reference`, and platform-specific performance as footnotes. Do not promote an Illumina/WGBS figure to an ONT healthy range. Independent source audit is needed before changing any displayed figure. |
| Frag-x-meth | Among D21 eligible derived-masked molecules with ≥4 C+m CpG calls, each read contributes its fraction of argmax 5mC among C+m sites. `short_fragment_hypomethylation_delta` is mean(long 151–220 bp) minus mean(short 100–150 bp). | No peer-reviewed source has been verified for this exact ONT estimator, binning, or denominator. | The current entry is `SPECULATIVE`, `reference: none`, and has no ONT normal range or scientific performance claim. Null explicitly names the missing size group. |
| Breakpoint motifs | `cpg_near_end_fraction` counts D21 eligible aligned-span starts and ends-1 within ±10 bp of a reference CG C; complete A/C/G/T contexts supply motifs. | Aligned ends are not necessarily physical cleavage sites, and this ONT estimator has no verified healthy interval or diagnostic performance. | The current entry is `SPECULATIVE`, `reference: none`, with a LEGAL caution flag. No patent or data rights are implied. |
| Signed metric stability | The library loader requires every signed metric to declare stability. Forty-seven entries use `unvalidated`; stability panels show raw absolute differences and separately labelled relative differences. | A declared `unvalidated` state supplies no tolerance or evidence of repeatability. | Keep the panel visible as measurement context, with no stable verdict or invented cutoff. |
| Model and calibration dependencies | MethylBERT needs an operator-supplied fine-tuned checkpoint and qualified classified-read support; epi-clock needs a verified CpG map; prenatal and donor chrY fractions need input-scoped matched calibrations. | No bundled model, generic coordinate guess, or example calibration establishes biological validity. | Missing prerequisites skip or leave a result null. A stored-manifest provenance check protects new chrY calibration slots but does not verify the study behind them. |

The synthetic acceptance file `tests/lab/test_interpretation_evidence.py`
records engine boundary behavior. It does not prove kit input completeness,
real-data recovery, a correct modified-base model, or calibration suitability.
