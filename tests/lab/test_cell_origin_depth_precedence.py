"""The measured-read floor decides low-depth cell-origin status first."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from traceback_lab.algorithms import cell_origin
from traceback_lab.registry import InputContext


@pytest.mark.parametrize("eligible_reads", [None, 0, cell_origin.MIN_ELIGIBLE_READS - 1])
def test_real_low_depth_precedes_missing_setup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, eligible_reads: int | None,
) -> None:
    assert cell_origin.CELL_ORIGIN.requires.min_eligible_reads == cell_origin.MIN_ELIGIBLE_READS
    monkeypatch.setattr(cell_origin, "loyfer_assets", lambda _: pytest.fail("low depth must not probe assets"))
    monkeypatch.setattr(cell_origin, "header_model", lambda _: pytest.fail("low depth must not inspect BAM"))
    ctx = InputContext(
        root=tmp_path, source_id="a" * 64, input_id="b" * 64,
        reference_id="hg38", reference_sha256="c" * 64,
        input_dir=tmp_path / "missing-input", eligible_reads=eligible_reads,
    )
    assert cell_origin.CELL_ORIGIN.preflight(ctx).reason_code == "depth"


def test_example_and_supported_depth_keep_their_distinct_reasons(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = InputContext(
        root=tmp_path, source_id="a" * 64, input_id="b" * 64,
        reference_id="hg38", reference_sha256="c" * 64,
        input_dir=tmp_path / "missing-input", eligible_reads=cell_origin.MIN_ELIGIBLE_READS,
    )
    def missing_assets(_: Path) -> None:
        raise FileNotFoundError("atlas is not registered")

    monkeypatch.setattr(cell_origin, "loyfer_assets", missing_assets)
    assert cell_origin.CELL_ORIGIN.preflight(ctx).reason_code == "missing_asset"
    assert cell_origin.CELL_ORIGIN.preflight(
        replace(ctx, eligible_reads=0, example=True)
    ).reason_code == "example_reference"
