#!/usr/bin/env bash
# Install, remove or inspect the daily golden-path real-BAM canary (launchd, macOS).
# See docs/CANARIES.md.
#
#   install_canary.sh install --fasta F --bam B [--baseline P] [--log-dir D] [--repeat N]
#   install_canary.sh uninstall
#   install_canary.sh status
#   install_canary.sh render --fasta F --bam B [...]   # print the plist; no side effects
#
# install refuses unless a baseline exists (record one first with
# `uv run python scripts/canary/real_bam_canary.py --fasta F --bam B --record-baseline`).
# The rendered plist holds absolute input paths; it stays in ~/Library/LaunchAgents.
#
# Environment overrides: TRACEBACK_CANARY_BASELINE, TRACEBACK_CANARY_LOG_DIR,
# TRACEBACK_CANARY_LAUNCH_AGENTS (plist directory), UV (uv binary), PYTHON
# (interpreter used to render the template; default "uv run python").
set -euo pipefail

LABEL="com.traceback.real-bam-canary"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/../.." && pwd -P)"
TEMPLATE="$SCRIPT_DIR/$LABEL.plist.template"
AGENTS_DIR="${TRACEBACK_CANARY_LAUNCH_AGENTS:-$HOME/Library/LaunchAgents}"
PLIST="$AGENTS_DIR/$LABEL.plist"
BASELINE="${TRACEBACK_CANARY_BASELINE:-$HOME/.config/traceback-canary/baseline.json}"
LOG_DIR="${TRACEBACK_CANARY_LOG_DIR:-$HOME/Library/Logs/traceback-canary}"
REPEAT=2
FASTA=""
BAM=""

die() {
  echo "install_canary: $*" >&2
  exit 2
}

usage() {
  sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'
  exit 2
}

absolute() {
  # absolute PATH -> absolute path of an existing file
  [ -f "$1" ] || die "$2 does not exist or is not a file"
  echo "$(cd "$(dirname "$1")" && pwd)/$(basename "$1")"
}

parse_inputs() {
  while [ $# -gt 0 ]; do
    case "$1" in
      --fasta) FASTA="${2:-}"; shift 2 ;;
      --bam) BAM="${2:-}"; shift 2 ;;
      --baseline) BASELINE="${2:-}"; shift 2 ;;
      --log-dir) LOG_DIR="${2:-}"; shift 2 ;;
      --repeat) REPEAT="${2:-}"; shift 2 ;;
      *) die "unknown option: $1" ;;
    esac
  done
  [ -n "$FASTA" ] && [ -n "$BAM" ] || die "--fasta and --bam are required"
  case "$REPEAT" in ''|*[!0-9]*|0) die "--repeat must be a positive integer" ;; esac
  FASTA="$(absolute "$FASTA" FASTA)"
  BAM="$(absolute "$BAM" BAM)"
  [ -f "$FASTA.fai" ] || die "the FASTA has no .fai index (samtools faidx)"
  [ -f "$BAM.bai" ] || die "the BAM has no .bai index (samtools index)"
  case "$BASELINE" in /*) ;; *) BASELINE="$PWD/$BASELINE" ;; esac
  case "$LOG_DIR" in /*) ;; *) LOG_DIR="$PWD/$LOG_DIR" ;; esac
  # Real-sample counts and input paths must never land in a Git work tree
  # (the repository is public).
  in_work_tree "$BASELINE" && die "the baseline must live outside the repository"
  in_work_tree "$LOG_DIR" && die "the log directory must live outside the repository"
  in_work_tree "$AGENTS_DIR" && die "the launchd plist must live outside the repository"
  return 0
}

in_work_tree() {
  # in_work_tree PATH -> 0 when PATH, fully resolved (every symlink including
  # the last component, and ..), lies in any Git work tree.
  local -a python
  read -r -a python <<<"$(python_command)"
  local directory
  directory="$("${python[@]}" -c 'import os, sys; print(os.path.realpath(sys.argv[1]))' "$1")" \
    || die "cannot resolve a path"
  while :; do
    [ -e "$directory/.git" ] && return 0
    [ "$directory" = "/" ] && return 1
    directory="$(dirname "$directory")"
  done
}

uv_path() {
  if [ -n "${UV:-}" ]; then echo "$UV"; return; fi
  command -v uv || die "uv is not on PATH; set UV=/path/to/uv"
}

python_command() {
  # One interpreter for render, path checks and status: $PYTHON, else the
  # project's uv-managed Python.
  if [ -n "${PYTHON:-}" ]; then echo "$PYTHON"; return; fi
  echo "$(uv_path) run --quiet --project $REPO python"
}

render() {
  local uv
  uv="$(uv_path)"
  local -a python
  read -r -a python <<<"$(python_command)"
  "${python[@]}" - "$TEMPLATE" "$uv" "$REPO" "$FASTA" "$BAM" "$BASELINE" "$LOG_DIR" \
    "$REPEAT" "$(dirname "$uv"):/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin" <<'PY'
import re
import sys
from xml.sax.saxutils import escape

template, uv, repo, fasta, bam, baseline, log_dir, repeat, path = sys.argv[1:]
text = open(template, encoding="utf-8").read()
for key, value in {
    "UV": uv, "REPO": repo, "FASTA": fasta, "BAM": bam, "BASELINE": baseline,
    "LOG_DIR": log_dir, "REPEAT": repeat, "PATH": path,
}.items():
    text = text.replace(f"@@{key}@@", escape(value))
if re.search(r"@@[A-Z_]+@@", text):
    raise SystemExit("unfilled placeholder in the launchd template")
sys.stdout.write(text)
PY
}

command_install() {
  parse_inputs "$@"
  [ -f "$BASELINE" ] || die "no baseline at the configured path; record one first:
  uv run python scripts/canary/real_bam_canary.py --fasta F --bam B --record-baseline"
  chmod 600 "$BASELINE"
  mkdir -p "$AGENTS_DIR"
  mkdir -p "$LOG_DIR" && chmod 700 "$LOG_DIR"
  # launchd appends to these; keep them private like the results.
  (umask 077 && touch "$LOG_DIR/launchd.out.log" "$LOG_DIR/launchd.err.log")
  chmod 600 "$LOG_DIR/launchd.out.log" "$LOG_DIR/launchd.err.log"
  local rendered
  rendered="$(mktemp "${TMPDIR:-/tmp}/traceback-canary-plist.XXXXXX")"
  render >"$rendered"
  plutil -lint "$rendered" >/dev/null || { rm -f "$rendered"; die "rendered plist is invalid"; }
  # Keep the previous agent so a failed reinstall can put it back.
  local previous=""
  if [ -f "$PLIST" ]; then
    previous="$(mktemp "${TMPDIR:-/tmp}/traceback-canary-previous.XXXXXX")"
    cp "$PLIST" "$previous"
  fi
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
  if install -m 0644 "$rendered" "$PLIST" && launchctl bootstrap "gui/$(id -u)" "$PLIST"; then
    rm -f "$rendered" "$previous"
    echo "installed $LABEL: daily at 03:30 local; results in $LOG_DIR"
    return
  fi
  rm -f "$rendered"
  if [ -n "$previous" ]; then
    install -m 0644 "$previous" "$PLIST" && rm -f "$previous"
    if launchctl bootstrap "gui/$(id -u)" "$PLIST" 2>/dev/null; then
      die "install failed; the previous agent was restored"
    fi
    die "install failed and the previous agent could not be reloaded; run install again"
  fi
  rm -f "$PLIST"
  die "install failed; no agent is installed"
}

command_uninstall() {
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
  rm -f "$PLIST"
  echo "uninstalled $LABEL (baseline and logs kept)"
}

command_status() {
  if [ -f "$PLIST" ]; then echo "agent: installed ($PLIST)"; else echo "agent: not installed"; fi
  if command -v launchctl >/dev/null 2>&1; then
    launchctl print "gui/$(id -u)/$LABEL" 2>/dev/null \
      | grep -E '^\s*(state|last exit code|runs) =' || echo "launchd: not loaded"
  fi
  local -a python
  read -r -a python <<<"$(python_command)"
  local log_dir="$LOG_DIR" installed=""
  if [ -f "$PLIST" ]; then
    # Use the log directory the installed agent actually writes to.
    installed="$("${python[@]}" - "$PLIST" <<'PY' 2>/dev/null || true
import plistlib, sys
arguments = plistlib.load(open(sys.argv[1], "rb"))["ProgramArguments"]
print(arguments[arguments.index("--log-dir") + 1])
PY
)"
    [ -n "$installed" ] && log_dir="$installed"
  fi
  local latest="$log_dir/latest.json"
  if [ ! -f "$latest" ]; then
    echo "last result: none yet"
    return
  fi
  "${python[@]}" - "$latest" <<'PY'
import json, sys
result = json.load(open(sys.argv[1], encoding="utf-8"))
print(f"last result: {result['status'].upper()} (finished {result['finished_at']})")
for run in result.get("runs", [])[:1]:
    m = run["metrics"].get("measurement", {})
    print(f"  records scanned {m.get('records_scanned')}, eligible {m.get('eligible_alignments')}")
print(f"  reproducible: {result.get('reproducible')}")
for line in result.get("warnings", []):
    print(f"  WARN {line}")
for line in result.get("failures", []):
    print(f"  FAIL {line}")
PY
}

[ $# -ge 1 ] || usage
sub="$1"
shift
case "$sub" in
  install) command_install "$@" ;;
  uninstall) command_uninstall ;;
  status) command_status ;;
  render) parse_inputs "$@"; render ;;
  -h|--help|help) usage ;;
  *) usage ;;
esac
