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
| D10 | Real-data copy into the runner | `run` seals the input the same way `demo` does; `doctor` checks free space >= 2x input size | Keeps runner recovery semantics; disk is the cost |

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
| B3 | `UNAPPROVED_LOCAL` contracts v2 + honest report | Critical | 3 d / 3 h | none |
| B4 | `traceback run` real BAM | High | 2 d / 2 h | B2, B3 |
| B5 | `traceback catalog import` | High | 3 d / 3 h | B3, B4 |
| B6 | `traceback serve` with catalog explorer | High | 1 d / 1 h | B1, B5 |
| B7 | `traceback doctor` real checks | Medium | 0.5 d / 30 min | B2 |
| DoD | Scripted acceptance run | Critical | 1 d / 1 h | B1-B7 |

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
1. That test completes in under 5 s (was ~30 s), checked with `--durations`.
2. Default behaviour unchanged: a test asserts `PRAGMA busy_timeout` returns `30000` on a default catalog.
3. 20 consecutive local runs of the three touched test files pass (`pytest --count` not required; a shell loop is fine).

Rollback: revert. No stored data changes.

### A3. Per-state-root web lock

Change (D2): in `_open_startup_anchor` (`server.py:178-274`), take the `/tmp` directory flock only while creating and validating the anchor, then release it (`LOCK_UN`) before returning. `_close_startup_anchor` (`server.py:276-302`) re-takes it with `LOCK_EX|LOCK_NB` retried every 50 ms for up to 5 s around the identity check and `unlink`; on timeout it skips the unlink (a stale anchor file is harmless: the next start re-opens it and its own `flock` decides) and still releases the per-anchor lock and closes descriptors. The per-anchor flock (`server.py:223`) and the instance lease (`server.py:1159`) stay held for the server's lifetime. `_require_startup_anchor` keeps every identity check.

Acceptance:
1. Two `RunningLocalWebService.start` calls with different `state_directory` values run at the same time in one process and in two processes.
2. Two starts with the same `state_directory` still fail with `LocalWebServerError("local web service is already running")`.
3. Running `pytest tests/web` in two worktrees at once produces 0 `already running` failures (was 16).
4. Existing anchor-identity tests still pass unchanged.

Tests: +3 in `tests/web/test_loopback_server.py` (distinct roots concurrent, same root rejected, two-process via `subprocess`).
Rollback: revert the commit; the lock file format is unchanged.
Conflict: `server.py` is also edited by the uncommitted E12 browser branch and by security items H1/H6. Land after the browser PR.

### A4. Split the product-gates fixture

Change: commit `tests/fixtures/product_gates/foundation_report.json`, produced once by `run_foundation_gates(... run_id="gate_run_20260929", captured_at=2026-09-29T00:00Z)`. The module `report` fixture loads it with `ProductGateReport.model_validate_json`. One new test, marked `@pytest.mark.slow`, runs the live harness and asserts the live report's structure (record counts, gate states) equals the fixture's, ignoring timing and memory fields. Add a regeneration script `scripts/regenerate_product_gate_fixture.py`.

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

Change: `reader_cli.py:811` opens `root / "runner" / "runner.sqlite3"` (the path `cli.py:556, 626` use); if it does not exist, exit 6 with "runner database not found; run `traceback demo` or `traceback run` first". The `explorer=None` half is NOT fixed here: B6's PR adds `build_local_explorer(root)` and wires it into `reader launch` as well as `serve`. Whether a reader-launched session may read the explorer at all is decided by security H1 (see Pending conflicts), so B1 only fixes the path.

Acceptance:
1. Regression test: `traceback demo --root R`, then call `reader_cli._launch` with a test grant and a closed stdin; assert the `JobStore` it opened is `R/runner/runner.sqlite3` and that the store returns the demo job by ID. The test does not go through HTTP, so it does not depend on the H1 session policy.
2. Missing DB exits 6 with the message above, and creates no file.

Tests: +2 in `tests/test_reader_cli.py` (or the existing reader CLI test module).
Rollback: revert.

### B2. Reference registration and `preflight --reference`

CLI:
```
traceback reference register --fasta PATH --id ID --root ROOT [--assembly NAME] [--json]
traceback preflight BAM [--index BAI] --reference ID --root ROOT [--json]
```
`--reference` is optional; when absent, preflight keeps the synthetic reference (today's behaviour, so the synthetic tests still pass).

Storage: `ROOT/references/ID/registered-reference.json` (canonical JSON of `RegisteredReference`) plus `ROOT/references/ID/source.json` holding the absolute FASTA path and size at registration (the FASTA is referenced, not copied). `--assembly` defaults to `ID`; `AS` on `@SQ` lines is compared to that value. Both files `0600`, write-once; re-register with identical bytes is a no-op, different bytes exits 3 TBX-REF-002). `asset_sha256` is the SHA-256 of the FASTA file bytes. Per contig: name, length and MD5 of the uppercase sequence with newlines removed (the SAM `M5` definition). Reads the FASTA streaming; requires `PATH.fai` and checks contig names and lengths against it.

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

Tests: +6 unit (register: md5 definition, fai mismatch, idempotent re-register, conflicting re-register; match: absent M5 → WARN, wrong M5 → BLOCKED), +1 CLI integration on a generated 2-contig FASTA/BAM.
Rollback: revert; delete `ROOT/references/`.

### B3. `UNAPPROVED_LOCAL` contracts and an honest report

Change (D3, D4):
- `ApprovalState.UNAPPROVED_LOCAL = "unapproved_local"`.
- New versions, each accepting `approval_state: Literal[UNAPPROVED_SYNTHETIC, UNAPPROVED_LOCAL]`: `traceback.fragment-policy.v2`, `traceback.fragment-measurement.v2`, `traceback.workflow-release.v2` (also replaces `synthetic_only: Literal[True]` with `data_origin: Literal["synthetic", "local_unqualified"]`). v1 models stay as they are and keep validating v1 bytes.
- Every membership set and dispatch that names a v1 literal must accept v2. Known sites from `grep -rln "traceback.fragment-measurement.v1"`: `traceback_runner/contracts.py`, `evidence_inspector/result_catalog.py`, `tests/test_bundles.py`, `tests/test_cohort_import.py`, `tests/test_runner_measurement.py`, `docs/MEASUREMENT-SOURCE-ARTIFACT-REGISTRY.md`. Repeat the grep for each bumped literal before coding. Then verify at runtime: a test imports each set and asserts `"...v2" in SET` (do not rely on grep; a missing comma silently concatenates literals).
- `build_result_bundle` (`bundles.py:180`, docstring "synthetic-only") chooses the report template by `approval_state`. New `report-local.html` template: title "Unqualified local measurement", a fixed banner "Unqualified. Local development record. Not for clinical use. Development signing key only.", no synthetic wording. Synthetic bundles keep today's `report.html` bytes.
- `limitations.json` for local records lists: unqualified method, development trust, no protocol approval (E0), reference matched by name and length only when D1 WARN applied.
- Any fingerprint or digest pinned over these files is recomputed and its version bumped per repo convention.

Do not touch the `synthetic_only: Literal[True]` fields in `evidence_inspector/*` registries (result-trust, result-view-source, cohort, etc.). B5 imports into E04 and builds E06, and neither path requires a synthetic-only registry change. If B5 finds one that does, stop and file it as a separate item.

Acceptance:
1. A v1 synthetic bundle produced before this change verifies and imports unchanged (frozen fixture test).
2. A v2 measurement with `approval_state="unapproved_local"` round-trips through `canonical_json_bytes`.
3. `"traceback.fragment-measurement.v2"` is in every runtime membership set that contains v1 (test enumerates them).
4. `report.html` for a local bundle contains the exact banner string; after removing that banner string, the HTML has no case-insensitive match for `synthetic` and no match for the regex `(?<![Uu]n)qualified`.

Tests: +8 (contract round-trips x3, membership runtime test, v1 fixture regression, report template x2, limitations content).
Rollback: revert before any v2 bundle is written. After v2 bundles exist on disk, rollback means v1-only code cannot read them; keep v2 readers if partial rollback is needed.

### B4. `traceback run` on real data

CLI: `traceback run BAM [--index BAI] --reference ID --root ROOT [--json]`. Without `--reference`, keep TBX-RUN-003 (so the old refusal path still has a test).

Behaviour: reuse the `demo` flow (`cli.py:536-589`): `JobRequest` → `Runner(root/"runner")` → three stages (preflight, measure, sign) → `_publish_verified_record`. Differences from `demo`:
- reference from B2 storage; preflight stage requires `fragment_measurement_eligible`.
- locked measurement policy `aligned-reference-span-local-v2`: contigs = the registered reference's contigs matching `^chr([0-9]{1,2}|X|Y)$`, or all registered contigs when none match (CI's generated 2-contig reference); `definition_id` embeds the reference ID; `min_mapping_quality=20`, bins `[0,100) [100,150) [150,200) [200,300) [300,500) [500,1000) [1000,inf)`, `approval_state=UNAPPROVED_LOCAL`. The policy is a module constant, not CLI input.
- workflow release `local-unqualified-v0`, `data_origin="local_unqualified"`.
- stage metadata says `"data_origin": "local_unqualified"` instead of `"synthetic_only": True`.
- result message: "Signed local record ready (development trust, unqualified, not for clinical use)".
- `Runner` is constructed with whatever flag replaces `synthetic_enabled=True` for this path; add `local_unqualified_enabled` rather than reusing `synthetic_enabled`.

Acceptance:
1. On the real BAM: exit 0, `eligible_alignments == 3435813`, run under 120 s end to end on Dan's Mac (measured 12.4 s for validate+measure+sign at library level; sealing copies 2.1 GB).
2. `traceback verify ROOT/records/<id> --trust-store ROOT/trust/development-result-trust.json` passes.
3. `traceback status JOB --root ROOT` reports complete and verified.
4. A BLOCKED preflight creates a failed job with TBX-BAM-002 and no record.
5. `pause`/`resume` between stages works as for `demo` (existing runner tests extended to the new stage set).

Tests: +4 CLI integration on the small generated BAM (success, blocked reference, no `--reference` refusal, resume).
Rollback: revert; `ROOT/runner` jobs created by `run` can be deleted with the root.

### B5. `traceback catalog import`

CLI: `traceback catalog import BUNDLE --root ROOT [--json]`.

Behaviour:
- Local method authority, created by `ensure_local_method_authority(root)` in `traceback_runner/local_authority.py`. That module ships in B4's PR (B4 needs the method identity to sign); B5 reuses it. Created on first use at `ROOT/authority/method-registry/` with the existing `MethodRegistry` and `AuthorityHead` APIs: one tool, one asset (the registered reference), one `MethodDefinition` `mth_fragment_aligned_reference_span@1.0.0-local`, one `QualificationRecord` with `DEVELOPMENT_UNQUALIFIED`, one `DisplayRoleAssignment` with `RESEARCH_BASELINE`, scope `scope_local`. Never QUALIFIED, never PROVIDER_PRIMARY (D5). Write-once; head pinned in `ROOT/authority/pins.json`.
- Result-trust registry at `ROOT/trust/result-trust-registry/` seeded from the development trust key `run` used; the catalog opens with `result_trust_registry=` (the protected path, `result_catalog.py:1120`), not `trust_store=`.
- `ResultCatalog(ROOT/catalog, import_roots={"records": ROOT/records}, result_trust_registry=...)` then `import_bundle(root_id="records", relative_path=<bundle dir name>, ...)`. `run` stamps the bundle's method identity from this same registry via `local_method_identity(root)` in the same module.
- Build the E06 result-view artifact at import and store it where `CanonicalExplorerArtifactRepository` reads it, so `/api/v1/explorer/results/{id}` returns 200.
- Idempotent: re-importing the same bundle returns the existing `result_id`.

Acceptance:
1. After import, `CatalogQuery(limit=10)` returns one row with `qualification_state="development_unqualified"`, `current_provider_eligible=false`, `trust_state="development_signature_verified"`.
2. The row's `has_registered_view` is true.
3. Re-import exits 0 with the same `result_id` and no new row.
4. A bundle signed by a key not in the trust registry exits 3 and adds no row.
5. Parsing every JSON file under `ROOT/authority`, no `qualification_state` or `state` field equals `"qualified"` and no `display_role` field equals `"provider_primary"`.

Tests: +5 integration on the small BAM.
Rollback: delete `ROOT/catalog` and `ROOT/authority`; records stay valid.

### B6. `traceback serve`

CLI: `traceback serve --root ROOT [--ipv6]`. Prints the one-use operator bootstrap link (the same B01 flow the existing loopback tests use) and blocks until Ctrl-C/EOF.

Behaviour: `build_local_explorer(root) -> IntegratedExplorerSource` in a new `traceback_runner/web/local_source.py`: catalog from B5, `CatalogAuthorityIndex` with one binding per catalog row built from `ROOT/authority`, `CanonicalExplorerArtifactRepository` over the B5 view artifacts. `RunningLocalWebService.start(store=JobStore(root/"runner"/"runner.sqlite3"), state_directory=root/"web", explorer=...)`.

Acceptance:
1. After B5, an operator-cookie `GET /api/v1/explorer/catalog?limit=10` returns 200 with the record; `GET /api/v1/explorer/results/{id}` returns 200.
2. With no catalog, the catalog route returns the existing unavailable problem, not a 500.
3. Two `serve` processes on different roots run at once (depends on A3).

Tests: +3 in `tests/web/`.
Rollback: revert; no persistent state beyond `ROOT/web`.

### B7. `traceback doctor`

Replace the hard-coded `real_data: blocked` check (`cli.py:174-210`) with:
- `samtools`: on PATH and `samtools --version` parses; WARN if absent (only needed for the user to index).
- `reference`: for `--root`, list registered references; each PASSes when the FASTA path in `source.json` exists with the recorded size and, with `--deep`, its SHA-256 equals `asset_sha256`. A missing FASTA is WARN (records already made stay valid).
- `disk`: free bytes on ROOT's volume; WARN under 10 GB.
- `root`: exists or can be created, owner is the user, mode not group/other-writable.
Exit stays `BLOCKED` only when the synthetic runtime check fails, as today.

Acceptance: each check has a pass and a non-pass test (+8 unit, using monkeypatched `shutil.which` and `shutil.disk_usage`).

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
A3 ──> B1 ──┐
B2 ──> B7   │
B2 ─┐       │
B3 ─┴> B4 ──> B5 ──> B6 ──> DoD
                     ^
                     B1
A5 after the E12 browser PR merges
```

Sequencing: A1-A3 first because every later PR needs green CI and parallel suites. B3 before B4 because `run` must never emit a record labelled synthetic. B5 before B6 because serve has nothing to list otherwise.

## Pending conflicts with in-flight work

| In-flight work | Overlap | Resolution |
|---|---|---|
| E12 browser branch `epic-e/e12-browser-integration` (uncommitted edits to `traceback_runner/web/server.py`, `web/explorer.py`, `web/reader_session.py`, `docs/RESULT-EXPLORER.md`, `docs/LONGITUDINAL-COMPARISON-REGISTRY.md`) | A3 (server.py), A5 (both docs), B6 (explorer wiring) | Browser PR merges first; A3, A5 and B6 rebase onto it |
| Security spec `docs/PILOT-SECURITY-HARDENING.md` H1 (reader sessions get 403 on explorer routes) | B1/B6: a reader-launched session would no longer list the catalog | D6: the DoD uses `traceback serve` with an operator session. Whether `reader launch` passes an explorer at all is H1's call |
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

Every item is one PR and reverts cleanly, except B3 after v2 bundles exist (see B3). No item migrates an existing store.

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
| `traceback_runner/contracts.py:40, 203-239, 387-389` | B3 |
| `traceback_runner/bundles.py:180`, report template (new) | B3 |
| `evidence_inspector/result_catalog.py` (v2 membership) | B3 |
| `traceback_runner/local_authority.py` (new) | B4 (created), B5 (used) |
| `traceback_runner/web/local_source.py` (new) | B6 |
| `traceback_runner/fixtures.py`, `scripts/golden_path_acceptance.sh` (new), `tests/test_golden_path_acceptance.py` (new) | DoD |

## Testing summary

| Layer | What | Count |
|---|---|---|
| Unit | ruff fixes, timeouts, reference md5/match, contracts v2, doctor checks | about +25 |
| Integration | web lock concurrency, CLI register/preflight/run/import/serve on generated BAM | about +17 |
| E2E | acceptance script in CI (small BAM) | +1 |
| Manual evidence | real BAM DoD run | 1 |

## Effort

Human: about 16 days total (A: 4 d, B: 12.5 d incl. DoD). CC + review: about 16 h of build time plus review rounds.
