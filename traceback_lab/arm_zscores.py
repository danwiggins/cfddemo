"""Read-side, cohort-relative arm z scores from pinned arm-summary results."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from .algorithms.arm_summary import ARMS, ARM_SUMMARY
from .arm_zscore_core import ArmObservation, ArmScore, score_arm
from .cohorts import member_for_generation, read_current
from .problems import LabProblem
from .store import LabStore


@dataclass(frozen=True)
class ArmZReport:
    source_id: str
    cohort_id: str
    cohort_version: str | None
    arm_asset_sha256: str | None
    target_panel_sha256: str
    target_panel_n: int
    target_excluded: bool
    target_arm_result_available: bool
    scores: dict[str, ArmScore]

    def public(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id, "cohort_id": self.cohort_id,
            "cohort_version": self.cohort_version,
            "arm_asset_sha256": self.arm_asset_sha256,
            "target_panel_sha256": self.target_panel_sha256,
            "target_panel_n": self.target_panel_n,
            "target_excluded": self.target_excluded,
            "target_arm_result_available": self.target_arm_result_available,
            "scores": {arm: asdict(score) for arm, score in self.scores.items()},
        }


def report_arm_zscores(store: LabStore, cohort_id: str, source_id: str) -> ArmZReport:
    """Pin one generation; exclude the target and require ten matched controls.

    A member of an exactly-ten-source cohort has only nine eligible controls
    after self-exclusion, so its z scores remain unavailable. The method
    makes no abnormality call and does not infer donor independence.
    """

    generation = read_current(store, cohort_id)
    if generation is None:
        raise LabProblem(
            "TBX-LAB-017", cause=f"cohort {cohort_id} has no published generation",
            fix="Build that cohort, then rerun arm-zscores with the same --cohort and --root",
            data={"cohort_id": cohort_id},
        )
    if generation.pon_build is None or generation.pon_asset_sha256 is None:
        raise LabProblem(
            "TBX-LAB-020", cause=f"cohort {cohort_id} has no verified panel of normals",
            fix="Build an internal protocol-matched cohort before running arm-zscores",
            data={"cohort_id": cohort_id, "field": "panel of normals"},
        )
    asset_digests = ARM_SUMMARY.asset_sha256s(store.root)
    asset_sha256 = asset_digests[0] if len(asset_digests) == 1 else None

    target = member_for_generation(store, generation, source_id)
    target_excluded = target.pon_asset_sha256 is not None
    target_panel_sha256 = target.pon_asset_sha256 or generation.pon_asset_sha256
    target_panel_n = generation.pon_n - int(target_excluded)
    selected = {result.algorithm_id: result for result in target.results}
    arm_result = selected.get("arm-summary")
    depth_result = selected.get("read-summary")
    depth = depth_result.metrics.get("eligible_reads") if depth_result is not None else None
    eligible = depth if type(depth) is int else None
    protocol = arm_result.cohort_compat_key if arm_result is not None and generation.pon_build else None
    controls = []
    if asset_sha256 is not None:
        for member in generation.members:
            result = next((item for item in member.results if item.algorithm_id == "arm-summary"), None)
            if result is not None:
                controls.append((member.source_id, result))
    scores: dict[str, ArmScore] = {}
    for arm in ARMS:
        metric_id = f"{arm}_log2"
        target_value = arm_result.metrics.get(metric_id) if arm_result is not None else None
        observations = tuple(ArmObservation(
            source_id=control_source, arm_id=arm,
            protocol_key=result.cohort_compat_key,
            arm_asset_sha256=asset_sha256,
            log2=result.metrics.get(metric_id),
            bins_used=result.metric_n.get(metric_id),
        ) for control_source, result in controls) if asset_sha256 is not None else ()
        scores[arm] = score_arm(
            target_source_id=source_id, arm_id=arm,
            target_log2=target_value,
            target_bins_used=arm_result.metric_n.get(metric_id) if arm_result is not None else None,
            target_protocol_key=protocol, eligible_reads=eligible,
            arm_asset_sha256=asset_sha256, observations=observations,
        )
    return ArmZReport(
        source_id, cohort_id, generation.version_id, asset_sha256,
        target_panel_sha256, target_panel_n, target_excluded, arm_result is not None, scores,
    )


__all__ = ["ArmZReport", "report_arm_zscores"]
