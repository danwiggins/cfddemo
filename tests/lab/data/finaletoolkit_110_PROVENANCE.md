# FinaleToolkit 1.1.0 replay outputs

`finaletoolkit_110_motifs.tsv` and `finaletoolkit_110_delfi.tsv` are captured
outputs from FinaleToolkit 1.1.0 on the deterministic paired-end BAM and FASTA
created by `tests/lab/test_finaletoolkit_xcheck.py::_fixture`. The reference is
a synthetic hg38-labelled 300 kb chr1 plus 100 kb chrX; it is not a human
reference or biological qualification input.

The test's `test_actual_finaletoolkit_110_pair_parity` invokes the pinned tool
through `FinaleToolkitXcheck.run` with these arguments (paths vary by test run):

```
finaletoolkit end-motifs aligned.bam hg38.fa -k 4 --min-length 4 -q 20 -t 1 -o motifs.tsv
finaletoolkit delfi aligned.bam autosomes.chrom.sizes hg38.fa autosomes.100kb.bed --no-gc-correct --no-merge-bins --no-remove-nocov -q 20 -t 1 -o delfi.tsv
```

On 2026-10-17, the installed 1.1.0 tool ran under Python 3.11.16 on macOS and
reproduced both checked-in outputs byte for byte:

| Output | SHA-256 |
| --- | --- |
| motifs TSV | `f7a013fa86a28e111b2ff2f840198edc9921a25bfd0b13f2f6852893480405c0` |
| DELFI TSV | `1f33fb48217af100d8fc1b6e957c56d74ee84c988f99d454fc5cae4914190205` |

The replay test is unconditional and checks parsing and verdict plumbing. The
live-tool test is optional because CI does not install FinaleToolkit; it checks
the comparator against a separate run of the pinned package. These synthetic
checks do not establish real hg38 runtime, whole-genome agreement, or clinical
performance.
