"""PoN bytes and parameter invariants before cohort binding is wired."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from traceback_lab.adapters import ichor
from traceback_lab.adapters.ichor import CopyNumberError, LabCopyNumberParameters, stage_verified_pon
from traceback_lab.algorithms import copy_number as cn_module
from traceback_lab import cohorts, pon, pon_consumer
from traceback_lab.cohorts import add_sample, build_cohort, read_current
from traceback_lab.envelope import ResultEnvelope, provenance
from traceback_lab.identity import cohort_compat_key
from traceback_lab.pon_consumer import bind_copy_number
from traceback_lab.registry import InputContext, PlanItem, execution_identity, load_registry, plan
from traceback_lab.runner import RunTarget, run_plan
from traceback_lab.scrub import Scrubber
from traceback_lab.store import LabStore, new_run_id, summarize_run
from traceback_lab.web.api import LabApi
from traceback_lab.problems import LabProblem
from traceback_lab.views import load_sample, selected_results
from traceback_runner.references import ReferenceProblem
from tests.lab.test_cohort_store import _sample


def test_sample_view_marks_old_copy_number_and_instability_as_superseded(tmp_path: Path) -> None:
    store = LabStore(tmp_path)
    source_id = _sample(store, 1)
    input_id = f"{101:064x}"
    derived_id = f"{301:064x}"
    source_path = store.sources_dir / f"{source_id}.json"
    record = json.loads(source_path.read_text())
    record["derived"] = [{
        "parent_input_id": input_id, "input_id": derived_id,
        "label": "split-A", "transform": "split", "parameters": {},
    }]
    source_path.write_text(json.dumps(record))
    for selected_input in (input_id, derived_id):
        for algorithm_id in ("copy-number", "instability", "arm-summary"):
            store.publish_result(ResultEnvelope(
                algorithm_id=algorithm_id, algorithm_version="1", status="ok",
                provenance=provenance(
                    tool="fixture", parameters_sha256={
                        "copy-number": "8", "instability": "9", "arm-summary": "a",
                    }[algorithm_id] * 64,
                    source_id=source_id, input_id=selected_input, reference_id="hg38",
                ),
            ), input_id=selected_input)
    view = load_sample(store, source_id)
    assert view is not None
    assert len(view.derived) == 1
    for visible_input, selected_input in ((view.full, input_id), (view.derived[0], derived_id)):
        for algorithm_id in ("copy-number", "instability", "arm-summary"):
            shown = visible_input.results[algorithm_id]
            assert shown.status == "skipped" and shown.skip_reason == "superseded_method"
            assert selected_results(store, selected_input)[algorithm_id].status == "ok"


@pytest.mark.parametrize("record_upstream", [False, True])
@pytest.mark.parametrize("consumed_version", ["1", "2"])
def test_sample_view_hides_instability_consuming_old_copy_number_identity(
    tmp_path: Path, record_upstream: bool, consumed_version: str,
) -> None:
    store = LabStore(tmp_path)
    source_id = _sample(store, 1)
    input_id = f"{101:064x}"
    old_identity, current_identity, instability_identity = "8" * 64, "9" * 64, "a" * 64

    def result(algorithm_id: str, version: str, identity: str) -> ResultEnvelope:
        return ResultEnvelope(
            algorithm_id=algorithm_id, algorithm_version=version, status="ok",
            provenance=provenance(
                tool="fixture", parameters_sha256=identity,
                source_id=source_id, input_id=input_id, reference_id="hg38",
            ),
        )

    old_attempt = store.publish_result(result("copy-number", consumed_version, old_identity), input_id=input_id)
    store.publish_result(
        result("instability", "1", instability_identity), input_id=input_id,
        upstream_selections=[{
            "algorithm_id": "copy-number", "input_id": input_id,
            "parameters_sha256": old_identity, "attempt_id": old_attempt,
        }] if record_upstream else None,
    )
    store.publish_result(
        result("arm-summary", "1", "b" * 64), input_id=input_id,
        upstream_selections=[{
            "algorithm_id": "copy-number", "input_id": input_id,
            "parameters_sha256": old_identity, "attempt_id": old_attempt,
        }] if record_upstream else None,
    )
    store.publish_result(result("copy-number", "2", current_identity), input_id=input_id)
    view = load_sample(store, source_id)
    assert view is not None
    assert view.full.results["copy-number"].status == "ok"
    assert view.full.results["copy-number"].algorithm_version == "2"
    assert view.full.results["instability"].skip_reason == "superseded_method"
    assert view.full.results["arm-summary"].skip_reason == "superseded_method"
    assert store.read_result(input_id, "copy-number", old_identity).status == "ok"
    assert store.read_result(input_id, "instability", instability_identity).status == "ok"


def test_sample_view_hides_instability_without_selected_copy_number(tmp_path: Path) -> None:
    store = LabStore(tmp_path)
    source_id = _sample(store, 1)
    input_id = f"{101:064x}"
    identity = "a" * 64
    store.publish_result(ResultEnvelope(
        algorithm_id="instability", algorithm_version="1", status="ok",
        provenance=provenance(
            tool="fixture", parameters_sha256=identity,
            source_id=source_id, input_id=input_id, reference_id="hg38",
        ),
    ), input_id=input_id)
    view = load_sample(store, source_id)
    assert view is not None
    assert view.full.results["instability"].skip_reason == "superseded_method"
    assert store.read_result(input_id, "instability", identity).status == "ok"


def test_sample_view_hides_instability_after_same_identity_copy_number_rerun(tmp_path: Path) -> None:
    store = LabStore(tmp_path)
    source_id = _sample(store, 1)
    input_id = f"{101:064x}"
    cn_identity, instability_identity = "8" * 64, "9" * 64

    def result(algorithm_id: str, identity: str) -> ResultEnvelope:
        return ResultEnvelope(
            algorithm_id=algorithm_id, algorithm_version="2", status="ok",
            provenance=provenance(
                tool="fixture", parameters_sha256=identity,
                source_id=source_id, input_id=input_id, reference_id="hg38",
            ),
        )

    consumed_attempt = store.publish_result(result("copy-number", cn_identity), input_id=input_id)
    store.publish_result(
        result("instability", instability_identity), input_id=input_id,
        upstream_selections=[{
            "algorithm_id": "copy-number", "input_id": input_id,
            "parameters_sha256": cn_identity, "attempt_id": consumed_attempt,
        }],
    )
    store.publish_result(result("copy-number", cn_identity), input_id=input_id)
    view = load_sample(store, source_id)
    assert view is not None
    assert view.full.results["copy-number"].status == "ok"
    assert view.full.results["instability"].skip_reason == "superseded_method"


def test_sample_view_preserves_upstream_skip_when_copy_number_asset_is_missing(tmp_path: Path) -> None:
    store = LabStore(tmp_path)
    source_id = _sample(store, 1)
    input_id = f"{101:064x}"
    for algorithm_id, reason, identity in (
        ("copy-number", "missing_asset", "8" * 64),
        ("instability", "upstream", "9" * 64),
    ):
        store.publish_result(ResultEnvelope(
            algorithm_id=algorithm_id, algorithm_version="2", status="skipped",
            skip_reason=reason, skip_message="method asset unavailable",
            provenance=provenance(
                tool="fixture", parameters_sha256=identity,
                source_id=source_id, input_id=input_id, reference_id="hg38",
            ),
        ), input_id=input_id)
    view = load_sample(store, source_id)
    assert view is not None
    assert view.full.results["copy-number"].skip_reason == "missing_asset"
    assert view.full.results["instability"].skip_reason == "upstream"


def test_projection_keeps_independent_metric_when_panel_assets_are_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = LabStore(tmp_path)
    monkeypatch.setattr(cohorts, "load_registry", lambda: (
        SimpleNamespace(id="fragment-size", version="1", parameters={}, tools=[], upstream=()),
    ))
    source_id = _sample(store, 1)
    other_source_id = _sample(store, 2)
    add_sample(store, "healthy", source_id)
    add_sample(store, "healthy", other_source_id)
    generation = build_cohort(store, "healthy").model_copy(update={"pon_asset_sha256": "a" * 64})
    assert any(result.algorithm_id == "fragment-size" for result in generation.members[0].results)
    assert any(result.algorithm_id == "fragment-size" for result in cohorts.member_from_store(store, source_id).results)
    monkeypatch.setattr(cohorts, "read_current", lambda *_: generation)
    unavailable = LabProblem(
        "TBX-LAB-020", cause="registered assets are unavailable",
        fix="Restore method assets", data={"field": "method assets"},
    )
    monkeypatch.setattr(
        pon_consumer, "selection_for_generation", lambda *_, **__: (_ for _ in ()).throw(unavailable),
    )
    independent = cohorts.project_sample_metric(
        store, "healthy", target_source_id=source_id,
        algorithm_id="fragment-size", metric_id="mode_bp",
    )
    assert independent is not None
    assert cohorts.project_sample_metric(
        store, "healthy", target_source_id=source_id,
        algorithm_id="copy-number", metric_id="tumor_fraction",
    ) is None


def test_pon_contract_requires_policy_and_digest_together() -> None:
    digest = "a" * 64
    assert LabCopyNumberParameters(signed=object()).pon_policy == "none"
    assert LabCopyNumberParameters(signed=object(), pon_policy="lab_loo_v1", pon_sha256=digest).pon_sha256 == digest
    with pytest.raises(ValueError, match="no-PoN"):
        LabCopyNumberParameters(signed=object(), pon_sha256=digest)
    for bad in (None, "A" * 64, "short"):
        with pytest.raises(ValueError, match="requires a lowercase"):
            LabCopyNumberParameters(signed=object(), pon_policy="lab_loo_v1", pon_sha256=bad)


def test_pon_staging_verifies_exact_bytes(tmp_path: Path) -> None:
    source = tmp_path / "selected.rds"
    source.write_bytes(b"synthetic-pon-v1")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    destination = tmp_path / "scratch" / "pon.rds"
    destination.parent.mkdir()
    stage_verified_pon(source, destination, digest)
    assert destination.read_bytes() == source.read_bytes()

    source.write_bytes(b"mutated-after-selection")
    with pytest.raises(CopyNumberError, match="digest verification"):
        stage_verified_pon(source, destination, digest)


def test_pon_staging_refuses_missing_and_linked_source(tmp_path: Path) -> None:
    destination = tmp_path / "staged.rds"
    with pytest.raises(CopyNumberError, match="not a regular file"):
        stage_verified_pon(tmp_path / "absent.rds", destination, "a" * 64)
    source = tmp_path / "source.rds"
    source.write_bytes(b"synthetic")
    link = tmp_path / "link.rds"
    link.symlink_to(source)
    with pytest.raises(CopyNumberError, match="not a regular file"):
        stage_verified_pon(link, destination, hashlib.sha256(source.read_bytes()).hexdigest())


def test_copy_number_configured_boundary_passes_one_verified_panel(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    digest = "a" * 64
    default = cn_module.ichor.default_parameters()
    parameters = replace(default, pon_policy="lab_loo_v1", pon_sha256=digest)
    panel = tmp_path / "selected.rds"
    panel.write_bytes(b"synthetic-panel")
    seen: dict[str, object] = {}

    def measure(counting, counted, scanned, work, *, toolchain, assets, parameters, pon, validate_grid):
        seen.update(counting=counting, parameters=parameters.contract(), pon=pon)
        validate_grid("b" * 64)
        return SimpleNamespace(
            bins=[], segments=[{"call": "NEUT", "n_bins": 1, "median_log2": 0.0}],
            tumor_fraction=0.0, ploidy=2.0, identifiable=True,
        )

    monkeypatch.setattr(cn_module, "_toolchain", lambda: object())
    monkeypatch.setattr(cn_module.ichor, "resolve_assets", lambda *_: object())
    monkeypatch.setattr(cn_module.ichor, "measure", measure)
    ctx = InputContext(
        root=tmp_path, source_id="a" * 64, input_id="b" * 64,
        reference_id="hg38", reference_sha256="c" * 64,
        input_dir=tmp_path, eligible_reads=2_000_000,
        extras={"counting_bam": str(tmp_path / "counting.bam"), "counted_reads": 2_000_000},
    )
    out = tmp_path / "attempt" / "artifacts"
    out.mkdir(parents=True)
    output = cn_module.COPY_NUMBER.run_configured(
        ctx, out, parameters=parameters, pon=panel,
        validate_grid=lambda actual: seen.update(grid=actual),
    )
    assert seen == {
        "counting": tmp_path / "counting.bam", "parameters": parameters.contract(),
        "pon": panel, "grid": "b" * 64,
    }
    assert next(metric for metric in output.metrics if metric["metric_id"] == "pon_policy")["value"] == "lab_loo_v1"
    with pytest.raises(CopyNumberError, match="requires grid verification"):
        cn_module.COPY_NUMBER.run_configured(ctx, out, parameters=parameters, pon=panel)


def test_grid_mismatch_stops_before_ichor_receives_panel(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    parameters = replace(ichor.default_parameters(), pon_policy="lab_loo_v1", pon_sha256="a" * 64)
    called: list[str] = []
    monkeypatch.setattr(ichor, "read_counter", lambda *args: called.append("read-counter"))
    monkeypatch.setattr(ichor, "analysis_grid", lambda *args: (
        SimpleNamespace(bin_definition_sha256="b" * 64), "c" * 64,
    ))
    monkeypatch.setattr(ichor, "run_ichor", lambda *args, **kwargs: called.append("ichor"))

    def reject_grid(actual: str) -> None:
        assert actual == "b" * 64
        raise CopyNumberError("selected PoN grid mismatch")

    with pytest.raises(CopyNumberError, match="grid mismatch"):
        ichor.measure(
            tmp_path / "counting.bam", 2_000_000, 2_000_000, tmp_path,
            toolchain=object(), assets=object(), parameters=parameters,
            pon=tmp_path / "selected.rds", validate_grid=reject_grid,
        )
    assert called == ["read-counter"]


def test_bound_panel_changes_execution_but_not_cohort_compatibility() -> None:
    items = plan(load_registry(), only=["instability"])
    first = bind_copy_number(items, SimpleNamespace(pon_policy="lab_loo_v1", pon_sha256="a" * 64))
    second = bind_copy_number(items, SimpleNamespace(pon_policy="lab_loo_v1", pon_sha256="b" * 64))
    copy_a = next(item.algorithm for item in first if item.algorithm.id == "copy-number")
    copy_b = next(item.algorithm for item in second if item.algorithm.id == "copy-number")
    assert copy_a.parameters["pon_sha256"] == "a" * 64
    assert copy_b.parameters["pon_sha256"] == "b" * 64
    cn_a = execution_identity(copy_a, reference_sha256="c" * 64)
    cn_b = execution_identity(copy_b, reference_sha256="c" * 64)
    assert cn_a != cn_b
    compat = dict(
        algorithm_id="copy-number", algorithm_version=copy_a.version,
        tools=[tool.identity() for tool in copy_a.tools], reference_sha256="c" * 64,
        aligner_identity={"kind": "aligned"}, pon_policy="lab_loo_v1",
        per_sample_asset_keys=("pon_sha256",),
    )
    assert cohort_compat_key(parameters=copy_a.parameters, **compat) == cohort_compat_key(
        parameters=copy_b.parameters, **compat
    )
    assert cohort_compat_key(parameters=copy_a.parameters, **compat) != cohort_compat_key(
        parameters=items[1].algorithm.parameters, **{**compat, "pon_policy": "none"}
    )


def test_instability_compatibility_distinguishes_pon_protocol(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = LabStore(tmp_path)
    source_id = _sample(store, 1)
    input_id = f"{101:064x}"
    manifest = json.loads((store.inputs_dir / input_id / "manifest.json").read_bytes())
    copy = replace(cn_module.COPY_NUMBER, upstream=(), tools=())
    instability = SimpleNamespace(id="instability", version="1", parameters={"measure": "tmad"},
                                  tools=(), upstream=("copy-number",))
    monkeypatch.setattr(type(copy), "asset_sha256s", lambda self, root: [])
    monkeypatch.setattr(cohorts, "load_registry", lambda: (copy, instability))

    def publish_chain(producer):
        cn_identity = execution_identity(producer, reference_sha256=manifest["reference_sha256"])
        def envelope(algorithm, identity):
            return ResultEnvelope(
                algorithm_id=algorithm.id, algorithm_version=algorithm.version, status="ok",
                provenance=provenance(tool="fixture", parameters_sha256=identity,
                                      source_id=source_id, input_id=input_id, reference_id="synthetic"),
            )
        attempt = store.publish_result(envelope(producer, cn_identity), input_id=input_id)
        upstream = [{"algorithm_id": copy.id, "parameters_sha256": cn_identity}]
        identity = execution_identity(instability, reference_sha256=manifest["reference_sha256"], upstream=upstream)
        store.publish_result(envelope(instability, identity), input_id=input_id, upstream_selections=[{
            "algorithm_id": copy.id, "parameters_sha256": cn_identity,
            "input_id": input_id, "attempt_id": attempt,
        }])

    publish_chain(copy)
    baseline = cohorts._selected_results(store, input_id, manifest)
    selection = SimpleNamespace(pon_policy="lab_loo_v1", pon_sha256="e" * 64)
    publish_chain(pon_consumer.bind_copy_number_algorithm(copy, selection))
    selected = cohorts._selected_results(store, input_id, manifest, pon_selection=selection)
    assert [row.algorithm_id for row in baseline] == ["copy-number", "instability"]
    assert [row.algorithm_id for row in selected] == ["copy-number", "instability"]
    assert baseline[1].cohort_compat_key != selected[1].cohort_compat_key


def test_projection_target_uses_pinned_generation_selection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = LabStore(tmp_path)
    generation = SimpleNamespace(pon_asset_sha256="a" * 64)
    source_id = "b" * 64
    manifest = {"reference_sha256": "c" * 64, "kind": "aligned",
                "tools": {"minimap2": "2.28", "samtools": "1.20"},
                "mmi_sha256": "d" * 64, "aligner_argv": ["minimap2", "-ax", "map-ont"]}
    monkeypatch.setattr(cohorts, "_checked_source_record", lambda *_: {})
    monkeypatch.setattr(cohorts, "_full_input", lambda *_: ("e" * 64, manifest))
    selected = object()
    calls = []
    monkeypatch.setattr(pon_consumer, "selection_for_generation", lambda *args, **kwargs: (
        calls.append((args, kwargs)) or selected
    ))
    monkeypatch.setattr(cohorts, "member_from_store", lambda _store, _source, *, pon_selection=None: pon_selection)
    assert cohorts.member_for_generation(store, generation, source_id) is selected
    assert calls[0][0] == (store, generation)
    assert calls[0][1]["source_id"] == source_id
    assert calls[0][1]["reference_sha256"] == "c" * 64


def test_default_without_built_panel_stays_none_but_explicit_selection_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = LabStore(tmp_path)
    monkeypatch.setattr(cohorts, "read_current", lambda *_: None)
    common = dict(store=store, source_id="a" * 64, reference_sha256="b" * 64,
                  aligner_identity="minimap2 2.28; samtools 1.20")
    assert pon_consumer.selection_for_target(cohort_id=None, **common) is None
    with pytest.raises(LabProblem, match="TBX-LAB-020"):
        pon_consumer.selection_for_target(cohort_id="healthy-v1", **common)


def test_explicit_cohort_is_checked_even_when_copy_number_is_not_planned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cohorts, "read_current", lambda *_: None)
    target = RunTarget(
        input_id="a" * 64, source_id="b" * 64, input_dir=tmp_path,
        reference_id="hg38", reference_sha256="c" * 64,
    )
    manifest = {
        "kind": "aligned", "tools": {"minimap2": "2.28", "samtools": "1.20"},
        "mmi_sha256": "d" * 64, "aligner_argv": ["minimap2", "-ax", "map-ont"],
    }
    items = plan(load_registry(), only=["bam-qc"])
    assert pon_consumer.bind_target(LabStore(tmp_path), items, target, manifest, cohort_id=None) == list(items)
    with pytest.raises(LabProblem, match="TBX-LAB-020"):
        pon_consumer.bind_target(LabStore(tmp_path), items, target, manifest, cohort_id="healthy-v1")


def test_binding_pins_source_and_all_protocol_hashes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    generation = SimpleNamespace(cohort_id="healthy-v1", pon_asset_sha256="a" * 64, kind="internal_cohort")
    monkeypatch.setattr(cohorts, "read_current", lambda *_: generation)
    toolchain = SimpleNamespace(identity=SimpleNamespace(lock_sha256="b" * 64))
    monkeypatch.setattr(cn_module, "toolchain_or_none", lambda: toolchain)
    monkeypatch.setattr(pon_consumer.ichor, "resolve_assets", lambda *_: object())
    monkeypatch.setattr(pon_consumer.ichor, "asset_sha256s", lambda *_: {
        "gc_wig": "c" * 64, "map_wig": "d" * 64, "centromere": "e" * 64,
    })
    calls: list[tuple[object, object, str, dict[str, object]]] = []
    selected = object()

    def fake_binding(store, generation, source_id, **kwargs):
        calls.append((store, generation, source_id, kwargs))
        return selected

    monkeypatch.setattr(pon, "binding_for", fake_binding)
    store = LabStore(tmp_path)
    result = pon_consumer.selection_for_target(
        store, cohort_id="healthy-v1", source_id="f" * 64,
        reference_sha256="1" * 64, aligner_identity="minimap2 2.28; samtools 1.20",
    )
    assert result is selected
    assert calls[0][:3] == (store, generation, "f" * 64)
    assert calls[0][3]["toolchain_lock_sha256"] == "b" * 64
    assert [calls[0][3][name] for name in ("gc_sha256", "map_sha256", "centromere_sha256")] == [
        "c" * 64, "d" * 64, "e" * 64,
    ]


def test_target_binding_carries_same_selection_into_derived_runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    selection = pon.PonSelection(
        "healthy-v1", "healthy-v1:1", "lab_loo_v1", "a" * 64,
        tmp_path / "lab" / "assets" / "pon" / f"{'a' * 64}.rds", 10,
        "b" * 64, "c" * 64, "d" * 64, "e" * 64, "f" * 64,
        "1" * 64, "2" * 64, "3" * 64, (), True,
    )
    monkeypatch.setattr(pon_consumer, "selection_for_target", lambda *_, **__: selection)
    target = RunTarget(
        input_id="4" * 64, source_id="5" * 64, input_dir=tmp_path,
        reference_id="hg38", reference_sha256="b" * 64,
    )
    manifest = {
        "kind": "aligned", "tools": {"minimap2": "2.28", "samtools": "1.20"},
        "mmi_sha256": "6" * 64, "aligner_argv": ["minimap2", "-ax", "map-ont"],
    }
    bound = pon_consumer.bind_target(
        LabStore(tmp_path), plan(load_registry(), only=["instability"]), target, manifest,
        cohort_id="healthy-v1",
    )
    copy = next(item.algorithm for item in bound if item.algorithm.id == "copy-number")
    assert copy.parameters["pon_sha256"] == selection.pon_sha256
    assert target.extras["pon_selection"]["cohort_version"] == "healthy-v1:1"
    restored = pon_consumer.selection_from_payload(target.extras["pon_selection"], tmp_path, "b" * 64)
    assert restored == selection
    with pytest.raises(CopyNumberError, match="path or reference"):
        pon_consumer.selection_from_payload(target.extras["pon_selection"], tmp_path, "0" * 64)


def test_bound_copy_number_run_consumes_selected_panel_bytes_and_grid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = LabStore(tmp_path)
    panel_bytes = b"verified synthetic panel"
    digest = hashlib.sha256(panel_bytes).hexdigest()
    panel = store.lab / "assets" / "pon" / f"{digest}.rds"
    panel.parent.mkdir(parents=True)
    panel.write_bytes(panel_bytes)
    grid_sha256 = "d" * 64
    selection = pon.PonSelection(
        "healthy-v1", "healthy-v1:1", "lab_loo_v1", digest, panel, 10,
        "b" * 64, "c" * 64, grid_sha256, "e" * 64, "f" * 64,
        "1" * 64, "2" * 64, "3" * 64, (), False,
    )
    monkeypatch.setattr(pon_consumer, "selection_for_target", lambda *_, **__: selection)
    target = RunTarget(
        input_id="4" * 64, source_id="5" * 64, input_dir=tmp_path,
        reference_id="hg38", reference_sha256="b" * 64,
        extras={"counting_bam": str(tmp_path / "counting.bam"), "counted_reads": 2_000_000},
    )
    manifest = {
        "kind": "aligned", "tools": {"minimap2": "2.28", "samtools": "1.20"},
        "mmi_sha256": "6" * 64, "aligner_argv": ["minimap2", "-ax", "map-ont"],
    }
    items = pon_consumer.bind_target(
        store, plan(load_registry(), only=["copy-number"]), target, manifest,
        cohort_id="healthy-v1",
    )
    bound = next(item.algorithm for item in items if item.algorithm.id == "copy-number")
    assert bound.parameters["pon_sha256"] == digest
    seen: dict[str, object] = {}
    actual_grid = grid_sha256

    def measure(counting, counted, scanned, work, *, toolchain, assets, parameters, pon, validate_grid):
        validate_grid(actual_grid)
        stage_verified_pon(pon, work / "verified-panel.rds", parameters.pon_sha256)
        seen.update(panel=pon, contract=parameters.contract(), counted=counted)
        return SimpleNamespace(
            bins=[], segments=[{"call": "NEUT", "n_bins": 1, "median_log2": 0.0}],
            tumor_fraction=0.0, ploidy=2.0, identifiable=True,
        )

    monkeypatch.setattr(cn_module, "_toolchain", lambda: object())
    monkeypatch.setattr(cn_module.ichor, "resolve_assets", lambda *_: object())
    monkeypatch.setattr(cn_module.ichor, "measure", measure)
    out = tmp_path / "attempt" / "artifacts"
    out.mkdir(parents=True)
    output = bound.run(target.context(store.root), out)
    assert seen == {"panel": panel, "contract": bound.parameters, "counted": 2_000_000}
    assert next(metric for metric in output.metrics if metric["metric_id"] == "pon_policy")["value"] == "lab_loo_v1"

    actual_grid = "0" * 64
    with pytest.raises(LabProblem) as mismatch:
        bound.run(target.context(store.root), out)
    assert "grid" in mismatch.value.cause
    actual_grid = grid_sha256
    panel.write_bytes(b"changed panel bytes")
    with pytest.raises(CopyNumberError, match="digest verification"):
        bound.run(target.context(store.root), out)


@pytest.mark.parametrize("platform,example", [("illumina", False), ("ont", True)])
def test_implicit_ont_panel_does_not_block_other_input_kinds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, platform: str, example: bool,
) -> None:
    monkeypatch.setattr(
        pon_consumer, "selection_for_target",
        lambda *_, **__: pytest.fail("implicit ONT panel must not be consulted"),
    )
    target = RunTarget(
        input_id="4" * 64, source_id="5" * 64, input_dir=tmp_path,
        reference_id="hg38", reference_sha256="b" * 64,
        platform=platform, example=example,
    )
    items = plan(load_registry(), only=["instability"])
    assert pon_consumer.bind_target(
        LabStore(tmp_path), items, target, {}, cohort_id=None,
    ) == list(items)
    assert "pon_selection" not in target.extras


@pytest.mark.parametrize("platform,example,code,field", [
    ("illumina", False, "TBX-LAB-024", "platform"),
    ("ont", True, "TBX-LAB-021", "EXAMPLE"),
])
def test_explicit_ont_panel_refuses_other_input_kinds_before_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, platform: str, example: bool,
    code: str, field: str,
) -> None:
    monkeypatch.setattr(
        pon_consumer, "selection_for_target",
        lambda *_, **__: pytest.fail("incompatible input must be refused first"),
    )
    target = RunTarget(
        input_id="4" * 64, source_id="5" * 64, input_dir=tmp_path,
        reference_id="hg38", reference_sha256="b" * 64,
        platform=platform, example=example,
    )
    with pytest.raises(LabProblem) as error:
        pon_consumer.bind_target(
            LabStore(tmp_path), plan(load_registry(), only=["instability"]),
            target, {}, cohort_id="healthy-v1",
        )
    assert error.value.code == code and error.value.data["field"] == field
    assert "pon_selection" not in target.extras


@pytest.mark.parametrize("field,reason", [
    ("toolchain", "missing_toolchain"), ("method assets", "missing_asset"),
])
def test_implicit_unavailable_panel_marks_only_copy_number_for_skip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str, reason: str,
) -> None:
    problem = LabProblem(
        "TBX-LAB-020", cause="selected panel method is unavailable",
        fix="Restore the method", data={"field": field},
    )
    monkeypatch.setattr(
        pon_consumer, "selection_for_target",
        lambda *_, **__: (_ for _ in ()).throw(problem),
    )
    target = RunTarget(
        input_id="4" * 64, source_id="5" * 64, input_dir=tmp_path,
        reference_id="hg38", reference_sha256="b" * 64,
    )
    manifest = {
        "kind": "aligned", "tools": {"minimap2": "2.28", "samtools": "1.20"},
        "mmi_sha256": "6" * 64, "aligner_argv": ["minimap2", "-ax", "map-ont"],
    }
    items = plan(load_registry(), only=["instability"])
    assert pon_consumer.bind_target(
        LabStore(tmp_path), items, target, manifest, cohort_id=None,
    ) == list(items)
    assert target.extras["pon_unavailable"]["reason_code"] == reason
    with pytest.raises(LabProblem):
        pon_consumer.bind_target(
            LabStore(tmp_path), items, target, manifest, cohort_id="healthy-v1",
        )


def test_missing_registered_panel_asset_becomes_lab_unavailable_problem(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cn_module, "toolchain_or_none", lambda: SimpleNamespace(
        identity=SimpleNamespace(lock_sha256="1" * 64),
    ))
    missing = ReferenceProblem(
        "TBX-ASSET-004", "Asset is not registered", cause="registration absent",
        fix="Register the asset",
    )
    monkeypatch.setattr(ichor, "resolve_assets", lambda *_: (_ for _ in ()).throw(missing))
    with pytest.raises(LabProblem) as caught:
        pon_consumer.selection_for_generation(
            LabStore(tmp_path), SimpleNamespace(cohort_id="healthy-v1", kind="internal_cohort"),
            source_id="2" * 64, reference_sha256="3" * 64, aligner_identity="synthetic",
        )
    assert caught.value.code == "TBX-LAB-020"
    assert caught.value.data["field"] == "method assets"


@pytest.mark.parametrize("reason", ["missing_toolchain", "missing_asset"])
def test_unavailable_panel_skip_cannot_reuse_uncorrected_cached_copy_number(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reason: str,
) -> None:
    store = LabStore(tmp_path)
    store.ensure()
    monkeypatch.setattr(cn_module, "toolchain_or_none", lambda: None)
    algorithm = cn_module.COPY_NUMBER
    target = RunTarget(
        input_id="4" * 64, source_id="5" * 64, input_dir=tmp_path,
        reference_id="hg38", reference_sha256="b" * 64,
        extras={"pon_unavailable": {
            "reason_code": reason, "message": "built panel unavailable",
        }},
    )
    identity = execution_identity(
        algorithm, reference_sha256=target.reference_sha256,
        tool_pins=[tool.identity() for tool in algorithm.tools],
    )
    cached = ResultEnvelope(
        algorithm_id="copy-number", algorithm_version=algorithm.version, status="ok",
        provenance=provenance(
            tool="fixture", parameters_sha256=identity,
            source_id=target.source_id, input_id=target.input_id,
            reference_id=target.reference_id,
        ),
    )
    store.publish_result(cached, input_id=target.input_id)
    monkeypatch.setattr(
        cn_module.CopyNumber, "asset_sha256s",
        lambda *_: pytest.fail("unavailable panel must not hash method assets"),
    )
    monkeypatch.setattr(
        cn_module.CopyNumber, "preflight",
        lambda *_: pytest.fail("unavailable panel must skip before preflight"),
    )
    run_id = new_run_id()
    result = run_plan(
        store, None, store.run_log(run_id), target,
        [PlanItem(algorithm, False)], {"copy-number": "traceback_lab.algorithms.copy_number:COPY_NUMBER"},
        run_id=run_id, scrubber=Scrubber(root=store.root),
    )[0]
    assert result.status == "skipped" and result.skip_reason == reason
    assert not result.cached
    assert store.read_result(target.input_id, "copy-number", identity).status == "skipped"


def test_member_recompute_runs_pinned_loo_chain_and_requires_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from traceback_lab import runner

    store = LabStore(tmp_path)
    source_id, input_id = "a" * 64, "b" * 64
    manifest = {
        "reference_id": "hg38", "reference_sha256": "c" * 64, "kind": "aligned",
        "tools": {"minimap2": "2.28", "samtools": "1.20"},
        "mmi_sha256": "d" * 64, "aligner_argv": ["minimap2", "-ax", "map-ont"],
    }
    selection = pon.PonSelection(
        "healthy-v1", "healthy-v1:2", "lab_loo_v1", "e" * 64,
        tmp_path / "lab" / "assets" / "pon" / f"{'e' * 64}.rds", 10,
        "c" * 64, "minimap2 2.28; samtools 1.20", "f" * 64,
        "1" * 64, "2" * 64, "3" * 64, "4" * 64, "5" * 64, (), True,
    )
    monkeypatch.setattr(cohorts, "_checked_source_record", lambda *_: {})
    monkeypatch.setattr(cohorts, "_full_input", lambda *_: (input_id, manifest))
    monkeypatch.setattr(pon_consumer, "selection_for_generation", lambda *_, **__: selection)
    expected = object()
    monkeypatch.setattr(cohorts, "member_from_store", lambda *_, **kwargs: (
        expected if kwargs["pon_selection"] is selection else None
    ))
    observed: dict[str, object] = {}

    def fake_run_plan(store, lock, log, target, items, entries, **kwargs):
        assert log.events()[-1]["event"] == "sample"
        assert log.events()[-1]["input_id"] == input_id
        observed["ids"] = [item.algorithm.id for item in items]
        observed["digest"] = next(item.algorithm.parameters["pon_sha256"] for item in items
                                  if item.algorithm.id == "copy-number")
        observed["target"] = target
        return [SimpleNamespace(algorithm_id=item.algorithm.id, status="ok") for item in items]

    monkeypatch.setattr(runner, "run_plan", fake_run_plan)
    generation = SimpleNamespace(cohort_id="healthy-v1")
    log = store.run_log(new_run_id())
    assert pon_consumer.recompute_member(
        store, generation, source_id, lock=None, log=log, run_id=log.run_id, timeout_s=1,
    ) is expected
    sample_events = [event for event in log.events() if event["event"] == "sample"]
    assert len(sample_events) == 1
    assert sample_events[0]["source_id"] == source_id
    assert sample_events[0]["input_id"] == input_id
    assert observed["ids"] == ["read-summary", "copy-number", "instability", "arm-summary"]
    assert observed["digest"] == selection.pon_sha256
    assert observed["target"].extras["pon_selection"]["cohort_version"] == "healthy-v1:2"

    monkeypatch.setattr(runner, "run_plan", lambda *_, **__: [
        SimpleNamespace(algorithm_id="copy-number", status="ok"),
        SimpleNamespace(algorithm_id="instability", status="ok"),
        SimpleNamespace(algorithm_id="arm-summary", status="skipped"),
    ])
    assert pon_consumer.recompute_member(
        store, generation, source_id, lock=None, log=None, run_id="synthetic", timeout_s=1,
    ) is expected

    monkeypatch.setattr(runner, "run_plan", lambda *_, **__: [
        SimpleNamespace(algorithm_id="copy-number", status="failed")
    ])
    with pytest.raises(LabProblem, match="TBX-LAB-020"):
        pon_consumer.recompute_member(
            store, generation, source_id, lock=None, log=None, run_id="synthetic", timeout_s=1,
        )


def test_builder_recomputes_each_loo_before_selecting_generation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = LabStore(tmp_path)
    sources = tuple(_sample(store, index) for index in range(1, 11))
    for source_id in sources:
        add_sample(store, "healthy-v1", source_id)
    build = pon.PonBuild(
        source_ids=sources, full_asset_sha256="a" * 64,
        loo_asset_sha256s={source_id: f"{index:064x}" for index, source_id in enumerate(sources, 101)},
        reference_sha256=f"{900:064x}", aligner_identity="minimap2 2.28; samtools 1.20",
        grid_sha256="b" * 64, counting_parameters_sha256="c" * 64,
        toolchain_lock_sha256="d" * 64, builder_source_sha256="e" * 64,
        builder_sha256="3" * 64, validator_sha256="4" * 64,
        gc_sha256="f" * 64, map_sha256="1" * 64, centromere_sha256="2" * 64,
    )
    monkeypatch.setattr(pon, "build_panels", lambda *_: build)
    calls: list[str] = []

    def fake_recompute(store, pending, source_id, **kwargs):
        assert read_current(store, "healthy-v1") is None
        member = next(item for item in pending.members if item.source_id == source_id)
        assert member.pon_asset_sha256 == build.loo_asset_sha256s[source_id]
        kwargs["log"].append("sample", sample=source_id[:12], source_id=source_id, input_id=member.input_id)
        calls.append(source_id)
        return member

    monkeypatch.setattr(pon_consumer, "recompute_member", fake_recompute)
    snapshot = build_cohort(store, "healthy-v1")
    assert calls == list(sources)
    assert snapshot.pon_n == 10 and snapshot.pon_asset_sha256 == "a" * 64
    assert read_current(store, "healthy-v1") == snapshot
    log = store.run_log(store.run_ids()[0])
    assert [event["event"] for event in log.events()].count("sample") == 10
    assert [event["event"] for event in log.events()][0] == "run_start"
    assert log.events()[-1]["event"] == "run_end"
    assert summarize_run(log.events(), store)["status"] == "ok"
    assert LabApi(store).status()["interrupted"] is None
    assert LabApi(store).status()["run_log_unavailable"] is None


@pytest.mark.parametrize("error,status", [
    (LabProblem("TBX-LAB-020", cause="synthetic member failure", fix="retry"), "blocked"),
    (KeyboardInterrupt(), "interrupted"),
])
def test_failed_member_recompute_keeps_previous_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: BaseException, status: str,
) -> None:
    store = LabStore(tmp_path)
    sources = tuple(_sample(store, index) for index in range(1, 11))
    for source_id in sources[:9]:
        add_sample(store, "healthy-v1", source_id)
    previous = build_cohort(store, "healthy-v1")
    add_sample(store, "healthy-v1", sources[9])
    build = pon.PonBuild(
        source_ids=sources, full_asset_sha256="a" * 64,
        loo_asset_sha256s={source_id: f"{index:064x}" for index, source_id in enumerate(sources, 101)},
        reference_sha256=f"{900:064x}", aligner_identity="minimap2 2.28; samtools 1.20",
        grid_sha256="b" * 64, counting_parameters_sha256="c" * 64,
        toolchain_lock_sha256="d" * 64, builder_source_sha256="e" * 64,
        builder_sha256="3" * 64, validator_sha256="4" * 64,
        gc_sha256="f" * 64, map_sha256="1" * 64, centromere_sha256="2" * 64,
    )
    monkeypatch.setattr(pon, "build_panels", lambda *_: build)
    before_runs = set(store.run_ids())
    attempted: list[str] = []
    first_input = previous.members[0].input_id

    def cn_result(identity: str) -> ResultEnvelope:
        return ResultEnvelope(
            algorithm_id="copy-number", algorithm_version="2", status="ok",
            provenance=provenance(
                tool="fixture", parameters_sha256=identity,
                source_id=sources[0], input_id=first_input, reference_id="hg38",
            ),
        )

    old_identity, pending_identity = "8" * 64, "9" * 64
    store.publish_result(cn_result(old_identity), input_id=first_input)

    def fake_recompute(store, pending, source_id, **kwargs):
        member = next(item for item in pending.members if item.source_id == source_id)
        log = kwargs["log"]
        log.append("sample", sample=source_id[:12], source_id=source_id, input_id=member.input_id)
        attempted.append(source_id)
        if len(attempted) == 1:
            active = LabApi(store).status()["active"]
            assert active["source_id"] == source_id and active["input_id"] == member.input_id
            store.publish_result(cn_result(pending_identity), input_id=first_input)
            assert selected_results(store, first_input)["copy-number"].provenance.parameters_sha256 == pending_identity
        if len(attempted) == 2:
            raise error
        return member

    monkeypatch.setattr(pon_consumer, "recompute_member", fake_recompute)
    with pytest.raises(type(error)):
        build_cohort(store, "healthy-v1")
    assert read_current(store, "healthy-v1") == previous
    assert selected_results(store, first_input)["copy-number"].provenance.parameters_sha256 == old_identity
    assert store.selected_attempt(first_input, "copy-number", pending_identity) is None
    assert attempted == list(sources[:2])
    run_ids = set(store.run_ids()) - before_runs
    assert len(run_ids) == 1
    log = store.run_log(run_ids.pop())
    assert [event["event"] for event in log.events()] == ["run_start", "sample", "sample", "run_end"]
    assert summarize_run(log.events(), store)["status"] == status
    assert log.events()[-1]["status"] == status
    if status == "blocked":
        assert log.events()[-1]["code"] == "TBX-LAB-020"
        assert LabApi(store).status()["interrupted"] is None
    else:
        assert LabApi(store).status()["interrupted"]["run_id"] == log.run_id


@pytest.mark.parametrize("failure_site", ["generation", "current_pointer"])
def test_failed_generation_publish_restores_member_selections(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure_site: str,
) -> None:
    store = LabStore(tmp_path)
    sources = tuple(_sample(store, index) for index in range(1, 11))
    for source_id in sources[:9]:
        add_sample(store, "healthy-v1", source_id)
    previous = build_cohort(store, "healthy-v1")
    add_sample(store, "healthy-v1", sources[9])
    build = pon.PonBuild(
        source_ids=sources, full_asset_sha256="a" * 64,
        loo_asset_sha256s={source_id: f"{index:064x}" for index, source_id in enumerate(sources, 101)},
        reference_sha256=f"{900:064x}", aligner_identity="minimap2 2.28; samtools 1.20",
        grid_sha256="b" * 64, counting_parameters_sha256="c" * 64,
        toolchain_lock_sha256="d" * 64, builder_source_sha256="e" * 64,
        builder_sha256="3" * 64, validator_sha256="4" * 64,
        gc_sha256="f" * 64, map_sha256="1" * 64, centromere_sha256="2" * 64,
    )
    monkeypatch.setattr(pon, "build_panels", lambda *_: build)
    old_identity, pending_identity = "8" * 64, "9" * 64
    first, second = previous.members[:2]

    def result_for(algorithm_id: str, source_id: str, input_id: str, identity: str) -> ResultEnvelope:
        return ResultEnvelope(
            algorithm_id=algorithm_id, algorithm_version="2" if algorithm_id == "copy-number" else "1",
            status="ok",
            provenance=provenance(
                tool="fixture", parameters_sha256=identity,
                source_id=source_id, input_id=input_id, reference_id="hg38",
            ),
        )

    store.publish_result(result_for("copy-number", first.source_id, first.input_id, old_identity), input_id=first.input_id)
    original_selected = store.selected_attempt(first.input_id, "copy-number", old_identity)
    assert original_selected is not None
    old_arm_identity, pending_arm_identity = "c" * 64, "d" * 64
    store.publish_result(result_for("arm-summary", first.source_id, first.input_id, old_arm_identity), input_id=first.input_id)
    original_arm_selected = store.selected_attempt(first.input_id, "arm-summary", old_arm_identity)
    assert original_arm_selected is not None
    attempted: list[str] = []

    def fake_recompute(store, pending, source_id, **kwargs):
        member = next(item for item in pending.members if item.source_id == source_id)
        attempted.append(source_id)
        kwargs["log"].append("sample", sample=source_id[:12], source_id=source_id, input_id=member.input_id)
        if source_id in (first.source_id, second.source_id):
            identity = old_identity if source_id == first.source_id else pending_identity
            store.publish_result(result_for("copy-number", source_id, member.input_id, identity), input_id=member.input_id)
            arm_identity = old_arm_identity if source_id == first.source_id else pending_arm_identity
            store.publish_result(result_for("arm-summary", source_id, member.input_id, arm_identity), input_id=member.input_id)
        return member

    monkeypatch.setattr(pon_consumer, "recompute_member", fake_recompute)
    original_publish = store.publish_directory

    def fail_generation_publish(parent, name, write):
        if parent == store.lab / "cohorts" / "healthy-v1" / "generations":
            raise OSError("synthetic generation publish failure")
        return original_publish(parent, name, write)

    original_write_atomic = cohorts.write_atomic

    def fail_current_write(path, content):
        if path == store.lab / "cohorts" / "healthy-v1" / "current.json":
            raise OSError("synthetic current pointer failure")
        return original_write_atomic(path, content)

    if failure_site == "generation":
        monkeypatch.setattr(store, "publish_directory", fail_generation_publish)
    else:
        monkeypatch.setattr(cohorts, "write_atomic", fail_current_write)
    with pytest.raises(OSError, match="synthetic generation publish failure|synthetic current pointer failure"):
        build_cohort(store, "healthy-v1")
    assert attempted == list(sources)
    assert read_current(store, "healthy-v1") == previous
    assert selected_results(store, first.input_id)["copy-number"].provenance.parameters_sha256 == old_identity
    assert store.selected_attempt(first.input_id, "copy-number", old_identity) == original_selected
    assert selected_results(store, first.input_id)["arm-summary"].provenance.parameters_sha256 == old_arm_identity
    assert store.selected_attempt(first.input_id, "arm-summary", old_arm_identity) == original_arm_selected
    assert "copy-number" not in selected_results(store, second.input_id)
    assert "arm-summary" not in selected_results(store, second.input_id)
    assert store.selected_attempt(second.input_id, "copy-number", pending_identity) is None
    assert store.selected_attempt(second.input_id, "arm-summary", pending_arm_identity) is None
    for member, identity in ((first, old_identity), (second, pending_identity)):
        attempts = store.result_dir(member.input_id, "copy-number", identity) / "attempts"
        assert any((path / "result.json").is_file() for path in attempts.iterdir())
    assert len(list((store.result_dir(first.input_id, "copy-number", old_identity) / "attempts").iterdir())) >= 2
    for member, identity in ((first, old_arm_identity), (second, pending_arm_identity)):
        attempts = store.result_dir(member.input_id, "arm-summary", identity) / "attempts"
        assert any((path / "result.json").is_file() for path in attempts.iterdir())
    assert len(list((store.result_dir(first.input_id, "arm-summary", old_arm_identity) / "attempts").iterdir())) >= 2


@pytest.mark.parametrize("selected_panel", [None, "a" * 64, "b" * 64])
def test_recorded_cn_plan_cannot_override_requested_panel(tmp_path, monkeypatch, selected_panel) -> None:
    """A valid historical identity cannot replace the panel requested now."""
    store = LabStore(tmp_path)
    source_id = _sample(store, 1)
    input_id = f"{101:064x}"
    manifest = json.loads((store.inputs_dir / input_id / "manifest.json").read_bytes())
    algorithm = replace(cn_module.COPY_NUMBER, upstream=())
    monkeypatch.setattr(type(algorithm), "asset_sha256s", lambda self, root: [])
    monkeypatch.setattr(cohorts, "load_registry", lambda: [algorithm])
    requested = SimpleNamespace(pon_policy="lab_loo_v1", pon_sha256="a" * 64)

    def publish(panel):
        producer = (pon_consumer.bind_copy_number_algorithm(
            algorithm, SimpleNamespace(pon_policy="lab_loo_v1", pon_sha256=panel),
        ) if panel is not None else algorithm)
        identity = execution_identity(producer, reference_sha256=manifest["reference_sha256"])
        store.publish_result(ResultEnvelope(
            algorithm_id=producer.id, algorithm_version=producer.version, status="ok",
            provenance=provenance(tool="fixture", parameters_sha256=identity,
                                  source_id=source_id, input_id=input_id, reference_id="synthetic"),
        ), input_id=input_id, execution_plan={
            "schema": "traceback.lab.execution-plan.v1", "algorithm_id": producer.id,
            "algorithm_version": producer.version, "input_id": input_id,
            "reference_sha256": manifest["reference_sha256"], "platform": "ont",
            "parameters": producer.parameters, "tools": [tool.identity() for tool in producer.tools],
            "asset_sha256s": [], "upstream": [],
        })
        return identity

    old_identity = publish("a" * 64)
    current_identity = publish(selected_panel)
    selected = cohorts._selected_results(store, input_id, manifest, pon_selection=requested)
    assert [row.parameters_sha256 for row in selected] == (
        [current_identity] if selected_panel == requested.pon_sha256 else []
    )
    # Rejected latest identities never fall back to the older valid PoN attempt.
    assert store.selected_attempt(input_id, algorithm.id, old_identity) is not None
    independent = cohorts._selected_results(store, input_id, manifest)
    assert [row.parameters_sha256 for row in independent] == (
        [current_identity] if selected_panel is None else []
    )
