# Exploratory male-donor chrY cfDNA adapter

`dd-cfdna-chry` is available only for an attested male-donor, female-recipient
transplant sample with pregnancy excluded. It uses a protocol-matched female
chrY background and full male-donor response supplied by the operator. It is
**SPECULATIVE**, not a rejection assay or clinical threshold.

The operator registers one immutable, input-scoped JSON calibration. Save the
exact bytes, calculate their SHA-256, and run `traceback lab donor register
--file CALIBRATION.json --sha256 HEX_SHA256 --root ROOT`. `traceback lab donor
inspect INPUT_ID --root ROOT` verifies the registration. The schema is:

```json
{
  "schema": "traceback.lab.dd-cfdna-contract.v1",
  "source_id": "64 lowercase hex characters",
  "input_id": "64 lowercase hex characters",
  "reference_sha256": "64 lowercase hex characters",
  "male_donor_attested": true,
  "female_recipient_attested": true,
  "transplant_attested": true,
  "pregnancy_excluded_attested": true,
  "calibration_status": "operator_verified",
  "calibration_source": "operator's matched-mixture study and version",
  "protocol_label": "matched sequencing, alignment, and counting protocol",
  "counting_protocol": "D21-canonical-chrY-nonPAR-starts-per-million-all-eligible.v1",
  "female_control_n": 10,
  "male_calibration_n": 10,
  "min_depth_x": 4.0,
  "female_background_y_per_million": 1000.0,
  "male_full_donor_y_per_million": 3000.0
}
```

**Every numeric value above is illustrative, not a recommended threshold or
calibration.** The depth minimum must come from the operator's calibration,
not from this example. A file marked `synthetic` supports test arithmetic but
cannot run on an input. `operator_verified` is an assertion, not independent
verification by the software. The Lab binds the exact calibration bytes to the
input and reference SHA-256 and refuses changes to a registered input.
Synthetic calibration needs `--allow-synthetic` at registration. That option
irreversibly reserves the input's calibration slot for mechanics-only testing;
it does not permit a numeric donor-fraction result. If an existing registration
is damaged, restore its exact original bytes from backup. A different
calibration requires a new input identity; the Lab never overwrites the
immutable provenance.

The estimator counts eligible read starts on hg38 chrY outside its two
pseudoautosomal intervals. It divides that count by all eligible reads to get
a rate per million. For rates inside the calibrated interval, the reported
fraction is `(observed - female_background) / (full_male_response -
female_background)`. It returns null below the background or above the full
male response rather than extrapolating or clipping. The adapter also requires
canonical hg38 chr1–22, X, Y layout and the calibrated minimum depth.
The fixed counting-protocol value means D21 primary, mapped, QC-passing,
non-duplicate reads with MAPQ at least 20 on canonical chromosomes; chrY
read starts in either pseudoautosomal interval are excluded only from the
numerator. The operator's background, full-male response, and depth study must
use this exact count definition and the same reference and laboratory protocol.

ChrY signal can arise from sources other than a donor, and sample identity,
transplant context, sex-chromosome variation, and assay mismatch can invalidate
this calculation. The aggregate count and rate are supporting metrics; no
read identifiers or donor genotype are published. A numeric output still
requires matched-mixture accuracy and limit-of-detection validation before
biological or clinical interpretation.
