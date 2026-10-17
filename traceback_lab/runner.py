"""Per-input algorithm execution: preflight, run or skip, publish (D19, D20, D22, D35).

Each algorithm runs in a worker process in its own process group with a
timeout, so a failure is isolated and nothing it started outlives it.  A
failure is written as ``status: failed`` with the last 50 scrubbed stderr
lines; there is no automatic retry. A rerun recomputes missing or failed
results and dependents whose selected upstream attempt changed, even when
the D19 execution identity is unchanged. ``force`` recomputes all results;
skipped results are preflighted again every time, because depth can change.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from traceback_runner.serialization import canonical_json_bytes

from .envelope import Depth, Failure, ResultEnvelope, provenance
from .pipeline import Stage, run_pipeline
from .registry import (
    Algorithm,
    InputContext,
    PlanItem,
    Ready,
    Skip,
    context_to_json,
    execution_identity,
)
from .scrub import Scrubber
from .store import LabStore, RunLog, WriterLock

DEFAULT_TIMEOUT_S = 3600.0


@dataclass
class StepResult:
    algorithm_id: str
    status: str  # ok | skipped | failed
    upstream_only: bool
    parameters_sha256: str
    skip_reason: str | None = None
    skip_message: str | None = None
    first_line: str | None = None
    duration_s: float | None = None
    peak_rss_bytes: int | None = None
    cached: bool = False
    input_label: str = "full"
    artifacts_dir: str | None = None
    input_id: str | None = None
    attempt_id: str | None = None

    def public(self) -> dict[str, Any]:
        return {
            "algorithm_id": self.algorithm_id,
            "status": self.status,
            "upstream_only": self.upstream_only,
            "skip_reason": self.skip_reason,
            "first_line": self.first_line,
            "duration_s": self.duration_s,
            "peak_rss_bytes": self.peak_rss_bytes,
            "cached": self.cached,
            "input": self.input_label,
        }


@dataclass
class RunTarget:
    """One input the plan runs against (the aligned input, or a derived one)."""

    input_id: str
    source_id: str
    input_dir: Path
    reference_id: str
    reference_sha256: str
    label: str = "full"
    eligible_reads: int | None = None
    approx_depth_x: float | None = None
    depth: Depth | None = None
    derived: Mapping[str, Any] | None = None
    example: bool = False
    extras: dict[str, Any] = field(default_factory=dict)

    def context(self, root: Path) -> InputContext:
        return InputContext(
            root=root,
            source_id=self.source_id,
            input_id=self.input_id,
            reference_id=self.reference_id,
            reference_sha256=self.reference_sha256,
            input_dir=self.input_dir,
            eligible_reads=self.eligible_reads,
            approx_depth_x=self.approx_depth_x,
            derived=self.derived,
            example=self.example,
            extras=self.extras,
        )


def _worker_env() -> dict[str, str]:
    env = dict(os.environ)
    paths = [entry for entry in sys.path if entry and os.path.isdir(entry)]
    env["PYTHONPATH"] = os.pathsep.join(paths)
    env.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    return env


def _envelope(
    algorithm: Algorithm,
    target: RunTarget,
    parameters_sha256: str,
    *,
    status: str,
    skip: Skip | None = None,
    failure: Failure | None = None,
    metrics: Sequence[Mapping[str, Any]] = (),
    panels: Sequence[Mapping[str, Any]] = (),
    duration_s: float | None = None,
) -> ResultEnvelope:
    return ResultEnvelope.model_validate(
        {
            "algorithm_id": algorithm.id,
            "algorithm_version": algorithm.version,
            "status": status,
            "skip_reason": skip.reason_code if skip else None,
            "skip_message": skip.message[:300] if skip else None,
            "provenance": provenance(
                tool=f"traceback_lab.{algorithm.id.replace('-', '_')}",
                parameters_sha256=parameters_sha256,
                source_id=target.source_id,
                input_id=target.input_id,
                reference_id=target.reference_id,
            ),
            "depth": target.depth.model_dump() if target.depth is not None else None,
            "metrics": list(metrics),
            "panels": list(panels),
            "failure": failure.model_dump() if failure else None,
            "duration_s": round(duration_s, 3) if duration_s is not None else None,
        }
    )


def _selected_upstream_attempts(
    store: LabStore, done: Mapping[str, StepResult], names: Sequence[str],
) -> tuple[dict[str, str], ...] | None:
    """Pin direct attempts actually consumed, including parent-input aliases."""

    rows: list[dict[str, str]] = []
    for name in sorted(names):
        result = done.get(name)
        if (result is None or result.status != "ok" or result.input_id is None
                or result.attempt_id is None):
            return None
        selected = store.selected_attempt(result.input_id, name, result.parameters_sha256)
        if selected is None or selected.name != result.attempt_id:
            return None
        rows.append({
            "algorithm_id": name, "input_id": result.input_id,
            "parameters_sha256": result.parameters_sha256, "attempt_id": result.attempt_id,
        })
    return tuple(rows)


def run_plan(
    store: LabStore,
    lock: WriterLock | None,
    log: RunLog,
    target: RunTarget,
    items: Sequence[PlanItem],
    entries: Mapping[str, str],
    *,
    run_id: str,
    force: bool = False,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    scrubber: Scrubber,
    progress: Callable[[str], None] | None = None,
    echo: Callable[[str], None] | None = None,
    upstream_results: dict[str, StepResult] | None = None,
) -> list[StepResult]:
    """Run ``items`` (registry order) against ``target``; returns one result each."""

    results: list[StepResult] = []
    done: dict[str, StepResult] = dict(upstream_results or {})
    scratch_root = store.lab / "tmp" / run_id
    for item in items:
        algorithm = item.algorithm
        upstream = [
            {"algorithm_id": name, "parameters_sha256": done[name].parameters_sha256}
            for name in algorithm.upstream
            if name in done
        ]
        assets_of = getattr(algorithm, "asset_sha256s", None)
        planned_assets = list(assets_of(store.root) if callable(assets_of) else ())
        planned_tools = [tool.identity() for tool in algorithm.tools]
        identity = execution_identity(
            algorithm,
            reference_sha256=target.reference_sha256,
            upstream=upstream,
            # Consumed assets enter the execution identity (D19), so a
            # re-registered asset never reuses an older result.
            asset_sha256s=planned_assets,
            tool_pins=planned_tools,
        )
        step = algorithm.id if target.label == "full" else f"{algorithm.id}@{target.label}"
        existing = store.read_result(target.input_id, algorithm.id, identity)
        blocked = [name for name in algorithm.upstream if done.get(name) is None or done[name].status != "ok"]
        consumed = _selected_upstream_attempts(store, done, algorithm.upstream) if not blocked else None
        cache_valid = not algorithm.upstream or (
            consumed is not None
            and store.selected_upstream_selections(target.input_id, algorithm.id, identity) == consumed
        )
        if existing is not None and existing.status == "ok" and not force and not blocked and cache_valid:
            store.reselect_cached_result(target.input_id, algorithm.id, identity)
            selected = store.selected_attempt(target.input_id, algorithm.id, identity)
            result = StepResult(
                algorithm.id, "ok", item.upstream_only, identity, cached=True, input_label=target.label,
                input_id=target.input_id, attempt_id=selected.name if selected is not None else None,
            )
            _after_ok(store, target, algorithm, identity, result, existing)
            results.append(result)
            done[algorithm.id] = result
            if progress:
                progress(f"{step:<24} ok (cached)")
            continue
        target.extras["upstream"] = {
            name: done[name].artifacts_dir
            for name in algorithm.upstream
            if name in done and done[name].artifacts_dir
        }
        target.extras["planned_asset_sha256s"] = planned_assets
        target.extras["planned_tool_pins"] = planned_tools
        ctx = target.context(store.root)
        decision: Ready | Skip
        if blocked:
            decision = Skip("upstream", f"upstream not ok: {', '.join(blocked)}")
        else:
            try:
                decision = algorithm.preflight(ctx)
            except Exception as exc:  # noqa: BLE001 - a broken preflight is a failed result
                result = _publish_failure(
                    store, algorithm, target, identity, item, log, step,
                    first_line=scrubber.line(f"preflight raised {type(exc).__name__}: {exc}"),
                    tail=[], exit_code=None, timed_out=False, duration_s=0.0, peak_rss=None,
                )
                results.append(result)
                done[algorithm.id] = result
                if progress:
                    progress(f"{step:<24} failed: {result.first_line}")
                continue
        if isinstance(decision, Skip):
            same = (
                existing is not None
                and existing.status == "skipped"
                and existing.skip_reason == decision.reason_code
                and existing.skip_message == decision.message[:300]
            )
            if not same:
                attempt = store.publish_result(
                    _envelope(algorithm, target, identity, status="skipped", skip=decision),
                    input_id=target.input_id,
                )
            else:
                selected = store.selected_attempt(target.input_id, algorithm.id, identity)
                attempt = selected.name if selected is not None else None
            log.append(
                "step_end",
                step=step,
                status="skipped",
                skip_reason=decision.reason_code,
                parameters_sha256=identity,
            )
            result = StepResult(
                algorithm.id,
                "skipped",
                item.upstream_only,
                identity,
                skip_reason=decision.reason_code,
                skip_message=decision.message,
                input_label=target.label,
                input_id=target.input_id,
                attempt_id=attempt,
            )
            results.append(result)
            done[algorithm.id] = result
            if progress:
                progress(f"{step:<24} skipped ({decision.reason_code}): {decision.message}")
            continue
        # Ready: run it in a worker process group.
        if lock is not None:
            lock.set_step(step)
        log.append("step_start", step=step, parameters_sha256=identity)
        if progress:
            progress(f"{step:<24} running{' (upstream)' if item.upstream_only else ''}")
        scratch = scratch_root / f"{algorithm.id}-{target.label}"
        shutil.rmtree(scratch, ignore_errors=True)
        scratch.mkdir(parents=True)
        request = scratch / "request.json"
        request.write_bytes(
            canonical_json_bytes(
                {"entry": entries[algorithm.id], "context": context_to_json(ctx), "out_dir": str(scratch)}
            )
        )
        try:
            outcome = run_pipeline(
                [Stage("worker", [sys.executable, "-m", "traceback_lab.worker", str(request)])],
                timeout_s=timeout_s,
                env=_worker_env(),
                cwd=scratch,
                echo=echo,
            )
            if outcome.ok:
                try:
                    produced = json.loads((scratch / "output.json").read_bytes())
                    artifacts = {
                        str(path.relative_to(scratch))
                        for path in sorted((scratch / "artifacts").rglob("*"))
                        if path.is_file() and not path.is_symlink()
                    }
                    envelope = _envelope(
                        algorithm,
                        target,
                        identity,
                        status="ok",
                        metrics=produced.get("metrics", []),
                        panels=produced.get("panels", []),
                        duration_s=outcome.duration_s,
                    )
                    missing = [p.data_ref for p in envelope.panels if p.data_ref not in artifacts]
                    if missing:
                        raise ValueError(f"panel data_ref not produced: {missing[0]}")
                except (OSError, ValueError) as exc:
                    result = _publish_failure(
                        store, algorithm, target, identity, item, log, step,
                        first_line=scrubber.line(f"invalid output: {type(exc).__name__}: {exc}"),
                        tail=[], exit_code=0, timed_out=False,
                        duration_s=outcome.duration_s, peak_rss=outcome.peak_rss_bytes,
                    )
                else:
                    attempt = store.publish_result(
                        envelope,
                        input_id=target.input_id,
                        artifacts_dir=scratch / "artifacts",
                        upstream_selections=consumed,
                    )
                    log.append(
                        "step_end",
                        step=step,
                        status="ok",
                        duration_s=round(outcome.duration_s, 3),
                        peak_rss_bytes=outcome.peak_rss_bytes,
                        parameters_sha256=identity,
                        tool_versions={tool.name: tool.version for tool in algorithm.tools},
                    )
                    result = StepResult(
                        algorithm.id,
                        "ok",
                        item.upstream_only,
                        identity,
                        duration_s=outcome.duration_s,
                        peak_rss_bytes=outcome.peak_rss_bytes,
                        input_label=target.label,
                        input_id=target.input_id,
                        attempt_id=attempt,
                    )
                    _after_ok(store, target, algorithm, identity, result, envelope)
            else:
                stage = outcome.stages[0]
                tail = stage.stderr.scrubbed(scrubber, last=50)
                lines = [line for line in tail if line.strip()]
                first = "timed out" if outcome.timed_out else (lines[-1] if lines else f"exited {stage.returncode}")
                result = _publish_failure(
                    store, algorithm, target, identity, item, log, step,
                    first_line=scrubber.line(first), tail=tail, exit_code=stage.returncode,
                    timed_out=outcome.timed_out, duration_s=outcome.duration_s,
                    peak_rss=outcome.peak_rss_bytes,
                )
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
        results.append(result)
        done[algorithm.id] = result
        if progress:
            if result.status == "ok":
                progress(f"{step:<24} ok ({result.duration_s or 0:.1f}s)")
            else:
                progress(f"{step:<24} failed: {result.first_line}")
    shutil.rmtree(scratch_root, ignore_errors=True)
    return results


DEPTH_SOURCE = "read-summary"


def _after_ok(
    store: LabStore,
    target: RunTarget,
    algorithm: Algorithm,
    identity: str,
    result: StepResult,
    envelope: ResultEnvelope,
) -> None:
    """Record where the result's artifacts live; the read summary also sets
    the input's eligible-read depth for every later preflight (D33)."""

    artifacts = store.selected_artifacts(target.input_id, algorithm.id, identity)
    result.artifacts_dir = str(artifacts) if artifacts is not None else None
    if algorithm.id == DEPTH_SOURCE and target.derived is None:
        values = {metric.metric_id: metric.value for metric in envelope.metrics}
        eligible = values.get("eligible_reads")
        depth_x = values.get("approx_depth_x")
        if isinstance(eligible, int) and isinstance(depth_x, (int, float)):
            target.eligible_reads = eligible
            target.approx_depth_x = float(depth_x)
            target.depth = Depth(
                eligible_reads=eligible,
                index_mapped_upper_bound=values.get("index_mapped_upper_bound"),
                mean_span_bp=values.get("mean_span_bp"),
                approx_depth_x=float(depth_x),
            )


def _publish_failure(
    store: LabStore,
    algorithm: Algorithm,
    target: RunTarget,
    identity: str,
    item: PlanItem,
    log: RunLog,
    step: str,
    *,
    first_line: str,
    tail: list[str],
    exit_code: int | None,
    timed_out: bool,
    duration_s: float,
    peak_rss: int | None,
) -> StepResult:
    failure = Failure(
        first_line=first_line[:300] or "failed",
        stderr_tail=[line[:300] for line in tail[-50:]],
        exit_code=exit_code,
        timed_out=timed_out,
    )
    attempt = store.publish_result(
        _envelope(algorithm, target, identity, status="failed", failure=failure, duration_s=duration_s),
        input_id=target.input_id,
    )
    log.append(
        "step_end",
        step=step,
        status="failed",
        first_line=failure.first_line,
        duration_s=round(duration_s, 3),
        peak_rss_bytes=peak_rss,
        timed_out=timed_out,
        parameters_sha256=identity,
    )
    return StepResult(
        algorithm.id,
        "failed",
        item.upstream_only,
        identity,
        first_line=failure.first_line,
        duration_s=duration_s,
        peak_rss_bytes=peak_rss,
        input_label=target.label,
        input_id=target.input_id,
        attempt_id=attempt,
    )


__all__ = ["DEFAULT_TIMEOUT_S", "RunTarget", "StepResult", "run_plan"]
