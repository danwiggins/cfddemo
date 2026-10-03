# L0 development control plane

Status: first implementation wave; synthetic data only.

L0 is the higher-model coordinator and reviewer. L0 owns sprint planning,
feature scope, roadmap, staffing and implementation lanes, dependency
sequencing, interface arbitration, integration, verification, and the PR
queue. Sessions 1–5 are the implementers. The reviewed PRODUCT-SPEC.md and
EPICS.md are the product authority. PRODUCT-PLAN.md is historical. The public
Streamlit demo remains separate. No deployment, main-branch merge, real genomic
processing, or protocol approval is implied by this wave.

## First-wave PR stack

| Session | Branch | PR base | Ownership and outcome |
|---|---|---|---|
| 1 | l0/s1-contracts | main | E1 strict shared contracts, canonical serialization, synthetic fixture generators, migration rules; includes preserved planning baseline |
| 2 | l0/s2-runner | l0/s1-contracts | E2 SQLite WAL jobs, idempotency, fenced leases, sealed snapshots and receipts, crash recovery |
| 3 | l0/s3-measurement | l0/s2-runner | E3/E5/E6 synthetic modBAM preflight and complete aligned reference-span scan, deterministic aggregates |
| 4 | l0/s4-signing | l0/s3-measurement | E8 development Ed25519 trust, canonical bundles, offline verification, export allowlist, claims/privacy tests |
| 5 | l0/s5-operator | l0/s4-signing | E7 and E2 integration: offline doctor/demo/inspect/verify and job/recovery CLI, protocol approval rendering, operator documentation |

Task IDs for handoff discovery:

- L0: `01a0e07f-9738-7113-a9ed-88854d60d661`
- S1: `01a0e088-3a0b-71e2-82db-8ce7a1643e16`
- S2: `01a0e088-8fbb-7782-a412-b2edc0042837`
- S3: `01a0e088-f546-7a03-a9c2-4451accb99e6`
- S4: `01a0e089-59e8-71c1-b5fe-60a49b372362`
- S5: `01a0e08a-2f4a-76f1-8234-0bd2431bd8ef`

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

## CI

`.github/workflows/ci.yml` runs on every pull request and on pushes to `main`,
on `macos-latest` and `ubuntu-latest` with Python 3.11. macOS team workstations
are the product host (security spec X1, #86), so only `ci (macos-latest)` is a
required check. The ubuntu job runs with `continue-on-error` and is
informational until a Linux-compat follow-up fixes the `/proc/self/fd` bundle
path and the fork-based tests. Each job runs
`uv sync --frozen`, `uvx ruff@0.7.4 check .`, and
`pytest -m "not slow" --timeout 600` in two steps: everything except the
local web service tests under `pytest -n auto`, then `tests/web` and
`tests/test_reader_cli.py` serially. The serial step exists because every
running local web service holds one host-wide `/tmp` lock; it folds back into
the parallel step once golden-path item A3 (per-state-root web lock) lands.

`.github/workflows/slow.yml` runs `pytest -m slow` nightly, on demand, and on
pull requests that touch `traceback_runner/product_gates.py`,
`traceback_runner/web/`, or the product-gates fixture. Today the only slow test
compares the live product-gates harness with the frozen
`tests/fixtures/product_gates/foundation_report.json`. After a contract change,
regenerate the fixture with
`uv run python scripts/regenerate_product_gate_fixture.py`; never hand-edit it.

Branch protection is a manual repository-admin action (golden-path decision
D8). The operator runs, once `ci (macos-latest)` has reported on a pull request:

```
gh api -X PUT repos/danwiggins/cfddemo/branches/main/protection \
  -H "Accept: application/vnd.github+json" \
  -F 'required_status_checks[strict]=true' \
  -f 'required_status_checks[contexts][]=ci (macos-latest)' \
  -F 'enforce_admins=false' -F 'required_pull_request_reviews=null' -F 'restrictions=null'
```

`-F` sends `true`, `false`, and `null` as JSON literals; `-f` would send the
string `"true"` where the endpoint expects a boolean. The `slow` workflow is
deliberately not a required check: it is path-filtered and would block
unrelated pull requests.
