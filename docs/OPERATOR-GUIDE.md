# Traceback operator guide

Status: first development wave; synthetic data, plus one unqualified local-BAM
path (see "Real local BAM (unqualified)"). Nothing here is qualified or for
clinical use.

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
  --trust-store ./traceback-synthetic/trust/development-result-trust.json
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
traceback doctor [--root ROOT] [--deep] [--json]
traceback protocol show [--json]
traceback demo [--root ROOT] [--json]
traceback reference register --fasta FASTA --id ID [--assembly NAME] [--root ROOT] [--json]
traceback preflight INPUT [--reference ID] [--root ROOT] [--json]
traceback run INPUT --reference ID [--index INDEX] [--import] [--label TEXT] [--root ROOT] [--json]
traceback jobs [--limit N] [--root ROOT] [--json]
traceback catalog import RECORD_ID|RECORD_DIR [--root ROOT] [--json]
traceback catalog list [--root ROOT] [--json]
traceback catalog export --csv OUT.csv [--root ROOT] [--json]
traceback label RECORD_ID TEXT [--root ROOT] [--json]
traceback serve [--root ROOT] [--ipv6]
traceback status JOB_ID [--root ROOT] [--json]
traceback status JOB_ID [--root ROOT] --trust-registry REGISTRY_ROOT \
  --trust-registry-id ID --trust-registry-epoch EPOCH \
  --trust-registry-head HEAD [--json]
traceback logs JOB_ID [--root ROOT] [--json]
traceback pause JOB_ID [--root ROOT] [--json]
traceback resume JOB_ID [--root ROOT] [--json]
traceback retry JOB_ID [--root ROOT] [--json]
traceback inspect BUNDLE [--json]
traceback verify BUNDLE --trust-store TRUST_STORE [--json]
traceback verify RECORD_ID --root ROOT [--json]
traceback verify BUNDLE --trust-registry REGISTRY_ROOT \
  --trust-registry-id ID --trust-registry-epoch EPOCH \
  --trust-registry-head HEAD [--json]
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

`verify --trust-registry` checks a result bundle against the current trust of a
protected result-trust registry (`docs/RESULT-TRUST-REGISTRY.md`). It needs the
independently retained registry ID, epoch, and current head, refuses an older
head, and never creates a registry. A key revoked in the registry fails
verification. `assets` commands verify release signatures and take
`--trust-store` only; the result-trust registry holds result keys only.

`status` reports `trust_state` (`verified`, `not_verified` or `unknown`) and
the `trust_source` it used, and always exits 0 for a known job:

| `trust_source` | Meaning |
|---|---|
| `trust_registry` | Checked against the registry's current trust (with the same pins as `verify`). A registry revocation reaches it. |
| `trust_registry_error` | The registry did not open at the given ID, epoch and head (an older head is refused). State is `not_verified`. |
| `development_file` | Checked against the fixed `ROOT/trust/development-result-trust.json`. Registry revocations do not reach this file. |
| `development_file_error` | That file exists but could not be read. State is `not_verified`. |
| `none` | No trust is available under ROOT and no registry was given. State is `unknown`. |
| `no_record` | The job has no complete signed record yet. State is `not_verified`. |

`preflight` is technical inspection only. A passing report does not approve
real-data execution. `run` without `--reference` refuses (TBX-RUN-003);
`run --reference` makes an unqualified local record (see "Real local BAM
(unqualified)"). File-selection arguments may be local paths; JSON output,
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

## Real local BAM (unqualified)

The golden path (`docs/GOLDEN-PATH-MVP-SLICE.md`) takes one local BAM through a
locked fragment-length measurement to a signed record, a local catalog and a
loopback browser view. Every record made this way is unqualified, local, not
for clinical use, and signed with a development key only. Nothing is uploaded
and nothing here is a qualified method, reference or workflow.

Two rules apply before any real data enters this path:

- Write down the source BAM's provenance and consent before any record from it
  is shown to anyone outside the team.
- No donor data until the pilot security items H1 and H6-min have landed
  (`docs/PILOT-SECURITY-HARDENING.md`).

### Prerequisites

- `uv sync` in this repository (commands below run as `uv run traceback ...`).
- `brew install samtools` (only to index your inputs; `doctor` warns if absent).
- `brew install minimap2` (only to align MinKNOW output, below; `doctor` warns
  if absent and never blocks).
- An uncompressed FASTA with its `.fai` beside it (`samtools faidx REF.fa`).
- A coordinate-sorted BAM with its `.bai` beside it (`samtools index SAMPLE.bam`).
  MinKNOW and Dorado write unaligned BAMs; align them first (next section).
- Free space on ROOT's volume of at least twice the BAM plus index: `run`
  seals a copy of the input under ROOT.

### Aligning MinKNOW output

Alignment is an assisted prerequisite, outside traceback: traceback prints the
command but never aligns for you, because the preset and the reference are
scientific choices. `preflight` and `run` refuse an unaligned BAM (no `@SQ`
lines) with TBX-BAM-003 and print this command; `preflight --reference ID`
fills in the registered FASTA in its human output.

```bash
samtools fastq -T MM,ML,MN IN.bam \
  | minimap2 -ax map-ont -y REF.fa - \
  | samtools sort -o OUT.sorted.bam
samtools index OUT.sorted.bam
```

`-T MM,ML,MN` and `-y` carry the modification tags through alignment. The
aligned BAM has no `@RG` header line, so `preflight` reports TBX-MOD-001 WARN
("basecall model not declared"); that needs no action for fragment length.
Add `-t N` to minimap2 to use N threads (default 3).

Measured once (Apple M5 Pro, 18 cores, 48 GB, macOS 26.4.1; minimap2 2.31,
samtools 1.24; hg38 primary FASTA) on one 3.3 GB unaligned BAM of 3.9 M
reads, wall time including `samtools index`: about 18 minutes with the
command as printed (about 5.5 min per GB), about 9 minutes with `-t 16`
(about 2.8 min per GB). minimap2 rebuilds the hg38 index each time, which is
several minutes of that; `minimap2 -d REF.mmi REF.fa` once, then `REF.mmi` in
place of `REF.fa`, skips it. Peak memory was about 8.5 GB.

**Merge per barcode only.** A MinKNOW run writes many small BAM chunks per
sample under `bam_pass/barcodeNN/` (barcoded runs) or straight under
`bam_pass/` (one sample). Merge one sample's `bam_pass` chunks into one BAM
before aligning:

```bash
samtools cat -o SAMPLE.bam bam_pass/barcode01/*.bam
```

Never merge across barcode directories (`bam_pass/*/*.bam`): that mixes
samples into one record. Leave out `bam_fail/` (failed reads; its chunks are
often empty and give TBX-BAM-004) and `unclassified/`.

The block below aligns and runs every barcode of one MinKNOW run, one after
another. A barcode whose merge, alignment, index or run fails prints
`FAILED barcodeNN` and the loop goes on with the next one. It uses
`FASTA` and `R` from the variables below, the `ref` ID from the journey, and
two more variables:

```bash
MINKNOW_RUN=/path/to/minknow-run   # holds bam_pass/barcodeNN/*.bam
ALIGNED=/path/to/aligned           # merged and aligned BAMs go here
```

The test suite runs this block verbatim on generated chunks
(`tests/test_operator_guide.py`).

<!-- minknow-batch:begin -->
```bash
mkdir -p "$ALIGNED"
for d in "$MINKNOW_RUN"/bam_pass/barcode*/; do
  s="$(basename "$d")"
  { samtools cat -o "$ALIGNED/$s.bam" "$d"*.bam \
    && (set -o pipefail
        samtools fastq -T MM,ML,MN "$ALIGNED/$s.bam" \
          | minimap2 -ax map-ont -y "$FASTA" - \
          | samtools sort -o "$ALIGNED/$s.sorted.bam") \
    && samtools index "$ALIGNED/$s.sorted.bam" \
    && uv run traceback run "$ALIGNED/$s.sorted.bam" --reference ref --root "$R"
  } || echo "FAILED $s"
done
for record in "$R"/records/*/; do
  uv run traceback catalog import "$record" --root "$R"
done
```
<!-- minknow-batch:end -->

Each `run` prints its `RECORD_ID`; note which barcode each one came from
(records carry no sample label yet). Importing a record twice is a no-op.

Set three variables. ROOT (`R`) holds everything this path writes; use a fresh
directory per experiment.

```bash
FASTA=/path/to/REF.fa          # with REF.fa.fai beside it
BAM=/path/to/SAMPLE.bam        # coordinate-sorted, with SAMPLE.bam.bai beside it
R="$HOME/traceback-local"      # ROOT
```

### Copy-paste journey

The test suite runs this block verbatim on a generated FASTA and BAM
(`tests/test_operator_guide.py`), so it stays in step with the CLI.

<!-- golden-path-journey:begin -->
```bash
uv run traceback doctor --root "$R"
uv run traceback reference register --fasta "$FASTA" --id ref --root "$R"
uv run traceback preflight "$BAM" --reference ref --root "$R"
uv run traceback run "$BAM" --reference ref --root "$R"
# A fresh ROOT holds exactly one record; otherwise copy the RECORD_ID run printed.
RECORD_ID="$(ls "$R/records")"
uv run traceback verify "$RECORD_ID" --root "$R"
uv run traceback catalog import "$R/records/$RECORD_ID" --root "$R"
```
<!-- golden-path-journey:end -->

Then serve the catalog to your own browser (it stays in the foreground):

```bash
uv run traceback serve --root "$R"
```

### What each step prints

1. **`doctor`** prints the resolved absolute ROOT and checks Python, samtools,
   registered references, free disk and trust. `PASS ... (1 warning)` is normal
   on a fresh ROOT. It exits 3 only when the local runtime cannot run.
2. **`reference register`** reads the FASTA once (SHA-256 and per-contig `M5`;
   minutes for a full human reference) and prints
   `PASS  Reference ref registered (N contigs); unqualified, local`. The
   registration is write-once. Add `--assembly NAME` only if the BAM's `@SQ AS`
   should be compared; without it the ID is used as a label and never shown as
   an assembly name.
3. **`preflight`** inspects the BAM against the registered reference and
   processes nothing. Its overall outcome is the worst check outcome:
   - `pass`: every check passed.
   - `warn`: the BAM header has no `M5`/`AS`, so it matched the reference by
     contig name and length only (TBX-BAM-002 WARN); or the reads carry valid
     `MM`/`ML`/`MN` tags but the header declares no modified-base model
     (TBX-MOD-001 WARN, normal after the alignment command above). The run
     continues and the report says `name_and_length_only`.
   - `partial`: modification tags (`MM`/`ML`/`MN`) are absent or
     contradictory (TBX-MOD-001/002). Fragment measurement continues; only
     future methylation work is ineligible. Most aligned BAMs without
     modification calls are `partial`.
   - `blocked`: a BAM or reference check failed (TBX-BAM-001 to 004). `run`
     will refuse; see the code in the table below. An unaligned BAM gives
     TBX-BAM-003 and the alignment command; a contig mismatch lists the first
     three differing `@SQ` positions.

   Without `--reference`, `preflight` uses a built-in synthetic reference
   only while ROOT has no registered reference; once one is registered it
   refuses with TBX-REF-004 and lists the registered IDs.
4. **`run`** prints the locked policy (`aligned-reference-span-local-v2`:
   chr1-chr22, chrX, chrY when registered, else every registered contig;
   MAPQ >= 20; primary, mapped, non-duplicate, non-QC-fail alignments; bins 0,
   100, 150, 200, 300, 500, 1000 bp), one `STAGE` line per stage (seal,
   preflight, measure, sign), then
   `PASS  Signed local record ready (development trust, unqualified, not for clinical use)`
   with `RECORD_ID`, `RECORDS_SCANNED`, `ELIGIBLE_ALIGNMENTS`, the absolute
   `REPORT_PATH` and the next commands. Open `report.html` for the local
   report. Re-running the same BAM on the same ROOT returns the existing
   record.
5. **`verify`** prints
   `PASS  Signed local record verified with development trust` (exit 0).
   `verify RECORD_ID --root R` uses `R/trust/development-result-trust.json`;
   the explicit form is
   `verify "$R/records/$RECORD_ID" --trust-store "$R/trust/development-result-trust.json"`.
6. **`catalog import`** adds the record to `R/catalog` as
   `QUALIFICATION_STATE  development_unqualified` with
   `CURRENT_PROVIDER_ELIGIBLE  False`, and saves its explorer view under
   `R/explorer`. Re-importing is a no-op with the same `RESULT_ID`.
7. **`serve`** prints a one-use operator link on its first line, for example
   `http://127.0.0.1:PORT/#bootstrap=...`. Open it in a browser on this machine
   within 60 seconds; do not share it (it is a bearer secret until used). The
   next lines say how many cataloged record views were loaded and how many
   invalid explorer files were skipped. In a terminal, Enter prints a fresh
   one-use link (also a bearer secret: do not paste terminal output into
   chats, tickets or logs); Ctrl-C stops the server. With stdin closed or redirected (for example
   in the background), only Ctrl-C or SIGTERM stops it. On stop it prints
   `Stopped; the web lock for ROOT is released.` The page shows the jobs and
   the catalog; records stay unqualified and local. A record imported while
   `serve` runs appears on the next catalog request; no restart is needed.

`--json` on every command except `serve` emits one canonical
`traceback.cli-result.v2` object with `data_origin: "local_unqualified"`; the
exit code is the same as in human output. `--json` output and the record never
contain the FASTA or BAM path; human output names the FASTA in one place only,
the alignment command that `preflight --reference ID` (`ALIGN_COMMAND`) and
`run --reference ID` (`FIX`) print for an unaligned BAM.

### Many BAMs: jobs, records and labels

`run` checks its inputs before it creates anything: a missing BAM
(TBX-RUN-008), a missing index (TBX-RUN-009) or a file that is not a BAM
(TBX-RUN-010) is refused with no job. Once a job exists, every refusal prints
its `JOB_ID`, and `traceback status JOB_ID` shows `FAILED: CODE summary` with
the cause and fix (`logs` shows the same, with ISO-8601 UTC times).

```bash
uv run traceback run "$BAM" --reference ref --label "batch 2 sample A" --import --root "$R"
uv run traceback jobs --root "$R"          # every job, newest first, with its failure code
uv run traceback catalog list --root "$R"  # every record: label, eligible count, imported or not
uv run traceback label RECORD_ID "new note" --root "$R"
uv run traceback catalog export --csv counts.csv --root "$R"
```

- `--import` catalogs the record right after `run` publishes it (the same as
  `catalog import`). `catalog import` takes a record ID, or the first 8 or
  more hex digits of one, as well as a record directory.
- `catalog list` shows each record's 12-character short ID, label, reference,
  policy (`built-in`), eligible alignments, import date, verification, and
  "same measurement as" when its measurement equals an earlier record's. A
  record that is not imported shows `not imported` and the import command.
- A label is an operator note, not part of the signed record. It is stored
  in `R/labels/RECORD_ID.json`, never changes a byte under `R/records`, and
  never appears in `--json` output, logs, bundles or support bundles. It is
  shown in human CLI output only. Do not put
  donor names or identifiers in labels. Labels are 1-80 characters with no
  `/`, `\`, control characters, paths or identifiers.
- `catalog export --csv` writes one row per imported record and histogram bin
  (`record_id, reference_id, policy_id, min_mapq, bin_lower, bin_upper, count,
  eligible, scanned`; an empty `bin_upper` is the open last bin). Counts come
  from the signed measurement; nothing is derived. It never overwrites a file.

### Upgrading

A local job's key names the exact method definition (policy, reference
bytes and tool), so a run under a changed method is a new job and never
returns an earlier method's record. Jobs written before this change used a
fixed key: they still show as local jobs and still resume, but re-running the
same BAM after upgrading makes a new job and a second record. Its
measurement equals the first record's, and `run` says so with
`SAME_MEASUREMENT_AS <record ID>` (`data.same_measurement_as` in `--json`).
Both records stay valid; the second is the same input measured again under the
same method.

### Daily canary

To rerun this path daily against the same FASTA and BAM and compare the counts
with a local baseline, record a baseline once and install the launchd job
(`docs/CANARIES.md`). The baseline and logs stay on the workstation.

```bash
uv run python scripts/canary/real_bam_canary.py --fasta "$FASTA" --bam "$BAM" \
  --record-baseline --repeat 2
```

### Cleanup

Sealed records are read-only. Stop `serve` first, then remove the root:

```bash
chmod -R u+w "$R" && rm -rf "$R"
```

### Troubleshooting

Every refusal prints `CODE`, `CAUSE`, `FIX`, `RETRYABLE` and `DOCS`; `DOCS`
points at the row below. A refusal changes nothing under ROOT unless the row
says otherwise. Exit codes are listed under "Stable exit codes".

| Code | Exit | Cause | Fix |
|---|---:|---|---|
| <a id="tbx-ref-001"></a>TBX-REF-001 | 3 or 4 | FASTA missing (4), gzip-compressed, without a `.fai`, or the `.fai` contradicts the FASTA | Decompress, run `samtools faidx REF.fa`, then register again |
| <a id="tbx-ref-002"></a>TBX-REF-002 | 3 | A different FASTA is already registered under this `--id` (registrations are write-once) | Keep the existing registration, or register under a new `--id` |
| <a id="tbx-ref-003"></a>TBX-REF-003 | 3 | The reference ID is not registered under this ROOT, or its registration files are damaged | Run `reference register` first (check `--root`); remove a damaged `R/references/ID` and register again |
| <a id="tbx-bam-001"></a>TBX-BAM-001 | 3 | BAM or index unreadable, truncated, not coordinate-sorted, or the index contradicts the BAM; or `--index` is not a `.bai`/`.csi` | `samtools sort`, then `samtools index`, and rerun |
| <a id="tbx-bam-002"></a>TBX-BAM-002 | 0 (WARN) or 3 | WARN: header has no `M5`/`AS`, matched by name and length only. BLOCKED: a contig name, length, order, `M5` or `AS` differs from the registered reference; the problem lists the first 3 differing positions as `position name_in_BAM length_in_BAM \| name_in_reference length_in_reference` (`-` where a side has no contig) and the total | WARN needs no action (optionally `samtools reheader` with `M5`/`AS`). BLOCKED, names differ only by a `chr` prefix with every length matching: rename with `samtools reheader` (the FIX prints a `sed` example). Otherwise realign against the registered FASTA, or register the FASTA the BAM was aligned to |
| <a id="tbx-bam-003"></a>TBX-BAM-003 | 3 | The BAM is unaligned (no `@SQ` lines); MinKNOW and Dorado write unaligned BAMs by default. `run` refuses before it creates a job | Align it with the printed command (see "Aligning MinKNOW output"), then preflight `OUT.sorted.bam` |
| <a id="tbx-bam-004"></a>TBX-BAM-004 | 3 | The BAM has a header but no alignment records. `run` refuses before it creates a job | Often a `bam_fail` or empty chunk; use the sample's `bam_pass` files |
| <a id="tbx-mod-001"></a>TBX-MOD-001 | 0 (WARN or PARTIAL) | WARN: valid `MM`/`ML`/`MN` tags, but no modified-base model declared in the header (no `@RG DS modbase_models=`; alignment drops `@RG`). PARTIAL: sampled reads carry no modification tags | WARN: no action needed for fragment length. PARTIAL: none for fragment length; re-basecall with modification calls for future methylation work |
| <a id="tbx-mod-002"></a>TBX-MOD-002 | 0 (PARTIAL) | Sampled modification tags are structurally contradictory | As TBX-MOD-001 |
| <a id="tbx-ref-004"></a>TBX-REF-004 | 2 | `preflight` without `--reference` on a ROOT that has registered references (the synthetic default would block a real BAM with a misleading contig error), or whose `R/references` could not be read (it never falls back to the synthetic default) | Add `--reference ID`; the problem lists the registered IDs. If `R/references` could not be read, check it with `traceback doctor --root R` |
| <a id="tbx-internal-001"></a>TBX-INTERNAL-001 | 3 (`run`) or 7 (`preflight`) | Preflight stopped on an unexpected internal error, not a BAM read or format error. In `run` the job ends terminally (never retried in a loop); no record | Retrying will not change it. From `run`: `traceback support-bundle JOB_ID --output DIR`, then report the code. From `preflight` (no job exists): report the code and the command you ran |
| <a id="tbx-run-003"></a>TBX-RUN-003 | 3 | `run` without `--reference` | Register the FASTA, then pass `--reference ID`; `traceback demo` is the synthetic workflow |
| <a id="tbx-run-004"></a>TBX-RUN-004 | 3 | Free space under 2x the input (retryable; reports required and available bytes), or ROOT's volume filled during the run; no record | Free space or use a `--root` on a larger volume, then run again under a fresh ROOT |
| <a id="tbx-run-005"></a>TBX-RUN-005 | 3 | No complete eligible denominator: no alignment passed the locked policy; the job fails, no record | Check contig names against the policy (chr1-chr22, chrX, chrY), MAPQ 20, and duplicate/secondary/supplementary/QC-fail flags |
| <a id="tbx-run-006"></a>TBX-RUN-006 | 3 | `R/trust/provenance-hmac.key` is not a private 32-byte file (edited, truncated, replaced, or readable by others; 0600 and 0400 are both accepted). A short key left by an older version's crash is replaced automatically while `R/records` is empty, so this refusal means records already exist | Restore it from a backup with mode 0600, or start a fresh ROOT |
| <a id="tbx-run-007"></a>TBX-RUN-007 | 3 | `R/trust/development-local-signing.key` is not a private 32-byte file | Restore it from a backup with mode 0600, or start a fresh ROOT |
| <a id="tbx-run-008"></a>TBX-RUN-008 | 4 | `run`: the BAM is missing, a directory or a symbolic link; no job was created | Check the path; pass the BAM file itself |
| <a id="tbx-run-009"></a>TBX-RUN-009 | 4 | `run`: no index at `--index` (default `BAM.bai`); no job was created | `samtools index BAM`, or pass `--index` |
| <a id="tbx-run-010"></a>TBX-RUN-010 | 3 | `run`: the file does not start with the BGZF bytes every BAM starts with; no job was created | This is not a BAM; FASTQ and POD5 must be aligned first (see "Aligning MinKNOW output") |
| <a id="tbx-cat-001"></a>TBX-CAT-001 | 3 | The path is not a verifiable local record (not a `run` record, not signed by this ROOT's key, or damaged); no catalog row | Pass `R/records/RECORD_ID` from `run`, and check it with `traceback verify RECORD_ID --root R` |
| <a id="tbx-cat-002"></a>TBX-CAT-002 | 3 | The registered reference ID fails the explorer's public-text rules (for example `patient-id`); no catalog row | Register the FASTA again under a neutral ID (for example `hg38`) and rerun |
| <a id="tbx-auth-local-001"></a>TBX-AUTH-LOCAL-001 | 3 | `R/authority` is missing, not private, or a store fails its pinned SHA-256 or replay check | Restore `R/authority` from a backup, or remove `R/authority` and `R/catalog` together and import the records again |
| <a id="tbx-auth-local-002"></a>TBX-AUTH-LOCAL-002 | 3 | `R/trust/result-trust-registry` or its `.pin.json` is missing or does not open at its pin | Remove `R/trust/result-trust-registry` and its `.pin.json`, then import again (the registry mirrors `development-result-trust.json`) |
| <a id="tbx-job-001"></a>TBX-JOB-001 | 6 | The local run failed without a record for an unexpected reason | `traceback status JOB_ID --root R` and `traceback logs JOB_ID --root R`, fix the stated cause, then `retry` and `resume` |
| <a id="tbx-job-002"></a>TBX-JOB-002 | 3 | Another traceback process holds this job's worker lease (it is running, or stopped less than a lease length ago); prints the `JOB_ID` | Wait for it, or check `traceback status JOB_ID --root R` |
| <a id="tbx-label-001"></a>TBX-LABEL-001 | 3 | `R/labels` is a symbolic link or a file, so no label can be written (the site would not show one either) | Remove `R/labels` (labels are unsigned notes) and set the label again |
| <a id="tbx-cat-003"></a>TBX-CAT-003 | 3 | `catalog export`: the `--csv` file already exists; nothing was written | Choose a new file name, or move the existing file |
| <a id="tbx-serve-001"></a>TBX-SERVE-001 | 4 | `serve`: no `R/runner/runner.sqlite3` (wrong `--root`, or no run yet); nothing started | Run `traceback run ... --root R` first, or pass the right `--root` |
| <a id="tbx-serve-002"></a>TBX-SERVE-002 | 4 | `serve`: no `R/catalog`: no record imported; nothing started | `traceback catalog import R/records/RECORD_ID --root R` first |
| <a id="tbx-serve-003"></a>TBX-SERVE-003 | 3 | `serve` could not start its listener. `local web service is already running`: another `serve` holds this ROOT's web lock. `local web startup lock is busy` (retryable): another local web service was starting at the same moment. Other messages: `R/web` or the temporary directory is not private | Already running: use that service, or stop it (Ctrl-C in its terminal) and start again. Busy: wait a few seconds and retry. Otherwise make `R` and `R/web` owned by you, not group/other-writable |
| <a id="tbx-serve-004"></a>TBX-SERVE-004 | 3 | `serve` stopped itself: a security check of the running service failed (its state directory or listener changed) | Restart `traceback serve --root R` |
| <a id="tbx-auth-001"></a>TBX-AUTH-001 | browser 401 | The browser session expired (8 h), was idle for 20 min, or was logged out | Press Enter in `serve`'s terminal for a fresh link (or restart `serve`) |
| <a id="tbx-auth-002"></a>TBX-AUTH-002 | browser 403 | A changing request carried no valid CSRF token | Reload the page from a fresh `serve` link |
| <a id="tbx-auth-003"></a>TBX-AUTH-003 | browser 403 | The request's host, origin or path is not the local service's own (for example through a proxy) | Open the link `serve` printed, on this machine |
| <a id="tbx-auth-004"></a>TBX-AUTH-004 | browser 403 | Too many browser sessions are active | Log out of an old tab, or restart `serve` |
| <a id="tbx-auth-005"></a>TBX-AUTH-005 | browser 429 | Too many wrong launch links were tried | Wait a minute, then use a fresh link from `serve` |
| <a id="tbx-auth-006"></a>TBX-AUTH-006 | browser 403 | This reader session is already bound to a reader grant | Ask the operator for a new reader launch link |
| <a id="tbx-auth-007"></a>TBX-AUTH-007 | browser 403 | A reader session asked for an operator route (jobs or explorer) | Use the operator link that `serve` prints; reader links never reach the explorer |
| <a id="tbx-web-400"></a>TBX-WEB-400 | browser 400 | The local web service refused a malformed request | Reload the page and select again |
| <a id="tbx-web-404"></a>TBX-WEB-404 | browser 404 | The page, record or route does not exist | Return to the catalog and select again |
| <a id="tbx-web-431"></a>TBX-WEB-431 | browser 431 | The request headers were too large | Reload; clear this site's cookies if it repeats |
| <a id="tbx-web-503"></a>TBX-WEB-503 | browser 503 | The service is busy or a store is temporarily unavailable | Retry in a few seconds |
| <a id="tbx-internal"></a>TBX-INTERNAL | browser 500 | An internal error occurred; nothing was changed | Retry; if it repeats, write a support bundle and report it |
| <a id="tbx-out-001"></a>TBX-OUT-001 | none | Release gate only: an output contained a value the privacy rules forbid | Remove the forbidden value |
| <a id="operator-busy"></a>Operator busy | 3 | `A local action or unexpired worker lease is active`: another CLI mutation holds `R/.operator.lock` | Wait for it to finish, then rerun |

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
  Passing preflight alone processes nothing; `run --reference` makes an
  unqualified local record.

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
