<!-- /autoplan restore point: ~/.gstack/projects/danwiggins-cfddemo/docs-operator-usability-and-analysis-autoplan-restore-20261003-200941.md -->
# Operator usability, changeable analysis and an interpretable site

Status: draft spec, 2026-10-03. Verified against `main` at `e76e9d0`.
Scope: one epic, four tracks. Track A makes more BAMs runnable without help.
Track B lets a scientist change the fragment analysis without an engineer and
without breaking existing records. Track C makes the local site show the
histogram and explain itself in plain words. Track D pays down the code-health
debt that sits under A to C.

This is a development prototype. Nothing here qualifies a method, approves a
protocol, or makes a record fit for clinical use. Every output keeps its
"unqualified, local, not for clinical use" label.

## Context

The golden path works end to end on one real BAM: `reference register`,
`preflight`, `run`, `verify`, `catalog import` and `serve` (operator review,
2026-10-04: 47 s `run`, 0.8 s import). Four reviews of `e76e9d0` (evidence:
`~/scratch/crash-resume-2026-09-30/review2-2026-10-04/`, files `run-files.md`,
`algo.md`, `ui.md`, `code.md`, `test-quality.md`, `ui-shots/`) found that the
next three things the operator wants all fail:

1. **More BAMs.** A zip of more BAMs is pending. MinKNOW output is unaligned;
   preflight calls it "unreadable, truncated, or structurally invalid"
   (TBX-BAM-001), and nothing says how to align it. Records cannot be told
   apart (no label), listed (no `jobs` or `catalog list`), or compared
   (compare always says `unknown`).
2. **Change the analysis.** Bin edges and MAPQ are Python constants. One edit
   bricks every ROOT (TBX-AUTH-LOCAL-001), and a re-run on the same BAM returns
   the old record because the job key ignores the method.
3. **Show understandable results.** The site never shows the histogram. It
   shows machine tokens ("complete; sufficient; verified;
   development_unqualified") and JSON in `<pre>` blocks.

Who is affected: Dan (operator and the only reader today), the team's macOS
workstations that run `traceback`, the AI builder sessions that work in
parallel worktrees, and any scientist later shown a record.

Why now: the second batch of BAMs arrives soon, and every one of them hits the
unaligned-BAM blocker and the no-labels problem. The dedupe bug silently
returns a wrong record and must be fixed before anyone tries a second analysis.

How we know it is done: see "Definition of done". In short, the operator takes
three new BAMs (one unaligned) from a fresh ROOT to a labelled catalog, runs a
second, research policy on one of them, and opens a site that shows each
record's histogram, a plain-language status table and a side-by-side
comparison, without reading source code or JSON.

## Current state (verified 2026-10-03 against `e76e9d0`)

Review claims were checked in the code before use. Confirmed:

| Claim | Location | Verified |
|---|---|---|
| Job key ignores the method | `traceback_runner/cli.py:1636-1643` builds `JobRequest(sample_token="local-<ref>", input_tree_sha256_local, workflow_release_sha256=_local_workflow_sha256())`; `_local_workflow_sha256` (`cli.py:1053`) hashes the constant `_LOCAL_WORKFLOW_ID = "local-unqualified-v0"` (`cli.py:1029`). `JobStore.submit` (`store.py:449-466`) returns the existing job when `request_key` matches | yes |
| `_is_local_request` depends on that constant | `cli.py:1057-1060`, used by `status` (`cli.py:2221`), `resume` (`cli.py:2303`) and the envelope heuristic (`cli.py:2970`). `resume` parses the reference from the sample token (`cli.py:2322`) | yes (new: a key fix must keep these working) |
| Modification check requires a traceback-only `@PG` | `preflight.py:85-90`, `DS == "traceback.modified_base_model=<id>"` | yes |
| Unaligned BAM reported as corrupt | bare `except Exception` at `preflight.py:383` maps every pysam error to TBX-BAM-001 | yes |
| Explorer artifact built without `fragment` | `local_catalog.py:391-395` | yes |
| `fragment` is a two-record contract | `FragmentExplorerState` refuses `left.result_id == right.result_id` (`evidence_inspector/fragment_explorer.py:207`); `fragment_source_from_verified_bundle` accepts only `ResultBundleManifestV2` (`fragment_explorer.py:390`); local records are `ResultBundleManifestV3` (`contracts.py:591`) | yes (new: "populate `fragment`" cannot work for one record) |
| `report.html` bytes are part of the signed record | `bundles.py:547` refuses a bundle whose report differs from `render_bundle_report(...)` | yes (new: restyling the report invalidates every existing record) |
| Authority must equal the code-built registry | `local_authority.py:458-467`; `validate_local_method_authorities` (`local_authority.py:505-525`) refuses any non-reference-ID entry under `ROOT/authority` | yes |
| Parameters are constants | `local_authority.py:84-85` (`LOCAL_MIN_MAPPING_QUALITY = 20`, `LOCAL_BIN_EDGES = (0, 100, 150, 200, 300, 500, 1000)`) | yes |
| Duplicate lease renewer | `_stage_heartbeat` `cli.py:1199-1218`, used at `cli.py:1278, 1318`; swallows every exception | yes |
| HMAC key not crash-safe | `_provenance_hmac_key` `cli.py:1063-1115`: `O_EXCL` create, then write; a crash after create leaves a short file | yes |
| Code grammar | `web/contracts.py:280` and `cli.py:1035` use `^TBX-[A-Z]+-[0-9]{3}`; `TBX-AUTH-LOCAL-001/002` and `TBX-INTERNAL` do not match | yes |
| "Validated" in Streamlit | `app.py:473` ("Validated AI assessment replay") and `app.py:1382` ("Validated measurements"); `app.py:490` is a "Not built" disclaimer and stays | yes |
| JSON in `<pre>` | `web/static/app.js:29-42` | yes |
| Failure reason stored but not shown | `store.py:388` has `last_error`; only `_refuse_failed_job` (`cli.py:1545-1564`) reads it; `_status` (`cli.py:2209-2234`) does not | yes (the column exists; no migration needed) |

Not re-measured here (taken from the reviews, which ran them): wall times,
the 2x disk growth, the web "Runner status is stale" text, the 390 px table
overflow.

Problem codes in use today: `TBX-AUTH-001..007`, `TBX-AUTH-LOCAL-001..002`,
`TBX-BAM-001..002`, `TBX-CAT-001..002`, `TBX-INTERNAL`, `TBX-JOB-001`,
`TBX-MOD-001..002`, `TBX-OUT-001`, `TBX-REF-001..003`, `TBX-RUN-003..007`,
`TBX-SERVE-001..004`, `TBX-WEB-400/404/431/503`. New codes below take the next
free number in their family.

Exit codes (`cli.py:47-56`): 0 OK, 2 USAGE, 3 BLOCKED, 4 NOT_FOUND,
5 VERIFICATION_FAILED, 6 RETRYABLE_FAILURE, 7 INTERNAL_ERROR.

## Decisions (made autonomously; least-blocking defaults)

| # | Decision | Default chosen | Why |
|---|---|---|---|
| D1 | Where the method enters the job key (B1) | `workflow_release_sha256 = sha256("local-unqualified-v0:" + method_definition_sha256)`; `_is_local_request` = token starts with `local-` and the workflow hash is not the synthetic one. A re-run after upgrade makes a second record (job ID is in provenance), marked "same measurement as" | Field exists, `JobRequest` stays v1; the recognition rule no longer depends on code constants (eng review, both voices) |
| D2 | Research sample token | `local-<reference_id>` (built-in, unchanged) or `local-<reference_id>:<policy_id>` (research). `:` cannot appear in a reference ID (`references.py:35`); policy ID max 32 characters and reference + policy at most 63 | `resume` must know the policy; length cap keeps the token under `Identifier`'s 128 and the method version inside `MethodVersion`'s pattern |
| D3 | Research authority location | `ROOT/research-authority/<reference_id>/<policy_id>/` (same three files and write-once rules as the built-in store), not `ROOT/authority/<ref>+<policy>/` as `algo.md` sketched | `validate_local_method_authorities` refuses unexpected names under `ROOT/authority` (`local_authority.py:520-523`); a separate directory keeps the built-in path byte-identical and untouched |
| D4 | Policy file format | Canonical `FragmentMeasurementPolicyV2` bytes with `approval_state=unapproved_local`, at `ROOT/policies/<policy_id>/fragment-policy.json` plus `pins.json` (sha256), 0600, write-once | No schema bump; the measurement already validates any contiguous bins (`contracts.py:198-208`) |
| D5 | Single-record histogram source (C1) | A typed, non-persisted `LocalRecordView` built per request through the catalog's authority-bound reader, at `GET /api/v1/records/{record_id}`. The explorer artifact's `fragment` stays `None` | E07 needs two distinct results and a v1 measurement; a per-request view avoids a digested contract change; the authority-bound reader rejects stale authority |
| D6 | Pair comparison (C6) | A new non-persisted `LocalPairView` over two `LocalRecordView`s; E07 untouched. Same reference, MAPQ and bins: overlay and B−A table (copy "Same analysis settings; differences are descriptive"). Otherwise side by side, no subtraction | E07 cannot take local records without a contract revision (eng review, both voices) |
| D7 | Styled report (C7) | The signed `report.html` stays byte-identical. The styled report is a serve route, `GET /records/{record_id}/report`, styled by a packaged `report.css` (CSP forbids inline styles) | `bundles.py:547` would refuse every existing record if the signed report changed |
| D8 | Labels (A4b) | Unsigned operator labels in `ROOT/labels/<record_id>.json` (`{"label": str, "set_at": ISO-8601}`), grammar = `validate_public_text` + 1-80 characters, no `/`, `\` or control characters; never in CLI `--json`, logs, bundles or support bundles; shown in human output and the operator-session web API | A label inside signed bytes would change every record's digest; DESIGN.md forbids sample labels in JSON diagnostics |
| D9 | Failure reason (A3) | Show the existing `last_error` (`CODE: summary`) in `status` and `logs`; map CODE to CAUSE/FIX from one table in `traceback_runner/problems.py` | No DB migration; the code is already persisted |
| D10 | Unaligned BAM (A1) | Detect "no `@SQ` lines" before opening records; refuse with new TBX-BAM-003, exit 3, and print a `minimap2` + `samtools` command. Do not align for the operator | Alignment choices (preset, reference) are scientific decisions; printing the command is honest and cheap |
| D11 | Modification check (A5) | Accept a model declared in any `@RG DS` field as `modbase_models=<id>` (Dorado's header form) or in the traceback `@PG DS`. With valid MM/ML/MN tags and no declaration: WARN "tags present, model not declared", never "re-basecall" | The current advice is wrong for every real Dorado BAM |
| D12 | `preflight` without `--reference` | If ROOT has at least one registered reference, refuse with new TBX-REF-004 (exit 2) listing the IDs; with none, keep the synthetic default | Silent synthetic default always blocks a real BAM with a misleading contig error |
| D13 | Failed-run cleanup (A8) | On TERMINAL_FAILURE, delete the sealed input copy under `ROOT/runner/` and keep the job row and stage outputs; `traceback clean --failed --root R` removes copies left by earlier versions | A failed run's 1x-input copy has no further use; the job row still explains the failure |
| D14 | cli.py split timing (D10) | After wave 1's `cli.py` items (B1, D1, D2, A2, A3, A4a, A4b, A4c) and before wave 2's (A7, A8, A9, B2a, B2b, D4, D5). Move-only; canary unloaded and re-run around it | Wave 1 is a serial chain on `cli.py` anyway; wave 2 is where four lanes run in parallel and the split pays off. Revised by autoplan: all three outside voices that commented put the split off the critical path |
| D15 | Code family rename (D4) | `TBX-AUTH-LOCAL-001/002` become `TBX-AUTHORITY-001/002`; `TBX-INTERNAL` becomes `TBX-INTERNAL-001`. The guide keeps the old IDs as anchors that point to the new rows | Old job rows may hold the old strings in `last_error`; the anchors keep links working |
| D16 | Second analysis family | Out of scope (cell-origin first, trigger below) | 2-4 engineer-weeks (`algo.md` 1c); research mode covers parameter changes now |
| D17 | Derived fragment metrics (B3) | Computed in `LocalRecordView` from integer counts, only for 1-bp policies, never stored, never signed | No measurement schema bump; values are reproducible from the signed counts |
| D18 | Delta display for same-settings local pairs (C6) | Shown, under the heading "Same analysis settings; differences are descriptive" (taste T4; Codex voices would block deltas until a comparability decision exists) | You asked for deltas withheld only across definitions; same settings is a computational match, not a sample comparability claim, and the copy says so |
| D19 | Navigation (C0) | Hash routes `#/`, `#/records/{record_id}`, `#/compare?a=&b=`; catalog is home | Bookmarkable, Back works, no server routing change |
| D20 | Public record identifier | `record_id` (12-char short form in tables) in every command, route and label; `job_id` only for execution and recovery; `result_id` only in exact values | Three IDs confused the operator (DX review); the server maps record to result |

## Child items

Effort is human days / CC time. Every item is one PR, and each item's acceptance criteria test only that item's own behaviour; where a later item extends a test, the later item says so. Items marked (AP) were added or reshaped by the autoplan review below.

Wave 1 gets batch 2 running and viewable; wave 2 is the rest. Whether wave 2 is built as written, trimmed, or re-planned after wave 1 is User Challenge UC1 (undecided).

| # | Title | Wave | Priority | Effort | Depends on |
|---|---|---|---|---|---|
| A0 | Read-only triage of the pending zip (AP) | 1 | Critical | 0.5 d / 20 min | none |
| B1 | Job key includes the method | 1 | Critical | 0.75 d / 40 min | none |
| D1 | Crash-safe provenance HMAC key | 1 | High | 0.5 d / 20 min | none |
| D2 | Delete `_stage_heartbeat` | 1 | High | 0.5 d / 20 min | none |
| A1 | Unaligned BAM detection + alignment guide | 1 | Critical | 1 d / 1 h | A0 |
| A2 | Up-front input checks in `run` | 1 | High | 1 d / 45 min | B1 |
| A3 | JOB_ID on BLOCKED; reason in status and logs | 1 | High | 1.5 d / 1 h | A2 |
| A4a | `jobs`, `catalog list`, `run --import`, serve reload (AP) | 1 | High | 2 d / 1.5 h | A3 |
| A4b | `run --label` and `traceback label` | 1 | High | 1.5 d / 1 h | A4a |
| A4c | `catalog export --csv` (AP) | 1 | Medium | 0.5 d / 20 min | A4a |
| A5 | Modification check for real Dorado BAMs | 1 | High | 1 d / 45 min | A0 |
| A6 | Contig-mismatch diff; `preflight` reference rule | 1 | Medium | 1 d / 45 min | A1 (same file) |
| C0 | Site structure, navigation and states (AP) | 1 | Critical | 1.5 d / 1 h | none |
| C1 | `LocalRecordView` + histogram (SVG + table) | 1 | Critical | 3 d / 2.5 h | C0 |
| C3 | Plain-language state copy; preflight checks shown | 1 | High | 1 d / 45 min | C1 |
| C5 | Catalog record table, labels, checkbox compare; jobs disclosure | 1 | High | 2 d / 1.5 h | C1, A4b |
| D10 | Split `cli.py` (move only) | 2 (first) | High | 2 d / 1.5 h | wave 1 `cli.py` items |
| A7 | State-aware retry/pause refusals | 2 | Medium | 0.5 d / 30 min | D10 |
| A8 | Clean up failed-run copies | 2 | Medium | 1.5 d / 1 h | D10 |
| A9 | Human-readable output and papercuts | 2 | Medium | 1.5 d / 1 h | D10 |
| B2a | Policy store, `policy add` / `policy show` | 2 | High | 2 d / 1.5 h | D10 |
| B2b | `run --policy`, research authority, labelling | 2 | High | 3 d / 2.5 h | B2a |
| B3 | 1-bp policy preset + derived descriptive metrics | 2 | Medium | 1 d / 45 min | B2b, C1 |
| B4 | Single source for policy text; baseline regeneration | 2 | Medium | 1 d / 45 min | B2a |
| C2 | Typed renderers replace JSON `<pre>` | 2 | High | 1.5 d / 1 h | C1 |
| C4 | Denominator strip | 2 | High | 1 d / 45 min | C1 |
| C6 | On-demand pair comparison (`LocalPairView`) | 2 | High | 2 d / 1.5 h | C1, C5 |
| C7 | Styled printable report; strip "Validated" | 2 | Medium | 1 d / 45 min | C1, C4 |
| D3 | Filesystem write primitives | 2 | Medium | 2.5 d / 2 h | D1 |
| D4 | Code grammar and exit/retryable consistency | 2 | Medium | 1.5 d / 1 h | D10, A3 |
| D5 | v2 envelope for every local command | 2 | Medium | 1 d / 45 min | D10 |
| D6 | Authority rigidity documented | 2 | Low | 0.25 d / 15 min | B2b |
| D7 | Test globals and flake candidates | any | Medium | 0.75 d / 30 min | none |
| D8 | End-to-end lease-loss test | any | Medium | 1.5 d / 1 h | D2 |
| D9 | Doc drift | 2 (last) | Low | 1 d / 45 min | A9, B2b |
| DoD | Usability acceptance run | 1 and 2 | Critical | 1.5 d / 1 h | each wave's items |

### Track A: running files

#### A0. Read-only triage of the pending zip (day 0)

Added by the autoplan CEO review (both voices). Before A1, A5 or A6 is built, inspect the pending zip without running anything that writes under a ROOT: list its tree (barcode directories, `bam_pass`/`bam_fail`, POD5, FASTQ), count and size files, and for one BAM per barcode record the `@SQ` count, `@RG DS` (`basecall_model=`, `modbase_models=`), the `@PG` chain, MM/ML/MN presence on 100 reads, and whether it is coordinate-sorted. Forecast disk: zip size x 3 (unzipped, aligned copy, sealed copy). Write the result as a table (no paths, no read names, no sample identifiers) in the A1 PR description, and adjust A1/A5/A6 if the zip differs from the 28-file `bam_pass` set reviewed. Effort 0.5 d. No code.

#### A1. Unaligned BAM detection and an alignment guide

Change:
- `traceback_runner/preflight.py`: before reading records, if the header has
  zero `@SQ` lines, return one BLOCKED check, new `TBX-BAM-003` "This BAM is
  unaligned (no @SQ reference lines). MinKNOW and Dorado write unaligned BAMs
  by default." FIX text is the command block below, with the registered FASTA
  path filled in when `--reference` is given, else `REF.fa`:
  ```
  samtools fastq -T MM,ML,MN IN.bam \
    | minimap2 -ax map-ont -y REF.fa - \
    | samtools sort -o OUT.sorted.bam
  samtools index OUT.sorted.bam
  ```
  (`-T`/`-y` carry the modification tags through alignment.) The FIX text names the FASTA only in human output; `--json` and the preflight report carry the reference ID, never a path (the canary's leak scrub, `scripts/canary/real_bam_canary.py:307`). The `@SQ` check runs before D12's TBX-REF-004, so an unaligned BAM is told to align first. The guide labels alignment "an assisted prerequisite, outside traceback" (PRODUCT-SPEC's no-shell-commands goal is not met by this slice; see premise P5).
- Replace the bare `except Exception` at `preflight.py:383` with
  `except (OSError, ValueError)` for TBX-BAM-001; anything else propagates to
  the CLI's internal-error path. Keep the redacted message (no input paths).
- An empty BAM (zero records after the header) returns new `TBX-BAM-004`
  "BAM has no alignment records", BLOCKED, FIX "this is often a `bam_fail` or empty chunk; use the sample's `bam_pass` files".
- `doctor` reports a missing `minimap2` as WARN with `brew install minimap2`; the guide's prerequisites list it.
- `docs/OPERATOR-GUIDE.md`: new section "Aligning MinKNOW output" with the
  command, how to merge one sample's `bam_pass` chunks (`samtools cat -o SAMPLE.bam bam_pass/barcodeNN/*.bam`; never merge across barcode directories, which mixes samples),
  expected time per GB measured once on the operator's workstation (the PR records the Mac model, chip, macOS version and the `time` output), and a serial batch
  block: per barcode directory, merge, align, index, then `traceback run "$f" --reference ID --label "$(basename "$f" .sorted.bam)" --import --root R || echo "FAILED $f"`, ending with `traceback jobs --root R`. `tests/test_operator_guide.py` executes this block on generated fixtures (with `minimap2` stubbed when absent). `doctor`
  reports `minimap2` presence as WARN when absent (never blocks).

Acceptance:
1. A generated unaligned BAM (fixture extension in `traceback_runner/fixtures.py`, 0 `@SQ`, 50 reads) gives TBX-BAM-003, exit 3, and the printed command contains `minimap2 -ax map-ont`.
2. A header-only BAM gives TBX-BAM-004, exit 3; `run` on it never creates a job.
3. A truncated BAM still gives TBX-BAM-001.
4. A `RuntimeError` raised inside the scan reaches exit 7 (TBX-INTERNAL-001 after D4), not TBX-BAM-001, from `preflight`; inside `run` the validate stage turns it into a terminal `LocalStageRefusal` with TBX-INTERNAL-001 (never retried in a loop).
6. The A1 PR re-records the operator's real-BAM canary baseline if the preflight check list changed (`real_bam_canary.py --record-baseline --force`), so the 03:30 run does not go red on the merge night.
5. `tests/test_operator_guide.py` still executes the guide's bash block.

Tests: +5 in `tests/test_preflight*.py` (find the file by `grep -l validate_bam_snapshot tests/`), +1 doctor.
Rollback: revert.

#### A2. Up-front input checks in `run`

Change: in `_local_input_files` (`cli.py:1442`, moved by D10), before any
authority or copy work, stat the BAM and the index and refuse with:
- `TBX-RUN-008` BAM missing or not a regular file (exit 4);
- `TBX-RUN-009` index missing (exit 4), FIX "`samtools index BAM`, or pass `--index`";
- `TBX-RUN-010` the BAM's first 4 bytes are not BGZF magic `1f 8b 08 04` (exit 3), FIX "this is not a BAM; for FASTQ or POD5 see Aligning MinKNOW output".
Each problem prints CODE/CAUSE/FIX in human output and in `--json`. No input path appears in `--json` output (the existing locator rule).

Acceptance:
1. Missing BAM, missing `.bai`, and a text file renamed `.bam` each give their code and exit, and no `ROOT/runner` job row exists afterwards.
2. The previous "NOT_FOUND job, bundle, or trust material" text never appears for these cases.
3. `tests/test_run_local.py` locator tests still pass (no input path in JSON).

Tests: +3. Rollback: revert.

#### A3. JOB_ID on BLOCKED; failure reason in status and logs

Change:
- Every `run` refusal that happens after `submit` includes `job_id` in data and in the human line "JOB_ID <id>".
- The uncoded "BLOCKED lease active" (`_reject_live_worker`, `cli.py:712`) gets new `TBX-JOB-002` "another traceback process holds this job" with FIX "wait for it, or `traceback status JOB_ID`".
- `status`: for TERMINAL_FAILURE and RETRYABLE_FAILURE, the headline is `FAILED: <CODE> <summary>` (or `RETRYABLE: ...`) from `last_error`, and data gains `failure: {code, summary, cause, fix}`. CAUSE and FIX come from `traceback_runner/problems.py` `PROBLEM_TABLE: dict[str, ProblemText]` (new; one row per code; also used by the guide test). The summary is shown only when `last_error` matches `^TBX-[A-Z]+(-[A-Z]+)?-?[0-9]{0,3}:`; any other `last_error` (an uncoded exception, which can contain a filename, e.g. `SnapshotViolation`, `snapshots.py:78`) shows `code: null, summary: "uncoded failure; see traceback support-bundle"` and never prints the raw text in human or JSON output. `PROBLEM_TABLE` carries alias rows for `TBX-AUTH-LOCAL-001/002` and `TBX-INTERNAL` so rows written before D4 still show CAUSE/FIX.
- `status` also shows `input_remove_failed` (A8) when present.
- `logs`: same `failure` block, and timestamps as ISO-8601 UTC strings, not epoch floats.
- Exit code of `status` stays 0 (the query succeeded).

Acceptance:
1. A run refused by TBX-RUN-005 (empty eligible set) prints its JOB_ID; `traceback status JOB_ID` prints `FAILED: TBX-RUN-005`, cause and fix.
2. A test asserts every code literal in `traceback_runner/` and `evidence_inspector/` (regex `TBX-[A-Z]+(-[A-Z]+)?(-[0-9]{3})?`, which also catches the pre-D4 `TBX-AUTH-LOCAL-*` and `TBX-INTERNAL` forms) has a `PROBLEM_TABLE` row and a guide anchor. Codes built from f-strings are listed explicitly in the test.
3. Two concurrent `run` calls on one BAM: the second exits 3 with TBX-JOB-002 and a JOB_ID.

Tests: +5. Rollback: revert; no stored data changes.

#### A4a. `traceback jobs`, `traceback catalog list`, `run --import`

Change:
- `traceback jobs --root R [--json] [--limit N=20]`: newest first; columns JOB_ID (12 chars), state word, reference, policy (`built-in` or ID), label (human output only), started (local time, minutes), failure code if any. Reads the store read-only.
- `traceback catalog list --root R [--json]`: columns RECORD (12-char record ID; the one public record identifier, used in URLs and every command), label (human output only), reference, policy, eligible (thousands separators), imported (date), verification, and "same measurement as <id>" (B1). A record under `ROOT/records/` that is not imported shows `not imported` with the import command. `result_id` appears only in the exact-values disclosure.
- `run --import`: after publish, imports the record (same code path as `catalog import`) and prints the site URL hint. `catalog import` accepts a RECORD ID as well as a path.
- `serve` sees records imported after it started: the explorer source reloads persisted artifacts when the catalog database's modification time changes (checked per catalog request).
- Both list commands print `No jobs yet.` / `No records yet.` with the next command when empty.
- Labels never appear in any `--json` output (DESIGN.md privacy: no sample labels in JSON diagnostics).

Acceptance:
1. With 3 records (2 imported), `catalog list` prints 3 rows and one `not imported`.
2. `--json` rows contain no absolute paths and no labels.
3. `jobs` on a ROOT without `runner/runner.sqlite3` exits 0 with "No jobs yet".
4. A record imported while `serve` runs appears on the next catalog request without a restart.

Tests: +8. Rollback: revert.

#### A4b. Labels: `run --label` and `traceback label`

Change (D8): `run --label TEXT` writes `ROOT/labels/<record_id>.json` after publish (temp file + fsync + `os.replace`, 0600; last writer wins, no lock; read with no-follow and a 4 KiB bound). `traceback label RECORD_ID TEXT --root R` sets or replaces it. When `run` reuses an existing record and `--label` differs, the reuse line says "label changed from <old> to <new>". Human `catalog list`/`jobs` output, the site (C5) and the record view (C1) show it with the "operator note, not part of the signed record" qualifier. Labels never enter bundles, exports, support bundles, logs or any CLI `--json` output. They do reach the operator-session web API (taste T6; `DESIGN.md` Privacy gains one sentence saying so).

Acceptance:
1. Label grammar: 1-80 characters, must pass `validate_public_text` (`web/contracts.py:113-150`, the server's own check, so a label never 500s a route) plus: no `/`, `\`, NUL or control characters; violations exit 2 naming the rule.
2. Setting a label leaves every byte under `ROOT/records/<id>/` unchanged (tree sha256 before = after).
3. The guide says: do not put donor names or identifiers in labels.

Tests: +5. Rollback: revert; delete `ROOT/labels/`.

#### A4c. `traceback catalog export --csv`

Added by the autoplan CEO review. `traceback catalog export --root R --csv OUT.csv` writes one row per (imported record, bin): `record_id, reference_id, policy_id, min_mapq, bin_lower, bin_upper, count, eligible, scanned`. No labels, paths or identifiers beyond record and reference IDs. For notebooks; nothing is derived. Acceptance: counts per record sum to `eligible` and match `LocalRecordView`; refuses to overwrite an existing file (exit 3). +2 tests. Effort 0.5 d. Rollback: revert.

#### A5. Modification check for real Dorado BAMs

Change (D11): `_header_has_model` (`preflight.py:85-90`) also accepts any `@RG` whose `DS` contains `modbase_models=<id>`; `<id>` is recorded in the preflight report's check message as "declared by @RG DS". With valid MM/ML/MN on sampled reads and no declaration anywhere, TBX-MOD-001 is WARN with "Modification tags present; basecall model not declared in the header", FIX "No action needed for fragment length." The word "re-basecall" appears only when tags are absent or invalid.

Acceptance:
1. Fixture BAM with Dorado-style `@RG DS:basecall_model=... modbase_models=...` and valid tags: TBX-MOD-001 PASS.
2. Same tags, no declaration: WARN, and output never contains "Re-basecall".
3. No tags: unchanged behaviour (existing tests pass).

Tests: +3. Rollback: revert.

#### A6. Contig-mismatch diff; `preflight` reference rule

Change:
- TBX-BAM-002 BLOCKED compares the BAM's `@SQ` list with the reference by position and prints the first 3 differing positions (a missing or extra contig shows `-` on the absent side; a reorder shows as name differences at each moved position) as `position name_in_BAM length_in_BAM | name_in_reference length_in_reference` and the total count. When every length matches in order and names differ only by a `chr` prefix, FIX adds "rename with `samtools reheader`" and a one-line sed example; otherwise "this BAM was aligned to a different reference; register that FASTA or realign".
- D12: `preflight` without `--reference` on a ROOT with registered references exits 2 with new TBX-REF-004 listing the IDs, FIX "add `--reference <id>`" with the first registered ID filled in.

Acceptance:
1. A `chr1`→`1` renamed fixture: diff printed, reheader hint present.
2. A GRCh37-length fixture: diff printed, no reheader hint.
3. No `--reference`, one registered reference: TBX-REF-004, exit 2, ID listed.

Tests: +3. Rollback: revert.

#### A7. State-aware retry and pause refusals

Change: `retry` and `pause` (`cli.py:2257-2279`) check state first. COMPLETE: new `TBX-JOB-003` "job is complete; nothing to retry/pause", exit 3, FIX names the record. TERMINAL_FAILURE: new `TBX-JOB-004` "job failed terminally", exit 3, with the stored failure code and FIX "fix the input (for example realign or reindex it) and run it again on the same ROOT; a changed input is a new job". The generic "synthetic runner action failed" (`cli.py:3105`) is reserved for genuinely unexpected errors and exits 7.

Acceptance: retry on complete, retry on failed, pause on complete each give their code and exit 3 (+3 tests). Rollback: revert.

#### A8. Clean up failed-run copies

Change (D13): when a local job reaches TERMINAL_FAILURE, the CLI makes the job's sealed input directory owner-writable (snapshots are 0444/0555, `snapshots.py:108-109`), removes the sealed input files that `_sealed_local_names` (`cli.py:2280`) lists for that job (the BAM and index copies only) and appends `input_removed` to the job log. If deletion fails, it logs `input_remove_failed` with the OS error name, leaves the job state unchanged, and `clean --failed` retries. New `traceback clean --failed --root R [--dry-run]` removes copies of failed jobs left by earlier versions, prints bytes freed, never touches COMPLETE jobs, RETRYABLE jobs or `ROOT/records/`.

Acceptance:
1. After a TBX-RUN-005 failure, the job's sealed BAM is gone and `status` still shows the failure.
2. `clean --failed --dry-run` lists without deleting; a real run frees the listed bytes.
3. A RETRYABLE job's copy survives `clean --failed`; `resume` still works.
4. `status` and `support-bundle` on a job with `input_removed` succeed (the snapshot checks, `runner.py:667-696`, treat removed input of a terminal job as expected).
5. `traceback clean` with no flag is a usage error (exit 2).

Tests: +4. Rollback: revert; deleted copies are re-creatable by re-running.

#### A9. Human-readable output and papercuts

Change:
- Human output never prints one-line JSON: `doctor` checks render as an aligned `STATUS NAME detail` table; `logs` lines as `ISO-time stage message`.
- Word `PARTIAL` instead of "PASS ... partial".
- `run` reuse: when `submit` returns an existing COMPLETE job, print "Reused existing record <id> from job <job_id>; nothing was re-measured" and skip the "STAGE seal: copying" line. A new record whose measurement equals an earlier record's prints "same measurement as <id>" (B1).
- `NEXT_COMMANDS` use the guide's `verify` spelling.
- Guide: reference register takes seconds, not minutes; `RECORD_ID=$(ls R/records)` replaced by `run --json`'s `data.record_id`; the guide opens with a chooser ("I have a MinKNOW zip" / "I have an aligned BAM" / "I want the demo") and the real-BAM path moves to the top; an "Upgrading" section covers B1's second record and D15's code aliases; a "disk: each run adds about 1x the input under ROOT" note; lock message and its code in troubleshooting.
- Web jobs list: a finished, verified job no longer says "Runner status is stale" (state word from C3).

Acceptance: a golden-file test per command for human output (`doctor`, `logs`, `run` reuse), with paths and times normalised; no `{` at the start of any human-output line in those tests. +5 tests. Rollback: revert.

### Track B: changeable analysis

#### B1. Job key includes the method (CRITICAL)

Root cause: the runner deduplicates on the canonical request (`store.py:449-466`), and the local request names no method (`cli.py:1636-1643`). A run under a changed method returns the old job and its old record (exit 0, old bins).

Change (D1, revised by the eng review):
- `_local_workflow_sha256(method_definition_sha256: str) -> str` returns `sha256(("local-unqualified-v0:" + method_definition_sha256).encode("ascii"))`. `_run` passes `method_definition_sha256(local_method_definition(loaded.registered))` (B2b passes the research definition's).
- `_is_local_request(request)` becomes: the sample token starts with `local-` AND `workflow_release_sha256` is not the synthetic workflow hash (`cli.py:2304-2306`). It no longer compares against one constant, so legacy rows, new rows and rows written under a later method all stay recognisable without loading references or policies.
- Resume reads the reference (and, after B2b, the policy) from the sample token, never from the method version string.
- After upgrade, re-running a BAM that ran under the legacy key creates one new job and, because the job ID enters signed provenance as `run_token` (`cli.py:1371`) and provenance enters `record_id` (`bundles.py:329-336`), a second record with the same measurement. This is accepted and documented: the reuse line (A9) and `catalog list` (A4a) mark records whose canonical measurement sha256 equals an earlier record's as "same measurement as <short id>". The guide's upgrade note says so.

Acceptance:
1. Same BAM, two different method definitions on one ROOT (the test monkeypatches `local_method_definition` in its defining module and bypasses the authority check): two job IDs. B2b adds the end-to-end version with a research policy.
2. Same BAM, same method, twice: one job ID, A9's reuse line.
3. A job row written with the legacy constant (fixture DB) still reports `local_unqualified: true` in `status` and resumes.
4. A legacy-key job re-run after upgrade yields a second record; `catalog list` marks it "same measurement as" the first.
5. `JobRequest` schema stays `traceback.job-request.v1`; the 03:30 real-BAM canary passes on the operator's workstation against this branch before merge (recorded in the PR).

Tests: +5 in `tests/test_run_local.py`. Rollback: revert; new-key rows stay valid because the recognition rule no longer depends on the key.

#### B2a. Policy store, `policy add` and `policy show`

Change (D4):
- New `traceback_runner/policies.py`: `add_policy(root, policy_id, reference_id, min_mapq, bin_edges) -> Path`, `load_policy(root, policy_id) -> FragmentMeasurementPolicyV2`, `list_policies(root)`.
- Policy ID grammar: `^[a-z0-9][a-z0-9._-]{0,31}$`, no `..`, and `len(reference_id) + len(policy_id) <= 63` (so the research sample token stays within `Identifier`'s 128 characters, `contracts.py:26`, and the method version within `MethodVersion`'s pattern, `method_registry.py:84-101`). Reserved: `builtin` and `aligned-reference-span-local-v2` (the built-in `LOCAL_POLICY_ID`, `local_authority.py:81`).
- `traceback policy add --id ID --reference REF --min-mapq N --bin-edges 0,100,150,...,1000 --root R`. Closed flags only; no free-text fields. `--bin-edges` lists bounded edges (the help text says so, and that a final open bin is added): strictly increasing integers, the first edge must be 0 (a higher first edge would leave spans in no bin and fail the measurement's sum check, `contracts.py:476`, as an untyped retryable error), at most 4,095 edges (explorer limit 4,096 bins); the final unbounded bin starts at the last edge and is added automatically. Errors exit 2 with a one-line message naming the bad value and the rule.
- Files: `ROOT/policies/<id>/fragment-policy.json` (canonical bytes, `definition_id = "<id>.<reference_id>"`, `approval_state = unapproved_local`) and `pins.json` (canonical JSON `{"policy_sha256": "<hex>", "schema_version": "traceback.policy-pins.v1"}`). Write-once: an existing ID with different bytes exits 3 with new `TBX-POL-001` "policy ID already used with different settings; choose a new ID".
- `traceback policy show ID|builtin --root R [--json|--markdown]` prints MAPQ, bins and a plain sentence ("counts each eligible primary alignment's aligned reference span; excludes unmapped, secondary, supplementary, QC-fail and duplicate"). `--markdown` is B4's single source.
- `traceback policy list --root R`.
- `measurement.py:130-137` `_histogram` switches to `bisect` over the bin lower bounds (today it is O(distinct spans x bins); at 4,095 bins and long ONT reads that is about 4e8 steps).

Acceptance:
1. `policy add` then `policy show --json` round-trips the exact bins and MAPQ.
2. Re-adding the same settings exits 0 (idempotent); different settings give TBX-POL-001.
3. `--bin-edges 0,100,100`, `--bin-edges 50,100`, `--min-mapq 256`, a reserved ID and an over-long ID each exit 2.
4. Adding a policy changes no byte under `ROOT/authority/` or `ROOT/records/`.
5. `_histogram` gives identical counts to the old loop on the synthetic fixture and runs a 4,095-bin policy over 1e5 distinct spans in under 1 s.

Tests: +9. Rollback: revert; delete `ROOT/policies/`.

#### B2b. `run --policy`, research authority and labelling

Change (D2, D3):
- `run --policy ID`: loads the policy; refuses `TBX-POL-002` naming both references if the policy's reference differs from `--reference`. `run --preset ...` is a usage error that points to `policy add --preset`.
- Definition: `definition_id = "<policy_id>.<ref>"`, method version `1.0.0-local-<ref>-<policy_id>` (accepted by `MethodVersion` within the B2a length cap; add a test; never parsed back), `parameter_schema_sha256 = sha256(policy bytes)`.
- Research authority: `ROOT/research-authority/<ref>/<policy_id>/` built and validated by the same functions as the built-in store, parameterised by policy. `import_local_record` (`local_catalog.py:676`) peeks the measurement's `definition_id` (`_peek_local_record`, `local_catalog.py:126`) and opens the matching authority instead of always the built-in one.
- The persisted denominator artifact's exclusion label is built from the policy's MAPQ ("Below MAPQ <n>"), not the literal "Below MAPQ 20" (`local_catalog.py:220`).
- `serve` validates the built-in store as today (`validate_local_method_authorities`, `local_authority.py:497`) and each research store separately: a damaged research store hides only that policy's records (catalog row "failed verification: research policy <id> authority is damaged", TBX-AUTHORITY-002 with FIX "run `traceback doctor`; restore `ROOT/research-authority/<ref>/<id>/` from backup or stop using that policy; built-in records are unaffected"). It never tells the operator to delete the built-in authority.
- Labelling: run output, `LocalRecordView`, the catalog row and the signed `report.html` say "research policy `<id>` (operator-supplied, unqualified)". The signed report text changes only when `definition_id` is not the built-in one, so built-in records stay byte-identical.
- Canary: `scripts/canary/real_bam_canary.py --policy ID` records and checks a baseline per policy (`baseline-<policy_id>.json`). The 03:30 nightly canary keeps checking only the built-in path unless a policy baseline exists.
- Without `--policy`, the canonical measurement bytes and the rendered `report.html` for the same inputs are identical to `e76e9d0`'s. (Record IDs, job rows and signatures differ per run and per ROOT by design.)

Acceptance:
1. Golden test: the built-in run on the generated BAM yields the same canonical measurement sha256 and the same `report.html` bytes (rendered from a fixed measurement and limitations) as on `e76e9d0`; both pinned as literals in the test.
2. A research run on the same BAM yields a different record, which `verify`, `catalog import` and `serve` accept, with method version `1.0.0-local-ref-<id>` and the explorer's exclusion label "Below MAPQ <n>".
3. Removing a byte from a research authority file hides that policy's records in `serve` with TBX-AUTHORITY-002; built-in records still render.
4. `resume` of an interrupted research job resumes under the same policy (token parse).

Tests: +11. Rollback: one-way door once research records are imported (catalog rows bind the research authority context). Reverting the code on such a ROOT is unsupported; use a fresh ROOT. Built-in-only ROOTs revert cleanly.

#### B3. 1-bp policy preset and derived descriptive metrics

Change (D17):
- `policy add --preset one-bp --id ID --reference REF` = MAPQ 20, edges `0,1,2,...,1000` (1,001 bins incl. `1000+`).
- `LocalRecordView` (C1) adds, only when every bin below 1,000 is 1 bp wide:
  - `short_fraction`: count of spans in [100, 150) divided by count in [100, 220), with numerator and denominator shown;
  - `periodicity_10bp`: let `c[k]` be the counts for spans `60 + k`, `k = 0..89`, and `N = sum(c)`. `F(f) = |sum_k c[k] * exp(-2*pi*i*f*k)|`. The index is `F(1/10) / N`, rounded half-even to 3 decimals, with the window [60, 150) shown. `N = 0` gives `null` and the text "no eligible alignments in 60-149 bp". Same zero rule for `short_fraction`.
  Both are labelled "descriptive; not a diagnostic or a validated metric" and carry their formula as text.
- The chart for 1-bp policies draws a line over 1-bp densities, with the built-in bins as optional gridlines.

Acceptance: on a synthetic fixture with a planted 10-bp oscillation (amplitude 20% of mean) the index is above 0.05, and on a flat fixture it is below 0.005 (absolute thresholds: a flat signal's coefficient is exactly 0, so a ratio test divides by zero); integer inputs give identical outputs across runs; the values never appear in signed bytes (grep the bundle). +4 tests. Rollback: revert.

#### B4. Single source for policy text; baseline regeneration

Change: `docs/OPERATOR-GUIDE.md` and `docs/ALGORITHMS.md` include the built-in policy as a fenced block generated by `traceback policy show builtin --markdown`; `tests/test_policy_docs.py` asserts the blocks equal the command's output. New `scripts/regenerate_canary_baseline.py --synthetic` rewrites `tests/fixtures/canary/` baselines and prints the diff; the PR template line says when to run it.

Acceptance: editing a bin edge in code without regenerating fails the docs test with the exact command to run (+2 tests). Rollback: revert.

### Track C: an interpretable site

Layout target (from `ui.md`): single-record view, in order: header with "Development record · not for clinical use"; stat tiles; histogram; denominator strip; "What this record is"; "How to read it (descriptive, not diagnostic)"; collapsed exact values and identities. No reference band, threshold or "normal" range anywhere.

#### C0. Site structure, navigation and states

Added by the autoplan design review (both voices). Defines the contract every Track C item tests.
- Routes (D19): `#/` catalog (home), `#/records/{record_id}`, `#/compare?a={record_id}&b={record_id}`. Clicking a catalog label opens the record; Back returns to the catalog with checkboxes intact (selection in `sessionStorage`, cleared on logout); focus moves to the view's `h1` after each route change. A persistent banner on every view: "Development records · unqualified · not for clinical use".
- Record view order (taste T5): (1) `h1` = label, else short record ID, with reference, policy and short ID beneath; (2) one status line: qualification word, verification word, preflight warning count; (3) histogram; (4) denominator strip with two values ("Eligible of scanned", "Share over 1 kb"; "Share 150-200 bp" only when the policy has edges at 150 and 200); (5) "What this record is" with preflight warnings listed; (6) "How to read it (descriptive, not diagnostic)"; (7) exact values and identities, collapsed. No green/red or threshold colouring on any number.
- States per view: the table in this document's Phase 2, Pass 2 is normative (loading, empty, error, success, partial for catalog, record, compare, report, jobs, session). Implemented with the `data-state` pattern from `longitudinal.js:548`. Refresh on window focus plus a Refresh button; no polling.
- Reader sessions: the new routes return 403; the E12 section stays below the catalog.
- Below 42rem: catalog rows become stacked blocks in table order; charts drop per-bar labels and thin ticks to 0, 200, 500, 1000; compare stacks charts vertically. 200% zoom reflows. All new controls are at least 44 px and keyboard-operable; catalog -> record -> compare -> Back completes by keyboard alone.
- Rounding: one decimal, half-even; footnote "shares rounded; exact counts in the table".

Acceptance: DOM harness tests for every state cell in the Pass 2 table (about 20), keyboard-only journey, focus after route change, no horizontal scroll at 390 px per view. Effort 1.5 d. Rollback: revert.

#### C1. `LocalRecordView` and the histogram

Change (D5):
- New `traceback_runner/web/records.py`: `LocalRecordView` (Pydantic, not persisted, not digested): `record_id`, `result_id`, `label | None`, `reference_id`, `policy: {id, builtin: bool, min_mapq, bins}`, `method_version`, `records_scanned`, `eligible_alignments`, `exclusions: [{reason, count}]`, `histogram: [{lower, upper | None, count}]`, `measurement_sha256`, `preflight: {outcome, checks: [{code, outcome, summary}]}`, `states: [{axis, token, label, meaning}]` (C3), `derived` (B3).
- Built per request through the catalog's authority-bound reader (the path the live explorer reader uses, which replays the current capability and rejects stale authority, `result_catalog.py:3604`), not `verify_reference` alone (`result_catalog.py:2904`, bytes and signature only).
- Route `GET /api/v1/records/{record_id}`, operator session only (reader sessions get 403 like the explorer routes). The server maps `record_id` to `result_id` through the catalog. The route joins the hardened dispatch table and integrity pins (`_INSTALLED_EXPLORER_HTTP_DEPENDENCIES` `server.py:675`, `assert_intact` `server.py:634-672`, route table `server.py:1296+`). Every string passes `validate_public_text` (`server.py:669-671`).
- `app.js`: the record view per C0. An SVG histogram drawn with the `svg()` helper moved from `longitudinal.js:256-387` into `static/chart.js`. Variable-width bars, area = share of eligible (y = share / bin width in bp). The open `1000+` bin is hatched and drawn from 1,000 to 1,200 bp with height share / 200 as a display convention; a visible footnote and its table row say "open bin, width not defined; share is exact". X ticks at bin edges, axis titles "Aligned reference span (bp)" and "Share of eligible alignments per bp", each bar labelled `n` and `%` (desktop only), the bin with the largest count annotated "most common bin" (by count, not by height). Caption: "n = 3,435,813 eligible alignments; policy built-in (MAPQ >= 20)". An HTML table with the same rows follows the chart. For policies with more than 50 bins: a line over per-bp densities, no per-bar labels, and the table grouped into 10-bp rows with a "show every bin" toggle.
- Tokens in `styles.css` `:root`: `--chart-a`, `--chart-b`, `--chart-hatch`, `--neutral`, `--focus` (one focus colour), each checked at 4.5:1. SVG has `role="img"`, `<title>`/`<desc>` via `aria-labelledby`, `aria-describedby` to the table.

Acceptance:
1. API: the view's histogram counts equal the signed measurement's and sum to `eligible_alignments`.
2. A tampered bundle (one count edited) and a stale authority each make the route return HTTP 503 with TBX-WEB-503 and no counts; the page shows C0's 503 copy.
3. DOM harness (`tests/web/app_dom_harness.js`): one `<svg>` with N `<rect>` for an N-bin built-in policy, the hatched final bar, the footnote, and a table with N rows; for a 1,001-bin policy one `<path>`, a 101-row grouped table, and no per-bar labels.
4. No horizontal page scroll at 390 px; 200% zoom reflows; screenshots at 1280 and 390 px attached to the PR.

Tests: +7 Python, +4 DOM. Rollback: revert; nothing stored.

#### C2. Typed renderers replace JSON `<pre>`

Change: `displayValue`/`renderArtifact` (`app.js:29-42`) are replaced by per-kind renderers (E07 comparison table, E11 provenance key/value list, E10 portable table). Panels whose artifact is absent for local records (E08, E09, E13) are not rendered at all; a single line says "Cell-origin, copy-number and sensitivity analyses are not part of local records." "Displayed" and "Eligible" merge into one "Eligible alignments" column.

Acceptance: no `<pre>` in the rendered single-record or compare views (DOM test); no "missing" text for local records (+3 DOM tests). Rollback: revert.

#### C3. Plain-language state copy

Change: `traceback_runner/web/state_copy.py` holds one row per enum value shown in the UI: qualification (`development_unqualified` = "Not qualified: a development measurement, not checked against any approved method"), trust (`development_signature_verified`), display role (`research_baseline`), reference match (`name_and_length_only`), preflight outcomes, comparison outcomes, job states. `LocalRecordView.states` carries `{axis, token, label, meaning}`. The site's "What this record is" table renders one row per axis. Preflight WARN/PARTIAL checks appear as a list with their codes. The jobs list uses the same job-state words (fixes "stale" for finished jobs).

Acceptance: a test enumerates every member of the enums used in `LocalRecordView` and asserts a row exists; no raw token appears as visible text outside the "exact values" disclosure (DOM test). +3 tests. Rollback: revert.

#### C4. Denominator strip

Change (cut from four tiles to two values by the design review): the strip's header carries "Eligible of scanned" (`3,435,813 of 5,822,296 (59.0%)`) and "Share over 1 kb"; "Share 150-200 bp" only when the policy has edges at 150 and 200; "most common bin" lives on the chart only. No colour encodes good or bad. Strip: scanned → each exclusion reason with count and % of scanned → eligible, as one horizontal bar plus a list. Numbers use thousands separators and one-decimal percentages.

Acceptance: the strip values and the strip reconcile (exclusions + eligible = scanned) in a DOM test; the 150-200 share on the real BAM is reported in the PR as manual evidence. +2 tests. Rollback: revert.

#### C5. Catalog record table, labels, checkbox compare; jobs disclosure

Change: the landing view (C0 `#/`) is a table: record (label with the short record ID beside it; the column header carries "(operator note)" once), reference, policy, eligible, scanned, preflight (word + warning count, expandable), imported, "same measurement as" (B1), a compare checkbox named "Compare <label>". Method ID and version become `<select>` filters populated from the catalog. Selecting exactly 2 enables "Compare"; the disabled button carries `aria-describedby` "Select exactly 2 records (N selected)" in a polite live region. No comparison is shown by default. Jobs collapse to one `<details>` line ("Last job finished and verified; no job is running" or "Running: stage measure, 1 min 20 s"); "Runner status is stale" applies only to non-terminal jobs, per DESIGN.md's rule. Rows that fail verification show "failed verification" and the verify command; other rows render.

Acceptance: DOM tests for 0, 1, 2 and 3 checked rows (button disabled, disabled, enabled, disabled); a failed-verification row among good rows; an 80-character label; no free-text filter inputs. +7 tests. Rollback: revert.

#### C6. On-demand pair comparison

Change (D6, revised by the eng review): `GET /api/v1/records/compare?a={record_id}&b={record_id}` returns a new non-persisted `LocalPairView` built from two `LocalRecordView`s (C1). It does not use E07: `FragmentExplorerView` accepts only v1 measurements, hard-codes synthetic definition IDs and `unit_bp`, needs one registry and authority head for both sides, and the local compatibility policy forbids deltas (`fragment_explorer.py:85-89, 279, 321-322, 332`; `compatibility.py:852`; `local_catalog.py:355`). `LocalPairView`: `a`, `b`, `same_settings: bool` (equal reference, MAPQ and bin edges), `differences: [{field, a, b}]`, and, only when `same_settings`, `rows: [{lower, upper, share_a, share_b, delta_pp}]`. Display per C0 and taste T4: two charts; when `same_settings`, overlaid outlines (A solid, B dashed) and the A/B/B−A table under the heading "Same analysis settings; differences are descriptive"; otherwise separate charts and a banner "Different analysis settings: <differences>. Values are shown side by side and not subtracted." A = earlier import, with a Swap control. The existing `/api/v1/explorer/compare` and E07 are unchanged.

Acceptance:
1. Two built-in records: `same_settings: true`, `delta_pp` rows sum to 0.0 ± 0.1.
2. Built-in vs research record: `same_settings: false`, no `rows`, `differences` lists MAPQ and/or bins.
3. Same ID twice: 400. One side unknown: 404 naming that side.
4. The existing `/api/v1/explorer/compare` tests pass unchanged.

Tests: +6. Rollback: revert; nothing stored.

#### C7. Styled printable report; strip "Validated"

Change (D7): `GET /records/{record_id}/report` returns an HTML page with header, the two strip values, histogram, strip and the "How to read it" text, styled by a packaged `static/report.css` (the server's CSP is `style-src 'self'` with no inline styles, `server.py:88-90`; SVG uses classes, not `style` attributes), plus a print stylesheet. No scripts. Its header says "Formatted view of signed record <id>. The signed report.html in the record is the record of truth", linking to it, and "Inspection aid, not a shareable scientific report". The signed `report.html` is unchanged. `app.py:473` becomes "Recorded AI assessment replay; no provider call" and `app.py:1382` becomes "Measurements".

Acceptance: the page has no `<script>` and no `style=` attribute; it renders styled under the production CSP (DOM or headless check); it contains "not for clinical use"; `grep -n "Validated" app.py` returns only line 490's "Not built" disclaimer. +3 tests. Rollback: revert.

### Track D: code health

#### D1. Crash-safe provenance HMAC key
Replace `_provenance_hmac_key` (`cli.py:1063-1115`) with the `_local_signing_key` pattern (`cli.py:1116`): write to a temp file, fsync, `link` into place; accept modes with no group/other bits (0600 or 0400; today's check is `& 0o077`, `cli.py:1098`, and must not tighten to `== 0o600`). The key is first created inside the sign stage (`cli.py:1365`), after the job row exists, so a 0-byte or short key is replaced when `ROOT/records/` is empty or absent; otherwise TBX-RUN-006 with FIX text naming the file. Acceptance: a 0-byte key with no records recovers on the next run; with a record it refuses; an existing 0400 key is accepted (+3). Rollback: revert.

#### D2. Delete `_stage_heartbeat`
Remove `cli.py:1199-1218` and its two uses. Acceptance: a test runs a local stage lasting 3x the lease length on a FakeClock and the job completes through `_LeaseKeeper` alone (+1). Rollback: revert.

#### D3. Filesystem write primitives (three, not one)
`traceback_runner/filesystem.py` gains three primitives with different crash semantics, because the six helpers today are not interchangeable: `create_only(dir_fd, name, bytes)` (descriptor-relative exclusive create; refuses any existing file; reference registration, `references.py:304`, keys), `replace_derived(path, bytes)` (atomic replace of derived artifacts; today's `_persist_once`, `local_catalog.py:436`), and `publish_directory(staging, target)` (fsynced staging plus no-replace rename; `local_authority.py:391`). Plus `read_private_bounded` and `fsync_dir`. Each call site moves to the primitive matching its current semantics; none changes semantics. Acceptance: crash-point tests per primitive (kill after create, after write, before rename); existing crash-recovery tests pass unchanged; `grep -c "def _fsync_directory"` across the package = 0 (+9). Rollback: revert.

#### D4. Code grammar and exit/retryable consistency
D15 renames, with a deprecation window: for one release the problem payload carries `code` (new) and `legacy_code` (old) and the guide row lists both. `ReferenceProblem.exit_code` becomes an `ExitCode`; TBX-JOB-001 exits one value everywhere (3); retryable problems exit 6 and non-retryable ones never do; `_reject_live_worker` uses `>` like the store. A test walks `PROBLEM_TABLE` and asserts grammar, exit and retryable agree. Problem base class moves to `traceback_runner/problems.py` as `OperatorProblem`; `ReferenceProblem` and `LocalStageRefusal` subclass it. (+4 tests.) Rollback: revert.

#### D5. v2 envelope for every local command
`doctor`, `inspect`, `demo` and `assets` emit `traceback.cli-result.v2` with `data_origin` (`synthetic` or `local`); `_concerns_local_data` (`cli.py:2941`) closes its `JobStore`; `serve --json` prints its startup line as JSON. Acceptance: on a real-BAM ROOT, `doctor --json` never says `synthetic_only: true` (+4). Rollback: revert.

#### D6. Authority rigidity documented
`docs/OPERATOR-GUIDE.md` and `TBX-AUTHORITY-001`'s FIX say: changing built-in constants makes existing ROOTs refuse; use `traceback policy add` instead. No code change to the equality rule. (+0; the guide test covers the text.)

#### D7. Test globals and flake candidates
`tests/test_awake.py` already patches only `_assertion_available` (fixed by `d4050ad`, in `e76e9d0`); nothing to do there. `tests/test_doctor.py:23-29` drops the autouse fixture for explicit per-test patches; `tests/web/test_loopback_server.py:843` stale-anchor test uses a per-test `tmp_path` anchor directory; `tests/test_internal_errors.py:193-203` joins the watchdog before teardown; `tests/test_canary.py` stubs `launchctl`; every `subprocess.run` in tests has a timeout. Acceptance: `pytest -n 8` of those files 20 times in a loop, 0 failures (recorded in the PR). Rollback: revert.

#### D8. End-to-end lease-loss test
`tests/test_run_local.py`: the CLI's real local stages on the generated BAM, a FakeClock that expires the lease mid-measure, then `traceback resume JOB_ID` produces a verified record identical to an uninterrupted run. Also a TBX-SERVE-004 watchdog-exit test. (+2.) Rollback: revert.

#### D9. Doc drift
Fix the `code.md` doc-drift list: `OPERATOR-GUIDE.md` v2 claim, missing `--index`, catalog DB path; add guide rows for TBX-AUTH-002..006, TBX-WEB-*, TBX-INTERNAL-001 and every new code here; create `docs/operator/privacy.md` (TBX-OUT-001 target) or retarget the code; strip the stale `cli.py:NNN` references from `GOLDEN-PATH-MVP-SLICE.md` and mark `PILOT-SECURITY-HARDENING.md` items landed/not landed. Acceptance: A3's code-coverage test passes; a link checker over `docs/` reports 0 broken intra-repo anchors.

#### D10. Split `cli.py` (move only)
`traceback_runner/cli/` package: `__init__.py` (parser + dispatch + `main`, re-exporting every name tests import today), `run.py` (run, resume, local stages, publish), `jobs.py` (status, logs, pause, retry, jobs), `catalog.py`, `serve.py`, `doctor.py`, `reference.py`, `verify.py`, `assets.py`, `output.py` (`_result`, `_emit`, envelopes). `python -m traceback_runner.cli` and the `traceback` entry point keep working. `tests/test_operator_guide.py:30` (`PROBLEM_MODULES` lists `traceback_runner/cli.py` by path) globs `traceback_runner/cli/*.py`. Tests that monkeypatch `cli._local_stages` and similar (`tests/test_run_local.py:339`) patch the defining module, or the patch silently stops working. The installed 03:30 canary runs from the checkout (`docs/CANARIES.md:99-100`): the PR unloads it (`scripts/canary/install_canary.sh uninstall`), runs the exact launchd command once against the branch, and reloads it after merge.

Acceptance: the full suite passes with only import-path edits in tests; a test asserts each monkeypatched name is looked up from the module that calls it; `git diff --color-moved` shows no logic change; the canary command passes on the branch (recorded in the PR). Rollback: revert (one commit).

## Lanes and file overlap

| Wave | Lane | Items | Main files |
|---|---|---|---|
| 1 | W1-cli (serial) | B1, D1, D2, A2, A3, A4a, A4b, A4c | `cli.py`, `problems.py` (new), `local_catalog.py` (A4a import by ID), `web/server.py` (A4a reload) |
| 1 | W1-intake | A0, A1, A5, A6 | `preflight.py`, `fixtures.py`, guide |
| 1 | W1-site | C0, C1, C3, C5 | `web/records.py`, `web/state_copy.py`, `web/static/*`, `web/server.py` (routes) |
| 2 | first | D10 | `cli.py` → `cli/` |
| 2 | W2-run | A7, A8, A9 | `cli/jobs.py`, `cli/run.py`, `runner.py`, `snapshots.py` |
| 2 | W2-analysis | B2a, B2b, B3, B4, D6 | `policies.py`, `measurement.py`, `local_authority.py`, `local_catalog.py`, `cli/run.py` (B2b only), `scripts/canary/` |
| 2 | W2-site | C2, C4, C6, C7 | `web/records.py`, `web/static/*`, `web/server.py`, `app.py` |
| 2 | W2-hygiene | D3, D4, D5, D7, D8, D9 | `filesystem.py`, `problems.py`, tests, docs |

Collisions: A4a and W1-site both touch `web/server.py` (A4a's reload is in the explorer source, C1's routes in the route table; land C1 first, A4a rebases). B2b and W2-run both edit `cli/run.py`. D4 extends A3's `problems.py`. D3 touches files in every lane: land it last in wave 2 or accept mechanical rebases.

## Dependency graph

```
WAVE 1
A0 ─┬─> A1 ─> A6
    └─> A5
B1 ─> A2 ─> A3 ─> A4a ─┬─> A4b ──────────────┐
D1, D2 (any time,      └─> A4c               │
        same lane)                           v
C0 ─> C1 ─┬─> C3                             C5
          └──────────────────────────────────┘
DoD-1 after all of the above

WAVE 2
D10 ─┬─> A7, A8, A9
     ├─> B2a ─┬─> B2b ─> B3 (needs C1), D6
     │        └─> B4
     └─> D4 ─> D5
C1 ─> C2, C4 ─> C7 ;  C5 ─> C6
D3 late; D7, D8 any time; D9 last; DoD-2 after all
```

Sequencing: B1 first because it silently returns wrong records. A0 before A1/A5/A6 because the zip decides their priority. Wave 1's `cli.py` items form one serial chain, so the split (D10) waits until wave 2, where four lanes run in parallel. L-intake and L-site do not touch `cli.py` and start on day 1.

## Definition of done

`scripts/usability_acceptance.sh` (new; CI runs it on macOS and ubuntu with generated fixtures; the operator runs it once on real data). Wave 1 (DoD-1) = steps 1-3, 5 and 7-9; wave 2 (DoD-2) adds steps 4 and 6.
1. From a fresh ROOT: `reference register`; `preflight` on an unaligned fixture BAM exits 3 with TBX-BAM-003 and a `minimap2` command.
2. Three aligned fixture BAMs with different read sets (so their job keys differ) run with `--label` and `--import`; `traceback catalog list` shows 3 labelled rows.
3. A run with a missing `.bai` exits 4 with TBX-RUN-009 and creates no job.
4. `policy add --preset one-bp --id fine`; `run --policy fine --import` on one BAM produces a fourth, distinct record labelled "research policy fine".
5. `serve` (started before step 2's imports is fine); an operator-session GET of `/api/v1/records/{record_id}` returns histogram counts that sum to `eligible_alignments` for each record.
6. GET `/api/v1/records/compare` on two built-in records: `same_settings: true`; on built-in vs `fine`: `false` with `differences`.
7. A fourth fixture BAM whose reads all have MAPQ 0 runs and fails with TBX-RUN-005, printing its JOB_ID.
8. `traceback jobs` lists every job with the failed one showing TBX-RUN-005; `traceback status <failed JOB_ID>` prints its cause and fix.
9. No `--json` output from steps 1-8 contains a label or an absolute path (grep).

Manual evidence (in the DoD PRs, as hand-written tables, no paths or identifiers):
- The operator processes the real zip unaided from the guide and logs every intervention (where they had to read code, ask, or guess). Target for DoD-1: zero code reading.
- Alignment wall time per GB, `run` times, eligible counts, screenshots at 1280 and 390 px.
- The histogram of one real BAM cross-checked against an independent count (for example `samtools view -F 0xF04 -q 20` piped to an awk span count over the same contigs): bin counts equal.

## Out of scope (follow-ups with triggers)

| Item | Trigger |
|---|---|
| Cell-origin as a second analysis family (bundle kind v4, modkit + Loyfer assets, E08 at import) | Research mode used on at least 3 BAMs and a scientist asks for methylation output |
| `run --batch DIR` / `traceback intake` over a zip | A0 shows more than 10 BAMs, or the guide loop is used twice in one week |
| `traceback align` wrapping a pinned, administrator-issued alignment release | Premise P5 answered "alignment must be in the product", or a second batch needs alignment |
| N-record overlay view | A0 and the batch-2 question show more than 3 records per comparison |
| De-duplicating preflight inside `run` (21 s per run) | Batches above 10 BAMs, or preflight above 60 s on one BAM |
| Retiring the Streamlit app | C1-C7 merged and the operator site used for one external walkthrough |
| Migration of built-in constants across ROOTs | A change to the built-in policy is required (not a research policy) |
| Folding the ~10 BAM re-hashes per run into one verify | `run` above 5 min on one BAM |
| Signed labels | A record with a label is shared outside the team |
| Canary pinned to an installed release instead of the checkout | D10 or B1 merged, or a second Mac installs the canary |
| Multi-workstation install, upgrade and reference distribution | A second operator runs `traceback` on a second Mac |
| `TRACEBACK_ROOT` environment variable | Taste T1 |

## Rollback

Code reverts cleanly per PR; data written by newer code does not always read on older code:
- B1: new-key job rows are recognised by old code only if the old `_is_local_request` is kept; reverting B1 makes rows written after it read as non-local in `status` (cosmetic; resume refuses). Records are unaffected.
- B2a/B2b: one-way door once a research record is imported (catalog rows bind the research authority context). Reverting on such a ROOT is unsupported; keep `ROOT/research-authority/` and use a fresh ROOT for the old code. Built-in-only ROOTs revert cleanly. Never delete `research-authority/` to roll back: it makes research records unverifiable.
- A4b: `ROOT/labels/` is ignored by old code.
- A8: deleted failed-run copies are re-creatable by re-running.
- D15/D4: old code does not know the new codes; rows written with them show `code` without CAUSE/FIX.
No item changes a signed byte of a built-in record.

## Files reference

| File | Items |
|---|---|
| `traceback_runner/cli.py` → `traceback_runner/cli/*.py` | B1, D1, D2, D10, A2, A3, A4a, A4b, A7, A8, A9, B2a, B2b, D4, D5 |
| `traceback_runner/preflight.py:85-90, 383` | A1, A5, A6 |
| `traceback_runner/fixtures.py` | A1, A5, A6, DoD |
| `traceback_runner/problems.py` (new) | A3, D4 |
| `traceback_runner/runner.py:506-509, 667-696`, `traceback_runner/snapshots.py:108-109` | A8, A1 (run path) |
| `traceback_runner/measurement.py:130-137` | B2a (`bisect`) |
| `evidence_inspector/result_catalog.py:3604` (read path reused) | C1 |
| `DESIGN.md` (Privacy: labels in the operator web API) | A4b |
| `scripts/canary/install_canary.sh` (unload/reload around D10) | D10 |
| `traceback_runner/web/static/report.css` (new) | C7 |
| `traceback_runner/store.py:449-466` (read only) | B1 |
| `traceback_runner/policies.py` (new) | B2a, B2b, B3 |
| `traceback_runner/local_authority.py:84-85, 159-186, 428-525` | B2b, D6 |
| `traceback_runner/local_catalog.py:126, 220, 245-405, 676-720` | A4a, B2b |
| `traceback_runner/export.py:228-270` | B2b (research label only) |
| `traceback_runner/web/records.py`, `web/state_copy.py` (new) | C1, C3, B3 |
| `traceback_runner/web/server.py` (routes, `server.py:634-675, 1296+`; explorer reload) | C1, C6, C7, A4a |
| `traceback_runner/web/static/app.js`, `chart.js` (new), `index.html`, `styles.css`, `longitudinal.js:256-387` | C1-C7 |
| `app.py:473, 1382` | C7 |
| `traceback_runner/filesystem.py` | D3 |
| `scripts/canary/real_bam_canary.py`, `scripts/regenerate_canary_baseline.py` (new) | B2b, B4 |
| `scripts/usability_acceptance.sh` (new), `tests/test_usability_acceptance.py` (new) | DoD |
| `docs/OPERATOR-GUIDE.md`, `docs/ALGORITHMS.md`, `docs/operator/privacy.md` | A1, A4b, A9, B4, D6, D9 |
| tests: `test_run_local.py`, `test_preflight*.py`, `test_awake.py`, `test_doctor.py`, `web/test_loopback_server.py`, `web/test_internal_errors.py`, `test_canary.py`, `web/app_dom_harness.js`, new `test_policies.py`, `test_policy_docs.py`, `web/test_records.py` | all |

## Testing summary

| Layer | What | Count |
|---|---|---|
| Unit | problem table, policy grammar, label grammar, state copy coverage, derived metrics, key derivation | about +40 |
| Integration | CLI on generated BAMs (unaligned, empty, renamed contigs, Dorado header), research run/import/serve, record and compare routes, lease loss | about +45 |
| DOM | histogram SVG, renderers, catalog table, compare states | about +15 |
| E2E | `usability_acceptance.sh` in CI on macOS and ubuntu | +1 |
| Manual | real-data DoD table and screenshots | 1 |

## Effort

Human: about 47 days (A: 13.5, B: 7.75, C: 13, D: 11.5 incl. D10, DoD 1.5). Wave 1: about 19 days, about 2 calendar weeks with three lanes. Wave 2: about 28 days, about 3 calendar weeks with four lanes. Review on this repo has roughly doubled build time, so 5-6 calendar weeks for both waves is the realistic range (CEO review). CC: about 32 h of build time plus review rounds.

---

# /autoplan review (2026-10-03, commit fd12d1f)

Run autonomously. Intermediate questions were auto-decided with the six autoplan principles. Premises and User Challenges are NOT decided; they are listed under "Pending user gates". UI scope: yes (Track C; histogram, table, layout, button, compare view). DX scope: yes (CLI commands, flags, error messages, operator guide). Phases run: CEO, Design, Eng, DX.

Process notes (deviations, recorded in the audit trail): the four Claude subagent voices were launched in parallel at the start, because each is required to have no prior-phase context, so ordering does not change their input; the Codex voices ran in phase order, each with the prior phases' findings. Codex prompts went in on stdin from a file (`codex exec -s read-only - < prompt`), so `< /dev/null` was not also possible. Design mockups (plan-design-review Step 0.5) were not generated: autonomous run, no design binary session. Every phase's AskUserQuestion was auto-decided except the premise gate and User Challenges.

Date note (raised by both CEO voices): the evidence directory is named `review2-2026-10-04` because its reviews were written after 00:00 UTC on 2026-10-04, which is the evening of 2026-10-03 Pacific. The spec date (2026-10-03) is local time. "Verified against `e76e9d0`" means the code; `fd12d1f` adds only this document.

## Phase 1: CEO review (mode: SELECTIVE EXPANSION)

### System audit

- `main` at `e76e9d0`. The last 30 commits are the golden path landing: B5a/B5b catalog import (#102), `traceback serve` and the operator guide (#105), lease and key fixes (#100, #104). Hottest files in 30 days: `evidence_inspector/result_catalog.py` (32 commits), `traceback_runner/cli.py` (28), `web/server.py` (21).
- 3 stale stashes on old D05 branches; unrelated.
- `TODOS.md`: "longitudinal comparison only after repeatability evidence and paid demand"; "remove reassurance language before reuse"; "create DESIGN.md and populated mockups". The first bears directly on C6.
- `docs/PRODUCT-SPEC.md:16-28`: the first release succeeds when a non-bioinformatician operator obtains a record "without composing shell commands". A1 prints a shell pipeline for alignment. `PRODUCT-SPEC.md:38-48`: "The customer does not configure the pipeline"; workflow releases are administrator-owned. B2 lets the operator create policies.
- `docs/EPICS.md:350-371`: E12 longitudinal comparison depends on repeatability evidence; MVP deferred. C6 adds a delta view for local pairs.
- `docs/PRODUCT-PLAN.md:55-73`: demand gate (20 interviews, "show the actual research-only output and its limitations"). Track C is the first thing that makes that output showable.
- `docs/CANARIES.md:99-100`: the canary "runs from the repository checkout, so it tests whatever is checked out there at 03:30". D10 and B1 change that checkout.
- `DESIGN.md` (architecture only, no visual tokens) forbids "filenames, sample labels" in JSON diagnostics (Privacy section). A4a/A4b put labels in `--json` and the web API.
- Code-claim drift: `tests/test_awake.py` no longer patches `sys.platform` (fixed by `d4050ad`, already in `e76e9d0`). `code.md`'s item for it is stale, so D7 shrinks.

### 0A. Premise challenge

| # | Premise | Stated or assumed | Assessment |
|---|---|---|---|
| P1 | The pending zip holds unaligned MinKNOW/Dorado BAMs like the 28 `bam_pass` files reviewed | Assumed | Nobody has opened it. It may hold barcoded subdirectories (one merge would mix samples), POD5, FASTQ or BAMs aligned to another reference. A1/A5/A6 priorities depend on it |
| P2 | "Change the analysis" means changing MAPQ and bin edges | Assumed (from `algo.md`) | Could mean end motifs, methylation or cell-origin. Bins alone can be handled by measuring once at 1 bp and binning at view time |
| P3 | Two local records under one exact method are comparable enough to show a delta | Assumed (D18) | Method identity does not establish comparability across samples, kits or pre-analytics; `EPICS.md` E12 waits for repeatability evidence |
| P4 | The site is for Dan now, and later for outside readers in demand interviews | Stated (Dan only reader) | Copy and report polish (C3, C7) serve outside readers who do not exist yet; they do serve PRODUCT-PLAN's demand interviews once P7 is settled |
| P5 | Printing an alignment command is acceptable for this slice | Stated (D10) | Conflicts with PRODUCT-SPEC's "without composing shell commands"; acceptable only if labelled as an assisted prerequisite |
| P6 | Three calendar weeks with four lanes | Stated | Review roughly doubles build time on this repo; 5-6 weeks is realistic for all 32 items |
| P7 | The new BAMs' provenance and consent are known before any record from them is shown outside the team | Assumed | Carried from the golden-path gate P1; labels make this sharper (donor names in labels) |
| P8 | Team macOS workstations run from a repo checkout with `uv run` | Assumed | No install, upgrade or ROOT-discovery story; the canary inherits whatever is checked out |

### 0B. Existing code leverage

| Sub-problem | Existing code | Plan reuses? |
|---|---|---|
| Unaligned detection | pysam header read in `preflight.py` | yes (A1 adds a pre-check) |
| Failure reason | `store.py:388` `last_error`; `_PROBLEM_CODE` (`cli.py:1035`) | yes (A3, no migration) |
| Record reuse on identical manifest | `_publish_verified_record` (`cli.py:885-933`) | yes (B1) |
| Policy shape | `FragmentMeasurementPolicyV2` (`contracts.py:237`) | yes (B2a) |
| Authority store | `ensure_local_method_authority`, `_create_store` (`local_authority.py:530`) | yes, parameterised (B2b) |
| Pair compare | `build_fragment_explorer_view` (`fragment_explorer.py:806`), `/api/v1/explorer/compare` (`explorer.py:617-720`) | yes (C6, widened to V3) |
| Chart helper | `svg()` in `longitudinal.js:256-387` | yes (C1 moves it) |
| Stat tiles, "how to read" copy | `app.py` Streamlit presentation | pattern only (C3/C4) |
| Write-once helper | `_local_signing_key` pattern (`cli.py:1116`) | yes (D1, D3) |
| Stale-status rule | `DESIGN.md` "Runner status is stale" after 3 missed 15 s observations | A9 must apply it to live jobs only |

### 0C. Dream state

```
CURRENT (e76e9d0)                 THIS PLAN                           12-MONTH IDEAL
one real BAM, golden path   --->  any MinKNOW batch runs with   --->  operator selects a MinKNOW run dir;
works; unaligned BAMs fail;       labels; research analysis           pinned workflow aligns, measures,
records unlabelled, not           variants without bricking ROOTs;    signs; records browsable with
comparable; site shows            site shows histogram, plain         plain-language status; comparisons
tokens and JSON                   states, side-by-side compare        only where repeatability evidence
                                                                      exists; outside readers in demand
                                                                      interviews see one honest page
```

The plan moves toward the ideal on intake visibility, labels and the site. It moves sideways on analysis change (operator-created policies are not the administrator-owned releases the product spec describes) and ahead of the ideal on comparison deltas.

### 0C-bis. Implementation alternatives

```
APPROACH A: Full epic as written (32 items, 4 tracks)
  Effort: L (about 42 human days; CC about 28 h plus review)   Risk: Med
  Pros: fixes every reviewed defect; code health under the new work; one plan
  Cons: 5-6 calendar weeks before batch-2 results are fully viewable; builds a
        policy authority system before knowing what "change the analysis" means
  Reuses: everything in 0B

APPROACH B: Batch-learning milestone first (minimal viable)
  Summary: day-0 zip triage; B1; A1/A2/A3/A4a/A4b/A5/A6; C1 + C3-lite + C5-lite
           as one read-only page per record; run the real zip and log every
           intervention. Then decide B2/C6/D on what the batch shows.
  Effort: M (about 12-14 human days)   Risk: Low
  Pros: batch-2 results viewable in about 2 weeks; requirements come from the
        real zip; fewest one-way doors (no policy stores yet)
  Cons: Track D debt persists; a second planning pass; analysis change waits
  Reuses: same as A for its items

APPROACH C: Measure once at 1-bp (ideal architecture for "change the bins")
  Summary: a new built-in method version stores 1-bp counts (0-999 plus 1000+,
           optionally per MAPQ bucket); bin edges become a view parameter; no
           per-policy authority or job-key churn for bin changes.
  Effort: M   Risk: Med (new built-in version, new signed bytes once; MAPQ
        changes still need a re-measure)
  Pros: removes most of B2 for the common change; comparison of differently
        binned views becomes a display choice
  Cons: one migration of the built-in method (old ROOTs keep the old version);
        does not cover MAPQ or new analysis families
  Reuses: bin contract, explorer 4,096-bin limit
```

RECOMMENDATION: B, then C if P2 confirms bins are the change, else B2 as written. Completeness of the full outcome is unchanged; the order puts real data first (P1, P6). Both CEO voices independently proposed B and C, so this is raised as User Challenges UC1 and UC2 rather than decided here; the spec body stays as written (approach A) until Dan answers.

### 0D. Selective expansion analysis

Complexity check: the plan touches more than 40 files and adds 6 new modules (`problems.py`, `policies.py`, `web/records.py`, `web/state_copy.py`, `static/chart.js`, `cli/` package). That is a smell, mitigated by the item split; the minimum set that achieves "run more BAMs and see them" is approach B.

Expansion candidates (each auto-decided, P2/P3):

| # | Candidate | Effort | Decision | Why |
|---|---|---|---|---|
| X1 | Day-0 triage of the pending zip (headers, `@SQ`, `@RG DS`, MM/ML, barcodes, sizes, disk forecast), written up as an item "A0" | S | ACCEPTED (added as A0) | In blast radius, under 1 d, unblocks A1/A5/A6 priorities |
| X2 | `TRACEBACK_ROOT` env var | S | TASTE T1 | The golden-path review chose no env var (T6 there); DX voice now asks for it |
| X3 | `run --import` (import after publish) | S | ACCEPTED into A4a | Removes one step per BAM; reuses `import_local_record` |
| X4 | CSV export of per-record counts (`traceback catalog export --csv`) | S | ACCEPTED as A4c | Fastest route to a notebook; aggregates only, no paths |
| X5 | N-record overlay instead of pairwise compare | M | DEFERRED | Decide after A0 shows how many samples and what question |
| X6 | `traceback align` wrapping a pinned minimap2 preset | M | DEFERRED (trigger: second batch needs alignment) | P5 decides whether alignment becomes a product step |
| X7 | Cross-check the histogram against an external fragmentomics tool on the real BAM | S | ACCEPTED into DoD manual evidence | Cheap correctness check; no code |
| X8 | Pin the canary to an installed release, not the checkout | M | DEFERRED (trigger: D10 or B1 lands) | Outside this plan's radius; recorded |

### 0E. Temporal interrogation

```
HOUR 1 (foundations): which ID does each surface use (job, record, result)?
                      where do labels live and who may see them in JSON?
HOUR 2-3 (core):      does a re-run after B1 publish into the same record dir?
                      does the research authority need its own serve check?
HOUR 4-5 (integration): E07 V2|V3 union: do any persisted fragment views or
                      fixtures pin the V2-only schema? does serve need a
                      restart to see new imports?
HOUR 6+ (polish):     1-bp policies vs bar labels and tiles; 390 px chart;
                      the canary after the cli.py move
```

Resolved in the spec now: the ID question (D20), label JSON exposure (D8, A4a/A4b), the B1 second-record behaviour (B1), the 1-bp chart rule (C1). Left open: none of these.

### 0F. Mode

SELECTIVE EXPANSION (iteration on an existing system; autoplan override).

### Dual voices (CEO)

CLAUDE SUBAGENT (CEO, strategic independence), summarised:
- HIGH: the plan optimises the tool, not the decision; about 11 days of Track D hygiene sit off the path to batch-2 results. Cut to a 12-14 day "zip to results" slice.
- HIGH: no stated scientific question for batch 2 (replicates, donors, timepoints?). It decides pairwise vs N-record views.
- HIGH (10x): measure once at 1 bp, or a MAPQ x length integer grid; bins become a display parameter and most of Track B goes away.
- Premises: zip contents unknown (critical); minimap2 command untested on real data; serial loop sufficiency; disk headroom; "only reader" vs polish; 3 weeks unrealistic; canary affected by B1/D10; date chronology.
- Regret: a pile of write-once policy IDs; two UIs; `periodicity_10bp` read as signal on unvalidated data.
- Verdicts: premises partly, right problem partly, scope no, alternatives no, competitive no, 6-month partly.

CODEX SAYS (CEO, strategy challenge), summarised:
- The plan precedes evidence: specified before opening the zip. Process the batch manually first.
- Printing a shell pipeline dodges PRODUCT-SPEC's "without composing shell commands"; automate or classify alignment as an assisted prerequisite.
- Operator-created policies conflict with the product model (administrator-owned releases); start with one named preset tied to a question.
- Comparison is scientifically under-gated; E12 waits for repeatability evidence.
- Derived metrics are claims through a side door; no owner, benchmark or threshold.
- DoD proves construction, not usability; test that Dan can ingest the real zip unaided and record interventions.
- Multi-workstation operation (install, upgrade, reference distribution, ROOT discovery) is unaddressed. The canary tests whatever is checked out.
- Rollback that deletes research authority destroys verifiability of outputs already produced.
- Better cut: one batch-learning milestone.
- Verdicts: premises partly, right problem partly, scope no, alternatives no, competitive no, 6-month no.

```
CEO DUAL VOICES — CONSENSUS TABLE:
═══════════════════════════════════════════════════════════════
  Dimension                           Claude  Codex   Consensus
  ──────────────────────────────────── ─────── ─────── ─────────
  1. Premises valid?                   partly  partly  CONFIRMED (partly)
  2. Right problem to solve?           partly  partly  CONFIRMED (partly)
  3. Scope calibration correct?        no      no      CONFIRMED (no) -> UC1
  4. Alternatives sufficiently explored? no    no      CONFIRMED (no) -> UC2
  5. Competitive/market risks covered? no      no      CONFIRMED (no)
  6. 6-month trajectory sound?         partly  no      DISAGREE (degree) -> T2
═══════════════════════════════════════════════════════════════
```

### Review sections 1-11 (CEO)

**1. Architecture.** Examined the new components against `cli.py`, `local_authority.py`, `local_catalog.py`, `fragment_explorer.py`, `web/explorer.py`. Findings: (a) research authority in a sibling directory (D3) is the right call because `validate_local_method_authorities` refuses unknown names (`local_authority.py:520-523`); (b) `LocalRecordView` per request (D5) avoids a digested contract but makes `serve` re-verify bundles on every page view; fine at 1-100 records (Eng section 4); (c) C6's V2|V3 union touches an evidence_inspector contract; Eng owns it. Diagram:

```
CLI (cli/ package after D10)
 ├─ run ──> Runner/JobStore (key = workflow sha(method) ── B1)
 │           └─ publish ─> ROOT/records/<record_id>   (signed, unchanged)
 │                          └─ labels: ROOT/labels/<record_id>.json (unsigned)
 ├─ policy add/show ─> ROOT/policies/<id>/            (B2a)
 ├─ run --policy ─> ROOT/research-authority/<ref>/<id>/ (B2b)
 ├─ catalog import/list ─> catalog DB + ROOT/explorer/ (unchanged artifacts)
 └─ serve ─> web/server.py
              ├─ /api/v1/explorer/*        (existing)
              ├─ /api/v1/records/{id}      (C1, LocalRecordView, re-verifies)
              ├─ /api/v1/records/compare   (C6, E07 built per request)
              └─ /records/{id}/report      (C7, no scripts)
```
Auto-decided: keep the architecture; add an ID rule (D20).

**2. Error and rescue map.**

```
CODEPATH                    | WHAT CAN GO WRONG               | CODE / CLASS
----------------------------|---------------------------------|---------------------------
preflight header read       | no @SQ (unaligned)              | TBX-BAM-003 (new)
                            | 0 records                       | TBX-BAM-004 (new)
                            | truncated / bad BGZF            | TBX-BAM-001 (OSError/ValueError)
                            | other exception                 | exit 7 (was swallowed) 
run input stat              | BAM missing / index missing     | TBX-RUN-008 / -009 (exit 4)
                            | not BGZF                        | TBX-RUN-010
run submit                  | job held by live worker         | TBX-JOB-002 (was uncoded)
policy add                  | ID reused, other settings       | TBX-POL-001
run --policy                | policy/reference mismatch       | TBX-POL-002
serve research authority    | damaged store                   | TBX-AUTHORITY-002
/api/v1/records/{id}        | unknown id / tampered bundle    | TBX-WEB-404 / TBX-WEB-503
compare                     | same id twice                   | 400
failed-run cleanup          | unlink fails                    | input_remove_failed (log) ← GAP: status must show it
retry/pause                 | complete / terminal job         | TBX-JOB-003 / -004
status after D15            | legacy TBX-AUTH-LOCAL in row    | ← GAP: alias rows needed
```
GAPs auto-fixed in the spec (P1): `status` surfaces `input_remove_failed`; `PROBLEM_TABLE` carries alias rows for `TBX-AUTH-LOCAL-001/002` and `TBX-INTERNAL`. TBX-JOB-004's FIX and the research TBX-AUTHORITY-002 FIX were wrong (would send the operator to a fresh ROOT or to delete the built-in authority); corrected (DX finding).

**3. Security and threat model.** New surface: 3 HTTP routes (operator session only, same auth as explorer routes), operator labels (free text), policy files (closed flags). Threats: label text reaching JSON diagnostics against `DESIGN.md` privacy (Med likelihood, Med impact) -> auto-decided: labels appear in human output and the operator-session web view only; never in `--json`, logs, support bundles or exports (D8). Policy files: closed grammar, write-once, 0600; low risk. Report route: no scripts, escaped text; low risk. Reader sessions: the new routes are operator-only; reader sessions get 403 like other explorer routes (Design D-F10).

**4. Data flow and interaction edge cases.** Traced run -> label -> import -> view. Edge cases added: same BAM re-run with a different `--label` relabels the reused record and the reuse line says so (DX); a re-run after the B1 key change makes a second record, marked "same measurement as" (B1); a catalog with 0 records shows the next CLI command; a record that fails verification among good rows shows that row as "failed verification" and keeps the rest (Design).

**5. Code quality.** DRY: `PROBLEM_TABLE` becomes the single CAUSE/FIX source (A3, D4); the authority store is parameterised, not copied (B2b). Naming: `jobs` vs `catalog list` vs `policy list` inconsistency -> taste T3. Over-engineering: `state_copy.py` plus `problems.py` are two tables of copy; acceptable (different audiences).

**6. Test review.** See Phase 3 Section 3 for the full diagram. CEO-level gap: the DoD proves construction, not use. Auto-decided: add a manual "real zip, unaided" DoD step recording every intervention (Codex, P1).

**7. Performance.** `serve` re-verifies a bundle per record view and two per compare; a bundle is a few KB of JSON plus signature; under 50 ms each (estimate; Eng measures). A 1-bp policy makes 1,001-row tables: render collapsed (Design).

**8. Observability.** New: `traceback jobs` with failure codes; `input_removed` log events; reuse line. Gap: no record of which policy produced which record in `jobs` output -> added (policy column already present). Canary observability is unchanged.

**9. Deployment and rollout.** No server deploy; "deploy" is a `git pull` on each team Mac. Risks: B1 re-measures each previously run BAM once (one copy of disk each); D10 moves modules the 03:30 canary imports. Auto-decided: B1 and D10 acceptance include "the canary passes against the moved code" (Claude CEO P7). Rollback with research records: imported research rows bind the research authority context in the catalog (Codex eng #6), so reverting B2b on such a ROOT is a one-way door; Rollback now says so and says never to delete `research-authority/` (Codex CEO: deleting it destroys verifiability).

**10. Long-term trajectory.** Reversibility 3/5 (policy stores and labels are new on-disk state; B1 keys are additive). Debt: operator-created policies are a parallel method-release path the product spec reserves for administrators (UC2). Six-month risk: the derived-metric tile read as signal (UC3).

**11. Design and UX.** Information hierarchy and states are underspecified for C5-C7; Phase 2 covers them.

### Error & Rescue Registry

| Method / codepath | Failure | Rescued | Rescue action | User sees |
|---|---|---|---|---|
| `validate_bam_snapshot` header | no `@SQ` | Y | TBX-BAM-003 + align command | BLOCKED with command |
| `validate_bam_snapshot` scan | `OSError`/`ValueError` | Y | TBX-BAM-001 | "unreadable or truncated" |
| `validate_bam_snapshot` scan | other exception | N (by design) | exit 7, TBX-INTERNAL-001 | internal error with support-bundle hint |
| `_local_input_files` | missing BAM / index | Y | TBX-RUN-008 / -009 | CODE/CAUSE/FIX, no job |
| `_reject_live_worker` | lease active | Y | TBX-JOB-002 + JOB_ID | BLOCKED |
| `add_policy` | ID reuse | Y | TBX-POL-001 | BLOCKED |
| research authority open | damaged | Y | TBX-AUTHORITY-002 (research FIX) | BLOCKED naming the policy |
| `GET /api/v1/records/{id}` | verify fails | Y | 503 TBX-WEB-503 | page: "This record failed verification; run `traceback verify`" |
| failed-run cleanup | unlink fails | Y | log + `status` line | "sealed copy could not be removed; run `traceback clean --failed`" |

### Failure Modes Registry

| Codepath | Failure mode | Rescued | Test | User sees | Logged |
|---|---|---|---|---|---|
| B1 key | re-run after upgrade publishes a second record (job ID is in provenance) | Y (documented, marked "same measurement as") | B1-4 | two rows, linked | job log |
| B1 key | `_is_local_request` false for a legacy row | Y | A-3 | status says local | — |
| A4b label | donor name in label reaches JSON | Y (after fix) | added | not in JSON | — |
| B2b | research run resumed under built-in method | Y | B2b-4 | resumes under policy | job log |
| C1 | tampered bundle rendered as numbers | Y | C1-2 | 503 page | server log |
| C6 | V2 view digest changes after union widening | Y | C6 pin test | — | — |
| D10 | canary imports a moved module and fails at 03:30 | Y (after fix) | canary in D10 acceptance | canary FAIL notice | canary log |
| A8 | cleanup deletes a RETRYABLE job's copy | Y | A8-3 | — | job log |

No row is RESCUED=N + TEST=N + silent: 0 critical gaps after the auto-fixes.

### NOT in scope (CEO additions)

| Item | Rationale |
|---|---|
| `traceback align` (X6) | Waits on P5 |
| N-record overlay (X5) | Waits on A0 and the batch-2 question |
| Canary pinned to an installed release (X8) | Separate install story |
| Multi-workstation install/upgrade/reference distribution | Real, but a separate epic; trigger: a second operator runs `traceback` on a second Mac |

### What already exists

See 0B. The plan reuses every existing piece it could find; nothing is rebuilt.

### Dream state delta

After this plan: any BAM batch runs with labels and listing; bins and MAPQ can vary without bricking ROOTs; the site explains one record. Still missing against the ideal: alignment inside the product, administrator-owned releases instead of operator policies, comparisons gated by repeatability evidence, a multi-Mac install story.

### CEO completion summary

```
+====================================================================+
|            MEGA PLAN REVIEW — COMPLETION SUMMARY                   |
+====================================================================+
| Mode selected        | SELECTIVE EXPANSION                         |
| System Audit         | product spec conflicts (shell cmds, config- |
|                      | urable pipeline, E12 deferral); stale D7    |
| Step 0               | approach A kept pending UC1/UC2             |
| Section 1  (Arch)    | 2 issues (ID rule, V2|V3 union)             |
| Section 2  (Errors)  | 13 error paths mapped, 2 GAPS (fixed)       |
| Section 3  (Security)| 1 issue (labels in JSON), 0 High            |
| Section 4  (Data/UX) | 4 edge cases mapped, 0 unhandled after fix  |
| Section 5  (Quality) | 1 issue (list-command naming, T3)           |
| Section 6  (Tests)   | diagram in Phase 3, 1 gap (unaided DoD)     |
| Section 7  (Perf)    | 1 issue (1-bp table size)                   |
| Section 8  (Observ)  | 0 gaps after jobs/policy column             |
| Section 9  (Deploy)  | 2 risks (B1 re-measure, canary vs D10)      |
| Section 10 (Future)  | Reversibility: 3/5, debt items: 2           |
| Section 11 (Design)  | deferred to Phase 2                         |
+--------------------------------------------------------------------+
| NOT in scope         | written (4 items)                           |
| What already exists  | written                                     |
| Dream state delta    | written                                     |
| Error/rescue registry| 9 methods, 0 CRITICAL GAPS                  |
| Failure modes        | 8 total, 0 CRITICAL GAPS                    |
| TODOS.md updates     | 4 items (X5, X6, X8, multi-Mac)             |
| Scope proposals      | 8 proposed, 4 accepted                      |
| CEO plan             | not written separately (this file)          |
| Outside voice        | ran (codex + claude)                        |
| Lake Score           | 6/8 recommendations chose complete option   |
| Diagrams produced    | 3 (architecture, dream state, error map)    |
| Stale diagrams found | 0                                           |
| Unresolved decisions | premises P1-P8, UC1-UC4                     |
+====================================================================+
```

**Phase 1 complete.** Codex: 15 concerns. Claude subagent: 14 issues. Consensus: 5/6 confirmed, 1 disagreement (degree) surfaced as taste T2. Premise gate written under "Pending user gates", undecided.

## Phase 2: Design review

### Step 0: design scope

Initial completeness: 4/10. C1 is specific (bar geometry, open bin, axis titles, caption); C5-C7 are generic; no view has a state table; no navigation model. `DESIGN.md` exists but is an architecture note with no visual tokens. `styles.css` hard-codes colours and has two focus colours (`#d88400` and `#1d4ed8`). Existing patterns to reuse: the `.lg` longitudinal block's data-state model (loading, slow, empty, error, partial, revoked, retry; `longitudinal.js:548`), its contrast-checked colours and 44 px targets, and the skip link and `aria-live` region. Classifier: APP UI (data-dense, task-focused).

### Dual voices (Design)

CLAUDE SUBAGENT (design, independent review), summarised:
- CRITICAL: no navigation model (how a catalog row opens a record; Back; bookmarkable URLs). CRITICAL: no state table for any view.
- HIGH: the 1-bp policy breaks per-bar labels, the 1,001-row table and the "150-200 bp" tile. HIGH: compare A/B roles, encoding and the word "comparable". HIGH: two reports (signed vs styled) with no explanation. HIGH: record header lacks identity.
- MEDIUM: four generic tiles (cut "most common bin"; no green/red); hover-only tooltip; compare-button reason; reader and E12 sessions ignored; 390 px details; no tokens; live refresh; label edge cases; rounding footnote.
- Ratings: IA 5, states 2, journey 4, slop 6, design system 4, responsive 4, a11y 4.

CODEX SAYS (design, UX challenge), summarised:
- Name the page's decision: "Is this record technically trustworthy enough for descriptive review, and what do I do next?"
- Hierarchy is backwards: limitations, completeness, warnings and denominator reconciliation should precede the chart.
- Ship C1+C3+C4-lite first and test it on the pending batch.
- Remove operator-configurable policies and derived metrics from this milestone; block pairwise deltas until an explicit comparability decision exists.
- States unspecified; adopt the longitudinal state model. 1-bp chart contradictions. Responsive and a11y are checkboxes, not requirements.

```
DESIGN LITMUS SCORECARD (cross-model):
═══════════════════════════════════════════════════════════════
  Check                               Claude  Codex   Consensus
  ──────────────────────────────────── ─────── ─────── ─────────
  1. Hierarchy right?                  partly  no      DISAGREE (order) -> T5
  2. States specified?                 no      no      CONFIRMED (no)
  3. Journey coherent?                 partly  partly  CONFIRMED (partly)
  4. Specific, not generic?            partly  partly  CONFIRMED (partly)
  5. Accessibility specified?          no      no      CONFIRMED (no)
  6. Responsive intentional?           partly  no      DISAGREE (degree)
  7. Honest labelling preserved?       yes     partly  DISAGREE (deltas) -> T4
═══════════════════════════════════════════════════════════════
```

### Pass 1: Information architecture (4/10 -> 8/10)

Added to the spec (C0, new item "Site structure and states"): hash routes `#/` (catalog), `#/records/{record_id}`, `#/compare?a={record_id}&b={record_id}`; Back returns to the catalog with checkboxes intact (selection kept in `sessionStorage`, cleared on logout); focus moves to the view's `h1` after each route change. One persistent banner on every view: "Development records · unqualified · not for clinical use".

```
#/ catalog ──click label──> #/records/{id} ──"Compare with…"──> #/compare?a&b
   │  ▲                          │                                   │
   │  └──────── Back ────────────┘◄────────────── Back ──────────────┘
   └─ jobs disclosure (one line)        └─ "Printable view" -> /records/{id}/report
```

Record view order (taste T5 decided): (1) `h1` = label, else short record ID; under it reference, policy (built-in or research ID) and short ID; (2) one-line status: qualification word, verification word, preflight warning count ("2 warnings, see below"); (3) histogram; (4) denominator strip; (5) "What this record is" (one row per state axis, with preflight warnings listed); (6) "How to read it (descriptive, not diagnostic)"; (7) exact values and identities, collapsed. Tiles reduced to two inside the strip: "Eligible of scanned" and "Share over 1 kb"; "Most common bin" is annotated on the chart only; "Share 150-200 bp" shows only when the policy has edges at 150 and 200, else "not available for this policy's bins".

### Pass 2: Interaction state coverage (2/10 -> 8/10)

Added to C0 as the contract every Track C item tests:

```
VIEW     | LOADING                 | EMPTY                              | ERROR                                   | SUCCESS            | PARTIAL
---------|-------------------------|------------------------------------|-----------------------------------------|--------------------|-----------------------------
catalog  | "Loading records…" in   | "No records yet. Run: traceback run | "Could not read the catalog (TBX-…).    | table              | rows that fail verification
         | the live region; table  | BAM --reference ID --label NAME     | Run traceback doctor." + Retry button   |                    | shown as "failed verification"
         | skeleton not used       | --import"                           |                                         |                    | with the verify command; others normal
record   | "Loading record…"       | eligible = 0: chart replaced by     | 404: "No record with this ID" + link to | full view          | preflight PARTIAL/WARN: chart
         |                         | "No eligible alignments; see the    | catalog. 503: "This record failed       |                    | shown, warnings listed above it
         |                         | denominator strip"                  | verification. Nothing is shown. Run     |                    | in the status line
         |                         |                                     | traceback verify --root R ID"           |                    |
compare  | "Loading both records…" | fewer than 2 selected: button       | one side fails: that side shows its     | two charts (+delta | different settings: no overlay,
         |                         | disabled with reason                | error, the other side still renders     | per T4)            | banner lists the differences
report   | n/a (server-rendered)   | eligible = 0 as record view         | same copy as record 404/503, HTTP code  | page               | warnings printed above chart
jobs     | —                       | "No job has run on this ROOT"       | "Job status unavailable" (no stale)     | "Last job finished | running: "Running: stage
         |                         |                                     |                                         | and verified"      | measure, 1 min 20 s"
session  | —                       | —                                   | expired: "Session ended. Run traceback  | —                  | —
         |                         |                                     | serve again" (existing TBX-AUTH copy)   |                    |
```
Reuse the `data-state` attribute pattern from `longitudinal.js`. Refresh: re-fetch on window focus plus a Refresh button; no polling.

### Pass 3: User journey (4/10 -> 7/10)

```
STEP | USER DOES                          | USER FEELS                  | PLAN SPECIFIES?
-----|------------------------------------|-----------------------------|-------------------------------
1    | opens serve link                   | "which of these is mine?"   | yes: labels + short IDs (C5)
2    | clicks a label                     | "is this one OK to look at?"| yes: status line before chart (T5)
3    | reads histogram                    | "what am I seeing?"         | yes: axis titles, caption, table
4    | reads "How to read it"             | "can I say anything?"       | yes: descriptive sentence only
5    | selects two, Compare               | "are these comparable?"     | partly: T4 decides delta display
6    | prints                             | "is this the record?"       | yes after fix: header names the signed report.html as the record of truth
```
5-second: label, banner, status word. 5-minute: chart + strip. Long term: the operator trusts that a "failed verification" row is never drawn as data.

### Pass 4: AI slop risk (6/10 -> 8/10)

Removed generic patterns: four-tile KPI row cut to two values inside the strip; no green/red or threshold colouring on any number; no icons; no cards on desktop (table rows; cards only at < 42rem). Copy: "comparable" replaced by "Same analysis settings; differences are descriptive" (T4).

### Pass 5: Design system alignment (4/10 -> 7/10)

`DESIGN.md` has no visual section. Added to C1: CSS custom properties in `styles.css` `:root` for `--chart-a`, `--chart-b`, `--chart-hatch`, `--neutral`, `--focus` (one focus colour; `#1d4ed8` kept, `#d88400` removed), each pair checked at 4.5:1 against the background, and A/B told apart by stroke pattern (solid vs dashed) as well as colour. Recommend `/design-consultation` later for a fuller system (deferred; trigger: an outside reader sees the site).

### Pass 6: Responsive and accessibility (4/10 -> 8/10)

Added to C0/C1/C5/C6 acceptance:
- Below 42rem: catalog rows become stacked blocks in table order (label, reference, eligible, status); chart drops per-bar labels (the table carries them), ticks thin to 0, 200, 500, 1000; compare stacks the two charts vertically, delta table below; no horizontal page scroll at 390 px for every view (test per view, not only C1).
- 200% zoom reflows without loss.
- SVG: `role="img"`, `<title>` and `<desc>` via `aria-labelledby`, `aria-describedby` pointing at the table. The open-bin wording is a visible footnote and a table note, not a hover tooltip.
- Compare checkbox accessible name "Compare <label>"; disabled button has `aria-describedby` "Select exactly 2 records (N selected)" in a polite live region.
- 44 px minimum targets in all new controls; keyboard-only completion of catalog -> record -> compare -> back, tested in the DOM harness.
- Rounding: shares one decimal, half-even; footnote "shares rounded; exact counts in the table".

### Pass 7: Unresolved design decisions

| Decision | Resolution |
|---|---|
| Home view | catalog (`#/`) |
| Record ID in URLs | `record_id` everywhere (Eng E3); server maps to `result_id` |
| Chart for 1-bp policies | line chart of 1-bp densities; table grouped into 10-bp rows by default with a "show 1-bp rows" toggle; per-bar labels off |
| Signed vs styled report | styled page header: "Formatted view of signed record <id>. The signed report.html in the record is the record of truth", with a link |
| Labels vs signed identity | label in `h1`, short ID always beside it; the qualifier "(operator note, not part of the signed record)" once in the column header and once on the record view |
| Reader sessions | the new routes return 403 for reader sessions, like the explorer routes; E12 stays where it is, below the catalog |
| Compare roles | A = earlier import time; "Swap" button; B−A sign stated in the table header |
| Partial preflight | chart shown; warnings above it |
| Printable output | an inspection aid, not a shareable artifact; says so |

**Phase 2 complete.** Codex: 12 concerns. Claude subagent: 15 issues. Consensus: 4/7 confirmed, 3 disagreements (T4, T5, responsive degree folded in). Passing to Phase 3.

## Phase 3: Eng review

### Step 0: scope challenge (against the code)

Read: `cli.py` (run, resume, status, `_is_local_request`, `_publish_verified_record`, provenance), `store.py:449-466`, `bundles.py:329-336, 547`, `local_authority.py:81-186, 428-560`, `local_catalog.py:126, 220, 245-405, 436, 676-720`, `fragment_explorer.py:85-89, 193-240, 269-332, 381-400`, `web/explorer.py:600-720`, `web/server.py:86-90, 634-675`, `measurement.py:128-138`, `tests/test_operator_guide.py:28-32`, `tests/test_awake.py`, `tests/test_doctor.py:20-30`, `DESIGN.md`, `docs/CANARIES.md:86-100`.

- Complexity check: more than 8 files and more than 2 new modules. Not reduced here (autoplan override P2: never reduce scope in Eng); the scope question is UC1.
- What existing code solves sub-problems: see CEO 0B. New here: `result_catalog.py:3604` (authority-bound read) is what C1 must reuse; `validate_public_text` (`web/contracts.py:113-150`) is what the label grammar must reuse.
- Search check: no new infrastructure pattern; `bisect` for histogram binning is the standard-library answer [Layer 1].
- TODOS cross-reference: "longitudinal comparison only after repeatability evidence" constrains C6 (taste T4).
- Distribution: no new artifact type.

Four spec claims were false against the code and are corrected in the spec body above (both voices found the first two independently):
1. B1 "re-run reuses the existing record directory": false; `run_token` carries the random job ID (`cli.py:1371`) into provenance, which enters `record_id` (`bundles.py:329-336`).
2. C6 "widen one function to V2|V3": false; E07 rejects local records in five places and across two authorities (`fragment_explorer.py:85-89, 279, 321-322, 332`; `compatibility.py:852`; `local_catalog.py:355`). C6 now builds `LocalPairView` from two `LocalRecordView`s.
3. B2b "every byte identical / tree sha256 pinned": impossible (job ID, per-ROOT key and HMAC in every record). Now pins canonical measurement sha256 and rendered report bytes.
4. C7 "inline CSS": blocked by the server CSP `style-src 'self'` (`server.py:88-90`). Now a packaged `report.css`.

### Dual voices (Eng)

CLAUDE SUBAGENT (eng, independent review), summarised (22 findings, each quoted against code):
- CRITICAL: C6 cannot use E07 (5 blockers). HIGH: B1 reuse false; B2b pin impossible; ID length caps (token up to 135 > 128; `MethodVersion` pattern 64; ambiguous parse); `last_error` can carry a filename into JSON; labels vs DESIGN.md and vs `validate_public_text`; C7 CSP.
- MEDIUM: D1 repair can never fire (key created after the job row; 0400 key would be refused); first bin edge above 0 loses spans; reserve the built-in policy ID; A1 FASTA path into JSON; new routes must join the integrity-pinned dispatch; `_is_local_request` coupling; damaged research store refuses everything; D10 breaks `test_operator_guide.py:30` and silent monkeypatch no-ops; `_histogram` O(spans x bins); A5 turns the real canary red; A8 deletes read-only snapshot files; A1's narrowed except loops as RETRYABLE inside `run`; DoD fixtures must differ; B3 ratio test divides by zero.
- Verdicts: architecture partly, tests partly, performance no, security partly, errors partly, deployment yes with fixes.

CODEX SAYS (eng, architecture challenge), summarised (13 findings):
- CRITICAL: B1 reuse false. CRITICAL: C6 cannot widen E07; built-in vs research cannot share one `FragmentExplorerView` (one registry and head required).
- HIGH: recomputing the method hash in `_is_local_request` un-recognises earlier new-format rows after any method change; C1 must use the authority-bound reader, not `verify_reference`; import opens only the built-in authority, and rollback does not remove imported research rows; labels violate DESIGN.md; research MAPQ mislabelled "Below MAPQ 20" (`local_catalog.py:220`); D3 erases distinct crash semantics (fd-relative create, staged directories, repairable artifacts); D10 unsafe for the checkout-based canary; scope uncalibrated.
- MEDIUM: CLI/API identity model not navigable; no frozen E07 view fixture, so a pin must be literal bytes.
- Verdicts: architecture no, tests no, performance partly, security partly, errors partly, deployment no.

```
ENG DUAL VOICES — CONSENSUS TABLE:
═══════════════════════════════════════════════════════════════
  Dimension                           Claude  Codex   Consensus
  ──────────────────────────────────── ─────── ─────── ─────────
  1. Architecture sound?               partly  no      DISAGREE (degree; fixes folded in)
  2. Test coverage sufficient?         partly  no      DISAGREE (degree; tests added)
  3. Performance risks addressed?      no      partly  DISAGREE (degree; bisect added)
  4. Security threats covered?         partly  partly  CONFIRMED (partly)
  5. Error paths handled?              partly  partly  CONFIRMED (partly)
  6. Deployment risk manageable?       yes*    no      DISAGREE -> T7 (*with canary re-baseline and duplicate note)
═══════════════════════════════════════════════════════════════
```

All findings except the scope question were mechanical (one right answer against the code) and are folded into the spec; the deployment-risk verdict is taste T7.

### Section 1: Architecture

```
                       ┌──────────────── operator CLI ────────────────┐
 BAM ─> preflight (A1/A5/A6) ─> run ─┬─ JobStore  key = req(token, input sha, workflow sha(method))   [B1]
                                    │   token = local-<ref>[:<policy>]                                [D2]
                                    ├─ stages ─> publish ROOT/records/<record_id>  (signed; unchanged)
                                    │              └─ ROOT/labels/<record_id>.json (unsigned)         [A4b]
                                    └─ --import ─> import_local_record ─ peek definition_id
                                                     ├─ built-in:  ROOT/authority/<ref>/
                                                     └─ research:  ROOT/research-authority/<ref>/<pol>/ [B2b]
 policy add ─> ROOT/policies/<id>/ (write-once, pinned)                                            [B2a]

 serve ── route table (integrity-pinned) ─────────────────────────────────────────────
   /api/v1/explorer/*        existing E04/E06/E07 (unchanged)
   /api/v1/records/{rid}     ─> catalog authority-bound reader ─> LocalRecordView        [C1]
   /api/v1/records/compare   ─> LocalRecordView x2 ─> LocalPairView (no E07)             [C6]
   /records/{rid}/report     ─> LocalRecordView ─> HTML + report.css (no scripts)        [C7]
   explorer source reload on catalog DB mtime change                                     [A4a]
```

Coupling added: `web/records.py` depends on the catalog reader and `local_authority` (for the policy description); acceptable, same direction as `local_catalog.py`. Removed coupling: C6 no longer reaches into `evidence_inspector/fragment_explorer.py`. Production failure per new integration point: (a) catalog DB locked during `run --import` while `serve` reads: SQLite busy timeout applies (30 s, `result_catalog.py:1734`); (b) research authority damaged: that policy's rows hidden, others render; (c) labels file replaced by a symlink: no-follow read refuses it, row shows no label.

### Section 2: Code quality

- DRY: `PROBLEM_TABLE` is the one CAUSE/FIX source; `state_copy.py` is the one UI copy source. The authority store is parameterised, not copied.
- Naming: `validate_local_method_authorities` (corrected in the spec); `LocalPairView` vs `ExplorerComparison` are distinct on purpose (local, non-persisted).
- Error handling: the bare `except Exception` at `preflight.py:383` is narrowed with an explicit run-path test (A1-4).
- Over-engineering check: `research-authority` per policy is the heaviest new structure; its alternative (measure once at 1 bp) is UC2.
- Stale diagrams: none in touched files.

### Section 3: Test review

Framework: pytest (`pyproject.toml`), plus the Node DOM harness (`tests/web/app_dom_harness.js`). 92 test files today.

```
CODE PATHS                                              USER FLOWS
[+] preflight.py                                        [+] Zip to record
  ├── no @SQ -> BAM-003            [GAP->A1-1]            ├── [GAP] [→E2E] unaligned -> align -> run (DoD-1 step 1)
  ├── 0 records -> BAM-004         [GAP->A1-2]            ├── [GAP] [→E2E] 3 labelled runs + import (DoD step 2)
  ├── OSError/ValueError -> BAM-001[★★ exists]            └── [GAP] manual: real zip unaided (DoD manual)
  ├── other exc (preflight/run)    [GAP->A1-4]          [+] Failure recovery
  ├── @RG DS modbase_models        [GAP->A5-1]            ├── [GAP] missing .bai -> RUN-009, no job (DoD 3)
  └── contig diff / chr rename     [GAP->A6-1,2]          ├── [GAP] MAPQ-0 BAM -> RUN-005 -> status cause/fix (DoD 7-8)
[+] cli run/status/jobs                                   └── [GAP] [→E2E] lease loss -> resume (D8)
  ├── key with method              [GAP->B1-1]          [+] Site
  ├── legacy row recognised        [GAP->B1-3]            ├── [GAP] catalog -> record -> compare -> Back, keyboard only (C0)
  ├── second record after upgrade  [GAP->B1-4]            ├── [GAP] every state cell (C0, ~20 DOM tests)
  ├── last_error uncoded -> hidden [GAP->A3]              ├── [GAP] 390 px per view (C0)
  ├── labels absent from --json    [GAP->DoD 9]           └── [GAP] tampered/stale record -> 503 page (C1-2)
  └── input_removed + status       [GAP->A8-4]          [+] Analysis change
[+] policies.py                                           ├── [GAP] policy add -> run --policy -> import -> view (DoD 4)
  ├── grammar, caps, reserved IDs  [GAP->B2a-3]           └── [GAP] built-in vs research compare (DoD 6)
  ├── first edge 0                 [GAP->B2a-3]
  └── bisect == loop               [GAP->B2a-5]
[+] web/records.py
  ├── counts == signed             [GAP->C1-1]
  ├── stale authority -> 503       [GAP->C1-2]
  └── LocalPairView same/different [GAP->C6-1,2]
[+] research authority
  ├── damaged -> only its rows     [GAP->B2b-3]
  └── MAPQ label "Below MAPQ n"    [GAP->B2b-2]
[+] refactors
  ├── D3 crash points x3           [GAP->D3]
  ├── D10 monkeypatch targets      [GAP->D10]
  └── canary after A1/A5/D10       [manual, PR evidence]
COVERAGE today: 1/40 paths (only BAM-001). Every GAP above maps to a numbered acceptance criterion in the spec.
QUALITY target: ★★★ for B1, B2a/b, C1, C6 (happy + edge + error); ★★ acceptable for A9 golden files.
```

Regression rule: B1 (`_is_local_request`) and A1 (exception narrowing) modify existing behaviour; both have regression tests named above (CRITICAL tag). The 2am-Friday test: DoD-1 on the real zip unaided. The hostile-QA test: a label of 80 characters containing `..` (must be refused at the CLI, never 500 the route). Chaos test: D8 lease loss mid-measure, then `resume`.

Test plan artifact: `~/.gstack/projects/danwiggins-cfddemo/danwiggins-docs-operator-usability-and-analysis-eng-review-test-plan-20261003.md`.

### Section 4: Performance

- `_histogram` O(distinct spans x bins) (`measurement.py:130-137`): with 4,095 bins and about 1e5 distinct ONT spans, about 4e8 Python steps; fixed by `bisect` in B2a (acceptance: under 1 s).
- `LocalRecordView` re-verifies per request: one bundle of a few KB plus an Ed25519 verify; expected under 50 ms; compare does two. Not cached (correctness over speed at 1-100 records; revisit if a catalog page exceeds 1 s).
- Explorer reload on catalog mtime (A4a): one `stat` per catalog request.
- 1-bp policy: 1,001-row tables are grouped (C1); JSON for one record about 40 KB.
- Disk: each run seals a copy (1x input); A0 forecasts the zip at 3x its size; A8 frees failed copies.

### Failure modes (Eng)

| New codepath | Realistic failure | Test | Error handling | User sees |
|---|---|---|---|---|
| B1 recognition | earlier new-key row after a method change | B1-3 variant | prefix rule | status shows local |
| A3 status | uncoded `last_error` holds a filename | A3 test | summary hidden | "uncoded failure; see support-bundle" |
| A4a reload | catalog changed mid-request | A4a-4 | reload on next request | new row appears |
| B2a binning | first edge above 0 | B2a-3 | refused at `policy add` | exit 2 with rule |
| B2b import | research record with built-in authority opened | B2b-2 | peek `definition_id` | correct authority |
| C1 | stale authority | C1-2 | 503 | C0 503 copy |
| C7 | inline styles blocked by CSP | C7 | `report.css` | styled page |
| D10 | canary imports a moved name at 03:30 | D10 manual | re-export from `cli/__init__.py`, canary run in PR | canary passes |
| A8 | unlink on 0444 snapshot | A8-1 | chmod first; log on failure | status line |

0 critical gaps (every row has a test and visible handling).

### Worktree parallelization

| Step | Modules touched | Depends on |
|---|---|---|
| W1-cli | `cli.py`, `problems.py`, `local_catalog.py`, `web/server.py` (reload) | — |
| W1-intake | `preflight.py`, `fixtures.py`, guide | A0 |
| W1-site | `web/records.py`, `web/state_copy.py`, `web/static/*`, `web/server.py` (routes) | — |
| D10 | `cli/` | W1-cli |
| W2-run / W2-analysis / W2-site / W2-hygiene | see "Lanes" | D10 |

Parallel lanes: wave 1 runs W1-cli, W1-intake and W1-site at once (shared file: `web/server.py`, different regions; C1 lands first). Wave 2 runs four lanes after D10. Conflict flags: W2-run and W2-analysis both edit `cli/run.py` (B2b rebases); D3 touches every lane (land last).

### NOT in scope (Eng additions)

| Item | Rationale |
|---|---|
| E07 contract revision for local records | C6 no longer needs it; trigger: comparisons must be persisted or signed |
| Caching `LocalRecordView` | Not needed below 100 records |
| A frozen E07 view fixture | No E07 change now |

### What already exists (Eng additions)

`result_catalog.py:3604` authority-bound reader (C1 reuses it); `validate_public_text` (A4b grammar reuses it); `_local_signing_key` create pattern (D1); `longitudinal.js` `data-state` model and `svg()` (C0/C1).

### Eng completion summary

| Item | Result |
|---|---|
| Step 0 | 4 false claims found and corrected; scope question left to UC1 |
| Architecture | diagram produced; 3 coupling changes; 3 production failures mapped |
| Code quality | 4 findings, all folded in |
| Tests | 40-path diagram; 1 covered today; all gaps mapped to acceptance criteria; test plan written |
| Performance | 1 real issue (binning), fixed in B2a |
| Failure modes | 9 mapped, 0 critical gaps |
| Outside voices | Claude 22, Codex 13; 2/6 confirmed, 4 degree disagreements |
| Parallelization | 3 lanes in wave 1, 4 in wave 2 |

**Phase 3 complete.** Codex: 13 concerns. Claude subagent: 22 issues. Consensus: 2/6 confirmed, 4 disagreements of degree (1 surfaced as T7). Passing to Phase 3.5.

## Phase 3.5: DX review (mode: DX POLISH; product type: CLI tool + local web viewer)

### Developer persona card

| Field | Value |
|---|---|
| Who | A scientist or sequencing operator on a team macOS workstation; knows MinKNOW, has used samtools a little; not a Python developer |
| Has | A zip of MinKNOW output (likely `bam_pass/barcodeNN/*.bam`, unaligned), a reference FASTA somewhere |
| Wants | Each sample's fragment-length histogram, named, side by side; occasionally a different MAPQ or finer bins |
| Tolerates | A few terminal commands copied from a guide; minutes of compute |
| Will not tolerate | Reading Python, guessing which ID goes where, an error that says "corrupt" when the file is fine |
| Environment | macOS, Homebrew, `uv` in a repo checkout, no root access needed |

### Developer empathy narrative

I unzip the run and find 28 small BAMs per barcode. The guide's first section is a synthetic demo, so I scroll to "Real local BAM". I register the FASTA in 12 s, which is quicker than the guide says. Preflight on one chunk tells me the BAM is "unreadable, truncated, or structurally invalid". It isn't; it's unaligned, but nothing says that and nothing mentions minimap2. Once someone tells me to align it, `run` works, but I have to `ls` the records folder to find the ID, import it by path, and restart `serve` to see it. The site shows tokens and JSON, no histogram. My second sample's row looks identical to the first because there are no names. After this plan: preflight tells me to align and prints the command; `run --label --import` names and catalogs the record in one step; the site shows the histogram. I still type `--root` on every command, and I still have to work out the per-barcode merge myself unless the guide spells it out (it now does).

### Competitive DX benchmark

| Tool | Time to first histogram from aligned BAM | Notes |
|---|---|---|
| `samtools stats` + plot script | 2-5 min | No provenance, no labels, plots by hand |
| EPI2ME (ONT) workflows | 10-30 min first time (install), then one command | Hosted/managed; fragmentomics coverage varies |
| Notebook with pysam | 10-20 min | Flexible; nothing signed or comparable |
| traceback today | not reachable unaided from an unaligned zip; about 4 min from an aligned BAM with the guide | Signed, honest labels; no histogram on the site |
| traceback after wave 1 | about 3-4 min from an aligned BAM (register, run --label --import, serve) | Signed record + histogram + catalog |

Target tier: Competitive (2-5 min) from an aligned BAM, measured excluding alignment compute. Alignment itself is outside the tool (premise P5).

### Magical moment

`traceback run SAMPLE.bam --reference hg38 --label "barcode07" --import` prints "Signed local record ready", and the already-open site shows barcode07's histogram on the next focus, with "unqualified, not for clinical use" above it. Delivery vehicle: A4a (`--import` + reload) + C1. Lowest effort that reaches the tier (P5).

### Developer journey map

| Stage | Today | After plan | Friction resolved by |
|---|---|---|---|
| 1 Discover | README -> guide; demo first | guide opens with a chooser; MinKNOW-zip path first | A9 |
| 2 Install | `uv sync`, `brew install samtools` | + `brew install minimap2`, doctor WARN | A1 |
| 3 Inspect zip | nothing | A0 triage table in the guide's format | A0 |
| 4 Align | not mentioned | per-barcode merge + minimap2 command, labelled assisted prerequisite | A1 |
| 5 First run | "corrupt" / NOT_FOUND job/bundle/trust | TBX-BAM-003 / RUN-008/009/010 with FIX | A1, A2 |
| 6 Name + list | `ls R/records`, no labels | `--label`, `catalog list`, `jobs` | A4a, A4b |
| 7 View | restart serve, tokens and JSON | `--import`, live reload, histogram | A4a, C0, C1 |
| 8 Change analysis | edit Python, bricks ROOT | `policy add`, `run --policy` (or UC2 alternative) | B2a, B2b |
| 9 Upgrade | no notes | "Upgrading" section; second record after B1; code aliases | A9, D4 |

### First-time developer confusion report

| Confusion | Addressed |
|---|---|
| "Corrupt" for an unaligned BAM | yes (A1) |
| Which ID is which (job, record, result) | yes (D20: record is the public ID) |
| Merging `bam_pass/*.bam` mixes barcodes | yes (A1 guide: per barcode only) |
| `--bins` values are edges, plus a hidden unbounded bin | yes (renamed `--bin-edges`; help text says the final bin is added) |
| "Run under a fresh ROOT" after a failure | yes (TBX-JOB-004 FIX now says same ROOT) |
| Research authority damage advice deletes the built-in store | yes (research-specific FIX) |
| `--preset` only exists on `policy add` | yes (`run --preset` error points there) |
| `--root` on every command | no (taste T1) |
| No `traceback --version` / upgrade command | partly (guide "Upgrading" section; no command; deferred) |

### Dual voices (DX)

CLAUDE SUBAGENT (DX, independent review), summarised:
- TTHW from a zip: unbounded today (about 12 steps); about 10 steps after the plan. HIGH: run does not import; import by path vs verify by ID; serve restart; `--root` everywhere. HIGH: three IDs with routes on `result_id`. HIGH: two FIX texts wrong (JOB-004 "fresh ROOT"; research AUTHORITY-002 deleting the built-in store). HIGH: batch path untested and barcode-unaware (merging `bam_pass/*.bam` mixes samples). Upgrade: B1 reuse unverified; legacy code aliases; no `run --force`.
- Verdicts: getting started partly, naming partly, errors partly, docs partly, upgrade partly, environment no.

CODEX SAYS (DX, developer experience challenge), summarised:
- CRITICAL: no batch-learning milestone; under-5-minutes impossible from zero (separate "first diagnosis" from "completed analysis" targets); zip designed around without inspection (propose `traceback intake inspect ZIP`); printed alignment pipeline violates the product constraint; B1 migration claim wrong.
- HIGH: operator policies cross the authority boundary; identifier model incoherent; run should verify and catalog atomically and print one `open` command; terminology implementation-shaped (`policy`, `ROOT`, `catalog import`; `--bins`); labels in JSON; errors outside the problem table (argparse, missing executables); hostile terminal-failure advice; guide not a 2-minute entry point; examples not copy-paste-complete; no upgrade story; renaming codes breaks scripts (need aliases); rollback claims false; D10 sequencing risk.
- Verdicts: getting started no, naming partly, errors partly, docs no, upgrade no, environment no.

```
DX DUAL VOICES — CONSENSUS TABLE:
═══════════════════════════════════════════════════════════════
  Dimension                           Claude  Codex   Consensus
  ──────────────────────────────────── ─────── ─────── ─────────
  1. Getting started < 5 min?          partly  no      DISAGREE (degree)
  2. API/CLI naming guessable?         partly  partly  CONFIRMED (partly)
  3. Error messages actionable?        partly  partly  CONFIRMED (partly)
  4. Docs findable & complete?         partly  no      DISAGREE (degree)
  5. Upgrade path safe?                partly  no      DISAGREE (degree)
  6. Dev environment friction-free?    no      no      CONFIRMED (no)
═══════════════════════════════════════════════════════════════
```

### DX passes

| Pass | Before | After | What changed in the spec |
|---|---|---|---|
| 1 Getting started | 2 | 6 | A0 triage; chooser-first guide; `--import`; live reload; TTHW targets split: first diagnosis under 1 min (preflight), first record from an aligned BAM under 5 min |
| 2 CLI design | 4 | 7 | one public record ID (D20); `catalog import RECORD_ID`; `--bin-edges`; `run --preset` points to `policy add`; `clean` without a flag is a usage error |
| 3 Errors | 4 | 7 | every new code has FIX text (BAM-004, REF-004 prints the `--reference` to add, POL-002 names both references); exit-2 messages name the rule; corrected JOB-004 and research AUTHORITY-002; uncoded failures hide raw text |
| 4 Docs | 3 | 7 | per-barcode merge; tested batch block executed by `test_operator_guide.py`; "Upgrading" section; `run --json` gives the record ID instead of `ls` |
| 5 Upgrade | 2 | 6 | B1 second-record behaviour documented and marked; code renames emit `legacy_code` for one release; rollback states one-way doors |
| 6 Environment | 4 | 5 | minimap2 in prerequisites and doctor; `--root` still per command (T1); repo checkout still required (deferred multi-Mac install) |
| 7 Community | n/a 3 | 3 | Private prototype; no change; not in scope |
| 8 Measurement | 1 | 5 | DoD-1 logs every intervention on the real zip; repeat at DoD-2 |

TTHW: today, unreachable unaided from an unaligned zip; about 4 min from an aligned BAM. After the plan: first diagnosis under 1 min; first viewable record from an aligned BAM about 3-4 min; from the zip, alignment compute plus about 5 min.

### DX scorecard

```
+====================================================================+
|              DX PLAN REVIEW — SCORECARD                             |
+====================================================================+
| Dimension            | Score  | Prior  | Trend  |
|----------------------|--------|--------|--------|
| Getting Started      |  6/10  |  2/10  |  +4 ↑  |
| API/CLI/SDK          |  7/10  |  4/10  |  +3 ↑  |
| Error Messages       |  7/10  |  4/10  |  +3 ↑  |
| Documentation        |  7/10  |  3/10  |  +4 ↑  |
| Upgrade Path         |  6/10  |  2/10  |  +4 ↑  |
| Dev Environment      |  5/10  |  4/10  |  +1 ↑  |
| Community            |  3/10  |  3/10  |   0    |
| DX Measurement       |  5/10  |  1/10  |  +4 ↑  |
+--------------------------------------------------------------------+
| TTHW                 | ~4 min (aligned) | unreachable (zip) | ↑ |
| Competitive Rank     | Competitive (aligned BAM); Needs Work (zip) |
| Magical Moment       | designed via run --label --import + C1      |
| Product Type         | CLI tool + local web viewer                 |
| Mode                 | POLISH                                      |
| Overall DX           |  6/10  |  3/10  |  +3 ↑  |
+====================================================================+
| DX PRINCIPLE COVERAGE                                               |
| Zero Friction      | gap (alignment outside the tool; --root)       |
| Learn by Doing     | covered (tested guide blocks)                  |
| Fight Uncertainty  | covered (CODE/CAUSE/FIX everywhere)            |
| Opinionated + Escape Hatches | covered (built-in default; policies)  |
| Code in Context    | covered (real-zip path first)                  |
| Magical Moments    | covered (label + import + live site)           |
+====================================================================+
```

### DX implementation checklist

```
[ ] First diagnosis < 1 min; first record from an aligned BAM < 5 min
[ ] Guide opens with the MinKNOW-zip chooser; per-barcode merge shown
[ ] run --label --import -> visible on the open site without restart
[ ] Every new error: CODE + CAUSE + FIX + guide anchor (A3 test)
[ ] One public record ID in CLI, routes and site
[ ] --bin-edges, --preset, clean flags behave as specified
[ ] Upgrading section: second record after B1; legacy_code window
[ ] Batch block executed by test_operator_guide.py
[ ] DoD-1 intervention log on the real zip
```

### NOT in scope (DX)

| Item | Rationale |
|---|---|
| `traceback intake inspect ZIP` command | A0 does it by hand first; trigger in Out of scope (`run --batch`) |
| `traceback open RECORD` / `--latest` | `run --json` gives the ID; revisit after DoD-1's intervention log |
| Renaming `policy` to `analysis`, `catalog` to `records` | Taste T8 |
| `traceback --version` and an upgrade command | Multi-Mac install epic |
| `run --force` to bypass dedupe | B1 makes a method change a new job; a forced re-run of the same method has no use yet |

### What already exists (DX)

`test_operator_guide.py` executes the guide's bash block (reuse for the batch block); the six-field problem helper `_problem` (`cli.py:313`); `NEXT_COMMANDS` in `run` output; doctor's resolved-root line.

**Phase 3.5 complete.** DX overall: 6/10 (from 3/10). TTHW: unreachable from the zip -> alignment compute plus about 5 min. Codex: 19 concerns. Claude subagent: 15 issues. Consensus: 3/6 confirmed, 3 disagreements of degree. Passing to the final gate.

## Cross-phase themes

- **Open the zip first; build in waves.** CEO (both voices), Design (Codex), Eng (Codex), DX (Codex; Claude partly). High-confidence signal -> A0 added; waves defined; whether wave 2 is built as written is UC1.
- **Operator-created analysis policies.** CEO (both), Design (Codex), DX (Codex): they make the operator a method-release administrator, against `PRODUCT-SPEC.md:38-48`; measure-once-at-1-bp removes most of the need. -> UC2.
- **Derived metrics are claims through a side door.** CEO (both), Design (Codex). -> UC3.
- **Comparison is under-gated.** CEO (Codex), Design (Codex; Claude asked for copy changes). -> T4.
- **The spec over-trusted its own reading of the code.** Eng (both) and DX (Claude) found four false claims (B1 reuse, C6 via E07, B2b byte pin, C7 inline CSS). All corrected. Lesson for implementers: run the claim against the code before building on it.
- **Labels and privacy.** CEO, Eng (both), DX (both): labels in JSON conflict with `DESIGN.md`. -> D8 and T6.
- **The 03:30 canary runs from the checkout.** CEO (Claude), Eng (both), DX (Codex). -> A1/A5 re-baseline step, D10 unload/run/reload, follow-up to pin the canary.

## Gate decisions (operator, 2026-10-04)

| Gate | Decision |
|---|---|
| UC1 scope | **Wave 1 first** (about 19 days). Then run the real zip through it unaided, logging every intervention, and re-plan wave 2 from that log. |
| UC2 analysis changes | **1-bp counts plus admin-owned presets.** Measure once at 1-bp resolution. Re-binning and derived metrics happen at display. Presets are owned by the operator or scientist; there is no operator-authored policy CLI. Ask the scientist what "change the analysis" means (P2) before building B2. |
| UC3 derived metrics | **Keep.** Short-fragment fraction and 10-bp periodicity are shown as display projections of the 1-bp counts, labelled descriptive and unqualified. |
| UC4 hygiene | **Keep all** (D3, D4/D15, D5, D6, D9) in wave 2. |
| Premises | **All accepted:** P1 inspect the zip first (A0), P3, P4, P5, P7, P8. P2 is resolved through UC2. P6: plan 5–6 weeks in total. |
| Taste T1–T10 | Accepted as auto-decided. |

The original gate text follows for the record.

## Pending user gates (now resolved, see above)

Nothing below is decided. The spec body reflects your stated direction plus mechanical fixes from the reviews (false claims corrected, missing states, privacy, FIX texts). Each gate says what changes if you answer it the other way.

### Premise gate (Phase 1, never auto-decided)

| # | Premise | Recommendation | Status |
|---|---|---|---|
| P1 | The pending zip holds unaligned MinKNOW/Dorado BAMs like the 28 reviewed `bam_pass` files | Do A0 (read-only triage, 0.5 d) before building A1/A5/A6; re-plan if it differs | UNDECIDED |
| P2 | "Change the analysis" means changing MAPQ and bin edges | Ask the scientist what they would change first; if bins, see UC2; if a new analysis family, B2 is the wrong investment | UNDECIDED |
| P3 | Two local records under one exact method are comparable enough to show a delta | Show it as "same analysis settings; differences are descriptive" (T4), never as a sample comparison | UNDECIDED |
| P4 | The site's audience is you now and outside readers in demand interviews later | Accept; C7 and outside-reader polish wait until P7 is settled | UNDECIDED |
| P5 | Printing an alignment command (alignment outside the tool) is acceptable for this slice | Accept for this slice, labelled "assisted prerequisite"; PRODUCT-SPEC's no-shell-commands goal stays open; `traceback align` is a triggered follow-up | UNDECIDED |
| P6 | About 3 calendar weeks | Plan for 5-6 weeks for both waves; about 2 for wave 1 | UNDECIDED |
| P7 | The new BAMs' provenance and consent are known before any record from them is shown outside the team | Write one line on provenance and rights in the A0 PR; no external showing until then; no donor identifiers in labels | UNDECIDED |
| P8 | Team Macs run traceback from a repo checkout with `uv run` | Accept for now; multi-Mac install is a triggered follow-up | UNDECIDED |

### User Challenges (both models recommend changing your stated direction)

**UC1: Build wave 1, then re-plan wave 2 from batch evidence** (CEO both voices; Eng Codex; DX Codex; Design Codex)
- You said: one epic with all four tracks (A-D) and the listed items.
- Both models recommend: build wave 1 only (A0, B1, D1, D2, A1-A6, A4c, C0, C1, C3, C5; about 19 days), run the real zip through it unaided with an intervention log, then decide which of wave 2 (B2, B3, C6, C7, D3-D10) still earns its place.
- Why: the requirements for batch intake, analysis change and comparison depend on a zip nobody has opened; wave 1 is the shortest path to batch-2 results; wave 2 holds most of the one-way doors (policy stores, research authority).
- What we might be missing: you may need research mode and comparison for a specific conversation soon; parallel builder lanes may already be staffed, making wave 2 cheap to run now.
- If we're wrong, the cost is: wave 2 starts about 2 weeks later than it could have.
- Recommendation: accept. UNDECIDED.

**UC2: Replace operator-created research policies with measure-once 1-bp counts (or administrator-issued presets)** (CEO both voices; Design Codex; DX Codex)
- You said: research mode exactly as designed in `algo.md` (ROOT/policies, `policy add/show`, `run --policy`, an authority per reference and policy).
- Both models recommend: first ask what the scientist wants to change (P2). If it is bins, add one new built-in method version that stores 1-bp counts (0-999 plus 1000+) and make bin edges a view-time choice; MAPQ changes become one or two named presets issued like the built-in method, not operator-authored files.
- Why: per-policy authority stores and write-once IDs are a parallel release path the product spec reserves for administrators (`PRODUCT-SPEC.md:38-48`); 1-bp storage makes the most common change free and keeps every record comparable on one axis.
- What we might be missing: `algo.md`'s design keeps every existing record byte-identical with zero migration; a new built-in version means old ROOTs keep the old method; scientists may want arbitrary MAPQ values quickly.
- If we're wrong, the cost is: about 1 extra week now (new built-in version plus view-time binning) and less flexibility for MAPQ.
- Recommendation: answer P2 first; if bins, accept. UNDECIDED.

**UC3: Drop the derived metrics (short fraction, 10-bp periodicity) from B3** (CEO both voices; Design Codex)
- You said: a 1-bp policy with short-fragment ratio and periodicity as explorer projections.
- Both models recommend: keep the 1-bp preset and its chart; drop the two derived values from the site (or keep them in `catalog export` only).
- Why: they have no scientific owner, validation or threshold; a disclaimer does not stop a number on a page from being read as signal; this is the main path to overclaiming in the plan.
- What we might be missing: a scientist may already use these exact definitions and want them on screen for a specific comparison.
- If we're wrong, the cost is: the values are computed in a notebook from the CSV export instead of on the page.
- Recommendation: accept. UNDECIDED.

**UC4: Defer most of Track D** (CEO both voices; Eng Codex on scope)
- You said: D1-D10 in this epic.
- Both models recommend: keep D1, D2 (tiny, wave 1), D7 and D8 (test safety); defer D3, D4/D15, D5, D6, D9 with triggers (D3: a fourth write-once helper is added; D4/D15: a script consumes problem codes; D5: someone parses `doctor --json`; D9: before an outside reader sees the guide). D10 only when wave 2 runs with parallel lanes.
- Why: none of these moves batch-2 results; D3 touches every lane and D15 renames codes scripts may consume.
- What we might be missing: the code-quality review ranked several as real defects (D4's exit/retryable mismatch can mislead retry logic).
- If we're wrong, the cost is: about 6 days of debt carried longer; D4's mismatch persists.
- Recommendation: accept, but keep D4's exit/retryable part (not the rename) in wave 2. UNDECIDED.

### Taste decisions (auto-decided with a recommendation; override if you disagree)

| # | Phase | Decision taken | Alternative |
|---|---|---|---|
| T1 | DX | No `TRACEBACK_ROOT` env var (kept from the golden-path T6); doctor prints the resolved root | Add the env var now (DX Claude) |
| T2 | CEO | Treat the 6-month trajectory as "partly sound" with the wave split | Codex: "not sound" without re-planning |
| T3 | DX | Keep `jobs`, `catalog list`, `policy list` | `job list` with `jobs` alias (DX Claude) |
| T4 | Design | Same-settings pairs show overlay and B−A under "Same analysis settings; differences are descriptive" | Codex (CEO, Design): no subtraction until an explicit comparability decision exists |
| T5 | Design | Record view: identity, status line with warning count, then histogram, then denominator and limitations | Codex: limitations and denominator before the chart |
| T6 | Eng | Labels reach the operator-session web API; `DESIGN.md` Privacy amended; never in CLI JSON | No labels in any JSON; the site shows short IDs only |
| T7 | Eng | Deployment risk manageable with the canary re-baseline and second-record note | Codex: not manageable until the canary is pinned to an installed release |
| T8 | DX | Keep `policy` and `catalog` nouns | Codex: `analysis create`, `records list` |
| T9 | DX | `run --import` is opt-in | Codex: import by default, `--no-import` to opt out |
| T10 | Eng | D10 after wave 1's `cli.py` chain (D14 revised) | Split first (original D14) |

<!-- AUTONOMOUS DECISION LOG -->
## Decision Audit Trail

| # | Phase | Decision | Classification | Principle | Rationale | Rejected |
|---|-------|----------|----------------|-----------|-----------|----------|
| 1 | Setup | Launch the 4 Claude voices in parallel up front | Mechanical (deviation) | P6 | each must see no prior phase, so order does not change input | sequential |
| 2 | Setup | Codex prompts via stdin file, not `< /dev/null` | Mechanical (deviation) | user instruction | both cannot use stdin | — |
| 3 | Setup | No design mockups | Mechanical (deviation) | P6 | autonomous run | generate |
| 4 | CEO | Mode SELECTIVE EXPANSION | Mechanical | autoplan override | iteration on existing system | HOLD, EXPANSION |
| 5 | CEO | Keep approach A in the body pending UC1/UC2 | User Challenge | — | user's stated scope is the default | B, C |
| 6 | CEO | Add A0 zip triage | Mechanical | P2 | in radius, 0.5 d, unblocks A1/A5/A6 | — |
| 7 | CEO | Add `run --import` + serve reload to A4a | Mechanical | P2 | removes a step per BAM, reuses import | — |
| 8 | CEO | Add A4c CSV export | Mechanical | P2 | < 1 d, no new infra | notebook only |
| 9 | CEO | Defer N-record overlay, `traceback align`, canary pinning, multi-Mac install | Mechanical | P3 | outside radius; triggers written | build now |
| 10 | CEO | Add real-zip unaided intervention log to DoD | Mechanical | P1 | DoD proved construction only | — |
| 11 | CEO | Add external histogram cross-check to DoD manual evidence | Mechanical | P1 | cheap correctness check | — |
| 12 | CEO | Rollback states one-way doors; never delete research authority | Mechanical | P1 | "reverts cleanly" was false | — |
| 13 | CEO | Effort range 5-6 weeks | Mechanical | P1 | review doubles build time | 3 weeks |
| 14 | CEO | CEO plan spec-review loop not run separately | Mechanical (deviation) | P6 | spec passed the codex gate; 8 outside voices ran | 3-round loop |
| 15 | Design | Add C0 (routes, states, a11y, 390 px) | Mechanical | P1 | both voices: no navigation, no states | — |
| 16 | Design | Record view order | Taste T5 | P5 | identity and status before chart satisfy both partly | limitations first |
| 17 | Design | Cut tiles to two values in the strip | Mechanical | P5 | slop risk; tiles repeated the chart | four tiles |
| 18 | Design | 1-bp chart rule (line, grouped table) | Mechanical | P1 | bar labels impossible at 1,001 bins | — |
| 19 | Design | "Same analysis settings; differences are descriptive" copy | Taste T4 | P1 | user asked for same-definition deltas | block deltas |
| 20 | Design | Styled report names the signed report as the record of truth | Mechanical | P5 | two reports confused | — |
| 21 | Design | Tokens + one focus colour | Mechanical | P5 | two focus colours today | — |
| 22 | Eng | B1: second record after upgrade, marked "same measurement as" | Mechanical | P5 | reuse claim false (`cli.py:1371`) | pre-execution reuse index |
| 23 | Eng | `_is_local_request` = prefix and not synthetic | Mechanical | P5 | recomputing the hash un-recognises rows | recompute |
| 24 | Eng | Policy ID cap, combined length 63, reserved IDs | Mechanical | P1 | `Identifier`/`MethodVersion` limits | — |
| 25 | Eng | First bin edge must be 0 | Mechanical | P1 | lost spans fail as retryable | — |
| 26 | Eng | `bisect` binning | Mechanical | P5 | O(spans x bins) | — |
| 27 | Eng | B2b pins measurement sha + report bytes, not tree sha | Mechanical | P5 | tree sha impossible | — |
| 28 | Eng | Import peeks `definition_id` to pick authority | Mechanical | P1 | import opens built-in only | — |
| 29 | Eng | MAPQ exclusion label from policy | Mechanical | P1 | "Below MAPQ 20" hard-coded | — |
| 30 | Eng | Damaged research store hides only its rows | Mechanical | P1 | one store bricked serve | refuse all |
| 31 | Eng | C1 via authority-bound reader; routes join integrity pins | Mechanical | P1 | `verify_reference` skips authority | — |
| 32 | Eng | C6 as `LocalPairView`, no E07 | Mechanical | P5 | E07 rejects local records 5 ways | E07 revision |
| 33 | Eng | C7 `report.css` | Mechanical | P5 | CSP blocks inline styles | per-route CSP hash |
| 34 | Eng | `last_error` shown only when coded | Mechanical | P1 | filenames in uncoded messages | verbatim |
| 35 | Eng | Labels: `validate_public_text` grammar; none in CLI JSON | Mechanical | P1 | DESIGN.md privacy; 500 risk | — |
| 36 | Eng | Labels in operator web API | Taste T6 | P3 | site needs names | none in JSON |
| 37 | Eng | D1 repair when records empty; accept 0400 | Mechanical | P5 | repair could never fire | — |
| 38 | Eng | D3 as three primitives | Mechanical | P5 | helpers have different crash semantics | one helper |
| 39 | Eng | D10 test fixes + canary unload/run/reload | Mechanical | P1 | path-based test, silent monkeypatch, checkout canary | — |
| 40 | Eng | D10 timing after wave 1 | Taste T10 | P3 | wave 1 is serial anyway | split first |
| 41 | Eng | A1/A5 PRs re-record the real canary baseline | Mechanical | P1 | 03:30 red on merge night | — |
| 42 | Eng | A8 chmod + snapshot-check tolerance | Mechanical | P1 | 0444 snapshots | — |
| 43 | Eng | A1 run-path exceptions terminal, not retried | Mechanical | P1 | retry loop | — |
| 44 | Eng | DoD fixtures differ; no labels/paths in JSON check | Mechanical | P1 | identical fixtures dedupe | — |
| 45 | Eng | B3 absolute thresholds | Mechanical | P5 | ratio divides by zero | — |
| 46 | Eng | D7: drop the awake item (already fixed by `d4050ad`) | Mechanical | P4 | stale review claim | — |
| 47 | Eng | Deployment risk manageable | Taste T7 | P6 | fixes planned in | not manageable |
| 48 | DX | Mode POLISH, product CLI + local viewer | Mechanical | autoplan override | — | — |
| 49 | DX | Public ID = record_id (D20) | Mechanical | P5 | three IDs confused | result_id routes |
| 50 | DX | `catalog import RECORD_ID` | Mechanical | P5 | path vs ID mismatch | path only |
| 51 | DX | `--bin-edges` rename | Mechanical | P5 | values are edges | `--bins` |
| 52 | DX | Corrected TBX-JOB-004 and research AUTHORITY-002 FIX | Mechanical | P1 | destructive advice | — |
| 53 | DX | FIX text for BAM-004, REF-004, POL-002; exit-2 messages | Mechanical | P1 | missing fixes | — |
| 54 | DX | Legacy code aliases + `legacy_code` window | Mechanical | P1 | renames break scripts | hard rename |
| 55 | DX | Per-barcode merge; tested batch block | Mechanical | P1 | merging mixes samples | untested loop |
| 56 | DX | Guide chooser + Upgrading section | Mechanical | P1 | real path buried | — |
| 57 | DX | Label change reported on reuse | Mechanical | P5 | silent overwrite | — |
| 58 | DX | No `TRACEBACK_ROOT` | Taste T1 | P3 | earlier gate kept it out | env var |
| 59 | DX | Keep list command names | Taste T3 | P5 | matches `catalog import` | `job list` |
| 60 | DX | Keep `policy`/`catalog` nouns | Taste T8 | P5 | renames churn the existing CLI | `analysis`, `records` |
| 61 | DX | `--import` opt-in | Taste T9 | P3 | keeps `run`'s current contract | default on |
| 62 | All | Premises P1-P8 left undecided | Gate | — | never auto-decided | — |
| 63 | All | UC1-UC4 left undecided | User Challenge | — | never auto-decided | — |

## Implementation Tasks (aggregated)

Tasks are the spec's child items; the reviews changed these (P1 blocks wave 1, P2 same wave, P3 follow-up):
- [ ] **T1 (P1, human ~0.5 d / CC ~20 min)** — A0 — triage the pending zip read-only. Files: none (PR description).
- [ ] **T2 (P1, ~0.75 d / 40 min)** — B1 — method in the key; prefix recognition; second-record marking. Files: `traceback_runner/cli.py`, `tests/test_run_local.py`.
- [ ] **T3 (P1, ~2 d / 1.5 h)** — A4a — `jobs`, `catalog list`, `--import`, import by ID, serve reload; no labels in JSON. Files: `cli.py`, `local_catalog.py`, `web/explorer.py`.
- [ ] **T4 (P1, ~1.5 d / 1 h)** — C0 — routes, state table, a11y, 390 px. Files: `web/static/*`.
- [ ] **T5 (P1, ~3 d / 2.5 h)** — C1 — `LocalRecordView` via authority-bound reader; integrity-pinned route; 1-bp chart rule. Files: `web/records.py`, `web/server.py`, `web/static/chart.js`.
- [ ] **T6 (P1, ~1.5 d / 1 h)** — A3 — coded-only `last_error`, `PROBLEM_TABLE` with aliases. Files: `cli.py`, `problems.py`.
- [ ] **T7 (P2, ~2 d / 1.5 h)** — C6 — `LocalPairView`; E07 untouched. Files: `web/records.py`, `web/server.py`.
- [ ] **T8 (P2, ~2 d / 1.5 h)** — B2a — ID caps, first edge 0, `bisect`. Files: `policies.py`, `measurement.py`.
- [ ] **T9 (P2, ~3 d / 2.5 h)** — B2b — import picks authority; MAPQ label; per-policy quarantine. Files: `local_catalog.py`, `local_authority.py`.
- [ ] **T10 (P2, ~2 d / 1.5 h)** — D10 — split with test fixes and canary run. Files: `cli/`, `tests/test_operator_guide.py`.
- [ ] **T11 (P3)** — follow-ups in "Out of scope" with their triggers.

## GSTACK REVIEW REPORT

| Review | Trigger | Why | Runs | Status | Findings |
|--------|---------|-----|------|--------|----------|
| CEO Review | `/plan-ceo-review` (via /autoplan) | Scope & strategy | 1 | issues_open | 8 proposals, 4 accepted, 4 deferred; 4 user challenges; premise gate pending |
| Codex Review | `/codex` (spec quality gate) | Independent 2nd opinion | 1 | clean | 7/10 on the first dispatch; 12 ambiguities folded into the spec |
| Eng Review | `/plan-eng-review` (via /autoplan) | Architecture & tests (required) | 1 | issues_open | 35 issues (4 false spec claims), 0 critical gaps after revisions |
| Design Review | `/plan-design-review` (via /autoplan) | UI/UX gaps | 1 | issues_open | score: 4/10 → 8/10 (IA), states 2 → 8; 2 taste decisions |
| DX Review | `/plan-devex-review` (via /autoplan) | Developer experience gaps | 1 | issues_open | score: 3/10 → 6/10, TTHW: unreachable from the zip → about 4 min from an aligned BAM |

- **CODEX:** quality gate 7/10 (one dispatch); CEO 15, Design 12, Eng 13, DX 19 concerns, all folded in or raised as gates.
- **CROSS-MODEL:** Claude and Codex agreed on 14 of 25 consensus dimensions; every disagreement was one of degree (Codex harsher). Both independently found: open the zip first, B1's false reuse claim, C6's E07 blockers, labels vs DESIGN.md privacy, and the checkout-based canary risk.
- **VERDICT:** not cleared. Mechanical findings are folded into the spec; the premise gate and UC1-UC4 need Dan's answers before implementation starts. Wave 1 does not depend on UC2-UC4.

**UNRESOLVED DECISIONS:**
- Premise gate P1-P8
- UC1 build wave 1, then re-plan wave 2 from batch evidence
- UC2 replace operator research policies with measure-once 1-bp counts or administrator presets
- UC3 drop the derived metrics from B3
- UC4 defer most of Track D
