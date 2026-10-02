<!-- /autoplan restore point: ~/.gstack/projects/danwiggins-cfddemo/docs-golden-path-mvp-slice-autoplan-restore-20261002-161756.md -->
# Golden-path MVP slice and engineering hygiene

Status: draft spec, 2026-10-02. Verified against `main` at `d3739ca`.
Scope: one epic, two tracks. Track A makes the test suite trustworthy in CI.
Track B makes one real BAM travel from the CLI to the browser, with every
"unqualified, local, not for clinical use" label intact.

This is a development prototype. Nothing here qualifies a method, approves a
protocol, or makes a record fit for clinical use.

## Context

Every stage of the golden path already exists as a library API. A scratch
script (`glue.py`, deep review 2026-10-02) drove a real 2.1 GB hg38 BAM through
measurement, a signed bundle, an E04 catalog import and the loopback explorer
in under 30 s of compute. No CLI command connects those stages. An operator
today can run `traceback demo` (synthetic) and `traceback preflight`, which
blocks the real BAM. Everything after that needs Python.

The suite also has no CI. There is no `.github/workflows/` directory on
`main`, ruff reports 13 findings, and two sessions running the suite on one
machine fail 16 tests on a global lock.

Who is affected: Dan (solo operator and reviewer), the AI builder sessions that
run the suite in parallel worktrees, and any pilot reader shown a record later.

Why now: the plan-vs-reality review orders the work as CI, then E12 browser
finish and freeze, then this golden path, then human E0 work, then E3. Track A
is the first step of that order. Track B is the third.

## Current state (verified 2026-10-02)

| Stage | Library API | CLI today | Gap |
|---|---|---|---|
| Reference registration | `RegisteredReference` (`traceback_runner/contracts.py:163-175`) | none | Preflight hard-codes `synthetic_registered_reference()` (`traceback_runner/cli.py:601`) |
| Preflight | `validate_bam_snapshot` (`traceback_runner/preflight.py:155`) | `traceback preflight BAM` | `_reference_matches` (`preflight.py:46-58`) requires `AS` and `M5` on every `@SQ` line; the real BAM has neither, so TBX-BAM-002 blocks |
| Measurement + sign | `scan_aligned_reference_spans`, `finalize_measurement`, `build_result_bundle` (`traceback_runner/bundles.py:180`) | `traceback demo` only (synthetic) | `traceback run` always returns TBX-RUN-003 (`cli.py:226-238`, dispatched at `cli.py:1224`) |
| Approval label | `ApprovalState` has one member, `UNAPPROVED_SYNTHETIC` (`contracts.py:40-41`) | n/a | Pinned as a `Literal` at `contracts.py:205, 238, 389, 470`; `WorkflowRelease.synthetic_only: Literal[True]` (`contracts.py:239`) |
| Verify | `verify_bundle` | `traceback verify BUNDLE --trust-store/--trust-registry` (`cli.py:99-111`) | works |
| Catalog import | `ResultCatalog.import_bundle` (E04) | none | No production `MethodRegistry`; tests build one in `tests/test_result_catalog.py:72-101` (`_authority()`), and that one is QUALIFIED + PROVIDER_PRIMARY |
| Result view | E06 result-view artifact | none | glue run: catalog row has `has_registered_view: false`, result detail GET returns 404 |
| Serve | `RunningLocalWebService.start(..., explorer=...)` (`traceback_runner/web/server.py:1128-1135`) | `traceback reader launch` | `reader launch` opens `ROOT/runner.sqlite3` (`traceback_runner/reader_cli.py:811`); `demo` writes `ROOT/runner/runner.sqlite3` (`cli.py:556, 626`). `explorer` is never passed (`reader_cli.py:812-817`), so `/api/v1/explorer/catalog` answers unavailable (`server.py:842-853`) |

Measured on the real BAM (glue run, library level): preflight 14.8 s,
measurement 9.9 s, 5,822,296 records scanned, 3,435,813 eligible primary
alignments, 195 `@SQ` lines matching the 195-line `hg38.primary.fa.fai`. The
BAM has no `@RG`, `M5` or `AS` tags and no MM/ML modification tags
(TBX-MOD-001 is PARTIAL).

Honesty defect found in the glue run: the catalog row reported
`qualification_state: "qualified"` for an unqualified local record, because the
test authority declares QUALIFIED. `_qualification` (`evidence_inspector/result_catalog.py:827-830`)
copies the capability's state. B5 must not reuse the test authority.

Test-suite state:

| Item | Location | Today |
|---|---|---|
| ruff 0.7.4 | whole repo | 13 findings: 10 F401, 2 E731 (`tests/test_record_supersession_store.py:1355, 1368`), 1 E702 (`tests/test_operator_recovery.py:157`) |
| dev deps | `pyproject.toml` `[dependency-groups] dev` | `pytest==8.4.1` only |
| SQLite worker timeout | `evidence_inspector/record_supersession_store.py:115` | `10.0` s |
| subprocess timeout | `tests/test_assets.py:532` | `2` s |
| thread joins | `tests/test_cohort_registry.py:270, 904` | `2` s |
| catalog busy timeout | `evidence_inspector/result_catalog.py:1734` (`timeout=30`), `:1795` (`busy_timeout=30000`) | hard-coded 30 s; `tests/test_result_catalog.py:627` waits the full 30 s |
| web global lock | `traceback_runner/web/server.py:59, 205` | `flock(LOCK_EX)` on `/tmp` itself, held for the server's lifetime |
| product gates fixture | `tests/test_product_gates.py:58-64` | module-scoped `report` runs the full 10k-record harness for 19 tests |

Root cause of the web lock collision: `_open_startup_anchor` already creates a
per-state-directory anchor (`server.py:206-223`, name derived from
`sha256(state_directory)`) and `_acquire_instance_lease` holds a per-directory
lease (`server.py:1159`). The global contention comes only from the `flock` on
the `/tmp` directory descriptor at `server.py:205`, which is released in
`_close_startup_anchor` (`server.py:301`), at server shutdown. Every server on
the machine therefore excludes every other.

## Decisions (made autonomously; least-blocking defaults)

| # | Decision | Default chosen | Why |
|---|---|---|---|
| D1 | Reference match when `M5`/`AS` are absent (B2) | Match on name + length + order; emit TBX-BAM-002 as WARN when `M5` or `AS` is missing; BLOCK on any present-but-different value | `samtools reheader` changes the BAM bytes the operator sealed; WARN keeps provenance honest without a rewrite |
| D2 | Web lock (A3) | Option (a): hold the `/tmp` directory flock only around anchor create/unlink, not for the server's lifetime | Keeps single-instance per state directory (the anchor + lease already enforce it); no test-only code path in a security boundary |
| D3 | New approval member name | `ApprovalState.UNAPPROVED_LOCAL = "unapproved_local"` | Says what it is: not synthetic, not approved |
| D4 | Contract versioning | New `v2` schema literals for each contract that gains `UNAPPROVED_LOCAL`; `v1` stays `UNAPPROVED_SYNTHETIC`-only | Repo convention: a digested contract change bumps the version and every membership set, never mutates v1 |
| D5 | Local method authority (B5) | `QualificationState.DEVELOPMENT_UNQUALIFIED` + `DisplayRole.RESEARCH_BASELINE` | Catalog then reports `development_unqualified`, `current_provider_eligible: false` (enforced by `method_registry.py:522-530`) |
| D6 | Serve entry point (B6) | New `traceback serve --root ROOT`, operator (bootstrap) session, not a reader launch | The security spec's H1 makes reader-launched sessions 403 on the explorer routes; an operator session keeps the catalog route usable |
| D7 | Signing in `run` | Development trust (same key flow as `demo`, `cli.py:563-567`) | Production custody is out of scope; every output keeps `development_trust_only: true` |
| D8 | Branch protection | Documented `gh api` command, not executed | Repo admin action is Dan's |
| D9 | Real BAM in CI | No. CI uses a generated small BAM + tiny FASTA; the real BAM run is manual evidence | 2.1 GB data is local-only and must never be committed |
| D11 | Where measurement v2 lives | New `result-bundle.v3` + new catalog reader; v2 bundles unchanged | The catalog picks one reader per bundle version and matches the measurement tuple exactly (`result_catalog.py:400-420`) |
| D12 | `WorkflowRelease` v2 | Not bumped | Never constructed or persisted today (`cli.py:550`); bumping it would be ceremony. Revisit at E0 |
| D13 | Trust namespace for local records | New `development-local` namespace (B3b) | A local record signed as `development-synthetic` is a false label inside signed bytes |
| D10 | Real-data copy into the runner | `run` seals the input the same way `demo` does and refuses up front (TBX-RUN-004) when free space is under 2x the input | Keeps runner recovery semantics; disk is the cost |

## Child items

| # | Title | Priority | Effort (human / CC) | Depends on |
|---|---|---|---|---|
| A1 | CI workflow + ruff clean | Critical | 1 d / 1 h | none |
| A2 | Flake timeouts + injectable catalog busy timeout | Critical | 0.5 d / 30 min | none |
| A3 | Per-state-root web lock | Critical | 1 d / 1 h | none |
| A4 | Split product-gates fixture | High | 1 d / 45 min | A1 (slow marker in CI) |
| A5 | Stale docs | Medium | 0.5 d / 30 min | after E12 browser PR merges |
| B1 | `reader launch` DB path + explorer | High | 0.5 d / 30 min | A3 |
| B2 | `reference register` + `preflight --reference` | High | 2 d / 2 h | none |
| B3a | Measurement v2, bundle v3, honest report | Critical | 3 d / 3 h | none |
| B3b | `development-local` trust namespace | Critical | 2 d / 2 h | B3a |
| B4 | `traceback run` real BAM | High | 2.5 d / 2.5 h | B2, B3a, B3b |
| B5a | `catalog import` + local authority store | High | 2 d / 2 h | B4 |
| B5b | Persisted explorer artifacts | High | 3 d / 3 h | B5a |
| B6 | `traceback serve` with catalog explorer | High | 1 d / 1 h | B1, B5b, A3 |
| B7 | `traceback doctor` real checks | Medium | 0.5 d / 30 min | B2 |
| B8 | Operator guide for the real-BAM path | High | 0.5 d / 30 min | B6 |
| DoD | Scripted acceptance run | Critical | 1 d / 1 h | B1-B8 |

### A1. CI workflow and ruff clean

Change:
- Add `.github/workflows/ci.yml`. Triggers: `pull_request` and `push` to `main`. Matrix: `ubuntu-latest`, `macos-latest`. Python 3.11 (`requires-python = ">=3.11,<3.12"`).
- Steps: `astral-sh/setup-uv` pinned by commit SHA; `uv sync --frozen`; `uvx ruff@0.7.4 check .`; `uv run pytest -p no:cacheprovider -n auto --timeout 600 -m "not slow"`.
- `pyproject.toml` dev group adds `pytest-timeout` and `pytest-xdist` at exact pins; regenerate `uv.lock`. Register the `slow` marker under `[tool.pytest.ini_options] markers`.
- Install `samtools` in CI (`apt-get install samtools` / `brew install samtools`) because B2/B7 tests and the DoD need it.
- Fix the 13 ruff findings: `ruff check --fix` for the 10 F401; rewrite the 2 lambdas as `def`; split the semicolon line.
- Branch protection is manual. Document in `docs/DEVELOPMENT-CONTROL.md` (new "CI" section):
  ```
  gh api -X PUT repos/danwiggins/cfddemo/branches/main/protection \
    -H "Accept: application/vnd.github+json" \
    -f 'required_status_checks[strict]=true' \
    -f 'required_status_checks[contexts][]=ci (ubuntu-latest)' \
    -f 'required_status_checks[contexts][]=ci (macos-latest)' \
    -F 'enforce_admins=false' -F 'required_pull_request_reviews=null' -F 'restrictions=null'
  ```

Acceptance:
1. `uvx ruff@0.7.4 check .` exits 0 locally.
2. The workflow runs on a PR and both matrix jobs finish green.
3. `uv sync --frozen` succeeds on a clean runner (lockfile matches `pyproject.toml`).
4. Wall time per job is recorded in the PR description; if a job exceeds 30 min, the PR lists the 10 slowest tests (`--durations=10`).

Tests: the workflow itself. Rollback: delete the workflow file; revert the dev-group pins.

### A2. Flake timeouts

Change:
- `record_supersession_store.py:115`: `_SQLITE_WORKER_TIMEOUT_SECONDS = 30.0`.
- `tests/test_assets.py:532`: subprocess `timeout=30`.
- `tests/test_cohort_registry.py:270, 904`: `join(timeout=30)`, followed by `assert not workers[0].is_alive()` if not already asserted.
- `ResultCatalog.__init__` (`result_catalog.py:1114`) gains keyword-only `sqlite_busy_timeout_seconds: float = 30.0`, validated `0 < x <= 30`, used at `:1734` and `:1795` (`int(x * 1000)` for the pragma). `test_failed_publisher_never_unlinks_an_object_adopted_concurrently` passes `0.5`.

Acceptance:
1. Before shortening it, the PR states why that test waits: intended contention in the scenario, or a transaction held longer than it should be. If it is a held transaction, fix that instead and leave the timeout alone.
2. That test completes in under 5 s (was ~30 s), checked with `--durations`.
3. Default behaviour unchanged: a test asserts `PRAGMA busy_timeout` returns `30000` on a default catalog.
4. Every raised wait is followed by an assertion that the waited-on thing finished (`assert not worker.is_alive()`, `assert proc.returncode is not None`), so a hang still fails, just later.
5. 20 consecutive local runs of the three touched test files pass (`pytest --count` not required; a shell loop is fine).

Rollback: revert. No stored data changes.

### A3. Per-state-root web lock

Change (D2): in `_open_startup_anchor` (`server.py:178-274`), take the `/tmp` directory flock only while creating and validating the anchor, then release it (`LOCK_UN`) before returning. Opening also retries the `/tmp` lock with `LOCK_EX|LOCK_NB` every 50 ms for up to 5 s; only a failure on the per-anchor lock (`server.py:223`) means "already running". `_close_startup_anchor` (`server.py:276-302`) re-takes it with `LOCK_EX|LOCK_NB` retried every 50 ms for up to 5 s around the identity check and `unlink`; on timeout it skips the unlink (a stale anchor file is harmless: the next start re-opens it and its own `flock` decides) and still releases the per-anchor lock and closes descriptors. The per-anchor flock (`server.py:223`) and the instance lease (`server.py:1159`) stay held for the server's lifetime. `_require_startup_anchor` keeps every identity check.

Acceptance:
1. Two `RunningLocalWebService.start` calls with different `state_directory` values run at the same time in one process and in two processes.
2. Two starts with the same `state_directory` still fail with `LocalWebServerError("local web service is already running")`.
3. Running `pytest tests/web` in two worktrees at once produces 0 `already running` failures (was 16).
4. Existing anchor-identity tests still pass unchanged.
5. 16 concurrent starts on 16 distinct roots: 0 failures.
6. Adversarial: replacing or unlinking the anchor file during open, runtime (watchdog, `server.py:1237`) and close never lets two servers hold one root (tests with a stale anchor plus a racing start).

Tests: +6 in `tests/web/test_loopback_server.py`.
Rollback: revert the commit; the lock file format is unchanged.
Conflict: `server.py` is also edited by the uncommitted E12 browser branch and by security items H1/H6. Land after the browser PR.

### A4. Split the product-gates fixture

Change: commit `tests/fixtures/product_gates/foundation_report.json`, produced once by `run_foundation_gates(... run_id="gate_run_20260929", captured_at=2026-09-29T00:00Z)`. The module `report` fixture loads it with `ProductGateReport.model_validate_json`. One new test, marked `@pytest.mark.slow`, runs the live harness and asserts the live report's structure (record counts, gate states) equals the fixture's, ignoring timing and memory fields. Add a regeneration script `scripts/regenerate_product_gate_fixture.py`. A second workflow, `.github/workflows/slow.yml`, runs `pytest -m slow` nightly and on any PR touching `traceback_runner/product_gates.py` or the fixture, so fixture drift cannot hide.

Acceptance:
1. `pytest tests/test_product_gates.py -m "not slow"` runs in under 10 s.
2. The slow test passes locally and is excluded from CI's default lane.
3. All 19 existing tests pass against the fixture without edits to their assertions, except tests that assert on live timing, which move behind `slow`.

Rollback: revert. Watch item: the fixture is a frozen artifact; a later contract bump must regenerate it with the script, never hand-edit.

### A5. Stale docs

Do this after the E12 browser PR merges, because that branch edits `docs/RESULT-EXPLORER.md` and `docs/LONGITUDINAL-COMPARISON-REGISTRY.md` (uncommitted on `epic-e/e12-browser-integration`).

| File | Claim | Fact on `main` | Change |
|---|---|---|---|
| `docs/E12-INTEGRATION-PLAN.md:788-790` | fence-adapter prerequisites "must merge before builder work" | `evidence_inspector/composite_authority_fence.py` exists and D08 merged (#83) | Mark resolved, cite the module and PR |
| `docs/LONGITUDINAL-COMPARISON-REGISTRY.md:203-206` | sibling registries' interrupted-write behaviour "listed as a shared follow-up" | registry storage hardening merged (#82) | Re-check against #82 and state the current behaviour, or keep and link the open follow-up |
| `docs/RESULT-EXPLORER.md:42, 82` and `docs/PRODUCT-GATES.md:27, 62` | "E12 is not implemented" | reader authority + launch route (`ea26cb0`) and D08 read model (#83) are on `main`; no browser route serves D08; `product_gates.py:326` still pins `e12_state="unavailable_not_implemented"` | Say exactly that: E12 authority and read model exist, the browser surface is not served, and the gate contract still reports unavailable. Do not change the `product_gates.py` literal (digested contract) |
| `docs/DEVELOPMENT-CONTROL.md:1-3` | "first implementation wave" | historical | Add a top banner: "Historical. Describes the first wave; current process is in EPICS.md and this repo's PR history." Add the A1 CI section |

Acceptance: each row's change is in the diff; `grep -n "not implemented" docs/RESULT-EXPLORER.md docs/PRODUCT-GATES.md` returns only sentences that also name what does exist.

### B1. `reader launch` database path and explorer

Change: `reader_cli.py:811` opens `root / "runner" / "runner.sqlite3"` (the path `cli.py:556, 626` use); if it does not exist, exit 4 (the guide's "local material not found" code, `docs/OPERATOR-GUIDE.md:101-111`) with "runner database not found under ROOT; run `traceback demo` or `traceback run` first". `explorer=None` stays: whether a reader-launched session may read the explorer at all is security H1's decision (D6), so B1 only fixes the path.

Acceptance:
1. Regression test: `traceback demo --root R`, then call `reader_cli._launch` with a test grant and a closed stdin; assert the `JobStore` it opened is `R/runner/runner.sqlite3` and that the store returns the demo job by ID. The test does not go through HTTP, so it does not depend on the H1 session policy.
2. Missing DB exits 4 with the message above, and creates no file.

Tests: +2 in `tests/test_reader_cli.py` (or the existing reader CLI test module).
Rollback: revert.

### B2. Reference registration and `preflight --reference`

CLI:
```
traceback reference register --fasta PATH --id ID --root ROOT [--assembly NAME] [--json]
traceback preflight BAM [--index BAI] --reference ID --root ROOT [--json]
```
`--reference` is optional; when absent, preflight keeps the synthetic reference (today's behaviour, so the synthetic tests still pass).

`ID` must match `^[a-z0-9][a-z0-9._-]{0,63}$` and must not contain `..`; it becomes a directory name.

Storage: `ROOT/references/ID/registered-reference.json` (canonical JSON of `RegisteredReference`) plus `ROOT/references/ID/source.json` holding the absolute FASTA path and size at registration (the FASTA is referenced, not copied). Without `--assembly`, `AS` is not compared (its absence or presence counts toward the WARN); with `--assembly NAME`, a present `AS` must equal `NAME`. This avoids blocking a BAM whose `AS` is `GRCh38` against a registration named `hg38-local`. Both files `0600`, write-once; re-register with identical bytes is a no-op, different bytes exits 3 TBX-REF-002). `asset_sha256` is the SHA-256 of the FASTA file bytes. Per contig: name, length and MD5 of the uppercase sequence with newlines removed (the SAM `M5` definition). Reads the FASTA streaming; requires `PATH.fai` and checks contig names and lengths against it.

Matching (D1), replacing `_reference_matches`:
- `@SQ` count, order, `SN` and `LN` must equal the registration, else BLOCKED (unchanged).
- If every `@SQ` has `M5` and `AS` and they match: PASS.
- If any `M5` or `AS` is present and differs: BLOCKED.
- If `M5` or `AS` is absent on any line: WARN, problem text "BAM header lacks M5/AS; contig names and lengths match the registered reference", remediation "Optional: `samtools reheader` with M5/AS for full provenance".

Acceptance:
1. `reference register` on `data/local/reference/hg38.primary.fa` produces 195 contigs and finishes in under 5 min on Dan's Mac (recorded as evidence, not a CI test).
2. `preflight data/local/alignment-work/aligned.sorted.bam --reference hg38-local --root R` reports TBX-BAM-002 WARN, overall outcome PARTIAL (TBX-MOD-001 is PARTIAL), `fragment_measurement_eligible: true`, exit 0.
3. A BAM whose `M5` differs from the registration is BLOCKED.
4. Synthetic preflight without `--reference` is byte-identical to today's output.

Also: `reference register` joins the `_operator_lock` command set; gzip FASTA is refused with TBX-REF-001 ("decompress first"); a test asserts no bundle, catalog row or explorer JSON ever contains the `source.json` path.

Tests: +9 unit (ID traversal, gzip refusal, path never exported, register: md5 definition, fai mismatch, idempotent re-register, conflicting re-register; match: absent M5 → WARN, wrong M5 → BLOCKED), +1 CLI integration on a generated 2-contig FASTA/BAM.
Rollback: revert; delete `ROOT/references/`.

### B3a. `UNAPPROVED_LOCAL` measurement contracts, bundle v3, honest report

Change (D3, D4, D11):
- `ApprovalState.UNAPPROVED_LOCAL = "unapproved_local"`.
- Explicit, separate classes, never a widened v1: `FragmentMeasurementPolicyV2` (`traceback.fragment-policy.v2`) and `FragmentMeasurementV2` (`traceback.fragment-measurement.v2`), each with `approval_state: Literal[UNAPPROVED_SYNTHETIC, UNAPPROVED_LOCAL]`. Readers dispatch on `schema_version` through one function, `parse_fragment_measurement(bytes) -> FragmentMeasurement | FragmentMeasurementV2`, used by `bundles.py:382`, `export.py:80`, `VerifiedBundle`, `chart_for_measurement` and the catalog. v1 classes and bytes are untouched.
- `MeasurementScan` (`measurement.py:44`) carries the policy's schema version and `approval_state`; `finalize_measurement` (`measurement.py:218`) builds V1 from a v1 policy and V2 from a v2 policy. Today it drops `approval_state`, so a local policy would finalize into the synthetic default.
- `ExportLimitations` (`export.py:45-47`) gains `traceback.limitations.v2` with `template_id: Literal["synthetic-fragment-length-research-use.v1", "local-fragment-length-research-use.v1"]`. The local template lists: unqualified method, development trust, no E0 protocol approval, reference matched by name and length only when D1 WARN applied.
- Bundle: v2 measurements are written only in a new `traceback.result-bundle.v3` manifest (signing payload `traceback.bundle-signing-payload.v3`). Reason: the catalog selects exactly one reader per bundle version and compares the measurement tuple by equality (`result_catalog.py:400-420`), so one v2 reader cannot accept both measurement versions. Add `ResultBundleReader(reader_id="reader_result_bundle_v3", minimum_version=3, maximum_version=3, measurement_schema_versions=("traceback.fragment-measurement.v2",))`; the v2 reader is unchanged.
- `WorkflowRelease` is NOT bumped (D12): `demo` never constructs or persists one (`cli.py:550` hashes the ID string). B4 records `workflow_release_id="local-unqualified-v0"` exactly as `demo` records its ID.
- Report: new `report-local.html` template chosen when `approval_state == UNAPPROVED_LOCAL`. Fixed banner: "Unqualified. Local development record. Not for clinical use. Development signing key only." Synthetic bundles keep today's `report.html` bytes.
- Frozen v1 fixture first: before any code change, commit `tests/fixtures/bundles/v2-synthetic/` (a real `traceback demo` bundle: policy, measurement, limitations, manifest, checksums, signature, report) and `tests/fixtures/bundles/v2-synthetic.sha256` (SHA-256 of each file).

Acceptance:
1. The frozen v2-synthetic bundle verifies with the new code, its file hashes match the committed `.sha256`, and it imports into the catalog through `reader_result_bundle_v2`.
2. A v3 bundle with a `FragmentMeasurementV2(approval_state="unapproved_local")` builds, verifies and imports through `reader_result_bundle_v3`.
3. A v3 manifest that lists `fragment-measurement.v1`, or a v2 manifest that lists `fragment-measurement.v2`, is rejected with `CatalogUnsupportedSchema`.
4. `finalize_measurement` on a v2 local policy returns a V2 object with `approval_state == "unapproved_local"`.
5. `report-local.html` contains the exact banner; after removing the banner string, there is no case-insensitive `synthetic` and no match for `(?<![Uu]n)qualified`.
6. Runtime test: every reader dispatch table and every place that pattern-matches a measurement or bundle schema literal (list them in the PR from `grep -rn "fragment-measurement.v\|result-bundle.v\|bundle-signing-payload.v"`) accepts the new version; verified by importing and calling, not by grep.

Tests: about +12. Rollback: revert before any v3 bundle exists; after that, keep the v3 reader.

### B3b. `development-local` trust namespace

The signing payload and trust store namespace is a single value, `TrustNamespace.DEVELOPMENT_SYNTHETIC = "development-synthetic"` (`signing.py:50-51`), stamped into every bundle signature (`bundles.py:120-130`) and the only namespace the result-trust registry accepts. Signing a real local record under "development-synthetic" is a false label inside signed bytes.

Change (D13): add `TrustNamespace.DEVELOPMENT_LOCAL = "development-local"`. v3 signing payloads use it; v1/v2 payloads keep `development-synthetic`. `ResultTrustRegistry` (`evidence_inspector/result_trust_registry.py`) accepts keys in either namespace, recorded per key; a key is valid for exactly one namespace. This is a digested-contract change to the registry: bump its schema version, its backup format and its membership sets, and regenerate any frozen registry fixtures with the repo's script, never by hand. Its `synthetic_only: Literal[True]` (`result_trust_registry.py:212`) becomes `data_origin` on the v2 snapshot.

Acceptance:
1. A v3 bundle signed by a `development-synthetic` key fails verification (namespace mismatch), and vice versa for v2.
2. An existing registry directory created before the change reopens read-only and verifies v2 bundles (frozen registry fixture).
3. `traceback verify --trust-registry` works for both namespaces.

Tests: about +6. Effort: 2 d / 2 h. Rollback: as B3a.

### B4. `traceback run` on real data

CLI: `traceback run BAM [--index BAI] --reference ID --root ROOT [--json]`. Without `--reference`, keep TBX-RUN-003 (the old refusal path keeps its test).

Behaviour:
- Extract `_execute_signed_run(root, request, source, relative_files, stages, signing_key)` from `_demo` (`cli.py:536-589`); `demo` calls it and its JSON output stays byte-identical. `run` calls it with local stages. No copied orchestration.
- `run` joins the `_operator_lock` command set (`cli.py:1274-1279`).
- Before sealing, refuse with TBX-RUN-004 ("not enough space under ROOT; need 2x the input size", retryable) when free space on ROOT's volume is under 2x the BAM + index size.
- Stages: preflight (B2 reference; requires `fragment_measurement_eligible`), measure, sign. `MeasurementUnavailableError` (`measurement.py:72`) maps to TBX-RUN-005 ("measurement unavailable: no complete eligible denominator", not retryable); the job ends failed, no record.
- Locked policy `aligned-reference-span-local-v2`: contigs = registered contigs matching `^chr([0-9]{1,2}|X|Y)$`, or all registered contigs when none match (CI's 2-contig reference); `min_mapping_quality=20`; bins `[0,100) [100,150) [150,200) [200,300) [300,500) [500,1000) [1000,inf)`; `approval_state=UNAPPROVED_LOCAL`; `definition_id` embeds the reference ID. A module constant, not CLI input.
- Provenance: `provider_hmac_sha256` uses a per-root random 32-byte key at `ROOT/trust/provenance-hmac.key` (0600, created once). The current code uses a fixed key (`cli.py:415`).
- Method identity comes from `local_method_identity(root)` in new `traceback_runner/local_authority.py` (B4 creates the module with `ensure_local_method_authority(root)`; B5a reuses it).
- `Runner` gains `local_unqualified_enabled: bool = False` beside `synthetic_enabled` (`runner.py:127-135, 271`); the local path never sets `synthetic_enabled`. Its transition messages (`runner.py:284, 402-403`, "synthetic execution started" etc.) become origin-specific. Before coding, grep `synthetic_only`/`synthetic` readers in `runner.py`, `operator.py`, `store.py` and list them in the PR.
- Stage metadata: `"data_origin": "local_unqualified"` instead of `"synthetic_only": True`.
- Human mode prints one line per stage boundary; `--json` output is unchanged in shape.
- Re-running the same BAM on the same ROOT returns the existing record (runner dedupe by `input_tree_sha256`), exit 0.
- CLI envelope: `_result` hard-codes `"synthetic_only": True` (`cli.py:142-155`) on every command. Add `traceback.cli-result.v2` with `data_origin: "synthetic" | "local_unqualified"`, used by `run`, `catalog import`, `serve` and `reference register`; `demo` and the other existing commands keep v1 bytes. Update the `run` help text ("process a real input (disabled)", `cli.py:85`) and the module docstring.
- Errors: one helper `_problem(code, summary, cause, fix, retryable, docs_anchor)` builds every new error payload; every new code (TBX-REF-001..003, TBX-RUN-004/005, TBX-AUTH-LOCAL-001) carries all six fields, and TBX-RUN-004 reports required and available bytes. TBX-RUN-005's fix names what to check (contig names vs the policy, MAPQ 20, duplicate/secondary flags).
- Next steps: on success, human output prints the absolute record path, the absolute trust-store path and the exact next commands (`traceback verify ...`, `traceback catalog import ...`); `--json` adds `bundle_path` and `trust_store_path` as absolute paths. The locked policy (contigs, MAPQ, bins) is printed before the run starts.
- `verify` gains `--root ROOT RECORD_ID` as an alternative to a bundle path plus `--trust-store` (resolves both under ROOT).

Acceptance:
1. Real BAM: exit 0, `eligible_alignments == 3435813`, under 120 s end to end on Dan's Mac (library-level 12.4 s; sealing copies 2.1 GB).
2. `traceback verify` on the record passes; `traceback status JOB` reports complete and verified.
3. BLOCKED preflight: failed job with TBX-BAM-002, no record.
4. A BAM with zero eligible alignments: TBX-RUN-005, no record.
5. Insufficient space (monkeypatched `shutil.disk_usage`): TBX-RUN-004 before any copy.
6. Two concurrent `run`s on one ROOT: the second gets `OperatorBusy`.
7. Two roots, same BAM: different `provider_hmac_sha256`.
8. `demo` JSON is byte-identical to the frozen pre-change output.
9. Pause/resume between stages works.

Tests: about +9 CLI integration on the generated BAM. Rollback: revert; delete ROOT.

### B5a. `traceback catalog import` and the local method authority store

CLI: `traceback catalog import BUNDLE --root ROOT [--json]`; joins the `_operator_lock` command set.

`MethodRegistry` and `AuthorityHead` are immutable models with canonicalization helpers, not a durable store (`method_registry.py:253, 464`). B5a adds the store in `traceback_runner/local_authority.py`:
- Layout: `ROOT/authority/method-registry.json` (canonical bytes of one `MethodRegistry`), `ROOT/authority/authority-head.json`, `ROOT/authority/pins.json` (`{"registry_sha256", "authority_head_sha256"}`). All 0600, directory 0700.
- Creation (first `run` or `import`, under `_operator_lock`): build in `ROOT/authority.tmp-<pid>`, fsync, rename to `ROOT/authority`. If `ROOT/authority` exists, never rewrite; reopen and validate: recompute both SHA-256s, compare to pins, call `resolve_current_capability`. Any mismatch exits 3 TBX-AUTH-LOCAL-001 and changes nothing. A leftover `authority.tmp-*` is removed on next start.
- Contents (D5): one tool, one asset (the registered reference's `asset_sha256`), one `MethodDefinition` `mth_fragment_aligned_reference_span@1.0.0-local`, one `QualificationRecord` with `DEVELOPMENT_UNQUALIFIED`, one `DisplayRoleAssignment` with `RESEARCH_BASELINE`, scope `scope_local`. Timestamps are the creation second (UTC), written once. With more than one registered reference, each gets its own asset and method version `1.0.0-local-<reference_id>`; `run` selects by `--reference`.
- Result-trust registry at `ROOT/trust/result-trust-registry/`, seeded with the development-local key `run` used (B3b). The catalog opens with `result_trust_registry=` (`result_catalog.py:1120`), never `trust_store=`.
- `import_bundle` needs `CatalogAliases` (`result_catalog.py:630-635`): derive them deterministically from the first 8 hex chars of `sha256(record_id)` (`dsp_`, `rnx_`, `tpt_` prefixes).
- Idempotent: re-import returns the same `result_id`. Two concurrent imports of the same bundle: the second gets `OperatorBusy`.

Acceptance:
1. One row with `qualification_state="development_unqualified"`, `current_provider_eligible=false`, `trust_state="development_signature_verified"`.
2. Re-import: exit 0, same `result_id`, no new row.
3. A bundle signed by a key not in the trust registry: exit 3, no row.
4. A tampered `method-registry.json`: exit 3 TBX-AUTH-LOCAL-001, catalog untouched.
5. Parsing every JSON file under `ROOT/authority`, no `qualification_state`/`state` equals `"qualified"` and no `display_role` equals `"provider_primary"`.
6. `catalog import` on a directory that is not a bundle: exit 3, no row.

Tests: about +7. Effort: 2 d / 2 h. Rollback: delete `ROOT/catalog` and `ROOT/authority`.

### B5b. Persisted explorer artifacts

`CanonicalExplorerArtifactRepository` and `CatalogAuthorityIndex` are in-memory only (`web/explorer.py:69, 122`); nothing reads them from disk, so a detail page cannot survive a restart. The model to persist already exists: `ExplorerArtifactRecord` (`traceback.explorer-artifact-record.v1`, `web/explorer.py:92-100`).

Change, at the end of `catalog import`:
1. Adapt the imported bundle to a `VerifiedMeasurementRecord` (`evidence_inspector/compatibility.py:219`).
2. `decide_compatibility` (`compatibility.py:824`) for that single record.
3. Build a `DenominatorLedger` (`result_view.py:172`) from the measurement's `records_scanned`, `eligible_alignments` and exclusion counts.
4. `bind_result_view_source(record, compatibility_decision, denominator, accessible_label, qc_label)` (`result_view.py:460`) with closed-vocabulary labels: `accessible_label="Fragment length, unqualified local record"`, `qc_label="unqualified"` (no operator-entered text; security must-fix 6).
5. `build_result_view(request)` (`result_view.py:729`).
6. Write the canonical `ExplorerArtifactRecord` to `ROOT/explorer/artifacts/<result_id>.json` and its `CatalogAuthorityBinding` to `ROOT/explorer/bindings/<result_id>.json`, write-once (temp + fsync + rename), 0600.
This store is separate from `result_view_source_registry` (whose `synthetic_only: Literal[True]` at `:320` stays untouched).

Acceptance:
1. After import, both files exist and re-parse to models equal to what was written.
2. A truncated or edited artifact file makes B6 refuse that row with the existing unavailable problem, not a 500, and the other rows still serve.
3. Re-import does not rewrite either file (mtime unchanged).
4. If the catalog row exists but its artifact file does not (crash between the two writes), `import` writes the artifact and exits 0.

Tests: about +6. Effort: 3 d / 3 h. Rollback: delete `ROOT/explorer`.

### B6. `traceback serve`

CLI: `traceback serve --root ROOT [--ipv6]`. Prints the one-use operator bootstrap link (the B01 flow the loopback tests use) and runs until SIGINT/SIGTERM. Stdin is read only when it is a TTY (Enter prints a fresh link, as `reader launch` does); with stdin closed or redirected, `serve` keeps running, so the DoD can background it.

Behaviour:
- `build_local_explorer(root) -> IntegratedExplorerSource | None` in new `traceback_runner/web/local_source.py`. Returns `None` when `ROOT/catalog` does not exist. If the catalog exists but `ROOT/authority` or the trust registry is missing or fails validation, `serve` exits 3 and does not start (no partial state).
- Loads every `ROOT/explorer/artifacts/*.json` and `bindings/*.json` (B5b), validates each, skips invalid ones with a count in the startup line.
- The caller owns and closes the catalog and trust registry when the server stops.
- `RunningLocalWebService.start(store=JobStore(root/"runner"/"runner.sqlite3"), state_directory=root/"web", explorer=...)`.
- `reader launch` keeps `explorer=None` until security H1 settles reader-session explorer access (D6).
- The bootstrap link is printed to stdout only. The DoD script reads it from a pipe and never echoes it; CI logs must not contain it.

Acceptance:
1. After B5, an operator-cookie `GET /api/v1/explorer/catalog?limit=10` returns 200 with the record; `GET /api/v1/explorer/results/{id}` returns 200.
2. No catalog: the catalog route returns the existing unavailable problem.
3. Catalog present, authority missing: exit 3, no listener.
4. Two `serve` processes on different roots run at once (A3).
5. Startup with 100 imported records finishes in under 5 s (generated records).

Tests: about +5 in `tests/web/`. Effort: 1 d / 1 h. Rollback: revert.

### B7. `traceback doctor`

Replace the hard-coded `real_data: blocked` check (`cli.py:174-210`) with:
- `samtools`: on PATH and `samtools --version` parses; WARN if absent (only needed for the user to index).
- `reference`: for `--root`, list registered references; each PASSes when the FASTA path in `source.json` exists with the recorded size and, with `--deep`, its SHA-256 equals `asset_sha256`. A missing FASTA is WARN (records already made stay valid).
- `disk`: free bytes on ROOT's volume; WARN under 10 GB.
- `root`: exists or can be created, owner is the user, mode not group/other-writable.
- `trust`: if `ROOT/records/` holds any record, `ROOT/trust/development-result-trust.json` and the result-trust registry must exist and parse, else doctor reports BLOCKED and exits 3 (every later `verify` would fail). An empty root without trust is PASS (`run` creates it).
- `root`: doctor prints the resolved absolute ROOT, so a second accidental `.traceback` in another directory is visible.
Exit stays `BLOCKED` only when the synthetic runtime check fails, as today.

Acceptance: each check has a pass and a non-pass test (+10 unit, using monkeypatched `shutil.which` and `shutil.disk_usage`).

### B8. Operator guide for the real-BAM path

`docs/OPERATOR-GUIDE.md` is titled "synthetic operator guide", says `run` rejects real data, and its verify example passes `--trust-store ./traceback-synthetic/trust` while the file is `trust/development-result-trust.json` (`OPERATOR-GUIDE.md:9-19`). README "Run locally" covers Streamlit only.

Change: add a "Real local BAM (unqualified)" section to `docs/OPERATOR-GUIDE.md` with prerequisites (`uv sync`, `brew install samtools`, a coordinate-sorted indexed BAM, a FASTA with `.fai`), the copy-paste command sequence using `uv run traceback ...`, the expected output of each step, the new error codes with fixes, and the cleanup command. Fix the existing verify example. Link the section from README "Run locally". Each new `_problem` docs anchor points into this section.

Acceptance: the commands in the section, run verbatim against the CI generated fixture in a fresh root, succeed (the DoD script executes the doc's code block, extracted by heading, so the doc cannot drift). Effort: 0.5 d / 30 min. Depends on: B6.

## Definition of done

`scripts/golden_path_acceptance.sh` runs, from a fresh `mktemp -d` root:
The script reads the record ID from `traceback run --json` (`data.record_id`, the key `demo` emits at `cli.py:583`) and the bootstrap URL from `serve`'s first stdout line, exchanges it with the helper the loopback tests use (`tests/web/test_loopback_server.py:64`, `_exchange`), polls the port for up to 10 s, and kills `serve` on exit via `trap`.

1. `traceback reference register --fasta F --id ref --root R`
2. `traceback preflight BAM --reference ref --root R` exits 0 and the JSON outcome is not `blocked`
3. `traceback run BAM --reference ref --root R` exits 0
4. `traceback verify R/records/<id> --trust-store R/trust/development-result-trust.json` exits 0
5. `traceback catalog import R/records/<id> --root R` exits 0
6. `traceback serve --root R` started in the background
7. an operator-session HTTP GET of `/api/v1/explorer/catalog?limit=10` lists the record with `qualification_state="development_unqualified"`

In CI the script uses a generated BAM (2 contigs, about 1,000 reads, no `M5`/`AS`, so the WARN path is exercised) and a matching tiny FASTA, both generated at test time by an extension of `traceback_runner/fixtures.py`. A pytest wrapper `tests/test_golden_path_acceptance.py` runs the script. The real BAM run is executed once manually; its evidence goes in the B6 PR as a hand-written table (exit codes, `eligible_alignments`, `records_scanned`, preflight outcome, wall times, catalog `qualification_state`). Raw CLI JSON is not pasted because it contains absolute local paths.

## Dependency graph

```
A1 ──> A4
A2 (any time)
A3 ─────────────────────────────┐
B1 (any time) ──────────────────┤
B2 ──> B7                       │
B2 ─┐                           v
B3a ─> B3b ─┴> B4 ──> B5a ──> B5b ──> B6 ──> B8 ──> DoD
A5 after the E12 browser PR merges
```

Sequencing: A1-A3 first because every later PR needs green CI and parallel suites. B3a/B3b before B4 because `run` must never emit a record labelled synthetic. B5a before B5b because artifacts bind to catalog rows. B6 last because it only reads what B5a/B5b wrote.

## Pending conflicts with in-flight work

| In-flight work | Overlap | Resolution |
|---|---|---|
| E12 browser branch `epic-e/e12-browser-integration` (uncommitted edits to `traceback_runner/web/server.py`, `web/explorer.py`, `web/reader_session.py`, `docs/RESULT-EXPLORER.md`, `docs/LONGITUDINAL-COMPARISON-REGISTRY.md`) | A3 (server.py), A5 (both docs), B6 (explorer wiring) | Browser PR merges first; A3, A5 and B6 rebase onto it |
| Security spec `docs/PILOT-SECURITY-HARDENING.md` H1 (reader sessions get 403 on explorer routes) | B6 adds a second operator-bootstrap launch path | D6: the DoD uses `traceback serve` with an operator session; `reader launch` keeps `explorer=None` until H1 lands. Taste T2: get the security spec's sign-off on `serve` before coding B6 |
| Security H6 (`server.py`, `auth.py`), watchdog/launch fix (`reader_cli.py:827-830`) | A3, B1 | Order: browser PR, then A3, then H1/H6; B1 touches only `reader_cli.py:811` and rebases trivially |
| Security `--profile` top-level option and `status --trust-registry` (`traceback_runner/cli.py`) | B2, B4, B5, B6, B7 all add subcommands to `cli.py` | Mechanical parser conflicts; whichever lands second rebases. B5's trust-registry binding should reuse the security spec's `_require_trust_registry_identity` path if it lands first |
| Security must-fix 5 (retire caller `TrustStore` paths) | B5 | B5 already opens the catalog with `result_trust_registry=`, not `trust_store=` |

## Out of scope (follow-ups with triggers)

| Item | Trigger to revisit |
|---|---|
| Shared JournaledRegistry base (12 near-duplicate storage layers, about 10k lines) | A 13th registry is proposed, or a storage fix has to be applied to more than 3 registries |
| Removing the anti-tamper seal layer (about 2-3k lines) | The threat model formally drops in-process mutation, or a seal bug costs more than 1 day |
| E12 beyond the in-flight browser PR (`epic-e/e12-browser-integration`) | Browser PR merged and frozen |
| Security hardening (`docs/PILOT-SECURITY-HARDENING.md`, branch `docs/pilot-security-hardening`) | Its own review; it must land before any non-synthetic donor data on a shared host |
| POD5/Dorado (E4) | Golden path DoD green |
| MinKNOW run-folder preflight (E3) | Golden path DoD green and E0 progressing |
| E0 protocol approval | Human work; not engineering |
| D01 O(n) linkage commit cost | Linkage store above 10k entries, or a commit exceeds 1 s |
| Per-reader scoping of explorer routes | Covered by security H1 |

## Rollback

Every item is one PR and reverts cleanly, except B3a after v3 bundles exist and B3b after a trust registry is written in the new schema. B3b changes the trust-registry schema: an existing registry reopens read-only, and a store newer than the running code fails closed with "this ROOT was written by a newer traceback; upgrade or use another ROOT". Sealed records are read-only, so deleting a root is `chmod -R u+w ROOT && rm -rf ROOT`, not `rm -rf` alone.

## Files reference

| File | Items |
|---|---|
| `.github/workflows/ci.yml` (new) | A1 |
| `pyproject.toml`, `uv.lock` | A1 |
| 9 files with ruff findings (see A1) | A1 |
| `evidence_inspector/record_supersession_store.py:115` | A2 |
| `evidence_inspector/result_catalog.py:1114, 1734, 1795` | A2 |
| `tests/test_assets.py:532`, `tests/test_cohort_registry.py:270, 904`, `tests/test_result_catalog.py:627` | A2 |
| `traceback_runner/web/server.py:178-302` | A3 (also B6 call site only) |
| `tests/test_product_gates.py:58`, `tests/fixtures/product_gates/foundation_report.json` (new), `scripts/regenerate_product_gate_fixture.py` (new) | A4 |
| `docs/E12-INTEGRATION-PLAN.md`, `docs/LONGITUDINAL-COMPARISON-REGISTRY.md`, `docs/RESULT-EXPLORER.md`, `docs/PRODUCT-GATES.md`, `docs/DEVELOPMENT-CONTROL.md` | A5 |
| `traceback_runner/reader_cli.py:811-817` | B1 |
| `traceback_runner/cli.py` (parser 60-140, `_preflight` 592, `_doctor` 174, `_real_run_blocked` 226, dispatch ~1224) | B2, B4, B5, B6, B7 |
| `traceback_runner/preflight.py:46-58` | B2 |
| `traceback_runner/references.py` (new) | B2 |
| `traceback_runner/contracts.py:40, 203-230, 387-410` | B3a |
| `traceback_runner/measurement.py:44, 72, 218` | B3a |
| `traceback_runner/export.py:45-47, 80` | B3a |
| `traceback_runner/signing.py:50-51`, `evidence_inspector/result_trust_registry.py` | B3b |
| `traceback_runner/runner.py:127-135, 271-284, 402-403` | B4 |
| `tests/fixtures/bundles/v2-synthetic/` (new, frozen) | B3a |
| `traceback_runner/bundles.py:120-130, 180, 382`, report template (new) | B3a |
| `evidence_inspector/result_catalog.py:400-420` (v3 reader) | B3a |
| `traceback_runner/local_authority.py` (new) | B4 (created), B5a (store) |
| `evidence_inspector/compatibility.py:219, 824`, `evidence_inspector/result_view.py:172, 460, 729`, `traceback_runner/web/explorer.py:69-122` | B5b |
| `traceback_runner/web/local_source.py` (new) | B6 |
| `.github/workflows/slow.yml` (new) | A4 |
| `traceback_runner/fixtures.py`, `scripts/golden_path_acceptance.sh` (new), `tests/test_golden_path_acceptance.py` (new) | DoD |
| `docs/OPERATOR-GUIDE.md`, `README.md` | B8 |

## Testing summary

| Layer | What | Count |
|---|---|---|
| Unit | ruff fixes, timeouts, reference md5/match/ID, contracts v2/v3, trust namespace, doctor checks | about +40 |
| Integration | web lock concurrency + adversarial anchor, CLI register/preflight/run/import/serve on generated BAM, explorer artifact persistence | about +30 |
| E2E | acceptance script in CI (small BAM) | +1 |
| Scheduled | `pytest -m slow` nightly (product-gates live harness) | 1 lane |
| Manual evidence | real BAM DoD run | 1 |

## Effort

Human: about 21.5 days total (A: 4 d, B: 17.5 d incl. DoD; B3b and B5b were added by the eng review, B8 by the DX review). Review rounds are not included; on this repo contract work has taken about twice its build time once review is counted. CC + review: about 21 h of build time plus review rounds.

---

# /autoplan review (2026-10-02, commit b7979b3)

Run autonomously. Intermediate questions were auto-decided with the six autoplan principles. Premises and User Challenges are NOT decided; they are listed under "Pending user gates" below. UI scope: none detected (0 view terms). DX scope: yes (CLI-heavy). Phases run: CEO, Eng, DX. Design skipped.

## Phase 1: CEO review (mode: SELECTIVE EXPANSION)

### System audit

- `main` at `d3739ca`; last 30 commits are E04 content fence, registry storage hardening (#82), composite authority fence (#80) and the D08 workspace (#83). The repo is deep in E12 infrastructure; no commit in the window touches the CLI's real-data path.
- `TODOS.md` (repo) lists deferred product work (cell-origin method validation, removing reassurance language, `DESIGN.md`, longitudinal comparison "only after repeatability evidence and paid demand"). None blocks this plan; the "remove clean/normal/reassurance language before reuse" item applies directly to B3's report template.
- No design doc and no CLAUDE.md in the repo. `docs/PRODUCT-PLAN.md:61-73` sets a demand gate (interview target customers with research-only output before building the platform); no record of that gate being run was found.

### 0A. Premise challenge

| # | Premise (stated or implied) | Status | Assessment |
|---|---|---|---|
| P1 | The local 2.1 GB BAM is a meaningful stand-in for the product's input | Assumed | No provenance, consent or licence statement. It has no MM/ML tags, so it exercises fragment length only. Needs one line on origin and rights before any record from it is shown to anyone |
| P2 | Fragment-length output alone is worth a full CLI path | Assumed | EPICS.md defines the MVP around modBAM and later POD5; this slice proves one of the three signals |
| P3 | Plumbing (real BAM to browser) is the right next build, ahead of demand evidence and E0 | Stated (plan-vs-reality order) | Both outside voices challenge it; see User Challenge UC2 |
| P4 | Catalog + explorer is the right viewing surface for the first real record | Assumed | A static `report-local.html` or the existing E13 portable view (`evidence_inspector/portable_view.py`) would show the same record with fewer new surfaces |
| P5 | The E12 browser PR merges soon | Assumed | A3, A5 and B6 are gated on it; it is uncommitted work in a worktree today |
| P6 | Security H1/H6 ordering holds and no donor data enters this path | Assumed | The plan never says the golden path is forbidden on donor data until H1/H6 land |
| P7 | Name + length + order with WARN is acceptable reference provenance for an unqualified record | Decided as D1 (least-blocking default) | Codex says it leaves sequence identity unbound; Claude suggests `samtools reheader` on a copy. See UC3 |

These premises are the premise gate. They are recorded as undecided under "Pending user gates".

### 0B. Existing code leverage

| Sub-problem | Existing code | Plan reuses? |
|---|---|---|
| Reference record | `RegisteredReference`, `ReferenceContig` (`contracts.py:158-175`) | Yes |
| Preflight | `validate_bam_snapshot`, `_report` (`preflight.py:133-155`) | Yes; changes `_reference_matches` only |
| Run orchestration | `_demo` + `_demo_stages` + `Runner` (`cli.py:339-589`) | Yes, but the plan does not say "refactor, don't copy" (finding C5) |
| Signed bundle | `build_result_bundle` (`bundles.py:180`) | Yes |
| Verify | `traceback verify` (`cli.py:99-111`) | Yes, unchanged |
| Catalog | `ResultCatalog.import_bundle`, `resolve_current_capability` | Yes |
| Method authority | test-only `_authority()` (`tests/test_result_catalog.py:72`) | Pattern reused, values changed to unqualified |
| Viewer | `RunningLocalWebService`, `IntegratedExplorerSource`, `portable_view.py` | Server yes; portable view not considered |
| Operator mutex | `_operator_lock` (`cli.py:257`, applied at `cli.py:1276`) | Not mentioned (finding C4) |

### 0C. Dream state

```
CURRENT                         THIS PLAN                           12-MONTH IDEAL
synthetic demo only;     --->   one real aligned BAM becomes a  ---> modBAM/POD5 from a MinKNOW run
real BAM blocked at             signed, honestly labelled,           folder, E0-approved protocol,
preflight; no CI; suite         catalogued record on a CI-tested     qualified method, record shown to
fails under parallel runs       CLI path                             paying pilot readers under H1/H6
```
Delta: the plan moves toward the ideal on plumbing and honesty labels. It does not move on input realism (modBAM), protocol approval or demand evidence.

### 0C-bis. Implementation alternatives

```
APPROACH A: Full slice as written (B1-B7, catalog + serve)
  Effort: L (about 12.5 human days)   Risk: Med
  Pros: one CLI path end to end; catalog and explorer exercised with real data
  Cons: B5/B6 collide with the E12 browser branch and security H1/H6; 4 new durable surfaces
  Reuses: everything in 0B

APPROACH B: Minimal viable: register -> preflight -> run -> verify -> report-local.html
  Effort: M (about 7.5 human days)    Risk: Low
  Pros: proves "real BAM to signed, honestly labelled record"; no server.py/explorer conflicts
  Cons: no catalog or browser listing; B5/B6 deferred to a second milestone
  Reuses: runner, bundles, verify, report template

APPROACH C: Script-only: promote glue.py to scripts/dev_golden_path.py + one generated-BAM test
  Effort: S (about 2 human days)      Risk: Med (reuses test-only authority = QUALIFIED label)
  Pros: fastest demo
  Cons: keeps the "qualified" honesty defect unless patched; no CLI; no contract change
```
RECOMMENDATION (auto-decided, P1 completeness): A, the plan as written. Both outside voices prefer B. Because that changes the user's stated scope, it is User Challenge UC1, not an auto-decision.

### 0D. Selective expansion analysis

Complexity check: the plan touches more than 8 files and adds 4 new modules (`references.py`, `local_authority.py`, `web/local_source.py`, acceptance script). That is above the smell threshold. The minimum set that proves the core claim is approach B. Per autoplan override (CEO phase: never reduce silently) the scope stays; the reduction is surfaced as UC1.

Expansion candidates (each auto-decided; in-blast-radius and under 1 day CC unless noted):

| # | Candidate | Effort | Decision | Reason |
|---|---|---|---|---|
| X1 | Per-root random HMAC key for `provider_hmac_sha256` in `run` provenance (glue used a constant key) | S | ACCEPTED into B4 | Security gap in blast radius (P2) |
| X2 | `run`, `reference register`, `catalog import` take `_operator_lock` | S | ACCEPTED into B2/B4/B5 | Existing mutex, DRY (P4) |
| X3 | One table documenting the ROOT directory layout | S | ACCEPTED (new spec section via Eng phase) | Layout becomes a contract |
| X4 | APFS/reflink clone instead of a full 2.1 GB copy at seal | M | DEFERRED to TODOS | Outside blast radius (runner internals) |
| X5 | DoD ends with the record opened in a browser and shown to one outside reader | S | Part of UC2 | Changes the user's DoD |
| X6 | CI macOS job only on `main` pushes | S | TASTE (T1) | Cost vs coverage |
| X7 | Static portable view instead of `serve` | M | Part of UC1 | Changes stated scope |

### 0E. Temporal interrogation

```
HOUR 1  where does ROOT/records live, what perms, which HMAC key; v2 contract shape
HOUR 2-3 how run reuses _demo without copying it; what Runner flag replaces synthetic_enabled
HOUR 4-5 catalog import needs the same method identity run signed with (shared helper, B4 owns it);
         E06 view artifact API is not named in the plan
HOUR 6+  frozen fixtures and membership sets for v2; macOS runner time; real-BAM evidence table
```
Resolved now: method identity ownership (B4), HMAC key (X1), lock (X2). Still open for Eng: the E06 builder API name.

### 0F. Mode

SELECTIVE EXPANSION (autoplan override). Approach A retained pending UC1.

### Dual voices (CEO)

CODEX SAYS (CEO, strategy challenge): 8 findings. Integration milestone, not an MVP; the 10x move is a week of provider/workflow discovery first; "every stage exists" hides about 9 days of new composition; the BAM is availability-biased (no MM/ML/RG/M5/AS); E0 is sequenced too late and v2 contracts will encode temporary method assumptions; WARN weakens reference provenance; security ordering is contradictory (wants CI, E12 freeze, A3, minimum H1/H6, then any donor-data run); a cheaper alternative (glue as dev command + one test, defer B3/B5/B6) was dismissed.

CLAUDE SUBAGENT (CEO, strategic independence): 10 findings. DoD ends at a JSON GET instead of a human (critical; PRODUCT-PLAN.md demand gate unrun); 6 unstated premises (P1-P6 above); scope about 30% too big, defer B5/B6; v2 bump is the costly one-way part; 6-month regret is "beautiful CI, zero customers"; alternatives (glue script, portable view, reheader a copy) unanalysed; Track A should be its own epic; D6 forks the security model; competitive positioning absent; estimates exclude review rounds.

```
CEO DUAL VOICES — CONSENSUS TABLE:
═══════════════════════════════════════════════════════════════
  Dimension                            Claude  Codex  Consensus
  ──────────────────────────────────── ─────── ─────── ─────────
  1. Premises valid?                   mixed   mixed  CONFIRMED (mixed)
  2. Right problem to solve?           mixed   no     DISAGREE (degree)
  3. Scope calibrated correct?         no      no     CONFIRMED (too big) -> UC1
  4. Alternatives sufficiently explored? no    no     CONFIRMED (no) -> 0C-bis added
  5. Competitive/market risks covered? no      no     CONFIRMED (no)
  6. 6-month trajectory sound?         mixed   mixed  CONFIRMED (mixed) -> UC2
═══════════════════════════════════════════════════════════════
```

### Review sections 1-10 (CEO)

**1. Architecture.** New components: `references.py` (register/load), `local_authority.py` (method registry + identity), `web/local_source.py` (explorer factory), `serve` and `catalog import` subcommands. Coupling added: `traceback_runner/cli.py` now composes `evidence_inspector` method registry, result-trust registry and catalog, which before were composed only in tests and `glue.py`. That coupling is justified (it is the point of the slice) but it makes the ROOT layout a de facto contract (X3). Single point of failure: the development trust file `ROOT/trust/development-result-trust.json`; losing it orphans every record's verification. Rollback is per-PR revert except B3 (see 9). Diagram in Eng Section 1.

**2. Error & rescue map.** See the registry below. Two gaps: (a) disk exhaustion while sealing a 2.1 GB input has no named code; (b) a BAM with zero eligible alignments ends in `MeasurementUnavailableError` (`measurement.py:72`; the model itself also refuses at `contracts.py:406-407`), which `run` must map to a named outcome, not a traceback. Both auto-decided: add TBX-RUN-004 (insufficient space, retryable) and TBX-RUN-005 (measurement unavailable, not retryable) to B4.

**3. Security & threat model.**
| Threat | Likelihood | Impact | Mitigated? |
|---|---|---|---|
| `serve` adds a second operator-bootstrap launch path while H1 tightens reader sessions | Med | Med | Partly (loopback, B01 auth reused). Taste T2: security-spec sign-off on D6 |
| Constant HMAC key in provenance lets anyone recompute provider artifact tokens | High (glue did it) | Med | No -> X1 accepted |
| Real BAM from an unknown source shown as "real data" | Med | High (claims, consent) | No -> premise P1 |
| `source.json` stores an absolute FASTA path (path disclosure) | Low | Low | Acceptable: stays under ROOT, never in a bundle; B2 must assert bundles never include it |
| Donor data run before H1/H6 | Low today | High | No -> premise P6 |

**4. Data flow & edge cases.**
```
BAM ──▶ preflight ──▶ seal copy ──▶ measure ──▶ sign ──▶ publish ──▶ import ──▶ serve
 [missing .bai]  [BLOCKED]   [ENOSPC]     [0 eligible]  [key gone] [exists]  [untrusted]  [port busy]
```
| Interaction | Edge case | Handled? | How |
|---|---|---|---|
| `run` | same BAM twice | Partly | Runner dedupes by `input_tree_sha256` (as `demo` relies on); plan must state "returns the existing record" |
| `run` | Ctrl-C mid-measure | Yes | Runner pause/resume (B4 AC5) |
| `run` | concurrent `run` on one ROOT | No -> X2 | `_operator_lock` |
| `catalog import` | same bundle twice | Yes | B5 AC3 |
| `serve` | no catalog yet | Yes | B6 AC2 |
| `reference register` | 3.2 GB FASTA, no `.fai` | Yes | B2 requires `.fai` |

**5. Code quality.** Finding C5: B4 says "reuse the demo flow" but not how. Auto-decided (P4 DRY, P5 explicit): extract `_execute_signed_run(root, request, source, files, stages, key)` from `_demo` and call it from both; `demo` output bytes must not change. No over-engineering found: each new module has one caller set. `local_authority.py` is the only new abstraction and it replaces a test helper being reused in production.

**6. Tests.** Full diagram in Eng Section 3. CEO-level gaps: no test for ENOSPC (TBX-RUN-004), zero-eligible (TBX-RUN-005), operator-lock contention, or "catalog row never says qualified" for the real path (B5 AC5 covers files, not the API). All four added to the test plan artifact. The 2am-Friday test is the DoD script; the hostile-QA test is "import a bundle signed by a key removed from the trust registry".

**7. Performance.** Slow paths: sealing copies the 2.1 GB BAM (seconds to tens of seconds on SSD), FASTA MD5 over 3.2 GB (about 10-30 s), measurement scan 9.9 s. None is user-blocking at this scale. Memory: measurement streams; catalog rows are 1 per record. The ">= 2x input free space" doctor rule scales to about 60 GB for a 30 GB modBAM later, which is fine to state. No issues beyond X4.

**8. Observability.** CLI emits structured JSON results (`traceback.cli-result.v1`) and the runner keeps an audit log (`traceback logs`). Gap: `run` gives no progress output during a 2-minute run. Auto-decided (P1): `run` prints one line per stage boundary in human mode; JSON mode unchanged. Server diagnostics are out of scope (security spec item 8).

**9. Deployment & rollout.** No hosted deploy. Rollout is CI first (A1), then PRs in dependency order. Feature flag: `local_unqualified_enabled` on `Runner` (B4) is the kill switch. One-way door: B3 v2 contracts once any v2 bundle exists on disk; mitigation (Claude finding 4): keep real-run output in throwaway roots until the DoD is green. Branch protection is manual (D8).

**10. Long-term trajectory.** Reversibility 3/5 (v2 contracts and ROOT layout are sticky; everything else reverts). Debt introduced: a second report template, a production method-authority bootstrap with fixed values, fragment-only policy constants that E0 may change (Codex finding 5: expect a v3). Platform value: `local_authority.py` and `local_source.py` become the composition root E3/E4 will reuse.

**11. Design & UX.** Skipped: no UI scope detected.

### Error & Rescue Registry

| Codepath | What can go wrong | Exception / code | Rescued? | User sees |
|---|---|---|---|---|
| `reference register` | `.fai` missing or disagrees | TBX-REF-001 (new) | Y | "index missing or contradicts FASTA; run `samtools faidx`" |
| `reference register` | re-register with different bytes | TBX-REF-002 | Y | exit 3, existing registration kept |
| `preflight --reference` | unknown ID | TBX-REF-003 (new) | Y | exit 3 "reference not registered under ROOT" |
| `preflight` | M5 present and wrong | TBX-BAM-002 BLOCKED | Y | existing message |
| `run` | no `--reference` | TBX-RUN-003 | Y | existing refusal |
| `run` | ENOSPC during seal | TBX-RUN-004 (new) | N -> GAP, fixed | "not enough space under ROOT; need 2x input" |
| `run` | zero eligible alignments | `MeasurementUnavailableError` -> TBX-RUN-005 (new) | N -> GAP, fixed | "measurement unavailable: no eligible alignments" |
| `run`/`import`/`register` | another mutation in progress | `OperatorBusy` (`cli.py:252`) | Y after X2 | "another traceback command owns this ROOT" |
| `catalog import` | signer not in trust registry | catalog trust error | Y | exit 3, no row |
| `serve` | same ROOT already served | `LocalWebServerError` | Y | "already running" |

### Failure Modes Registry

| Codepath | Failure mode | Rescued? | Test? | User sees? | Logged? |
|---|---|---|---|---|---|
| run seal | disk full | Y (after fix) | Y (planned) | named error | runner audit |
| run measure | 0 eligible | Y (after fix) | Y (planned) | named error | runner audit |
| run sign | trust file deleted between runs | N | N | verify fails later | no | **CRITICAL GAP** -> doctor checks trust file presence (added to B7) |
| import | method identity mismatch with run | Y | Y | exit 3 | no |
| catalog row | says `qualified` for local record | Y (D5) | Y (B5 AC1) | correct state | n/a |
| serve | started twice on one ROOT | Y | Y (A3) | already running | no |

### NOT in scope (CEO additions)

- Customer interviews and the PRODUCT-PLAN demand gate: product work, not engineering; raised as UC2.
- modBAM/methylation signals: E4 territory; the real BAM has no MM/ML tags.
- Seal-by-clone (X4): runner internals; TODO.
- Competitive positioning copy: one paragraph belongs in PRODUCT-PLAN.md, not this spec.

### What already exists

See 0B. Short form: every stage has a library API; `glue.py` proved the chain at library level in about 30 s of compute; the missing parts are composition (authority, trust registry, explorer factory), honest labels (v2 approval state, report template) and CLI entry points.

### Dream state delta

After this plan: one real aligned BAM can become a signed, honestly labelled, catalogued record from the CLI, with CI guarding it. Still missing versus the 12-month ideal: modBAM/POD5 input, MinKNOW run folders (E3), an approved protocol (E0), qualified methods, security hardening for shared hosts, and any evidence that a reader wants the record.

### CEO completion summary

```
  +====================================================================+
  |            MEGA PLAN REVIEW — COMPLETION SUMMARY                   |
  +====================================================================+
  | Mode selected        | SELECTIVE EXPANSION                          |
  | System Audit         | no CI, no CLAUDE.md, demand gate unrun       |
  | Step 0               | approach A kept; UC1/UC2/UC3 raised          |
  | Section 1  (Arch)    | 2 issues (ROOT layout contract, trust SPOF)  |
  | Section 2  (Errors)  | 10 error paths mapped, 2 GAPS (fixed)        |
  | Section 3  (Security)| 5 issues found, 1 High-impact open (P1)      |
  | Section 4  (Data/UX) | 6 edge cases mapped, 1 unhandled (fixed X2)  |
  | Section 5  (Quality) | 1 issue (C5 DRY)                             |
  | Section 6  (Tests)   | 4 gaps added to test plan                    |
  | Section 7  (Perf)    | 0 issues (X4 deferred)                       |
  | Section 8  (Observ)  | 1 gap (run progress)                         |
  | Section 9  (Deploy)  | 1 risk (v2 one-way door)                     |
  | Section 10 (Future)  | Reversibility: 3/5, debt items: 3            |
  | Section 11 (Design)  | SKIPPED (no UI scope)                        |
  +--------------------------------------------------------------------+
  | NOT in scope         | written (4 items)                            |
  | What already exists  | written                                      |
  | Dream state delta    | written                                      |
  | Error/rescue registry| 10 paths, 0 CRITICAL GAPS after fixes        |
  | Failure modes        | 6 total, 1 CRITICAL GAP (trust file)         |
  | TODOS.md updates     | 2 items proposed                             |
  | Scope proposals      | 7 proposed, 3 accepted, 1 deferred           |
  | CEO plan             | written                                      |
  | Outside voice        | ran (codex + claude)                         |
  | Lake Score           | 6/7 recommendations chose complete option    |
  | Diagrams produced    | 3 (dream state, alternatives, data flow)     |
  | Stale diagrams found | 0                                            |
  | Unresolved decisions | 3 user challenges + premise gate (below)     |
  +====================================================================+
```

### CEO implementation tasks

- [ ] **C-T1 (P1, human: ~2h / CC: ~10min)** — B4 — add TBX-RUN-004 (ENOSPC) and TBX-RUN-005 (zero eligible) with tests. Surfaced by: Section 2. Files: `traceback_runner/cli.py`. Verify: `pytest -k "run_004 or run_005"`.
- [ ] **C-T2 (P1, human: ~2h / CC: ~10min)** — B4 — per-root random HMAC key for provenance tokens, `ROOT/trust/provenance-hmac.key` 0600. Surfaced by: Section 3. Files: `traceback_runner/cli.py`. Verify: two roots give different `provider_hmac_sha256` for the same BAM.
- [ ] **C-T3 (P1, human: ~1h / CC: ~10min)** — B2/B4/B5 — wrap mutations in `_operator_lock`. Surfaced by: Section 4. Files: `traceback_runner/cli.py`. Verify: concurrent `run` gets `OperatorBusy`.
- [ ] **C-T4 (P2, human: ~3h / CC: ~20min)** — B4 — extract `_execute_signed_run` from `_demo`; `demo` JSON unchanged. Surfaced by: Section 5. Files: `traceback_runner/cli.py`. Verify: existing demo tests byte-equal.
- [ ] **C-T5 (P2, human: ~1h / CC: ~5min)** — B7 — doctor checks the development trust file exists and parses. Surfaced by: Failure modes (CRITICAL GAP). Files: `traceback_runner/cli.py`.
- [ ] **C-T6 (P2, human: ~1h / CC: ~5min)** — B4 — stage-boundary progress lines in human mode. Surfaced by: Section 8.

> **Phase 1 complete.** Codex: 8 concerns. Claude subagent: 10 issues. Consensus: 5/6 confirmed, 1 disagreement (degree on "right problem"). Premise gate and 3 User Challenges pending (not auto-decided).

## Phase 3: Eng review

### Step 0: scope challenge (against the code)

1. Existing code per sub-problem: see CEO 0B. New finding: the E06 explorer path has no persistence at all (`web/explorer.py:69, 122` are in-memory), and the trust namespace (`signing.py:50-51`) and limitations template (`export.py:45-47`) are synthetic-only literals inside signed bytes. The original B3/B5 under-scoped both.
2. Minimum set: unchanged from CEO approach B (run -> verify -> report). Scope kept per autoplan rule (Eng: never reduce); the reduction stays User Challenge UC1.
3. Complexity check: triggers. After revision the plan touches about 30 files and adds 4 modules plus 3 contract versions (measurement v2, bundle v3, trust registry snapshot v2). Under autoplan this is not a stop; it strengthens UC1.
4. Search check: no framework built-ins apply; flock semantics are POSIX [Layer 1]; `samtools faidx`/`M5` definitions follow the SAM spec [Layer 1]. WebSearch not used.
5. TODOS cross-reference: repo `TODOS.md` item "remove clean/normal/reassurance language before reuse" is satisfied for the local template by B3a AC5. No TODO blocks this plan.
6. Completeness: the revised B3a/B3b/B5b are the complete versions; the shortcut (sign local records as `development-synthetic`) was rejected (D13).
7. Distribution: no new artifact type; the CLI ships in the existing wheel (`pyproject.toml` `[project.scripts]`). A1 adds CI but not publishing; publishing is out of scope (local prototype).

### Dual voices (Eng)

CODEX SAYS (eng, architecture challenge): 11 findings. B3 versioning incomplete (v1 `FragmentMeasurement` hard-wired in `bundles.py:382`, `export.py:80`; `MeasurementScan` drops `approval_state`); synthetic-only signed content beyond the measurement (`ExportLimitations`, `TrustNamespace`, result-trust registry); no frozen v1 bundle and catalog reader pinned to measurement v1; `WorkflowRelease` v2 is ceremonial; B5's durable authority API does not exist; no E06 persistence; B6 empty-root behaviour contradictory; A3 needs adversarial tests and must not wire reader launch before H1; A2 turns hangs into slower hangs; A4 drops live CI coverage; accepted CEO fixes missing from the item bodies. Verdict: no on 5 of 6.

CLAUDE SUBAGENT (eng, independent review): verified 4 code claims (A3 root cause holds; D5 enforcement holds; E06 store claim false; catalog reader uses exact tuple equality, not membership). Critical: E06 step is unplanned work (split B5b); B3 catalog reader mis-specified (needs a version decision). High: A3 short-lived `/tmp` lock with `LOCK_NB` causes spurious "already running" under `-n auto`; A2's 30 s test may hide a held transaction; 30 GB seal and 100-record replay unmeasured; `--id` path traversal, absolute FASTA path leakage, bootstrap URL in CI logs, reader explorer before H1. Medium: Runner flag plumbing, A4 fixture drift, missing CLI error-path tests.

```
ENG DUAL VOICES — CONSENSUS TABLE:
═══════════════════════════════════════════════════════════════
  Dimension                            Claude  Codex  Consensus
  ──────────────────────────────────── ─────── ─────── ─────────
  1. Architecture sound?               mixed   no     CONFIRMED (not as written) -> B3a/B3b/B5a/B5b rewrite
  2. Test coverage sufficient?         no      no     CONFIRMED (no) -> tests added
  3. Performance risks addressed?      mixed   mixed  CONFIRMED (mixed) -> TBX-RUN-004, 100-record budget
  4. Security threats covered?         mixed   no     CONFIRMED (gaps) -> ID regex, path, URL, reader explorer
  5. Error paths handled?              mixed   no     CONFIRMED (gaps) -> error registry extended
  6. Deployment risk manageable?       yes     no     DISAGREE -> taste T5
═══════════════════════════════════════════════════════════════
```

All CONFIRMED gaps were fixed in the item bodies above (auto-decided, P1/P5). Revisions applied: B3 split into B3a (measurement v2, bundle v3 + v3 catalog reader, limitations v2, frozen v1 fixture, scan carries approval state, no `WorkflowRelease` bump) and B3b (`development-local` trust namespace); B4 absorbed the CEO fixes; B5 split into B5a (durable authority store with pins and reopen validation, aliases, trust registry) and B5b (persisted `ExplorerArtifactRecord`s built through `bind_result_view_source`/`build_result_view`); B6 returns `None` on empty roots, refuses partial state, keeps reader launch at `explorer=None`; A2 gained liveness assertions and a root-cause step; A3 gained open-side retry and adversarial tests; A4 gained a nightly slow lane.

### Section 1: Architecture

```
                      traceback CLI (traceback_runner/cli.py)
   ┌───────────────┬──────────────┬───────────────┬────────────────┬──────────────┐
   reference       preflight      run             catalog import    serve
   register        --reference    (B4)            (B5a/B5b)         (B6)
   │ (B2)          │              │               │                 │
   v               v              v               v                 v
 references.py  preflight.py   _execute_signed_run  local_authority.py   web/local_source.py
 ROOT/references (WARN rule)   Runner(local_      ROOT/authority/*     build_local_explorer
                               unqualified_       ResultTrustRegistry  ─> IntegratedExplorerSource
                               enabled)           ROOT/trust/...           (catalog + bindings
                               │                  ResultCatalog            + artifacts)
                               v                  ROOT/catalog             │
                         measurement.py (v2)      compatibility.decide     v
                         bundles.py (bundle v3,   result_view.build      RunningLocalWebService
                         development-local sig)   ROOT/explorer/*        (server.py, A3 lock)
                               │                       ^
                               └── ROOT/records/<id> ──┘
```

ROOT layout (X3, now a contract):

| Path | Writer | Mode | Lifecycle |
|---|---|---|---|
| `ROOT/references/<id>/registered-reference.json`, `source.json` | B2 | 0600 | write-once |
| `ROOT/runner/runner.sqlite3`, sealed inputs | Runner | existing | per job |
| `ROOT/records/<record_id>/` | `_publish_verified_record` | 0700 dir | write-once |
| `ROOT/trust/development-result-trust.json`, `provenance-hmac.key`, `result-trust-registry/` | B4/B5a | 0600 | append/forward-only |
| `ROOT/authority/{method-registry,authority-head,pins}.json` | B5a | 0600 | write-once, pinned |
| `ROOT/catalog/` | ResultCatalog | existing | append |
| `ROOT/explorer/{artifacts,bindings}/<result_id>.json` | B5b | 0600 | write-once |
| `ROOT/web/` | server | 0700 | per serve |

Production failure per integration point: (a) `run` -> Runner: power loss mid-seal leaves a partial sealed input; existing runner recovery handles it (RUNNER-RECOVERY.md). (b) `import` -> catalog: crash between catalog row and explorer artifact write leaves a row with no view; B6 serves the row with `has_registered_view=false` and the detail route returns unavailable; re-running `import` writes the missing artifact (B5b must not treat "row exists" as "done"; added as B5b AC4 below). (c) `serve` -> server: two roots starting at once; A3 retry. Coupling: the CLI now composes `evidence_inspector` registries; acceptable, and it is the only composition root (no second copy in tests other than fixtures).

Decision added: B5b AC4 "if the catalog row exists but the artifact file does not, `import` writes the artifact and exits 0" (auto, P1).

### Section 2: Code quality

- DRY: `_execute_signed_run` (C5) fixed in B4. The deterministic alias derivation (B5a) must live in `local_authority.py`, not inline in `cli.py`.
- Naming: `local_unqualified_enabled` mirrors `synthetic_enabled`; consistent.
- Error handling: `serve` startup must not catch `Exception` when loading artifacts; it catches the named model validation and `OSError` only, and counts skips (B6).
- Over-engineering check: B3b adds a namespace rather than a new key type; minimal. Under-engineering: none left after B5a pins.
- Stale diagrams: the plan's own dependency graph was redrawn. `docs/RESULT-CATALOG.md` and `docs/SIGNING.md` describe one namespace and one reader; B3a/B3b PRs must update them (added to the files reference implicitly through the item docs; listed here so it is not lost).

### Section 3: Test review

```
CODE PATHS                                              USER FLOWS
[+] references.py                                        [+] Register hg38, then preflight real BAM
  ├── register: md5/fai/ID/gzip/idempotent  [PLANNED]      ├── [PLANNED] WARN path (no M5/AS)        [→E2E DoD]
  └── load by ID, unknown ID               [PLANNED]       └── [PLANNED] BLOCKED path (wrong M5)
[+] preflight._reference_matches (WARN rule) [PLANNED]   [+] run real BAM
[+] measurement v2 + scan approval_state   [PLANNED]       ├── [PLANNED] success, re-run dedupe     [→E2E DoD]
[+] bundles v3 + development-local signing [PLANNED]       ├── [PLANNED] ENOSPC, zero eligible, busy
[+] catalog v3 reader; v2 frozen fixture   [PLANNED] REG   └── [PLANNED] pause/resume
[+] result-trust registry namespace        [PLANNED] REG [+] catalog import
[+] _execute_signed_run (demo bytes equal) [PLANNED] REG   ├── [PLANNED] idempotent, untrusted key, tampered authority
[+] local_authority store + pins           [PLANNED]       └── [PLANNED] row exists, artifact missing (B5b AC4)
[+] explorer artifact build + persist      [PLANNED]     [+] serve
[+] local_source (None / partial / skip)   [PLANNED]       ├── [PLANNED] list + detail 200            [→E2E DoD]
[+] server anchor retry + adversarial      [PLANNED]       ├── [PLANNED] empty root, partial state
[+] reader_cli path fix                    [PLANNED] REG   └── [PLANNED] 100-record startup budget
[+] doctor checks                          [PLANNED]
COVERAGE (planned): 15/15 code paths, 12/12 flows have a named test  |  REG = regression test required (CRITICAL)
GAPS remaining: none in plan; real-BAM run is manual evidence only
```

Regression rule applied: frozen v2-synthetic bundle (B3a AC1), frozen trust registry (B3b AC2), `demo` byte-equality (B4 AC8), and the B1 path fix are mandatory regression tests. Flakiness risks: A3 concurrency tests (bound every wait, assert liveness), 100-record startup budget (use a generous 5 s and generated records). Test plan artifact written to `~/.gstack/projects/danwiggins-cfddemo/` (see report).

### Section 4: Performance

- Seal copy: linear in input size; 30 GB modBAM later means 60 GB free and minutes of copying. TBX-RUN-004 makes the refusal explicit; clone-on-seal (X4) is the TODO.
- FASTA MD5: one 3.2 GB pass at register; `doctor --deep` repeats it, so `--deep` is opt-in only.
- Serve startup: O(records) artifact parse + validation; budget 5 s at 100 records (B6 AC5).
- Catalog query: indexed SQLite, unchanged.
No N+1 or memory issues: measurement streams and artifacts are per record.

### Failure modes (Eng)

| New codepath | Realistic failure | Test? | Handling? | Silent? |
|---|---|---|---|---|
| register | 3 GB FASTA edited after registration | Y (doctor --deep) | WARN | no |
| run | disk fills mid-seal despite pre-check (another process) | N | Runner fails the job with OSError | no, but unnamed -> mapped to TBX-RUN-004 in the stage wrapper (auto-added) |
| bundle v3 | key in wrong namespace | Y | verify fails | no |
| import | crash between row and artifact | Y (B5b AC4) | re-import heals | no |
| serve | artifact file corrupted | Y (B5b AC2) | row unavailable, others serve | no |
| server lock | stale anchor + racing start | Y (A3 AC6) | per-anchor flock decides | no |
No critical gaps remain (every row has a test or a named, visible error).

### Worktree parallelization

| Step | Modules touched | Depends on |
|---|---|---|
| A1 | `.github/`, `pyproject.toml`, scattered lint fixes | — |
| A2 | `evidence_inspector/` (2 files), `tests/` | — |
| A3 | `traceback_runner/web/` (server.py) | E12 browser PR |
| A4 | `tests/`, `scripts/`, `.github/` | A1 |
| A5 | `docs/` | E12 browser PR |
| B1 | `traceback_runner/` (reader_cli.py) | — |
| B2 | `traceback_runner/` (cli.py, preflight.py, new references.py) | — |
| B3a | `traceback_runner/` (contracts, measurement, bundles, export), `evidence_inspector/result_catalog.py` | — |
| B3b | `traceback_runner/signing.py`, `evidence_inspector/result_trust_registry.py` | B3a |
| B4 | `traceback_runner/` (cli.py, runner.py, new local_authority.py) | B2, B3a, B3b |
| B5a | `traceback_runner/` (cli.py, local_authority.py) | B4 |
| B5b | `traceback_runner/` (local_authority.py), `evidence_inspector/` read-only use | B5a |
| B6 | `traceback_runner/web/` (new local_source.py), cli.py | B1, B5b, A3 |
| B7 | `traceback_runner/cli.py` | B2 |

Lanes:
- Lane A: A1 -> A4 (shared `.github/`).
- Lane B: A2 (independent).
- Lane C: B1 (independent; one line in reader_cli.py).
- Lane D: B2 -> B7 (shared cli.py doctor/preflight).
- Lane E: B3a -> B3b (contracts/signing).
- Lane F (after D and E merge): B4 -> B5a -> B5b -> B6 -> DoD.
- Lane G (after the E12 browser PR merges): A3, then A5.

Launch A, B, C, D, E in parallel worktrees. Merge them. Then F. G whenever the browser PR lands, but A3 must merge before B6.
Conflict flags: D and F both edit the `cli.py` parser (sequence D before F). E and F both edit `bundles.py`/`contracts.py` (E first). G and B6 both touch `traceback_runner/web/` (A3 first). B1 and security H1/launch fixes both touch `reader_cli.py` (trivial).

### NOT in scope (Eng additions)

- Publishing the CLI (wheel release pipeline): local prototype; A1 is test CI only.
- `WorkflowRelease` persistence and v2 (D12): revisit at E0.
- Clone-on-seal (X4): TODO with trigger "inputs above 10 GB".
- Result-view-source registry changes: B5b uses its own store.

### What already exists (Eng additions)

`ExplorerArtifactRecord` model (`web/explorer.py:92`), `bind_result_view_source`/`build_result_view` (`result_view.py:460, 729`), `decide_compatibility` (`compatibility.py:824`), `MeasurementUnavailableError` (`measurement.py:72`), `_operator_lock` command set (`cli.py:1274-1279`), per-anchor flock and instance lease (`server.py:223, 349-371`). The plan now reuses all of them.

### Eng completion summary

- Step 0: scope accepted as-is per autoplan rule; reduction surfaced as UC1; B3 and B5 split.
- Architecture review: 4 issues (E06 persistence missing, catalog reader model, trust namespace, partial-state serve).
- Code quality review: 3 issues (DRY run path, alias helper placement, catch-all in serve loader).
- Test review: diagram produced, 11 gaps identified and planned (4 regression-critical).
- Performance review: 2 issues (seal size, serve startup budget).
- NOT in scope: written. What already exists: written.
- TODOS.md updates: 2 items (X4 clone-on-seal; WorkflowRelease persistence at E0).
- Failure modes: 0 critical gaps after revisions.
- Outside voice: ran (codex + claude).
- Parallelization: 7 lanes, 5 parallel / 2 sequential.
- Lake Score: 9/10 recommendations chose the complete option (the exception: no `WorkflowRelease` bump).

> **Phase 3 complete.** Codex: 11 concerns. Claude subagent: 11 issues. Consensus: 5/6 confirmed, 1 disagreement (deployment risk) -> taste T5. Passing to Phase 3.5 (DX).

## Phase 3.5: DX review (mode: DX POLISH; product type: CLI tool)

### Developer persona card

| Field | Value |
|---|---|
| Who | One technical operator on a Mac (today the founder; later a lab operator on a provider workstation) plus AI coding agents that drive the CLI and suite |
| Knows | samtools, BAM/FASTA, shell; not this repo's ROOT layout or contract versions |
| Wants | "my BAM became a signed, honestly labelled record I can open", in one terminal session |
| Tolerates | a few minutes of hashing and copying; not guessing paths or flags |
| Fails on | relative paths, silent mislabels, errors without a fix |

### Developer empathy narrative

I have an aligned BAM and hg38 on disk. The README tells me how to run Streamlit, and the operator guide is titled "synthetic". I find `traceback run` in `--help`, it says "(disabled)". After this plan I would type six commands I have to discover from the spec, and the tool would tell me my record is `synthetic_only: true` in its own JSON. With the DX fixes, `run` shows me the locked policy, does the work, prints the record path and the next two commands, and the guide has the whole sequence to copy.

### Competitive DX benchmark

| Tool | Hello-world shape | TTHW |
|---|---|---|
| EPI2ME Labs workflows (Nextflow) | one command with `--bam` and `--ref` | Competitive (2-5 min after install) |
| `samtools stats` / `mosdepth` | one command, stdout summary | Champion |
| traceback today (real BAM) | not possible from the CLI | n/a |
| traceback after plan (as originally written) | 6 commands + doc archaeology | Red flag (15-25 min first time) |
| traceback after DX fixes | register once, then `run`, then open the printed report path; catalog/serve optional | Needs work to competitive (about 5-8 min, dominated by FASTA hashing and the seal copy) |

### Magical moment

`traceback run BAM --reference hg38` prints the locked policy, a stage line per stage, then "Signed local record ready (development trust, unqualified, not for clinical use)" with the absolute path to `report-local.html`. Delivery vehicle: `run`'s human output (B4) plus B8's guide. Under UC1's report-first DoD this is the whole journey.

### Developer journey map

```
STAGE        | DEVELOPER DOES                          | FRICTION POINTS                              | STATUS
-------------|-----------------------------------------|----------------------------------------------|---------
1. Discover  | reads README, OPERATOR-GUIDE            | no real-BAM path documented                  | fixed (B8)
2. Install   | uv sync; brew install samtools          | samtools prerequisite undocumented           | fixed (B8, B7 doctor)
3. Hello     | reference register; run                 | --assembly default blocked GRCh38 AS         | fixed (B2)
             |                                         | envelope says synthetic_only: true           | fixed (cli-result v2, B4)
4. Real use  | verify; catalog import; serve           | relative paths, verify needs bundle+trust    | fixed (absolute paths, verify --root)
             |                                         | serve exits on closed stdin                  | fixed (B6 signal-only)
5. Debug     | reads error JSON                        | codes without cause/fix/docs                 | fixed (_problem helper)
6. Upgrade   | runs new code on old ROOT               | trust-registry schema bump, no message       | fixed (fail-closed newer-store message)
7. Clean up  | deletes ROOT                            | sealed records are read-only                 | fixed (documented chmod; TODO purge cmd)
```

### First-time developer confusion report

```
Persona: technical operator, fresh clone, real BAM + hg38 on disk
T+0:00  README "Run locally" -> uv sync; streamlit. Nothing about the CLI.            [B8]
T+1:00  OPERATOR-GUIDE "synthetic operator guide"; `traceback` not on PATH (uv run).  [B8]
T+2:00  `run --help`: "(disabled)".                                                   [B4 help text]
T+3:00  reference register --id hg38-local; preflight blocks: AS=GRCh38 != hg38-local [B2 assembly rule]
T+8:00  run succeeds; JSON says synthetic_only: true.                                  [cli-result v2]
T+9:00  verify: which trust path? guide example is wrong.                              [verify --root, B8]
T+11:00 serve in background exits immediately (stdin closed).                          [B6]
```
All seven addressed in the item bodies above (auto-decided, P1/P5).

### Dual voices (DX)

CODEX SAYS (DX challenge): golden path cannot meet 5 minutes (7 commands + JSON + background server); plan ignores the CEO scope correction (defer B5a/B5b/B6, end at the HTML report); docs not copy-paste complete and no item updates README/guide; catalog import + serve expose architecture as operator work; errors lack a uniform problem/cause/fix contract and B1 used the wrong exit code; B7 contradictory; upgrade path lacks version/compat messaging. Verdict: no on 4 of 6.

CLAUDE SUBAGENT (DX independent): TTHW today impossible, after plan about 9 steps / 15-25 min; no doc carries the path (critical); every CLI envelope says `synthetic_only: true` (critical honesty defect); no guided chaining, relative paths; naming (`--id` vs `--reference`, `serve` vs `reader launch`, `--root` type); `--assembly` default trap; partial fix text; `serve` EOF breaks backgrounding; roots read-only on cleanup; escape hatches correctly absent but policy invisible; samtools undocumented.

```
DX DUAL VOICES — CONSENSUS TABLE:
═══════════════════════════════════════════════════════════════
  Dimension                            Claude  Codex  Consensus
  ──────────────────────────────────── ─────── ─────── ─────────
  1. Getting started < 5 min?          no      no     CONFIRMED (no) -> UC1 report-first
  2. API/CLI naming guessable?         mixed   mixed  CONFIRMED (mixed) -> T7
  3. Error messages actionable?        mixed   no     CONFIRMED (gaps) -> _problem helper
  4. Docs findable & complete?         no      no     CONFIRMED (no) -> B8
  5. Upgrade path safe?                mixed   mixed  CONFIRMED (mixed) -> fail-closed message
  6. Dev environment friction-free?    mixed   no     CONFIRMED (gaps) -> samtools doc, cleanup
═══════════════════════════════════════════════════════════════
```

### DX passes

| Pass | Before | After fixes | Evidence / what a 10 needs |
|---|---|---|---|
| 1 Getting started | 2 | 6 | 6 commands, no doc (confusion T+0..T+3). A 10 is one `run` after a one-time register, report path printed; needs UC1 |
| 2 CLI design | 5 | 7 | consistent `--root`, `verify --root`; `--id` vs `--reference` open (T7) |
| 3 Errors | 4 | 8 | `_problem` with code/cause/fix/retryable/docs; byte counts on ENOSPC. Not 10: no per-code doc pages beyond the guide table |
| 4 Docs | 2 | 7 | B8 guide section executed by the DoD script. Not 10: no troubleshooting walkthrough |
| 5 Upgrade | 5 | 7 | frozen v1/v2 fixtures, fail-closed newer-store message. Not 10: no migrate command (TODO) |
| 6 Dev environment | 4 | 8 | CI on both OSes, parallel-safe suite (A1-A3), samtools documented, doctor prints ROOT |
| 7 Community | n/a | n/a | private prototype with one operator; examined README/CONTRIBUTING absence, nothing to add now |
| 8 DX measurement | 1 | 5 | DoD script records per-step wall time in its output; no telemetry (correct for local-first) |

### DX scorecard

```
+====================================================================+
|              DX PLAN REVIEW — SCORECARD                             |
+====================================================================+
| Dimension            | Score  | Prior  | Trend  |
|----------------------|--------|--------|--------|
| Getting Started      |  6/10  |  2/10  |  +4 ↑  |
| API/CLI/SDK          |  7/10  |  5/10  |  +2 ↑  |
| Error Messages       |  8/10  |  4/10  |  +4 ↑  |
| Documentation        |  7/10  |  2/10  |  +5 ↑  |
| Upgrade Path         |  7/10  |  5/10  |  +2 ↑  |
| Dev Environment      |  8/10  |  4/10  |  +4 ↑  |
| Community            |  n/a   |  n/a   |   —    |
| DX Measurement       |  5/10  |  1/10  |  +4 ↑  |
+--------------------------------------------------------------------+
| TTHW                 | ~6 min | n/a (impossible) | ↑            |
| Competitive Rank     | Needs Work (Competitive under UC1)            |
| Magical Moment       | designed via `run` human output + B8           |
| Product Type         | CLI tool                                       |
| Mode                 | POLISH                                         |
| Overall DX           |  7/10  |  3/10  |  +4 ↑  |
+====================================================================+
| Zero Friction: gap (UC1) | Learn by Doing: covered (B8 executed) |
| Fight Uncertainty: covered | Opinionated + Escape Hatches: covered (locked policy, visible) |
| Code in Context: covered | Magical Moments: covered |
+====================================================================+
```

### DX implementation checklist

```
[ ] TTHW (register done) under 5 min; first time under 8 min
[ ] run prints policy, stage lines, absolute record + report path, next commands
[ ] cli-result v2 data_origin on every new command
[ ] every new error: code + summary + cause + fix + retryable + docs anchor
[ ] --root consistent; verify --root RECORD_ID
[ ] OPERATOR-GUIDE real-BAM section executed verbatim by the DoD script
[ ] README links it; samtools prerequisite listed
[ ] newer-store fail-closed message; cleanup command documented
[ ] serve runs with stdin closed
```

### NOT in scope (DX)

- `TRACEBACK_ROOT` env var (T6): doctor printing the resolved root covers the confusion for now.
- `traceback purge`/migrate commands: TODO; documented manual steps suffice for one operator.
- Folding `serve` and `reader launch`: blocked on security H1.
- Overrides for the locked policy, WARN rule or trust: deliberately absent (honesty).

### What already exists (DX)

`docs/OPERATOR-GUIDE.md` stable exit-code table (`:101-111`), `traceback.cli-result.v1` envelope, `TBX-*` code convention with `fix`/`retryable` in `_real_run_blocked` (`cli.py:226-238`), `reader launch`'s Enter-for-new-link loop (`reader_cli.py:819-835`), `--json` canonical output.

> **Phase 3.5 complete.** DX overall: 3/10 -> 7/10. TTHW: impossible -> about 6 min (target under 5 min needs UC1). Codex: 7 concerns. Claude subagent: 11 issues. Consensus: 6/6 confirmed.

## Cross-phase themes

- **Scope: finish at a human-visible report, defer catalog import + serve.** Flagged independently in CEO (both voices), Eng (Codex), DX (both voices). High-confidence signal -> UC1.
- **Honest labels leak through every layer.** CEO found the catalog's "qualified" label; Eng found `development-synthetic` in signed bytes and the synthetic limitations template; DX found `synthetic_only: true` in every CLI envelope. Fixed in D5, B3a, B3b and cli-result v2. Lesson for implementers: grep the whole pipeline for `synthetic` before calling the slice honest.
- **Security ordering with H1/H6.** CEO (both), Eng (both) -> taste T2 and premise P6.
- **"Every stage exists" undercounts composition work.** CEO (Codex), Eng (both: authority store and E06 persistence did not exist). Effort rose from about 16 to about 21.5 human days.

## Gate decisions (resolved 2026-10-02)

The operator instructed: "build when gates are in", taking the reviews' recommendations unless one reverses an explicit operator decision. None did.

| Gate | Decision |
|---|---|
| UC1: milestone split | **Accepted.** Milestone 1 DoD = DoD steps 1–4 plus `traceback run` writing the honest HTML report, opened from `R/records/<id>/report.html`. Items: A1, A2, A4, B1 (database path only; `explorer=None` stays), B2, B3a, B3b, B4, B7. **Milestone 2** = B5a → B5b → B6 → B8 plus DoD steps 5–7. A3 and A5 land after the E12 browser PR. |
| UC2: E0 and demand | **Accepted.** E0 protocol work and the PRODUCT-PLAN demand interviews start now, in parallel, as operator work. The "one outside reader sees the record" step waits for P1. |
| UC3: reference matching | **Keep WARN** (name + length; a missing `M5`/`AS` is a warning, a mismatch blocks). |
| UC4: timeouts | **Accepted.** The production SQLite timeout stays at 10 s. Only test waits are raised, each followed by an assertion that the thread or process finished. |
| P1 | Rule: the source BAM's provenance and consent are written down (operator) before any record from it is shown to anyone outside the team. |
| P6 | Rule: no donor data until security H1 and H6-min land. |
| Other premises (P2, P4, P5, P7) | Accepted as written. P3 is covered by UC2. |
| Taste T1–T8 | Accepted as auto-decided. |

The original gate text follows for the record.

## Pending user gates (now resolved, see above)

Nothing below is decided. The spec body reflects the user's stated direction plus auto-decided fixes; each gate says what changes if answered the other way.

### Premise gate (Phase 1, never auto-decided)

| # | Premise | Recommendation | Status |
|---|---|---|---|
| P1 | The local 2.1 GB BAM is an acceptable stand-in; its origin, consent and licence are known | Write one line on provenance and rights in the PR that runs it; do not show records from it outside the team until then | UNDECIDED |
| P2 | Fragment-length-only output justifies a full CLI path before modBAM | Accept for plumbing; do not present it as the product's signal set | UNDECIDED |
| P3 | Plumbing is the right next build ahead of demand evidence and E0 | See UC2 | UNDECIDED |
| P4 | Catalog + explorer is the right first viewing surface | See UC1 (report first) | UNDECIDED |
| P5 | The E12 browser PR merges soon | Accept; A3/A5/B6 wait for it | UNDECIDED |
| P6 | No donor data enters this path before security H1/H6 | Make it an explicit rule in the guide (B8) | UNDECIDED |
| P7 | Name + length + order with WARN is acceptable reference provenance for an unqualified record | See UC3 | UNDECIDED |

### User Challenges (both models recommend changing the stated direction)

**UC1: Report-first DoD; defer catalog import + serve** (CEO, Eng, DX)
- You said: Track B ends at `catalog import` -> `serve` -> HTTP GET lists the record.
- Both models recommend: DoD-1 = register -> preflight -> run -> verify -> open `report-local.html`; B5a/B5b/B6 become a second milestone.
- Why: it proves "real BAM to signed, honestly labelled record" in about 7.5 instead of about 13 human days, avoids every conflict with the E12 browser branch and security H1/H6, and gives a better first-run experience.
- What we might be missing: you may need the catalog/explorer path exercised with real data to freeze E12, or for a specific demo.
- If we're wrong, the cost is: real records are not browsable for another 1-2 weeks, and E12 integration bugs surface later.
- Recommendation: accept. UNDECIDED.

**UC2: Start E0 and demand evidence now, and end the slice with a person seeing the record** (CEO, both voices)
- You said: order is CI -> E12 freeze -> golden path -> E0 -> E3.
- Both models recommend: run E0 discovery and the PRODUCT-PLAN.md demand interviews (`docs/PRODUCT-PLAN.md:61-73`) in parallel with CI/E12, and add a DoD step "the record is shown to one outside reader".
- Why: the v2 contracts and locked policy encode method assumptions E0 may change (a v3 later); demand evidence is the binding constraint for a pre-seed company, not code.
- What we might be missing: E0 needs people and approvals you may not have yet; a record from a BAM of unknown provenance may not be showable (P1).
- If we're wrong, the cost is: founder time diverted from engineering for a week; possibly showing an output before it is ready.
- Recommendation: accept the parallel start; the "show one reader" step only after P1 is settled. UNDECIDED.

**UC3: Reference provenance (D1)** (CEO; agreement is partial)
- You said: least-blocking default, WARN when M5/AS are absent.
- Codex recommends: do not weaken to WARN; name+length does not bind sequence identity. Claude did not reject WARN but flagged `samtools reheader` on a copy as an unanalysed alternative that does not break sealing.
- Why: a record whose reference identity is unbound is weaker evidence, even when labelled unqualified.
- What we might be missing: the real BAM cannot be re-aligned cheaply; WARN is honest about the gap and the report says so.
- If we're wrong, the cost is: one extra prep step (`samtools reheader` with an M5-bearing header on a copy) for every BAM.
- Recommendation: keep WARN for this slice (it is labelled in limitations), and add a `--strict-reference` flag later only if E0 requires it. UNDECIDED.

**UC4: A2 timeout raises** (Eng, both voices)
- You said: raise `_SQLITE_WORKER_TIMEOUT_SECONDS` 10 -> 30 in production code and test waits 2 -> 30.
- Both models recommend: keep the production 10 s unless measured contention justifies 30; raise only test-harness waits, each followed by a liveness assertion; root-cause the 30 s catalog test before injecting a shorter timeout.
- Why: longer deadlines turn real hangs into slower hangs.
- What we might be missing: you have seen these flakes under parallel load; 30 s may be the measured need.
- If we're wrong, the cost is: some flakes remain under heavy parallel runs.
- Recommendation: accept; the spec already adds the liveness assertions and the root-cause step. UNDECIDED on the production constant.

### Taste decisions (auto-decided with a recommendation; override if you disagree)

| # | Phase | Decision taken | Alternative |
|---|---|---|---|
| T1 | CEO | macOS CI job on every PR (the product runs on Macs; flock and APFS behave differently) | macOS only on `main` pushes to save runner minutes |
| T2 | CEO/Eng | Ask the security spec's author to sign off on `traceback serve` (D6) before B6 | Build B6 first and let H1 adapt |
| T3 | Eng | Separate `development-local` trust namespace (B3b, +2 d) | Keep `development-synthetic` as a legacy name and document it |
| T4 | Eng | A4 frozen fixture + nightly/path-triggered slow lane | Codex: keep one bounded live product-gates case in every CI run |
| T5 | Eng | Deployment risk judged manageable (one-way doors documented) | Codex: not manageable until B3a/B3b land behind throwaway roots only |
| T6 | DX | No `TRACEBACK_ROOT` env var; doctor prints the resolved root | Add the env var now |
| T7 | DX | Keep `reference register --id` as you specified | Rename to `--reference` for one flag name across register/preflight/run |
| T8 | CEO | Keep Track A and Track B in one doc, with A1-A4 as their own milestone | Split Track A into a separate epic |

<!-- AUTONOMOUS DECISION LOG -->
## Decision Audit Trail

| # | Phase | Decision | Classification | Principle | Rationale | Rejected |
|---|-------|----------|----------------|-----------|-----------|----------|
| 1 | CEO | Mode SELECTIVE EXPANSION | Mechanical | autoplan override | iteration on existing system | HOLD, EXPANSION |
| 2 | CEO | Keep approach A (full slice) pending UC1 | User Challenge | P1 | user's stated scope is the default | B, C |
| 3 | CEO | Accept X1 per-root HMAC key | Mechanical | P2 | security gap, in blast radius, S | constant key |
| 4 | CEO | Accept X2 operator lock on new mutations | Mechanical | P4 | reuse `_operator_lock` | new mutex |
| 5 | CEO | Accept X3 ROOT layout table | Mechanical | P1 | layout is a contract | — |
| 6 | CEO | Defer X4 clone-on-seal | Mechanical | P3 | runner internals, outside radius | build now |
| 7 | CEO | Add TBX-RUN-004/005 | Mechanical | P1 | unnamed failures | traceback output |
| 8 | CEO | Extract `_execute_signed_run` | Mechanical | P4 | no copied orchestration | copy `_demo` |
| 9 | CEO | Doctor trust-file check | Mechanical | P1 | critical gap | — |
| 10 | CEO | Run progress lines | Mechanical | P1 | observability | silent run |
| 11 | CEO | macOS on every PR | Taste T1 | P1 | coverage | main only |
| 12 | CEO | Track A stays in this doc | Taste T8 | P3 | one plan, two milestones | split epic |
| 13 | CEO | CEO plan spec-review loop not run separately | Mechanical | P6 | the spec already passed 2 codex gates and 2 CEO voices; recorded as a deviation | 3-round loop |
| 14 | Eng | Split B3 into B3a/B3b | Mechanical | P5 | trust namespace is a separate contract | one item |
| 15 | Eng | Measurement v2 ships in bundle v3 + v3 reader | Mechanical | P5 | exact tuple match per reader (`result_catalog.py:400-420`) | widen v2 reader |
| 16 | Eng | No `WorkflowRelease` bump (D12) | Mechanical | P5 | never persisted today | bump anyway |
| 17 | Eng | New `development-local` namespace (D13) | Taste T3 | P1 | false label in signed bytes | legacy name |
| 18 | Eng | Frozen v2-synthetic bundle fixture first | Mechanical | regression rule | proves old bytes still verify | grep-only test |
| 19 | Eng | Split B5 into B5a (authority store) / B5b (explorer artifacts) | Mechanical | P5 | neither store exists | one item |
| 20 | Eng | Deterministic catalog aliases from record hash | Mechanical | P5 | `import_bundle` requires aliases | operator input |
| 21 | Eng | B5b heals a missing artifact on re-import | Mechanical | P1 | crash between writes | manual repair |
| 22 | Eng | `build_local_explorer` returns None; refuse partial state | Mechanical | P5 | resolves contradiction | always build |
| 23 | Eng | `reader launch` keeps `explorer=None` until H1 | Mechanical | P3 | user allowed reader launch OR serve | wire both |
| 24 | Eng | A3 open-side retry + adversarial tests | Mechanical | P1 | spurious "already running" under `-n auto` | NB only |
| 25 | Eng | A2 liveness asserts + root-cause step | Mechanical | P1 | hangs must still fail | blind raise |
| 26 | Eng | A2 production constant | User Challenge UC4 | — | both voices disagree with stated direction | — |
| 27 | Eng | A4 nightly + path-triggered slow lane | Taste T4 | P1 | fixture drift | live case per PR |
| 28 | Eng | Reference ID regex, gzip refusal, path-not-exported test | Mechanical | P1 | traversal and leakage | — |
| 29 | Eng | 100-record serve startup budget | Mechanical | P1 | unmeasured scaling | none |
| 30 | Eng | Deployment risk manageable | Taste T5 | P6 | one-way doors documented | not manageable |
| 31 | DX | Mode POLISH, product type CLI | Mechanical | autoplan override | — | — |
| 32 | DX | Add B8 operator guide item, executed by DoD | Mechanical | P1 | no doc carries the path | leave docs |
| 33 | DX | cli-result v2 with `data_origin` | Mechanical | P1 | `synthetic_only: true` on real records (`cli.py:154`) | keep v1 |
| 34 | DX | `_problem` helper with six fields | Mechanical | P4 | one error contract | ad hoc |
| 35 | DX | `--assembly` absent skips AS comparison | Mechanical | P5 | GRCh38 vs hg38-local trap | default to ID |
| 36 | DX | Absolute paths + next commands from `run`; `verify --root` | Mechanical | P5 | relative paths | — |
| 37 | DX | `serve` signal-only when stdin is not a TTY | Mechanical | P5 | backgrounding | EOF exit |
| 38 | DX | B1 missing DB exit 4 | Mechanical | P5 | guide's exit table | exit 6 |
| 39 | DX | Doctor trust rule only when records exist | Mechanical | P5 | removes contradiction | always BLOCKED |
| 40 | DX | Newer-store fail-closed message; documented chmod cleanup | Mechanical | P1 | upgrade/cleanup friction | — |
| 41 | DX | No `TRACEBACK_ROOT` | Taste T6 | P3 | doctor shows root | env var |
| 42 | DX | Keep `--id` | Taste T7 | user direction | user specified it | rename |
| 43 | All | Premise gate P1-P7 left undecided | Gate | — | never auto-decided | — |
| 44 | All | UC1-UC3 left undecided | User Challenge | — | never auto-decided | — |

## Implementation Tasks (aggregated)

CEO tasks C-T1..C-T6 and Eng tasks E-T1..E-T10 are listed in their phases and written to `~/.gstack/projects/danwiggins-cfddemo/tasks-*-review-*.jsonl`. DX tasks:
- [ ] **D-T1 (P1, human: ~3h / CC: ~15min)** — B4 — cli-result v2 `data_origin`; help text. Files: `traceback_runner/cli.py`.
- [ ] **D-T2 (P1, human: ~3h / CC: ~15min)** — B2/B4/B5a — `_problem` helper and six-field errors. Files: `traceback_runner/cli.py`.
- [ ] **D-T3 (P1, human: ~4h / CC: ~30min)** — B8 — operator guide real-BAM section, README link, executed by the DoD. Files: `docs/OPERATOR-GUIDE.md`, `README.md`, `scripts/golden_path_acceptance.sh`.
- [ ] **D-T4 (P2, human: ~1h / CC: ~10min)** — B2 — `--assembly` comparison rule. Files: `traceback_runner/preflight.py`.
- [ ] **D-T5 (P2, human: ~2h / CC: ~10min)** — B4 — absolute paths, next commands, `verify --root`. Files: `traceback_runner/cli.py`.
- [ ] **D-T6 (P2, human: ~1h / CC: ~10min)** — B6 — signal-only serve when stdin is not a TTY. Files: `traceback_runner/cli.py`.

## GSTACK REVIEW REPORT

| Review | Trigger | Why | Runs | Status | Findings |
|--------|---------|-----|------|--------|----------|
| CEO Review | `/plan-ceo-review` (via /autoplan) | Scope & strategy | 1 | issues_open | 7 proposals, 3 accepted, 1 deferred; 3 user challenges; premise gate pending |
| Codex Review | `/codex` (spec quality gate) | Independent 2nd opinion | 2 | clean | 7/10, 7/10; ambiguities folded into the spec |
| Eng Review | `/plan-eng-review` (via /autoplan) | Architecture & tests (required) | 1 | issues_open | 12 issues, 0 critical gaps after revisions; 1 user challenge (UC4) |
| Design Review | `/plan-design-review` | UI/UX gaps | 0 | skipped | no UI scope |
| DX Review | `/plan-devex-review` (via /autoplan) | Developer experience gaps | 1 | issues_open | score: 3/10 → 7/10, TTHW: impossible → about 6 min |

- **CODEX:** quality gate 7/10 twice; CEO 8, Eng 11, DX 7 concerns, all folded in or raised as gates.
- **CROSS-MODEL:** Claude and Codex agreed on 16 of 18 dimensions; disagreements on "right problem" (degree) and deployment risk (T5). Both independently found the report-first scope cut and the synthetic-label leaks.
- **VERDICT:** not cleared. Eng review findings are folded into the spec; premise gate and user challenges UC1-UC4 need Dan's answers before implementation starts.

**UNRESOLVED DECISIONS:**
- Premise gate P1-P7
- UC1 report-first DoD (defer B5a/B5b/B6)
- UC2 start E0 and demand evidence in parallel; show one reader
- UC3 reference provenance WARN vs strict
- UC4 A2 production timeout constant
