# Weidner three-CpG coordinate provenance

This page supports the [operator-registerable map](assets/weidner-3-grch38-map.json)
used by the exploratory [epi-clock adapter](epi-clock.md). It establishes CpG
identity and reference coordinates. It does not validate ONT methylation
fractions, cfDNA age predictions, or the transfer of coefficients from the
original blood assay.

## Site identity

[Weidner et al. 2014](https://link.springer.com/article/10.1186/gb-2014-15-2-r24)
Methods defines alpha as `cg02228185` (ASPA), beta as `cg25809905`
(ITGA2B), and gamma as the neighboring PDE4C CpG **upstream of** array probe
`cg17861230`. In the [authors' original Figure 2](https://media.springernature.com/original/springer-static/esm/art%3A10.1186%2Fgb-2014-15-2-r24/MediaObjects/13059_2013_3218_MOESM4_ESM.tif),
panel c underlines the array probe and the neighboring CpG; the callout selects
the second PDE4C underline for the fitted pyrosequencing model. The original
[supplement](https://media.springernature.com/original/springer-static/esm/art%3A10.1186%2Fgb-2014-15-2-r24/MediaObjects/13059_2013_3218_MOESM1_ESM.pdf)
contains its pyrosequencing primer table.

The independent [Illumina HumanMethylation450 v1.2 manifest](https://webdata.illumina.com/downloads/productfiles/humanmethylation450/humanmethylation450_15017482_v1-2.csv)
identifies all three array probes, marks each `Methyl27_Loci=TRUE`, and gives
their build 37 `MAPINFO` and full `Forward_Sequence` with `[CG]` at the probe.
The probe flanks and Figure 2c sequences were matched exactly to official
RefSeq chromosome intervals in both builds. The figure prints ASPA and ITGA2B
opposite the reference orientation; their sequences match after reverse
complementing. PDE4C Figure 2c matches the forward reference orientation.

| Model site | Illumina build 37 C (`MAPINFO`) | GRCh38.p13 accession | GRCh38 1-based C | GRCh38 0-based C |
|---|---:|---|---:|---:|
| `cg02228185` | 3,379,567 | `NC_000017.11` | 3,476,273 | 3,476,272 |
| `cg25809905` | 42,467,728 | `NC_000017.11` | 44,390,360 | 44,390,359 |
| `pde4c_upstream_of_cg17861230` | neighboring site; no array ID | `NC_000019.10` | 18,233,105 | 18,233,104 |
| `cg17861230` array probe, **not** model gamma | 18,343,901 | `NC_000019.10` | 18,233,091 | 18,233,090 |

The nearby ITGA2B CpG at GRCh38 44,390,358 is distinct from the underlined
`cg25809905` at 44,390,360. The PDE4C model CpG is 14 bases after its array
probe in forward-reference coordinates. [NCBI annotates PDE4C on the reverse
strand](https://www.ncbi.nlm.nih.gov/gene/5143), so this is upstream in gene
orientation. For every map position, the forward-reference base and next base
are `CG`; `pos0` records the C.

## Pinned reference and exact source bytes

The [NCBI GRCh38.p13 assembly report](https://ftp.ncbi.nlm.nih.gov/genomes/all/GCF/000/001/405/GCF_000001405.39_GRCh38.p13/GCF_000001405.39_GRCh38.p13_assembly_report.txt)
pins RefSeq assembly `GCF_000001405.39`, chromosome 17 to `NC_000017.11`,
and chromosome 19 to `NC_000019.10`. Its SHA-256 is
`15f83782257f61c12f30f51bb990cb0e7f598073babd2f93fcf16de0ab0c6138`.
The original Figure 2 TIFF SHA-256 is
`d0c5ab4e37a9adb027d5af23c9d5a76f013b44865ccdfcfd80bb67c62b452cc4`.
The Illumina manifest SHA-256 is
`9b729b2e905ae6d02df777c06380980ef3f502a2990b05cda9169332c2e8e7a2`.

The following NCBI EFetch FASTA intervals (1-based, inclusive) were used for
exact flank and Figure 2c sequence matching. Fetch each with
`https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi?db=nuccore&id=ACCESSION&seq_start=START&seq_stop=STOP&rettype=fasta&retmode=text`.
Replace `ACCESSION`, `START`, and `STOP` from each row, verify its SHA-256,
and search the returned sequence for the relevant Illumina `Forward_Sequence`
after replacing `[CG]` with `CG`. In build 37, its C must equal `MAPINFO`;
in GRCh38 it must equal the 1-based C above. Exact-match the Figure 2c
sequence, reverse-complementing for ASPA and ITGA2B, and verify the selected
underlined CpG at the listed model position.

| Accession | Interval | FASTA SHA-256 |
|---|---|---|
| `NC_000017.10` | 3,379,440–3,379,680 | `52da50a41c482b7922b9e4223e1d4296454bcd191252d475c83c6d54a8c2f175` |
| `NC_000017.10` | 42,467,610–42,467,850 | `628b4eabd7f02656985a080e6dfd7e2443f01c09f7aa693e685f72bad42ca8c0` |
| `NC_000019.9` | 18,343,780–18,344,020 | `6be4c4c35df850f213de24093ed65df594f33890e3f077f43d7261f82aea0fc7` |
| `NC_000017.11` | 3,476,150–3,476,390 | `7f4b5d129b809a449087ce34a34e66760e04da4dc2c6dd92101f3628eb1e9434` |
| `NC_000017.11` | 44,390,240–44,390,480 | `989993930aca601f7605c9d83342efae7847886c61aa2cd49660ffa53e8e5787` |
| `NC_000019.10` | 18,232,970–18,233,210 | `47036ac049b8270f9bc23c492fa2e11cc9f59953df07a435a0a9702439e82c55` |

NCBI EFetch may label these chromosome accession versions with a newer GRCh38
patch release. The pinned p13 assembly report includes the same primary
chromosome accession versions; the interval hashes above fix the exact
sequences used. The checked-in map's SHA-256 is
`ba5ce4d1f2313f6e39b5ff50fd19823ea96e7edec68ea611db5dcbff1b0f21df`.

The map is offered for explicit operator review and immutable registration in
a chosen Lab root. It is not loaded or registered automatically. Reference
sequence checks and a verified coordinate map are prerequisites for running
the exploratory arithmetic, not evidence of matched-platform age accuracy.
