#!/usr/bin/env bash
# Golden-path acceptance run, Milestone 1 (docs/GOLDEN-PATH-MVP-SLICE.md,
# "Definition of done", steps 1-4), from a fresh mktemp root:
#
#   1. traceback reference register --fasta F --id ref --root R
#   2. traceback preflight BAM --reference ref --root R   (exit 0, not blocked)
#   3. traceback run BAM --reference ref --root R         (exit 0)
#   4. traceback verify R/records/<record> --trust-store R/trust/development-result-trust.json
#      (exit 0), and R/records/<record>/report.html is the local report.
#
# TODO(Milestone 2): steps 5-7 (traceback catalog import, traceback serve, and an
# operator-session GET of /api/v1/explorer/catalog?limit=10 that lists the record
# with qualification_state="development_unqualified") land with B5a/B5b/B6.
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

REPORT="$RECORD/report.html"
[ -f "$REPORT" ] || fail "record has no report.html"
grep -qF "Unqualified. Local development record. Not for clinical use. Development signing key only." \
  "$REPORT" || fail "report.html lacks the local banner"
if grep -qi "synthetic" "$REPORT"; then fail "report.html mentions synthetic"; fi

# Locator check: the FASTA and BAM absolute paths never leave the operator's input.
"${TRACEBACK_CMD[@]}" logs "$JOB_ID" --root "$R" --json >"$OUT/logs.json"
"${TRACEBACK_CMD[@]}" status "$JOB_ID" --root "$R" --json >"$OUT/status.json"
for locator in "$FASTA_ABS" "$BAM_ABS" "$(dirname "$BAM_ABS")"; do
  if grep -rqF "$locator" "$R/records" "$OUT"; then
    fail "an input path appears in the record or command output"
  fi
done

echo
echo "M1 golden-path summary (unqualified, local, not for clinical use)"
for line in "${STEP_LINES[@]}"; do echo "  $line"; done
echo "  preflight outcome: $PREFLIGHT_OUTCOME"
echo "  records scanned: $SCANNED"
echo "  eligible alignments: $ELIGIBLE"
echo "  verify: $VERIFIED"
echo "M1 ACCEPTANCE PASSED"
