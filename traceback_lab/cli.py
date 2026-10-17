"""``traceback lab …``: the Lab's argparse tree, ROOT resolution and output.

Every Lab command takes ``--root`` and ``--json``.  ROOT is ``--root``, else
``$TRACEBACK_ROOT``, else ``.traceback`` (the signed default), always printed
as an absolute path: the first human line is ``ROOT  /absolute/path`` and
``--json`` carries ``data.root`` (D36).  ``--json`` prints exactly one
``traceback.cli-result.v2`` object on stdout.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from traceback_runner.cli import _HUMAN, _emit, _local_envelope, _result, _with_human

from .problems import EXIT_INTERNAL, EXIT_OK, EXIT_USAGE, LabProblem, problem_lines, problem_payload
from .registry import load_registry
from .store import LabStore, RunLog, summarize_run

ROOT_ENV = "TRACEBACK_ROOT"
DEFAULT_ROOT = ".traceback"

Handler = Callable[[argparse.Namespace, LabStore], tuple[int, dict[str, Any]]]


def resolve_root(value: Path | None, environ: dict[str, str] | None = None) -> Path:
    """``--root``, else ``$TRACEBACK_ROOT``, else ``.traceback``; made absolute."""

    env = os.environ if environ is None else environ
    chosen = value if value is not None else Path(env[ROOT_ENV]) if env.get(ROOT_ENV) else Path(DEFAULT_ROOT)
    return Path(os.path.abspath(chosen.expanduser()))


class UsageError(Exception):
    """An argparse refusal, routed through the Lab output contract (exit 2)."""


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # type: ignore[override]
        raise UsageError(f"{self.prog}: {message}")


def _common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--root",
        type=Path,
        default=None,
        help=f"data root (default: ${ROOT_ENV}, else {DEFAULT_ROOT})",
    )
    parser.add_argument("--json", action="store_true", dest="as_json")


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(
        prog="traceback lab",
        epilog="Quickstart:\n  traceback lab doctor\n  traceback lab run --example\n  traceback lab serve --open",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Traceback Lab: run every cfDNA algorithm a kit BAM supports. "
            "Research use only. Not CLIA-validated. Not a diagnosis."
        ),
    )
    commands = parser.add_subparsers(
        dest="lab_command", required=True, metavar="COMMAND", parser_class=_Parser
    )

    algorithms = commands.add_parser("algorithms", help="list registered algorithms")
    _common(algorithms)
    algorithms.set_defaults(handler=_algorithms)

    from .run_command import add_run_parser

    add_run_parser(commands, _common)

    from .simulate_command import add_simulate_parser

    add_simulate_parser(commands, _common)

    from .asset_command import add_asset_parser

    add_asset_parser(commands, _common)

    from .doctor import add_doctor_parser

    add_doctor_parser(commands, _common)

    from .serve_command import add_serve_parser

    add_serve_parser(commands, _common)

    from .cohort_command import add_cohort_parser

    add_cohort_parser(commands, _common)

    from .microbial_command import add_microbial_parser

    add_microbial_parser(commands, _common)

    runs = commands.add_parser("runs", help="list Lab runs, or one run's steps")
    runs.add_argument("run_id", nargs="?", metavar="RUN_ID")
    runs.add_argument("--step", metavar="S", help="only this step's events")
    _common(runs)
    runs.set_defaults(handler=_runs)
    return parser


def _ok(command: str, summary: str, data: dict[str, Any]) -> dict[str, Any]:
    return _local_envelope(_result(f"lab {command}", "ok", summary, data=data))


def _algorithms(args: argparse.Namespace, store: LabStore) -> tuple[int, dict[str, Any]]:
    from .interpretation.engine import load_library

    registry = load_registry()
    library = load_library()

    def catalog(algorithm_id: str) -> dict[str, Any]:
        entry = library.headline(algorithm_id)
        if entry is None:
            return {"tier": None, "footnote": None, "license_ids": [], "min_depth_x": None, "headline_metric": None}
        performance = entry.get("performance")
        return {
            "tier": entry.tier,
            "footnote": performance.get("value") if performance else entry.get("performance_missing_reason"),
            "license_ids": list(entry.get("license_ids") or []),
            "min_depth_x": entry.get("min_depth_x"),
            "headline_metric": entry.metric_id,
        }

    rows = [
        {
            "id": algorithm.id,
            "version": algorithm.version,
            "family": algorithm.family,
            "cost": algorithm.cost,
            "consumes": algorithm.consumes,
            "min_eligible_reads": algorithm.requires.min_eligible_reads,
            "upstream": list(algorithm.upstream),
            "hidden": algorithm.id in library.hidden_algorithms,
            **catalog(algorithm.id),
            "tools": [
                {"name": tool.name, "version": tool.version, "license_id": tool.license_id}
                for tool in algorithm.tools
            ],
        }
        for algorithm in registry
    ]
    payload = _ok(
        "algorithms",
        f"{len(rows)} algorithm(s) registered",
        {"root": str(store.root), "algorithms": rows, "interpretation_version": library.version},
    )
    lines = [f"ROOT  {store.root}"]
    if rows:
        lines.append(_table(
            ("ID", "TIER", "FAMILY", "COST", "MIN DEPTH", "LICENSES", "FOOTNOTE"),
            [
                (
                    row["id"] + (" (intermediate)" if row["hidden"] else ""),
                    row["tier"] or "-",
                    row["family"],
                    row["cost"],
                    f"{row['min_depth_x']:g}x" if row["min_depth_x"] else "any",
                    ", ".join(row["license_ids"]) or "-",
                    (row["footnote"] or "-")[:70],
                )
                for row in rows
            ],
        ))
    else:
        lines.append("No algorithms are registered yet.")
    return EXIT_OK, _with_human(payload, lines, replace_data=True)


def _table(header: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    widths = [len(cell) for cell in header]
    for row in rows:
        widths = [max(width, len(str(cell))) for width, cell in zip(widths, row)]
    render = lambda cells: "  ".join(str(c).ljust(w) for c, w in zip(cells, widths)).rstrip()  # noqa: E731
    return "\n".join([render(header), *(render(row) for row in rows)])


def _runs(args: argparse.Namespace, store: LabStore) -> tuple[int, dict[str, Any]]:
    store.check_version()
    if args.run_id is None:
        rows = [summarize_run(store.run_log(run_id).events(), store) for run_id in store.run_ids()]
        payload = _ok(
            "runs", f"{len(rows)} run(s)", {"root": str(store.root), "runs": rows}
        )
        lines = [f"ROOT  {store.root}"]
        if rows:
            lines.append(_table(
                ("RUN", "COMMAND", "SAMPLE", "STARTED", "STATUS", "STEPS"),
                [
                    (
                        row["run_id"],
                        row["command"] or "-",
                        row["sample"] or "-",
                        row["started_utc"] or "-",
                        row["status"],
                        " ".join(f"{key}={value}" for key, value in sorted(row["steps"].items())) or "-",
                    )
                    for row in rows
                ],
            ))
        else:
            lines.append("No Lab runs under this ROOT.")
        return EXIT_OK, _with_human(payload, lines, replace_data=True)

    try:
        log = RunLog(store, args.run_id)
    except ValueError:
        log = None
    events = log.events() if log is not None else []
    if not events:
        raise LabProblem(
            "TBX-LAB-017",
            cause=f"no Lab run {args.run_id!r} under this ROOT",
            fix="`traceback lab runs` lists runs; pass the same --root you ran with",
            data={"run_id": args.run_id, "candidates": store.run_ids()[:10]},
        )
    if args.step:
        events = [event for event in events if event.get("step") == args.step]
    summary = summarize_run(log.events(), store)
    payload = _ok(
        "runs",
        f"run {args.run_id}: {summary['status']}",
        {"root": str(store.root), "run": summary, "events": events},
    )
    lines = [f"ROOT  {store.root}", f"RUN   {args.run_id}  {summary['status']}"]
    for event in events:
        detail = " ".join(
            f"{key}={value}"
            for key, value in event.items()
            if key not in {"event", "run_id", "utc", "step"} and value is not None
        )
        lines.append(f"{event.get('utc', '')}  {event.get('event', ''):<10}  {event.get('step', '') or ''}  {detail}".rstrip())
    return EXIT_OK, _with_human(payload, lines, replace_data=True)


def emit(payload: dict[str, Any], *, as_json: bool, root: Path) -> None:
    """Print one result; the first human line is always ``ROOT  /abs``."""

    human = payload.get(_HUMAN)
    if as_json:
        data = payload.setdefault("data", {})
        if "root" not in data:
            payload["data"] = {"root": str(root), **data}
        _emit(payload, as_json=True)
        return
    lines = list((human or {}).get("lines", ()))
    if not lines or not lines[0].startswith("ROOT  "):
        lines.insert(0, f"ROOT  {root}")
    if (human or {}).get("bare"):
        # A self-contained block (the run summary): no status label line.
        for line in lines:
            print(line)
        return
    print(lines[0])
    _emit(_with_human(payload, lines[1:], replace_data=True), as_json=False)


def _usage(message: str, raw: Sequence[str]) -> int:
    """Exit 2: a one-object JSON envelope with ``--json``, argparse text otherwise."""

    if "--json" in raw:
        root_value: Path | None = None
        if "--root" in raw:
            position = raw.index("--root")
            if position + 1 < len(raw):
                root_value = Path(raw[position + 1])
        root = resolve_root(root_value)
        payload = _local_envelope(
            _result("lab", "usage", message, data={"root": str(root), "usage": message})
        )
        _emit(payload, as_json=True)
    else:
        print(message, file=sys.stderr)
        print("See `traceback lab --help`.", file=sys.stderr)
    return EXIT_USAGE


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    raw = list(sys.argv[2:] if argv is None else argv)
    try:
        args = parser.parse_args(raw)
    except UsageError as exc:
        return _usage(str(exc), raw)
    root = resolve_root(args.root)
    store = LabStore(root)
    if getattr(args, "long_running", False):  # lab serve prints its own lines
        try:
            return int(args.handler(args, store))
        except LabProblem as problem:
            emit(
                _with_human(problem_payload(f"lab {args.lab_command}", problem, root=str(root)),
                            [f"ROOT  {root}", *problem_lines(problem)], replace_data=True),
                as_json=bool(getattr(args, "as_json", False)),
                root=root,
            )
            return problem.exit_code
    as_json = bool(getattr(args, "as_json", False))
    command = f"lab {args.lab_command}"
    try:
        code, payload = args.handler(args, store)
    except LabProblem as problem:
        code = problem.exit_code
        payload = _with_human(
            problem_payload(command, problem, root=str(root)),
            [f"ROOT  {root}", *problem_lines(problem)],
            replace_data=True,
        )
    except KeyboardInterrupt:
        raise
    except Exception as exc:  # noqa: BLE001 - every escape is TBX-LAB-020
        problem = LabProblem(
            "TBX-LAB-020",
            cause=f"{type(exc).__name__} in `{command}`",
            fix="`traceback lab runs`, then report the code and the command",
        )
        code = EXIT_INTERNAL
        payload = _with_human(
            problem_payload(command, problem, root=str(root)),
            [f"ROOT  {root}", *problem_lines(problem)],
            replace_data=True,
        )
    emit(payload, as_json=as_json, root=root)
    return int(code)


__all__ = ["DEFAULT_ROOT", "ROOT_ENV", "build_parser", "emit", "main", "resolve_root"]
