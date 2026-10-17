"""Synthetic QC boundary cases with independently calculated expectations."""

from pathlib import Path

import pysam
import pytest

from traceback_lab.algorithms.bam_qc import BAM_QC, covered_fraction
from traceback_lab.read_summary import ReadSummary, write_summary
from traceback_lab.registry import InputContext


def _record(out, name, tid, start, length, *, quality=30, qs=None):
    record = pysam.AlignedSegment(out.header)
    record.query_name = name
    record.query_sequence = "A" * length
    record.reference_id = tid
    record.reference_start = start
    record.mapping_quality = 60
    record.cigarstring = f"{length}M"
    record.query_qualities = None if quality is None else [quality] * length
    if qs is not None:
        record.set_tag("qs", qs)
    out.write(record)


def _summary(path: Path) -> ReadSummary:
    pysam.index(str(path))
    write_summary(path, path.parent / "columns")
    return ReadSummary.open(path.parent / "columns")


def _metrics(root: Path):
    ctx = InputContext(
        root=root, source_id="a" * 64, input_id="b" * 64,
        reference_id="toy", reference_sha256="c" * 64, input_dir=root,
        extras={"upstream": {"read-summary": str(root)}},
    )
    return {m["metric_id"]: m for m in BAM_QC.run(ctx, root).metrics}


def test_qs_overrides_base_qualities_and_missing_quality_is_not_zero(tmp_path: Path) -> None:
    path = tmp_path / "aligned.bam"
    header = {"HD": {"SO": "coordinate"}, "SQ": [{"SN": "chr1", "LN": 1000}]}
    with pysam.AlignmentFile(str(path), "wb", header=header) as out:
        _record(out, "synthetic-tagged", 0, 0, 10, quality=40, qs=12.5)
        _record(out, "synthetic-fallback", 0, 20, 100, quality=27)
        _record(out, "synthetic-missing", 0, 140, 10, quality=None)
    _summary(path)
    metric = _metrics(tmp_path)["mean_read_q"]
    assert metric["n"] == 2
    assert metric["value"] == pytest.approx(19.75)


def test_empty_input_has_no_quality_or_mapping_estimate(tmp_path: Path) -> None:
    path = tmp_path / "aligned.bam"
    with pysam.AlignmentFile(str(path), "wb", header={"SQ": [{"SN": "chr1", "LN": 1000}]}):
        pass
    _summary(path)
    metrics = _metrics(tmp_path)
    assert metrics["total_reads"]["value"] == 0
    assert metrics["total_reads"]["n"] == 0
    for name in ("mapped_fraction", "mapq_ge20_fraction", "supplementary_fraction", "mean_read_q"):
        assert metrics[name]["value"] is None
        assert metrics[name]["n"] == 0
    assert metrics["genome_ge1x_fraction"]["value"] == 0
    assert metrics["genome_ge1x_fraction"]["n"] == 1000


def test_coverage_sampling_continues_across_autosomes_and_clips_last_window(tmp_path: Path) -> None:
    # chr1 contributes 99 windows; chr2's second window is global index 100.
    # Thus sampled windows are chr1 [0,1M) and chr2 [1M,1M+50).
    # chrX comes first so including it would shift the sampling phase.
    path = tmp_path / "aligned.bam"
    header = {"HD": {"SO": "coordinate"}, "SQ": [
        {"SN": "chrX", "LN": 1000},
        {"SN": "chr1", "LN": 99_000_000}, {"SN": "chr2", "LN": 1_000_050},
    ]}
    with pysam.AlignmentFile(str(path), "wb", header=header) as out:
        _record(out, "synthetic-nonautosome", 0, 0, 100)
        _record(out, "synthetic-crossing", 1, 999_990, 30)  # only ten bases in sampled window
        _record(out, "synthetic-unsampled-1", 1, 2_000_000, 50)
        _record(out, "synthetic-unsampled-2", 2, 0, 50)
        _record(out, "synthetic-last-a", 2, 1_000_000, 20)
        _record(out, "synthetic-last-b", 2, 1_000_040, 10)
    summary = _summary(path)
    fraction, denominator = covered_fraction(path, summary, None)
    assert denominator == 1_000_050
    assert fraction == pytest.approx(40 / 1_000_050)
