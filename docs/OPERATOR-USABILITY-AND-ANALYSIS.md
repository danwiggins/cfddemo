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
| Authority must equal the code-built registry | `local_authority.py:458-467`; `validate_local_authorities` (`local_authority.py:505-525`) refuses any non-reference-ID entry under `ROOT/authority` | yes |
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
| D1 | Where the method enters the job key (B1) | `workflow_release_sha256 = sha256("local-unqualified-v0:" + method_definition_sha256)`; `_is_local_request` accepts the legacy constant OR that form (it checks the `local-` sample-token prefix and that the workflow hash is the legacy constant or is recomputed from the reference and policy the token names) | The field already exists, so `JobRequest` keeps schema v1; old jobs still resume and report |
| D2 | Research sample token | `local-<reference_id>` (built-in, unchanged) or `local-<reference_id>:<policy_id>` (research). `:` cannot appear in a reference ID (`references.py:35`), so parsing is unambiguous | `resume` must know the policy; no new field |
| D3 | Research authority location | `ROOT/research-authority/<reference_id>/<policy_id>/` (same three files and write-once rules as the built-in store), not `ROOT/authority/<ref>+<policy>/` as `algo.md` sketched | `validate_local_authorities` refuses unexpected names under `ROOT/authority` (`local_authority.py:520-523`); a separate directory keeps the built-in path byte-identical and untouched |
| D4 | Policy file format | Canonical `FragmentMeasurementPolicyV2` bytes with `approval_state=unapproved_local`, at `ROOT/policies/<policy_id>/fragment-policy.json` plus `pins.json` (sha256), 0600, write-once | No schema bump; the measurement already validates any contiguous bins (`contracts.py:198-208`) |
| D5 | Single-record histogram source (C1) | A new typed, non-persisted `LocalRecordView` built per request from the catalog-verified bundle, at `GET /api/v1/records/{result_id}`. The explorer artifact's `fragment` stays `None` for one record | E07 needs two distinct results and a v2 manifest (see Current state); a per-request view avoids a digested contract change |
| D6 | Pair comparison (C6) | Built at request time from two catalog-verified bundles. Same `method_definition_sha256`: comparable, delta in percentage points shown. Different definitions: side by side, no delta, a banner names the difference. Nothing persisted | Matches `algo.md` section 3; no stored decision to migrate |
| D7 | Styled report (C7) | The signed `report.html` stays byte-identical. The styled report is a serve route, `GET /records/{result_id}/report`, printable, built from the same `LocalRecordView` | `bundles.py:547` would refuse every existing record if the signed report changed |
| D8 | Labels (A4b) | Unsigned operator labels in `ROOT/labels/<record_id>.json` (`{"label": str, "set_at": ISO-8601}`), 1-80 printable characters, no `/`, `\` or control characters; replaceable with `traceback label RECORD_ID "text"`. Shown as "label (operator note, not part of the signed record)" | A label inside signed bytes would change every record's digest; labels are for the operator's eyes only |
| D9 | Failure reason (A3) | Show the existing `last_error` (`CODE: summary`) in `status` and `logs`; map CODE to CAUSE/FIX from one table in `traceback_runner/problems.py` | No DB migration; the code is already persisted |
| D10 | Unaligned BAM (A1) | Detect "no `@SQ` lines" before opening records; refuse with new TBX-BAM-003, exit 3, and print a `minimap2` + `samtools` command. Do not align for the operator | Alignment choices (preset, reference) are scientific decisions; printing the command is honest and cheap |
| D11 | Modification check (A5) | Accept a model declared in any `@RG DS` field as `modbase_models=<id>` (Dorado's header form) or in the traceback `@PG DS`. With valid MM/ML/MN tags and no declaration: WARN "tags present, model not declared", never "re-basecall" | The current advice is wrong for every real Dorado BAM |
| D12 | `preflight` without `--reference` | If ROOT has at least one registered reference, refuse with new TBX-REF-004 (exit 2) listing the IDs; with none, keep the synthetic default | Silent synthetic default always blocks a real BAM with a misleading contig error |
| D13 | Failed-run cleanup (A8) | On TERMINAL_FAILURE, delete the sealed input copy under `ROOT/runner/` and keep the job row and stage outputs; `traceback clean --failed --root R` removes copies left by earlier versions | A failed run's 1x-input copy has no further use; the job row still explains the failure |
| D14 | cli.py split timing (D10) | After B1, D1 and D2 (three small `cli.py` fixes), before every other A/B/C item that touches `cli.py`. Move-only: no behaviour change, tests unchanged | 14 of the 32 items below touch `cli.py`; splitting first turns one serial queue into four lanes (see "Lanes") |
| D15 | Code family rename (D4) | `TBX-AUTH-LOCAL-001/002` become `TBX-AUTHORITY-001/002`; `TBX-INTERNAL` becomes `TBX-INTERNAL-001`. The guide keeps the old IDs as anchors that point to the new rows | Old job rows may hold the old strings in `last_error`; the anchors keep links working |
| D16 | Second analysis family | Out of scope (cell-origin first, trigger below) | 2-4 engineer-weeks (`algo.md` 1c); research mode covers parameter changes now |
| D17 | Derived fragment metrics (B3) | Computed in `LocalRecordView` from integer counts, only for 1-bp policies, never stored, never signed | No measurement schema bump; values are reproducible from the signed counts |
| D18 | Compatibility for same-definition local pairs (C6) | Delta allowed when both records share `method_definition_sha256` and reference | Differences between two unqualified records under one exact method are descriptive; the label stays "unqualified" |

## Child items

Effort is human days / CC time. Every item is one PR, and each item's acceptance criteria test only that item's own behaviour; where a later item extends a test, the later item says so.

| # | Title | Priority | Effort | Depends on |
|---|---|---|---|---|
| B1 | Job key includes the method | Critical | 0.5 d / 30 min | none |
| D1 | Crash-safe provenance HMAC key | High | 0.5 d / 20 min | none |
| D2 | Delete `_stage_heartbeat` | High | 0.5 d / 20 min | none |
| D10 | Split `cli.py` (move only) | High | 2 d / 1.5 h | B1, D1, D2 |
| A1 | Unaligned BAM detection + alignment guide | Critical | 1 d / 1 h | none (preflight.py) |
| A2 | Up-front input checks in `run` | High | 1 d / 45 min | D10 |
| A3 | JOB_ID on BLOCKED; reason in status and logs | High | 1.5 d / 1 h | D10 |
| A4a | `traceback jobs` and `traceback catalog list` | High | 1.5 d / 1 h | D10 |
| A4b | `run --label` and `traceback label` | High | 1.5 d / 1 h | A4a |
| A5 | Modification check for real Dorado BAMs | High | 1 d / 45 min | none (preflight.py) |
| A6 | Contig-mismatch diff; `preflight` reference rule | Medium | 1 d / 45 min | A1 (same file) |
| A7 | State-aware retry/pause refusals | Medium | 0.5 d / 30 min | D10, A3 |
| A8 | Clean up failed-run copies | Medium | 1 d / 45 min | D10 |
| A9 | Human-readable output and papercuts | Medium | 1.5 d / 1 h | D10, A3 |
| B2a | Policy store, `policy add` / `policy show` | High | 1.5 d / 1.5 h | B1, D10 |
| B2b | `run --policy`, research authority, labelling | High | 2.5 d / 2.5 h | B2a |
| B3 | 1-bp policy preset + derived descriptive metrics | Medium | 1 d / 45 min | B2b, C1 |
| B4 | Single source for policy text; baseline regeneration | Medium | 1 d / 45 min | B2a |
| C1 | `LocalRecordView` + histogram (SVG + table) | Critical | 2.5 d / 2 h | none |
| C2 | Typed renderers replace JSON `<pre>` | High | 1.5 d / 1 h | C1 |
| C3 | Plain-language state copy; preflight checks shown | High | 1 d / 45 min | C1 |
| C4 | Denominator strip and stat tiles | High | 1 d / 45 min | C1 |
| C5 | Catalog record table, labels, checkbox compare; jobs disclosure | High | 2 d / 1.5 h | C1, A4b |
| C6 | On-demand pair comparison | High | 2.5 d / 2 h | C1, C5 |
| C7 | Styled printable report route; strip "Validated" | Medium | 1 d / 45 min | C1, C4 |
| D3 | One set of filesystem write-once helpers | Medium | 2 d / 1.5 h | D1 |
| D4 | Code grammar and exit/retryable consistency | Medium | 1.5 d / 1 h | D10 |
| D5 | v2 envelope for every local command | Medium | 1 d / 45 min | D10 |
| D6 | Authority rigidity documented and named | Low | 0.25 d / 15 min | B2b |
| D7 | Test globals and flake candidates | Medium | 1 d / 45 min | none |
| D8 | End-to-end lease-loss test | Medium | 1.5 d / 1 h | D2 |
| D9 | Doc drift | Low | 1 d / 45 min | A9, B2b |
| DoD | Usability acceptance run | Critical | 1 d / 1 h | all above |

### Track A: running files

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
  (`-T`/`-y` carry the modification tags through alignment.)
- Replace the bare `except Exception` at `preflight.py:383` with
  `except (OSError, ValueError)` for TBX-BAM-001; anything else propagates to
  the CLI's internal-error path. Keep the redacted message (no input paths).
- An empty BAM (zero records after the header) returns new `TBX-BAM-004`
  "BAM has no alignment records", BLOCKED.
- `docs/OPERATOR-GUIDE.md`: new section "Aligning MinKNOW output" with the
  command, how to merge `bam_pass/*.bam` (`samtools cat -o all.bam bam_pass/*.bam`),
  expected time per GB measured once on the operator's workstation (the PR records the Mac model, chip, macOS version and the `time` output), and a serial batch
  loop (`for f in *.sorted.bam; do traceback run "$f" ...; done`). `doctor`
  reports `minimap2` presence as WARN when absent (never blocks).

Acceptance:
1. A generated unaligned BAM (fixture extension in `traceback_runner/fixtures.py`, 0 `@SQ`, 50 reads) gives TBX-BAM-003, exit 3, and the printed command contains `minimap2 -ax map-ont`.
2. A header-only BAM gives TBX-BAM-004, exit 3; `run` on it never creates a job.
3. A truncated BAM still gives TBX-BAM-001.
4. A `RuntimeError` raised inside the scan reaches exit 7 (TBX-INTERNAL-001 after D4), not TBX-BAM-001.
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
- `status`: for TERMINAL_FAILURE and RETRYABLE_FAILURE, the headline is `FAILED: <CODE> <summary>` (or `RETRYABLE: ...`) from `last_error`, and data gains `failure: {code, summary, cause, fix}`. CAUSE and FIX come from `traceback_runner/problems.py` `PROBLEM_TABLE: dict[str, ProblemText]` (new; one row per code; also used by the guide test). A `last_error` without a code shows `code: null` and the raw summary.
- `logs`: same `failure` block, and timestamps as ISO-8601 UTC strings, not epoch floats.
- Exit code of `status` stays 0 (the query succeeded).

Acceptance:
1. A run refused by TBX-RUN-005 (empty eligible set) prints its JOB_ID; `traceback status JOB_ID` prints `FAILED: TBX-RUN-005`, cause and fix.
2. A test asserts every code literal in `traceback_runner/` and `evidence_inspector/` (regex `TBX-[A-Z]+(-[A-Z]+)?(-[0-9]{3})?`, which also catches the pre-D4 `TBX-AUTH-LOCAL-*` and `TBX-INTERNAL` forms) has a `PROBLEM_TABLE` row and a guide anchor. Codes built from f-strings are listed explicitly in the test.
3. Two concurrent `run` calls on one BAM: the second exits 3 with TBX-JOB-002 and a JOB_ID.

Tests: +5. Rollback: revert; no stored data changes.

#### A4a. `traceback jobs` and `traceback catalog list`

Change:
- `traceback jobs --root R [--json] [--limit N=20]`: newest first; columns JOB_ID (12 chars), state word, reference, policy (`built-in` or ID), label (A4b), started (local time, minutes), failure code if any. Reads the store read-only.
- `traceback catalog list --root R [--json]`: columns RECORD_ID (12 chars), label, reference, policy, eligible (thousands separators), imported (date), verification. A record under `ROOT/records/` that is not imported shows `not imported` with the import command.
- Both print `No jobs yet.` / `No records yet.` with the next command when empty.

Acceptance:
1. With 3 records (2 imported), `catalog list` prints 3 rows and one `not imported`.
2. `--json` rows contain no absolute paths.
3. `jobs` on a ROOT without `runner/runner.sqlite3` exits 0 with "No jobs yet".

Tests: +6. Rollback: revert.

#### A4b. Labels: `run --label` and `traceback label`

Change (D8): `run --label TEXT` writes `ROOT/labels/<record_id>.json` after publish (temp file + fsync + `os.replace`, 0600; last writer wins, no lock). `traceback label RECORD_ID TEXT --root R` sets or replaces it. `catalog list`, `jobs`, the site (C5) and the record view (C1) show it with the "operator note, not part of the signed record" qualifier. Labels never enter bundles, exports, support bundles or `--json` of `verify`.

Acceptance:
1. Label grammar: 1-80 characters, Unicode printable, no `/`, `\`, NUL or control characters; violations exit 2.
2. Setting a label leaves every byte under `ROOT/records/<id>/` unchanged (tree sha256 before = after).
3. The guide says: do not put donor names or identifiers in labels.

Tests: +4. Rollback: revert; delete `ROOT/labels/`.

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
- D12: `preflight` without `--reference` on a ROOT with registered references exits 2 with new TBX-REF-004 listing the IDs.

Acceptance:
1. A `chr1`→`1` renamed fixture: diff printed, reheader hint present.
2. A GRCh37-length fixture: diff printed, no reheader hint.
3. No `--reference`, one registered reference: TBX-REF-004, exit 2, ID listed.

Tests: +3. Rollback: revert.

#### A7. State-aware retry and pause refusals

Change: `retry` and `pause` (`cli.py:2257-2279`) check state first. COMPLETE: new `TBX-JOB-003` "job is complete; nothing to retry/pause", exit 3, FIX names the record. TERMINAL_FAILURE: new `TBX-JOB-004` "job failed terminally; fix the input and run it under a fresh ROOT", exit 3, with the stored failure code. The generic "synthetic runner action failed" (`cli.py:3105`) is reserved for genuinely unexpected errors and exits 7.

Acceptance: retry on complete, retry on failed, pause on complete each give their code and exit 3 (+3 tests). Rollback: revert.

#### A8. Clean up failed-run copies

Change (D13): when a local job reaches TERMINAL_FAILURE, the CLI removes the sealed input files that `_sealed_local_names` (`cli.py:2280`) lists for that job (the BAM and index copies only) and appends `input_removed` to the job log. If deletion fails, it logs `input_remove_failed` with the OS error name, leaves the job state unchanged, and `clean --failed` retries. New `traceback clean --failed --root R [--dry-run]` removes copies of failed jobs left by earlier versions, prints bytes freed, never touches COMPLETE jobs, RETRYABLE jobs or `ROOT/records/`.

Acceptance:
1. After a TBX-RUN-005 failure, the job's sealed BAM is gone and `status` still shows the failure.
2. `clean --failed --dry-run` lists without deleting; a real run frees the listed bytes.
3. A RETRYABLE job's copy survives `clean --failed`; `resume` still works.

Tests: +4. Rollback: revert; deleted copies are re-creatable by re-running.

#### A9. Human-readable output and papercuts

Change:
- Human output never prints one-line JSON: `doctor` checks render as an aligned `STATUS NAME detail` table; `logs` lines as `ISO-time stage message`.
- Word `PARTIAL` instead of "PASS ... partial".
- `run` reuse: when `submit` returns an existing COMPLETE job, print "Reused existing record <id> from job <job_id>; nothing was re-measured" and skip the "STAGE seal: copying" line.
- `NEXT_COMMANDS` use the guide's `verify` spelling.
- Guide: reference register takes seconds, not minutes; `RECORD_ID=$(ls R/records)` replaced by `traceback catalog list --json`; a "disk: each run adds about 1x the input under ROOT" note; lock message and its code in troubleshooting.
- Web jobs list: a finished, verified job no longer says "Runner status is stale" (state word from C3).

Acceptance: a golden-file test per command for human output (`doctor`, `logs`, `run` reuse), with paths and times normalised; no `{` at the start of any human-output line in those tests. +5 tests. Rollback: revert.

### Track B: changeable analysis

#### B1. Job key includes the method (CRITICAL)

Root cause: the runner deduplicates on the canonical request (`store.py:449-466`), and the local request names no method (`cli.py:1636-1643`). A run under a changed method returns the old job and its old record (exit 0, old bins).

Change (D1):
- `_local_workflow_sha256(method_definition_sha256: str) -> str` returns `sha256(("local-unqualified-v0:" + method_definition_sha256).encode("ascii"))`. `_run` passes `method_definition_sha256(local_method_definition(loaded.registered))` (B2b passes the research definition's).
- `_LEGACY_LOCAL_WORKFLOW_SHA256` keeps today's constant.
- `_is_local_request(request, root)`: true when the token starts with `local-` and the workflow hash is the legacy constant or equals `_local_workflow_sha256` of the definition the token names (reference, plus policy after B2b). If the reference cannot be loaded, `status` labels by prefix only and `resume` refuses with TBX-REF-002 as today.
- After upgrade, re-running a BAM that ran under the legacy key creates one new job (one re-measure, one sealed copy); `_publish_verified_record` reuses the existing record directory when the manifest is identical (`cli.py:902-907`).

Acceptance:
1. Same BAM, two different method definitions on one ROOT (test monkeypatches `local_method_definition` to return a second definition and bypasses the authority check): two job IDs. B2b adds the end-to-end version with a research policy.
2. Same BAM, same method, twice: one job ID, A9's reuse line.
3. A job row written with the legacy constant (fixture DB) still reports `local_unqualified: true` in `status` and resumes.
4. `JobRequest` schema stays `traceback.job-request.v1`.

Tests: +4 in `tests/test_run_local.py`. Rollback: revert; jobs written under the new key stay valid because `_is_local_request` is additive.

#### B2a. Policy store, `policy add` and `policy show`

Change (D4):
- New `traceback_runner/policies.py`: `add_policy(root, policy_id, reference_id, min_mapq, bin_edges) -> Path`, `load_policy(root, policy_id) -> FragmentMeasurementPolicyV2`, `list_policies(root)`.
- Policy ID grammar = reference ID grammar (`^[a-z0-9][a-z0-9._-]{0,63}$`), and `builtin` is reserved.
- `traceback policy add --id ID --reference REF --min-mapq N --bins 0,100,150,...,1000 --root R`. Closed flags only; no free-text fields. Bins: strictly increasing integers, first `>= 0`, at most 4,095 edges (explorer limit 4,096 bins), final bin unbounded (added automatically).
- `--bins` lists bounded edges; the final unbounded bin starts at the last edge and is added automatically.
- Files: `ROOT/policies/<id>/fragment-policy.json` (canonical bytes, `definition_id = "<id>.<reference_id>"`, `approval_state = unapproved_local`) and `pins.json` (canonical JSON `{"policy_sha256": "<hex>", "schema_version": "traceback.policy-pins.v1"}`). Write-once: an existing ID with different bytes exits 3 with new `TBX-POL-001` "policy ID already used with different settings; choose a new ID".
- `traceback policy show ID|builtin --root R [--json|--markdown]` prints MAPQ, bins and a plain sentence ("counts each eligible primary alignment's aligned reference span; excludes unmapped, secondary, supplementary, QC-fail and duplicate"). `--markdown` is B4's single source.
- `traceback policy list --root R`.

Acceptance:
1. `policy add` then `policy show --json` round-trips the exact bins and MAPQ.
2. Re-adding the same settings exits 0 (idempotent); different settings give TBX-POL-001.
3. `--bins 0,100,100` and `--min-mapq 256` exit 2.
4. Adding a policy changes no byte under `ROOT/authority/` or `ROOT/records/`.

Tests: +8. Rollback: revert; delete `ROOT/policies/`.

#### B2b. `run --policy`, research authority and labelling

Change (D2, D3):
- `run --policy ID`: loads the policy; refuses `TBX-POL-002` if its reference differs from `--reference`.
- Definition: `definition_id = "<policy_id>.<ref>"`, method version `1.0.0-local-<ref>-<policy_id>` (accepted by `MethodVersion` today; add a test), `parameter_schema_sha256 = sha256(policy bytes)`.
- Research authority: `ROOT/research-authority/<ref>/<policy_id>/` built and validated by the same functions as the built-in store, parameterised by policy. `catalog import` and `serve` validate every research store too; a damaged one refuses with TBX-AUTHORITY-002 (D4 rename) naming the policy.
- Labelling: run output, `LocalRecordView`, the catalog row and the signed `report.html` say "research policy `<id>` (operator-supplied, unqualified)". The signed report text changes only when `definition_id` is not the built-in one, so built-in records stay byte-identical.
- Canary: `scripts/canary/real_bam_canary.py --policy ID` records and checks a baseline per policy (`baseline-<policy_id>.json`). The 03:30 nightly canary keeps checking only the built-in path unless a policy baseline exists.
- Without `--policy`, every byte written under `ROOT/records/`, `ROOT/authority/` and the catalog is identical to `e76e9d0` output for the same inputs. (The job row differs because of B1's key; that is expected.)

Acceptance:
1. Golden test: built-in run on the generated BAM yields the same record tree sha256 before and after this PR (pinned in the test).
2. A research run on the same BAM yields a different record, which `verify`, `catalog import` and `serve` accept, with method version `1.0.0-local-ref-<id>`.
3. Removing a byte from a research authority file makes `serve` exit 3 with TBX-AUTHORITY-002; the built-in store is unaffected.
4. `resume` of an interrupted research job resumes under the same policy (token parse).

Tests: +10. Rollback: revert; delete `ROOT/research-authority/` and research records (built-in ROOTs are untouched).

#### B3. 1-bp policy preset and derived descriptive metrics

Change (D17):
- `policy add --preset one-bp --id ID --reference REF` = MAPQ 20, edges `0,1,2,...,1000` (1,001 bins incl. `1000+`).
- `LocalRecordView` (C1) adds, only when every bin below 1,000 is 1 bp wide:
  - `short_fraction`: count of spans in [100, 150) divided by count in [100, 220), with numerator and denominator shown;
  - `periodicity_10bp`: let `c[k]` be the counts for spans `60 + k`, `k = 0..89`, and `N = sum(c)`. `F(f) = |sum_k c[k] * exp(-2*pi*i*f*k)|`. The index is `F(1/10) / N`, rounded half-even to 3 decimals, with the window [60, 150) shown. `N = 0` gives `null` and the text "no eligible alignments in 60-149 bp". Same zero rule for `short_fraction`.
  Both are labelled "descriptive; not a diagnostic or a validated metric" and carry their formula as text.
- The chart for 1-bp policies draws a line over 1-bp densities, with the built-in bins as optional gridlines.

Acceptance: on a synthetic fixture with a planted 10-bp oscillation the index exceeds the flat fixture's by at least 5x; integer inputs give identical outputs across runs; the values never appear in signed bytes (grep the bundle). +4 tests. Rollback: revert.

#### B4. Single source for policy text; baseline regeneration

Change: `docs/OPERATOR-GUIDE.md` and `docs/ALGORITHMS.md` include the built-in policy as a fenced block generated by `traceback policy show builtin --markdown`; `tests/test_policy_docs.py` asserts the blocks equal the command's output. New `scripts/regenerate_canary_baseline.py --synthetic` rewrites `tests/fixtures/canary/` baselines and prints the diff; the PR template line says when to run it.

Acceptance: editing a bin edge in code without regenerating fails the docs test with the exact command to run (+2 tests). Rollback: revert.

### Track C: an interpretable site

Layout target (from `ui.md`): single-record view, in order: header with "Development record · not for clinical use"; stat tiles; histogram; denominator strip; "What this record is"; "How to read it (descriptive, not diagnostic)"; collapsed exact values and identities. No reference band, threshold or "normal" range anywhere.

#### C1. `LocalRecordView` and the histogram

Change (D5):
- New `traceback_runner/web/records.py`: `LocalRecordView` (Pydantic, not persisted, not digested): `result_id`, `record_id`, `label | None`, `reference_id`, `policy: {id, builtin: bool, min_mapq, bins}`, `method_version`, `records_scanned`, `eligible_alignments`, `exclusions: [{reason, count}]`, `histogram: [{lower, upper | None, count}]`, `preflight: {outcome, checks: [{code, outcome, summary}]}`, `states: [{axis, token, label, meaning}]` (C3), `derived` (B3).
- Built per request from `catalog.verify_reference` (re-verifies signature and bytes each time; 1 record = a few KB of JSON).
- Route `GET /api/v1/records/{result_id}` under the operator session (same auth as the explorer routes; 404 TBX-WEB-404 for unknown IDs).
- `app.js`: an SVG histogram drawn with the `svg()` helper moved from `longitudinal.js:256-387` into `static/chart.js`. Variable-width bars, area = share of eligible (y = share / bin width in bp). The open `1000+` bin is hatched and drawn from 1,000 to 1,200 bp with height share / 200 as a display convention; its tooltip and table row say "open bin, width not defined; share is exact". Then x ticks at bin edges, axis titles "Aligned reference span (bp)" and "Share of eligible alignments per bp", each bar labelled `n` and `%`, the bin with the largest count annotated "most common bin" (by count, not by height). Caption: "n = 3,435,813 eligible alignments; policy built-in (MAPQ >= 20)". An HTML table with the same rows follows the chart.

Acceptance:
1. API: the view's histogram counts equal the signed measurement's, and sum to `eligible_alignments`.
2. A tampered bundle (one count edited) makes the route return HTTP 503 with TBX-WEB-503 and no counts.
3. DOM harness (`tests/web/app_dom_harness.js`): one `<svg>` with N `<rect>` for N bins, the hatched final bar, and a table with N rows.
4. Screenshot evidence at 1280 px and 390 px attached to the PR (no horizontal scroll at 390 px).

Tests: +6 Python, +3 DOM. Rollback: revert; nothing stored.

#### C2. Typed renderers replace JSON `<pre>`

Change: `displayValue`/`renderArtifact` (`app.js:29-42`) are replaced by per-kind renderers (E07 comparison table, E11 provenance key/value list, E10 portable table). Panels whose artifact is absent for local records (E08, E09, E13) are not rendered at all; a single line says "Cell-origin, copy-number and sensitivity analyses are not part of local records." "Displayed" and "Eligible" merge into one "Eligible alignments" column.

Acceptance: no `<pre>` in the rendered single-record or compare views (DOM test); no "missing" text for local records (+3 DOM tests). Rollback: revert.

#### C3. Plain-language state copy

Change: `traceback_runner/web/state_copy.py` holds one row per enum value shown in the UI: qualification (`development_unqualified` = "Not qualified: a development measurement, not checked against any approved method"), trust (`development_signature_verified`), display role (`research_baseline`), reference match (`name_and_length_only`), preflight outcomes, comparison outcomes, job states. `LocalRecordView.states` carries `{axis, token, label, meaning}`. The site's "What this record is" table renders one row per axis. Preflight WARN/PARTIAL checks appear as a list with their codes. The jobs list uses the same job-state words (fixes "stale" for finished jobs).

Acceptance: a test enumerates every member of the enums used in `LocalRecordView` and asserts a row exists; no raw token appears as visible text outside the "exact values" disclosure (DOM test). +3 tests. Rollback: revert.

#### C4. Denominator strip and stat tiles

Change: tiles: "Eligible of scanned" (`3,435,813 of 5,822,296 (59.0%)`), "Most common bin", "Share 150-200 bp", "Share over 1 kb". Strip: scanned → each exclusion reason with count and % of scanned → eligible, as one horizontal bar plus a list. Numbers use thousands separators and one-decimal percentages.

Acceptance: tiles and strip reconcile (exclusions + eligible = scanned) in a DOM test; the 150-200 share on the real BAM is reported in the PR as manual evidence. +2 tests. Rollback: revert.

#### C5. Catalog record table, labels, checkbox compare; jobs disclosure

Change: the landing view is a table: record (label, else short ID), reference, policy, eligible, scanned, preflight (word + warning count, expandable), imported, a compare checkbox. Method ID and version become `<select>` filters populated from the catalog. Selecting exactly 2 enables "Compare"; no comparison is shown by default. Jobs collapse to one `<details>` line ("Last job finished and verified; no job is running"). At 390 px the table becomes stacked cards.

Acceptance: DOM tests for 0, 1, 2 and 3 checked rows (button disabled, disabled, enabled, disabled); labels shown with the "operator note" qualifier; no free-text filter inputs. +5 tests. Rollback: revert.

#### C6. On-demand pair comparison

Change (D6, D18): `GET /api/v1/records/compare?left=..&right=..` builds a `FragmentExplorerView` at request time from two catalog-verified bundles. Prerequisite inside this item: `fragment_source_from_verified_bundle` (`fragment_explorer.py:381-400`) accepts `ResultBundleManifestV3` as well as V2 (a widened union; existing V2 bytes and digests unchanged; a test pins one V2 view digest). Same `method_definition_sha256` and reference: comparable; overlaid density outlines and a bin table with A, B and B−A in percentage points. Different definitions or references: two separate charts, no delta, a banner "Different analysis settings: <diff of MAPQ/bins/reference>. Values are shown side by side and not subtracted." The persisted explorer artifacts are not changed.

Acceptance:
1. Two built-in records: `delta_available: true`, B−A rows sum to 0.0 pp ± 0.1.
2. Built-in vs research record: `delta_available: false`, banner lists the differing fields.
3. Same ID twice: 400.
4. The existing `/api/v1/explorer/compare` behaviour is unchanged (existing tests pass).

Tests: +6. Rollback: revert; nothing stored.

#### C7. Styled printable report; strip "Validated"

Change (D7): `GET /records/{result_id}/report` returns a self-contained HTML page (inline CSS and SVG, no scripts) with header, tiles, histogram, strip and the "How to read it" text, plus a print stylesheet. The signed `report.html` is unchanged. `app.py:473` becomes "Recorded AI assessment replay; no provider call" and `app.py:1382` becomes "Measurements".

Acceptance: the page has no `<script>`; it contains "not for clinical use"; `grep -n "Validated" app.py` returns only line 490's "Not built" disclaimer. +2 tests. Rollback: revert.

### Track D: code health

#### D1. Crash-safe provenance HMAC key
Replace `_provenance_hmac_key` (`cli.py:1063-1115`) with the `_local_signing_key` pattern (`cli.py:1116`): write to a temp file, fsync, `link` into place, mode check `== 0o600`. A 0-byte or short existing key is replaced only when ROOT has no records (`ROOT/records/` empty or absent) and no job rows (`ROOT/runner/runner.sqlite3` absent or zero rows); otherwise TBX-RUN-006 with FIX text naming the file. Acceptance: a test that kills after create (simulated by a 0-byte file) on an empty ROOT recovers; on a ROOT with records it refuses (+2). Rollback: revert.

#### D2. Delete `_stage_heartbeat`
Remove `cli.py:1199-1218` and its two uses. Acceptance: a test runs a local stage lasting 3x the lease length on a FakeClock and the job completes through `_LeaseKeeper` alone (+1). Rollback: revert.

#### D3. One set of filesystem write-once helpers
`traceback_runner/filesystem.py` gains `write_private_once(path, bytes, *, repair: bool)`, `read_private_bounded(path, max_bytes)`, `fsync_dir(path)`. Replace the six write-once helpers and five `_fsync_directory` copies (`cli.py:639, 673`, `local_catalog.py:428, 436`, plus those in `runner.py`, `references.py`, `local_authority.py`, `assets.py`; the PR lists each). Crash semantics: `repair=False` refuses differing bytes, `repair=True` atomically replaces (today's `_persist_once`). Acceptance: existing crash-recovery tests pass unchanged; `grep -c "def _fsync_directory"` across the package = 0 (+6 helper tests). Rollback: revert.

#### D4. Code grammar and exit/retryable consistency
D15 renames. `ReferenceProblem.exit_code` becomes an `ExitCode`; TBX-JOB-001 exits one value everywhere (3); retryable problems exit 6 and non-retryable ones never do; `_reject_live_worker` uses `>` like the store. A test walks `PROBLEM_TABLE` and asserts grammar, exit and retryable agree. Problem base class moves to `traceback_runner/problems.py` as `OperatorProblem`; `ReferenceProblem` and `LocalStageRefusal` subclass it. (+4 tests.) Rollback: revert.

#### D5. v2 envelope for every local command
`doctor`, `inspect`, `demo` and `assets` emit `traceback.cli-result.v2` with `data_origin` (`synthetic` or `local`); `_concerns_local_data` (`cli.py:2941`) closes its `JobStore`; `serve --json` prints its startup line as JSON. Acceptance: on a real-BAM ROOT, `doctor --json` never says `synthetic_only: true` (+4). Rollback: revert.

#### D6. Authority rigidity documented
`docs/OPERATOR-GUIDE.md` and `TBX-AUTHORITY-001`'s FIX say: changing built-in constants makes existing ROOTs refuse; use `traceback policy add` instead. No code change to the equality rule. (+0; the guide test covers the text.)

#### D7. Test globals and flake candidates
`tests/test_awake.py` patches `awake._assertion_available`, not `sys.platform`; `tests/test_doctor.py` drops the autouse fixture for explicit per-test patches; `tests/web/test_loopback_server.py` stale-anchor test uses a per-test `tmp_path` anchor directory; `tests/test_internal_errors.py:193-203` joins the watchdog before teardown; `tests/test_canary.py` stubs `launchctl`; every `subprocess.run` in tests has a timeout. Acceptance: `pytest -n 8` of those files 20 times in a loop, 0 failures (recorded in the PR). Rollback: revert.

#### D8. End-to-end lease-loss test
`tests/test_run_local.py`: the CLI's real local stages on the generated BAM, a FakeClock that expires the lease mid-measure, then `traceback resume JOB_ID` produces a verified record identical to an uninterrupted run. Also a TBX-SERVE-004 watchdog-exit test. (+2.) Rollback: revert.

#### D9. Doc drift
Fix the `code.md` doc-drift list: `OPERATOR-GUIDE.md` v2 claim, missing `--index`, catalog DB path; add guide rows for TBX-AUTH-002..006, TBX-WEB-*, TBX-INTERNAL-001 and every new code here; create `docs/operator/privacy.md` (TBX-OUT-001 target) or retarget the code; strip the stale `cli.py:NNN` references from `GOLDEN-PATH-MVP-SLICE.md` and mark `PILOT-SECURITY-HARDENING.md` items landed/not landed. Acceptance: A3's code-coverage test passes; a link checker over `docs/` reports 0 broken intra-repo anchors.

#### D10. Split `cli.py` (move only)
`traceback_runner/cli/` package: `__init__.py` (parser + dispatch + `main`), `run.py` (run, resume, local stages, publish), `jobs.py` (status, logs, pause, retry, jobs), `catalog.py`, `serve.py`, `doctor.py`, `reference.py`, `verify.py`, `assets.py`, `output.py` (`_result`, `_emit`, envelopes). `python -m traceback_runner.cli` and the `traceback` entry point keep working. Acceptance: the full suite passes with only import-path edits in tests; `git diff --stat` shows no logic change (reviewer checks moved blocks with `git diff --color-moved`); the wheel manifest is regenerated. Rollback: revert (one commit).

## Lanes and file overlap

| Lane | Items | Main files |
|---|---|---|
| L0 serial first | B1, D1, D2, then D10 | `cli.py` |
| L1 intake | A1, A5, A6 | `preflight.py`, `fixtures.py`, guide |
| L2 run and jobs | A2, A3, A7, A8, A9, A4a, A4b | `cli/run.py`, `cli/jobs.py`, `cli/catalog.py`, `problems.py`, `runner.py` (A8) |
| L3 analysis | B2a, B2b, B3, B4, D6 | `policies.py`, `local_authority.py`, `local_catalog.py`, `cli/run.py` (B2b only), `scripts/canary/` |
| L4 site | C1-C7 | `web/records.py`, `web/state_copy.py`, `web/static/*`, `web/server.py` (routes), `fragment_explorer.py` (C6), `app.py` (C7) |
| L5 hygiene | D3, D4, D5, D7, D8, D9 | `filesystem.py`, `problems.py`, tests, docs |

Collisions to sequence: A3 and D4 both create `problems.py` (A3 first; D4 extends it). B2b and L2 both edit `cli/run.py` (B2b rebases on A2/A3). C1 and B3 share `web/records.py` (C1 first). D3 touches files in every lane; land it after L2 and L3 settle, or accept mechanical rebases.

## Dependency graph

```
B1 ─┐
D1 ─┼─> D10 ─┬─> A2 ─> A3 ─┬─> A7
D2 ─┘        │             ├─> A9
             │             └─> D4 ─> D5
             ├─> A4a ─> A4b ──────────────┐
             ├─> A8                       │
             └─> B2a ─┬─> B2b ─> B3 <─ C1 │
                      └─> B4              │
A1 ─> A6   A5                             │
C1 ─┬─> C2, C3, C4 ─> C7                  │
    └─> C5 <──────────────────────────────┘
        C5 ─> C6
D3 after L2/L3; D7 any time; D8 after D2; D9 last; DoD after all
```

Sequencing: B1 first because it silently returns wrong records. D1/D2 are tiny `cli.py` edits that would otherwise conflict with the split. D10 before the rest because 14 items edit `cli.py`. L1 and L4 do not touch `cli.py` and start on day 1.

## Definition of done

`scripts/usability_acceptance.sh` (new; CI runs it with generated fixtures, the operator runs it once on real data):
1. From a fresh ROOT: `reference register`; `preflight` on an unaligned fixture BAM exits 3 with TBX-BAM-003 and a `minimap2` command.
2. Three aligned fixture BAMs run with `--label`; `traceback catalog list` shows 3 labelled rows after `catalog import`.
3. A run with a missing `.bai` exits 4 with TBX-RUN-009 and creates no job.
4. `policy add --preset one-bp --id fine`; `run --policy fine` on one BAM produces a fourth, distinct record labelled "research policy fine".
5. `serve`; an operator-session GET of `/api/v1/records/{id}` returns histogram counts that sum to `eligible_alignments` for each record.
6. GET `/api/v1/records/compare` on two built-in records: `delta_available: true`; on built-in vs `fine`: `false` with a banner.
7. A fourth fixture BAM whose reads all have MAPQ 0 runs and fails with TBX-RUN-005, printing its JOB_ID.
8. `traceback jobs` lists 5 jobs (4 complete, 1 failed with TBX-RUN-005); `traceback status <failed JOB_ID>` prints its cause and fix.
Manual evidence on the real data (in the DoD PR, as a hand-written table, no paths): alignment wall time per GB, `run` times, eligible counts, screenshots at 1280 and 390 px.

## Out of scope (follow-ups with triggers)

| Item | Trigger |
|---|---|
| Cell-origin as a second analysis family (bundle kind v4, modkit + Loyfer assets, E08 at import) | Research mode used on at least 3 BAMs and a scientist asks for methylation output |
| `run --batch DIR` | More than 10 BAMs per batch, or the guide loop is used twice in one week |
| De-duplicating preflight inside `run` (21 s per run) | Batches above 10 BAMs, or preflight above 60 s on one BAM |
| Retiring the Streamlit app | C1-C7 merged and the operator site used for one external walkthrough |
| Migration of built-in constants across ROOTs | A change to the built-in policy is required (not a research policy) |
| Folding the ~10 BAM re-hashes per run into one verify | `run` above 5 min on one BAM |
| Signed labels | A record with a label is shared outside the team |

## Rollback

Every item is one PR and reverts cleanly. Stored-data effects: B2a/B2b add `ROOT/policies/` and `ROOT/research-authority/` (deleting them removes research records' authority; built-in records are untouched); A4b adds `ROOT/labels/`; A8 deletes failed-run copies (re-creatable by re-running). B1 changes the key for new jobs only; old rows stay readable. No item changes a signed byte of a built-in record.

## Files reference

| File | Items |
|---|---|
| `traceback_runner/cli.py` → `traceback_runner/cli/*.py` | B1, D1, D2, D10, A2, A3, A4a, A4b, A7, A8, A9, B2a, B2b, D4, D5 |
| `traceback_runner/preflight.py:85-90, 383` | A1, A5, A6 |
| `traceback_runner/fixtures.py` | A1, A5, A6, DoD |
| `traceback_runner/problems.py` (new) | A3, D4 |
| `traceback_runner/runner.py` | A8 |
| `traceback_runner/store.py:449-466` (read only) | B1 |
| `traceback_runner/policies.py` (new) | B2a, B2b, B3 |
| `traceback_runner/local_authority.py:84-85, 159-186, 428-525` | B2b, D6 |
| `traceback_runner/local_catalog.py:245-405, 676-720` | B2b |
| `traceback_runner/export.py:228-270` | B2b (research label only) |
| `traceback_runner/web/records.py`, `web/state_copy.py` (new) | C1, C3, B3 |
| `traceback_runner/web/server.py` (routes) | C1, C6, C7 |
| `traceback_runner/web/static/app.js`, `chart.js` (new), `index.html`, `styles.css`, `longitudinal.js:256-387` | C1-C7 |
| `evidence_inspector/fragment_explorer.py:381-400` | C6 |
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

Human: about 42 days (A: 11.5, B: 6.5, C: 11.5, D: 11.25 incl. D10, DoD 1). With four lanes after D10, about 3 calendar weeks. CC: about 28 h of build time plus review rounds; on this repo review has roughly doubled build time.
