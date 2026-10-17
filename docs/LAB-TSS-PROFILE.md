# TSS coverage profile (exploratory)

`tss-profile` computes an in-house aggregate coverage profile around protein-coding transcription start sites. It does not run Zhao-lab code, infer expression for an individual gene, or use a clinical cutoff. The approved demo-lane spec assigns the method to L2, fragmentomics, the NEWER tier, and a 5 million-read gate.

## Annotation and registration

The only accepted annotation is [GENCODE human release 46, basic gene annotation on reference chromosomes](https://www.gencodegenes.org/human/release_46), `gencode.v46.basic.annotation.gtf.gz`, for GRCh38.p14. The pinned SHA-256 of the compressed official file is `d620d548ad23dad6c2d67486b7679d12f00f02f66dd5787183d8e6678dceb9b4`. The operator supplies it locally and registers it with `traceback lab asset add tss-annotation --file <GENCODE_GTF> --source-label "GENCODE v46 basic CHR GTF" --terms unknown --root <ROOT>`. Registration copies exact bytes and records their digest; preflight and consumption recheck the registration. The digest also enters the algorithm execution identity. No annotation is bundled or downloaded by a Lab run.

## Measurement

The gene set consists of all `gene_type "protein_coding"` **gene features** on `chr1`–`chr22` in that annotation. One gene feature contributes one TSS: GTF start − 1 on `+`, GTF end − 1 on `−`, in zero-based reference coordinates. Gene features lacking a complete ±1,000 bp window on the aligned contig are excluded. The aligned reference must have the hg38 chr1 length. This gene-boundary choice is deterministic; it does not select a transcript isoform or a fabricated expressed-gene panel.

The input is the read summary's D21-eligible autosomal primary mapped nonduplicate molecules (MAPQ ≥20), after any derived-input mask. At least **5,000,000** such molecules must remain. Each molecule contributes its aligned interval's exact overlap bases to 50 bp bins from −1,000 to +1,000 relative to the TSS; negative-strand genes are reversed to transcriptional orientation. Coverage in a bin is overlap bases divided by 50 bp and the count of genes with complete windows. The panel reports each bin's mean coverage, the gene count, eligible read count, and annotation digest.

The scalar `central_flank_ratio` is mean coverage in [−150,+50) divided by mean coverage in the combined [−1,000,−500) and [+500,+1,000) flanks. It is null when the flank depth is zero. A lower ratio describes a deeper aggregate coverage dip. `tss_genes` reports the gene denominator. There is no GC, mappability, or copy-number correction, reference interval, diagnostic classifier, or individual-gene expression result.

[Published cfDNA work](https://www.nature.com/articles/s41467-021-22463-y) describes a promoter nucleosome-depleted region near −150 to +50 bp and an association between relative coverage and gene expression. Those study results are context, not transferred performance for this ONT implementation. The [GENCODE GTF format](https://www.gencodegenes.org/pages/data_format.html) supplies the coordinate and feature-field conventions.
