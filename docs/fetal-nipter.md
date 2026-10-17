# Prenatal NIPTeR adapter

`fetal-fraction` is an exploratory NIPTeR 1.0.2 adapter for chromosome
13/18/21 standard z-scores. **Its fetal-fraction metric is always null:**
NIPTeR does not include a fetal-fraction estimator. No aneuploidy call,
screen-positive cutoff, or clinical reassurance is produced. Short-read NIPT
evidence does not qualify this ONT cfDNA transfer.

The operator supplies an input-scoped JSON contract and an existing NIPTeR
`NIPTControlGroup` RDS, prepared from euploid pregnancy samples on a matched
sequencing, alignment, reference, binning, and library protocol. The Lab keeps
the RDS outside its root and pins its exact bytes; the operator is responsible
for the cohort's provenance, rights, consent, and match. The contract contains
no name, gestational date, or other direct patient identifier:

The current adapter uses uncorrected, combined-strand 50 kb NIPTeR bins and
rejects a control RDS with a different correction or strand layout. Its own
eligibility gate counts Lab reads; NIPTeR performs its package-defined BAM
binning, so the operator must verify equivalent filtering in the control and
test sample before treating z-scores as comparable.

```json
{
  "schema": "traceback.lab.fetal-contract.v1",
  "source_id": "64-lowercase-hex-source-id",
  "input_id": "64-lowercase-hex-input-id",
  "reference_sha256": "64-lowercase-hex-reference-digest",
  "pregnancy_confirmed": true,
  "fetal_sex": "unknown",
  "control_path": "/absolute/operator/path/control_group.rds",
  "control_sha256": "64-lowercase-hex-RDS-digest",
  "control_n": 100,
  "protocol_label": "verified matched protocol and version",
  "control_euploid_attested": true,
  "control_protocol_matched_attested": true,
  "nipter_version": "1.0.2"
}
```

`fetal_sex` may be `male`, `female`, or `unknown`; it is an explicit assertion,
not an inference by this adapter. `control_n` must be at least ten and is
checked again against the RDS object during execution. These are engineering
admission checks, not clinical validation thresholds. The operator registers
the exact manifest with `traceback lab fetal register --file CONTRACT.json
--sha256 HEX_SHA256 --root ROOT`; `traceback lab fetal inspect INPUT_ID
--root ROOT` rehashes the contract and RDS without displaying the external
path. Registration for an input is immutable.

The adapter requires at least two million eligible reads, the full aligned
input, matching source/input/reference IDs, the verified control RDS, and an
installed Rscript with NIPTeR 1.0.2. The Rscript binary, exact installed
NIPTeR package files, R adapter code,
contract, and RDS digests enter the execution identity. A missing or changed
prerequisite leaves the method unavailable. The z-scores have no ONT-matched
reference range or clinical cutoff. Input-scoped prenatal control identities
are not pooled into a generic healthy-cohort reference. A separate, validated fetal-fraction
model is still required before that metric can be reported.

Sources: [NIPTeR package and license](https://github.com/molgenis/NIPTeR),
[official NIPTeR workflow](https://cran.r-universe.dev/NIPTeR/doc/NIPTeR.html),
[NIPTeR paper](https://doi.org/10.1186/s12859-018-2557-8).
