"""Synthetic contract tests for the exploratory frag-x-meth analysis."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from traceback_lab.algorithms.frag_x_meth import FRAG_X_METH
from traceback_lab.meth_calls import DTYPE, HAS_H, HAS_M, SCHEMA
from traceback_lab.read_summary import ReadSummary, derived_mask
from traceback_lab.registry import Ready

from .bam_fixtures import Read, context_for


def _row(
    read_row: int, pos: int, methylated: bool, *, channels: int = HAS_M, strand: int = 0
) -> tuple:
    p_m = 230 if methylated else 10
    return (read_row, 0, pos, strand, p_m, 0, 255 - p_m, channels)


def _store(
    base: Path,
    rows: list[tuple],
    *,
    chunk_size: int = 3,
    codes: tuple[str, ...] = ("C+m",),
) -> None:
    path = base / "mc" / "calls"
    path.mkdir(parents=True, exist_ok=True)
    for old_chunk in path.glob("calls-*.bin"):
        old_chunk.unlink()
    array = np.array(rows, dtype=DTYPE)
    chunks = []
    for index, offset in enumerate(range(0, len(array), chunk_size)):
        part = array[offset : offset + chunk_size]
        part.tofile(path / f"calls-{index:05d}.bin")
        chunks.append(len(part))
    (path / "meta.json").write_text(
        json.dumps(
            {
                "schema": SCHEMA,
                "chunks": chunks,
                "codes_sampled": list(codes),
                "records_with_mm": 10,
                "records_decoded": 10,
            }
        )
    )
    np.array([], dtype="<u4").tofile(path / "invalid_rows.bin")


def _fixture(base: Path, *, derived: dict | None = None):
    reads = [
        Read("short-100", "chr1", 100, "A" * 100),
        # Reverse and soft-clipped: query length 160, aligned span 150.
        Read("short-150", "chr1", 300, "A" * 160, flag=16, cigar=[(4, 10), (0, 150)]),
        Read("long-151", "chr1", 500, "A" * 151),
        Read("long-220", "chr1", 800, "A" * 220),
        Read("missing-m", "chr1", 1200, "A" * 100),
        Read("few-sites", "chr1", 1500, "A" * 100),
    ]
    ctx = context_for(
        base, [("chr1", 10_000)], reads, derived=derived, eligible_reads=1_000_000
    )
    # First molecule 1/4, second 4/8, third 3/4, fourth 4/4.
    rows = (
        [_row(0, 101 + i, i == 0) for i in range(4)]
        + [_row(1, 301 + i, i < 4, strand=1) for i in range(8)]
        + [_row(2, 501 + i, i < 3) for i in range(4)]
        + [_row(3, 801 + i, True) for i in range(4)]
        + [_row(4, 1201 + i, False, channels=HAS_H) for i in range(4)]
        + [_row(5, 1501 + i, True) for i in range(3)]
    )
    # A duplicate first site straddles a 3-row chunk boundary.  It must not
    # turn the first molecule's 1/4 into 2/5 or give that molecule more weight.
    rows.insert(3, rows[0])
    _store(base, rows)
    ctx.extras["upstream"]["meth-calls"] = str(base / "mc")
    return ctx


def _result(ctx, base: Path):
    output = FRAG_X_METH.run(ctx, base / "out")
    panel = json.loads(output.artifacts["size_methylation.json"])
    return output.metrics[0], panel


def test_hand_calculated_equal_molecule_weight_and_exact_boundaries(
    tmp_path: Path,
) -> None:
    ctx = _fixture(tmp_path)
    assert isinstance(FRAG_X_METH.preflight(ctx), Ready)
    metric, panel = _result(ctx, tmp_path)
    assert metric["value"] == pytest.approx(0.5)
    assert metric["n"] == 4 and (panel["short_n"], panel["long_n"]) == (2, 2)
    assert panel["n"] == 4
    assert panel["size_edges_bp"] == [1, 100, 151, 221, 301, 501, 1001]
    assert panel["counts"][1] == [0, 1, 1, 0]  # 100 and 150 bp
    assert panel["counts"][2] == [0, 0, 0, 2]  # 151 and 220 bp
    assert sum(map(sum, panel["counts"])) == 4
    assert (
        FRAG_X_METH.run(ctx, tmp_path / "out").artifacts["size_methylation.json"]
        == json.dumps(panel, separators=(",", ":")).encode()
    )


def test_empty_and_missing_channel_are_null_not_zero(tmp_path: Path) -> None:
    ctx = context_for(
        tmp_path,
        [("chr1", 1000)],
        [Read("one", "chr1", 100, "A" * 100)],
        eligible_reads=1_000_000,
    )
    _store(
        tmp_path,
        [_row(0, 101 + i, False, channels=HAS_H) for i in range(4)],
        codes=("C+h",),
    )
    ctx.extras["upstream"]["meth-calls"] = str(tmp_path / "mc")
    metric, panel = _result(ctx, tmp_path)
    assert (
        metric["value"] is None
        and metric["n"] == 0
        and panel["short_n"] == panel["long_n"] == 0
    )
    assert panel["n"] == 0 and sum(map(sum, panel["counts"])) == 0


def test_derived_masks_apply_to_the_join_for_splits_and_downsampling(
    tmp_path: Path,
) -> None:
    ctx = _fixture(tmp_path)
    full_metric, full_panel = _result(ctx, tmp_path)
    summary = ReadSummary.open(tmp_path / "read-summary" / "columns")
    for transform in (
        {"transform": "split", "half": "A"},
        {"transform": "split", "half": "B"},
        {"transform": "downsample", "fraction": 0.5},
    ):
        derived = _fixture_ctx(ctx, transform)
        metric, panel = _result(derived, tmp_path)
        selected = derived_mask(summary, transform)
        expected = int(selected[:4].sum())
        assert panel["n"] == expected
        assert metric["n"] == expected
        if panel["short_n"] == 0 or panel["long_n"] == 0:
            assert metric["value"] is None
        else:
            assert metric["value"] is not None
    a = _result(_fixture_ctx(ctx, {"transform": "split", "half": "A"}), tmp_path)[1][
        "n"
    ]
    b = _result(_fixture_ctx(ctx, {"transform": "split", "half": "B"}), tmp_path)[1][
        "n"
    ]
    assert a + b == full_panel["n"] == full_metric["n"]


def _fixture_ctx(ctx, derived: dict):
    from dataclasses import replace

    return replace(ctx, derived=derived)


def test_disordered_call_rows_are_refused(tmp_path: Path) -> None:
    ctx = _fixture(tmp_path)
    _store(tmp_path, [_row(1, 301, True), _row(0, 101, False)], chunk_size=1)
    with pytest.raises(ValueError, match="read_row values"):
        _result(ctx, tmp_path)


def test_streaming_peak_memory_is_bounded_by_chunk_not_total_calls(
    tmp_path: Path,
) -> None:
    import tracemalloc

    from traceback_lab.read_summary import SCHEMA as SUMMARY_SCHEMA
    from traceback_lab.registry import InputContext

    n_reads = 80_000
    summary_dir = tmp_path / "read-summary" / "columns"
    summary_dir.mkdir(parents=True)
    (summary_dir / "meta.json").write_text(
        json.dumps(
            {
                "schema": SUMMARY_SCHEMA,
                "rows": n_reads,
                "contigs": ["chr1"],
                "lengths": [20_000_000],
            }
        )
    )
    starts = np.arange(n_reads, dtype="<i4") * 200
    ends = starts + np.where(np.arange(n_reads) % 2 == 0, 100, 151).astype("<i4")
    starts.tofile(summary_dir / "start.bin")
    ends.tofile(summary_dir / "end.bin")
    rows = np.empty(n_reads * 4, dtype=DTYPE)
    rows["read_row"] = np.repeat(np.arange(n_reads, dtype="<u4"), 4)
    rows["contig"] = 0
    rows["ref_pos0"] = np.repeat(starts, 4) + np.tile(np.arange(4), n_reads)
    rows["strand"] = 0
    rows["p_m"] = np.tile([230, 230, 10, 10], n_reads)
    rows["p_h"] = 0
    rows["p_canonical"] = 255 - rows["p_m"]
    rows["channels"] = HAS_M
    path = tmp_path / "mc" / "calls"
    path.mkdir(parents=True)
    chunk_size = 1024
    chunks = []
    for index, offset in enumerate(range(0, len(rows), chunk_size)):
        part = rows[offset : offset + chunk_size]
        part.tofile(path / f"calls-{index:05d}.bin")
        chunks.append(len(part))
    (path / "meta.json").write_text(
        json.dumps({"schema": SCHEMA, "chunks": chunks, "records_with_mm": n_reads})
    )
    ctx = InputContext(
        root=tmp_path,
        source_id="0" * 64,
        input_id="0" * 64,
        reference_id="synth",
        reference_sha256="0" * 64,
        input_dir=tmp_path,
        eligible_reads=n_reads,
        extras={
            "upstream": {
                "read-summary": str(tmp_path / "read-summary"),
                "meth-calls": str(tmp_path / "mc"),
            }
        },
    )
    tracemalloc.start()
    try:
        metric, panel = _result(ctx, tmp_path)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    # The 320k-call store exceeds 4 MiB. Data is built before tracing;
    # this caps per-run Python/NumPy allocations, not mmap page cache or RSS.
    # The shared derived-input mask is absent here; chunks are 1,024 rows.
    assert n_reads * 4 * DTYPE.itemsize > 4 * 1024 * 1024
    assert peak < 3 * 1024 * 1024
    assert panel["n"] == n_reads and metric["value"] == pytest.approx(0.0)


def test_algorithm_output_passes_strict_result_envelope(tmp_path: Path) -> None:
    from traceback_lab.envelope import ResultEnvelope, provenance

    ctx = _fixture(tmp_path)
    output = FRAG_X_METH.run(ctx, tmp_path / "out")
    envelope = ResultEnvelope.model_validate(
        {
            "algorithm_id": FRAG_X_METH.id,
            "algorithm_version": FRAG_X_METH.version,
            "status": "ok",
            "provenance": provenance(
                tool="traceback_lab.frag_x_meth",
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
    assert envelope.metrics[0].n == 4
    assert envelope.to_bytes()


def test_no_calls_and_one_sided_delta_denominator(tmp_path: Path) -> None:
    ctx = context_for(
        tmp_path,
        [("chr1", 1000)],
        [Read("short", "chr1", 100, "A" * 100)],
        eligible_reads=1_000_000,
    )
    ctx.extras["upstream"]["meth-calls"] = str(tmp_path / "mc")
    _store(tmp_path, [])
    metric, panel = _result(ctx, tmp_path)
    assert metric["value"] is None and metric["n"] == panel["n"] == 0
    assert panel["short_mean"] is panel["long_mean"] is None
    _store(tmp_path, [_row(0, 101 + i, i < 2) for i in range(4)])
    metric, panel = _result(ctx, tmp_path)
    assert panel["short_n"] == 1 and panel["long_n"] == 0
    assert panel["short_mean"] == pytest.approx(0.5)
    assert panel["long_mean"] is None and metric["value"] is None and metric["n"] == 1


def test_absent_mm_tags_skip_before_analysis(tmp_path: Path) -> None:
    from traceback_lab.registry import Skip

    ctx = _fixture(tmp_path)
    meta_path = tmp_path / "mc" / "calls" / "meta.json"
    meta = json.loads(meta_path.read_text())
    meta["records_with_mm"] = 0
    meta_path.write_text(json.dumps(meta))
    decision = FRAG_X_METH.preflight(ctx)
    assert isinstance(decision, Skip) and decision.reason_code == "missing_tags"


def test_real_d28_producer_to_join(tmp_path: Path) -> None:
    from traceback_lab.algorithms.meth_calls import METH_CALLS

    reads = [
        Read(
            "short",
            "chr1",
            100,
            "CG" * 4 + "A" * 92,
            mm="C+m?,0,0,0,0;",
            ml=[230, 10, 10, 10],
        ),
        Read(
            "long", "chr1", 300, "CG" * 4 + "A" * 143, mm="C+m?,0,0,0,0;", ml=[230] * 4
        ),
    ]
    ctx = context_for(tmp_path, [("chr1", 1000)], reads, eligible_reads=1_000_000)
    METH_CALLS.run(ctx, tmp_path / "mc")
    ctx.extras["upstream"]["meth-calls"] = str(tmp_path / "mc")
    metric, panel = _result(ctx, tmp_path)
    assert (panel["n"], panel["short_n"], panel["long_n"]) == (2, 1, 1)
    assert metric["value"] == pytest.approx(0.75)


def test_conflicting_duplicate_call_probabilities_are_refused(tmp_path: Path) -> None:
    ctx = _fixture(tmp_path)
    rows = [_row(0, 101, True), _row(0, 101, False)]
    _store(tmp_path, rows, chunk_size=1)
    with pytest.raises(ValueError, match="conflicting duplicate"):
        _result(ctx, tmp_path)


def test_out_of_bounds_read_row_is_refused(tmp_path: Path) -> None:
    ctx = _fixture(tmp_path)
    _store(tmp_path, [_row(99, 101 + i, True) for i in range(4)], chunk_size=2)
    with pytest.raises(ValueError, match="outside read-summary"):
        _result(ctx, tmp_path)


def test_conflicting_missing_m_channel_across_chunks_is_refused(tmp_path: Path) -> None:
    ctx = _fixture(tmp_path)
    rows = [
        _row(0, 101, True, channels=HAS_M),
        _row(0, 102, False),
        _row(0, 103, False),
        _row(0, 101, False, channels=HAS_H),
    ]
    _store(tmp_path, rows, chunk_size=3)
    with pytest.raises(ValueError, match="conflicting duplicate"):
        _result(ctx, tmp_path)


def test_nonpositive_aligned_span_fails_for_qualifying_read(tmp_path: Path) -> None:
    ctx = _fixture(tmp_path)
    ends_path = tmp_path / "read-summary" / "columns" / "end.bin"
    ends = np.memmap(ends_path, dtype="<i4", mode="r+")
    ends[0] = 100  # read 0 also starts at 100
    ends.flush()
    del ends
    with pytest.raises(ValueError, match="nonpositive aligned span"):
        _result(ctx, tmp_path)


def test_panel_includes_molecules_outside_scalar_size_bands(tmp_path: Path) -> None:
    reads = [
        Read("short", "chr1", 100, "A" * 100),
        Read("long", "chr1", 300, "A" * 151),
        Read("other", "chr1", 600, "A" * 260),
    ]
    ctx = context_for(tmp_path, [("chr1", 2000)], reads, eligible_reads=1_000_000)
    rows = (
        [_row(0, 101 + i, False) for i in range(4)]
        + [_row(1, 301 + i, True) for i in range(4)]
        + [_row(2, 601 + i, i < 2) for i in range(4)]
    )
    _store(tmp_path, rows, chunk_size=3)
    ctx.extras["upstream"]["meth-calls"] = str(tmp_path / "mc")
    scalar, panel = _result(ctx, tmp_path)
    assert panel["n"] == 3
    assert panel["counts"][3][2] == 1  # 260 bp, 0.5 fraction
    assert scalar["n"] == panel["short_n"] + panel["long_n"] == 2
    assert scalar["value"] == pytest.approx(1.0)


def test_complete_read_batches_preserve_duplicates_and_derived_results(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from traceback_lab.algorithms import frag_x_meth as module

    ctx = _fixture(tmp_path)
    rows = np.concatenate(list(module.load_calls(ctx)[0].chunks())).tolist()
    # Keep a repeated site nonadjacent within one read. A processing batch
    # boundary must not turn its four distinct sites into five calls.
    rows.insert(4, rows[0])
    variants = (
        None,
        {"transform": "split", "half": "A"},
        {"transform": "downsample", "fraction": 0.5},
    )
    expected = {}
    for variant in variants:
        derived_ctx = _fixture_ctx(ctx, variant) if variant else ctx
        expected[str(variant)] = _result(derived_ctx, tmp_path)

    monkeypatch.setattr(module, "PROCESS_CALLS", 5)
    for chunk_size in (2, 17):
        _store(tmp_path, rows, chunk_size=chunk_size)
        for variant in variants:
            derived_ctx = _fixture_ctx(ctx, variant) if variant else ctx
            assert _result(derived_ctx, tmp_path) == expected[str(variant)]
