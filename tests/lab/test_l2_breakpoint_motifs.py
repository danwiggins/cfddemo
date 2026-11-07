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


def test_large_row_summary_keeps_peak_memory_bounded(
    tmp_path: Path, monkeypatch
) -> None:
    import tracemalloc

    from traceback_lab.read_summary import SCHEMA as SUMMARY_SCHEMA
    from traceback_lab.registry import InputContext

    n_reads = 10_000
    fasta = tmp_path / "large.fa"
    fasta.write_text(">chr1\n" + "A" * 100_010 + "\n")
    pysam.faidx(str(fasta))
    columns = tmp_path / "summary" / "columns"
    columns.mkdir(parents=True)
    (columns / "meta.json").write_text(
        json.dumps(
            {
                "schema": SUMMARY_SCHEMA,
                "rows": n_reads,
                "contigs": ["chr1"],
                "lengths": [100_010],
            }
        )
    )
    np.zeros(n_reads, dtype="<u2").tofile(columns / "flag.bin")
    np.zeros(n_reads, dtype="<i2").tofile(columns / "contig.bin")
    np.full(n_reads, 60, dtype="<u1").tofile(columns / "mapq.bin")
    starts = np.arange(n_reads, dtype="<i4") * 10
    starts.tofile(columns / "start.bin")
    (starts + 4).tofile(columns / "end.bin")
    ctx = InputContext(
        root=tmp_path,
        source_id="0" * 64,
        input_id="0" * 64,
        reference_id="synth",
        reference_sha256="0" * 64,
        input_dir=tmp_path,
        eligible_reads=n_reads,
        extras={
            "upstream": {"read-summary": str(tmp_path / "summary")},
            "reference_fasta": str(fasta),
        },
    )
    monkeypatch.setattr(module, "CHUNK_ROWS", 128)
    monkeypatch.setattr(module, "REFERENCE_WINDOW_BP", 4096)
    tracemalloc.start()
    try:
        output, panel = _result(ctx, tmp_path)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 32 * 1024 * 1024
    assert output.metrics[0]["n"] == 2 * n_reads and panel["near_ends"] == 0


def test_chunk_and_reference_windows_preserve_eligible_derived_ends(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fasta = tmp_path / "mixed.fa"
    sequence = list("A" * 200)
    sequence[15:17] = "CG"  # exercises the reference-window margin
    sequence[63:65] = "CG"
    fasta.write_text(">chr1\n" + "".join(sequence) + "\n")
    pysam.faidx(str(fasta))
    reads = [
        Read("eligible-1", "chr1", 0, "A" * 20),
        Read("duplicate", "chr1", 20, "A" * 20, flag=0x400),
        Read("eligible-2", "chr1", 40, "A" * 20),
        Read("low-mapq", "chr1", 60, "A" * 20, mapq=19),
        Read("eligible-3", "chr1", 80, "A" * 20),
    ]
    ctx = context_for(tmp_path, [("chr1", 200)], reads, fasta=fasta)
    summary = ReadSummary.open(tmp_path / "read-summary" / "columns")
    variants = (
        None,
        {"transform": "split", "half": "A"},
        {"transform": "downsample", "fraction": 0.5},
    )
    expected = {}
    for variant in variants:
        run_ctx = replace(ctx, derived=variant)
        expected[str(variant)] = _result(run_ctx, tmp_path)
        selected = summary.eligible()
        if variant:
            selected &= derived_mask(summary, variant)
        assert int(selected.sum()) > 0
        assert expected[str(variant)][0].metrics[0]["n"] == 2 * int(selected.sum())
    assert expected[str(None)][1]["near_ends"] > 0

    monkeypatch.setattr(module, "CHUNK_ROWS", 2)
    monkeypatch.setattr(module, "REFERENCE_WINDOW_BP", 16)
    for variant in variants:
        assert _result(replace(ctx, derived=variant), tmp_path) == expected[str(variant)]
