# Traceback synthetic operator guide

Status: first development wave; synthetic data only.

This guide covers the local CLI implemented for the synthetic vertical slice.
It does not qualify hardware, MinKNOW, Dorado, a wet-lab protocol, a reference,
or a real-data workflow. The existing Streamlit evidence demo is unchanged.

## First five minutes

Use the installed console command. The journey is offline and does not need an
account, cloud credential, network connection, or genomic input.

```bash
traceback doctor
traceback demo --root ./traceback-synthetic
traceback verify ./traceback-synthetic/records/<bundle> \
  --trust-store ./traceback-synthetic/trust
```

`demo` prints the exact bundle and trust-store locations to use. The command
generates a tiny synthetic BAM at runtime, exercises the durable local runner,
performs a complete aligned-reference-span scan, builds a development-signed
bundle, and verifies it against a separately configured development trust
root. It does not fall back to fixture metadata when a stage fails.

Successful completion is displayed as **Signed local record ready
(development trust)**. This means only that the synthetic local bundle passed
the first-wave validation and offline signature checks. Nothing was uploaded.

## Commands

```text
traceback doctor [--json]
traceback protocol show [--json]
traceback demo [--root ROOT] [--json]
traceback preflight INPUT [--root ROOT] [--json]
traceback run INPUT [--json]
traceback status JOB_ID [--root ROOT] [--json]
traceback logs JOB_ID [--root ROOT] [--json]
traceback pause JOB_ID [--root ROOT] [--json]
traceback resume JOB_ID [--root ROOT] [--json]
traceback retry JOB_ID [--root ROOT] [--json]
traceback inspect BUNDLE [--json]
traceback verify BUNDLE --trust-store TRUST_STORE [--json]
traceback assets install --release-evidence ENVELOPE \
  --trust-store TRUST_STORE --role-policy POLICY \
  --authority-head HEAD --asset ASSET_ID --version VERSION \
  --package PACKAGE [--root ROOT] [--json]
traceback assets verify --release-evidence ENVELOPE \
  --trust-store TRUST_STORE --role-policy POLICY \
  --authority-head HEAD --asset ASSET_ID --version VERSION \
  [--root ROOT] [--json]
traceback support-bundle JOB_ID --output OUTPUT [--root ROOT] [--json]
```

`preflight` is technical inspection only. A passing report does not approve
real-data execution. `run` rejects real-data execution in this wave rather than
simulating success. File-selection arguments may be local paths; JSON output,
support bundles, and exported record content do not include them.

## Offline synthetic assets

`assets install` accepts one bounded, uncompressed synthetic package and never
downloads content. The signed release envelope, public trust store, role
policy, and current authority head are separate local inputs. The CLI refuses
to infer trust from either the envelope or the asset package. Asset ID and
version must be selected explicitly.

`--root` retains its normal CLI meaning: the Traceback product root. Asset
objects and registrations are stored beneath its private `assets` directory.
Command output never includes input or registry paths.

Install succeeds only when authority is verified, lifecycle is active, the
package matches the authority-bound asset reference, byte integrity passes,
and the registry can retain its 20% free-space floor. Unknown, expired,
ambiguous, invalid, or revoked authority fails closed before asset staging.

`assets verify` reports these dimensions separately:

- `installed`: whether an identifier/version registration exists;
- `integrity`: `absent`, `valid`, or `invalid` for local bytes;
- `authority`: `verified`, `invalid`, or `unknown`;
- `lifecycle`: `active`, `revoked`, or `unknown`; and
- `verified_as_of` / `fresh_until`: the bounded offline authority window.

An existing object may remain integrity-valid when authority is unknown or
revoked. It remains unavailable for use and is not silently deleted. Asset
installation never authorizes execution, real input, or a qualification probe.

## Stable exit codes

| Code | Meaning |
|---:|---|
| 0 | Command completed successfully |
| 2 | CLI usage error |
| 3 | Blocked or unsupported operation |
| 4 | Local job, bundle, or trust material was not found |
| 5 | Bundle, asset package/integrity, signature, or authority-input verification failed |
| 6 | Retryable local runner failure |
| 7 | Unexpected internal failure |

Human output is the default. `--json` emits one canonical JSON object and uses
the same exit code as human output.

## Recovery

- **Stale status:** wait for or restart the local runner, then run `status`
  again. A successful local status read is a fresh observation even for an old
  completed or paused job. Do not start a duplicate job while freshness is unknown.
- **Paused:** use `resume` after the reason for pausing is resolved.
- **Interrupted process:** `resume` recovers a RUNNING job after its worker lease
  expires. An unexpired lease blocks recovery so a live worker is not displaced.
- **Retryable failure:** inspect `logs`, follow the one stated remediation,
  then use `retry` followed by `resume`. There is no background queue worker;
  retry queues the job and resume executes it. Verified completed stage receipts
  are reused.
- **Verification failed:** do not treat the record as ready. Confirm that the
  development trust store was configured independently from the bundle and
  rerun `verify`.
- **Blocked preflight:** correct the named input or compatibility problem.
  Passing preflight still does not enable real-data processing.

## Protocol & Setup boundary

`traceback protocol show` renders versioned setup content. Any wet-lab row
without an approved owner, source, version, and review date is displayed only
as `Instruction withheld pending scientific approval`. Generic instructions
are never substituted.

The first-wave protocol content deliberately withholds collection and library
preparation instructions. Exact materials, timing, equipment, software,
reference assets, and acceptance thresholds remain approval gates.

## Support bundles

`support-bundle` writes a local diagnostic artifact containing stable error
codes, software version, safe resource state, and redacted events. It excludes
local paths, filenames, sample labels, read identifiers, sequence, environment
secrets, and raw genomic hashes. Creating a support bundle does not upload it.

## Remaining gates

The synthetic workflow does not prove rootless OCI isolation, zero-egress
execution, Dorado behavior, MinION compatibility, real POD5 processing,
scientific validity, operator usability, signing-key custody, or a qualified
wet-lab protocol. Those gates remain open after this development wave.

Development public trust entries are accumulated, preserving prior keys and
revocations; ephemeral private keys are not persisted. Record copies are verified
in private staging, flushed, and published with an exclusive atomic rename on
macOS/Linux. An invalid existing record is retained and a verified recovery sibling
is published; no existing user output is deleted or replaced. CLI mutations are
serialized per workspace by an OS lock released on process exit.
