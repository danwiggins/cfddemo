"""Synthetic PoN construction and immutable cohort binding checks."""

from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from traceback_lab import pon
from traceback_lab.cohort_contracts import CohortGeneration, CohortMember
from traceback_lab.problems import LabProblem
from traceback_lab.store import LabStore


def _sha(number: int) -> str:
    return f"{number:064x}"


def _inputs(store: LabStore, count: int = 10) -> tuple[pon.PonInput, ...]:
    return tuple(
        pon.PonInput(_sha(number), _sha(number + 100), _sha(900), "minimap2 2.28; samtools 1.20",
                     store.inputs_dir / _sha(number + 100) / "aligned.bam")
        for number in range(1, count + 1)
    )


def _fake_builder(monkeypatch: pytest.MonkeyPatch, store: LabStore) -> list[tuple[str, tuple[str, ...]]]:
    from traceback_runner import toolchain

    monkeypatch.setattr(toolchain, "resolve_copy_number_toolchain", lambda: SimpleNamespace(
        identity=SimpleNamespace(lock_sha256=_sha(800)),
    ))
    assets_dir = store.root / "assets"
    assets_dir.mkdir()
    for name in ("gc.wig", "map.wig", "centromere.tsv"):
        (assets_dir / name).write_text(name)
    monkeypatch.setattr(pon.ichor, "resolve_assets", lambda *_: pon.ichor.IchorAssets(
        assets_dir / "gc.wig", assets_dir / "map.wig", assets_dir / "centromere.tsv",
    ))

    def count(item: pon.PonInput, work: Path, *_: object) -> tuple[Path, str]:
        work.mkdir()
        wig = work / f"{item.source_id}.wig"
        wig.write_text(item.source_id)
        return wig, _sha(700)

    calls: list[tuple[str, tuple[str, ...]]] = []

    def create(_: object, work: Path, wigs: tuple[Path, ...], assets: object, label: str) -> Path:
        del assets
        source_ids = tuple(wig.stem for wig in wigs)
        calls.append((label, source_ids))
        panel = work / label / "panel_median.rds"
        panel.parent.mkdir()
        panel.write_bytes("\n".join(source_ids).encode())
        return panel

    def validate(_: object, panel: Path, source_ids: tuple[str, ...], full: Path | None) -> None:
        assert tuple(panel.read_text().splitlines()) == source_ids
        assert full is None or tuple(full.read_text().splitlines()) == tuple(item.source_id for item in _inputs(store))

    monkeypatch.setattr(pon, "_count_wig", count)
    monkeypatch.setattr(pon, "_create_panel", create)
    monkeypatch.setattr(pon, "_validate_panel", validate)
    return calls


def test_full_and_leave_one_out_panels_are_distinct_immutable_assets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = LabStore(tmp_path)
    calls = _fake_builder(monkeypatch, store)
    build = pon.build_panels(store, tuple(reversed(_inputs(store))))
    expected = tuple(item.source_id for item in _inputs(store))
    assert build.source_ids == expected
    assert build.pon_n == 10
    assert calls[0] == ("full", expected)
    assert len(calls) == 11
    assert {excluded: included for label, included in calls[1:]
            for excluded in (label.removeprefix("loo-"),)} == {
        source_id: tuple(other for other in expected if other != source_id) for source_id in expected
    }
    parent = store.lab / "assets" / "pon"
    assert (parent / f"{build.full_asset_sha256}.rds").read_bytes() == "\n".join(expected).encode()
    for source_id, digest in build.loo_asset_sha256s.items():
        assert digest != build.full_asset_sha256
        assert (parent / f"{digest}.rds").exists()
        assert source_id not in (parent / f"{digest}.rds").read_text().splitlines()
    assert not list(parent.glob(".staging-*"))


def test_failed_panel_validation_publishes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = LabStore(tmp_path)
    _fake_builder(monkeypatch, store)
    pointer = store.lab / "cohorts" / "healthy" / "current.json"
    pointer.parent.mkdir(parents=True)
    pointer.write_bytes(b'{"version":1}')
    original = pon._validate_panel

    def fail_at_fifth(_: object, panel: Path, source_ids: tuple[str, ...], full: Path | None) -> None:
        original(_, panel, source_ids, full)
        if full is not None and len(source_ids) == 9 and _sha(5) not in source_ids:
            raise LabProblem("TBX-LAB-020", cause="synthetic R failure", fix="retry")

    monkeypatch.setattr(pon, "_validate_panel", fail_at_fifth)
    with pytest.raises(LabProblem, match="TBX-LAB-020"):
        pon.build_panels(store, _inputs(store))
    assert pointer.read_bytes() == b'{"version":1}'
    assert not list((store.lab / "assets" / "pon").glob("*.rds"))
    assert not list((store.lab / "assets" / "pon").glob(".staging-*"))


def test_cohort_binding_uses_member_loo_and_full_for_nonmember(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = LabStore(tmp_path)
    _fake_builder(monkeypatch, store)
    build = pon.build_panels(store, _inputs(store))
    members = tuple(CohortMember(
        source_id=item.source_id, input_id=item.input_id, aligner_identity=item.aligner_identity,
        results=(), pon_asset_sha256=build.loo_asset_sha256s[item.source_id],
    ) for item in _inputs(store))
    generation = CohortGeneration(
        cohort_id="healthy", version=1, members=members, pon_n=build.pon_n,
        pon_asset_sha256=build.full_asset_sha256, pon_build=build.provenance(),
    )
    expected = {
        "reference_sha256": _sha(900), "toolchain_lock_sha256": _sha(800),
        "counting_parameters_sha256": build.counting_parameters_sha256, "grid_sha256": _sha(700),
        "aligner_identity": build.aligner_identity, "gc_sha256": build.gc_sha256,
        "map_sha256": build.map_sha256, "centromere_sha256": build.centromere_sha256,
    }
    member_selection = pon.binding_for(store, generation, _sha(1), **expected)
    assert member_selection.pon_sha256 == build.loo_asset_sha256s[_sha(1)]
    assert member_selection.target_excluded
    assert _sha(1) not in member_selection.panel_source_ids
    external_selection = pon.binding_for(store, generation, _sha(99), **expected)
    assert external_selection.pon_sha256 == build.full_asset_sha256
    assert not external_selection.target_excluded
    assert external_selection.pon_policy == "lab_loo_v1"
    pon.require_grid(member_selection, _sha(700))
    with pytest.raises(LabProblem, match="TBX-LAB-020"):
        pon.require_grid(member_selection, _sha(701))
    with pytest.raises(LabProblem, match="TBX-LAB-020"):
        pon.binding_for(store, generation, _sha(1), **{**expected, "reference_sha256": _sha(901)})
    with pytest.raises(LabProblem, match="TBX-LAB-020"):
        pon.binding_for(store, generation, _sha(1), **{**expected, "aligner_identity": "other aligner"})
    with pytest.raises(LabProblem, match="TBX-LAB-020"):
        pon.binding_for(store, generation, _sha(1), **{**expected, "gc_sha256": _sha(901)})
    stored = store.lab / "assets" / "pon" / f"{build.full_asset_sha256}.rds"
    stored.write_bytes(b"changed")
    with pytest.raises(LabProblem, match="TBX-LAB-020"):
        pon.binding_for(store, generation, _sha(99), **expected)


def test_generation_rejects_missing_member_panel(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = LabStore(tmp_path)
    _fake_builder(monkeypatch, store)
    build = pon.build_panels(store, _inputs(store))
    members = tuple(CohortMember(source_id=item.source_id, input_id=item.input_id,
                                 aligner_identity=item.aligner_identity, results=()) for item in _inputs(store))
    with pytest.raises(ValidationError, match="leave-one-out"):
        CohortGeneration(cohort_id="healthy", version=1, members=members, pon_n=10,
                         pon_asset_sha256=build.full_asset_sha256, pon_build=build.provenance())


def test_explicit_selection_refuses_generation_without_panel(tmp_path: Path) -> None:
    generation = CohortGeneration(cohort_id="healthy", version=1, members=())
    with pytest.raises(LabProblem, match="TBX-LAB-020"):
        pon.binding_for(LabStore(tmp_path), generation, _sha(1), reference_sha256=_sha(900),
                        aligner_identity="minimap2 2.28; samtools 1.20", toolchain_lock_sha256=_sha(800),
                        counting_parameters_sha256=_sha(700), gc_sha256=_sha(701), map_sha256=_sha(702),
                        centromere_sha256=_sha(703))


def test_vendored_builder_matches_reviewed_upstream_bytes() -> None:
    assert hashlib.sha256(pon.BUILDER_SCRIPT.read_bytes()).hexdigest() == pon.BUILDER_SHA256


def test_builder_passes_canonical_protocol_arguments(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    observed: list[tuple[Path, str, tuple[str, ...]]] = []

    def fake_run(_: object, script: Path, checksum: str, args: tuple[str, ...], work: Path) -> None:
        observed.append((script, checksum, args))
        (work / "panel_median.rds").write_bytes(b"synthetic RDS")

    monkeypatch.setattr(pon, "_run_r", fake_run)
    work = tmp_path / "work"
    work.mkdir()
    wig = tmp_path / f"{_sha(1)}.wig"
    wig.write_text("synthetic")
    assets = pon.ichor.IchorAssets(tmp_path / "gc.wig", tmp_path / "map.wig", tmp_path / "centromere.tsv")
    panel = pon._create_panel(object(), work, (wig,), assets, "full")
    assert panel.read_bytes() == b"synthetic RDS"
    script, checksum, args = observed[0]
    assert script == pon.BUILDER_SCRIPT and checksum == pon.BUILDER_SHA256
    assert args[args.index("--chrs") + 1] == "c(1:22)"
    assert args[args.index("--chrNormalize") + 1] == "c(1:22)"
    assert args[args.index("--genomeStyle") + 1] == "UCSC"
    assert args[args.index("--method") + 1] == "median"
    assert Path(args[args.index("--filelist") + 1]).read_text() == f"{wig}\n"


def test_builder_rejects_too_few_or_mixed_protocol_sources(tmp_path: Path) -> None:
    store = LabStore(tmp_path)
    with pytest.raises(LabProblem, match="TBX-LAB-020"):
        pon.build_panels(store, _inputs(store, 9))
    mixed = list(_inputs(store))
    mixed[-1] = pon.PonInput(mixed[-1].source_id, mixed[-1].input_id, _sha(901),
                             mixed[-1].aligner_identity, mixed[-1].bam_path)
    with pytest.raises(LabProblem, match="TBX-LAB-020"):
        pon.build_panels(store, tuple(mixed))
