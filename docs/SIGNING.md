# Synthetic record signing and export boundary

Status: first-wave development implementation; synthetic data only.

This module proves the local bundle and offline-verification contracts. It does
not qualify production key custody, real genomic inputs, MinION hardware,
laboratory protocols, a workflow release, or scientific performance. No cloud
upload exists in this path.

## Public interfaces

`traceback_runner.signing` provides:

- `generate_development_keypair(purpose)` for ephemeral Ed25519 keys in the
  `development-synthetic` namespace;
- `sign_bytes` and `verify_signature`, which require an explicit `release` or
  `result` purpose;
- `TrustStore`, with independently supplied public keys and local revocation.
  `TrustStore.revoke` mutates only that instance; the protected, forward-only
  result trust authority is `evidence_inspector.result_trust_registry`
  (`docs/RESULT-TRUST-REGISTRY.md`), which can return a fresh `TrustStore` for
  its current head. D07, the E04 result catalog (`result_trust_registry=`),
  and `traceback verify --trust-registry` read that registry live, so a
  revocation there applies to their next verification;
- `development_trust_bytes` and `load_development_trust` for strict, canonical,
  public-only trust files. Private keys are never serialized.

`traceback_runner.bundles` provides:

- `build_result_bundle(output_dir, measurement=..., provenance=...,
  method=..., signing_key=...)`;
- `verify_bundle(bundle_dir, trust_store)` for verified offline loading; and
- `inspect_bundle(bundle_dir)` for explicitly unverified metadata inspection.

Expected verification failures derive from `BundleError` or `SigningError`.
Callers must not present `inspect_bundle` output as trusted.

## Bundle contract

The only accepted files are:

```text
bundle-manifest.json
measurements/fragment-length.v1.json
charts/fragment-length.v1.json
provenance.json
limitations.json
report.html
checksums.sha256
bundle.sig
```

Measurement, chart, provenance, limitations, manifest, signature, and public
trust JSON use canonical UTF-8 serialization. Chart rows are derived from the
validated measurement rather than accepted as caller-authored numbers. The
report is produced from fixed text with escaped identifiers. Measurement and
chart bytes are independent of signature bytes.

The signed payload binds the supported bundle schema, development trust
namespace, result-key purpose, exact filename inventory, and SHA-256 digest of
the canonical checksum inventory. Bundle v2 also binds the exact method ID,
method version, and method-definition digest. Verification recalculates hashes and sizes
from actual files, reparses every structured artifact with closed schemas,
re-derives the chart and report, and checks measurement/provenance identity.
It rejects missing or extra files, duplicate JSON keys, unsupported versions,
noncanonical JSON, malformed checksum paths, symlinks, non-regular entries,
unknown keys, revoked keys, wrong-purpose keys, and invalid signatures.
Verification also reapplies the current export privacy and claims policy after
signature verification; a trusted producer cannot sign policy-forbidden text
into an accepted record.

Untrusted directories are bounded before authentication. The reader opens each
allowlisted entry without following a final symlink, verifies the opened file is
regular, checks its declared filesystem size before reading, reads at most the
per-file limit plus one byte, and enforces a 36 MiB aggregate cap. Measurement
and chart JSON are each limited to 16 MiB; the fixed report is limited to 2 MiB;
manifests, provenance, limitations, checksums, and signatures have smaller
role-specific limits. These are format limits, not scientific thresholds.

The signature does not make SHA-256 a privacy mechanism. Export provenance
uses a provider-keyed HMAC commitment; ordinary input digests remain local.

## Privacy and claims boundary

The primary privacy control is a closed, explicit field allowlist. The builder
does not copy or recursively redact arbitrary upstream objects. Unknown fields
fail validation. There are no export fields for sequence, base qualities, read
IDs, local paths, sample labels, secrets, filenames, or ordinary genomic
digests.

Pattern checks for absolute paths, sequence-like text, raw hashes, sensitive
field names, and prohibited claims are defense in depth. They are not described
or tested as a complete blacklist. Fixed report and limitations templates keep
free-form upstream prose outside the record. Publication requires a complete,
nonempty, reconciled synthetic aggregate; partial, capped, interrupted, failed,
or zero-eligible scans cannot be serialized as publishable measurements.

## Key purpose and custody policy

Release and result keys are distinct Ed25519 purposes. A key registered for one
purpose cannot sign or verify the other. Development keys are generated at
runtime and are accepted only in the `development-synthetic` namespace. A
development signature can never establish trust for future non-synthetic data.
The bundle contains a key identifier, not a self-authorizing public key or trust
root. Verification therefore requires a public key supplied by the operator's
separate trust configuration.

For this synthetic wave, private keys live only in process memory. The public
trust document may be deliberately saved for a later offline `verify`
invocation. Repository files, fixtures, logs, bundles, and PRs must never contain
private keys.

Before any pilot or real-data use, the workflow owner must implement and
qualify all of the following:

- protected key generation and non-exportable custody appropriate to the
  approved workstation and operator roles;
- authenticated public trust-root distribution with an out-of-band fingerprint
  and rollback-resistant versioning;
- a rotation ceremony that overlaps old and new public keys while preserving
  historical verification;
- signed, versioned, offline-capable revocation distribution;
- lost-key response: stop publication, revoke the key, preserve affected
  records, investigate access, issue a new key, and never silently re-sign
  history;
- backup/recovery, separation of duties, access audit, expiry policy, and
  incident exercises; and
- independent security, privacy, claims, scientific, and operational approval.

Local revocation enforcement is implemented, but authenticated distribution of
revocation state is not. Key expiry, hardware-backed custody, multi-party
approval, production namespaces, release-manifest signing workflows, and pilot
trust roots remain deliberately unimplemented qualification gates.

## Offline synthetic example

```python
from pathlib import Path

from traceback_runner.bundles import build_result_bundle, verify_bundle
from traceback_runner.signing import (
    KeyPurpose,
    development_trust_bytes,
    generate_development_keypair,
    load_development_trust,
)

key = generate_development_keypair(KeyPurpose.RESULT)
bundle = build_result_bundle(
    Path("synthetic-record"),
    measurement=allowlisted_measurement,
    provenance=allowlisted_provenance,
    method=allowlisted_method_identity,
    signing_key=key,
)
public_trust = development_trust_bytes(key)
verified = verify_bundle(bundle, load_development_trust(public_trust))
```

The example is intentionally local and synthetic. Saving the public trust bytes
is explicit; saving or uploading the bundle is also explicit. No implicit
upload, deletion, or network operation occurs.
