"""An implicit panel mismatch skips copy number without hiding panel damage."""

from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from traceback_lab import cohorts, controls, pon, pon_consumer
from traceback_lab.adapters import ichor
from traceback_lab.algorithms._base import Base
from traceback_lab.algorithms.copy_number import COPY_NUMBER
from traceback_lab.algorithms.instability import INSTABILITY
from traceback_lab.cohort_contracts import CohortGeneration, CohortMember, CohortPon
from traceback_lab.derived import DerivedInput, DerivedSpec
from traceback_lab.problems import LabProblem
from traceback_lab.registry import PlanItem, Skip
from traceback_lab.runner import RunTarget, run_plan
from traceback_lab.scrub import Scrubber
from traceback_lab.store import LabStore, new_run_id
from traceback_runner.copy_number_method import (
    parameters_sha256 as signed_parameters_sha256,
)


def _built_panel(store: LabStore, monkeypatch: pytest.MonkeyPatch) -> tuple[CohortGeneration, Path]:
    panel_bytes = b"verified panel for protocol-drift regression"
    digest = hashlib.sha256(panel_bytes).hexdigest()
    path = store.lab / "assets" / "pon" / f"{digest}.rds"
    path.parent.mkdir(parents=True)
    path.write_bytes(panel_bytes)
    sources = tuple(f"{i:064x}" for i in range(1, 11))
    loo = tuple((source, f"{i + 100:064x}") for i, source in enumerate(sources, 1))
    reference = "b" * 64
    aligner = "minimap2 2.28; samtools 1.20"
    build = CohortPon(
        source_ids=sources, loo_asset_sha256s=loo,
        reference_sha256=reference, aligner_identity=aligner,
        grid_sha256="c" * 64,
        counting_parameters_sha256=signed_parameters_sha256(ichor.default_parameters().signed),
        toolchain_lock_sha256="d" * 64,
        builder_source_sha256=pon.BUILDER_SOURCE_SHA256,
        builder_sha256=pon.BUILDER_SHA256,
        validator_sha256=pon.VALIDATOR_SHA256,
        gc_sha256="e" * 64, map_sha256="f" * 64, centromere_sha256="1" * 64,
    )
    generation = CohortGeneration(
        cohort_id="healthy-v1", version=1,
        members=tuple(CohortMember(
            source_id=source, input_id=f"{i + 200:064x}",
            aligner_identity=aligner, results=(), pon_asset_sha256=loo_digest,
        ) for i, (source, loo_digest) in enumerate(loo, 1)),
        pon_n=10, pon_asset_sha256=digest, pon_build=build,
    )
    monkeypatch.setattr(cohorts, "read_current", lambda *_: generation)
    from traceback_lab.algorithms import copy_number

    monkeypatch.setattr(copy_number, "toolchain_or_none", lambda: SimpleNamespace(
        identity=SimpleNamespace(lock_sha256="d" * 64),
    ))
    monkeypatch.setattr(pon_consumer.ichor, "resolve_assets", lambda *_: object())
    monkeypatch.setattr(pon_consumer.ichor, "asset_sha256s", lambda *_: {
        "gc_wig": "e" * 64, "map_wig": "f" * 64, "centromere": "1" * 64,
    })
    return generation, path


def _target(store: LabStore) -> RunTarget:
    return RunTarget(
        input_id="a" * 64, source_id="9" * 64, input_dir=store.root,
        reference_id="hg38", reference_sha256="b" * 64,
    )


@pytest.mark.parametrize("manifest", [
    {"kind": "prealigned"},
    {"kind": "aligned", "tools": {"minimap2": "2.29", "samtools": "1.20"},
     "mmi_sha256": "2" * 64, "aligner_argv": ["minimap2", "-ax", "map-ont"]},
])
def test_implicit_real_protocol_drift_skips_only_copy_number_chain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, manifest: dict[str, object],
) -> None:
    store = LabStore(tmp_path)
    _built_panel(store, monkeypatch)
    target = _target(store)
    independent = Base("independent", upstream=())
    independent.preflight = lambda _: Skip("depth", "independent algorithm evaluated")  # type: ignore[method-assign]
    items = [PlanItem(COPY_NUMBER, False), PlanItem(INSTABILITY, False), PlanItem(independent, False)]
    assert pon_consumer.bind_target(store, items, target, manifest, cohort_id=None) == items
    assert target.extras["pon_unavailable"]["reason_code"] == "protocol_mismatch"
    assert "pon_selection" not in target.extras
    run_id = new_run_id()
    results = run_plan(
        store, None, store.run_log(run_id), target, items, {},
        run_id=run_id, scrubber=Scrubber(root=store.root),
    )
    assert [(result.algorithm_id, result.skip_reason) for result in results] == [
        ("copy-number", "protocol_mismatch"),
        ("instability", "upstream"),
        ("independent", "depth"),
    ]


def test_implicit_drift_propagates_to_derived_copy_number(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = LabStore(tmp_path)
    _built_panel(store, monkeypatch)
    parent = _target(store)
    parent.eligible_reads = 2_000_000
    pon_consumer.bind_target(store, [PlanItem(COPY_NUMBER, False)], parent, {"kind": "prealigned"}, cohort_id=None)
    spec = DerivedSpec("cna", {"tf": 0.1}, "fixture")
    child = DerivedInput(spec, "8" * 64, parent.input_id, tmp_path)
    monkeypatch.setattr(controls, "materialize", lambda *_, **__: {spec.label(): child})
    monkeypatch.setattr(controls, "record_derived", lambda *_, **__: None)
    run_id = new_run_id()
    runs = controls.run_derived(
        store, None, store.run_log(run_id), parent, [spec],
        [PlanItem(COPY_NUMBER, False)], {}, run_id=run_id, read_summary=None,
        timeout_s=1, scrubber=Scrubber(root=store.root),
    )
    assert len(runs) == 1
    assert [(result.algorithm_id, result.skip_reason) for result in runs[0].results] == [
        ("copy-number", "protocol_mismatch"),
    ]


def test_explicit_protocol_drift_still_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = LabStore(tmp_path)
    _built_panel(store, monkeypatch)
    target = _target(store)
    with pytest.raises(LabProblem) as error:
        pon_consumer.bind_target(
            store, [PlanItem(COPY_NUMBER, False)], target,
            {"kind": "prealigned"}, cohort_id="healthy-v1",
        )
    assert error.value.data["reason"] == "protocol_mismatch"
    assert "pon_unavailable" not in target.extras


def test_drift_with_tampered_panel_refuses_instead_of_skipping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = LabStore(tmp_path)
    _, panel = _built_panel(store, monkeypatch)
    panel.write_bytes(b"tampered after publication")
    target = _target(store)
    with pytest.raises(LabProblem) as error:
        pon_consumer.bind_target(
            store, [PlanItem(COPY_NUMBER, False)], target,
            {"kind": "prealigned"}, cohort_id=None,
        )
    assert error.value.cause == "the selected PoN asset changed"
    assert error.value.data.get("reason") is None
    assert "pon_unavailable" not in target.extras


def test_drift_with_wrong_member_loo_refuses_instead_of_skipping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = LabStore(tmp_path)
    generation, _ = _built_panel(store, monkeypatch)
    member = generation.members[0].model_copy(update={"pon_asset_sha256": "7" * 64})
    damaged = generation.model_copy(update={"members": (member, *generation.members[1:])})
    monkeypatch.setattr(cohorts, "read_current", lambda *_: damaged)
    target = _target(store)
    target.source_id = member.source_id
    with pytest.raises(LabProblem) as error:
        pon_consumer.bind_target(
            store, [PlanItem(COPY_NUMBER, False)], target,
            {"kind": "prealigned"}, cohort_id=None,
        )
    assert error.value.cause == "the selected cohort has the wrong member leave-one-out panel"
    assert error.value.data.get("reason") is None
    assert "pon_unavailable" not in target.extras
