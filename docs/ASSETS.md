# Offline synthetic asset registry

This module installs only assets named by current, independently verified
development release evidence. It does not download anything, grant a license,
qualify a workstation or scientific method, authorize real input, or authorize
execution. Installed state, byte integrity, release authority, and lifecycle
currentness are separate results.

## Package format

`traceback.synthetic-asset-package.v1` is deliberately narrower than an archive:

1. the fixed bytes `TRACEBACK-ASSET\0V1\n`;
2. one unsigned, big-endian 32-bit canonical-JSON header length, capped at 256 KiB;
3. one canonical JSON header containing an `identity` encoding, the complete
   frozen `AssetReference`, and its domain-separated reference digest;
4. exactly one uncompressed payload whose length and SHA-256 are declared by
   `AssetContentIdentity`.

There are no entry names, directories, links, devices, compression methods, or
multiple members. Unknown header fields, trailing bytes, partial payloads,
non-regular package files, and symlink package paths are rejected. Payloads are
streamed and have a 2 TiB format ceiling; actual installation is additionally
bounded by the target filesystem capacity and its free-space floor.

`build_synthetic_asset_package` exists to create tiny offline test fixtures. It
accepts caller-supplied bytes and never resolves a source or performs network I/O.

## Install transaction

`AssetRegistry` receives the registry root itself (the CLI maps its product root
to `<root>/assets`). An install:

1. parses the package and invokes `verify_release_asset_authorization` with the
   signed envelope, external public-key trust, external role policy, independent
   fresh authority head, exact expected release binding/package digest, selected
   asset ID/version, reference digest, and an injected aware-UTC time;
2. blocks unknown, invalid, expired, wrong-version, or revoked authority before
   creating a staging directory;
3. takes a process-shared POSIX lock, rejects conflicting duplicate identifiers,
   and rechecks capacity;
4. requires predicted available bytes after peak staging and registration cost
   to remain at or above 20% of total filesystem bytes;
5. copies into a private registry-local staging directory while hashing, fsyncs
   the payload and directory, and atomically publishes the content-addressed
   object;
6. fsyncs and atomically publishes the canonical identifier/version registration.

The registration rename is the commit point. A crash before it can leave only an
unregistered content-addressed object, which readers ignore. A later installer
verifies and adopts matching orphan bytes. A crash after it is an idempotent
success on retry. The lock serializes cooperating installers so they cannot
double-spend the same observed free capacity or overwrite an identifier with a
different reference.

The 20% check is admission control, not a filesystem quota: unrelated writers
outside the registry lock can still consume space after the check.

## Verification semantics

`verify` rehashes the installed object and re-runs current release authorization.
Its result reports independently:

- `installed` and `integrity_status` (`absent`, `valid`, or `invalid`);
- release `authority_status` and its failure code;
- current asset `lifecycle_status` (`active`, `revoked`, or `unknown`);
- whether the historical registration reference equals the current reference.

An old object may therefore remain byte-valid while current authority is unknown
or revoked. Historical bytes are retained for audit, but both
`execution_authorized` and `qualification_established` remain false.
