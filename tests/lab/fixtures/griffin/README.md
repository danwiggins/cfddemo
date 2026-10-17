# Synthetic Griffin-format parser fixture

`Synthetic_demo.GC_corrected.coverage.tsv` contains no observed sample values.
Run `python -m tests.lab.generate_griffin_fixture` from the repository root to
regenerate it exactly. Its SHA-256 is
`6dde4e889ce0feff498de054d5b629c24b0092e63fbd035ac99be86250fafdf0`.

The 132 bin-start coordinate columns (−990 through +975 bp in 15 bp steps)
and trailing metadata column names model Griffin's public corrected-profile
TSV format at pinned upstream commit
`b624c7a2ea7b8758b6eebd424066c815bfeece61`. Every numeric data value,
site label, and sample label is generated locally. Parser tests verify the
fixture's exact bytes, full coordinate grid, metadata, and rejection of a
renamed coordinate column. The separate synthetic workflow proof under
`docs/proofs/griffin/` exercises the pinned upstream code without committed
real-sample output.
It tests the corrected-profile parser only. The Lab's paired-end, 100–200 bp
window and five-million D21 pair gates run on the full or selected BAM in a
timed worker; this TSV fixture does not exercise those gates.
