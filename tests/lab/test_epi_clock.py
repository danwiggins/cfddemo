"""Mechanical checks for the exploratory Weidner clock and map gate."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from traceback_lab.algorithms import epi_clock
from traceback_lab.algorithms.meth_calls import METH_CALLS
from traceback_lab.epi_clock_model import ClockModelError, WEIDNER_SITES, _parse, inspect_map, register_map
from traceback_lab.meth_calls import DTYPE, HAS_H, HAS_M
from traceback_lab.registry import InputContext
from traceback_lab.registry import Ready, Skip, context_to_json
from traceback_lab.registry import execution_identity
from traceback_lab.store import LabStore

from .bam_fixtures import Read, context_for


def test_clock_identity_pins_literal_coefficients() -> None:
    from traceback_lab.interpretation.engine import load_library

    algorithm = epi_clock.EPI_CLOCK
    assert algorithm.parameters["weights"] == epi_clock.WEIDNER_WEIGHTS
    original = execution_identity(algorithm, reference_sha256="a" * 64)
    changed = replace(algorithm, parameters={**algorithm.parameters, "weights": (0.0, 0.0, 0.0)})
    assert execution_identity(changed, reference_sha256="a" * 64) != original
    assert load_library().entry("epi-clock", "epigenetic_age_years").stability == {
        "kind": "absolute", "tolerance": 1.0}


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
    monkeypatch.setattr(epi_clock, "load_summary", lambda ctx: (
        SimpleNamespace(contigs=["chr1"], lengths=[epi_clock.HG38_CHR1_LENGTH]), None))
    rows = np.zeros(15, dtype=DTYPE)
    rows["channels"] = HAS_M
    rows["ref_pos0"] = np.repeat([100, 110, 120], 5)
    rows["p_m"] = np.repeat([255, 0, 255], 5)
    rows["p_canonical"] = 255 - rows["p_m"]
    calls = SimpleNamespace(chunks=lambda mask: iter([rows]))
    monkeypatch.setattr(epi_clock, "load_calls", lambda ctx: (calls, None))
    monkeypatch.setattr(epi_clock.EPI_CLOCK, "preflight", lambda ctx: Ready())
    ctx = InputContext(tmp_path, "s", "i", "r", "a" * 64, tmp_path,
                       extras={"planned_asset_sha256s": [model.sha256]})
    output = epi_clock.EPI_CLOCK.run(ctx, tmp_path)
    metrics = {item["metric_id"]: item for item in output.metrics}
    assert metrics["epigenetic_age_years"]["value"] == pytest.approx(38 - 26.4 + 164.7)
    assert metrics["epigenetic_age_years"]["n"] == 5
    rows["channels"][5:7] = HAS_H
    rows["p_h"][5:7] = 255
    rows["p_canonical"][5:7] = 0
    output = epi_clock.EPI_CLOCK.run(ctx, tmp_path)
    metrics = {item["metric_id"]: item for item in output.metrics}
    assert metrics["cg25809905_modified_fraction"]["value"] == pytest.approx(0.4)
    assert metrics["cg25809905_modified_fraction"]["n"] == 5
    assert metrics["epigenetic_age_years"]["value"] == pytest.approx(38 - 26.4 - 23.7 * 0.4 + 164.7)
    rows = rows[:-1]
    output = epi_clock.EPI_CLOCK.run(ctx, tmp_path)
    assert output.metrics[0]["value"] is None


def test_clock_preflight_hides_reference_path_on_fasta_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    model = _parse(_map_bytes(), "a" * 64)
    secret = tmp_path / "private" / "participant-reference.fa"
    monkeypatch.setattr(epi_clock.MethBase, "preflight", lambda self, ctx: Ready())
    monkeypatch.setattr(epi_clock, "inspect_map", lambda store: model)
    monkeypatch.setattr(epi_clock, "load_summary", lambda ctx: (
        SimpleNamespace(contigs=["chr1"], lengths=[epi_clock.HG38_CHR1_LENGTH]), None))
    def fail_fasta(path):
        raise OSError(f"cannot open {path}")
    monkeypatch.setattr(epi_clock.pysam, "FastaFile", fail_fasta)
    ctx = InputContext(tmp_path, "s", "i", "hg38", "a" * 64, tmp_path,
                       eligible_reads=5_000_000,
                       extras={"planned_asset_sha256s": [model.sha256],
                               "reference_fasta": str(secret)})
    decision = epi_clock.EPI_CLOCK.preflight(ctx)
    assert isinstance(decision, Skip)
    assert str(secret) not in decision.message


def test_clock_skips_when_probe_chromosome_is_absent_from_bam(monkeypatch, tmp_path: Path) -> None:
    model = _parse(_map_bytes(), "a" * 64)
    model = replace(model, sites=(model.sites[0], model.sites[1],
                                  (model.sites[2][0], "chr19", model.sites[2][2])))
    monkeypatch.setattr(epi_clock.MethBase, "preflight", lambda self, ctx: Ready())
    monkeypatch.setattr(epi_clock, "inspect_map", lambda store: model)
    monkeypatch.setattr(epi_clock, "load_summary", lambda ctx: (
        SimpleNamespace(contigs=["chr1"], lengths=[epi_clock.HG38_CHR1_LENGTH]), None))
    ctx = InputContext(tmp_path, "s", "i", "hg38", "a" * 64, tmp_path,
                       eligible_reads=5_000_000,
                       extras={"planned_asset_sha256s": [model.sha256]})
    assert epi_clock.EPI_CLOCK.preflight(ctx).reason_code == "reference"
    monkeypatch.setattr(epi_clock, "load_calls", lambda ctx: (SimpleNamespace(chunks=lambda mask: iter(())), None))
    with pytest.raises(RuntimeError, match="reference or input changed"):
        epi_clock.EPI_CLOCK.run(ctx, tmp_path)


def test_clock_preflight_and_worker_with_real_call_store(tmp_path: Path) -> None:
    """Synthetic coordinates and depth exercise plumbing, not biological qualification."""

    import pysam

    sequence = list("A" * 200)
    for pos0 in (100, 110, 120):
        sequence[pos0:pos0 + 2] = list("CG")
    fasta = tmp_path / "synthetic.fa"
    # Stream a full-length synthetic chr1 without allocating 249 MB in memory.
    with fasta.open("w") as stream:
        stream.write(">chr1\n" + "".join(sequence))
        remaining = epi_clock.HG38_CHR1_LENGTH - len(sequence)
        block = "A" * (1 << 20)
        while remaining:
            size = min(remaining, len(block))
            stream.write(block[:size])
            remaining -= size
        stream.write("\n")
    pysam.faidx(str(fasta))
    reads = [Read(f"r-{site}-{i}", "chr1", pos0, "CGAAAAAAAA", mm="C+m?,0;", ml=[255 if site != 1 else 0])
             for site, pos0 in enumerate((100, 110, 120)) for i in range(5)]
    ctx = context_for(tmp_path, [("chr1", epi_clock.HG38_CHR1_LENGTH)], reads,
                      fasta=fasta, eligible_reads=5_000_000)
    source = tmp_path / "clock-map.json"
    raw = _map_bytes()
    source.write_bytes(raw)
    model = register_map(LabStore(tmp_path), source, hashlib.sha256(raw).hexdigest())
    intermediate = tmp_path / "meth-calls"
    METH_CALLS.run(ctx, intermediate)
    extras = dict(ctx.extras)
    extras["upstream"] = {**extras["upstream"], "meth-calls": str(intermediate)}
    extras["planned_asset_sha256s"] = [model.sha256]
    ctx = replace(ctx, extras=extras)
    assert isinstance(epi_clock.EPI_CLOCK.preflight(ctx), Ready)
    assert isinstance(epi_clock.EPI_CLOCK.preflight(replace(ctx, eligible_reads=4_999_999)), Skip)
    assert isinstance(epi_clock.EPI_CLOCK.preflight(replace(ctx, extras={**extras, "planned_asset_sha256s": []})), Skip)
    scratch = tmp_path / "worker"
    scratch.mkdir()
    request = scratch / "request.json"
    request.write_text(json.dumps({"entry": "epi_clock:EPI_CLOCK", "context": context_to_json(ctx),
                                   "out_dir": str(scratch)}))
    subprocess.run([sys.executable, "-m", "traceback_lab.worker", str(request)], check=True)
    metrics = {item["metric_id"]: item for item in json.loads((scratch / "output.json").read_text())["metrics"]}
    assert metrics["epigenetic_age_years"]["value"] == pytest.approx(38 - 26.4 + 164.7)
    assert metrics["epigenetic_age_years"]["n"] == 5
    derived = replace(ctx, derived={"transform": "split", "half": "A"})
    assert epi_clock.EPI_CLOCK.run(derived, scratch).metrics[0]["value"] is None
    without_reference = replace(ctx, extras={**extras, "reference_fasta": str(tmp_path / "missing.fa")})
    with pytest.raises(RuntimeError, match="reference or input changed"):
        epi_clock.EPI_CLOCK.run(without_reference, scratch)
    (LabStore(tmp_path).lab / "assets" / "epi-clock-map" / "coordinates.json").write_bytes(b"{}")
    assert isinstance(epi_clock.EPI_CLOCK.preflight(ctx), Skip)
    with pytest.raises(ClockModelError):
        epi_clock.EPI_CLOCK.run(ctx, scratch)
