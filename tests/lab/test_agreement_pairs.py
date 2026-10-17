"""Agreement pairs use source identity from one immutable cohort generation."""

from __future__ import annotations

from types import SimpleNamespace

from traceback_lab.cohort_contracts import CohortGeneration, CohortMember, CohortResult
from traceback_lab.web.api import _agreement_pairs


PARAMETERS = "b" * 64
COMPAT = "a" * 64
OTHER = "c" * 64


def _result(algorithm: str, metric: str, value: float, compat: str = COMPAT) -> CohortResult:
    return CohortResult(
        algorithm_id=algorithm, parameters_sha256=PARAMETERS,
        cohort_compat_key=compat, metrics={metric: value},
    )


def _member(n: int, *, copy: bool = True, mtdna_compat: str = COMPAT) -> CohortMember:
    results = [
        _result("fragment-size", "mode_bp", 100.0 * n),
        _result("mtdna", "chrm_fraction", 0.01 * n, mtdna_compat),
    ]
    if copy:
        results.append(_result("copy-number", "tumor_fraction", 0.1 * n))
    return CohortMember(
        source_id=f"{n:064x}", input_id=f"{100 + n:064x}",
        aligner_identity="minimap2 2.28; samtools 1.20", results=tuple(results),
    )


def _sample() -> SimpleNamespace:
    results = {algorithm: SimpleNamespace(
        status="ok", provenance=SimpleNamespace(parameters_sha256=PARAMETERS),
    ) for algorithm in ("fragment-size", "mtdna", "copy-number")}
    return SimpleNamespace(full=SimpleNamespace(results=results))


def test_pairs_join_by_source_after_compatibility_and_self_exclusion() -> None:
    target = _member(1)
    generation = CohortGeneration(cohort_id="healthy-v1", version=7, members=(
        target, _member(2), _member(3, copy=False), _member(4, mtdna_compat=OTHER),
    ))

    size_cn, mtdna_cn = _agreement_pairs(_sample(), generation, target, target.source_id, "ready")
    assert size_cn["cohort_version"] == "healthy-v1:7"
    assert size_cn["state"] == "ready" and size_cn["eligible_n"] == 2
    assert size_cn["points"] == [
        {"source_id": f"{2:064x}", "display_id": f"{2:064x}", "x": 200.0, "y": 0.2},
        {"source_id": f"{4:064x}", "display_id": f"{4:064x}", "x": 400.0, "y": 0.4},
    ]
    assert size_cn["excluded"]["x"]["self"] == 1
    assert size_cn["excluded"]["unpaired_x"] == 1
    assert mtdna_cn["eligible_n"] == 1
    assert mtdna_cn["points"] == [{"source_id": f"{2:064x}", "display_id": f"{2:064x}", "x": 0.02, "y": 0.2}]
    assert len({point["source_id"][:12] for point in size_cn["points"]}) == 1
    assert len({point["display_id"] for point in size_cn["points"]}) == 2
    assert mtdna_cn["excluded"]["x"]["incompatible"] == 1


def test_pairs_fail_closed_when_target_execution_or_generation_unavailable() -> None:
    target = _member(1)
    generation = CohortGeneration(cohort_id="healthy-v1", version=7, members=(target, _member(2)))
    sample = _sample()
    sample.full.results["copy-number"].provenance.parameters_sha256 = OTHER
    rows = _agreement_pairs(sample, generation, target, target.source_id, "ready")
    assert all(row["state"] == "target_metric_unavailable" and not row["points"] for row in rows)

    rows = _agreement_pairs(_sample(), None, None, target.source_id, "not_built")
    assert all(row["state"] == "not_built" and not row["points"] for row in rows)
