"""Read-only ``traceback lab arm-zscores`` command."""

from __future__ import annotations

import argparse
from collections.abc import Callable
from typing import Any

from traceback_runner.cli import _local_envelope, _result, _with_human

from .arm_zscores import report_arm_zscores
from .cohort_command import _cohort_id
from .ingest import resolve_sample
from .problems import EXIT_OK, LabProblem
from .store import LabStore


def add_arm_zscore_parser(commands: Any, common: Callable[[argparse.ArgumentParser], None]) -> None:
    parser = commands.add_parser("arm-zscores", help="show exploratory cohort-relative arm z scores")
    parser.add_argument("sample", metavar="ID12|latest")
    parser.add_argument("--cohort", required=True, type=_cohort_id,
                        help="published protocol-matched cohort id")
    common(parser)
    parser.set_defaults(handler=_show)


def _show(args: argparse.Namespace, store: LabStore) -> tuple[int, dict[str, Any]]:
    source_id = resolve_sample(store, args.sample)
    try:
        report = report_arm_zscores(store, args.cohort, source_id)
    except ValueError as exc:
        if str(exc) != "admitted source has no full aligned input":
            raise
        raise LabProblem(
            "TBX-LAB-017", cause=f"sample {source_id[:12]} has no stored full aligned input",
            fix="Inspect Lab runs, restore the sample's full input if needed, then rerun from its original input",
            data={"id12": source_id[:12]},
        ) from exc
    data = {"root": str(store.root), **report.public()}
    payload = _local_envelope(_result(
        "lab arm-zscores", "ok", "exploratory arm z scores; no abnormality cutoff", data=data,
    ))
    lines = [f"ROOT  {store.root}", f"SAMPLE {source_id[:12]}",
             f"COHORT {report.cohort_version or 'unbuilt'}"]
    def support(score: Any) -> str:
        target_bins = "?" if score.target_bins_used is None else str(score.target_bins_used)
        control_bins = "?" if score.reference_min_bins_used is None else str(score.reference_min_bins_used)
        return f"controls={score.reference_n}, target_bins={target_bins}, min_control_bins={control_bins}"

    lines.extend(
        f"{arm:<7} {score.z:+.3f} ({support(score)})" if score.z is not None
        else f"{arm:<7} {score.state} ({support(score)})"
        for arm, score in report.scores.items()
    )
    return EXIT_OK, _with_human(payload, lines, replace_data=True)


__all__ = ["add_arm_zscore_parser"]
