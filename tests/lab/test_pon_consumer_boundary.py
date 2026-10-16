"""PoN bytes and parameter invariants before cohort binding is wired."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from traceback_lab.adapters.ichor import CopyNumberError, LabCopyNumberParameters, stage_verified_pon
from traceback_lab.algorithms import copy_number as cn_module
from traceback_lab.registry import InputContext


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
