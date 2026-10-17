# Public ONT patient-source mixtures

This standalone utility supports the public-source experiment in demo spec D25(c).
It is separate from `lab simulate cna` and does not change production controls or
any signed method. Outputs are **patient-source eligible-read mixtures**, not
known tumour-fraction controls. Known-status patient material does not establish
independent tumour purity, and the background is not assumed tumour-free.

From the repository's locked environment on Linux or macOS:

```sh
python -m scripts.validation.mix_public_bams BACKGROUND.bam PATIENT.bam NEW_OUTPUT_DIRECTORY --total 1000000 --seed 1729 --reference-fasta MATCHING_REFERENCE.fa
```

Resolve the actual input filenames and matching reference before running. The
utility does not download data, infer status from filenames, inject missing
basecalling models, or execute Lab methods. Data-use permissions remain the
operator's responsibility. Before later Lab replay, register the exact matching
reference and use the normal method/toolchain checks; retain skips as skips.

## Inputs and selection

- Both inputs must declare coordinate order and all read groups must declare
  `PL:ONT`. The utility verifies record ordering against **SQ order**, including
  the unplaced-record tail. Ordered SN/LN dictionaries must match; differing M5
  values are rejected when both sources provide them. Missing M5 is disclosed.
- Optional uncompressed FASTA is streamed to verify ordered names, lengths and
  every available source M5; its file SHA256 is recorded from that same stream.
  Sequence lines may be unwrapped; FASTA headers are bounded to 64 KiB. Missing source M5 still
  prevents independent proof of the reference originally used for alignment.
- Selection uses the existing D21 record predicate: exclude flags
  0x4/0x100/0x200/0x400/0x800, MAPQ at least 20, canonical nuclear chr1–22/X/Y.
  This matches the Lab read-summary nuclear default, excludes chrM, and refuses
  references with no canonical nuclear contigs rather than using its toy fallback.
  Eligible paired records are refused; this is a single-end ONT experiment.
- `--total` is positive and divisible by 100. Each mixture has exactly that many
  reads, with source 1 fractions .01/.03/.10. Both sources must have enough unique
  eligible reads for all outputs. Identical-content inputs and duplicate eligible
  qnames within either source are refused, including nonadjacent duplicates.
- Fixed-seed SHA256 ranking chooses reads without replacement. Original record
  ordinal breaks hash ties. Selection nests within each source as its requested
  count grows; reads can legitimately recur across the three separate mixtures.
  Source ranking depends on source index, seed and original qname. Original record
  order must also be identical for byte-exact replay and collision tie-breaking.

## Outputs, privacy and resources

The new directory contains three coordinate-sorted BAMs, CSI indexes and a JSON
manifest. The manifest records source content hashes, original headers, total and
eligible counts, selected counts, exact rational q, seed, filter, software versions,
reference evidence and output hashes. `true_tumour_fraction` is null.

MM/ML, sequence, qualities and other source tags are preserved. RG/PG IDs and their
links receive source namespaces with original headers retained in the manifest.
Qnames become source/record-ordinal identifiers; private `ZQ` stores the original
qname and `ZS` stores source 0/1. Existing ZQ/ZS tags are refused. Source headers,
private tags and BAMs may contain identifying material: keep all actual outputs
local; never commit them, real read names or real source manifests.

A disk-backed SQLite index rejects duplicates and selects exact rank boundaries.
Its cache is 8 MiB; BAM merging holds one record from each source, without building
an in-memory read set or invoking an unbounded sort. `--max-work-bytes` defaults
to 20 GiB, bounds SQLite pages, and checks total staging files every 4096 selected
records and after each index/manifest. This is a checkpointed budget, not a strict
per-byte filesystem quota: compressed writes/index creation can transiently exceed
it between checks. Free-space errors fail construction. Input hashes are rechecked
before publication to reject source mutation.

Successful construction publishes the directory with the repository's exclusive
atomic rename helper. Existing destinations are never replaced, even if created
concurrently. Failures remove only the utility's own staging directory. A killed
process can leave a `.mixture-*` directory; inspect that private staging directory
before removing it. No partial destination is published.

Run the focused offline suite with:

```sh
python -m pytest tests/test_public_source_mixtures.py
```

Synthetic tests establish engineering behavior only. A real public-source replay
and independently characterized known-TF recovery remain separate qualifications.
