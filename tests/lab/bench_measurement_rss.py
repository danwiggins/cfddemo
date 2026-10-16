"""Reproducible, synthetic native-RSS benchmark for the two L2 measurements.

Run from the repository root with the Lab environment's Python::

    python tests/lab/bench_measurement_rss.py --small-reads 20000 --large-reads 500000

Each algorithm and scale runs in a fresh process after fixtures are written.
Only generated summary columns, CpG calls and a reference FASTA are used.
The large case can take several minutes; coordinate its execution with the
integration-test scheduler. Output is JSON containing Linux peak RSS in bytes.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import resource
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from traceback_lab.meth_calls import DTYPE, HAS_M, SCHEMA as CALL_SCHEMA  # noqa: E402
from traceback_lab.read_summary import SCHEMA as SUMMARY_SCHEMA  # noqa: E402
from traceback_lab.registry import InputContext  # noqa: E402


def _prepare(base: Path, n_reads: int) -> None:
    import pysam

    columns = base / "summary" / "columns"
    columns.mkdir(parents=True)
    (columns / "meta.json").write_text(
        json.dumps(
            {
                "schema": SUMMARY_SCHEMA,
                "rows": n_reads,
                "contigs": ["chr1"],
                "lengths": [1_000_000],
            }
        )
    )
    block_reads = 1 << 15
    for name, dtype in (
        ("flag", "<u2"),
        ("contig", "<i2"),
        ("mapq", "<u1"),
        ("start", "<i4"),
        ("end", "<i4"),
        ("h_split", "<u8"),
    ):
        with (columns / f"{name}.bin").open("wb") as target:
            for lo in range(0, n_reads, block_reads):
                ids = np.arange(lo, min(n_reads, lo + block_reads), dtype=np.int64)
                starts = (ids % 10_000) * 50
                if name == "start":
                    values = starts
                elif name == "end":
                    values = starts + np.where(ids % 2 == 0, 100, 151)
                elif name == "mapq":
                    values = np.full(ids.size, 60)
                elif name == "h_split":
                    values = np.where((ids // 2) % 2 == 0, 0, 2**63)
                else:
                    values = np.zeros(ids.size)
                np.asarray(values, dtype=dtype).tofile(target)

    calls = base / "mc" / "calls"
    calls.mkdir(parents=True)
    chunk_reads = 1 << 18  # four calls/read => production-size 1M-call chunks
    sizes: list[int] = []
    for lo in range(0, n_reads, chunk_reads):
        ids = np.arange(lo, min(n_reads, lo + chunk_reads), dtype="<u4")
        rows = np.empty(ids.size * 4, dtype=DTYPE)
        rows["read_row"] = np.repeat(ids, 4)
        rows["contig"] = 0
        rows["ref_pos0"] = np.repeat((ids % 10_000) * 50, 4) + np.tile(
            np.arange(4), ids.size
        )
        rows["strand"] = 0
        rows["p_m"] = np.tile([230, 10, 230, 10], ids.size)
        rows["p_h"] = 0
        rows["p_canonical"] = 255 - rows["p_m"]
        rows["channels"] = HAS_M
        rows.tofile(calls / f"calls-{len(sizes):05d}.bin")
        sizes.append(rows.size)
    (calls / "meta.json").write_text(
        json.dumps({"schema": CALL_SCHEMA, "chunks": sizes, "records_with_mm": n_reads})
    )
    fasta = base / "reference.fa"
    reference = bytearray(b"A" * 1_000_000)
    for pos in range(100, len(reference) - 1, 100):
        reference[pos : pos + 2] = b"CG"
    fasta.write_bytes(b">chr1\n" + reference + b"\n")
    pysam.faidx(str(fasta))


def _context(base: Path, n_reads: int) -> InputContext:
    return InputContext(
        root=base,
        source_id="0" * 64,
        input_id="0" * 64,
        reference_id="synthetic",
        reference_sha256="0" * 64,
        input_dir=base,
        eligible_reads=n_reads,
        derived={"transform": "split", "half": "A"},
        extras={
            "upstream": {
                "read-summary": str(base / "summary"),
                "meth-calls": str(base / "mc"),
            },
            "reference_fasta": str(base / "reference.fa"),
        },
    )


def _worker(base: Path, n_reads: int, algorithm: str, baseline: Path | None) -> None:
    from traceback_lab.algorithms.breakpoint_motifs import BREAKPOINT_MOTIFS
    from traceback_lab.algorithms.frag_x_meth import FRAG_X_METH

    obj = FRAG_X_METH if algorithm == "frag-x-meth" else BREAKPOINT_MOTIFS
    if baseline is not None:
        if algorithm != "frag-x-meth":
            raise ValueError("baseline source applies only to frag-x-meth")
        spec = importlib.util.spec_from_file_location(
            "traceback_lab.algorithms._baseline_frag_x_meth", baseline
        )
        assert spec is not None and spec.loader is not None
        baseline_module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = baseline_module
        spec.loader.exec_module(baseline_module)
        obj = baseline_module.FRAG_X_METH
    before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024
    output = obj.run(_context(base, n_reads), base / "out")
    after = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024
    print(
        json.dumps(
            {
                "algorithm": algorithm,
                "reads": n_reads,
                "selected_reads": (n_reads // 4) * 2 + min(n_reads % 4, 2),
                "implementation": "baseline" if baseline is not None else "current",
                "peak_before_run_bytes": before,
                "peak_rss_bytes": after,
                "metric": output.metrics[0],
            },
            sort_keys=True,
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--small-reads", type=int, default=20_000)
    parser.add_argument("--large-reads", type=int, default=500_000)
    parser.add_argument(
        "--baseline-frag-file",
        type=Path,
        help="optional baseline frag-x-meth source for same-fixture RSS comparison",
    )
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--base", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--reads", type=int, help=argparse.SUPPRESS)
    parser.add_argument(
        "--algorithm", choices=("frag-x-meth", "breakpoint-motifs"),
        help=argparse.SUPPRESS,
    )
    args = parser.parse_args()
    if args.worker:
        if args.base is None or args.reads is None or args.algorithm is None:
            parser.error("worker requires --base, --reads and --algorithm")
        _worker(args.base, args.reads, args.algorithm, args.baseline_frag_file)
        return
    if args.small_reads < 1 or args.large_reads <= args.small_reads:
        parser.error("require 0 < small-reads < large-reads")
    with tempfile.TemporaryDirectory(prefix="lab-measurement-rss-") as temp:
        for n_reads in (args.small_reads, args.large_reads):
            base = Path(temp) / str(n_reads)
            _prepare(base, n_reads)
            for algorithm in ("frag-x-meth", "breakpoint-motifs"):
                observed = []
                sources = (
                    (None, args.baseline_frag_file)
                    if algorithm == "frag-x-meth" and args.baseline_frag_file
                    else (None,)
                )
                for source in sources:
                    command = [
                        sys.executable,
                        str(Path(__file__).resolve()),
                        "--worker",
                        "--base",
                        str(base),
                        "--reads",
                        str(n_reads),
                        "--algorithm",
                        algorithm,
                    ]
                    if source is not None:
                        command.extend(("--baseline-frag-file", str(source)))
                    result = subprocess.run(
                        command, check=True, capture_output=True, text=True
                    )
                    record = json.loads(result.stdout)
                    selected = (n_reads // 4) * 2 + min(n_reads % 4, 2)
                    expected_n = selected * (
                        2 if algorithm == "breakpoint-motifs" else 1
                    )
                    if record["metric"]["n"] != expected_n:
                        raise AssertionError(
                            "synthetic selected-read denominator changed"
                        )
                    if record["metric"]["value"] is None:
                        raise AssertionError(
                            "synthetic metric unexpectedly unavailable"
                        )
                    if (
                        algorithm == "breakpoint-motifs"
                        and record["metric"]["value"] <= 0
                    ):
                        raise AssertionError(
                            "synthetic reference has no nearby CpG ends"
                        )
                    observed.append(record)
                    print(json.dumps(record, sort_keys=True), flush=True)
                if (
                    len(observed) == 2
                    and observed[0]["metric"] != observed[1]["metric"]
                ):
                    raise AssertionError("baseline and current metric values differ")


if __name__ == "__main__":
    main()
