"""Mechanical checks for the exploratory Weidner clock and map gate."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from traceback_lab.algorithms import epi_clock
from traceback_lab.epi_clock_model import ClockModelError, WEIDNER_SITES, _parse, inspect_map, register_map
from traceback_lab.meth_calls import DTYPE, HAS_M
from traceback_lab.registry import InputContext
from traceback_lab.store import LabStore


def _map_bytes() -> bytes:
    return json.dumps({
        "schema": "traceback.lab.epi-clock-map.v1", "model_id": "Weidner-3",
        "genome_build": "hg38", "source_label": "test fixture only",
        "sites": [{"cpg_id": name, "chrom": "chr1", "pos0": 100 + 10 * index}
                  for index, name in enumerate(WEIDNER_SITES)],
    }).encode()


def test_clock_map_requires_exact_model_and_immutable_sha(tmp_path: Path) -> None:
    raw = _map_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    source = tmp_path / "map.json"
    source.write_bytes(raw)
    store = LabStore(tmp_path / "root")
    with pytest.raises(ClockModelError, match="differs"):
        register_map(store, source, "0" * 64)
    registered = register_map(store, source, digest)
    assert registered.sha256 == inspect_map(store).sha256 == digest
    assert register_map(store, source, digest) == registered
    (store.lab / "assets" / "epi-clock-map" / "coordinates.json").write_bytes(b"{}")
    with pytest.raises(ClockModelError, match="changed"):
        inspect_map(store)


@pytest.mark.parametrize("change", [
    {"genome_build": "hg19"},
    {"model_id": "unknown"},
    {"sites": [{"cpg_id": name, "chrom": "chr1", "pos0": 100} for name in WEIDNER_SITES]},
])
def test_clock_map_rejects_incompatible_coordinates(change: dict) -> None:
    data = json.loads(_map_bytes())
    data.update(change)
    with pytest.raises(ClockModelError):
        _parse(json.dumps(data).encode(), "0" * 64)


def test_clock_requires_all_three_sites_and_uses_published_coefficients(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    model = _parse(_map_bytes(), "a" * 64)
    monkeypatch.setattr(epi_clock, "inspect_map", lambda store: model)
    monkeypatch.setattr(epi_clock, "load_summary", lambda ctx: (SimpleNamespace(contigs=["chr1"]), None))
    rows = np.zeros(15, dtype=DTYPE)
    rows["channels"] = HAS_M
    rows["ref_pos0"] = np.repeat([100, 110, 120], 5)
    rows["p_m"] = np.repeat([255, 0, 255], 5)
    rows["p_canonical"] = 255 - rows["p_m"]
    calls = SimpleNamespace(chunks=lambda mask: iter([rows]))
    monkeypatch.setattr(epi_clock, "load_calls", lambda ctx: (calls, None))
    ctx = InputContext(tmp_path, "s", "i", "r", "a" * 64, tmp_path,
                       extras={"planned_asset_sha256s": [model.sha256]})
    output = epi_clock.EPI_CLOCK.run(ctx, tmp_path)
    metrics = {item["metric_id"]: item for item in output.metrics}
    assert metrics["epigenetic_age_years"]["value"] == pytest.approx(38 - 26.4 + 164.7)
    assert metrics["epigenetic_age_years"]["n"] == 5
    rows = rows[:-1]
    output = epi_clock.EPI_CLOCK.run(ctx, tmp_path)
    assert output.metrics[0]["value"] is None
