"""Exact WPS geometry and bounded genome-track aggregation."""

from __future__ import annotations

import io
import hashlib
import json
from types import SimpleNamespace

import numpy as np
import pytest

from traceback_lab.algorithms import wps
from traceback_lab.algorithms.wps import HALF, WINDOW, WPS, chromosome_track, score_centres
from traceback_lab.envelope import ResultEnvelope, provenance
from traceback_lab.interpretation.engine import load_library
from traceback_lab.read_summary import write_summary
from traceback_lab.registry import InputContext, Ready, Skip, context_to_json, load_registry
from traceback_lab.store import LabStore
from traceback_lab.worker import main as worker_main


def _summary_context(tmp_path, *, depth: float | None = 10.0) -> InputContext:
    summary = tmp_path / "summary" / "columns"
    summary.mkdir(parents=True, exist_ok=True)
    (summary / "meta.json").write_text(json.dumps({"contigs": ["chr1"], "lengths": [1200]}))
    return InputContext(root=tmp_path, source_id="a" * 64, input_id="b" * 64,
                        reference_id="hg38", reference_sha256="c" * 64,
                        input_dir=tmp_path, approx_depth_x=depth,
                        extras={"upstream": {"read-summary": str(summary.parent)}})


def _naive(starts: np.ndarray, ends: np.ndarray, lo: int, hi: int) -> list[int]:
    scores = []
    for centre in range(lo, hi):
        left, right = centre - HALF, centre + HALF
        spanning = sum(start <= left and end >= right for start, end in zip(starts, ends))
        ending = sum(left <= start < right or left <= end - 1 < right
                     for start, end in zip(starts, ends))
        scores.append(spanning - ending)
    return scores


def test_wps_counts_fragments_with_both_ends_in_window_only_once() -> None:
    starts = np.asarray([100, 130, 160, 280], dtype=np.int64)
    ends = np.asarray([220, 270, 340, 400], dtype=np.int64)
    assert WINDOW == 120
    assert score_centres(starts, ends, 150, 350).tolist() == _naive(starts, ends, 150, 350)


def test_genome_track_is_chunk_invariant_and_filters_size_and_bounds() -> None:
    starts = np.asarray([100, 250, 500, 900, -5, 1020], dtype=np.int64)
    ends = np.asarray([220, 410, 680, 1019, 125, 1201], dtype=np.int64)
    expected = np.asarray(_naive(starts[:3], ends[:3], HALF, 1200 - HALF + 1))
    exact: list[tuple[int, np.ndarray]] = []
    mean, counts = chromosome_track(starts, ends, 1200, bin_bp=100, chunk_bp=137,
                                    emit=lambda start, score: exact.append((start, score.copy())))
    other, other_counts = chromosome_track(starts, ends, 1200, bin_bp=100, chunk_bp=400)
    assert np.array_equal(mean, other)
    assert np.array_equal(counts, other_counts)
    assert [start for start, _ in exact] == list(range(HALF, 1200 - HALF + 1, 137))
    assert np.concatenate([score for _, score in exact]).tolist() == expected.tolist()
    for index in range(len(mean)):
        positions = np.arange(HALF, 1200 - HALF + 1)
        selected = positions // 100 == index
        assert counts[index] == int(selected.sum())
        if selected.any():
            assert mean[index] == np.float32(expected[selected].mean())


def test_wps_accepts_a_real_read_summary(tmp_path) -> None:
    import pysam

    bam = tmp_path / "aligned.bam"
    with pysam.AlignmentFile(str(bam), "wb", header={
        "HD": {"VN": "1.6"}, "SQ": [{"SN": "chr1", "LN": 1200}],
    }) as out:
        for index, start in enumerate((100, 250, 500)):
            read = pysam.AlignedSegment()
            read.query_name = f"ont{index}"
            read.query_sequence = "A" * 150
            read.flag = 0
            read.reference_id = 0
            read.reference_start = start
            read.mapping_quality = 60
            read.cigarstring = "150M"
            out.write(read)
    ctx = _summary_context(tmp_path)
    write_summary(bam, tmp_path / "summary" / "columns")
    result = WPS.run(ctx, tmp_path / "output")
    panel = json.loads(result.artifacts["wps_summary.json"])
    assert panel["contigs"] == [{"contig": "chr1", "length_bp": 1200, "bins": 2}]
    assert panel["qualifying_fragments"] == 3
    assert panel["qualifying_fragment_depth_x"] == 450 / 1200
    assert next(m["value"] for m in result.metrics if m["metric_id"] == "qualifying_fragment_depth_x") == 450 / 1200
    assert result.metrics[0]["value"] is not None
    derived_ctx = InputContext(**{**ctx.__dict__, "derived": {"transform": "downsample", "fraction": 0.5}})
    derived_out = tmp_path / "derived-output"
    derived = WPS.run(derived_ctx, derived_out)
    assert {"wps_track.npz", "wps_summary.json"} == set(derived.artifacts)
    assert not (derived_out / "wps_exact").exists()
    assert json.loads(derived.artifacts["wps_summary.json"])["exact_index"] is None
    assert {row["metric_id"] for row in derived.metrics} == {
        "mean_wps", "positive_bin_fraction", "qualifying_fragment_depth_x",
    }


def test_wps_depth_gate_requires_known_ten_x(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(wps.shutil, "disk_usage", lambda path: SimpleNamespace(free=20 * 1024**3))
    ctx = _summary_context(tmp_path, depth=None)
    assert isinstance(WPS.preflight(ctx), Skip)
    ctx = _summary_context(tmp_path)
    assert isinstance(WPS.preflight(ctx), Ready)
    paired = InputContext(root=tmp_path, source_id="a" * 64, input_id="b" * 64,
                          reference_id="hg38", reference_sha256="c" * 64,
                          input_dir=tmp_path, platform="illumina-paired-end", approx_depth_x=10.0)
    assert WPS.preflight(paired) == Skip(
        "platform", "long-fragment WPS needs single-molecule ONT aligned spans")


def test_wps_disk_gate_uses_summary_contig_lengths(tmp_path, monkeypatch) -> None:
    ctx = _summary_context(tmp_path)
    required = (4 * 1200 * 11 + 9) // 10 + wps.DISK_MARGIN_BYTES
    monkeypatch.setattr(wps.shutil, "disk_usage", lambda path: SimpleNamespace(free=required - 1))
    assert WPS.preflight(ctx).reason_code == "disk"
    monkeypatch.setattr(wps.shutil, "disk_usage", lambda path: SimpleNamespace(free=required))
    assert isinstance(WPS.preflight(ctx), Ready)


def test_wps_reference_gate_explains_unprefixed_contigs_and_direct_worker_error(tmp_path) -> None:
    ctx = _summary_context(tmp_path)
    summary = tmp_path / "summary" / "columns" / "meta.json"
    summary.write_text(json.dumps({"contigs": ["1"], "lengths": [1200]}))
    decision = WPS.preflight(ctx)
    assert isinstance(decision, Skip) and decision.reason_code == "reference"
    with pytest.raises(ValueError, match="chr1–chr22"):
        WPS.run(ctx, tmp_path)


@pytest.mark.parametrize("valid_fragments", [True, False])
def test_wps_registry_and_algorithm_output(tmp_path, monkeypatch, valid_fragments: bool) -> None:
    class Summary:
        contigs = ["chr1"]
        lengths = [1200]
        data = {"contig": np.zeros(3, dtype=np.int16),
                "start": np.asarray([100, 250, 500]),
                "end": np.asarray([220, 410, 680] if valid_fragments else [310, 460, 750])}

        def autosome_ids(self):
            return np.asarray([0], dtype=np.int16)

        def eligible(self, *, contigs):
            return np.asarray([True, True, True])

        def column(self, name):
            return self.data[name]

    monkeypatch.setattr(wps, "load_summary", lambda ctx: (Summary(), None))
    original_track = wps.chromosome_track
    monkeypatch.setattr(wps, "chromosome_track",
                        lambda *args, **kwargs: original_track(*args, chunk_bp=137, **kwargs))
    ctx = _summary_context(tmp_path)
    result = WPS.run(ctx, tmp_path)
    with np.load(io.BytesIO(result.artifacts["wps_track.npz"])) as track:
        expected, counts = chromosome_track(Summary.data["start"], Summary.data["end"], 1200)
        assert np.array_equal(track["chr1"], expected)
    panel = json.loads(result.artifacts["wps_summary.json"])
    assert panel["valid_centres"] == int(counts.sum())
    assert panel["qualifying_fragments"] == (3 if valid_fragments else 0)
    assert panel["qualifying_fragment_depth_x"] == (
        (120 + 160 + 180) / 1200 if valid_fragments else 0
    )
    assert (result.metrics[0]["value"] is not None) == valid_fragments
    assert (result.metrics[1]["value"] is not None) == valid_fragments
    index = json.loads(result.artifacts["wps_exact_index.json"])
    chunks = index["chunks"]
    assert chunks[0]["start"] == HALF
    assert chunks[-1]["stop"] == 1200 - HALF + 1
    exact = []
    for chunk in chunks:
        target = tmp_path / chunk["path"]
        assert hashlib.sha256(target.read_bytes()).hexdigest() == chunk["sha256"]
        with np.load(target) as data:
            assert len(data["score"]) == chunk["stop"] - chunk["start"]
            exact.extend(data["score"].tolist())
    source_starts = Summary.data["start"] if valid_fragments else np.asarray([], dtype=np.int64)
    source_ends = Summary.data["end"] if valid_fragments else np.asarray([], dtype=np.int64)
    assert exact == _naive(source_starts, source_ends, HALF, 1200 - HALF + 1)
    assert result.metrics[0]["n"] == len(exact)
    if valid_fragments:
        assert result.metrics[0]["value"] == sum(exact) / len(exact)
    else:
        assert result.metrics[0]["value"] is None
    assert any(algorithm.id == "wps" for algorithm in load_registry())
    assert load_library().entry("wps", "mean_wps") is not None


def test_wps_worker_exact_chunks_survive_store_publication(tmp_path, monkeypatch) -> None:
    class Summary:
        contigs = ["chr1"]
        lengths = [1200]

        def eligible(self, *, contigs):
            return np.ones(3, dtype=bool)

        def column(self, name):
            return {"contig": np.zeros(3, dtype=np.int16),
                    "start": np.asarray([100, 250, 500]),
                    "end": np.asarray([220, 410, 680])}[name]

    monkeypatch.setattr(wps, "load_summary", lambda ctx: (Summary(), None))
    original_track = wps.chromosome_track
    monkeypatch.setattr(wps, "chromosome_track",
                        lambda *args, **kwargs: original_track(*args, chunk_bp=137, **kwargs))
    ctx = _summary_context(tmp_path)
    request = tmp_path / "request.json"
    scratch = tmp_path / "scratch"
    request.write_text(json.dumps({"entry": "wps:WPS", "context": context_to_json(ctx),
                                   "out_dir": str(scratch)}))
    assert worker_main([str(request)]) == 0
    output = json.loads((scratch / "output.json").read_bytes())
    identity = "d" * 64
    envelope = ResultEnvelope.model_validate({
        "algorithm_id": "wps", "algorithm_version": WPS.version, "status": "ok",
        "provenance": provenance(tool="traceback_lab.test", parameters_sha256=identity,
                                 source_id=ctx.source_id, input_id=ctx.input_id,
                                 reference_id=ctx.reference_id),
        "metrics": output["metrics"], "panels": output["panels"],
    })
    store = LabStore(tmp_path / "store")
    store.ensure()
    store.publish_result(envelope, input_id=ctx.input_id, artifacts_dir=scratch / "artifacts")
    published = store.selected_artifacts(ctx.input_id, "wps", identity)
    assert published is not None
    assert not (scratch / "artifacts").exists()
    assert (published / "wps_track.npz").is_file()
    index = json.loads((published / "wps_exact_index.json").read_bytes())
    assert len(index["chunks"]) > 1
    assert index["chunks"][0]["start"] == HALF
    assert index["chunks"][-1]["stop"] == 1200 - HALF + 1
    for chunk in index["chunks"]:
        target = published / chunk["path"]
        assert hashlib.sha256(target.read_bytes()).hexdigest() == chunk["sha256"]
        with np.load(target) as data:
            assert data["score"].size == chunk["stop"] - chunk["start"]
