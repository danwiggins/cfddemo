"""D33 read summary, D21 span parity, and the S3 algorithms on synthetic data."""

from __future__ import annotations

import json
import random
from pathlib import Path

import numpy as np
import pysam
import pytest

from traceback_lab.algorithms.bam_qc import _union, covered_fraction
from traceback_lab.algorithms.fragment_size import periodicity_power, summarize
from traceback_lab.read_summary import (
    ReadSummary,
    bin_counts,
    depth_stamp,
    derived_mask,
    downsample_mask,
    h64,
    span_counts,
    split_mask,
    write_summary,
)
from traceback_runner.contracts import ReferenceContig, RegisteredReference
from traceback_runner.local_authority import LOCAL_BIN_EDGES, local_fragment_policy
from traceback_runner.measurement import scan_aligned_reference_spans

CONTIGS = [("chr1", 200_000), ("chr2", 150_000), ("chrX", 100_000), ("chrM", 16_569), ("chrUn_x", 5_000)]


def _write_aligned(path: Path, *, n: int = 3000, seed: int = 1) -> Path:
    """A coordinate-sorted aligned BAM with every exclusion class represented."""

    rng = random.Random(seed)
    header = {"HD": {"VN": "1.6", "SO": "coordinate"}, "SQ": [{"SN": name, "LN": length} for name, length in CONTIGS]}
    records = []
    for serial in range(n):
        tid = rng.choices(range(len(CONTIGS)), weights=(40, 30, 20, 5, 5))[0]
        span = max(40, int(rng.gauss(167, 25))) if rng.random() > 0.05 else rng.randint(300, 1500)
        start = rng.randrange(0, CONTIGS[tid][1] - span)
        flag = rng.choice([0, 0, 0, 0, 16, 16, 16, 256, 2048, 1024, 512])
        mapq = rng.choice([60, 60, 60, 60, 5, 0])
        soft = rng.choice([0, 0, 0, 12])
        records.append((tid, start, span, flag, mapq, soft, f"read-{seed}-{serial}"))
    records.append((-1, -1, 0, 4, 0, 0, "unmapped-1"))
    records.sort(key=lambda r: (r[0] if r[0] >= 0 else 1 << 30, r[1]))
    unsorted = path.with_suffix(".tmp.bam")
    with pysam.AlignmentFile(str(unsorted), "wb", header=header) as out:
        for tid, start, span, flag, mapq, soft, name in records:
            record = pysam.AlignedSegment(out.header)
            record.query_name = name
            if tid < 0:
                record.flag = 4
                record.query_sequence = "ACGT" * 40
                record.reference_id = -1
                record.reference_start = -1
            else:
                record.flag = flag
                record.reference_id = tid
                record.reference_start = start
                record.mapping_quality = mapq
                record.query_sequence = "A" * (span + soft)
                record.cigartuples = ([(4, soft)] if soft else []) + [(0, span)]
                record.set_tag("MN", span + soft)
            record.query_qualities = pysam.qualitystring_to_array("5" * len(record.query_sequence))
            out.write(record)
    pysam.sort("-o", str(path), str(unsorted))
    pysam.index(str(path))
    unsorted.unlink()
    return path


@pytest.fixture(scope="module")
def aligned(tmp_path_factory) -> tuple[Path, ReadSummary]:
    base = tmp_path_factory.mktemp("summary")
    bam = _write_aligned(base / "aligned.bam")
    write_summary(bam, base / "columns")
    return bam, ReadSummary.open(base / "columns")


def test_one_row_per_record_and_hashes_not_names(aligned) -> None:
    bam, summary = aligned
    assert summary.rows == int(str(pysam.view("-c", str(bam))).strip())
    assert summary.contigs == [name for name, _ in CONTIGS]
    text = b"".join(path.read_bytes() for path in summary.directory.iterdir())
    assert b"read-1-" not in text and b"unmapped" not in text
    assert h64(b"ds-v1", "x") == int.from_bytes(__import__("hashlib").blake2b(b"ds-v1x").digest()[:8], "big")


def test_lab_span_counter_matches_scan_records(aligned) -> None:
    bam, summary = aligned
    reference = RegisteredReference(
        reference_id="toy",
        assembly="toy",
        asset_sha256="a" * 64,
        contigs=tuple(ReferenceContig(name=name, length=length, md5="0" * 32) for name, length in CONTIGS),
    )
    policy = local_fragment_policy(reference)
    scan = scan_aligned_reference_spans(bam, policy)
    signed = [entry.count for entry in scan.histogram]
    lab = bin_counts(span_counts(summary), LOCAL_BIN_EDGES)
    assert lab == signed
    assert sum(lab) == scan.eligible_alignments > 0


def test_eligibility_excludes_every_d21_class(aligned) -> None:
    _, summary = aligned
    eligible = summary.eligible()
    flag = summary.column("flag")
    assert not np.any(eligible & ((flag & 0xF04) != 0))
    assert not np.any(eligible & (summary.column("mapq") < 20))
    names = np.array(summary.contigs)[summary.column("contig")[eligible]]
    assert set(names) <= {"chr1", "chr2", "chrX"}


def test_downsample_masks_nest_and_validate(aligned) -> None:
    _, summary = aligned
    h = summary.column("h_ds")
    masks = {f: downsample_mask(h, f) for f in (0.5, 0.25, 0.125, 0.1, 0.05)}
    assert np.all(masks[0.5] | ~masks[0.25])  # 0.25 inside 0.5
    assert np.all(masks[0.125] <= masks[0.25]) and np.all(masks[0.05] <= masks[0.1])
    assert abs(masks[0.5].mean() - 0.5) < 0.05
    for bad in (0.0, 1.0, -0.2, 1.5):
        with pytest.raises(ValueError):
            downsample_mask(h, bad)


def test_split_halves_are_disjoint_cover_and_differ_from_half_downsample(aligned) -> None:
    _, summary = aligned
    a = split_mask(summary.column("h_split"), "A")
    b = split_mask(summary.column("h_split"), "B")
    assert not np.any(a & b) and np.all(a | b)
    half = downsample_mask(summary.column("h_ds"), 0.5)
    overlap = (a & half).sum() / a.sum()
    assert 0.4 < overlap < 0.6
    assert derived_mask(summary, {"transform": "split", "half": "B"}).sum() == b.sum()
    assert derived_mask(summary, None) is None


def test_depth_stamp_uses_eligible_reads(aligned) -> None:
    _, summary = aligned
    stamp = depth_stamp(summary)
    assert stamp["eligible_reads"] == int(summary.eligible().sum())
    assert stamp["index_mapped_upper_bound"] > stamp["eligible_reads"]
    spans = summary.spans()[summary.eligible()]
    assert stamp["approx_depth_x"] == pytest.approx(spans.sum() / summary.canonical_length(), rel=1e-4)


def test_fragment_summary_on_a_planted_mode() -> None:
    rng = np.random.default_rng(0)
    spans = np.concatenate([rng.normal(167, 10, 20000).astype(int), rng.normal(334, 15, 3000).astype(int)])
    values, counts = np.unique(spans, return_counts=True)
    stats = summarize(dict(zip(values.tolist(), counts.tolist())))
    assert 160 <= stats["mode_bp"] <= 174
    assert 320 <= stats["peak_di_bp"] <= 348
    assert stats["short_fraction_lt150"] == pytest.approx(((spans >= 50) & (spans < 150)).sum() / ((spans >= 50) & (spans <= 1000)).sum())


def test_periodicity_detects_a_10bp_rhythm() -> None:
    sizes = np.arange(0, 1001)
    smooth = np.exp(-((sizes - 160) ** 2) / (2 * 40.0**2)) * 1000
    rhythmic = smooth * (1 + 0.4 * np.sin(2 * np.pi * sizes / 10.4))
    assert periodicity_power(rhythmic.astype(int)) > 5 * periodicity_power(smooth.astype(int))
    assert periodicity_power(np.zeros(1001)) is None


def test_interval_union() -> None:
    assert _union(np.array([0, 5, 20]), np.array([10, 12, 30])) == 22
    assert _union(np.array([], dtype=int), np.array([], dtype=int)) == 0


def test_covered_fraction_counts_sampled_windows(aligned) -> None:
    _, summary = aligned
    value = covered_fraction(summary, summary.primary() & ((summary.column("flag") & 4) == 0))
    assert 0 < value <= 1


def test_corrupt_bam_is_an_error_not_an_empty_summary(tmp_path: Path) -> None:
    bad = tmp_path / "bad.bam"
    bad.write_bytes(b"not a bam")
    with pytest.raises((OSError, ValueError)):
        write_summary(bad, tmp_path / "columns")


def test_lab_run_computes_the_slice_algorithms(lab_world, capsys, shared_mmi) -> None:
    from traceback_runner import cli as signed_cli

    code = signed_cli.main(
        ["lab", "run", str(lab_world["chunks"]), "--root", str(lab_world["root"]), "--mmi", str(shared_mmi), "--json"]
    )
    payload = json.loads(capsys.readouterr().out)
    assert code == 0, payload
    steps = {step["algorithm_id"]: step["status"] for step in payload["data"]["steps"]}
    assert steps == {"read-summary": "ok", "bam-qc": "ok", "depth": "ok", "fragment-size": "ok"}
    depth = payload["data"]["depth"]
    assert depth["eligible_reads"] > 1500
    from traceback_lab.store import LabStore

    store = LabStore(lab_world["root"])
    input_id = payload["data"]["sample"]["input_id"]
    (frag_dir,) = (store.results_dir / input_id / "fragment-size").iterdir()
    envelope = store.read_result(input_id, "fragment-size", frag_dir.name)
    values = {metric.metric_id: metric.value for metric in envelope.metrics}
    assert 150 <= values["mode_bp"] <= 185
    assert envelope.depth is not None and envelope.depth.eligible_reads == depth["eligible_reads"]
    hist = json.loads((store.selected_attempt(input_id, "fragment-size", frag_dir.name) / "artifacts" / "size_hist.json").read_text())
    assert len(hist["count"]) == 1000
