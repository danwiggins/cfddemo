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

The operator must provide a verified hg38 coordinate mapping for the two
Illumina probe CpGs and the **upstream PDE4C assay CpG** used for the third
coefficient in [Weidner et al. 2014](https://doi.org/10.1186/gb-2014-15-2-r24).
The coefficient asset labels that third coefficient `cg17861230`, but the
paper's fitted pyrosequencing equation measures a nearby CpG upstream of that
array probe. Registering the `cg17861230` probe coordinate for the third site
would apply the coefficient to the wrong CpG. There is deliberately no built-in guess or automatic
liftover. The JSON file must be at most 8 KiB, with exactly these fields:

```json
{
  "schema": "traceback.lab.epi-clock-map.v2",
  "model_id": "Weidner-3",
  "genome_build": "hg38",
  "source_label": "name of verified annotation and version",
  "sites": [
    {"cpg_id": "cg02228185", "chrom": null, "pos0": null},
    {"cpg_id": "cg25809905", "chrom": null, "pos0": null},
    {"cpg_id": "pde4c_upstream_of_cg17861230", "chrom": null, "pos0": null}
  ]
}
```

The null values above make this template **unregisterable** until the real
assay-site coordinates have been verified. Supply the actual chromosome and zero-based position of the
forward-reference C for each measured CpG. Only
autosomes are accepted. The registration checks structure, unique positions,
and exact bytes; at run time the adapter checks the [GRCh38 primary-assembly
chromosome lengths](https://www.ncbi.nlm.nih.gov/grc/human/data?asm=GRCh38)
in both the aligned input and reference FASTA, and checks the FASTA
for `CG` at all three positions. Those checks cannot establish that a site ID
was mapped to the correct locus. The operator must verify that mapping against
the named annotation, and qualification remains pending until that evidence is
available.

Register with `traceback lab asset register-clock-map --file MAP.json --sha256
HEX_SHA256 --root ROOT`, then inspect with `traceback lab asset
inspect-clock-map --root ROOT`. Registration is immutable within a Lab root.
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
