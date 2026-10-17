"""L1.3: the D28 meth-calls store and its streaming consumers (acceptance #28)."""

from __future__ import annotations

import gzip
import hashlib
import json
import subprocess
import sys
import textwrap
from pathlib import Path

import numpy as np
import pytest

from traceback_lab.algorithms.global_5hmc import GLOBAL_5HMC
from traceback_lab.algorithms.global_5mc import GLOBAL_5MC
from traceback_lab.algorithms.hypometh_reads import HYPOMETH_READS, classify_reads
from traceback_lab.algorithms.meth_calls import METH_CALLS
from traceback_lab.algorithms.pmd_score import ASSET_NAME, PmdScore, inside, read_intervals
from traceback_lab.meth_calls import CHUNK_CALLS, DTYPE, HAS_H, HAS_M, SCHEMA, MethCalls, argmax_state
from traceback_lab.read_summary import ReadSummary
from traceback_lab.registry import Ready, Skip

from .bam_fixtures import Read, context_for

CONTIGS = [("chr1", 10_000)]


def _decode_reads() -> list[Read]:
    return [
        # forward: CpGs at query 1 and 6
        Read("fwd", "chr1", 100, "ACGTTACGAA", mm="C+m?,0,0;", ml=[250, 10]),
        # reverse: original read AACGTTCGAA (Cs at 2 and 6) stored as TTCGAACGTT
        Read("rev", "chr1", 200, "TTCGAACGTT", flag=16, mm="C+m?,0,0;", ml=[200, 30]),
        # skip mode ".": the skipped first CpG C is an implicit canonical call
        Read("dot", "chr1", 300, "ACGTACGTAA", mm="C+m.,1;", ml=[220]),
        # skip mode "?": the skipped C is missing, not canonical
        Read("qm", "chr1", 400, "ACGTACGTAA", mm="C+m?,1;", ml=[220]),
        # the CpG sits in an insertion: dropped, counted
        Read("ins", "chr1", 500, "AACGAAAAAA", cigar=[(0, 2), (1, 2), (0, 6)], mm="C+m?,0;", ml=[100]),
        # the CpG sits in a soft clip: dropped, counted
        Read("clip", "chr1", 600, "CGAAAAAAAA", cigar=[(4, 2), (0, 8)], mm="C+m?,0;", ml=[100]),
        # a primary whose MN mismatches: invalid, no calls
        Read("badmn", "chr1", 700, "ACGAAAAAAA", mm="C+m?,0;", ml=[100], mn=55),
        # a hard-clipped supplementary record: not D21-eligible, never decoded
        Read("supp", "chr1", 800, "ACGAAAAAAA", flag=2048, cigar=[(5, 20), (0, 10)], mm="C+m?,0;", ml=[100], mn=30),
        # 5mC + 5hmC on one site share one row
        Read("both", "chr1", 900, "ACGAAAAAAA", mm="C+h?,0;C+m?,0;", ml=[150, 60]),
    ]


def _run_meth_calls(base: Path) -> tuple[MethCalls, dict]:
    ctx = context_for(base, CONTIGS, _decode_reads())
    assert isinstance(METH_CALLS.preflight(ctx), Ready)
    out = base / "meth-calls"
    output = METH_CALLS.run(ctx, out)
    return MethCalls.open(out / "calls"), {m["metric_id"]: m["value"] for m in output.metrics}


def test_meth_calls_site_rows_and_counted_drops(tmp_path: Path) -> None:
    store, metrics = _run_meth_calls(tmp_path)
    rows = np.concatenate(list(store.chunks()))
    got = sorted(
        (int(r["ref_pos0"]), int(r["strand"]), int(r["p_m"]), int(r["p_h"]), int(r["p_canonical"]), int(r["channels"]) & (HAS_M | HAS_H))
        for r in rows
    )
    assert got == sorted(
        [
            (101, 0, 250, 0, 5, HAS_M),
            (106, 0, 10, 0, 245, HAS_M),
            (202, 1, 30, 0, 225, HAS_M),  # reverse: call on the G at 203, C at 202
            (206, 1, 200, 0, 55, HAS_M),
            (301, 0, 0, 0, 255, HAS_M),  # "." implicit canonical
            (305, 0, 220, 0, 35, HAS_M),
            (405, 0, 220, 0, 35, HAS_M),  # "?" keeps only the explicit call
            (901, 0, 60, 150, 45, HAS_M | HAS_H),
        ]
    )
    assert metrics["dropped_unaligned"] == 2
    assert metrics["invalid_mm"] == 1
    assert store.meta["records_decoded"] == 8  # the supplementary record is never decoded
    assert store.meta["codes_sampled"] == ["C+h", "C+m"]


def test_meth_calls_read_rows_index_the_read_summary(tmp_path: Path) -> None:
    store, _ = _run_meth_calls(tmp_path)
    summary = ReadSummary.open(tmp_path / "read-summary" / "columns")
    rows = np.concatenate(list(store.chunks()))
    starts = summary.column("start")[rows["read_row"]]
    # every call lies inside its own read's aligned span
    assert np.all((rows["ref_pos0"] >= starts - 1) & (rows["ref_pos0"] < summary.column("end")[rows["read_row"]]))


def test_meth_calls_drop_cpgs_split_by_a_deletion_and_expand_g_minus_channels(tmp_path: Path) -> None:
    reads = [
        # reverse record, query CG split by a 4 bp deletion: no genomic CpG, dropped
        Read("del", "chr1", 100, "CGAA", flag=16, cigar=[(0, 1), (2, 4), (0, 3)], mm="C+m?,0;", ml=[250]),
        # duplex-style forward record: C+m and G-m with implicit canonical calls
        Read("dup", "chr1", 200, "ACGTACGT", mm="C+m.,0;G-m.,0;", ml=[250, 250]),
    ]
    store_dir = tmp_path / "mc"
    ctx = context_for(tmp_path, CONTIGS, reads)
    output = METH_CALLS.run(ctx, store_dir)
    metrics = {m["metric_id"]: m["value"] for m in output.metrics}
    rows = sorted((int(r["ref_pos0"]), int(r["strand"]), int(r["p_m"])) for r in np.concatenate(list(MethCalls.open(store_dir / "calls").chunks())))
    assert rows == [(201, 0, 250), (201, 1, 250), (205, 0, 0), (205, 1, 0)]
    assert metrics["dropped_unaligned"] == 1


def test_channel_gate_samples_the_first_reads_tagged_or_not(tmp_path: Path) -> None:
    from traceback_lab.algorithms._records import eligible_record
    from traceback_lab.meth_calls import write_meth_calls

    reads = [Read(f"u{i:02d}", "chr1", 10 * i, "ACGTACGT") for i in range(5)]
    reads.append(Read("h", "chr1", 900, "ACGTACGT", mm="C+h?,0;", ml=[200]))
    context_for(tmp_path, CONTIGS, reads)
    bam = tmp_path / "input" / "aligned.bam"
    import traceback_lab.meth_calls as module

    original = module.CODE_SAMPLE
    module.CODE_SAMPLE = 5
    try:
        meta = write_meth_calls(bam, tmp_path / "calls", eligible=lambda r: eligible_record(r, {"chr1"}), contig_index={"chr1": 0})
    finally:
        module.CODE_SAMPLE = original
    assert meta["codes_sampled"] == [] and meta["code_sample_records"] == 5


def test_meth_calls_skip_without_mm(tmp_path: Path) -> None:
    reads = [Read("a", "chr1", 0, "ACGTACGT")]
    ctx = context_for(tmp_path, CONTIGS, reads)
    decision = METH_CALLS.preflight(ctx)
    assert isinstance(decision, Skip) and decision.reason_code == "missing_tags"


# ------------------------------------------------------------ consumers
def _write_store(directory: Path, rows: list[tuple], *, chunk: int = 3, codes=("C+h", "C+m")) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    array = np.array(rows, dtype=DTYPE)
    sizes = []
    for index, start in enumerate(range(0, array.size, chunk)):
        part = array[start : start + chunk]
        part.tofile(directory / f"calls-{index:05d}.bin")
        sizes.append(int(part.size))
    meta = {"schema": SCHEMA, "chunks": sizes, "codes_sampled": list(codes), "records_with_mm": 10, "records_decoded": 10}
    (directory / "meta.json").write_text(json.dumps(meta))
    return directory


def _consumer_ctx(tmp_path: Path, rows: list[tuple], *, codes=("C+h", "C+m"), derived=None, reads=None, contigs=CONTIGS):  # type: ignore[no-untyped-def]
    reads = reads or [Read(f"r{i}", "chr1", 100 * i, "A" * 50) for i in range(10)]
    ctx = context_for(tmp_path, contigs, reads, derived=derived, eligible_reads=600_000)
    _write_store(tmp_path / "mc" / "calls", rows, codes=codes)
    ctx.extras["upstream"]["meth-calls"] = str(tmp_path / "mc")
    return ctx


def _row(read_row: int, pos: int, p_m: int, p_h: int = 0, channels: int = HAS_M | HAS_H) -> tuple:
    return (read_row, 0, pos, 0, p_m, p_h, max(0, 255 - p_m - p_h), channels)


def test_argmax_state() -> None:
    chunk = np.array([_row(0, 1, 200, 10), _row(0, 2, 10, 200), _row(0, 3, 50, 50), _row(0, 4, 100, 100)], dtype=DTYPE)
    # canonical 45 / 45 / 155 / 55: 5mC, 5hmC, canonical, then a 5mC/5hmC tie -> 5mC
    assert argmax_state(chunk).tolist() == [1, 2, 0, 1]


def test_global_5mc_and_5hmc(tmp_path: Path) -> None:
    rows = [_row(0, 1, 230), _row(0, 2, 230), _row(1, 3, 10, 200), _row(2, 4, 5), _row(3, 5, 240, channels=HAS_M)]
    ctx = _consumer_ctx(tmp_path, rows)
    assert isinstance(GLOBAL_5MC.preflight(ctx), Ready)
    m = GLOBAL_5MC.run(ctx, tmp_path / "o").metrics[0]
    assert (m["value"], m["n"]) == (pytest.approx(3 / 5), 5)
    h = GLOBAL_5HMC.run(ctx, tmp_path / "o").metrics[0]
    assert (h["value"], h["n"]) == (pytest.approx(1 / 4), 4)  # only rows with a C+h channel


def test_global_5hmc_skips_without_the_channel(tmp_path: Path) -> None:
    ctx = _consumer_ctx(tmp_path, [_row(0, 1, 230, channels=HAS_M)], codes=("C+m",))
    decision = GLOBAL_5HMC.preflight(ctx)
    assert isinstance(decision, Skip) and decision.reason_code == "missing_channel"


def test_consumers_apply_the_derived_row_mask(tmp_path: Path) -> None:
    rows = [_row(i, i, 230 if i % 2 else 5) for i in range(10)]
    full = GLOBAL_5MC.run(_consumer_ctx(tmp_path, rows), tmp_path / "o").metrics[0]["n"]
    halves = [
        GLOBAL_5MC.run(_consumer_ctx(tmp_path, rows, derived={"transform": "split", "half": h}), tmp_path / "o").metrics[0]["n"]
        for h in "AB"
    ]
    assert sum(halves) == full == 10 and 0 < halves[0] < 10


def test_hypometh_reads_aggregate_across_chunk_boundaries(tmp_path: Path) -> None:
    rows = []
    # read 0: 4 calls, 0 methylated -> U, hypo; read 1: 5 calls, 4 methylated -> M;
    # read 2: 4 calls, 2 methylated -> X; read 3: 3 calls -> too few CpGs
    for read_row, pattern in ((0, [5, 5, 5, 5]), (1, [230, 230, 230, 230, 5]), (2, [230, 230, 5, 5]), (3, [230, 230, 230])):
        rows += [_row(read_row, 10 * read_row + i, p) for i, p in enumerate(pattern)]
    ctx = _consumer_ctx(tmp_path, rows)  # chunks of 3 split every read
    got = {m["metric_id"]: (m["value"], m["n"]) for m in HYPOMETH_READS.run(ctx, tmp_path / "o").metrics}
    assert got["hypometh_read_fraction"] == (pytest.approx(1 / 3), 3)
    assert got["u_x_m_counts"] == ({"U": 1, "X": 1, "M": 1}, 3)


def test_uxm_thresholds_are_inclusive_as_published() -> None:
    totals = np.array([4, 4, 4, 4, 4])
    methylated = np.array([0, 1, 2, 3, 4])  # 0, .25, .5, .75, 1
    assert classify_reads(totals, methylated) == {"reads": 5, "hypo": 1, "U": 2, "M": 2, "X": 1}


def test_pmd_score_inside_vs_outside(tmp_path: Path) -> None:
    bed = tmp_path / "root-assets" / ASSET_NAME
    bed.parent.mkdir(parents=True)
    with gzip.open(bed, "wt") as stream:
        stream.write("chr1\t0\t100\t0.3\tPMD\tcommonPMD\nchr1\t100\t200\tNA\tNA\tNeither\nchr1\t200\t300\t0.2\tPMD\tcommonPMD\n")
    sha = hashlib.sha256(bed.read_bytes()).hexdigest()
    algorithm = PmdScore("pmd-score", parameters={"asset": ASSET_NAME, "asset_sha256": sha, "label": "commonPMD", "call": "argmax"})
    rows = [_row(0, 50, 5), _row(0, 60, 230), _row(1, 150, 230), _row(1, 160, 230), _row(2, 250, 5)]
    toy = _consumer_ctx(tmp_path / "toy", rows)
    decision = algorithm.preflight(toy)
    assert isinstance(decision, Skip) and decision.reason_code == "reference"  # not hg38 coordinates
    ctx = _consumer_ctx(tmp_path, rows, contigs=[("chr1", 248_956_422)])
    decision = algorithm.preflight(ctx)
    assert isinstance(decision, Skip) and decision.reason_code == "missing_asset"
    target = tmp_path / "lab" / "assets" / "pmd" / ASSET_NAME
    target.parent.mkdir(parents=True)
    target.write_bytes(bed.read_bytes())
    assert isinstance(algorithm.preflight(ctx), Ready)
    got = {m["metric_id"]: m["value"] for m in algorithm.run(ctx, tmp_path / "o").metrics}
    assert got["pmd_meth"] == pytest.approx(1 / 3)
    assert got["non_pmd_meth"] == pytest.approx(1.0)
    assert got["pmd_delta"] == pytest.approx(2 / 3)
    target.write_bytes(b"other")
    decision = algorithm.preflight(ctx)
    assert isinstance(decision, Skip) and decision.reason_code == "missing_asset"


def test_pmd_interval_lookup() -> None:
    starts, ends = np.array([0, 200]), np.array([100, 300])
    assert inside(np.array([0, 99, 100, 199, 200, 299, 300]), starts, ends).tolist() == [
        True, True, False, False, True, True, False
    ]


def test_read_intervals_merges_and_filters(tmp_path: Path) -> None:
    bed = tmp_path / "p.bed.gz"
    with gzip.open(bed, "wt") as stream:
        stream.write("chr1\t100\t200\tx\tPMD\tcommonPMD\nchr1\t0\t100\tx\tPMD\tcommonPMD\nchr1\t300\t400\tx\tHMD\tcommonHMD\n")
    starts, ends = read_intervals(bed)["chr1"]
    assert starts.tolist() == [0] and ends.tolist() == [200]


def test_methylation_consumers_need_meth_calls_upstream() -> None:
    for algorithm in (GLOBAL_5MC, GLOBAL_5HMC, HYPOMETH_READS):
        assert algorithm.consumes == "meth_calls" and algorithm.upstream == ("read-summary", "meth-calls")
    assert HYPOMETH_READS.requires.min_eligible_reads == 500_000


# ---------------------------------------------------------- bounded RSS
_PEAK = textwrap.dedent(
    """
    import json, resource, sys, tracemalloc
    from pathlib import Path
    import numpy as np
    from traceback_lab.algorithms._meth import load_calls
    from traceback_lab.algorithms.global_5mc import GLOBAL_5MC
    from traceback_lab.registry import InputContext
    root = Path(sys.argv[1])
    ctx = InputContext(root=root, source_id="a" * 64, input_id="b" * 64,
                       reference_id="hg38", reference_sha256="c" * 64, input_dir=root,
                       extras={"upstream": {"meth-calls": str(root / "meth-calls"),
                                            "read-summary": str(root / "read-summary")}})
    calls, mask = load_calls(ctx)
    assert mask is None and len(calls.meta["chunks"]) > 1
    tracemalloc.start()
    result = None
    if sys.argv[2] == "stream":
        metric = GLOBAL_5MC.run(ctx, root).metrics[0]
        result = [metric["value"], metric["n"]]
    elif sys.argv[2] == "eager":
        materialized = np.concatenate(list(calls.chunks()))
        result = [int(materialized.size)]
    traced_peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    print(json.dumps({"rss": peak * (1 if sys.platform == "darwin" else 1024),
                      "traced_peak": traced_peak, "result": result}))
    """
)


def test_global_5mc_streaming_has_bounded_peak_rss(tmp_path: Path) -> None:
    """The production consumer stays below eager loading with >1 normal chunk."""

    chunk = CHUNK_CALLS
    n = 3 * chunk
    directory = tmp_path / "meth-calls" / "calls"
    directory.mkdir(parents=True)
    for index in range(n // chunk):
        part = np.zeros(chunk, dtype=DTYPE)
        part["read_row"] = np.arange(index * chunk, (index + 1) * chunk) // 8
        part["p_m"][1::4] = 255
        part["p_m"][3::4] = 255
        part["p_canonical"] = 255 - part["p_m"]
        part["channels"] = HAS_M
        part["channels"][::4] = 0
        part.tofile(directory / f"calls-{index:05d}.bin")
    (directory / "meta.json").write_text(json.dumps({"schema": SCHEMA, "chunks": [chunk] * (n // chunk), "codes_sampled": ["C+m"]}))
    summary = tmp_path / "read-summary" / "columns"
    summary.mkdir(parents=True)
    from traceback_lab.read_summary import SCHEMA as READ_SUMMARY_SCHEMA

    (summary / "meta.json").write_text(json.dumps({"schema": READ_SUMMARY_SCHEMA, "rows": n // 8,
                                                    "contigs": ["chr1"], "lengths": [1_000_000]}))

    def peak(mode: str) -> dict:
        out = subprocess.run([sys.executable, "-c", _PEAK, str(tmp_path), mode], check=True,
                             capture_output=True, text=True)
        return json.loads(out.stdout)

    baseline, eager, streamed = peak("open"), peak("eager"), peak("stream")
    assert eager["result"] == [n]
    assert streamed["result"] == [2 / 3, n * 3 // 4]
    # A 64 MiB fixed increment leaves room for platform allocator variation,
    # but remains below the cost of eagerly retaining all three normal chunks.
    assert streamed["rss"] - baseline["rss"] < 64 * 1024 * 1024, (baseline, streamed)
    # Import peaks can dominate ru_maxrss on CI; tracing begins after imports
    # and catches NumPy's eager materialization even when RSS highs coincide.
    assert eager["traced_peak"] - baseline["traced_peak"] > 32 * 1024 * 1024, (
        baseline, eager
    )
    assert streamed["traced_peak"] < eager["traced_peak"] - 8 * 1024 * 1024, (
        baseline, eager, streamed
    )


def test_every_l1_3_metric_has_a_citation_checked_library_entry() -> None:
    from traceback_lab.interpretation.engine import load_library

    library = load_library()
    expected = {
        "global-5mc": ["cpg_5mc_fraction"],
        "global-5hmc": ["cpg_5hmc_fraction"],
        "pmd-score": ["pmd_meth", "non_pmd_meth", "pmd_delta"],
        "hypometh-reads": ["hypometh_read_fraction", "u_x_m_counts"],
    }
    for algorithm_id, metric_ids in expected.items():
        assert library.headline(algorithm_id) is not None
        for metric_id in metric_ids:
            entry = library.entry(algorithm_id, metric_id)
            assert entry is not None and entry.citation_verified
    assert "meth-calls" in library.hidden_algorithms
    assert "zhou-pmd-terms-unknown" in library.entry("pmd-score", "pmd_delta")["license_ids"]


def test_lab_run_reuses_meth_calls_on_derived_inputs(lab_world, capsys, shared_mmi) -> None:
    from traceback_runner import cli as signed_cli

    code = signed_cli.main(
        ["lab", "run", str(lab_world["chunks"]), "--root", str(lab_world["root"]), "--mmi", str(shared_mmi), "--json"]
    )
    payload = json.loads(capsys.readouterr().out)
    assert code == 0, payload
    steps = {(step["algorithm_id"], step["input"]): step for step in payload["data"]["steps"]}
    assert steps[("meth-calls", "full")]["status"] == "ok"
    for label in ("0.5", "split-A", "split-B"):
        assert ("meth-calls", label) not in steps  # decoded once, reused by row mask
        assert steps[("global-5mc", label)]["status"] == "ok", steps[("global-5mc", label)]
    assert steps[("global-5mc", "full")]["status"] == "ok"
    # the synthetic chunks carry C+m only: no 5hmC channel
    assert (steps[("global-5hmc", "full")]["status"], steps[("global-5hmc", "full")]["skip_reason"]) == ("skipped", "missing_channel")
    # the toy input is far below 500k eligible reads
    assert steps[("hypometh-reads", "full")]["skip_reason"] == "depth"


def test_l0_copy_number_metrics_have_library_entries() -> None:
    """L0.6b: every metric copy-number emits has an entry (none falls to no_library_entry)."""

    import re

    from traceback_lab.interpretation.engine import load_library

    source = (Path(__file__).resolve().parents[2] / "traceback_lab" / "algorithms" / "copy_number.py").read_text()
    library = load_library()
    for metric_id in set(re.findall(r'metric\("([a-z0-9_]+)"', source)):
        entry = library.entry("copy-number", metric_id)
        assert entry is not None and entry.citation_verified, metric_id
