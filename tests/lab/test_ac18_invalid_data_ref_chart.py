"""AC18: invalid stored panel references fail closed and show the chart failure."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pysam
import pytest

from tests.lab.test_lab_server import _get, _session
from tests.lab.test_landing import _dom, needs_chrome
from traceback_lab.envelope import Metric, ResultEnvelope, provenance
from traceback_lab.identity import parameters_sha256
from traceback_lab.store import LabStore
from traceback_lab.web.server import RunningLabServer


def _stored_sample(store: LabStore, *, measured: bool) -> tuple[str, str, Path | None]:
    source_id, input_id = f"{1:064x}", f"{101:064x}"
    store.sources_dir.mkdir(parents=True)
    (store.sources_dir / f"{source_id}.json").write_text(json.dumps({
        "source_id": source_id, "example": False,
        "inputs": [{"input_id": input_id, "kind": "aligned", "used_utc": "2026-10-16T00:00:00Z"}],
    }))
    input_dir = store.inputs_dir / input_id
    input_dir.mkdir(parents=True)
    (input_dir / "manifest.json").write_text(json.dumps({
        "kind": "aligned", "source_id": source_id, "input_id": input_id,
        "reference_sha256": f"{900:064x}",
        "tools": {"samtools": "1.20", "minimap2": "2.28"},
        "mmi_sha256": f"{901:064x}", "aligner_argv": ["minimap2", "-ax", "map-ont"],
    }))
    header = {"HD": {"VN": "1.6"}, "SQ": [{"SN": "chr1", "LN": 1000}],
              "RG": [{"ID": "one", "PL": "ONT"}]}
    with pysam.AlignmentFile(str(input_dir / "aligned.bam"), "wb", header=header):
        pass
    if not measured:
        return source_id, input_id, None
    identity = parameters_sha256(
        algorithm_id="fragment-size", algorithm_version="1", parameters={},
        tools=[], reference_sha256=f"{900:064x}",
    )
    envelope = ResultEnvelope(
        algorithm_id="fragment-size", algorithm_version="1", status="ok",
        provenance=provenance(tool="fixture", parameters_sha256=identity,
                              source_id=source_id, input_id=input_id, reference_id="synthetic"),
        metrics=[Metric(metric_id="mode_bp", value=167)],
        panels=[{"panel_id": "size-density", "kind": "density", "data_ref": "artifacts/size_hist.json"}],
    )
    counts = [0] * 1000
    counts[166] = 10
    store.publish_result(envelope, input_id=input_id, artifacts={
        "size_hist.json": json.dumps({"span_bp": list(range(1, 1001)), "count": counts}).encode(),
    })
    return source_id, input_id, store.selected_attempt(input_id, "fragment-size", identity)


def _publish_panel(store: LabStore, source_id: str, input_id: str, algorithm_id: str, panel_id: str) -> Path:
    identity = parameters_sha256(
        algorithm_id=algorithm_id, algorithm_version="1", parameters={},
        tools=[], reference_sha256=f"{900:064x}",
    )
    envelope = ResultEnvelope(
        algorithm_id=algorithm_id, algorithm_version="1", status="ok",
        provenance=provenance(tool="fixture", parameters_sha256=identity,
                              source_id=source_id, input_id=input_id, reference_id="synthetic"),
        panels=[{"panel_id": panel_id, "kind": "chart", "data_ref": "artifacts/panel.json"}],
    )
    store.publish_result(envelope, input_id=input_id, artifacts={"panel.json": b"{}"})
    attempt = store.selected_attempt(input_id, algorithm_id, identity)
    assert attempt is not None
    return attempt


@needs_chrome
@pytest.mark.parametrize("invalid", ("traversal", "absolute", "symlink", "never_measured"))
def test_invalid_panel_ref_is_chart_failed_but_never_measured_is_empty(tmp_path: Path, invalid: str) -> None:
    store = LabStore(tmp_path / "root")
    _source_id, _, attempt = _stored_sample(store, measured=invalid != "never_measured")
    secret = tmp_path / "donor-secret.json"
    secret.write_text('{"donor_secret":true}')
    if attempt is not None:
        result = attempt / "result.json"
        if invalid in ("traversal", "absolute"):
            data = json.loads(result.read_bytes())
            data["panels"][0]["data_ref"] = "../donor-secret.json" if invalid == "traversal" else str(secret)
            result.write_text(json.dumps(data))
        else:
            histogram = attempt / "artifacts" / "size_hist.json"
            histogram.unlink()
            histogram.symlink_to(secret)

    with RunningLabServer(store) as server:
        cookie = _session(server)
        samples, body = _get(server, "/api/samples", cookie)
        assert samples.status == 200
        (sample_listing,) = json.loads(body)["samples"]
        id12 = sample_listing["id12"]
        sample_response, sample_data = _get(server, f"/api/samples/{id12}", cookie)
        artifact_response, artifact_data = _get(
            server, f"/api/samples/{id12}/artifact/full/fragment-size/size-density", cookie,
        )
        assert sample_response.status == 200
        sample = json.loads(sample_data)
        assert artifact_response.status == 404 and b"donor_secret" not in artifact_data
        if invalid != "never_measured":
            assert all(value not in sample_data.decode() for value in (str(secret), "donor-secret", "donor_secret"))
        dom = _dom(server, tmp_path / "browser", route=f"/sample/{id12}/trust-qc",
                   want=lambda page: "Fragment-size density" in page)
    if invalid != "never_measured":
        assert str(secret) not in dom and "donor-secret" not in dom and "donor_secret" not in dom

    section = re.search(r'<section[^>]*aria-labelledby="size-density-title".*?</section>', dom, re.DOTALL)
    assert section is not None
    html = section.group(0)
    if invalid == "never_measured":
        assert "fragment-size" not in sample["unreadable_results"]
        assert "fragment-size has no result for this sample." in html
        assert "Chart could not be drawn" not in html
    else:
        assert "fragment-size" in sample["unreadable_results"]
        assert "Chart could not be drawn (selected result unavailable)." in html
        assert "fragment-size has no result for this sample." not in html


@needs_chrome
@pytest.mark.parametrize(
    ("algorithm_id", "panel_id", "route"),
    (("fragment-size", "size-density", "signals"),
     ("copy-number", "genome-track", "signals"),
     ("cell-origin", "composition", "body-map")),
)
def test_invalid_panel_ref_is_chart_failed_on_other_artifact_views(
    tmp_path: Path, algorithm_id: str, panel_id: str, route: str,
) -> None:
    store = LabStore(tmp_path / "root")
    source_id, input_id, attempt = _stored_sample(store, measured=algorithm_id == "fragment-size")
    if attempt is None:
        attempt = _publish_panel(store, source_id, input_id, algorithm_id, panel_id)
    secret = tmp_path / "donor-secret.json"
    secret.write_text('{"donor_secret":true}')
    panel = attempt / "artifacts" / ("size_hist.json" if algorithm_id == "fragment-size" else "panel.json")
    panel.unlink()
    panel.symlink_to(secret)

    with RunningLabServer(store) as server:
        cookie = _session(server)
        id12 = json.loads(_get(server, "/api/samples", cookie)[1])["samples"][0]["id12"]
        sample = json.loads(_get(server, f"/api/samples/{id12}", cookie)[1])
        artifact_response, artifact_data = _get(
            server, f"/api/samples/{id12}/artifact/full/{algorithm_id}/{panel_id}", cookie,
        )
        assert artifact_response.status == 404 and b"donor_secret" not in artifact_data
        assert all(value not in json.dumps(sample) for value in (str(secret), "donor-secret", "donor_secret"))
        assert algorithm_id in sample["unreadable_results"]
        dom = _dom(server, tmp_path / "browser", route=f"/sample/{id12}/{route}",
                   want=lambda page: "Chart could not be drawn (selected result unavailable)." in page)
        if algorithm_id == "cell-origin":
            findings = _dom(server, tmp_path / "findings-browser", route=f"/sample/{id12}/findings",
                            want=lambda page: "Cell-type composition" in page)
    assert "Chart could not be drawn (selected result unavailable)." in dom
    assert str(secret) not in dom and "donor-secret" not in dom and "donor_secret" not in dom
    if algorithm_id == "cell-origin":
        assert "Chart could not be drawn (selected result unavailable)." in findings
        assert str(secret) not in findings and "donor-secret" not in findings and "donor_secret" not in findings


@needs_chrome
@pytest.mark.parametrize(
    ("algorithm_id", "route"),
    (("copy-number", "signals"), ("fragment-size", "trust-qc")),
)
def test_skipped_chart_method_keeps_skip_state(tmp_path: Path, algorithm_id: str, route: str) -> None:
    store = LabStore(tmp_path / "root")
    source_id, input_id, _ = _stored_sample(store, measured=False)
    identity = parameters_sha256(
        algorithm_id=algorithm_id, algorithm_version="1", parameters={},
        tools=[], reference_sha256=f"{900:064x}",
    )
    store.publish_result(ResultEnvelope(
        algorithm_id=algorithm_id, algorithm_version="1", status="skipped",
        skip_reason="depth", skip_message="synthetic low depth",
        provenance=provenance(tool="fixture", parameters_sha256=identity,
                              source_id=source_id, input_id=input_id, reference_id="synthetic"),
    ), input_id=input_id)
    with RunningLabServer(store) as server:
        cookie = _session(server)
        id12 = json.loads(_get(server, "/api/samples", cookie)[1])["samples"][0]["id12"]
        sample = json.loads(_get(server, f"/api/samples/{id12}", cookie)[1])
        dom = _dom(server, tmp_path / "browser", route=f"/sample/{id12}/{route}",
                   want=lambda page: f"{algorithm_id}: skipped (depth)" in page)
    assert algorithm_id not in sample["unreadable_results"]
    assert f"{algorithm_id}: skipped (depth)" in dom
    assert "Chart could not be drawn (selected result unavailable)." not in dom
