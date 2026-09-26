# Algorithms and evidence design

## 1. Fragment length

The pipeline streams alignment records, keeps primary records, deduplicates by
read ID across ordered inputs, requires a positive query length, and caps
accepted lengths at 1,000,000 bp. Every displayed statistic uses the same
denominator.

```text
BAM records
  → primary-record filter
  → first eligible record per read ID
  → raw query-sequence length
  → histogram + mode + median + fraction above 1 kb
```

The public chart uses raw query-sequence length. It does not subtract a fixed
adapter length. That definition matters: an aligned reference span and a raw
length minus 45 bp are different measurements.

## 2. Cell-origin deconvolution

```text
aligned modBAM
  → CpG methylation calls
  → overlap with Loyfer U250 marker regions
  → classify each informative fragment as U, X, or M
  → count-weighted marker methylation vector
  → non-negative least squares against the cell atlas
  → normalize mixture weights
  → seeded bootstrap confidence interval
```

For a marker with informative calls:

- **U:** methylated fraction ≤ 0.25
- **M:** methylated fraction ≥ 0.75
- **X:** intermediate

The observed marker value is the methylated contribution divided by informative
calls. NNLS finds non-negative cell-type weights whose atlas profile best
reconstructs that observed vector. Bootstrap resampling gives a stability
interval; it is not a clinical confidence interval.

The comparison chart uses the observed 23-donor healthy-plasma distribution
from the registered Loyfer reference: min–max, IQR, median, sample estimate, and
bootstrap interval.

## 3. Bounded AI evidence review

The model does not calculate fragment or methylation results. It receives:

- one claim;
- allowlisted tool capabilities;
- bounded source excerpts;
- aggregate registered results.

It first selects one permitted check. Deterministic code executes or retrieves
the check. The model then returns a strict structured assessment with status,
citations, revised wording, assumptions, and missing validation.

Validation rejects unknown evidence IDs, invented numeric fields, changed
values, mismatched units, changed denominators, changed filters, and stale claim
bindings.

## Known limitations

- One research sample does not establish diagnostic performance.
- The fragment bundle is a registered subset with unverified sample linkage.
- Cell-origin estimates depend on marker coverage and atlas assumptions.
- Shallow whole-genome sequencing may miss low tumor fractions.
- The report contains a known reproduction mismatch between its updated
  fragment method and older fixed-subtraction instructions.
