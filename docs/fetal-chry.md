# Male-fetus chrY fetal-fraction workflow

`fetal-fraction-chry` is separate from the NIPTeR chromosome z-score adapter.
It estimates a fetal fraction only for an asserted pregnancy with a male fetus
and female maternal background, using an operator-supplied, protocol-matched
chrY calibration. NIPTeR itself does not calculate fetal fraction. Neither
method makes an aneuploidy call, sets a clinical cutoff, or claims ONT assay
validation. The chrY method is **SPECULATIVE** with **PRIVACY**, **FDA**, and
**LEGAL** flags.

The operator must have female-background controls and known male-DNA mixture
samples prepared with the same cfDNA extraction, ONT sequencing, hg38 alignment,
and counting policy as the input. The contract pins their derived rates, validated
fraction interval, depth minimum, counts, and source/protocol labels to one
source/input/reference identity. These values and the `operator_verified` status
are operator assertions; the Lab cannot verify the calibration study. The
operator must keep patient identifiers out of free-text labels, which stay in
the private contract and are not echoed by the CLI.
The registration directory itself reveals that a male-fetus pregnancy
calibration was supplied. Protect the whole Lab root as sensitive storage.

Save a JSON file with these exact fields, calculate its SHA-256, and run
`traceback lab fetal chry-register --file CALIBRATION.json --sha256 HEX --root R`.
Use `traceback lab fetal chry-inspect INPUT_ID --root R` to verify it. Registration
is immutable for the input. The example numbers below demonstrate file shape
only; they are **not** validated thresholds or recommended calibration values.

```json
{
  "schema": "traceback.lab.fetal-chry-calibration.v1",
  "source_id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "input_id": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
  "reference_sha256": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
  "pregnancy_confirmed": true,
  "maternal_female_attested": true,
  "male_fetus_attested": true,
  "transplant_excluded_attested": true,
  "calibration_status": "operator_verified",
  "calibration_source": "operator matched-mixture study version",
  "protocol_label": "matched ONT cfDNA and hg38 counting protocol",
  "counting_protocol": "F1-canonical-chrY-nonPAR-starts-per-million-all-eligible.v1",
  "female_control_n": 10,
  "male_mixture_n": 10,
  "min_depth_x": 4.0,
  "female_background_y_per_million": 1000.0,
  "full_male_y_per_million": 3000.0,
  "validated_min_fraction": 0.05,
  "validated_max_fraction": 0.4
}
```

The fixed F1 policy counts D21-eligible primary, mapped, QC-passing,
non-duplicate reads with MAPQ at least 20 on canonical `chr1`–`chr22`, `chrX`,
and `chrY`. The numerator counts read starts on hg38 chrY outside its two
zero-based, half-open pseudoautosomal intervals; the denominator includes all
eligible reads, including chrY PAR reads. Rates are per million denominator
reads. The linear estimate is `(observed chrY rate - female background) /
(full male response - female background)`. The Lab reports null outside the
calibrated rate or validated fraction interval; it never extrapolates or clips.
It requires a full input with at least two million eligible reads, canonical
hg38 chromosome layout, and the calibration's minimum depth.

The software will not run a `synthetic` calibration. `--allow-synthetic` must
be supplied to register one for mechanics testing. Synthetic registrations use
a separate namespace and cannot consume the real calibration slot. Inspect one
with `traceback lab fetal chry-inspect INPUT_ID --synthetic --root R`. A damaged
registration needs the exact original bytes restored from backup; a different
real calibration requires a new input identity. No registered calibration is
silently overwritten. The raw chrY count and rate remain visible as exploratory,
sex-informative measurements even when the calibrated fraction is null.
`chry-inspect` shows the two calibration rates and counting protocol needed to
recalculate the fraction, while withholding the operator's free-text labels.

The numeric result still requires an independent ONT-matched accuracy,
precision, and detection-limit study before biological or clinical use. Male
donor DNA, sex-chromosome variation, fetal-sex error, and mapping bias can
confound chrY signal. Source: [Xu et al., PLOS ONE 2016](https://doi.org/10.1371/journal.pone.0146997)
describes the female-background/full-male chrY response method on a different
sequencing platform. No calibration constants from that paper are embedded here.
