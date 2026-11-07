#!/usr/bin/env python3
"""Golden-path canary: register -> preflight -> run -> verify on one FASTA + BAM.

Usage:
    uv run python scripts/canary/real_bam_canary.py --fasta F --bam B \
        [--analysis fragment|cell-origin --loyfer-dir DIR [--modbase-model ID]] \
        [--baseline PATH] [--record-baseline [--force]] [--repeat 2] [--keep]

``--analysis`` (default ``fragment``, unchanged behaviour) picks the analysis
the run makes a record for (signal CO6).  The record's measurement path is read
from its bundle manifest.  Each analysis keeps its own baseline: ``baseline.json``
for fragment length, ``baseline-<analysis>.json`` beside it otherwise.

Each repeat runs in a fresh temporary root and collects deterministic metrics
(exit codes, preflight outcome and per-check codes, scan and exclusion counts,
histogram bins, a SHA-256 of the canonical measurement JSON) plus wall times.
With --repeat N>1 every repeat must give identical deterministic metrics.
The metrics are compared with a baseline JSON: any drift in counts, codes or the
measurement digest fails; a step slower than 2x its baseline wall time warns.

The baseline and the result logs stay on this machine (defaults
~/.config/traceback-canary/baseline.json and ~/Library/Logs/traceback-canary/).
They hold counts from a real sample, so never commit them; the repository is
public. No result, baseline or console line carries an absolute input path;
inputs are identified by size and SHA-256 only.

Exit codes: 0 pass (warnings allowed), 1 fail, 2 refused (usage, an existing
baseline without --force). Every record the canary makes is unqualified, local
and not for clinical use; the temporary roots are deleted unless --keep.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

RESULT_SCHEMA = "traceback.canary-result.v1"
BASELINE_SCHEMA = "traceback.canary-baseline.v1"
REFERENCE_ID = "ref"
LOCAL_BANNER = (
    "Unqualified. Local development record. Not for clinical use. "
    "Development signing key only."
)
TRUST_RELATIVE = Path("trust/development-result-trust.json")
MANIFEST_RELATIVE = Path("bundle-manifest.json")
FRAGMENT = "fragment"
CELL_ORIGIN = "cell-origin"
# Copy number joins with CN6, behind the CI toolchain lock.
ANALYSES = (FRAGMENT, CELL_ORIGIN)
SLOW_FACTOR = 2.0
EXIT_PASS, EXIT_FAIL, EXIT_REFUSED = 0, 1, 2

# The measurement JSON (traceback.fragment-measurement.v2) carries no per-run
# field: no timestamp, job, key or record ID (checked by running the same BAM
# in two fresh roots; the files are byte-identical). Its canonical form is
# therefore hashed whole. If a later schema adds a per-run field, list it here.
VOLATILE_MEASUREMENT_FIELDS: frozenset[str] = frozenset()


class Refused(Exception):
    """A usage refusal (exit 2) raised before any work starts."""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _default_baseline(analysis: str = FRAGMENT) -> Path:
    """``baseline.json`` for fragment length; ``baseline-<analysis>.json`` beside it.

    ``$TRACEBACK_CANARY_BASELINE`` names the fragment baseline; another
    analysis's baseline sits in the same directory, so the two never share a
    file.
    """

    env = os.environ.get("TRACEBACK_CANARY_BASELINE")
    fragment = (
        Path(env) if env else Path.home() / ".config" / "traceback-canary" / "baseline.json"
    )
    if analysis == FRAGMENT:
        return fragment
    return fragment.with_name(f"baseline-{analysis}.json")


def _default_log_dir() -> Path:
    env = os.environ.get("TRACEBACK_CANARY_LOG_DIR")
    if env:
        return Path(env)
    return Path.home() / "Library" / "Logs" / "traceback-canary"


def file_identity(path: Path) -> dict[str, Any]:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return {"size_bytes": path.stat().st_size, "sha256": digest.hexdigest()}


def canonical_measurement_sha256(measurement: dict[str, Any]) -> str:
    stable = {k: v for k, v in measurement.items() if k not in VOLATILE_MEASUREMENT_FIELDS}
    canonical = json.dumps(stable, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class Scrubber:
    """Replaces input locators with placeholders in anything printed or stored."""

    def __init__(self, fasta: Path, bam: Path) -> None:
        pairs: list[tuple[str, str]] = []
        for path, label in ((fasta, "<FASTA>"), (bam, "<BAM>")):
            for variant in {os.path.abspath(path), os.path.realpath(path)}:
                pairs.append((variant, label))
                pairs.append((os.path.dirname(variant), f"{label}-DIR"))
        # Longest first so a file path wins over its directory.
        self.pairs = sorted(
            ((needle, label) for needle, label in set(pairs) if len(needle) > 1),
            key=lambda pair: -len(pair[0]),
        )

    @property
    def needles(self) -> list[str]:
        return [needle for needle, _ in self.pairs]

    def __call__(self, text: str) -> str:
        for needle, label in self.pairs:
            text = text.replace(needle, label)
        return text

    def leaks_in(self, directory: Path) -> list[str]:
        encoded = [needle.encode("utf-8") for needle in self.needles]
        leaked: list[str] = []
        for path in sorted(directory.rglob("*")):
            if path.is_file() and not path.is_symlink():
                data = path.read_bytes()
                if any(needle in data for needle in encoded):
                    leaked.append(path.relative_to(directory).as_posix())
        return leaked


def _tail(text: str, lines: int = 15) -> str:
    return "\n".join(text.strip().splitlines()[-lines:])


class Step:
    def __init__(self, name: str, completed: subprocess.CompletedProcess[str], seconds: float):
        self.name = name
        self.exit_code = completed.returncode
        self.stdout = completed.stdout
        self.stderr = completed.stderr
        self.seconds = seconds

    def json(self) -> dict[str, Any]:
        return json.loads(self.stdout)


def _cli(args: list[str], timeout: float) -> tuple[subprocess.CompletedProcess[str], float]:
    started = time.monotonic()
    try:
        completed = subprocess.run(
            [sys.executable, "-m", "traceback_runner", *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        completed = subprocess.CompletedProcess(
            exc.cmd, 124, _as_text(exc.stdout), _as_text(exc.stderr) + "\n(timed out)"
        )
    return completed, time.monotonic() - started


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    return value.decode("utf-8", "replace") if isinstance(value, bytes) else str(value)


def _histogram(bins: list[dict[str, Any]]) -> dict[str, int]:
    histogram: dict[str, int] = {}
    for entry in bins:
        lower = entry["bin"]["lower_inclusive"]
        upper = entry["bin"]["upper_exclusive"]
        histogram[f"{lower}-{'inf' if upper is None else upper}"] = entry["count"]
    return histogram


def measurement_relative(record: Path) -> Path:
    """The record's one measurement file, as its bundle manifest names it."""

    manifest = json.loads((record / MANIFEST_RELATIVE).read_text(encoding="utf-8"))
    paths = [
        item["relative_path"]
        for item in manifest.get("contents", [])
        if isinstance(item, dict)
        and str(item.get("relative_path", "")).startswith("measurements/")
    ]
    if len(paths) != 1:
        raise ValueError("the bundle manifest names no single measurement file")
    relative = Path(paths[0])
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("the bundle manifest names an unsafe measurement path")
    return relative


def fragment_metrics(measurement: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": measurement.get("schema_version"),
        "completion": measurement.get("completion"),
        "records_scanned": measurement.get("records_scanned"),
        "eligible_alignments": measurement.get("eligible_alignments"),
        "exclusions": measurement.get("exclusions"),
        "histogram": _histogram(measurement.get("histogram", [])),
    }


def cell_origin_metrics(measurement: dict[str, Any]) -> dict[str, Any]:
    """Denominators, contributor fractions and the fit residual (spec §6).

    Fractions are compared exactly: any drift fails.  A tolerance would be a
    scientist-approved method parameter, never added here.
    """

    solver = measurement.get("solver") or {}
    return {
        "schema_version": measurement.get("schema_version"),
        "denominators": measurement.get("denominators"),
        "fractions": {
            item["contributor_id"]: item["fraction"]
            for item in measurement.get("estimates", [])
        },
        "residual_l2": solver.get("residual_l2"),
    }


def run_once(
    fasta: Path,
    bam: Path,
    work: Path,
    scrub: Scrubber,
    timeout: float,
    log: Any,
    *,
    analysis: str = FRAGMENT,
    modbase_model: str | None = None,
    loyfer_dir: Path | None = None,
) -> dict[str, Any]:
    """One full golden-path pass in ``work``; returns metrics, timings and failures."""

    root = work / "root"
    out = work / "out"
    out.mkdir(parents=True)
    exit_codes: dict[str, int] = {}
    wall: dict[str, float] = {}
    failures: list[str] = []
    metrics: dict[str, Any] = {"exit_codes": exit_codes}
    result = {"metrics": metrics, "wall_seconds": wall, "failures": failures}

    def step(name: str, args: list[str]) -> Step | None:
        completed, seconds = _cli(args, timeout)
        current = Step(name, completed, seconds)
        exit_codes[name] = current.exit_code
        wall[name] = round(seconds, 3)
        (out / f"{name}.stdout").write_text(current.stdout, encoding="utf-8")
        (out / f"{name}.stderr").write_text(current.stderr, encoding="utf-8")
        log(f"  {name:<10} exit={current.exit_code}  {seconds:.1f}s")
        if current.exit_code != 0:
            failures.append(f"step {name} exited {current.exit_code}")
            detail = _tail(current.stdout + "\n" + current.stderr)
            if detail:
                log(scrub(detail))
            return None
        return current

    def json_of(current: Step) -> dict[str, Any] | None:
        try:
            return current.json()
        except ValueError:
            failures.append(f"step {current.name} did not print JSON")
            return None

    reg = step("register", ["reference", "register", "--fasta", str(fasta), "--id",
                            REFERENCE_ID, "--root", str(root), "--json"])
    if reg is None:
        return result
    if analysis == CELL_ORIGIN:
        # Each fresh ROOT registers the three Loyfer files (write-once, by digest).
        assets = step("assets", ["method-asset", "register", "--from-dir", str(loyfer_dir),
                                 "--root", str(root), "--json"])
        if assets is None:
            return result

    selected = [] if analysis == FRAGMENT else ["--analysis", analysis]
    if modbase_model is not None:
        selected += ["--modbase-model", modbase_model]
    pre = step("preflight", ["preflight", str(bam), "--reference", REFERENCE_ID,
                             "--root", str(root), "--json", *selected])
    if pre is None or (pre_json := json_of(pre)) is None:
        return result
    report = pre_json["data"]["report"]
    metrics["preflight"] = {
        "outcome": report["outcome"],
        "fragment_measurement_eligible": report["fragment_measurement_eligible"],
        "checks": [
            {"code": c["code"], "outcome": c["outcome"],
             "role": c.get("supporting_artifact_role")}
            for c in report["checks"]
        ],
    }
    if report["outcome"] == "blocked":
        failures.append("preflight outcome is blocked")
    if not report["fragment_measurement_eligible"]:
        failures.append("preflight did not make the BAM fragment-measurement eligible")
    if pre_json.get("data_origin") != "local_unqualified" or (
        pre_json["data"].get("qualified") is not False
    ):
        failures.append("preflight result is not labelled local and unqualified")
    if analysis != FRAGMENT:
        readiness = {
            item.get("analysis"): item for item in pre_json["data"].get("analyses", [])
        }.get(analysis)
        if readiness is None:
            failures.append(f"preflight reported no {analysis} readiness")
        else:
            metrics["preflight"]["readiness"] = {
                "readiness": readiness.get("readiness"),
                "checks": [
                    {"code": c.get("code"), "outcome": c.get("outcome")}
                    for c in readiness.get("checks", [])
                ],
            }
            if readiness.get("readiness") != "ready":
                failures.append(f"preflight says {analysis} is not ready")

    run = step("run", ["run", str(bam), "--reference", REFERENCE_ID, "--root", str(root),
                       "--json", *selected])
    if run is None or (run_json := json_of(run)) is None:
        return result
    data = run_json["data"]
    if analysis != FRAGMENT:
        rows = [row for row in data.get("analyses", []) if row.get("analysis") == analysis]
        if len(rows) != 1 or not rows[0].get("record_id"):
            failures.append(f"run made no {analysis} record")
            return result
        data = rows[0]
    metrics["run"] = {
        "data_origin": run_json.get("data_origin"),
        "state": data.get("state"),
        "preflight_outcome": data.get("preflight_outcome"),
        "reference_match": data.get("reference_match"),
        "qualified": data.get("qualified"),
    }
    if run_json.get("data_origin") != "local_unqualified":
        failures.append("run result is not labelled local_unqualified")
    if data.get("qualified") is not False or data.get("development_trust_only") is not True:
        failures.append("run result is not unqualified with development trust only")
    record_id = data["record_id"]
    if data.get("bundle") != f"records/{record_id}":
        failures.append("run did not publish at records/<record_id>")
    record = root / "records" / record_id

    verify = step("verify", ["verify", str(record), "--trust-store",
                             str(root / TRUST_RELATIVE), "--json"])
    if verify is not None and (verify_json := json_of(verify)) is not None:
        verified = verify_json.get("data", {}).get("verified") is True
        metrics["verified"] = verified
        if not verified:
            failures.append("verify did not report verified")

    # Job log and status are part of the command output the locator check covers.
    job_id = data["job_id"]
    step("logs", ["logs", job_id, "--root", str(root), "--json"])
    step("status", ["status", job_id, "--root", str(root), "--json"])

    try:
        relative = measurement_relative(record)
    except (OSError, ValueError, TypeError, KeyError):
        failures.append("record has no readable bundle manifest naming its measurement")
        relative = None
    measurement_path = record / relative if relative is not None else None
    if measurement_path is None:
        pass
    elif not measurement_path.is_file():
        failures.append(f"record has no {relative.as_posix()}")
    else:
        measurement = json.loads(measurement_path.read_text(encoding="utf-8"))
        metrics["measurement"] = (
            fragment_metrics(measurement)
            if analysis == FRAGMENT
            else cell_origin_metrics(measurement)
        )
        metrics["measurement_sha256"] = canonical_measurement_sha256(measurement)

    report_html = record / "report.html"
    if not report_html.is_file():
        failures.append("record has no report.html")
        metrics["report"] = {"local_banner": False, "mentions_synthetic": None}
    else:
        text = report_html.read_text(encoding="utf-8")
        banner = LOCAL_BANNER in text
        synthetic = "synthetic" in text.lower()
        metrics["report"] = {"local_banner": banner, "mentions_synthetic": synthetic}
        if not banner:
            failures.append("report.html lacks the local banner")
        if synthetic:
            failures.append("report.html mentions synthetic")

    leaks = scrub.leaks_in(root / "records") + [
        f"output/{name}" for name in scrub.leaks_in(out)
    ]
    metrics["locator_clean"] = not leaks
    if leaks:
        failures.append(f"an input path appears in {len(leaks)} file(s): {', '.join(leaks)}")
    return result


def flatten(value: Any, prefix: str = "") -> dict[str, Any]:
    if isinstance(value, dict):
        flat: dict[str, Any] = {}
        for key in sorted(value):
            flat.update(flatten(value[key], f"{prefix}.{key}" if prefix else str(key)))
        return flat or {prefix: {}}
    if isinstance(value, list):
        flat = {f"{prefix}.length": len(value)}
        for index, item in enumerate(value):
            flat.update(flatten(item, f"{prefix}[{index}]"))
        return flat
    return {prefix: value}


_MISSING = "<missing>"
#: Metric keys whose values a diff names but never prints (gate G1).
_WITHHELD = "measurement.fractions."


def diff_metrics(expected: Any, actual: Any) -> list[str]:
    """Readable per-field differences; empty when ``expected == actual``."""

    left, right = flatten(expected), flatten(actual)
    lines = []
    for key in sorted(set(left) | set(right)):
        old, new = left.get(key, _MISSING), right.get(key, _MISSING)
        if old != new:
            if _WITHHELD in key:
                # A mixture fraction is never printed (gate G1); the field is named.
                lines.append(f"{key}: changed (value withheld)")
                continue
            lines.append(f"{key}: expected {json.dumps(old)}, got {json.dumps(new)}")
    if not lines and expected != actual:  # pragma: no cover - defensive
        lines.append("metrics differ")
    return lines


def slow_steps(baseline_wall: dict[str, float] | None, wall: dict[str, float]) -> list[str]:
    warnings: list[str] = []
    if not isinstance(baseline_wall, dict):
        return warnings
    for name, seconds in sorted(baseline_wall.items()):
        current = wall.get(name)
        if not all(isinstance(value, (int, float)) for value in (seconds, current)):
            continue
        if current > SLOW_FACTOR * seconds:
            warnings.append(
                f"step {name} took {current:.1f}s, over {SLOW_FACTOR:g}x its baseline "
                f"{seconds:.1f}s"
            )
    return warnings


def _write_private(path: Path, payload: dict[str, Any], *, exclusive: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if exclusive:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        return
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def inside_git_worktree(path: Path) -> bool:
    """True when ``path`` (resolved, existing or not) lies in any Git work tree.

    Covers this checkout, its linked worktrees and any other clone: the public
    repository must never receive real-sample counts or sealed inputs.
    """

    current = Path(os.path.realpath(path))
    for directory in (current, *current.parents):
        if (directory / ".git").exists():
            return True
    return False


def write_result(log_dir: Path, result: dict[str, Any], stamp: str) -> Path:
    log_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    for attempt in range(1000):
        name = f"{stamp}.json" if attempt == 0 else f"{stamp}-{attempt}.json"
        target = log_dir / name
        try:
            _write_private(target, result, exclusive=True)
            break
        except FileExistsError:
            continue
    else:  # pragma: no cover
        raise RuntimeError("could not pick a unique result file name")
    try:
        _write_private(log_dir / "latest.json", result, exclusive=False)
    except OSError:
        # Never leave a timestamped "pass" behind a run that then fails.
        target.unlink(missing_ok=True)
        raise
    return target


def notify(message: str, enabled: bool) -> None:
    if not enabled or os.environ.get("TRACEBACK_CANARY_NO_NOTIFY"):
        return
    osascript = shutil.which("osascript")
    if osascript is None:
        return
    safe = message.replace("\\", "\\\\").replace('"', '\\"')[:200]
    try:
        subprocess.run(
            [osascript, "-e", f'display notification "{safe}" with title "Traceback canary"'],
            capture_output=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        pass


def _remove_tree(path: Path) -> None:
    # Sealed records and snapshots are read-only.
    for current, directories, files in os.walk(path):
        for name in directories + files:
            try:
                os.chmod(os.path.join(current, name), 0o700, follow_symlinks=False)
            except (OSError, NotImplementedError):
                pass
    shutil.rmtree(path, ignore_errors=True)


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--fasta", type=Path, required=True)
    parser.add_argument("--bam", type=Path, required=True, help="coordinate-sorted, with .bai")
    parser.add_argument("--analysis", choices=ANALYSES, default=FRAGMENT,
                        help="the analysis to make a record for (default fragment)")
    parser.add_argument("--loyfer-dir", type=Path, default=None,
                        help="cell origin: the directory holding the three Loyfer files, "
                        "registered in each fresh root")
    parser.add_argument("--modbase-model", default=None,
                        help="cell origin: the modified-base model, when the BAM header "
                        "does not declare it")
    parser.add_argument("--baseline", type=Path, default=None,
                        help="baseline JSON (default $TRACEBACK_CANARY_BASELINE or "
                        "~/.config/traceback-canary/baseline.json; baseline-<analysis>.json "
                        "beside it for another analysis)")
    parser.add_argument("--record-baseline", action="store_true",
                        help="write the baseline from this run instead of comparing")
    parser.add_argument("--force", action="store_true",
                        help="with --record-baseline, overwrite an existing baseline")
    parser.add_argument("--repeat", type=int, default=1,
                        help="run N times in fresh roots and require identical metrics")
    parser.add_argument("--log-dir", type=Path, default=None,
                        help="result directory (default $TRACEBACK_CANARY_LOG_DIR or "
                        "~/Library/Logs/traceback-canary)")
    parser.add_argument("--work-dir", type=Path, default=None,
                        help="parent for temporary roots (default: system temp)")
    parser.add_argument("--keep", action="store_true", help="keep the temporary roots")
    parser.add_argument("--step-timeout", type=float, default=4 * 3600.0)
    parser.add_argument("--no-notify", action="store_true",
                        help="never post a macOS notification on failure")
    args = parser.parse_args(argv)
    if args.repeat < 1:
        parser.error("--repeat must be at least 1")
    if args.force and not args.record_baseline:
        parser.error("--force only applies with --record-baseline")
    if args.modbase_model is not None and args.analysis != CELL_ORIGIN:
        parser.error("--modbase-model only applies with --analysis cell-origin")
    if (args.loyfer_dir is not None) != (args.analysis == CELL_ORIGIN):
        parser.error("--loyfer-dir is required with, and only with, --analysis cell-origin")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    # The CLI gets absolute paths, so the locator check covers every spelling.
    args.fasta = Path(os.path.abspath(args.fasta))
    args.bam = Path(os.path.abspath(args.bam))
    baseline_path = args.baseline or _default_baseline(args.analysis)
    log_dir = args.log_dir or _default_log_dir()
    started = _utc_now()
    stamp = started.strftime("%Y%m%dT%H%M%SZ")
    scrub = Scrubber(args.fasta, args.bam)

    def log(line: str) -> None:
        print(scrub(line), flush=True)

    try:
        for path, label in ((args.fasta, "FASTA"), (args.bam, "BAM")):
            if not path.is_file():
                raise Refused(f"{label} does not exist or is not a file")
        # The locator check treats each input's directory as a locator, so the
        # temporary roots must not live under it.
        work_parent = os.path.realpath(args.work_dir or tempfile.gettempdir())
        for path in (args.fasta, args.bam):
            directory = os.path.dirname(os.path.realpath(path))
            if work_parent == directory or work_parent.startswith(directory + os.sep):
                raise Refused(
                    "the temporary roots would sit under an input's directory; "
                    "pass --work-dir elsewhere"
                )
        # Real-sample counts and sealed inputs must never land in a Git work
        # tree (the repository is public).
        if args.record_baseline and inside_git_worktree(baseline_path):
            raise Refused("refusing to write a baseline inside a Git work tree")
        if inside_git_worktree(log_dir):
            raise Refused("refusing to write canary logs inside a Git work tree")
        if inside_git_worktree(Path(work_parent)):
            raise Refused("refusing to put temporary roots inside a Git work tree")
        if args.record_baseline and baseline_path.exists() and not args.force:
            raise Refused(
                "a baseline already exists; refusing to overwrite it (pass --force "
                "to replace it)"
            )
    except Refused as refusal:
        print(f"REFUSED  {refusal}", file=sys.stderr)
        return EXIT_REFUSED

    print("Traceback golden-path canary (unqualified, local, not for clinical use)")
    log("inputs: hashing FASTA and BAM (paths are never recorded)")
    failures: list[str] = []
    warnings: list[str] = []
    runs: list[dict[str, Any]] = []
    inputs: dict[str, Any] | None
    try:
        inputs = {"fasta": file_identity(args.fasta), "bam": file_identity(args.bam)}
    except OSError as exc:
        inputs = None
        failures.append(f"cannot read the inputs: {type(exc).__name__}: {scrub(str(exc))}")
    for index in range(args.repeat if inputs is not None else 0):
        work = Path(tempfile.mkdtemp(prefix="traceback-canary.", dir=args.work_dir))
        log(f"run {index + 1}/{args.repeat}")
        try:
            outcome = run_once(
                args.fasta, args.bam, work, scrub, args.step_timeout, log,
                analysis=args.analysis, modbase_model=args.modbase_model,
                loyfer_dir=(
                    Path(os.path.abspath(args.loyfer_dir)) if args.loyfer_dir else None
                ),
            )
        except Exception as exc:  # noqa: BLE001 - any surprise is a canary failure
            outcome = {
                "metrics": {},
                "wall_seconds": {},
                "failures": [f"internal error: {type(exc).__name__}: {scrub(str(exc))}"],
            }
        finally:
            if args.keep:
                print(f"  kept root: {work}")
            else:
                _remove_tree(work)
        runs.append(outcome)
        failures.extend(f"run {index + 1}: {item}" for item in outcome["failures"])

    reproducible: bool | None = None
    if len(runs) > 1:
        first = runs[0]["metrics"]
        mismatches = [
            (index, diff_metrics(first, other["metrics"]))
            for index, other in enumerate(runs[1:], start=2)
        ]
        reproducible = all(not lines for _, lines in mismatches)
        for index, lines in mismatches:
            for line in lines:
                failures.append(f"not reproducible: run 1 vs run {index}: {line}")

    baseline_report: dict[str, Any] = {"recorded": False, "compared": False, "diff": []}
    metrics = runs[0]["metrics"] if runs else {}
    wall = runs[0]["wall_seconds"] if runs else {}
    if inputs is None:
        pass  # nothing ran; the input failure is already recorded
    elif args.record_baseline:
        if failures:
            failures.append("baseline not recorded: the run failed")
        else:
            payload = {
                "schema_version": BASELINE_SCHEMA,
                "recorded_at": started.isoformat(),
                "note": "Local canary baseline; real-sample counts. Never commit it.",
                "inputs": inputs,
                "metrics": metrics,
                "wall_seconds": wall,
            }
            try:
                _write_private(baseline_path, payload, exclusive=not args.force)
            except FileExistsError:
                failures.append("baseline appeared during the run; not overwritten")
            except OSError as exc:
                failures.append(f"could not write the baseline: {type(exc).__name__}")
            else:
                baseline_report["recorded"] = True
                log("baseline recorded (mode 0600)")
    elif not baseline_path.is_file():
        failures.append(
            "no baseline: run once with --record-baseline (see docs/CANARIES.md)"
        )
    elif not isinstance(baseline := _load_json(baseline_path), dict) or (
        baseline.get("schema_version") != BASELINE_SCHEMA
    ):
        failures.append(f"the baseline is unreadable or not {BASELINE_SCHEMA}")
    else:
        baseline_report["compared"] = True
        # A committed synthetic baseline sets inputs to null: the generated BAM's
        # compressed bytes may differ by platform while its records do not.
        if baseline.get("inputs") is not None:
            input_diff = diff_metrics(baseline["inputs"], inputs)
            baseline_report["inputs_match"] = not input_diff
            failures.extend(f"inputs changed since the baseline: {line}" for line in input_diff)
        drift = diff_metrics(baseline.get("metrics"), metrics)
        baseline_report["diff"] = drift
        shown = drift
        if runs[0]["failures"]:
            # A failed step leaves later metrics unset; one line says so instead
            # of one "<missing>" line per baseline field.
            shown = [line for line in drift if not line.endswith(f"got {json.dumps(_MISSING)}")]
            if len(shown) < len(drift):
                shown.append(f"{len(drift) - len(shown)} baseline fields not measured "
                             "(the run failed first)")
        failures.extend(f"drift from baseline: {line}" for line in shown)
        for run_index, run in enumerate(runs, start=1):
            warnings.extend(
                f"run {run_index}: {line}"
                for line in slow_steps(baseline.get("wall_seconds"), run["wall_seconds"])
            )

    status = "fail" if failures else "pass"
    result = {
        "schema_version": RESULT_SCHEMA,
        "canary": "golden-path-real-bam",
        "analysis": args.analysis,
        "qualified": False,
        "note": "Unqualified, local, not for clinical use. Input paths are never recorded.",
        "started_at": started.isoformat(),
        "finished_at": _utc_now().isoformat(),
        "status": status,
        "inputs": inputs,
        "repeat": args.repeat,
        "reproducible": reproducible,
        "measurement_sha256": [run["metrics"].get("measurement_sha256") for run in runs],
        "runs": [
            {"metrics": run["metrics"], "wall_seconds": run["wall_seconds"],
             "failures": run["failures"]}
            for run in runs
        ],
        "baseline": baseline_report,
        "warnings": warnings,
        "failures": failures,
    }
    # Last line of defence: nothing stored may carry an input locator.
    serialized = json.dumps(result)
    if scrub(serialized) != serialized:
        result = json.loads(scrub(serialized))
        result["failures"].append("an input path reached the result; it was scrubbed")
        result["status"] = status = "fail"
        failures = result["failures"]
    try:
        written: Path | None = write_result(log_dir, result, stamp)
    except OSError as exc:
        written = None
        failures.append(f"could not write the result: {type(exc).__name__}")
        status = "fail"

    for line in warnings:
        log(f"WARN  {line}")
    for line in failures:
        log(f"FAIL  {line}")
    measurement = metrics.get("measurement", {})
    if args.analysis == FRAGMENT:
        log(f"records scanned: {measurement.get('records_scanned')}")
        log(f"eligible alignments: {measurement.get('eligible_alignments')}")
    else:
        # Counts only; no fraction is printed (gate G1).
        denominators = measurement.get("denominators") or {}
        log(f"records scanned: {denominators.get('records_scanned')}")
        log(f"classified fragments: {denominators.get('classified_fragments')}")
    log(f"measurement sha256: {metrics.get('measurement_sha256')}")
    if reproducible is not None:
        log(f"reproducible across {args.repeat} runs: {reproducible}")
    if written is not None:
        print(f"result: {written.name} in the canary log directory")
    print(f"CANARY {status.upper()}")
    if failures:
        notify(f"Golden-path canary FAILED: {failures[0]}", not args.no_notify)
        return EXIT_FAIL
    return EXIT_PASS


if __name__ == "__main__":
    sys.exit(main())
