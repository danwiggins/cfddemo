"""S5 Compare uses one immutable cohort snapshot and post-LOO D8 gates."""

from __future__ import annotations

import json
import shlex
from types import SimpleNamespace

import pytest

from traceback_lab.cohort_contracts import CohortGeneration, CohortMember, CohortResult
from traceback_lab.cohorts import CohortListing
from traceback_lab.envelope import Metric, ResultEnvelope, provenance
from traceback_lab.interpretation.engine import Entry, Library, classify, load_library
from traceback_lab.problems import LabProblem
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
    monkeypatch.setattr(web_api, "member_for_generation", lambda store, generation, source_id: members[0])
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
        input_id=f"{101:064x}", results={"fragment-size": envelope}, depth_x=lambda: 1.0,
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


@pytest.mark.parametrize("target_source,target_input", [
    (_TARGET, f"{102:064x}"),
    (f"{2:064x}", f"{101:064x}"),
])
def test_compare_rejects_changed_full_input_or_source(
    tmp_path, monkeypatch, target_source, target_input,
) -> None:
    generation = CohortGeneration(cohort_id="healthy", version=1, members=())
    monkeypatch.setattr(web_api, "read_current", lambda store, cohort_id: generation)
    envelope = ResultEnvelope(
        algorithm_id="fragment-size", algorithm_version="1", status="ok",
        provenance=provenance(
            tool="fixture", parameters_sha256=_PARAMETERS, source_id=_TARGET,
            input_id=f"{101:064x}", reference_id="synthetic",
        ),
        metrics=[Metric(metric_id="mode_bp", value=101.0)],
    )
    sample = SimpleNamespace(example=False, full=SimpleNamespace(
        input_id=f"{101:064x}", results={"fragment-size": envelope},
    ))
    monkeypatch.setattr(web_api.views, "load_sample", lambda store, source_id: sample)
    target = CohortMember(
        source_id=target_source, input_id=target_input,
        aligner_identity="minimap2 2.28; samtools 1.20",
        results=(_result(target_source, 101.0),),
    )
    monkeypatch.setattr(web_api, "member_for_generation", lambda store, generation, source_id: target)
    api = web_api.LabApi(LabStore(tmp_path))
    monkeypatch.setattr(api, "_resolve", lambda id12: (_TARGET, []))
    monkeypatch.setattr(api, "_id12s", lambda: {_TARGET: _TARGET[:12]})

    payload = api.compare(_TARGET[:12], "healthy")

    assert payload["state"] == "stale_target"
    assert payload["metrics"] == [] and payload["unscored"] == []
    assert payload["cohort_version"] == "healthy:1"


def test_cohort_list_keeps_healthy_selectable_beside_damaged(tmp_path, monkeypatch) -> None:
    healthy = CohortGeneration(cohort_id="healthy", version=7, members=())
    damaged = LabProblem(
        "TBX-LAB-022", cause="stored cohort broken has invalid generation.json",
        fix="Restore the matching cohort file from a known-good backup before retrying",
    )
    monkeypatch.setattr(web_api, "list_cohorts", lambda store: (
        CohortListing("broken", None, damaged),
        CohortListing("healthy", healthy, None),
        CohortListing("pending", None, None),
    ))

    payload = web_api.LabApi(LabStore(tmp_path)).cohorts()

    assert [row["cohort_id"] for row in payload["cohorts"]] == ["healthy"]
    assert payload["cohorts"][0]["cohort_version"] == "healthy:7"
    assert payload["unavailable"] == [{
        "cohort_id": "broken", "problem": {
            "code": "TBX-LAB-022", "summary": damaged.summary,
            "cause": damaged.cause, "fix": damaged.fix,
            "docs": "docs/LAB-GUIDE.md#tbx-lab-022",
        },
    }]


def test_cohort_root_symlink_returns_actionable_api_problem(tmp_path) -> None:
    from tests.lab.test_lab_server import _get, _session
    from traceback_lab.web.server import RunningLabServer

    store = LabStore(tmp_path)
    store.lab.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (store.lab / "cohorts").symlink_to(outside, target_is_directory=True)

    with RunningLabServer(store) as server:
        response, body = _get(server, "/api/cohorts", _session(server))

    assert response.status == 200
    payload = json.loads(body)
    assert payload["cohorts"] == [] and payload["unavailable"] == []
    assert payload["problem"]["code"] == "TBX-LAB-022"
    assert payload["problem"]["fix"] and payload["problem"]["docs"]


def test_real_store_list_and_compare_fail_closed_after_pointer_damage(tmp_path, monkeypatch) -> None:
    store = LabStore(tmp_path)
    healthy = CohortGeneration(cohort_id="healthy", version=1, members=())
    generation_dir = store.lab / "cohorts" / "healthy" / "generations" / "1"
    generation_dir.mkdir(parents=True)
    (generation_dir / "generation.json").write_text(healthy.model_dump_json(by_alias=True))
    pointer = generation_dir.parent.parent / "current.json"
    pointer.write_text('{"version":1}')
    broken_dir = store.lab / "cohorts" / "broken"
    broken_dir.mkdir()
    (broken_dir / "current.json").write_text("{")
    api = web_api.LabApi(store)

    listing = api.cohorts()
    assert [row["cohort_id"] for row in listing["cohorts"]] == ["healthy"]
    assert listing["unavailable"][0]["cohort_id"] == "broken"
    assert listing["unavailable"][0]["problem"]["code"] == "TBX-LAB-022"

    monkeypatch.setattr(api, "_resolve", lambda id12: (_TARGET, []))
    monkeypatch.setattr(web_api.views, "load_sample", lambda store, source_id: SimpleNamespace(example=False))
    selected_broken = api.compare(_TARGET[:12], "broken")
    assert selected_broken["state"] == "cohort_unavailable" and selected_broken["metrics"] == []

    pointer.write_text("{")
    selected_damaged = api.compare(_TARGET[:12], "healthy")
    assert selected_damaged["state"] == "cohort_unavailable" and selected_damaged["metrics"] == []
    assert selected_damaged["problem"]["code"] == "TBX-LAB-022"
    assert api.cohorts()["cohorts"] == []


@pytest.mark.parametrize("cohort_failure", ["damaged", "unbuilt"])
def test_compare_cohort_unavailable_has_no_metrics(tmp_path, monkeypatch, cohort_failure) -> None:
    problem = LabProblem(
        "TBX-LAB-022", cause="stored cohort broken has invalid current.json",
        fix="Restore the matching cohort file from a known-good backup before retrying",
    )
    def current(store, cohort_id):
        if cohort_failure == "damaged":
            raise problem
        return None
    monkeypatch.setattr(web_api, "read_current", current)
    monkeypatch.setattr(web_api.views, "load_sample", lambda store, source_id: SimpleNamespace(example=False))
    root = tmp_path / "root with spaces"
    api = web_api.LabApi(LabStore(root))
    monkeypatch.setattr(api, "_resolve", lambda id12: (_TARGET, []))

    payload = api.compare(_TARGET[:12], "broken")

    assert payload["found"] is True and payload["state"] == "cohort_unavailable"
    assert payload["metrics"] == [] and payload["unscored"] == []
    assert payload["problem"]["code"] == ("TBX-LAB-022" if cohort_failure == "damaged" else "TBX-LAB-017")
    assert "fix" in payload["problem"] and "docs" in payload["problem"]
    if cohort_failure == "unbuilt":
        assert f"--root {shlex.quote(str(root))}" in payload["problem"]["fix"]
        assert "--root R" not in payload["problem"]["fix"]


def test_compare_target_unavailable_has_no_metrics(tmp_path, monkeypatch) -> None:
    generation = CohortGeneration(cohort_id="healthy", version=7, members=())
    monkeypatch.setattr(web_api, "read_current", lambda store, cohort_id: generation)
    monkeypatch.setattr(web_api.views, "load_sample", lambda store, source_id: SimpleNamespace(example=False))
    problem = LabProblem(
        "TBX-LAB-020", cause="stored source record is malformed",
        fix="Restore the source record matching its filename before cohort add/build",
    )
    def unavailable(store, generation, source_id):
        raise problem
    monkeypatch.setattr(web_api, "member_for_generation", unavailable)
    api = web_api.LabApi(LabStore(tmp_path))
    monkeypatch.setattr(api, "_resolve", lambda id12: (_TARGET, []))
    monkeypatch.setattr(api, "_id12s", lambda: {_TARGET: _TARGET[:12]})

    payload = api.compare(_TARGET[:12], "healthy")

    assert payload["found"] is True and payload["state"] == "target_unavailable"
    assert payload["cohort_version"] == "healthy:7"
    assert payload["metrics"] == [] and payload["unscored"] == []
    assert payload["problem"]["code"] == "TBX-LAB-020"


def test_compare_missing_full_input_has_prose_recovery(tmp_path, monkeypatch) -> None:
    store = LabStore(tmp_path)
    store.sources_dir.mkdir(parents=True)
    (store.sources_dir / f"{_TARGET}.json").write_text(json.dumps({
        "source_id": _TARGET, "example": False, "inputs": [],
    }))
    generation = CohortGeneration(cohort_id="healthy", version=7, members=())
    monkeypatch.setattr(web_api, "read_current", lambda store, cohort_id: generation)
    monkeypatch.setattr(web_api.views, "load_sample", lambda store, source_id: SimpleNamespace(example=False))
    api = web_api.LabApi(store)
    monkeypatch.setattr(api, "_resolve", lambda id12: (_TARGET, []))
    monkeypatch.setattr(api, "_id12s", lambda: {_TARGET: _TARGET[:12]})

    payload = api.compare(_TARGET[:12], "healthy")

    assert payload["state"] == "target_unavailable" and payload["metrics"] == []
    assert "original input" in payload["problem"]["fix"]
    assert "--root R" not in payload["problem"]["fix"] and "ID12" not in payload["problem"]["fix"]


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

    from tests.lab.test_lab_server import _chrome, _dom_cdp
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
            "aligner_counts": {"minimap2 2.28; samtools 1.20": 9},
            "tier": "NEWER", "entry": {"origin_tier": "NEWER", "origin_platform": "synthetic source",
                                      "citations": ["doi:10.1234/synthetic"], "citation_checked": {"by": "fixture"}},
            "original_reference": {"kind": "published_mean", "value": 167, "unit": "bp", "source": "synthetic"},
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
        def dom(route: str, profile: str, *want: str) -> str:
            profile_path = tmp_path / profile
            return _dom_cdp([
                _chrome(), "--headless=new", "--disable-gpu", "--no-first-run",
                f"--user-data-dir={profile_path}", "--window-size=360,740",
                server.issue_link() + f"&route=/sample/{id12}/{route}",
            ], profile_path, ok=lambda page: 'data-ready="1"' in page and all(text in page for text in want), timeout=90)

        compare = dom("compare", "compare-profile", "Comparison against healthy:7")
        problem = {
            "code": "TBX-LAB-022", "summary": "Cohort generation unavailable",
            "cause": "selected generation cannot be verified",
            "fix": "Inspect the cohort manifest, then rebuild the generation",
            "docs": "docs/LAB-GUIDE.md#tbx-lab-022",
        }
        monkeypatch.setattr(web_api.LabApi, "cohorts", lambda self: {"cohorts": [{
            "cohort_id": "healthy", "cohort_version": "healthy:7", "kind": "internal_cohort",
            "member_n": 11, "pon_n": 0,
        }], "unavailable": [{"cohort_id": "broken", "problem": problem}]})
        with_broken = dom("compare", "broken-profile", "Unavailable cohorts", "Comparison against healthy:7")
        monkeypatch.setattr(web_api.LabApi, "compare", lambda self, value, cohort: {
            "found": True, "state": "target_unavailable", "cohort_id": "healthy",
            "metrics": [], "problem": problem,
        })
        target_unavailable = dom("compare", "unavailable-profile", "This sample: Cohort generation unavailable")
        monkeypatch.setattr(web_api.LabApi, "cohorts", lambda self: {
            "cohorts": [], "unavailable": [{"cohort_id": "broken", "problem": problem}],
        })
        only_broken = dom("compare", "only-broken-profile", "No selectable cohort generation is available")
        monkeypatch.setattr(web_api.LabApi, "cohorts", lambda self: {
            "cohorts": [], "unavailable": [], "problem": problem,
        })
        root_unavailable = dom("compare", "root-unavailable-profile", "Cohort storage: Cohort generation unavailable")
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
    assert 'aria-label="Comparison metrics"' in compare and 'aria-label="Generation aligner counts"' in compare
    assert compare.count('tabindex="0" role="region"') >= 2
    assert "Published mean: 167 bp" in compare and "Original evidence tier:" in compare
    assert "https://doi.org/10.1234%2Fsynthetic" in compare
    assert 'data-csp-violations="0"' in compare
    assert "Comparison against healthy:7" in with_broken
    assert 'value="healthy"' in with_broken and 'value="broken"' not in with_broken
    assert "Cohort broken: Cohort generation unavailable" in with_broken
    assert "Next action: " in with_broken and "docs/LAB-GUIDE.md#tbx-lab-022" in with_broken
    assert "This sample: Cohort generation unavailable" in target_unavailable
    assert "Comparison against healthy:7" not in target_unavailable
    assert "No selectable cohort generation is available" in only_broken
    assert "Cohort broken: Cohort generation unavailable" in only_broken
    assert 'aria-label="Cohort generation"' not in only_broken
    assert 'data-csp-violations="0"' in with_broken and 'data-csp-violations="0"' in target_unavailable
    assert 'data-csp-violations="0"' in only_broken
    assert "No selectable cohort generation is available" in root_unavailable
    assert "Next action: " in root_unavailable and "docs/LAB-GUIDE.md#tbx-lab-022" in root_unavailable
    assert 'aria-label="Cohort generation"' not in root_unavailable
    assert 'data-csp-violations="0"' in root_unavailable
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
