"""PoN bytes and parameter invariants before cohort binding is wired."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from traceback_lab.adapters.ichor import CopyNumberError, LabCopyNumberParameters, stage_verified_pon
from traceback_lab.algorithms import copy_number as cn_module
from traceback_lab import cohorts, pon, pon_consumer
from traceback_lab.cohorts import add_sample, build_cohort, read_current
from traceback_lab.identity import cohort_compat_key
from traceback_lab.pon_consumer import bind_copy_number
from traceback_lab.registry import InputContext, execution_identity, load_registry, plan
from traceback_lab.runner import RunTarget
from traceback_lab.store import LabStore
from traceback_lab.problems import LabProblem
from tests.lab.test_cohort_store import _sample


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


def test_default_without_built_panel_stays_none_but_explicit_selection_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = LabStore(tmp_path)
    monkeypatch.setattr(cohorts, "read_current", lambda *_: None)
    common = dict(store=store, source_id="a" * 64, reference_sha256="b" * 64,
                  aligner_identity="minimap2 2.28; samtools 1.20")
    assert pon_consumer.selection_for_target(cohort_id=None, **common) is None
    with pytest.raises(LabProblem, match="TBX-LAB-020"):
        pon_consumer.selection_for_target(cohort_id="healthy-v1", **common)


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
        toolchain_lock_sha256="d" * 64, builder_sha256="e" * 64,
        gc_sha256="f" * 64, map_sha256="1" * 64, centromere_sha256="2" * 64,
    )
    monkeypatch.setattr(pon, "build_panels", lambda *_: build)
    calls: list[str] = []

    def fake_recompute(store, pending, source_id, **kwargs):
        assert read_current(store, "healthy-v1") is None
        member = next(item for item in pending.members if item.source_id == source_id)
        assert member.pon_asset_sha256 == build.loo_asset_sha256s[source_id]
        calls.append(source_id)
        return member

    monkeypatch.setattr(pon_consumer, "recompute_member", fake_recompute)
    snapshot = build_cohort(store, "healthy-v1")
    assert calls == list(sources)
    assert snapshot.pon_n == 10 and snapshot.pon_asset_sha256 == "a" * 64
    assert read_current(store, "healthy-v1") == snapshot


def test_failed_member_recompute_keeps_previous_generation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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
        toolchain_lock_sha256="d" * 64, builder_sha256="e" * 64,
        gc_sha256="f" * 64, map_sha256="1" * 64, centromere_sha256="2" * 64,
    )
    monkeypatch.setattr(pon, "build_panels", lambda *_: build)
    monkeypatch.setattr(pon_consumer, "recompute_member", lambda *_, **__: (_ for _ in ()).throw(
        LabProblem("TBX-LAB-020", cause="synthetic member failure", fix="retry")
    ))
    with pytest.raises(LabProblem, match="TBX-LAB-020"):
        build_cohort(store, "healthy-v1")
    assert read_current(store, "healthy-v1") == previous
