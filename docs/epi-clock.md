# Exploratory epi-clock adapter

`epi-clock` transfers the three-CpG Weidner whole-blood model to eligible ONT
cfDNA CpG calls. It is **SPECULATIVE** on this platform and sample type. The
model is not validated as a cfDNA age assay and has no clinical reference
range. The four fixed coefficients come from `methylclockData`'s
`coefWeidner.rda` (SHA-256
`9a3feb7d0c500f9ccdb61ffe243775587fa21b082536142bb1260c566cd39645`).
The source R asset is not bundled.

The fitted equation is `38.0 - 26.4 × ASPA - 23.7 × ITGA2B + 164.7 × PDE4C`,
where each term is the modified fraction at its measured CpG. In this order,
the assay sites are ASPA (`cg02228185`), ITGA2B (`cg25809905`), and the PDE4C
CpG upstream of array probe `cg17861230`. The arithmetic can yield values
from −12.1 to 202.7 years; those are mathematical bounds, not a valid age
range or clinical interpretation. The one-year split-stability tolerance is
an analysis check, not a guarantee at the five-call completeness gate.
At exactly five eligible PDE4C calls, one call changing between modified and
unmodified changes that site's fraction by 0.2 and the fitted arithmetic by
`164.7 × 0.2 = 32.94` years. The gate therefore does not establish a precise
age estimate; inspect each site's call count and split stability.

The [verified GRCh38.p13 coordinate map](assets/weidner-3-grch38-map.json)
provides the two Illumina probe CpGs and the **upstream PDE4C assay CpG** used
for the third coefficient in [Weidner et al. 2014](https://doi.org/10.1186/gb-2014-15-2-r24).
Its source sequence, assembly accessions, checksums, strand interpretation,
and reproduction steps are recorded in the
[coordinate provenance](epi-clock-coordinate-provenance.md). These are
zero-based positions of the forward-reference `C` in each `CG`:

| Assay site | Chromosome | `pos0` |
|---|---|---:|
| `cg02228185` (ASPA) | `chr17` | 3,476,272 |
| `cg25809905` (ITGA2B) | `chr17` | 44,390,359 |
| `pde4c_upstream_of_cg17861230` (PDE4C) | `chr19` | 18,233,104 |

The coefficient asset labels that third coefficient `cg17861230`, but the
paper's fitted pyrosequencing equation measures a nearby CpG upstream of that
array probe. Registering the `cg17861230` probe coordinate for the third site
would apply the coefficient to the wrong CpG. The array probe is at `chr19`
`pos0` 18,233,090, 14 bases before the assay CpG on the forward reference.
PDE4C is on the reverse strand, so the selected site is upstream in gene
orientation. There is no automatic registration or liftover. The pinned map
uses this schema, with exactly these fields. The JSON file must contain
1–8192 bytes. `source_label` must contain 1–160 characters, with no slash,
backslash, or character below U+0020:

```json
{
  "schema": "traceback.lab.epi-clock-map.v2",
  "model_id": "Weidner-3",
  "genome_build": "hg38",
  "source_label": "Weidner2014-IlluminaHM450v1.2-NCBI-GRCh38p13",
  "sites": [
    {"cpg_id": "cg02228185", "chrom": "chr17", "pos0": 3476272},
    {"cpg_id": "cg25809905", "chrom": "chr17", "pos0": 44390359},
    {"cpg_id": "pde4c_upstream_of_cg17861230", "chrom": "chr19", "pos0": 18233104}
  ]
}
```

Only autosomes are accepted. Registration checks structure, unique positions,
and exact bytes; at run time the adapter checks the [GRCh38 primary-assembly
chromosome lengths](https://www.ncbi.nlm.nih.gov/grc/human/data?asm=GRCh38)
in both the aligned input and reference FASTA, and checks the FASTA
for `CG` at all three positions. Those runtime checks alone cannot establish
site identity; the linked provenance records the independent coordinate
cross-check. This does not qualify the age model on ONT cfDNA.

After reviewing the map, run these commands from the repository root to
register its exact bytes:

```bash
traceback lab asset register-clock-map --file docs/assets/weidner-3-grch38-map.json --sha256 ba5ce4d1f2313f6e39b5ff50fd19823ea96e7edec68ea611db5dcbff1b0f21df --root ROOT
traceback lab asset inspect-clock-map --root ROOT
```

Registration is immutable within a Lab root.
If a registered coordinate is wrong, use a new Lab root with a corrected,
independently verified map; the existing registration cannot be replaced.
If registration reports an uncertain publication outcome, inspect the map in
that root before retrying.

The array model's ordinary bisulfite beta value cannot separate 5mC from
5hmC. To approximate that input, each ONT CpG fraction counts an argmax 5mC
or 5hmC state as modified and an argmax canonical C state as unmodified;
rows with either modification channel are eligible. This is a categorical
ONT-to-array transfer, not a calibrated equivalence. The adapter requires
5 million eligible reads, an MM-tagged methylation stream, hg38, and at
least five eligible C+m or C+h calls at **each** CpG. The
five-call rule is an engineering completeness gate, not a published or clinical
performance threshold. If any site misses the gate, the age metric is null;
the adapter does not impute. Site fractions and counts remain visible. Even a
numerical output needs matched-platform validation before biological use.

Sources: [Weidner et al. 2014](https://doi.org/10.1186/gb-2014-15-2-r24),
[methylclock package](https://github.com/isglobal-brge/methylclock),
[methylclockData record](https://zenodo.org/records/22069904),
[Illumina methylation methods guide](https://emea.illumina.com/content/dam/illumina-marketing/documents/products/other/field_guide_methylation.pdf).
