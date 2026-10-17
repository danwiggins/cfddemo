"""D29 positive path from selected method results to the Agreement page."""

from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace

import numpy as np
import pysam
import pytest

from tests.lab.test_landing import _dom, needs_chrome
from traceback_lab.algorithms import copy_number, meth_tf
from traceback_lab.cohort_contracts import CohortGeneration, CohortMember, CohortResult
from traceback_lab.envelope import Depth, Metric, Panel, ResultEnvelope, provenance
from traceback_lab.registry import InputContext
from traceback_lab.store import LabStore
from traceback_lab.views import load_sample, selected_results
from traceback_lab.web import api as web_api
from traceback_lab.web.server import RunningLabServer
def _published_agreement(tmp_path, monkeypatch):
    root = tmp_path / "root"
    store = LabStore(root)
    source_id, input_id = "e" * 64, "f" * 64
    store.sources_dir.mkdir(parents=True)
    (store.sources_dir / f"{source_id}.json").write_text(json.dumps({
        "source_id": source_id, "example": False,
        "inputs": [{"input_id": input_id, "kind": "aligned", "used_utc": "2026-10-16T00:00:00Z"}],
    }))
    input_dir = store.inputs_dir / input_id
    input_dir.mkdir(parents=True)
    aligned_bam = input_dir / "aligned.bam"
    header = {
        "HD": {"VN": "1.6", "SO": "coordinate"},
        "SQ": [{"SN": "chr1", "LN": 100}],
        "RG": [{"ID": "ont-rg", "PL": "ONT", "DS": "basecall_model=synthetic modbase_models=model"}],
    }
    with pysam.AlignmentFile(str(aligned_bam), "wb", header=header) as bam:
        read = pysam.AlignedSegment(bam.header)
        read.query_name = "synthetic-ont"
        read.query_sequence = "ACGT"
        read.reference_id = 0
        read.reference_start = 1
        read.mapping_quality = 60
        read.cigartuples = [(0, 4)]
        read.query_qualities = pysam.qualitystring_to_array("5555")
        read.set_tag("RG", "ont-rg")
        bam.write(read)
    (input_dir / "manifest.json").write_text(json.dumps({
        "kind": "aligned", "platform": "ont", "source_id": source_id, "input_id": input_id,
        "reference_id": "synthetic", "reference_sha256": "9" * 64,
        "tools": {"samtools": "1.20", "minimap2": "2.28"},
        "mmi_sha256": "8" * 64, "aligner_argv": ["minimap2", "-ax", "map-ont"],
    }))
    id12 = web_api.LabApi(store)._id12s()[source_id]
    sample = load_sample(store, source_id)
    assert sample is not None and not sample.example
    assert sample.full.input_id == input_id
    ctx = InputContext(
        root=root, source_id=source_id, input_id=input_id,
        reference_id="synthetic", reference_sha256="9" * 64, input_dir=input_dir,
        extras={"counting_bam": str(aligned_bam), "counted_reads": 3_000_000,
                "upstream": {"read-summary": str(tmp_path / "summary")},
                "planned_asset_sha256s": ["c" * 64]},
    )

    parameters = replace(copy_number.ichor.default_parameters(), pon_policy="lab_loo_v1", pon_sha256="d" * 64)
    monkeypatch.setattr(copy_number.ichor, "default_parameters", lambda **kwargs: parameters)
    monkeypatch.setattr(copy_number, "_toolchain", lambda: SimpleNamespace(identity=SimpleNamespace(lock_sha256="a" * 64)))
    monkeypatch.setattr(copy_number.ichor, "resolve_assets", lambda root, toolchain: object())
    monkeypatch.setattr(copy_number.ichor, "measure", lambda *args, **kwargs: SimpleNamespace(
        tumor_fraction=0.25, ploidy=2.0, identifiable=True, bins=[], segments=[],
    ))
    cn = copy_number.COPY_NUMBER.run(ctx, tmp_path / "cn-out")
    assert next(metric["value"] for metric in cn.metrics if metric["metric_id"] == "pon_policy") == "lab_loo_v1"

    markers = [SimpleNamespace(chrom="chr1", pos0=1, healthy=0.0, tumour=1.0),
               SimpleNamespace(chrom="chr1", pos0=2, healthy=1.0, tumour=0.0)]
    reference = SimpleNamespace(
        sha256="c" * 64,
        reference=SimpleNamespace(reference_sha256=ctx.reference_sha256, modbase_model="model",
                                  call_definition=meth_tf.CALL_DEFINITION, markers=markers),
    )
    monkeypatch.setattr(meth_tf, "inspect_reference", lambda store: reference)
    from traceback_lab.algorithms import cell_origin

    monkeypatch.setattr(cell_origin, "header_model", lambda path: "model")
    monkeypatch.setattr(meth_tf, "ReadSummary", SimpleNamespace(
        open=lambda path: SimpleNamespace(contigs=["chr1"], lengths=[100]),
    ))
    monkeypatch.setattr(meth_tf, "load_calls", lambda ctx: (object(), None))
    monkeypatch.setattr(meth_tf, "tally_markers", lambda *args: (np.array([2, 8]), np.array([10, 10])))
    meth = meth_tf.METH_TF.run(ctx, tmp_path / "meth-out")
    assert next(metric["value"] for metric in meth.metrics if metric["metric_id"] == "tumor_fraction") == pytest.approx(0.2)

    def publish(algorithm_id, metrics, *, panels=(), artifacts=None):
        envelope = ResultEnvelope(
            algorithm_id=algorithm_id, algorithm_version="1", status="ok",
            provenance=provenance(tool="agreement-fixture", parameters_sha256={
                "copy-number": "1" * 64, "meth-tf": "2" * 64,
                "fragment-size": "3" * 64, "mtdna": "4" * 64,
            }[algorithm_id], source_id=source_id, input_id=input_id,
                reference_id=ctx.reference_id),
            depth=Depth(eligible_reads=3_000_000, approx_depth_x=1.0),
            metrics=list(metrics), panels=[Panel(**panel) for panel in panels],
        )
        store.publish_result(envelope, input_id=input_id, artifacts=artifacts)
        return envelope

    publish("copy-number", cn.metrics, panels=cn.panels, artifacts=cn.artifacts)
    publish("meth-tf", meth.metrics, artifacts=meth.artifacts)
    publish("fragment-size", [Metric(metric_id="mode_bp", value=170.0)])
    publish("mtdna", [Metric(metric_id="chrm_fraction", value=0.015)])

    selected = selected_results(store, input_id)
    assert all(selected[name].status == "ok" for name in ("copy-number", "meth-tf", "fragment-size", "mtdna"))
    compat = {"fragment-size": "a" * 64, "mtdna": "b" * 64}

    def result(name, value):
        return CohortResult(
            algorithm_id=name, parameters_sha256=selected[name].provenance.parameters_sha256,
            cohort_compat_key=compat[name], metrics={"mode_bp" if name == "fragment-size" else "chrm_fraction": value},
        )

    target = CohortMember(source_id=source_id, input_id=input_id, aligner_identity="fixture aligner",
                          results=(result("fragment-size", 170.0), result("mtdna", 0.015)))
    peers = tuple(CohortMember(
        source_id=f"{index:064x}", input_id=f"{index + 100:064x}", aligner_identity="fixture aligner",
        results=(result("fragment-size", 165.0), result("mtdna", 0.01)),
    ) for index in range(1, 11))
    incompatible = CohortMember(source_id=f"{99:064x}", input_id=f"{199:064x}", aligner_identity="fixture aligner",
                                results=(CohortResult(algorithm_id="fragment-size", parameters_sha256="5" * 64,
                                                      cohort_compat_key="f" * 64, metrics={"mode_bp": 999.0}),))
    generation = CohortGeneration(cohort_id="healthy-v1", version=1, members=(target, *peers, incompatible))
    monkeypatch.setattr(web_api, "read_current", lambda store, cohort_id: generation)
    # The fixture target copies parameter hashes from selected publications;
    # compatibility keys and cohort peers are supplied at the snapshot boundary.
    monkeypatch.setattr(web_api, "member_from_store", lambda store, source: target)
    return store, id12


def test_positive_agreement_uses_selected_results_and_source_excluded_baselines(tmp_path, monkeypatch):
    store, id12 = _published_agreement(tmp_path, monkeypatch)
    payload = web_api.LabApi(store).sample(id12)
    assert payload["cohort_state"] == "ready"
    report = payload["concordance"]
    assert "not independent confirmation" in report["caption"]
    assert "whole-genome doubling" in report["wgd_caveat"]
    assert "score" not in report
    pairs = {pair["id"]: pair for pair in report["pairs"]}
    assert set(pairs) == {"cn-meth", "cn-size", "cn-mtdna"}
    assert all(pair["state"] == "ok" and pair["y"] == pytest.approx(0.25) for pair in pairs.values())
    assert pairs["cn-meth"]["x"] == pytest.approx(0.2)
    assert pairs["cn-meth"]["reference_n"] is None
    assert pairs["cn-meth"]["baseline_median"] is None
    assert pairs["cn-meth"]["support"] == {
        "markers_covered": 2, "cpg_calls_used": 20,
        "reference_condition_number": pytest.approx(1.0), "fit_rms": pytest.approx(0.0),
    }
    assert (pairs["cn-size"]["x"], pairs["cn-size"]["baseline_median"], pairs["cn-size"]["reference_n"]) == (5.0, 165.0, 10)
    assert pairs["cn-mtdna"]["x"] == pytest.approx(0.005)
    assert pairs["cn-mtdna"]["baseline_median"] == pytest.approx(0.01)
    assert pairs["cn-mtdna"]["reference_n"] == 10


@needs_chrome
def test_positive_agreement_renders_three_plots_with_flags(tmp_path, monkeypatch):
    store, id12 = _published_agreement(tmp_path, monkeypatch)
    with RunningLabServer(store) as server:
        dom = _dom(server, tmp_path / "browser", route=f"/sample/{id12}/agreement",
                   want=lambda text: text.count("Data table") >= 3)
    assert dom.count("Data table") == 3
    assert dom.count("FLAG: FDA") >= 3 and "FLAG: CLIA" in dom
    assert "2 covered markers, 20 CpG calls" in dom
    assert "condition number 1" in dom and "Unnormalized weight-fit residual RMS 0" in dom
    assert "Matched cohort median 165 (n = 10)" in dom
    assert "not independent confirmation" in dom and "whole-genome doubling" in dom
    assert "aggregate score" not in dom.lower()
