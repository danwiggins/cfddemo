"""Derived-input runs: positive controls and (with S5) the downsample curve (D24, D25).

``run_derived`` materializes the requested derived inputs of one aligned
input in one pass, records them on the sample's source record, and runs the
plan against each as a row-mask target.  The read summary is the parent's;
only algorithms that consume the read summary or a counting BAM run on a
derived input (intermediates that read the full BAM never do).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .derived import (
    DEFAULT_TFS,
    DerivedInput,
    DerivedSpec,
    centromeres_from_table,
    cna,
    hg38_arms,
    materialize,
    record_derived,
    split,
)
from .read_summary import ReadSummary, depth_stamp, derived_mask
from .registry import PlanItem
from .runner import RunTarget, StepResult, run_plan
from .scrub import Scrubber
from .store import LabStore, RunLog, WriterLock

TF_TOLERANCE = 0.05
DETECTED_MIN = 0.01  # a recovered TF below this is 'not detected'
DERIVED_CONSUMES = ("read_summary", "meth_calls", "counting_bam", "full_bam")


@dataclass
class DerivedRun:
    derived: DerivedInput
    results: list[StepResult]


def cna_specs(root: Path, parent_bam: Path, tfs: Sequence[float]) -> list[DerivedSpec] | str:
    """CNA spike specs on the default arms, or the reason they cannot be built."""

    import pysam

    from .adapters import ichor
    from .algorithms.copy_number import toolchain_or_none

    toolchain = toolchain_or_none()
    if toolchain is None:
        return "missing_toolchain"
    try:
        assets = ichor.resolve_assets(root, toolchain)
    except Exception:  # noqa: BLE001
        return "missing_asset"
    with pysam.AlignmentFile(str(parent_bam), "rb") as bam:
        lengths = dict(zip(bam.references, bam.lengths))
    centromeres = centromeres_from_table(assets.centromere)
    try:
        arms = hg38_arms(centromeres, lengths)
    except KeyError:
        return "example_reference"
    return [cna(tf, arms) for tf in tfs]


def split_specs() -> list[DerivedSpec]:
    return [split("A"), split("B")]


def run_derived(
    store: LabStore,
    lock: WriterLock | None,
    log: RunLog,
    parent: RunTarget,
    specs: Sequence[DerivedSpec],
    items: Sequence[PlanItem],
    entries: Mapping[str, str],
    *,
    run_id: str,
    read_summary: StepResult | None,
    force: bool = False,
    timeout_s: float,
    scrubber: Scrubber,
    progress: Callable[[str], None] | None = None,
    parent_upstream: Mapping[str, StepResult] | None = None,
) -> list[DerivedRun]:
    if not specs:
        return []
    from .adapters import ichor

    signed = ichor.default_parameters().signed
    if lock is not None:
        lock.set_step("derive")
    if progress:
        progress(f"{'derive':<24} {len(specs)} derived input(s): " + ", ".join(spec.label() for spec in specs))
    # Intermediates that read the parent BAM (read-summary) never run on a
    # derived input; other full-BAM consumers (cell-origin) get a transient BAM.
    runnable = [
        item for item in items
        if item.algorithm.consumes in DERIVED_CONSUMES and item.algorithm.id != "read-summary"
    ]
    summary = None
    if read_summary is not None and read_summary.artifacts_dir:
        try:
            summary = ReadSummary.open(Path(read_summary.artifacts_dir) / "columns")
        except (OSError, ValueError):
            summary = None
    if summary is None:
        # Row masks need the parent's columns; without them a downsample or
        # split half would carry the parent's depth.  Only the CNA spike
        # (counting BAM only, parent depth by definition) can still run.
        dropped = [spec.label() for spec in specs if spec.transform in ("downsample", "split")]
        specs = [spec for spec in specs if spec.transform not in ("downsample", "split")]
        if dropped and progress:
            progress(f"{'derive':<24} skipped {', '.join(dropped)}: read summary unavailable")
        if not specs:
            return []
    needs_counting = any(item.algorithm.consumes == "counting_bam" for item in runnable)
    derived = materialize(
        store,
        parent.input_id,
        parent.input_dir / "aligned.bam",
        specs,
        counting_contigs=signed.counting_contigs,
        min_mapq=signed.min_mapq,
        counting=needs_counting,
    )
    record_derived(store, parent.source_id, derived.values())
    # Intermediates computed on the parent (read-summary, meth-calls) are
    # reused by every derived input through its row mask (D28, D33).
    upstream = dict(parent_upstream or {})
    if read_summary is not None:
        upstream["read-summary"] = read_summary
    runs: list[DerivedRun] = []
    for spec in specs:
        item = derived[spec.label()]
        mask_spec = spec.mask_spec()
        target = RunTarget(
            input_id=item.input_id,
            source_id=parent.source_id,
            input_dir=parent.input_dir,
            reference_id=parent.reference_id,
            reference_sha256=parent.reference_sha256,
            label=spec.label(),
            derived=mask_spec,
            example=parent.example,
            extras={
                **{key: value for key, value in parent.extras.items() if key != "upstream"},
                **(
                    {
                        "counting_bam": str(item.counting_bam),
                        "counted_reads": item.counted_reads,
                        "records_scanned": item.records_scanned,
                    }
                    if item.counting is not None
                    else {}
                ),
            },
        )
        if summary is not None and spec.transform in ("downsample", "split"):
            stamp = depth_stamp(summary, derived_mask(summary, mask_spec))
        else:
            stamp = {
                "eligible_reads": parent.eligible_reads or 0,
                "approx_depth_x": parent.approx_depth_x or 0.0,
                "mean_span_bp": None,
                "index_mapped_upper_bound": None,
            }
        from .envelope import Depth

        target.eligible_reads = stamp["eligible_reads"]
        target.approx_depth_x = stamp["approx_depth_x"]
        target.depth = Depth(
            eligible_reads=stamp["eligible_reads"],
            mean_span_bp=stamp["mean_span_bp"],
            approx_depth_x=stamp["approx_depth_x"],
            index_mapped_upper_bound=None,
        )
        chosen = (
            [entry for entry in runnable if entry.algorithm.consumes == "counting_bam"]
            if spec.transform == "cna"
            else runnable
        )
        results = run_plan(
            store,
            lock,
            log,
            target,
            chosen,
            entries,
            run_id=run_id,
            force=force,
            timeout_s=timeout_s,
            scrubber=scrubber,
            progress=progress,
            upstream_results=upstream,
        )
        runs.append(DerivedRun(item, results))
    return runs


def synthetic_summary(store: LabStore, runs: Sequence[DerivedRun]) -> list[dict[str, Any]]:
    """Expected vs recovered TF for every CNA spike (D25 a)."""

    rows: list[dict[str, Any]] = []
    for run in runs:
        if run.derived.spec.transform != "cna":
            continue
        expected = float(run.derived.spec.parameters["tf"])
        recovered = None
        status = "not run"
        for result in run.results:
            if result.algorithm_id != "copy-number":
                continue
            status = result.status if result.skip_reason is None else f"skipped ({result.skip_reason})"
            if result.status == "ok":
                envelope = store.read_result(run.derived.input_id, "copy-number", result.parameters_sha256)
                if envelope is not None:
                    values = {metric.metric_id: metric.value for metric in envelope.metrics}
                    recovered = values.get("tumor_fraction")
        rows.append(
            {
                "tf_expected": expected,
                "tf_recovered": recovered,
                # A spike counts as recovered only when it is detected at all:
                # at TF 0.03 a recovered 0.0 sits inside ±0.05 by arithmetic alone.
                "detected": recovered is not None and recovered >= DETECTED_MIN,
                "recovered_within_tolerance": recovered is not None
                and recovered >= DETECTED_MIN
                and abs(recovered - expected) <= TF_TOLERANCE,
                "status": status,
                "derived_input_id": run.derived.input_id,
                "duplicated": (run.derived.counting or {}).get("duplicated"),
                "removed": (run.derived.counting or {}).get("removed"),
            }
        )
    return rows


__all__ = [
    "DEFAULT_TFS",
    "DerivedRun",
    "TF_TOLERANCE",
    "cna_specs",
    "run_derived",
    "split_specs",
    "synthetic_summary",
]
