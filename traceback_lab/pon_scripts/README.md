`createPanelOfNormals.R` is the unmodified upstream ichorCNA script from
[`broadinstitute/ichorCNA` commit `5bfc03ed854f0e93fe5b624c97c1290fa0053837`](https://github.com/broadinstitute/ichorCNA/blob/5bfc03ed854f0e93fe5b624c97c1290fa0053837/scripts/createPanelOfNormals.R).
Its SHA-256 is `64d67f0c6526d5de1fe914c83a5ab5dccf63140f23b0ad9f4260f9a282642e05`.
Upstream licenses it under GPL-3.0. `buildPanelLab.R` is a Lab adaptation of
that same commit, also GPL-3.0. It retains the upstream WIG loading,
`loadReadCountsFromWig` correction, per-source copy columns, row median, and
RDS output. Its fixed parser replaces upstream `optparse` and `eval(parse())`
options: the Lab accepts only absolute file paths and always builds autosomal,
UCSC-style median panels. It passes `genomeStyle="UCSC"` and the autosomal
`chrNormalize` directly to ichorCNA's correction helper, whose defaults use
numeric chromosome labels. The adapted builder runs as a separate, isolated R
process with the signed ichorCNA toolchain; neither the signed lock nor driver
is changed. The build record pins the original source, adapted driver, and
validator by separate SHA-256 digests, and records the exact source-to-LOO
asset mapping.

`validatePanelOfNormals.R` is a Lab-owned output check, also run as a separate
isolated R process. It checks source columns, their arithmetic median, and
full-versus-leave-one-out bin identity before a panel becomes an asset.
Its bytes are pinned both before a build and at isolated R execution.

## Current Lab selection boundary

`traceback_lab.pon_consumer` binds a selected immutable panel digest into
copy-number's per-run execution identity and verifies the same selected bytes
before worker publication. An explicit `--cohort` requires a built internal,
protocol-matched cohort; an unbuilt default uses the no-PoN baseline. Run selection refuses a mismatched protocol. If the implicit built panel
lacks required tools or method assets, copy number is skipped. Read-side
cohort comparison may retain independently compatible metrics with a warning,
while PoN-dependent results stay unavailable. This source and its GPL terms
remain separate from signed runner release authority and scientific
qualification.
