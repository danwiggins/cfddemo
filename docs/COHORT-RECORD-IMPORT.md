# Verified cohort record import (D06)

D06 adds a protected local binding index between D05 cohort members and the
existing immutable aggregate-result catalog. It accepts only the exact signed
result-bundle v2 schema with fragment-measurement v1 through an explicit reader
registry. A bundle is copied through descriptor-relative, no-follow opens,
bounded by the result catalog's fixed eight-file inventory and 36 MiB total
limit, and verified against independently provisioned offline result-key trust.
The signature purpose, current key revocation state, manifest/content digests,
derived chart/report, method identity, and current method authority are checked
before the result row is indexed.

The cohort layer then binds that verified result to one exact member of the
canonical D05 manifest history. It rechecks the manifest against the live
protected linkage store and independently pinned provider trust both before and
after bundle verification. The bundle method definition must equal the cohort's
measurement anchor. Opaque result-catalog aliases are deterministically derived
from protected provider, analysis, run, and collection tokens so the same result
can be reused across immutable cohort versions without exposing those tokens in
the public result reference.

Bindings are canonical, append-only files in a mode-0700 local directory; each
file is mode 0600 and capped at 128 KiB. The catalog supports at most 100,000
bindings. Duplicate exact imports are idempotent, while a different result for
the same member and manifest or the same result assigned to another member in
that manifest is a conflict. Reads recheck the live D05 authority, the exact
binding bytes, the configured reader-registry digest, the stored immutable
bundle, and current result-key trust. A later linkage change or key revocation
therefore withholds the binding rather than turning stale evidence into an
available longitudinal record.

This implementation remains synthetic and local. The binding index contains
protected analysis and provider identifiers and must stay inside provider
managed encrypted storage and backup policy. It contains no raw sequence,
read-level data, free-text identity, network transport, clinical status, or
diagnostic interpretation. Development trust cannot establish provider
qualification; the downstream longitudinal capability remains disabled until
its separate dependency and qualification gates pass.

## Failure behavior and rollback

- Tampered, revoked, wrong-purpose, malformed, over-bound, symlinked, and
  unsupported-version bundles fail before result indexing.
- Invalid/stale D05 histories, nonmembers, wrong measurement anchors, and
  shadowed catalog/reader authority fail before cohort binding.
- Index corruption, unexpected files, root replacement, stale linkage, or
  current trust failure blocks reads with a sanitized error.
- Rollback stops new D06 imports and returns to the prior application release.
  Existing immutable result objects and binding bytes remain retained for
  offline verification; they are not rewritten to make rollback succeed.

Focused evidence lives in `tests/test_cohort_import.py`; the pre-existing
result-catalog adversarial and 10,000-record query tests continue to exercise
bounded filesystem import, exact SQLite schema, concurrency, and pagination.
