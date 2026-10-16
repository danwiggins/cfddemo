"""Synthetic contract tests for exploratory aligned-span end CpG motifs."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import numpy as np
import pysam
import pytest

from traceback_lab.algorithms import breakpoint_motifs as module
from traceback_lab.algorithms.breakpoint_motifs import BREAKPOINT_MOTIFS, _nearest_cpg
from traceback_lab.read_summary import ReadSummary, derived_mask
from traceback_lab.registry import Ready, Skip

from .bam_fixtures import Read, context_for


def _fixture(base: Path):
    sequence = list("A" * 100)
    sequence[10:12] = "CG"
    sequence[30:32] = "CG"
    fasta = base / "ref.fa"
    fasta.write_text(">chr1\n" + "".join(sequence) + "\n")
    pysam.faidx(str(fasta))
    reads = [
        Read("edge-left", "chr1", 0, "A" * 14),
        Read("mid", "chr1", 40, "A" * 20, flag=16, cigar=[(4, 5), (0, 15)]),
        Read("edge-right", "chr1", 97, "A" * 3),
    ]
    return context_for(
        base, [("chr1", 100)], reads, fasta=fasta, eligible_reads=1_000_000
    )


def _result(ctx, base: Path):
    output = BREAKPOINT_MOTIFS.run(ctx, base / "out")
    return output, json.loads(output.artifacts["cpg_end_motifs.json"])


def test_hand_calculated_ends_motifs_and_reference_edges(tmp_path: Path) -> None:
    ctx = _fixture(tmp_path)
    assert isinstance(BREAKPOINT_MOTIFS.preflight(ctx), Ready)
    output, panel = _result(ctx, tmp_path)
    metric = output.metrics[0]
    assert metric == {
        "metric_id": "cpg_near_end_fraction",
        "value": 0.5,
        "unit": "fraction",
        "n": 6,
    }
    assert (panel["near_ends"], panel["far_ends"]) == (3, 3)
    assert (panel["invalid_context_near"], panel["invalid_context_far"]) == (0, 1)
    near = dict(zip(panel["motif"], panel["near_count"]))
    far = dict(zip(panel["motif"], panel["far_count"]))
    assert near["AAAA"] == 2 and near["TTCG"] == 1
    assert far["TTTT"] == 2
    assert sum(panel["near_count"]) + panel["invalid_context_near"] == 3
    assert sum(panel["far_count"]) + panel["invalid_context_far"] == 3
    assert panel["methylation_link"]["status"] == "unavailable"
    assert BREAKPOINT_MOTIFS.run(ctx, tmp_path / "out").artifacts == output.artifacts


def test_nearest_cpg_inclusive_radius_and_lower_coordinate_tie() -> None:
    sequence = list("A" * 64)
    sequence[10:12] = "CG"
    sequence[30:32] = "CG"
    encoded = module._encode("".join(sequence))
    assert _nearest_cpg(encoded, np.array([0, 20, 40, 41, 63]), 0).tolist() == [
        10,
        10,
        30,
        -1,
        -1,
    ]


def test_fetch_windows_include_nearby_cpg_across_boundary_and_are_bounded(
    tmp_path: Path, monkeypatch
) -> None:
    ctx = _fixture(tmp_path)
    real = pysam.FastaFile
    requests = []

    class ObservedFasta:
        def __init__(self, path):
            self.reader = real(path)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.reader.close()

        def get_reference_length(self, name):
            return self.reader.get_reference_length(name)

        def fetch(self, name, start, end):
            requests.append((start, end))
            return self.reader.fetch(name, start, end)

    monkeypatch.setattr(module, "REFERENCE_WINDOW_BP", 16)
    monkeypatch.setattr(module, "CHUNK_ROWS", 2)
    monkeypatch.setattr(pysam, "FastaFile", ObservedFasta)
    output, _ = _result(ctx, tmp_path)
    assert output.metrics[0]["value"] == 0.5
    assert (
        requests
        and max(end - start for start, end in requests)
        <= 16 + 2 * (module.CPG_RADIUS_BP + module.K) + 1
    )


def test_missing_reference_and_empty_input(tmp_path: Path) -> None:
    ctx = _fixture(tmp_path)
    missing = replace(ctx, extras={"upstream": ctx.extras["upstream"]})
    decision = BREAKPOINT_MOTIFS.preflight(missing)
    assert isinstance(decision, Skip) and decision.reason_code == "reference"

    empty = tmp_path / "empty"
    empty.mkdir()
    sequence = empty / "ref.fa"
    sequence.write_text(">chr1\n" + "A" * 100 + "\n")
    pysam.faidx(str(sequence))
    zero = context_for(
        empty, [("chr1", 100)], [], fasta=sequence, eligible_reads=1_000_000
    )
    output, panel = _result(zero, empty)
    assert output.metrics[0]["value"] is None and output.metrics[0]["n"] == 0
    assert panel["near_ends"] == panel["far_ends"] == 0


def test_downsample_and_split_masks_match_summary_rows(tmp_path: Path) -> None:
    ctx = _fixture(tmp_path)
    summary = ReadSummary.open(tmp_path / "read-summary" / "columns")
    for derived in (
        {"transform": "downsample", "fraction": 0.5},
        {"transform": "split", "half": "A"},
        {"transform": "split", "half": "B"},
    ):
        output, panel = _result(replace(ctx, derived=derived), tmp_path)
        expected = int(derived_mask(summary, derived).sum()) * 2
        assert output.metrics[0]["n"] == expected
        assert panel["near_ends"] + panel["far_ends"] == expected
    a = _result(replace(ctx, derived={"transform": "split", "half": "A"}), tmp_path)[
        0
    ].metrics[0]["n"]
    b = _result(replace(ctx, derived={"transform": "split", "half": "B"}), tmp_path)[
        0
    ].metrics[0]["n"]
    assert a + b == 6


def test_strict_envelope_accepts_primary_only_without_meth_calls(
    tmp_path: Path,
) -> None:
    from traceback_lab.envelope import ResultEnvelope, provenance

    ctx = _fixture(tmp_path)
    output, _ = _result(ctx, tmp_path)
    envelope = ResultEnvelope.model_validate(
        {
            "algorithm_id": BREAKPOINT_MOTIFS.id,
            "algorithm_version": BREAKPOINT_MOTIFS.version,
            "status": "ok",
            "provenance": provenance(
                tool="traceback_lab.breakpoint_motifs",
                parameters_sha256="a" * 64,
                source_id="b" * 64,
                input_id="c" * 64,
                reference_id="synth",
                timestamp_utc="2026-10-16T00:00:00Z",
            ),
            "depth": {"eligible_reads": 1_000_000, "approx_depth_x": 0.1},
            "metrics": output.metrics,
            "panels": output.panels,
        }
    )
    assert envelope.metrics[0].n == 6
    assert BREAKPOINT_MOTIFS.upstream == ("read-summary",)


def test_reference_length_mismatch_fails_instead_of_using_wrong_coordinates(
    tmp_path: Path,
) -> None:
    ctx = _fixture(tmp_path)
    meta_path = tmp_path / "read-summary" / "columns" / "meta.json"
    meta = json.loads(meta_path.read_text())
    meta["lengths"] = [101]
    meta_path.write_text(json.dumps(meta))
    with pytest.raises(ValueError, match="differs from pinned reference"):
        _result(ctx, tmp_path)


def test_no_cpg_with_valid_ends_is_zero_not_null(tmp_path: Path) -> None:
    fasta = tmp_path / "no-cpg.fa"
    fasta.write_text(">chr1\n" + "A" * 100 + "\n")
    pysam.faidx(str(fasta))
    ctx = context_for(
        tmp_path,
        [("chr1", 100)],
        [Read("molecule", "chr1", 20, "A" * 10)],
        fasta=fasta,
        eligible_reads=1_000_000,
    )
    output, panel = _result(ctx, tmp_path)
    assert output.metrics[0]["value"] == 0.0 and output.metrics[0]["n"] == 2
    assert panel["near_ends"] == 0 and panel["far_ends"] == 2
