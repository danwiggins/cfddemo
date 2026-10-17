# cfDNA Lab: what each algorithm could help us learn

A plain-English guide to the 37 registered algorithms, plus the views that
combine their results. Updated 17 October 2026.

cfDNA is DNA circulating outside cells. The Lab examines its amount, fragment
patterns, chromosome representation, and methylation marks. These are different
views of the same sample. Together they can help us ask better research
questions; they do not automatically identify a disease or establish a diagnosis.

Most methods here operate on Oxford Nanopore (ONT) data. Three methods are
specifically for paired-end Illumina data. A registered algorithm is not a
promise that it can run on today's sample: depth, reference files, trained
models, controls, platform, and licensing requirements can prevent execution.
The Lab shows those missing requirements rather than filling in a result.

For commands and exact gates, use [LAB-GUIDE](LAB-GUIDE.md). The live inventory
is `traceback lab algorithms`. Evidence and interpretation are described in
[LAB-INTERPRETATION-EVIDENCE](LAB-INTERPRETATION-EVIDENCE.md).

## Epigenetic age: an interesting research starting point

DNA methylation is a chemical mark on DNA. Some methylation patterns vary with
age. An epigenetic clock applies a fitted model to selected sites and produces
an age-like estimate. Different clocks were trained on different tissues,
assays and outcomes; their numbers are not interchangeable.

Our current `epi-clock` implementation uses the three-site Weidner model,
originally developed from blood DNA using methylation assays. It reads sites
associated with **ASPA, ITGA2B and PDE4C**. Applying that model to ONT cfDNA is
an experimental transfer, not a validated cfDNA clock. The original research
supports studying these sites; it does not establish performance for our
platform or specimen. [Weidner et al., 2014](https://pmc.ncbi.nlm.nih.gov/articles/PMC4053864/)

What we could investigate:

- **Age association:** does the output track known chronological age in an
  independent group processed with the same collection and sequencing protocol?
- **Repeatability:** do separate preparations of the same sample agree, and how
  much does the estimate move when we reduce read depth?
- **Mixture effects:** does an apparent age difference accompany a change in
  estimated cell origins? A shifting mixture could change the methylation
  signal without demonstrating that a person has aged faster.
- **Changes over time:** after establishing technical repeatability, do repeated
  samples show a reproducible change larger than measurement variation?

Published cfDNA research has explored age-related methylation and tissue
composition, which makes those questions worth testing. It does not validate
our three-site implementation. [cfDNA methylation and aging study](https://pubmed.ncbi.nlm.nih.gov/38869474/)

**The immediate limitation is precision and mapping.** Our adapter needs a
verified hg38 assay-site map, including the correct PDE4C site upstream of
`cg17861230`; that array probe itself is not a substitute. It requires at least
five million eligible reads and five usable calls at each site. Five calls is
only a completeness check: one changed call at PDE4C can move the arithmetic by
about 33 years at that coverage. The Lab must not present such a number as a
precise biological age. See [the implementation and mapping notes](epi-clock.md).

We cannot currently infer “years younger,” lifespan, treatment benefit, organ
age, or a rate of aging from this result. A difference from chronological age
could reflect measurement noise, sample handling, the cell mixture, or the
transfer between assays. Establishing an aging interpretation requires a
separate validation study.

A future comparison could examine clocks trained specifically on cfDNA. For
example, a published study developed a 125-CpG cfDNA clock and evaluated it in
an independent dataset. That model is **not implemented in this Lab** and would
need its own reproducible assets, platform assessment and validation.
[Whole-genome cfDNA age and sex study](https://www.nature.com/articles/s43856-025-01220-y)

## Sample quality and identity

### 1. Read summary (`read-summary`)

**Measures:** a reusable summary of aligned reads, used by many other methods.
**What we learn:** which reads support the analysis and whether derived samples
use the intended subset. This is infrastructure, not a biological score; it
also avoids repeatedly scanning the BAM for every simple measurement.

### 2. Sequencing quality (`bam-qc`)

**Measures:** mapping quality, read quality, supplementary alignments, GC bias,
coverage summaries, and marked duplicates where available. **What we learn:**
whether preparation, sequencing or alignment problems may explain an unusual
result. Good QC makes a sample more usable; it does not make every downstream
interpretation reliable.

### 3. Usable depth (`depth`)

**Measures:** eligible reads and approximate aligned-base coverage.
**What we learn:** which analyses have enough data to attempt, and what might
improve with additional sequencing. A large total read count is not the same
as adequate coverage at every methylation marker or genomic region.

### 4. Long DNA fragments (`long-tail`)

**Measures:** proportions of unusually long molecules. **What we learn:**
whether a sample has a pattern consistent with handling effects or cellular
DNA contamination. It is a preparation-quality clue, not a diagnosis or a
unique explanation of how the sample was handled.

### 5. Chromosome X/Y check (`sex-check`)

**Measures:** relative X and Y read density. **What we learn:** whether the
chromosomal pattern is consistent with recorded sample information, which can
help investigate mix-ups. It is not a determination of gender, and unusual
biology or mixed DNA sources can complicate the pattern.

### 6. Sample identity (`identity`)

**Measures:** a Somalier genetic fingerprint; paired comparisons examine
relatedness and possible duplicates. **What we learn:** whether two samples
appear to come from the same genetic source or might have been swapped.
It requires sufficient coverage and appropriate sites; it is not a public
identity lookup or a substitute for chain-of-custody records.

### 7. Methylation-call quality (`modbase-qc`)

**Measures:** available modification channels, tagged-read support and call
confidence. **What we learn:** whether the methylation data can support the
requested analyses. A missing 5hmC channel means we cannot measure 5hmC, not
that the sample contains none.

## Fragment sizes and DNA packaging

### 8. ONT fragment sizes (`fragment-size`)

**Measures:** an aligned-span size distribution, short-fragment share, peaks
and periodicity. **What we learn:** whether fragmentation patterns differ
between samples or change over time. Collection, preparation and alignment
can also change the distribution; this is not a standalone cancer test.

### 9. Illumina fragment sizes (`short-read-fragment-size`)

**Measures:** paired-end template lengths from an Illumina-declared BAM.
**What we learn:** the corresponding short-read fragment distribution and a
basis for carefully designed platform comparisons. Template length and ONT
aligned span are different definitions and should not be pooled automatically.

### 10. End motifs (`end-motifs`)

**Measures:** reference sequence patterns at the two aligned fragment ends,
including motif diversity and CCCA share. **What we learn:** whether end-pattern
composition differs across samples. This can motivate studies of fragmentation
processes, but alignment-derived ends and reference bases are imperfect proxies
for the original physical cleavage event.

### 11. Breakpoint context (`breakpoint-motifs`)

**Measures:** end motifs and their proximity to CpG sites. **What we learn:**
whether observed fragment endpoints occur in different sequence contexts.
The current output does not establish a direct causal relationship between
methylation and cleavage, or prove that an aligned endpoint is the physical end.

### 12. Regional short/long ratios (`delfi-ratio`)

**Measures:** short-to-long fragment ratios across genomic bins.
**What we learn:** whether fragmentation differences are concentrated in
particular genomic regions. This implementation is an exploratory,
cohort-relative signal; it is not the published DELFI cancer classifier and
does not reproduce all of that classifier's corrections or validation.

### 13. Transcription-start profiles (`tss-profile`)

**Measures:** aggregate coverage around transcription start sites.
**What we learn:** whether DNA-packaging footprints differ around registered
gene sets. This may help generate hypotheses about contributing cell types or
chromatin states. It does not directly measure gene expression.

### 14. LIONHEART accessibility scores (`lionheart`)

**Measures:** 898 raw chromatin-accessibility correlation scores using the
upstream extraction workflow. **What we learn:** whether a sample's coverage
pattern resembles particular reference accessibility patterns. The Lab does
not run LIONHEART's cancer prediction; ONT use remains experimental and needs
its external reference resources and tools.

### 15. Griffin nucleosome profiles (`griffin`)

**Measures:** GC-corrected profiles around registered transcription-factor
binding sites. **What we learn:** how those local packaging patterns compare
between suitable samples. This adapter requires paired-end Illumina data and
at least five million eligible autosomal pairs. It skips native ONT and does
not turn a profile into a tissue-origin or disease call.

### 16. Windowed protection score (`wps`)

**Measures:** how often eligible fragments span a genomic window versus end
inside it. **What we learn:** fine-scale patterns related to protection and
DNA packaging. It needs high depth, appropriate reference data and substantial
storage. A raw average is strongly affected by coverage; the Lab does not
label it a validated nucleosome or health score.

### 17. Oriented and preferred ends (`aligned-end-structure`)

**Measures:** orientation-aware endpoint patterns around registered open
chromatin centers and preferred-end coordinates. **What we learn:** whether
fragments show reproducible directional structure around those sites.
This covers the exploratory OCF/preferred-end idea using aligned spans;
registered catalogues and high depth are required, and tissue-origin
calibration remains unestablished.

### 18. Independent toolkit cross-check (`finaletoolkit-xcheck`)

**Measures:** agreement between FinaleToolkit 1.1.0 and independent counters
for motifs and fragment-ratio counts on the same Illumina BAM.
**What we learn:** whether the implementations agree under their declared
settings. This is an engineering cross-check, not additional independent
biological evidence. ONT and derived inputs receive explicit skips.

### 19. Mitochondrial DNA (`mtdna`)

**Measures:** mitochondrial read share and fragment size.
**What we learn:** whether mitochondrial representation changes alongside
other signals. Sample handling, platelet contribution and alignment ambiguity
can affect it; it is not a direct measurement of mitochondrial function.

## Chromosome representation and cancer-related research signals

### 20. Copy number (`copy-number`)

**Measures:** genomic gains/losses, segments, ploidy and a model-based tumour
fraction through ichorCNA. **What we learn:** whether the data support broad
copy-number structure and what fraction the fitted model attributes to that
structure. A verified, protocol-matched panel of normals is needed for the
qualified comparison. Low signal, model ambiguity and whole-genome doubling
can complicate estimates; absence of a signal does not rule out cancer.

### 21. Genomic instability (`instability`)

**Measures:** a t-MAD-style summary of variation in the copy-number segments.
**What we learn:** whether a sample is more uneven than compatible controls.
Noise and coverage also affect unevenness. There is no validated ONT clinical
cutoff, and it depends on the underlying copy-number result.

### 22. Chromosome-arm summaries (`arm-summary`)

**Measures:** copy-number summaries for the p and q arms of the autosomes.
**What we learn:** which broad chromosome arms contribute to a pattern.
Centromere definitions, supported bins and the selected copy-number result
matter. These are exploratory summaries, not diagnoses of specific syndromes
or cancers.

### 23. Methylation tumour fraction (`meth-tf`)

**Measures:** a two-component nonnegative fit to registered healthy and tumour
methylation marker references. **What we learn:** whether observed methylation
is better represented by a mixture of those particular references.
The fitted share is reference-dependent, not a universal tumour percentage;
matching genome/model metadata and sufficient marker support are required.

### 24. MethylBERT (`methylbert`)

**Measures:** a trained model's read-level methylation classifications and
aggregate tumour-related output. **What we learn:** whether a more complex
pattern model captures information beyond simple summaries. It needs a trusted,
compatible fine-tuned checkpoint, GPU/runtime and adequate data. A missing
model cannot be replaced with invented weights, and output does not establish
clinical accuracy on ONT cfDNA.

## Methylation and cell origins

### 25. CpG call extraction (`meth-calls`)

**Measures:** site-level canonical, 5mC and 5hmC calls decoded from MM/ML tags.
**What we learn:** which molecular observations feed the methylation methods.
This is a shared intermediate, not an additional independent biological test.
Unknown or unsupported calls must remain distinguishable from unmethylated ones.

### 26. Cell origin (`cell-origin`)

**Measures:** an estimated mixture across 40 reference cell types, with
bootstrap intervals. **What we learn:** which reference cell types may
contribute DNA to the sample, and whether that mixture differs across samples.
It estimates a mixture of shed DNA, not the proportion of cells in the body.
Minor components need more support and remain particularly uncertain.

### 27. Global 5mC (`global-5mc`)

**Measures:** the share of eligible CpG calls classified as 5-methylcytosine.
**What we learn:** broad methylation differences that could motivate regional
or cell-mixture analysis. A global shift is not specific to cancer, aging or
one tissue, and comparisons require compatible call models and processing.

### 28. Global 5hmC (`global-5hmc`)

**Measures:** the share of eligible calls classified as
5-hydroxymethylcytosine. **What we learn:** another dimension of DNA modification
when the sequencing model provides that channel. Missing-channel skips are
not zero measurements; platform calibration is necessary for biological claims.

### 29. Partially methylated domains (`pmd-score`)

**Measures:** methylation inside versus outside registered PMD regions.
**What we learn:** whether broad regional methylation organization differs
from a suitable reference group. It needs the correct registered region asset;
a difference alone does not identify its biological cause.

### 30. Hypomethylated reads (`hypometh-reads`)

**Measures:** how many sufficiently observed molecules have low methylation,
along with unmethylated/intermediate/methylated categories.
**What we learn:** whether the sample contains a different distribution of
methylation states at the molecule level. A low-methylation molecule is not
necessarily tumour-derived.

### 31. Fragment size versus methylation (`frag-x-meth`)

**Measures:** the joint distribution of molecule length and methylation.
**What we learn:** whether short and long fragments carry different methylation
patterns within the same sample. This helps test whether two apparent signals
are related, but does not prove they come from independent biological processes.

### 32. Exploratory epigenetic age (`epi-clock`)

**Measures:** the three-site Weidner age arithmetic using ONT modification
calls. **What we learn:** whether that specific signal is reproducible and
age-associated in a properly designed cfDNA study. It needs verified assay
coordinates and adequate site-level support. It is not currently a validated
biological-age or longevity measurement; see the expanded discussion above.

## Prenatal and transplant research

### 33. Prenatal chromosome screening (`fetal-fraction`)

**Measures:** exploratory NIPTeR-based chromosome 13/18/21 dosage statistics
against registered controls. **What we learn:** whether chromosome
representation differs from a compatible control set. The registry name is
broader than the implemented output: those z-scores are not a general,
sex-independent fetal-fraction estimate. ONT calibration and suitable controls
remain required; this is not a clinical prenatal screening result.

### 34. Male-fetus fraction (`fetal-fraction-chry`)

**Measures:** a chrY-based fraction under input-specific, operator-verified
calibration and biological attestations. **What we learn:** an exploratory
fetal contribution estimate in the supported male-fetus scenario.
It does not apply to female or unknown fetal sex and does not replace a
sex-independent model. Incorrect source/reference provenance is refused
before a new immutable calibration is registered.

### 35. Donor fraction (`dd-cfdna-chry`)

**Measures:** a calibrated chrY-based donor fraction for the specific case of
a male donor and female recipient. **What we learn:** whether the estimated
donor-derived contribution differs across compatible samples.
It is not a general donor-fraction method, nor evidence of rejection or organ
injury on its own; calibration, biological exclusions and clinical studies
would be needed for those uses.

## Non-human sequence signals

### 36. Viral reads (`viral-reads`)

**Measures:** EBV, HPV and HBV alignment signals from a separate analysis of
human-unmapped reads against registered viral references.
**What we learn:** whether sequence evidence merits follow-up and contamination
checks. Sequence detection is not proof of active infection, viral load,
causation or a virus-associated cancer.

### 37. Microbial profile (`microbial`)

**Measures:** Kraken2/Bracken species classifications from selected
human-unmapped ONT reads using pinned tools and a registered database.
**What we learn:** which microbial sequence patterns appear and whether they
recur in controls or batches. Database errors and contamination are major
alternative explanations. This is not an infection diagnosis or an inventory
of all organisms in the person.

## What combining the results adds

**Arm z-scores** compare arm summaries with protocol-compatible controls.
They can highlight arms worth investigating, but require enough independent
controls and do not carry a validated abnormality cutoff.

**TF concordance and Agreement** compare copy-number, methylation, fragment-size
and mitochondrial observations. They help reveal agreement, disagreement and
possible confounding. The views distinguish raw plots from qualified paired
results and refuse a stale copy-number binding for the selected cohort.
Shared inputs mean agreement is not independent confirmation.

**Depth curves, split halves and synthetic controls** help distinguish
computational behavior from measurement fragility. Downsampling asks whether
a signal survives less data. Split halves test sensitivity within one sample;
they are not independent preparations. Synthetic spikes test a known engineered
change; they do not establish performance on real positive cases.

A useful first research sequence is to establish QC and repeatability, examine
cell-origin and fragment patterns, then assess more demanding signals such as
age or tumour-fraction models against independent, well-characterized samples.
The most informative result may be finding why two methods disagree.
