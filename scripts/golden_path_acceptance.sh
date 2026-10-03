#!/usr/bin/env bash
# Golden-path acceptance run (docs/GOLDEN-PATH-MVP-SLICE.md, "Definition of
# done", steps 1-5), from a fresh mktemp root:
#
#   1. traceback reference register --fasta F --id ref --root R
#   2. traceback preflight BAM --reference ref --root R   (exit 0, not blocked)
#   3. traceback run BAM --reference ref --root R         (exit 0)
#   4. traceback verify R/records/<record> --trust-store R/trust/development-result-trust.json
#      (exit 0), and R/records/<record>/report.html is the local report.
#   5. traceback catalog import R/records/<record> --root R    (exit 0), then a
#      library-level check: IntegratedExplorerSource over R's catalog and the
#      persisted explorer artifacts lists the record as development_unqualified
#      and serves its detail.
#
# TODO(Milestone 2, B6): steps 6-7 (traceback serve --root R in the background,
# and an operator-session GET of /api/v1/explorer/catalog?limit=10 that lists the
# record with qualification_state="development_unqualified").
#
# Inputs:
#   FASTA, BAM   optional; a FASTA with .fai and a coordinate-sorted BAM with .bai.
#                Without them a tiny 2-contig FASTA and a ~1,000-read BAM (no M5/AS,
#                so the reference WARN path runs) are generated.
#   TRACEBACK    command that runs the CLI (default: "uv run traceback").
#   PYTHON       Python used for fixtures and JSON parsing (default: "uv run python").
#   KEEP_ROOT=1  keep the temporary root for inspection.
#
# The script never echoes the FASTA or BAM path, and fails if either path appears
# in the record, the report, any command output, or the job log.
# Every record it makes is unqualified, local and not for clinical use.
set -euo pipefail

read -r -a TRACEBACK_CMD <<<"${TRACEBACK:-uv run traceback}"
read -r -a PYTHON_CMD <<<"${PYTHON:-uv run python}"

WORK="$(mktemp -d "${TMPDIR:-/tmp}/traceback-golden.XXXXXX")"
R="$WORK/root"
OUT="$WORK/out"
mkdir -p "$OUT"

cleanup() {
  if [ -n "${KEEP_ROOT:-}" ]; then
    echo "kept root: $R"
    return
  fi
  # Sealed records and snapshots are read-only.
  chmod -R u+w "$WORK" 2>/dev/null || true
  rm -rf "$WORK"
}
trap cleanup EXIT

now() { perl -MTime::HiRes=time -e 'printf "%.3f", time'; }

fail() {
  echo "FAIL  $*" >&2
  exit 1
}

json_get() {
  # json_get FILE dotted.key  -> prints the value (JSON for objects/lists)
  "${PYTHON_CMD[@]}" - "$1" "$2" <<'PY'
import json, sys
value = json.loads(open(sys.argv[1], encoding="utf-8").read())
for part in sys.argv[2].split("."):
    value = value[part]
print(value if isinstance(value, str) else json.dumps(value))
PY
}

if [ -z "${FASTA:-}" ] || [ -z "${BAM:-}" ]; then
  INPUTS="$WORK/inputs"
  "${PYTHON_CMD[@]}" - "$INPUTS" <<'PY'
import sys
from pathlib import Path
from traceback_runner.fixtures import create_local_golden_path_inputs
create_local_golden_path_inputs(Path(sys.argv[1]))
PY
  FASTA="$INPUTS/golden-reference.fa"
  BAM="$INPUTS/golden-aligned.bam"
  echo "inputs: generated 2-contig FASTA and small BAM (no M5/AS)"
else
  echo "inputs: operator-supplied FASTA and BAM (paths not echoed)"
fi
FASTA_ABS="$(cd "$(dirname "$FASTA")" && pwd)/$(basename "$FASTA")"
BAM_ABS="$(cd "$(dirname "$BAM")" && pwd)/$(basename "$BAM")"

STEP_LINES=()
step() {
  # step N LABEL OUTFILE -- command...
  local number="$1" label="$2" outfile="$3"
  shift 4
  local started ended status
  started="$(now)"
  set +e
  "$@" >"$outfile" 2>"$outfile.err"
  status=$?
  set -e
  ended="$(now)"
  local seconds
  seconds="$(perl -e "printf '%.1f', $ended - $started")"
  STEP_LINES+=("$number  $label  exit=$status  ${seconds}s")
  echo "step $number  $label  exit=$status  ${seconds}s"
  if [ "$status" -ne 0 ]; then
    cat "$outfile" >&2 || true
    fail "step $number ($label) exited $status"
  fi
}

step 1 "reference register" "$OUT/1.json" -- \
  "${TRACEBACK_CMD[@]}" reference register --fasta "$FASTA" --id ref --root "$R" --json

step 2 "preflight --reference" "$OUT/2.json" -- \
  "${TRACEBACK_CMD[@]}" preflight "$BAM" --reference ref --root "$R" --json
PREFLIGHT_OUTCOME="$(json_get "$OUT/2.json" data.report.outcome)"
[ "$PREFLIGHT_OUTCOME" != "blocked" ] || fail "preflight outcome is blocked"
[ "$(json_get "$OUT/2.json" data.report.fragment_measurement_eligible)" = "true" ] \
  || fail "preflight did not make the BAM fragment-measurement eligible"

step 3 "run" "$OUT/3.json" -- \
  "${TRACEBACK_CMD[@]}" run "$BAM" --reference ref --root "$R" --json
[ "$(json_get "$OUT/3.json" data_origin)" = "local_unqualified" ] \
  || fail "run result is not labelled local_unqualified"
BUNDLE="$(json_get "$OUT/3.json" data.bundle)"
RECORD_ID="$(json_get "$OUT/3.json" data.record_id)"
JOB_ID="$(json_get "$OUT/3.json" data.job_id)"
ELIGIBLE="$(json_get "$OUT/3.json" data.eligible_alignments)"
SCANNED="$(json_get "$OUT/3.json" data.records_scanned)"
# Local records publish at R/records/<record_id> (DoD step 4).
RECORD="$R/records/$RECORD_ID"
[ "$BUNDLE" = "records/$RECORD_ID" ] || fail "run did not publish at records/<record_id>"

step 4 "verify" "$OUT/4.json" -- \
  "${TRACEBACK_CMD[@]}" verify "$RECORD" \
  --trust-store "$R/trust/development-result-trust.json" --json
VERIFIED="$(json_get "$OUT/4.json" data.verified)"
[ "$VERIFIED" = "true" ] || fail "verify did not report verified"
step 4b "verify --root RECORD_ID" "$OUT/4b.json" -- \
  "${TRACEBACK_CMD[@]}" verify "$RECORD_ID" --root "$R" --json

step 5 "catalog import" "$OUT/5.json" -- \
  "${TRACEBACK_CMD[@]}" catalog import "$RECORD" --root "$R" --json
QUALIFICATION="$(json_get "$OUT/5.json" data.qualification_state)"
[ "$QUALIFICATION" = "development_unqualified" ] \
  || fail "catalog import did not record development_unqualified"
[ "$(json_get "$OUT/5.json" data.current_provider_eligible)" = "false" ] \
  || fail "catalog import made the record provider-eligible"
RESULT_ID="$(json_get "$OUT/5.json" data.result_id)"
step 5b "catalog import (again)" "$OUT/5b.json" -- \
  "${TRACEBACK_CMD[@]}" catalog import "$RECORD" --root "$R" --json
[ "$(json_get "$OUT/5b.json" data.result_id)" = "$RESULT_ID" ] \
  || fail "re-import changed the result ID"

# Library-level explorer check (step 7's assertion without the HTTP server, B6).
"${PYTHON_CMD[@]}" - "$R" "$RESULT_ID" >"$OUT/explorer.json" <<'PY' \
  || fail "the explorer over ROOT's catalog does not list the record"
import json, sys
from pathlib import Path
from evidence_inspector.result_catalog import CatalogQuery
from traceback_runner.local_catalog import open_local_explorer

root, result_id = Path(sys.argv[1]), sys.argv[2]
with open_local_explorer(root) as explorer:
    assert explorer is not None and explorer.skipped == 0
    page = explorer.source.query(CatalogQuery(limit=10))
    rows = {item.ref.result_id: item for item in page.results}
    item = rows[result_id]
    assert item.ref.qualification_state.value == "development_unqualified"
    assert item.has_registered_view
    document = explorer.source.get(result_id)
    print(json.dumps({
        "listed": len(rows),
        "qualification_state": item.ref.qualification_state.value,
        "detail_rows": document.models.result_view.visible_count,
    }, sort_keys=True))
PY
EXPLORER_LISTED="$(json_get "$OUT/explorer.json" listed)"

REPORT="$RECORD/report.html"
[ -f "$REPORT" ] || fail "record has no report.html"
grep -qF "Unqualified. Local development record. Not for clinical use. Development signing key only." \
  "$REPORT" || fail "report.html lacks the local banner"
if grep -qi "synthetic" "$REPORT"; then fail "report.html mentions synthetic"; fi

# Locator check: the FASTA and BAM absolute paths never leave the operator's input.
"${TRACEBACK_CMD[@]}" logs "$JOB_ID" --root "$R" --json >"$OUT/logs.json"
"${TRACEBACK_CMD[@]}" status "$JOB_ID" --root "$R" --json >"$OUT/status.json"
for locator in "$FASTA_ABS" "$BAM_ABS" "$(dirname "$BAM_ABS")"; do
  # An input directory that also contains this run's work root (TMPDIR=/tmp
  # with a BAM in /tmp) legitimately prefixes ROOT paths; skip only that one.
  case "$WORK/" in "$locator"/*) continue ;; esac
  if grep -rqF "$locator" "$R/records" "$R/catalog" "$R/explorer" "$R/authority" "$OUT"; then
    fail "an input path appears in the record, catalog, explorer files or command output"
  fi
done

echo
echo "Golden-path summary, DoD steps 1-5 (unqualified, local, not for clinical use)"
for line in "${STEP_LINES[@]}"; do echo "  $line"; done
echo "  preflight outcome: $PREFLIGHT_OUTCOME"
echo "  records scanned: $SCANNED"
echo "  eligible alignments: $ELIGIBLE"
echo "  verify: $VERIFIED"
echo "  catalog qualification_state: $QUALIFICATION"
echo "  explorer rows listed: $EXPLORER_LISTED"
echo "ACCEPTANCE PASSED (DoD steps 1-5; steps 6-7 wait for B6 serve)"
