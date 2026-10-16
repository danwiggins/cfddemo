"""S5 Compare uses one immutable cohort snapshot and post-LOO D8 gates."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from traceback_lab.cohort_contracts import CohortGeneration, CohortMember, CohortResult
from traceback_lab.envelope import Metric, ResultEnvelope, provenance
from traceback_lab.interpretation.engine import Entry, Library, classify, load_library
from traceback_lab.store import LabStore
from traceback_lab.web import api as web_api

_KEY = "a" * 64
_PARAMETERS = "b" * 64
_TARGET = f"{1:064x}"


def _result(source: str, value: float) -> CohortResult:
    return CohortResult(
        algorithm_id="fragment-size", parameters_sha256=_PARAMETERS,
        cohort_compat_key=_KEY, metrics={"mode_bp": value},
    )


@pytest.mark.parametrize(
    "member_n,kind,state,eligible_n",
    [
        (10, "internal_cohort", "no_reference", 9),
        (11, "provisional_external", "below_typical", 10),
        (31, "internal_cohort", "markedly_below", 30),
    ],
)
def test_compare_pins_one_generation_and_gates_after_leave_one_out(
    tmp_path, monkeypatch, member_n, kind, state, eligible_n,
) -> None:
    members = tuple(
        CohortMember(
            source_id=f"{index:064x}", input_id=f"{index + 100:064x}",
            aligner_identity="minimap2 2.28; samtools 1.20", results=(_result(f"{index:064x}", float(index + 100)),),
        )
        for index in range(1, member_n + 1)
    )
    generation = CohortGeneration(cohort_id="healthy", version=7, kind=kind, members=members)
    reads = []
    monkeypatch.setattr(web_api, "read_current", lambda store, cohort_id: (reads.append(cohort_id), generation)[1])
    monkeypatch.setattr(web_api, "member_from_store", lambda store, source_id: members[0])
    envelope = ResultEnvelope(
        algorithm_id="fragment-size", algorithm_version="1", status="ok",
        provenance=provenance(
            tool="fixture", parameters_sha256=_PARAMETERS, source_id=_TARGET,
            input_id=f"{101:064x}", reference_id="synthetic",
        ),
        metrics=[
            Metric(metric_id="mode_bp", value=101.0),
            Metric(metric_id="category_code", value=1.0),
            Metric(metric_id="unlisted_scalar", value=3.0),
        ],
    )
    sample = SimpleNamespace(example=False, full=SimpleNamespace(
        results={"fragment-size": envelope}, depth_x=lambda: 1.0,
    ))
    monkeypatch.setattr(web_api.views, "load_sample", lambda store, source_id: sample)
    original = load_library().entry("fragment-size", "mode_bp")
    assert original is not None
    assert original["reference"]["kind"] == "published_mean"
    categorical = Entry("fragment-size", "category_code", {
        **original.raw, "metric_id": "category_code", "value_kind": "categorical",
    })
    library = Library(version="1.0.0", entries={
        ("fragment-size", "mode_bp"): original,
        ("fragment-size", "category_code"): categorical,
    }, licenses={})
    api = web_api.LabApi(LabStore(tmp_path))
    monkeypatch.setattr(api, "_resolve", lambda id12: (_TARGET, []))
    monkeypatch.setattr(api, "_id12s", lambda: {_TARGET: _TARGET[:12]})
    monkeypatch.setattr(api.libraries, "get", lambda: library)

    payload = api.compare(_TARGET[:12], "healthy")

    assert reads == ["healthy"]
    assert payload["cohort_version"] == "healthy:7"
    assert payload["member_n"] == member_n
    assert payload["metrics"][0]["eligible_n"] == eligible_n
    assert len(payload["metrics"][0]["cohort_values"]) == eligible_n
    assert payload["metrics"][0]["excluded"] == {"self": 1}
    assert payload["metrics"][0]["aligner_counts"] == {"minimap2 2.28; samtools 1.20": eligible_n}
    assert payload["metrics"][0]["state"] == state
    assert not any(row["metric_id"] == "category_code" for row in payload["metrics"])
    assert {"algorithm_id": "fragment-size", "status": "non_scalar", "reason": "category_code"} in payload["unscored"]
    unknown = next(row for row in payload["metrics"] if row["metric_id"] == "unlisted_scalar")
    assert unknown["state"] == "no_reference" and unknown["reason"] == "no_library_entry"
    assert unknown["eligible_n"] is None and unknown["comparison_context"] == "no_library_entry"
    assert payload["metrics"][0]["original_reference"]["kind"] == "published_mean"
    assert classify(101.0, original, depth_x=1.0).state == "no_reference"
    if eligible_n < 10:
        assert payload["metrics"][0]["percentile"] is None
        assert payload["metrics"][0]["interval"] is None
    elif eligible_n < 30:
        assert payload["metrics"][0]["exploratory_n"] == eligible_n
        assert payload["metrics"][0]["percentile"] is None
        assert "Not a percentile; no confidence interval" in payload["metrics"][0]["caption"]
        assert "Protocol-mismatched" in payload["metrics"][0]["caption"]
    else:
        assert payload["metrics"][0]["percentile"] is not None
        assert payload["metrics"][0]["interval"] is not None


def test_example_is_explicitly_unscorable(tmp_path, monkeypatch) -> None:
    generation = CohortGeneration(cohort_id="healthy", version=1, members=())
    monkeypatch.setattr(web_api, "read_current", lambda store, cohort_id: generation)
    monkeypatch.setattr(web_api.views, "load_sample", lambda store, source_id: SimpleNamespace(example=True))
    api = web_api.LabApi(LabStore(tmp_path))
    monkeypatch.setattr(api, "_resolve", lambda id12: (_TARGET, []))
    monkeypatch.setattr(api, "_id12s", lambda: {_TARGET: _TARGET[:12]})
    payload = api.compare(_TARGET[:12], "healthy")
    assert payload["state"] == "example_unscorable" and payload["metrics"] == []


def test_cohort_commands_are_registered_with_common_root_and_json() -> None:
    from traceback_lab.cli import build_parser

    parser = build_parser()
    add = parser.parse_args(["cohort", "add", "abcd1234abcd", "--cohort", "healthy", "--root", "/tmp/lab", "--json"])
    build = parser.parse_args(["cohort", "build", "--cohort", "healthy", "--root", "/tmp/lab", "--json"])
    assert add.cohort_action == "add" and add.as_json and str(add.root) == "/tmp/lab"
    assert build.cohort_action == "build" and build.as_json and str(build.root) == "/tmp/lab"


@pytest.mark.skipif(__import__("tests.lab.test_lab_server", fromlist=["_chrome"])._chrome() is None,
                    reason="no Chrome/Chromium for Compare DOM check")
def test_compare_and_joint_panel_render_in_native_tables(tmp_path, monkeypatch) -> None:
    import json

    from tests.lab.test_lab_server import _chrome, headless_dom
    from traceback_lab.web.server import RunningLabServer

    id12 = _TARGET[:12]
    algorithm = {
        "algorithm_id": "frag-x-meth", "status": "ok", "hidden": False,
        "metrics": [{
            "metric_id": "short_fragment_hypomethylation_delta", "label": "Short-fragment hypomethylation delta",
            "value": None, "unit": "fraction", "state": "no_reference", "reason": "none_published",
            "exploratory_n": None,
        }],
        "provenance": {
            "tool": "fixture", "tool_version": "1", "parameters_sha256": _PARAMETERS,
            "input_id": f"{101:064x}", "source_id": _TARGET,
            "reference_id": "synthetic", "timestamp_utc": "2026-10-16T00:00:00Z",
        },
    }
    sample = {
        "found": True, "id12": id12, "source_id": _TARGET, "algorithms": [algorithm],
        "catalog": [], "licenses": [], "cohort_n": 11, "cohort_version": "healthy:7",
        "interpretation_version": "1.0.0", "excluded_chunks": [],
    }
    monkeypatch.setattr(web_api.LabApi, "samples", lambda self: {"samples": [{
        "id12": id12, "run_utc": "", "depth_x": 1.0, "example": False,
    }], "root_has_runs": True})
    monkeypatch.setattr(web_api.LabApi, "sample", lambda self, value: sample)
    monkeypatch.setattr(web_api.LabApi, "cohorts", lambda self: {"cohorts": [{
        "cohort_id": "healthy", "cohort_version": "healthy:7", "kind": "internal_cohort",
        "member_n": 11, "pon_n": 0,
    }]})
    monkeypatch.setattr(web_api.LabApi, "compare", lambda self, value, cohort: {
        "found": True, "id12": id12, "cohort_id": "healthy", "cohort_version": "healthy:7",
        "kind": "internal_cohort", "member_n": 11, "pon_n": 0, "state": "ready",
        "target_aligner": "minimap2 2.28; samtools 1.20", "aligner_counts": {"minimap2 2.28; samtools 1.20": 11},
        "metrics": [{
            "algorithm_id": "frag-x-meth", "metric_id": "short_fragment_hypomethylation_delta",
            "label": "Short-fragment hypomethylation delta", "value": None, "unit": "fraction",
            "state": "no_reference", "reason": "cohort_too_small", "caption": "No reference yet (cohort n = 9 of 10).",
            "exploratory_n": None, "eligible_n": 9, "cohort_values": list(range(1, 10)),
            "excluded": {"self": 1, "missing_metric": 1},
            "aligner_counts": {"minimap2 2.28; samtools 1.20": 9}, "original_reference": {"kind": "published_mean", "source": "synthetic"},
        }], "unscored": [],
    })
    artifact = {
        "size_edges_bp": [1, 100, 151, 221, 301, 501, 1001], "size_final_upper_bp": None,
        "methylation_edges": [0, 0.25, 0.5, 0.75, 1], "counts": [[0, 0, 0, 0]] * 6 + [[0, 0, 0, 1]],
        "n": 1, "unit": "molecules", "min_distinct_cpg_sites": 4,
        "short_n": 0, "long_n": 1, "short_mean": None, "long_mean": 0.9,
    }
    monkeypatch.setattr(web_api.LabApi, "artifact", lambda self, *args: json.dumps(artifact).encode())

    with RunningLabServer(LabStore(tmp_path)) as server:
        def dom(route: str, profile: str, want: str) -> str:
            return headless_dom([
                _chrome(), "--headless=new", "--disable-gpu", "--no-first-run",
                f"--user-data-dir={tmp_path / profile}", "--virtual-time-budget=15000",
                "--window-size=360,740", "--dump-dom", server.issue_link() + f"&route=/sample/{id12}/{route}",
            ], ok=lambda page: 'data-ready="1"' in page and want in page, timeout=90)

        compare = dom("compare", "compare-profile", "Comparison against healthy:7")
        signals = dom("signals", "signals-profile", "Molecules by aligned size and 5mC fraction")
        motif_algorithm = {
            **algorithm, "algorithm_id": "breakpoint-motifs",
            "metrics": [{"metric_id": "cpg_near_end_fraction", "label": "CpG near-end fraction",
                         "value": 0.5, "unit": "fraction", "state": "no_reference",
                         "reason": "none_published", "exploratory_n": None}],
        }
        sample["algorithms"] = [motif_algorithm]
        motifs = ["".join((a, b, c, d)) for a in "ACGT" for b in "ACGT" for c in "ACGT" for d in "ACGT"]
        motif_artifact = {
            "motif": motifs, "near_count": [1] + [0] * 255, "far_count": [0] * 256,
            "near_ends": 1, "far_ends": 0, "invalid_context_near": 2,
            "invalid_context_far": 3,
            "methylation_link": {"status": "unavailable", "reason": "joint input absent"},
        }
        monkeypatch.setattr(web_api.LabApi, "artifact", lambda self, *args: json.dumps(motif_artifact).encode())
        motif_dom = dom("signals", "motifs-profile", "All 256 aligned-end motifs")

    assert "FLAG: CLIA" in compare
    assert "No reference yet (cohort n = 9 of 10)" in compare
    assert "missing metric: 1" in compare and "minimap2 2.28; samtools 1.20: 9" in compare
    assert "Eligible values (9)" in compare and "Original library reference" in compare
    assert 'data-csp-violations="0"' in compare
    assert "1001+ bp" in signals and "75–100% (inclusive)" in signals
    assert "Short group n = 0, mean = —" in signals
    assert "A missing mean is not zero" in signals
    assert 'data-csp-violations="0"' in signals
    assert "physical cleavage sites are not established" in motif_dom
    assert "Methylation link unavailable: joint input absent" in motif_dom
    assert "Invalid contexts: near 2, far 3" in motif_dom
    assert "AAAA" in motif_dom and "TTTT" in motif_dom
    assert 'data-csp-violations="0"' in motif_dom


def test_asset_commands_are_registered_with_common_root_and_json() -> None:
    from traceback_lab.cli import build_parser

    parser = build_parser()
    add = parser.parse_args([
        "asset", "add", "pmd", "--file", "/tmp/pmd.bed.gz",
        "--source-label", "synthetic fixture", "--terms", "unknown",
        "--root", "/tmp/lab", "--json",
    ])
    inspect = parser.parse_args(["asset", "inspect", "pmd", "--root", "/tmp/lab", "--json"])
    assert add.asset_action == "add" and add.as_json and str(add.root) == "/tmp/lab"
    assert inspect.asset_action == "inspect" and inspect.as_json and str(inspect.root) == "/tmp/lab"
