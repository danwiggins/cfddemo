# L0 development control plane

Status: first implementation wave; synthetic data only.

L0 owns scope, staffing sessions 1–5, interface arbitration, integration,
verification, and the PR queue. The reviewed PRODUCT-SPEC.md and EPICS.md are
the product authority. PRODUCT-PLAN.md is historical. The public Streamlit
demo remains separate. No deployment, main-branch merge, real genomic
processing, or protocol approval is implied by this wave.

## First-wave PR stack

| Session | Branch | PR base | Ownership and outcome |
|---|---|---|---|
| 1 | l0/s1-contracts | main | E1 strict shared contracts, canonical serialization, synthetic fixture generators, migration rules; includes preserved planning baseline |
| 2 | l0/s2-runner | l0/s1-contracts | E2 SQLite WAL jobs, idempotency, fenced leases, sealed snapshots and receipts, crash recovery |
| 3 | l0/s3-measurement | l0/s2-runner | E3/E5/E6 synthetic modBAM preflight and complete aligned reference-span scan, deterministic aggregates |
| 4 | l0/s4-signing | l0/s3-measurement | E8 development Ed25519 trust, canonical bundles, offline verification, export allowlist, claims/privacy tests |
| 5 | l0/s5-operator | l0/s4-signing | E7 and E2 integration: offline doctor/demo/inspect/verify and job/recovery CLI, protocol approval rendering, operator documentation |

All sessions start from one preserved baseline and can work in parallel in
their owned modules. Before finalizing its PR, each session merges its completed
predecessor branch and reruns relevant tests. No force pushes or shared-checkout
editing. Each PR diff is reviewed against its immediate predecessor, and the
complete stack is validated against main. PRs remain drafts until dependencies
and acceptance checks pass. L0 owns retargeting and any later landing decision.

## File ownership

- S1: traceback_runner/contracts.py, new shared contract modules,
  traceback_runner/fixtures.py, tests/test_runner_contracts.py, contract tests,
  docs/RUNNER-CONTRACTS.md. Publish interfaces early. Preserve compatibility
  where possible; explicitly version any behavior change.
- S2: traceback_runner/store.py, snapshots.py, receipts.py, runner.py and
  corresponding focused tests; docs/RUNNER-RECOVERY.md.
- S3: traceback_runner/preflight.py, measurement.py and focused tests;
  docs/MEASUREMENT-CONTRACT.md. Reuse existing pure computation only when it
  satisfies the complete-scan contract; do not change demo behavior.
- S4: traceback_runner/signing.py, bundles.py, export.py and focused tests;
  docs/SIGNING.md. Own dependency updates in pyproject.toml and uv.lock.
- S5: traceback_runner/cli.py, __main__.py, operator.py, protocol.py and focused
  tests; docs/OPERATOR-GUIDE.md, DESIGN.md, synthetic UI-state fixtures.
- L0: this coordination document and cross-session interface decisions.

Request shared-file changes from the owner. Do not hard-code another session's
unpublished implementation or silently invent a second shared schema. Local
temporary adapters may be used during development, but final integration must
exercise the actual owned modules without fake success fallbacks.

## Common acceptance

1. Existing offline tests stay green; every behavior change has meaningful
   coverage. No AWS or network dependency in tests or the synthetic demo.
2. Generate synthetic BAMs at runtime; commit no sequence files or real inputs.
3. Identical complete validated input and workflow yield byte-identical
   measurement JSON. Missing modification tags do not block fragment analysis.
4. Interrupted, capped, or zero-eligible scans cannot publish measurements.
5. Stale lease tokens cannot commit; duplicate submission cannot duplicate jobs;
   crash recovery verifies immutable receipts and output digests.
6. Export contains only allowlisted aggregates, with no sequence, read IDs,
   local paths, sample labels, secrets, or raw genomic hashes.
7. Signatures verify offline, reject tampering and wrong/revoked keys, and
   distinguish development trust from future production trust.
8. Doctor, synthetic demo, and verification form a working local journey under
   five minutes. Do not imply that real hardware or wet-lab protocols are
   qualified, or that any upload occurred.

## Gates remaining after this wave

Real-data execution remains disabled until the specification's snapshot,
fencing, receipts, zero-egress execution, signing custody, measurement, and
migration gates have evidence. Pure synthetic in-process stages do not qualify
OCI isolation or Dorado. E0 scientific/protocol decisions, exact workflow and
asset versions, real POD5 qualification, operator study, and paid pilot remain
open. Cell origin, dosage, hosted services, and longitudinal analysis remain
deferred.

## Session handoff requirements

Report the branch, commit, PR URL, owned files, commands run and their results,
public module interfaces, dependency status, and remaining limitations. Report
actual blockers explicitly. Never treat a stub, metadata fixture, skipped test,
or self-declared approval as a completed release gate.
