"""Read-side paired tumour-fraction context; no combined score or cutoff."""

from __future__ import annotations

import math
import statistics
from typing import TYPE_CHECKING, Any

from .cohort_contracts import CohortGeneration, CohortMember, project_metric
from .interpretation.engine import COHORT_MIN

if TYPE_CHECKING:
    from .views import SampleView

CAPTION = "These signals share inputs and are not independent confirmation; agreement is not validation."
WGD_CAVEAT = "Copy-number tumour fraction can shift with whole-genome doubling; method agreement is not validation."


def _metric(sample: SampleView, algorithm_id: str, metric_id: str) -> Any:
    envelope = sample.full.results.get(algorithm_id)
    if envelope is None or envelope.status != "ok":
        return None
    return next((item.value for item in envelope.metrics if item.metric_id == metric_id), None)


def _number(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
        return float(value)
    return None


def _baseline(
    sample: SampleView, generation: CohortGeneration | None, target: CohortMember | None,
    algorithm_id: str, metric_id: str,
) -> tuple[float | None, int]:
    """Median of source-excluded, protocol-compatible controls only."""
    if generation is None or target is None or target.input_id != sample.full.input_id:
        return None, 0
    envelope = sample.full.results.get(algorithm_id)
    target_result = next((row for row in target.results if row.algorithm_id == algorithm_id), None)
    if (envelope is None or envelope.status != "ok" or target_result is None
            or target_result.parameters_sha256 != envelope.provenance.parameters_sha256):
        return None, 0
    projected = project_metric(
        generation, algorithm_id=algorithm_id, metric_id=metric_id,
        cohort_compat_key=target_result.cohort_compat_key, target_source_id=target.source_id,
    )
    if projected.percentile_n < COHORT_MIN:
        return None, projected.percentile_n
    return float(statistics.median(projected.values)), projected.percentile_n


def report_tf_concordance(
    sample: SampleView, generation: CohortGeneration | None = None,
    target: CohortMember | None = None,
) -> dict[str, Any]:
    """Three paired observations, each with an explicit unavailable reason."""
    cn_envelope = sample.full.results.get("copy-number")
    policy = _metric(sample, "copy-number", "pon_policy")
    cn = _number(_metric(sample, "copy-number", "tumor_fraction"))
    cn_identifiable = _metric(sample, "copy-number", "identifiable") is True
    cn_state = (
        "missing_cn" if cn_envelope is None or cn_envelope.status != "ok" else
        "no_pon" if policy in (None, "none") else
        "unidentifiable_cn" if not cn_identifiable else
        "missing_cn" if cn is None else "ok"
    )
    meth = _number(_metric(sample, "meth-tf", "tumor_fraction"))
    meth_identifiable = _metric(sample, "meth-tf", "identifiable") is True
    size = _number(_metric(sample, "fragment-size", "mode_bp"))
    mt = _number(_metric(sample, "mtdna", "chrm_fraction"))
    size_baseline, size_n = _baseline(sample, generation, target, "fragment-size", "mode_bp")
    mt_baseline, mt_n = _baseline(sample, generation, target, "mtdna", "chrm_fraction")

    def pair(
        pair_id: str, label: str, x_label: str, x: float | None,
        state: str, *, n: int | None = None, baseline: float | None = None,
    ) -> dict[str, Any]:
        return {
            "id": pair_id, "label": label, "x_label": x_label, "x": x if state == "ok" else None,
            "y_label": "PoN-qualified copy-number tumour fraction",
            "y": cn if state == "ok" else None, "state": state, "reference_n": n,
            "baseline_median": baseline,
        }

    meth_state = cn_state if cn_state != "ok" else (
        "unidentifiable_meth_tf" if _metric(sample, "meth-tf", "identifiable") is False else
        "missing_meth_tf" if meth is None else "unidentifiable_meth_tf" if not meth_identifiable else "ok"
    )
    size_state = cn_state if cn_state != "ok" else (
        "missing_size" if size is None else "no_size_baseline" if size_baseline is None else "ok"
    )
    mt_state = cn_state if cn_state != "ok" else (
        "missing_mtdna" if mt is None else "no_mtdna_baseline" if mt_baseline is None else "ok"
    )
    return {
        "caption": CAPTION, "wgd_caveat": WGD_CAVEAT,
        "pairs": [
            pair("cn-meth", "Methylation TF vs copy-number TF",
                 "methylation tumour fraction", meth, meth_state),
            pair("cn-size", "Size shift vs copy-number TF",
                 "fragment-size mode shift from cohort median (bp)",
                 size - size_baseline if size is not None and size_baseline is not None else None,
                 size_state, n=size_n, baseline=size_baseline),
            pair("cn-mtdna", "mtDNA trend vs copy-number TF",
                 "chrM fraction shift from cohort median",
                 mt - mt_baseline if mt is not None and mt_baseline is not None else None,
                 mt_state, n=mt_n, baseline=mt_baseline),
        ],
    }


__all__ = ["CAPTION", "WGD_CAVEAT", "report_tf_concordance"]
