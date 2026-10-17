"""Exact WPS geometry and bounded genome-track aggregation."""

from __future__ import annotations

import io
import json

import numpy as np
import pytest

from traceback_lab.algorithms import wps
from traceback_lab.algorithms.wps import HALF, WINDOW, WPS, chromosome_track, score_centres
from traceback_lab.interpretation.engine import load_library
from traceback_lab.registry import InputContext, Ready, Skip, load_registry


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
    mean, counts = chromosome_track(starts, ends, 1200, bin_bp=100, chunk_bp=137)
    other, other_counts = chromosome_track(starts, ends, 1200, bin_bp=100, chunk_bp=400)
    assert np.array_equal(mean, other)
    assert np.array_equal(counts, other_counts)
    for index in range(len(mean)):
        positions = np.arange(HALF, 1200 - HALF + 1)
        selected = positions // 100 == index
        assert counts[index] == int(selected.sum())
        if selected.any():
            assert mean[index] == np.float32(expected[selected].mean())


def test_wps_depth_gate_requires_known_ten_x(tmp_path) -> None:
    ctx = InputContext(root=tmp_path, source_id="a" * 64, input_id="b" * 64,
                       reference_id="hg38", reference_sha256="c" * 64,
                       input_dir=tmp_path)
    assert isinstance(WPS.preflight(ctx), Skip)
    ctx = InputContext(root=tmp_path, source_id="a" * 64, input_id="b" * 64,
                       reference_id="hg38", reference_sha256="c" * 64,
                       input_dir=tmp_path, approx_depth_x=10.0)
    assert isinstance(WPS.preflight(ctx), Ready)
    paired = InputContext(root=tmp_path, source_id="a" * 64, input_id="b" * 64,
                          reference_id="hg38", reference_sha256="c" * 64,
                          input_dir=tmp_path, platform="illumina-paired-end", approx_depth_x=10.0)
    assert WPS.preflight(paired) == Skip(
        "unsupported_platform", "long-fragment WPS needs single-molecule ONT aligned spans")


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
    ctx = InputContext(root=tmp_path, source_id="a" * 64, input_id="b" * 64,
                       reference_id="hg38", reference_sha256="c" * 64,
                       input_dir=tmp_path, approx_depth_x=10.0)
    result = WPS.run(ctx, tmp_path)
    with np.load(io.BytesIO(result.artifacts["wps_track.npz"])) as track:
        expected, counts = chromosome_track(Summary.data["start"], Summary.data["end"], 1200)
        assert np.array_equal(track["chr1"], expected)
    panel = json.loads(result.artifacts["wps_summary.json"])
    assert panel["valid_centres"] == int(counts.sum())
    assert panel["qualifying_fragments"] == (3 if valid_fragments else 0)
    assert (result.metrics[0]["value"] is not None) == valid_fragments
    assert (result.metrics[1]["value"] is not None) == valid_fragments
    assert any(algorithm.id == "wps" for algorithm in load_registry())
    assert load_library().entry("wps", "mean_wps") is not None
