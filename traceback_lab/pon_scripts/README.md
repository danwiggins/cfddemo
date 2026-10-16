`createPanelOfNormals.R` is the unmodified upstream ichorCNA script from
[`broadinstitute/ichorCNA` commit `5bfc03ed854f0e93fe5b624c97c1290fa0053837`](https://github.com/broadinstitute/ichorCNA/blob/5bfc03ed854f0e93fe5b624c97c1290fa0053837/scripts/createPanelOfNormals.R).
Its SHA-256 is `64d67f0c6526d5de1fe914c83a5ab5dccf63140f23b0ad9f4260f9a282642e05`.
Upstream licenses it under GPL-3.0. It runs as a separate, isolated R process;
the Python implementation does not import or modify it.

`validatePanelOfNormals.R` is a Lab-owned output check, also run as a separate
isolated R process. The builder accepts no panel as an asset until that check
passes.
