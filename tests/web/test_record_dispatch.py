"""Signal SH5: record views dispatched by measurement type, common catalog columns.

The v4 schema here is a synthetic, test-only probe (``traceback.sh5-probe-*``)
standing in for the cell-origin and copy-number views that land later (CO5,
CN5).  Its body deliberately carries an estimate-shaped value so the tests can
show it never reaches the catalog table.  The probe binds a hash-keyed SH1
method-authority store, so a changed method definition can be exercised.
Every record is generated, unqualified, local and not for clinical use.
"""

from __future__ import annotations

import json
import re
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

import traceback_runner.measurement_schemas as schemas_module
from tests.test_bundle_v4 import (
    OtherProbeMeasurement,
    _main,
    _probe_measurement,
    _spec,
    _v4_bundle,
)
from tests.test_method_authority import _definition
from tests.test_result_catalog import _bundle_method
from tests.web.longitudinal_env import needs_node
from tests.web.test_loopback_server import _exchange, _request
from tests.web.test_records import (
    RID,
    _attr,
    _listing,
    _nodes,
    _ok,
    _run,
    _summary,
    _tag,
    _text,
    _view,
)
from tests.web.test_serve import _get, _serving
from traceback_runner import cli
from traceback_runner.bundles import MANIFEST_PATH, PROVENANCE_PATH
from traceback_runner.contracts import JobState
from traceback_runner.fixtures import create_local_golden_path_inputs
from traceback_runner.local_authority import ensure_method_authority
from traceback_runner.measurement_schemas import (
    MeasurementSchemaError,
    RecordViewBinding,
    register_measurement_schema,
)
from traceback_runner.problems import PROBLEM_TABLE
from traceback_runner.web.contracts import JobProjection
from traceback_runner.web.records import (
    ANALYSIS_BANNER,
    LocalRecordView,
    RecordSummary,
)
from traceback_runner.web.source import analysis_of_sample_token, job_problem
from traceback_runner.web.state_copy import (
    ANALYSIS_RECORD_AXES,
    ENUM_SOURCES,
    RECORD_AXES,
    STATE_COPY,
)

SLUG = "sh5-probe"
VIEW = "traceback.local-sh5-probe-view.v1"
ESTIMATE = 0.4321  # the probe's estimate-shaped value; never in the catalog
WEB_503 = {"error": {"code": "TBX-WEB-503"}}
T1 = datetime(2026, 10, 4, 12, 0, 0, tzinfo=UTC)
T2 = datetime(2026, 10, 4, 13, 0, 0, tzinfo=UTC)


class ProbeBody(BaseModel):
    estimate_fraction: float
    mixture: tuple[dict[str, Any], ...]


def _probe_body(measurement: Any) -> ProbeBody:
    return ProbeBody(
        estimate_fraction=measurement.values[0] / 10000,
        mixture=({"name": "probe contributor", "fraction": measurement.values[0] / 10000},),
    )


# The definition the probe's catalog binding creates a store for; a test swaps
# it to model a tool reinstall (a new definition hash, a new store).
_CURRENT: dict[str, Any] = {}


def _probe_authority(root: Path, registered: Any) -> Any:
    return ensure_method_authority(
        root, registered.reference_id, SLUG, _CURRENT["definition"], now=_CURRENT["now"]
    )


def _view_binding(**overrides: Any) -> RecordViewBinding:
    values: dict[str, Any] = {
        "analysis": "copy_number",
        "view_schema_version": VIEW,
        "key_count_unit": "values counted",
        "key_count": lambda measurement: measurement.reads_counted,
        "build_body": _probe_body,
    }
    values.update(overrides)
    return RecordViewBinding(**values)


def _sh5_spec(*, record_view: RecordViewBinding | None = None, **overrides: Any) -> Any:
    spec = _spec(**overrides)
    catalog = spec.catalog.__class__(**{**spec.catalog.__dict__, "authority": _probe_authority})
    from dataclasses import replace

    return replace(spec, catalog=catalog, record_view=record_view)


@pytest.fixture
def probe(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(schemas_module, "_REGISTRY", {})
    _CURRENT.update(definition=_definition("ref", tool=b"tool-a"), now=T1)
    return register_measurement_schema(_sh5_spec(record_view=_view_binding()))


@pytest.fixture(scope="module")
def fragment_root(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, str]:
    work = tmp_path_factory.mktemp("sh5-root")
    inputs = create_local_golden_path_inputs(work / "inputs")
    root = work / "root"
    assert _main("reference", "register", "--fasta", inputs.fasta_path, "--id", "ref",
                 "--root", root)[0] == 0
    code, payload = _main("run", inputs.bam_path, "--reference", "ref", "--root", root)
    assert code == 0, payload
    record_id = payload["data"]["record_id"]
    assert _main("catalog", "import", root / "records" / record_id, "--root", root)[0] == 0
    return root, record_id


@pytest.fixture
def world(fragment_root: tuple[Path, str], tmp_path: Path) -> tuple[Path, str]:
    source, record_id = fragment_root
    root = tmp_path / "root"
    shutil.copytree(source, root, symlinks=True)
    return root, record_id


def _publish(root: Path, fragment_record: str, name: str, *, values: list[int]) -> str:
    """Sign, publish and import a probe record made from the fragment's input."""

    authority = ensure_method_authority(
        root, "ref", SLUG, _CURRENT["definition"], now=_CURRENT["now"]
    )
    provenance = json.loads((root / "records" / fragment_record / PROVENANCE_PATH).read_bytes())
    staging = root / "staging-probe" / name
    _v4_bundle(
        staging,
        key=cli._local_signing_key(root),
        method=_bundle_method(authority.capability),
        measurement=_probe_measurement(reference_id="ref", values=values),
        provenance=provenance,
    )
    record_id = json.loads((staging / MANIFEST_PATH).read_bytes())["record_id"]
    shutil.move(str(staging), root / "records" / record_id)
    code, payload = _main("catalog", "import", root / "records" / record_id, "--root", root)
    assert code == cli.ExitCode.OK, payload
    return record_id


def _rows(listing: dict) -> dict[str, dict]:
    return {row["record_id"]: row for row in listing["records"]}


# --- registration ------------------------------------------------------------------


def test_record_view_registration_refuses_fragment_reserved_and_duplicate_views(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(schemas_module, "_REGISTRY", {})
    for bad in (
        _view_binding(analysis="fragment"),
        _view_binding(analysis="tissue"),
        _view_binding(view_schema_version="traceback.local-record-view.v1"),
        _view_binding(view_schema_version="local-probe-view"),
        _view_binding(key_count_unit="reads in /private/path"),
        _view_binding(build_body="not callable"),
    ):
        with pytest.raises(MeasurementSchemaError):
            register_measurement_schema(_sh5_spec(record_view=bad))
    register_measurement_schema(_sh5_spec(record_view=_view_binding()))
    with pytest.raises(MeasurementSchemaError, match="record view"):
        register_measurement_schema(
            _sh5_spec(
                record_view=_view_binding(),
                schema_version="traceback.sh3-other-measurement.v1",
                path_stem="sh3-other.v1",
                model=OtherProbeMeasurement,
                result_schema_id="schema_sh3_other_measurement",
            )
        )


# --- the catalog row never carries an estimate ----------------------------------------

_SUMMARY_FIELDS = (
    "record_id", "short_id", "status", "status_label", "label", "analysis",
    "analysis_label", "input_digest", "key_count", "key_count_unit",
    "method_version_state", "reference_id", "policy_label", "eligible_alignments",
    "records_scanned", "preflight", "preflight_label", "preflight_warnings",
    "warning_texts", "method_version", "imported_at",
)
_ESTIMATE_NAME = re.compile(r"fraction|mixture|tumou?r|estimate|ploidy|contributor|purity", re.I)


def test_record_summary_projects_no_estimate_field() -> None:
    """Fails if any estimate (mixture, tumour fraction, ploidy) enters a catalog row."""

    assert tuple(RecordSummary.model_fields) == _SUMMARY_FIELDS
    assert not [name for name in RecordSummary.model_fields if _ESTIMATE_NAME.search(name)]
    for name, field in RecordSummary.model_fields.items():
        # Only counts are numeric; no float slot exists to hold an estimate.
        assert "float" not in str(field.annotation), name


# --- served dispatch over a mixed ROOT -------------------------------------------------

_FRAGMENT_VIEW_KEYS = sorted(LocalRecordView.model_fields)


def test_fragment_view_is_unchanged_and_v4_dispatches_to_its_view(probe, world) -> None:
    root, fragment_record = world
    probe_record = _publish(root, fragment_record, "a", values=[4321])
    with _serving(root) as (service, _):
        cookie, _ = _exchange(service)
        status, fragment = _get(service, f"/api/v1/records/{fragment_record}", cookie)
        assert status == 200, fragment
        # Golden: the fragment view keeps traceback.local-record-view.v1 exactly.
        assert fragment["schema_version"] == "traceback.local-record-view.v1"
        assert sorted(fragment) == _FRAGMENT_VIEW_KEYS == [
            "eligible_alignments", "exclusions", "histogram", "imported_at", "label",
            "measurement_sha256", "method_version", "policy", "preflight", "record_id",
            "records_scanned", "reference_id", "result_id", "schema_version", "short_id",
            "states", "warnings",
        ]
        assert [row["axis"] for row in fragment["states"]] == list(RECORD_AXES)
        assert "banner" not in fragment

        status, view = _get(service, f"/api/v1/records/{probe_record}", cookie)
        assert status == 200, view
        assert view["schema_version"] == VIEW
        assert view["analysis"] == "copy_number" and view["analysis_label"] == "Copy number"
        assert view["banner"] == ANALYSIS_BANNER
        assert view["key_count"] == 7 and view["key_count_unit"] == "values counted"
        assert view["body"]["estimate_fraction"] == ESTIMATE
        assert [row["axis"] for row in view["states"]] == list(ANALYSIS_RECORD_AXES)
        states = {row["axis"]: row["token"] for row in view["states"]}
        assert states["method_version"] == "current_method_version"

        status, listing = _get(service, "/api/v1/records", cookie)
        assert status == 200
        rows = _rows(listing)
        assert rows[fragment_record]["analysis"] == "fragment"
        assert rows[fragment_record]["key_count"] == rows[fragment_record]["eligible_alignments"]
        assert rows[probe_record]["analysis"] == "copy_number"
        assert rows[probe_record]["key_count"] == 7
        # One sealed input: both records share the short input digest.
        assert rows[probe_record]["input_digest"] == rows[fragment_record]["input_digest"]
        assert re.fullmatch(r"[0-9a-f]{12}", rows[probe_record]["input_digest"])
        assert {item["token"] for item in listing["analyses"]} == set(ENUM_SOURCES["analysis"])
        # No estimate reaches the table, by name or by value.
        encoded = json.dumps(listing)
        assert str(ESTIMATE) not in encoded and "4321" not in encoded
        for row in listing["records"]:
            assert not [key for key in row if _ESTIMATE_NAME.search(key)]
            assert not _ESTIMATE_NAME.search(json.dumps(row))


def test_a_v4_schema_without_a_registered_view_is_unavailable(
    world, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, fragment_record = world
    monkeypatch.setattr(schemas_module, "_REGISTRY", {})
    _CURRENT.update(definition=_definition("ref", tool=b"tool-a"), now=T1)
    register_measurement_schema(_sh5_spec(record_view=None))
    probe_record = _publish(root, fragment_record, "noview", values=[5])
    with _serving(root) as (service, _):
        cookie, _ = _exchange(service)
        status, _, body = _request(
            service, "GET", f"/api/v1/records/{probe_record}", headers={"Cookie": cookie}
        )
        assert (status, json.loads(body)) == (503, WEB_503)
        assert _get(service, f"/api/v1/records/{fragment_record}", cookie)[0] == 200


def test_earlier_method_version_is_a_state_not_a_failure(probe, world) -> None:
    root, fragment_record = world
    old = _publish(root, fragment_record, "old", values=[11])
    # A tool reinstall: a new definition hash, a new append-only store.
    _CURRENT.update(definition=_definition("ref", tool=b"tool-b"), now=T2)
    new = _publish(root, fragment_record, "new", values=[12])
    stores = sorted((root / "method-authority" / "ref" / SLUG).iterdir())
    assert len(stores) == 2
    with _serving(root) as (service, _):
        cookie, _ = _exchange(service)
        status, view = _get(service, f"/api/v1/records/{old}", cookie)
        assert status == 200, view  # never a 503
        states = {row["axis"]: row for row in view["states"]}
        assert states["method_version"]["token"] == "earlier_method_version"
        assert states["method_version"]["label"] == "Made under an earlier method version"
        assert states["trust"]["token"] == "development_signature_verified"
        status, current = _get(service, f"/api/v1/records/{new}", cookie)
        assert status == 200
        assert {row["axis"]: row["token"] for row in current["states"]}["method_version"] == (
            "current_method_version"
        )
        rows = _rows(_get(service, "/api/v1/records", cookie)[1])
        assert rows[old]["method_version_state"] == "earlier_method_version"
        assert rows[new]["method_version_state"] == "current_method_version"
        assert rows[old]["status"] == rows[new]["status"] == "verified"


def test_tampered_earlier_store_hides_only_its_records(probe, world) -> None:
    root, fragment_record = world
    old = _publish(root, fragment_record, "old", values=[11])
    _CURRENT.update(definition=_definition("ref", tool=b"tool-b"), now=T2)
    new = _publish(root, fragment_record, "new", values=[12])
    with _serving(root) as (service, _):
        cookie, _ = _exchange(service)
        old_store = root / "method-authority" / "ref" / SLUG / _old_hash(root, new)
        pins = old_store / "pins.json"
        pins.chmod(0o600)
        pins.write_bytes(pins.read_bytes().replace(b'"', b"'", 1))
        status, _, body = _request(
            service, "GET", f"/api/v1/records/{old}", headers={"Cookie": cookie}
        )
        assert (status, json.loads(body)) == (503, WEB_503)  # tamper is a failure
        assert _get(service, f"/api/v1/records/{new}", cookie)[0] == 200
        assert _get(service, f"/api/v1/records/{fragment_record}", cookie)[0] == 200
        rows = _rows(_get(service, "/api/v1/records", cookie)[1])
        assert rows[old]["status"] == "failed_verification"
        assert rows[new]["status"] == "verified"


def _old_hash(root: Path, new_record: str) -> str:
    """The store directory that is not the one the newer record is bound to."""

    names = sorted(p.name for p in (root / "method-authority" / "ref" / SLUG).iterdir())
    manifest = json.loads((root / "records" / new_record / MANIFEST_PATH).read_bytes())
    current = manifest["method"]["method_definition_sha256"]
    (other,) = [name for name in names if name != current]
    return other


# --- jobs: analysis and coded problem per row ------------------------------------------


def test_job_analysis_comes_from_the_sample_token() -> None:
    assert analysis_of_sample_token("local-ref") == "fragment"
    assert analysis_of_sample_token("local-ref:cell-origin") == "cell_origin"
    assert analysis_of_sample_token("local-ref:copy-number") == "copy_number"
    assert analysis_of_sample_token("local-ref:my-policy") == "fragment"
    assert analysis_of_sample_token("synthetic.sample.v1") == "fragment"


def test_job_problem_shows_code_and_fixed_label_only() -> None:
    job = "job_" + "a" * 32
    problem = job_problem(job, JobState.TERMINAL_FAILURE, "TBX-RUN-005: /private/x.bam had none")
    assert problem is not None and problem.code == "TBX-RUN-005"
    assert problem.problem == STATE_COPY["job_problem"]["TBX-RUN-005"][0]
    assert "/private" not in problem.model_dump_json()
    two_part = job_problem(job, JobState.RETRYABLE_FAILURE, "TBX-AUTH-LOCAL-003: damaged")
    assert two_part is not None and two_part.code == "TBX-AUTH-LOCAL-003" and two_part.retryable
    unknown = job_problem(job, JobState.TERMINAL_FAILURE, "TBX-ZZZ-999: new")
    assert unknown is not None and unknown.problem == STATE_COPY["job_problem"]["other"][0]
    assert job_problem(job, JobState.TERMINAL_FAILURE, "OSError at /private/x") is None
    assert job_problem(job, JobState.COMPLETE, "TBX-RUN-005: x") is None
    projection = JobProjection(
        job_id=job, state=JobState.TERMINAL_FAILURE, analysis="cell_origin",
        stage_label="measure", updated_at=T1, revision=0, stale=False,
        headline="Job failed", owner="operator", next_action="Inspect this local job",
        problem=problem,
    )
    assert projection.model_dump(mode="json")["analysis"] == "cell_origin"


# Codes a job never stops on: CLI arguments, registration, labels, the catalog,
# serving and the browser session.
_NOT_JOB_CODES = re.compile(
    r"^TBX-(?:REF-002|REF-004|RUN-003|LABEL-|CAT-|SERVE-|AUTH-\d|WEB-|OUT-|INTERNAL$)"
)


def test_every_job_reachable_code_has_a_job_label() -> None:
    missing = [
        code for code in PROBLEM_TABLE
        if not _NOT_JOB_CODES.match(code) and code not in STATE_COPY["job_problem"]
    ]
    assert missing == []
    assert set(ENUM_SOURCES["job_problem"]) == set(STATE_COPY["job_problem"])
    measuring = STATE_COPY["job"]["measuring"][1]
    assert "fragment" not in measuring.lower()  # stage copy is analysis-neutral


# --- DOM harness ---------------------------------------------------------------------


def _analysis_view(record_id: str = RID[1], *, earlier: bool = False) -> dict:
    fragment = _view(record_id, warnings=False)
    states = [*fragment["states"]]
    token = "earlier_method_version" if earlier else "current_method_version"
    label, meaning = STATE_COPY["method_version"][token]
    states.append({"axis": "method_version", "token": token, "label": label, "meaning": meaning})
    return {
        "schema_version": VIEW,
        "analysis": "copy_number",
        "analysis_label": "Copy number",
        "banner": ANALYSIS_BANNER,
        "record_id": record_id,
        "short_id": record_id[7:19],
        "result_id": "result_" + "d" * 40,
        "label": None,
        "reference_id": "ref",
        "method_version": "1.0.0-local-ref",
        "measurement_sha256": "e" * 64,
        "input_digest": "c" * 12,
        "key_count": 7,
        "key_count_unit": "values counted",
        "preflight": fragment["preflight"],
        "warnings": 0,
        "states": states,
        "imported_at": "2026-10-04T02:50:00Z",
        "body": {"estimate_fraction": ESTIMATE, "mixture": [{"name": "x", "fraction": ESTIMATE}]},
    }


def _analysis_summary(record_id: str, *, analysis: str = "copy_number", minute: int = 5,
                      digest: str = "c" * 12, earlier: bool = False) -> dict:
    row = _summary(record_id, minute=minute)
    row.update(
        analysis=analysis,
        analysis_label=STATE_COPY["analysis"][analysis][0],
        input_digest=digest,
        key_count=7,
        key_count_unit="values counted",
        method_version_state="earlier_method_version" if earlier else "current_method_version",
        policy_label=None,
        eligible_alignments=None,
        records_scanned=None,
    )
    return row


@needs_node
def test_dom_catalog_columns_analysis_filter_and_input_groups(tmp_path: Path) -> None:
    rows = _listing(
        _summary(RID[0], minute=1),
        _analysis_summary(RID[1], minute=2, earlier=True),
        _analysis_summary(RID[2], analysis="cell_origin", minute=3, digest="f" * 12),
    )
    report = _run(tmp_path, {"responses": {"/api/v1/records": _ok(rows)}})[0]
    view = report["view"]
    heads = [_text(n) for n in _nodes(view, lambda n: n["tag"] == "th" and n["attrs"].get("scope") == "col")]
    assert heads == ["Compare", "Analysis", "Record (operator note)", "Reference",
                     "Method version", "Key n", "Preflight", "Imported"]
    assert [n["attrs"]["id"] for n in _nodes(view, _tag("select"))] == ["filter-analysis", "filter-method"]
    options = [_text(n) for n in _nodes(_nodes(view, _attr("id", "filter-analysis"))[0], _tag("option"))]
    assert options == ["All analyses", "Cell origin", "Copy number", "Fragment length"]
    groups = _nodes(view, _tag("tbody"))
    assert [g["attrs"]["data-input"] for g in groups] == ["c" * 12, "f" * 12]
    assert "Input cccccccccccc: 2 records" in _text(groups[0])
    text = _text(view)
    assert "7 values counted" in text and "100 eligible alignments" in text
    assert "made under an earlier method version" in text
    assert str(ESTIMATE) not in json.dumps(view)


@needs_node
def test_dom_compare_accepts_only_same_analysis_pairs(tmp_path: Path) -> None:
    rows = _listing(_summary(RID[0], minute=1), _analysis_summary(RID[1], minute=2),
                    _analysis_summary(RID[2], minute=3))
    reports = _run(tmp_path, {
        "responses": {"/api/v1/records": _ok(rows)},
        "steps": [{"check": RID[0]}, {"check": RID[1]}, {"check": RID[0], "value": False},
                  {"check": RID[2]}, {"hash": f"#/compare?a={RID[0]}&b={RID[1]}"}],
    })

    def button(report):
        return _nodes(report["view"], _attr("id", "compare"))[0]

    mixed, same = reports[2], reports[4]
    assert button(mixed).get("disabled") is True
    assert "Only records of the same analysis can be compared" in _text(
        _nodes(mixed["view"], _attr("id", "compare-reason"))[0]
    )
    assert not button(same).get("disabled")
    refused = reports[5]
    assert refused["dataset"] == {"view": "compare", "state": "error"}
    assert "different analyses and cannot be compared" in _text(refused["view"])


@needs_node
def test_dom_non_fragment_view_has_banner_and_no_unregistered_estimate(tmp_path: Path) -> None:
    reports = _run(tmp_path, {
        "responses": {"/api/v1/records": _ok(_listing(_analysis_summary(RID[1]))),
                      f"/api/v1/records/{RID[1]}": _ok(_analysis_view(earlier=True))},
        "steps": [{"hash": f"#/records/{RID[1]}"}],
    })
    record = reports[1]
    assert record["dataset"] == {"view": "record", "state": "success"}
    view = record["view"]
    banner = _nodes(view, _attr("id", "analysis-banner"))[0]
    assert _text(banner) == ANALYSIS_BANNER
    order = json.dumps(view)
    assert order.index("analysis-banner") < order.index("view-title") < order.index("analysis-line")
    assert _text(_nodes(view, _attr("id", "analysis-line"))[0]).startswith("Copy number; reference ref")
    assert "made under an earlier method version" in _text(_nodes(view, _attr("id", "record-status"))[0])
    assert "Based on 7 values counted." in _text(view)
    # No renderer is registered for the probe: no estimate is drawn.
    assert str(ESTIMATE) not in order
    assert "report.html is the record of truth" in _text(_nodes(view, _attr("id", "analysis-body"))[0])
    rows = _nodes(_nodes(view, _attr("id", "states"))[0], _attr("data-axis"))
    assert [row["attrs"]["data-axis"] for row in rows] == list(ANALYSIS_RECORD_AXES)
    assert not _nodes(view, _tag("pre"))


@needs_node
def test_dom_fragment_view_has_no_analysis_banner(tmp_path: Path) -> None:
    reports = _run(tmp_path, {
        "responses": {"/api/v1/records": _ok(_listing(_summary(RID[0]))),
                      f"/api/v1/records/{RID[0]}": _ok(_view())},
        "steps": [{"hash": f"#/records/{RID[0]}"}],
    })
    view = reports[1]["view"]
    assert not _nodes(view, _attr("id", "analysis-banner"))
    assert _nodes(view, _attr("class", "chart-svg"))


@needs_node
def test_dom_job_rows_show_analysis_and_problem_code(tmp_path: Path) -> None:
    problem = job_problem("job_" + "0" * 32, JobState.TERMINAL_FAILURE, "TBX-RUN-005: x")
    jobs = {"jobs": [
        {"job_id": "job_" + "0" * 32, "state": "terminal_failure", "analysis": "copy_number",
         "stage_label": "measure", "stale": False, "updated_at": "2026-10-04T02:00:00Z",
         "headline": "Job failed", "problem": problem.model_dump(mode="json")},
        {"job_id": "job_" + "1" * 32, "state": "complete", "analysis": "fragment",
         "stage_label": "sign", "stale": False, "updated_at": "2026-10-04T01:00:00Z",
         "headline": "Done", "problem": None},
    ]}
    report = _run(tmp_path, {"responses": {"/api/v1/records": _ok(_listing())}, "jobs": jobs})[0]
    first, second = report["jobs"]
    assert first.startswith("Copy number: Failed; stage measure")
    assert first.endswith("stopped on TBX-RUN-005: No eligible alignments")
    assert second.startswith("Fragment length: Finished") and "stopped on" not in second
