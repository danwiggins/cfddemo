"""Exploratory autosomal arm summaries from a selected PoN copy-number track.

Arm boundaries come from the operator-registered, pinned ichorCNA GRCh38
centromere asset and the input reference's contig lengths. A corrected bin
contributes only when it is finite, unmasked, and wholly within one arm;
the arm value is the unweighted median of those bins. This is an explicit
software aggregation contract, not a clinical CNV threshold or reference.
The cohort-relative z calculation is read-side and separately gated.
"""

from __future__ import annotations

import hashlib
import json
import math
import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..adapters import ichor
from ..read_summary import ReadSummary
from ..registry import AlgorithmOutput, InputContext, Ready, Requirements, Skip, ToolPin
from ._base import Base, metric
from .copy_number import toolchain_or_none

ARMS = tuple(f"chr{chrom}{side}" for chrom in range(1, 23) for side in ("p", "q"))
POLICY = "finite-unmasked-whole-bin-median-v1"


def centromeres_from_table(path: Path) -> dict[str, tuple[int, int]]:
    """Load the existing centromere parser only when an arm worker runs."""

    from ..derived import centromeres_from_table as parse

    return parse(path)


def arm_intervals(
    centromeres: Mapping[str, tuple[int, int]], lengths: Mapping[str, int],
) -> dict[str, tuple[str, int, int]]:
    """Resolve 44 non-overlapping arms; refuse missing or invalid reference data."""

    intervals: dict[str, tuple[str, int, int]] = {}
    for chrom in range(1, 23):
        contig = f"chr{chrom}"
        bare = str(chrom)
        centromere_key = contig if contig in centromeres else bare
        length_key = contig if contig in lengths else bare
        if centromere_key not in centromeres or length_key not in lengths:
            raise ValueError(f"{contig} is missing from the arm definition or reference")
        start, end = centromeres[centromere_key]
        length = lengths[length_key]
        if any(type(value) is not int for value in (start, end, length)) or not 0 < start < end < length:
            raise ValueError(f"{contig} has invalid arm boundaries")
        intervals[f"{contig}p"] = (length_key, 0, start)
        intervals[f"{contig}q"] = (length_key, end, length)
    return intervals


def summarize_arms(
    bins: Sequence[Mapping[str, Any]], intervals: Mapping[str, tuple[str, int, int]],
) -> dict[str, tuple[float | None, int]]:
    """Use only whole, unmasked, finite bins; expose missing arms as null."""

    selected: dict[str, list[float]] = {arm: [] for arm in intervals}
    seen: set[tuple[str, int, int]] = set()
    for index, item in enumerate(bins):
        contig, start, end = item.get("chr"), item.get("start"), item.get("end")
        value, mask = item.get("log2"), item.get("mask")
        if (not isinstance(contig, str) or type(start) is not int or type(end) is not int
                or not 0 <= start < end or (contig, start, end) in seen):
            raise ValueError(f"copy-number bin {index} has invalid or duplicate coordinates")
        seen.add((contig, start, end))
        if mask is not None and not isinstance(mask, str):
            raise ValueError(f"copy-number bin {index} has invalid mask")
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))
                                  or not math.isfinite(value)):
            raise ValueError(f"copy-number bin {index} has invalid log2")
        if mask is not None or value is None:
            continue
        for arm, (chrom, left, right) in intervals.items():
            if contig == chrom and left <= start and end <= right:
                selected[arm].append(float(value))
                break
    return {
        arm: (float(statistics.median(values)) if values else None, len(values))
        for arm, values in selected.items()
    }


def _registered_centromere(root: Path) -> tuple[Path, str] | None:
    from traceback_runner.references import ReferenceProblem

    toolchain = toolchain_or_none()
    if toolchain is None:
        return None
    try:
        centromere = ichor.resolve_assets(root, toolchain).centromere
        digest = hashlib.sha256(centromere.read_bytes()).hexdigest()
    except (OSError, ValueError, RuntimeError, ReferenceProblem):
        return None
    return centromere, digest


@dataclass
class ArmSummary(Base):
    family: str = "copy-number"
    consumes: str = "read_summary"
    upstream: tuple[str, ...] = ("read-summary", "copy-number")
    requires: Requirements = field(default_factory=lambda: Requirements(min_eligible_reads=2_000_000))
    tools: tuple[ToolPin, ...] = (ToolPin("arm-definition", "registered-ichor-centromere-v1", "gpl-copyleft"),)
    parameters: dict[str, Any] = field(default_factory=lambda: {
        "arm_definition": "registered-ichor-grch38-centromere", "aggregation": POLICY,
    })

    def asset_sha256s(self, root: Path) -> list[str]:
        asset = _registered_centromere(root)
        return [asset[1]] if asset is not None else []

    def preflight(self, ctx: InputContext) -> Ready | Skip:
        if ctx.example:
            return Skip("example_reference", "arm summaries need a GRCh38-scale reference")
        if ctx.extras.get("pon_selection") is None:
            return Skip("no_pon", "arm summaries need a selected protocol-matched panel of normals")
        if toolchain_or_none() is None:
            return Skip("missing_toolchain", "ichorCNA toolchain is unavailable")
        if _registered_centromere(ctx.root) is None:
            return Skip("missing_asset", "register the pinned ichorCNA method assets")
        return super().preflight(ctx)

    def run(self, ctx: InputContext, out: Path) -> AlgorithmOutput:
        asset = _registered_centromere(ctx.root)
        if asset is None:
            raise ValueError("the registered arm-definition asset is unavailable")
        centromere_path, digest = asset
        planned = ctx.extras.get("planned_arm_centromere_sha256")
        if not isinstance(planned, str) or digest != planned:
            raise ValueError("the arm-definition asset changed after execution planning")
        centromere_bytes = centromere_path.read_bytes()
        if hashlib.sha256(centromere_bytes).hexdigest() != planned:
            raise ValueError("the arm-definition asset changed before worker consumption")
        private_centromere = out / "arm-centromere.tsv"
        private_centromere.write_bytes(centromere_bytes)
        source = ctx.extras.get("upstream", {})
        summary_dir, cn_dir = source.get("read-summary"), source.get("copy-number")
        if not summary_dir or not cn_dir:
            raise ValueError("arm summary needs selected read-summary and copy-number artifacts")
        summary = ReadSummary.open(Path(summary_dir) / "columns")
        lengths = dict(zip(summary.contigs, summary.lengths))
        intervals = arm_intervals(centromeres_from_table(private_centromere), lengths)
        payload = json.loads((Path(cn_dir) / "cn_bins.json").read_bytes())
        if not isinstance(payload, dict) or not isinstance(payload.get("bins"), list):
            raise ValueError("selected copy-number result has no bin track")
        scored = summarize_arms(payload["bins"], intervals)
        rows = [
            {"arm": arm, "chr": intervals[arm][0], "start": intervals[arm][1],
             "end": intervals[arm][2], "log2": value, "bins_used": count}
            for arm, (value, count) in scored.items()
        ]
        artifact = {"schema": "traceback.lab.arm-summary.v1", "policy": POLICY,
                    "arm_asset_sha256": digest, "arms": rows}
        return AlgorithmOutput(
            metrics=[metric(f"{arm}_log2", value, unit="log2", n=count) for arm, (value, count) in scored.items()]
            + [metric("arms_with_value", sum(value is not None for value, _ in scored.values()), unit="arms")],
            artifacts={"arm_summaries.json": json.dumps(artifact, separators=(",", ":")).encode()},
        )


ARM_SUMMARY = ArmSummary("arm-summary")

__all__ = ["ARM_SUMMARY", "ARMS", "POLICY", "arm_intervals", "summarize_arms"]
