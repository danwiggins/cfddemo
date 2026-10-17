"""``traceback lab simulate cna|split <id12|latest>``: rerun one positive control (D25).

Both take the writer lock.  ``cna`` builds the synthetic CNA spike (one-copy
gains on 1q/8q, losses on 13q/17p at TF/2) as stripped counting BAMs and runs
copy-number on each; ``split`` builds split halves A/B and runs every
read-summary and counting-BAM algorithm on each.
"""

from __future__ import annotations

import argparse
import signal
import time
from collections.abc import Callable
from typing import Any

from traceback_runner.cli import _local_envelope, _result, _with_human

from . import registry
from .controls import DEFAULT_TFS, TF_TOLERANCE, cna_specs, run_derived, split_specs, synthetic_summary
from .identity import validate_fraction
from .ingest import id12_for, resolve_sample
from .problems import EXIT_OK, EXIT_PARTIAL, LabProblem
from .run_command import _echo, _interrupted_handler, _progress, _stored_input, _target_from_input
from .runner import DEFAULT_TIMEOUT_S, run_plan
from .scrub import Scrubber
from .store import LabStore, new_run_id


def parse_fractions(text: str, flag: str) -> tuple[float, ...]:
    """A comma list of fractions, each 0 < f < 1 (TBX-LAB-002)."""

    try:
        values = tuple(validate_fraction(float(item)) for item in text.split(",") if item.strip())
    except ValueError as exc:
        raise LabProblem(
            "TBX-LAB-002",
            cause=f"{flag} {text!r} is not a list of fractions with 0 < f < 1",
            fix=f"For example: {flag} 0.5,0.25,0.1",
            data={"flag": flag, "value_ok": False},
        ) from exc
    values = tuple(dict.fromkeys(values))  # 0.1 and 0.10 are one value
    if not values:
        raise LabProblem(
            "TBX-LAB-002",
            cause=f"{flag} names no fraction",
            fix=f"For example: {flag} 0.5,0.25,0.1",
            data={"flag": flag, "value_ok": False},
        )
    return values


def add_simulate_parser(commands: Any, common: Callable[[argparse.ArgumentParser], None]) -> None:
    simulate = commands.add_parser("simulate", help="rerun one positive control on a stored sample")
    kinds = simulate.add_subparsers(dest="simulate_kind", required=True, metavar="KIND", parser_class=type(simulate))
    cna = kinds.add_parser("cna", help="synthetic CNA spike (copy-number recovery)")
    cna.add_argument("sample", metavar="ID12|latest")
    cna.add_argument("--tf", default=",".join(str(tf) for tf in DEFAULT_TFS), metavar="0.03,0.10,0.30")
    cna.add_argument("--force", action="store_true")
    cna.add_argument("--verbose", action="store_true", help="stream tool stderr live (never stored)")
    cna.add_argument("--timeout-s", type=float, default=DEFAULT_TIMEOUT_S, metavar="S")
    common(cna)
    cna.set_defaults(handler=simulate_command)
    halves = kinds.add_parser("split", help="split-half pseudo-replicate (sampling noise)")
    halves.add_argument("sample", metavar="ID12|latest")
    halves.add_argument("--force", action="store_true")
    halves.add_argument("--verbose", action="store_true", help="stream tool stderr live (never stored)")
    halves.add_argument("--timeout-s", type=float, default=DEFAULT_TIMEOUT_S, metavar="S")
    common(halves)
    halves.set_defaults(handler=simulate_command)


def simulate_command(args: argparse.Namespace, store: LabStore) -> tuple[int, dict[str, Any]]:
    store.check_version()
    kind = args.simulate_kind
    tfs = parse_fractions(args.tf, "--tf") if kind == "cna" else ()
    progress = _progress(args.as_json)
    source_id = resolve_sample(store, args.sample)
    aligned, record = _stored_input(store, source_id)
    entries = registry.registry_entries()
    by_id = {alg.id: entry for entry, alg in entries}
    items = registry.plan([alg for _, alg in entries])
    run_id = new_run_id()
    progress(f"ROOT  {store.root}")
    started = time.monotonic()
    previous = signal.signal(signal.SIGTERM, _interrupted_handler)
    try:
        with store.writer(f"simulate {kind}", run_id, step="read-summary") as lock:
            log = store.run_log(run_id)
            log.append("run_start", command=f"simulate {kind}", pid=_pid())
            id12 = id12_for(store, source_id)
            log.append("sample", sample=id12, source_id=source_id, input_id=aligned.input_id)
            parent = _target_from_input(
                aligned,
                aligned.manifest["reference_id"],
                aligned.manifest["reference_sha256"],
                bool(record.get("example")),
                store,
            )
            scrubber = Scrubber(root=store.root)
            try:
                # Intermediates (read-summary, meth-calls) on the parent; derived
                # inputs reuse them through their row masks (D33).
                names = registry.intermediate_ids([item.algorithm for item in items])
                summary_items = [item for item in items if item.algorithm.id in names]
                summary_results = run_plan(
                    store, lock, log, parent, summary_items, by_id, run_id=run_id,
                    timeout_s=args.timeout_s, scrubber=scrubber, progress=progress,
                    echo=_echo(args.verbose),
                )
                read_summary = next((r for r in summary_results if r.algorithm_id == "read-summary"), None)
                parent_upstream = {r.algorithm_id: r for r in summary_results if r.status == "ok"}
                if kind == "cna":
                    specs = cna_specs(store.root, aligned.directory / "aligned.bam", tfs)
                    if isinstance(specs, str):
                        payload = _local_envelope(
                            _result(
                                f"lab simulate {kind}",
                                "ok",
                                f"synthetic CNA spike not built ({specs})",
                                data={"root": str(store.root), "sample": {"id12": id12}, "skipped": specs, "synthetic": []},
                            )
                        )
                        log.append("run_end", status="ok", exit_code=0, skipped=specs)
                        return EXIT_OK, _with_human(
                            payload,
                            [f"ROOT  {store.root}", f"SYNTHETIC  not built: {specs}"],
                            replace_data=True,
                        )
                else:
                    specs = split_specs()
                runs = run_derived(
                    store, lock, log, parent, specs, items, by_id, run_id=run_id,
                    read_summary=read_summary, force=args.force, timeout_s=args.timeout_s,
                    scrubber=scrubber, progress=progress, echo=_echo(args.verbose),
                    parent_upstream=parent_upstream,
                )
            except KeyboardInterrupt:
                log.append("run_end", status="interrupted", exit_code=6, step=lock.step)
                raise LabProblem(
                    "TBX-LAB-015",
                    cause=f"Interrupted during {lock.step}. Completed results are kept.",
                    fix="Rerun the same command; only missing results recompute",
                    data={"step": lock.step, "run_id": run_id},
                ) from None
            results = [result for run in runs for result in run.results]
            failed = [result for result in results if result.status == "failed"]
            synthetic = synthetic_summary(store, runs)
            data = {
                "root": str(store.root),
                "run_id": run_id,
                "sample": {"id12": id12, "source_id": source_id, "input_id": aligned.input_id},
                "derived": [
                    {"label": run.derived.spec.label(), "input_id": run.derived.input_id, "counted_reads": run.derived.counted_reads}
                    for run in runs
                ],
                "synthetic": synthetic,
                "steps": [result.public() for result in results],
                "failed": [{"algorithm_id": r.algorithm_id, "input": r.input_label, "first_line": r.first_line} for r in failed],
            }
            lines = [f"ROOT    {store.root}", f"RUN     {run_id}", f"SAMPLE  {id12}"]
            for row in synthetic:
                recovered = "-" if row["tf_recovered"] is None else f"{row['tf_recovered']:.3f}"
                verdict = "recovered" if row["recovered_within_tolerance"] else "not recovered"
                lines.append(
                    f"SPIKE   TF {row['tf_expected']:.2f} -> {recovered}  ({verdict} within ±{TF_TOLERANCE}; {row['status']})"
                )
            if kind == "split":
                lines.append("SPLIT   halves A and B computed: " + ", ".join(sorted({r.algorithm_id for r in results if r.status == 'ok'})))
            for result in failed:
                lines.append(f"FAILED  {result.algorithm_id}@{result.input_label}: {result.first_line}")
            lines.append(f"DONE    {time.monotonic() - started:.0f}s")
            code = EXIT_PARTIAL if failed else EXIT_OK
            status = "partial_failure" if failed else "ok"
            log.append("run_end", status=status, exit_code=code)
            payload = _local_envelope(_result(f"lab simulate {kind}", status, f"simulate {kind}: {len(runs)} derived input(s)", data=data))
            human = _with_human(payload, lines, replace_data=True)
            human["_human"]["bare"] = True
            return code, human
    finally:
        signal.signal(signal.SIGTERM, previous)


def _pid() -> int:
    import os

    return os.getpid()


__all__ = ["add_simulate_parser", "parse_fractions", "simulate_command"]
