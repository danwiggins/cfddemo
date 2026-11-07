# L0 development control plane

> **Historical.** Everything above the CI section describes the first
> implementation wave: its sessions, branches, file ownership, acceptance and
> handoff rules. Current process is in `docs/EPICS.md` and this repo's PR history.
> The CI section at the end is current.

Status: first implementation wave (historical); synthetic data only.

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
on `macos-latest` and `ubuntu-latest` with Python 3.11. Both `ci (macos-latest)`
and `ci (ubuntu-latest)` are required checks. macOS team workstations remain the
product host (security spec X1, #86); the Ubuntu job catches portability
regressions. macOS installs `samtools` and `minimap2` with Homebrew. Ubuntu
installs pinned, checksummed upstream `samtools` 1.21 and `minimap2` 2.28 over
its older distribution copies. Each job verifies that `uv.lock` is tracked,
runs the literal `uv sync` command, and requires no lockfile diff; then it runs
`uvx ruff@0.7.4 check .` and
`uv run pytest -p no:cacheprovider -n auto --timeout 600 -m "not slow" --durations=10`.
Web service and Lab tests are included. The local web service's single-instance
lock is per state directory (golden-path item A3), so parallel workers on
distinct temporary roots do not collide.

### Reproducing an Ubuntu-only failure

When a pull request fails only in `ci (ubuntu-latest)` (typically headless
Chrome font and layout metrics, or Linux-only tool behaviour), reproduce it on
a Mac in a Linux container built from `scripts/ci/ubuntu-container/Dockerfile`
(Ubuntu 24.04, Google Chrome stable, the same pinned `samtools` 1.21 and
`minimap2` 2.28 releases, uv 0.11.10 with Python 3.11). Docker must be
installed and running. From the checkout to test:

```sh
scripts/ci/ubuntu-container/run.sh tests/lab/test_landing.py::test_landing_pixel_budget_at_1280x800 -n 0
```

The script builds the `traceback-ci-ubuntu` image when it is missing
(`--rebuild` forces a rebuild after a Dockerfile change), mounts the checkout
read-write at `/work`, runs `uv sync --frozen` into a throwaway environment
(`UV_PROJECT_ENVIRONMENT=/tmp/venv`, `UV_CACHE_DIR=/tmp/uvcache`, so the host
`.venv` is never touched), points `CHROME_BIN` at a wrapper that runs Chrome
with `--no-sandbox --disable-gpu --disable-dev-shm-usage`, and passes the
pytest exit code through. Without arguments it runs
`-q -p no:cacheprovider -n 0 --timeout 600 -m "not slow"`; `--shell` opens
bash in the synced container instead.
`tests/test_ci_ubuntu_container_pins.py` fails when the Dockerfile and
`ci.yml` tool pins drift.

What differs from hosted CI: the image is forced to `linux/amd64`, so on
Apple silicon it runs under emulation and is several times slower; use `-n 0`
for browser tests so emulated Chrome instances do not compete for CPU inside
the harness timeouts. The Chrome build is whatever Google ships as current
stable, not the copy on the hosted runner image, and the container runs as
root. A green run here is never a substitute for the hosted
`ci (ubuntu-latest)` job; it is a reproduction aid, and the required check
remains the gate.

### Headless Chrome harness budgets

The Lab browser tests drive Chrome through `_dom_cdp` in
`tests/lab/test_lab_server.py` (`_dom` in `tests/lab/test_landing.py` wraps it).
Chrome always starts with `--remote-debugging-port=0` in the test's own
profile; the harness deletes any old `DevToolsActivePort` before launch, reads
the ephemeral port from the new one, and checks that `/json/version` on that
port is the browser that wrote the file. No test uses a fixed DevTools port.

Waits grow with the host's one-minute load average per CPU
(`os.getloadavg()[0] / os.cpu_count()`): factor 1.0 at or below one runnable
process per CPU, rising linearly to a cap of 2.0. An idle host keeps the
historical timings.

| Wait | Base | Cap |
|---|---|---|
| DevTools page-target discovery (polls `/json/list`, 50 ms rising to 500 ms) | 15 s | 30 s |
| One CDP command response | 15 s | 30 s |
| Whole DOM wait (the test's `timeout`, default 90 s) | `timeout` | `2 x timeout`, at most 240 s (or `timeout` if larger), inside the 600 s pytest timeout |

Retries, each once only and inside the same overall budget:

- If Chrome never writes a usable `DevToolsActivePort` (or exits first), it is
  killed: a running leader with its process group, while a leader that already
  exited is never used as a group target. The launch group is then swept by
  pid from `ps -ww -A -o pid=,pgid=,stat=,command=` (`-ww` because BSD ps cuts
  command lines to about 79 columns without a tty): the group counts as this
  test's when ANY live member carries its `--user-data-dir` (many headless
  helpers do not carry it), and then every live member is killed; zombies are
  ignored. Only when no member survives a 2 s settle are the profile's
  `SingletonLock`/`SingletonSocket`/`SingletonCookie` removed and Chrome
  launched once more; otherwise the relaunch is skipped and the test fails with
  `relaunch skipped: launch group <pgid> still has live members [...]`. The same
  sweep runs after every browser is stopped; a survivor or a `ps` failure there
  is a cleanup error.
- If the first page WebSocket attach (before any emulation or navigation)
  fails with a socket error or timeout, the harness re-discovers the target and
  attaches again.
- If one DOM poll loses the socket (connection closed or reset), the harness
  re-attaches to the same page target and keeps polling; console messages and
  log entries that `Runtime.enable`/`Log.enable` replay are recorded once.
  This never happens in viewport mode:
  `Emulation.setDeviceMetricsOverride` belongs to the CDP session, so a new
  socket would see the page at Chrome's own width; a viewport test fails
  instead.

A command that runs past its deadline (a stalled renderer) is never retried and
fails the test. A one-time page `action` whose reply never arrives fails the
test and is never re-sent. A second failure of the same kind fails the test.
The predicate the test passes is never relaxed.

A discovery failure reads
`Chrome did not expose its Lab page through DevTools: phase=<phase> (<detail>); waited <s> of <budget>`,
where `phase` is `chrome-not-started` (no complete `DevToolsActivePort`; the
detail carries Chrome's return code), `devtools-unreachable` (port file present,
HTTP endpoint refused or not that browser), `no-page-target` (browser up, no
page listed) or `page-target-not-ours` (pages listed, none at the launch
origin). Every harness failure ends with
`budget=<s> (base <s>, discovery <s>, command <s>, load1=<load>, cpus=<n>), elapsed=<s>, launches=<n>`
plus the last observed DOM state, console tail and Chrome stderr tail, so a
loaded-runner stall reads differently from a page that never met its predicate.
A browser test that passes alone and fails in the tier with a high `load1`
is host contention; one that fails alone is a real defect.

`.github/workflows/slow.yml` runs `uv sync --frozen` followed by
`uv run pytest -p no:cacheprovider --timeout 600 -m slow --durations=10`
nightly, on demand, and on
pull requests that touch `traceback_runner/product_gates.py`,
`traceback_runner/web/`, product-gates tests or fixtures, or the regeneration
script. The workflow comment identifies its product-gates fixture comparison;
the marker selection itself is authoritative as the suite evolves. After a
product-gates contract change,
regenerate the fixture with
`uv run python scripts/regenerate_product_gate_fixture.py`; never hand-edit it.

`.github/workflows/canary.yml` runs the synthetic golden path, demo/verify,
and generated-BAM canary on macOS and Ubuntu nightly, on demand, and on
canary-related pull requests. Its in-job baseline and committed baseline are
synthetic checks. The real-input canary and its private baseline stay on an
operator workstation and do not run in repository CI.

Branch protection is a manual repository-admin action (golden-path decision
D8). The operator runs, once both `ci (...)` checks have reported on a pull
request:

```
gh api -X PUT repos/danwiggins/cfddemo/branches/main/protection \
  -H "Accept: application/vnd.github+json" \
  -F 'required_status_checks[strict]=true' \
  -f 'required_status_checks[contexts][]=ci (macos-latest)' \
  -f 'required_status_checks[contexts][]=ci (ubuntu-latest)' \
  -F 'enforce_admins=false' -F 'required_pull_request_reviews=null' -F 'restrictions=null'
```

`-F` sends `true`, `false`, and `null` as JSON literals; `-f` would send the
string `"true"` where the endpoint expects a boolean. The `slow` workflow is
deliberately not a required check: it is path-filtered and would block
unrelated pull requests.
