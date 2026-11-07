"""Cross-check the Lab's single-end ONT Griffin port against pinned upstream code.

Two steps, two environments (synthetic data only):

  lab       (Lab environment, from the repository root)
            writes the synthetic world and spans of tests/lab/test_griffin_ont.py
            and the Lab port's corrected TSV.
  upstream  (a proof environment with pandas 1.5.3, numpy 1.26, scipy 1.11,
            matplotlib, pysam, pyBigWig and PyYAML)
            presents each span as a proper pair (forward mate at the span start
            with TLEN = span, reverse mate ending at the span end), runs the
            pinned griffin_GC_counts.py, griffin_GC_bias.py, griffin_coverage.py
            and griffin_merge_sites.py, and compares their output with the port.

The upstream scripts call pybedtools only to sort, merge and concatenate BED
intervals. This proof writes a small pandas stand-in for those calls so that a
bedtools binary is not needed, and forces the fork start method so upstream's
unguarded multiprocessing pools run on macOS.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

_STUB = '''import pandas as pd
def set_tempdir(directory): pass
def cleanup(*args, **kwargs): pass
class BedTool:
    def __init__(self, source=None, from_string=False):
        if from_string:
            self.df = pd.DataFrame({"chrom": pd.Series(dtype=object), "start": pd.Series(dtype=int), "end": pd.Series(dtype=int)})
        elif isinstance(source, pd.DataFrame):
            self.df = source
        else:
            rows = [line.rstrip("\\n").split("\\t")[:3] for line in open(source) if line.strip()]
            self.df = pd.DataFrame(rows, columns=["chrom", "start", "end"]).astype({"start": int, "end": int})
    @classmethod
    def from_dataframe(cls, df):
        d = df.iloc[:, :3].copy(); d.columns = ["chrom", "start", "end"]
        return cls(d.astype({"start": int, "end": int}).reset_index(drop=True))
    def sort(self):
        return BedTool(self.df.sort_values(["chrom", "start", "end"]).reset_index(drop=True))
    def merge(self):
        out = []
        for chrom, group in self.df.sort_values(["chrom", "start", "end"]).groupby("chrom", sort=True):
            cur = None
            for s, e in zip(group["start"], group["end"]):
                if cur is not None and s <= cur[2]:
                    cur[2] = max(cur[2], e)
                else:
                    if cur is not None: out.append(cur)
                    cur = [chrom, s, e]
            if cur is not None: out.append(cur)
        return BedTool(pd.DataFrame(out, columns=["chrom", "start", "end"]))
    def cat(self, other):
        return BedTool(pd.concat([self.df, other.df], ignore_index=True)).merge()
    def total_coverage(self):
        return int((self.df["end"] - self.df["start"]).sum())
    def to_dataframe(self):
        return self.df.copy()
    def __len__(self):
        return len(self.df)
'''


def lab(work: Path) -> None:
    import numpy as np

    sys.path.insert(0, str(Path.cwd() / "tests" / "lab"))
    import test_griffin_ont as fixture
    from traceback_lab.adapters.griffin_ont import run_ont

    work.mkdir(parents=True)
    world = fixture.write_world(work / "world")
    starts, ends = fixture.plant_fragments(world)
    np.savez(work / "fragments.npz", starts=starts, ends=ends)
    (work / "ont").mkdir()
    run_ont(fixture._config(work / "ont", world, starts, ends), mappability=fixture._ones)
    print("planted spans", len(starts))


def upstream(work: Path, checkout: Path) -> None:
    import numpy as np
    import pysam
    import pyBigWig
    import yaml

    scripts, world, ref = checkout / "scripts", work / "world", work / "world" / "ref"
    out = work / "upstream"
    out.mkdir()
    stub = out / "stub"
    stub.mkdir()
    (stub / "pybedtools.py").write_text(_STUB)
    (stub / "sitecustomize.py").write_text('import multiprocessing\nmultiprocessing.set_start_method("fork", force=True)\n')
    spans = np.load(work / "fragments.npz")
    length = int((ref / "hg38.standard.chrom.sizes").read_text().split()[1])
    unsorted = out / "pairs.unsorted.bam"
    header = {"HD": {"VN": "1.6", "SO": "unsorted"}, "SQ": [{"SN": "chr1", "LN": length}]}
    with pysam.AlignmentFile(str(unsorted), "wb", header=header) as bam:
        for index, (start, end) in enumerate(zip(spans["starts"].tolist(), spans["ends"].tolist())):
            size, mate_length = end - start, min(end - start, 50)
            for first in (True, False):
                read = pysam.AlignedSegment()
                read.query_name, read.query_sequence = f"f{index}", "A" * mate_length
                read.reference_id = read.next_reference_id = 0
                read.mapping_quality, read.cigarstring = 60, f"{mate_length}M"
                read.flag = 0x1 | 0x2 | (0x20 | 0x40 if first else 0x10 | 0x80)
                read.reference_start = start if first else end - mate_length
                read.next_reference_start = end - mate_length if first else start
                read.template_length = size if first else -size
                bam.write(read)
    paired = out / "pairs.bam"
    pysam.sort("-o", str(paired), str(unsorted))
    pysam.index(str(paired))
    track = pyBigWig.open(str(out / "map.bw"), "w")
    track.addHeader([("chr1", length)])
    track.addEntries(["chr1"], [0], ends=[length], values=[1.0])
    track.close()
    # No bytecode in the pinned checkout; upstream's pandas deprecation noise is kept for failures only.
    env = {**os.environ, "PYTHONPATH": str(stub), "PYTHONDONTWRITEBYTECODE": "1"}

    def run(*args: object) -> None:
        done = subprocess.run([sys.executable, *map(str, args)], env=env, cwd=out,
                              capture_output=True, text=True)
        if done.returncode:
            sys.exit(f"{Path(str(args[0])).name} failed:\n{done.stderr[-4000:]}")

    for sub in ("GC_counts", "GC_bias", "GC_plots"):
        (out / "gc" / sub).mkdir(parents=True)
    run(scripts / "griffin_GC_counts.py", "--bam_file", paired, "--bam_file_name", "s",
        "--mappable_regions_path", ref / "k100_minus_exclusion_lists.mappable_regions.hg38.bed",
        "--ref_seq", world / "toy.fa", "--chrom_sizes", ref / "hg38.standard.chrom.sizes",
        "--out_dir", out / "gc", "--map_q", 20, "--size_range", 15, 500, "--CPU", 1)
    run(scripts / "griffin_GC_bias.py", "--bam_file_name", "s", "--mappable_name",
        "k100_minus_exclusion_lists.mappable_regions.hg38", "--genome_GC_frequency", ref / "genome_GC_frequency",
        "--out_dir", out / "gc", "--size_range", 15, 500)
    sites = out / "sites.yaml"
    sites.write_text(yaml.safe_dump({"site_lists": {"ToyTF": str(world / "ToyTF.txt")}}))
    (out / "tmp").mkdir()
    (out / "results").mkdir()
    common = ["--sites_yaml", sites, "--griffin_scripts", scripts, "--chrom_column", "Chrom",
              "--position_column", "position", "--strand_column", "Strand", "--chroms", "chr1",
              "--norm_window", -5000, 5000, "--number_of_sites", "none", "--sort_by", "none",
              "--ascending", "none", "--CPU", 1, "--chrom_sizes_path", ref / "hg38.standard.chrom.sizes",
              "--mappability_bw", out / "map.bw", "--mappability_correction", "False", "--tmp_dir", out / "tmp"]
    run(scripts / "griffin_coverage.py", "--sample_name", "s", "--bam", paired,
        "--GC_bias", out / "gc/GC_bias/s.GC_bias.txt", "--mappability_bias", "none",
        "--reference_genome", world / "toy.fa", "--size_range", 100, 200, "--map_quality", 20, *common)
    exclude = [ref / name for name in (
        "encode_unified_GRCh38_exclusion_list.bed", "hg38_centromeres.bed", "hg38_gaps.bed",
        "hg38_fix_patches.bed", "hg38_alternative_haplotypes.bed")]
    run(scripts / "griffin_merge_sites.py", "--sample_name", "s",
        "--uncorrected_bw_path", out / "tmp/s/tmp_bigWig/s.uncorrected.bw",
        "--GC_corrected_bw_path", out / "tmp/s/tmp_bigWig/s.GC_corrected.bw",
        "--GC_map_corrected_bw_path", "none", "--results_dir", out / "results",
        "--save_window", -1000, 1000, "--center_window", -30, 30, "--fft_window", -960, 960,
        "--fft_index", 10, "--smoothing_length", 165, "--exclude_paths", *exclude, "--step", 15,
        "--CNA_normalization", "False", "--individual", "False", "--smoothing", "True",
        "--exclude_outliers", "True", "--exclude_zero_mappability", "True", *common)
    compare(work)


def compare(work: Path) -> None:
    import numpy as np
    import pandas as pd

    port = next((work / "ont" / "attempt").glob("*.GC_bias.txt"))
    mine = pd.read_csv(port, sep="\t")
    theirs = pd.read_csv(work / "upstream/gc/GC_bias/s.GC_bias.txt", sep="\t")
    joined = mine.merge(theirs[["length", "num_GC", "smoothed_GC_bias"]], on=["length", "num_GC"],
                        suffixes=("_port", "_upstream"))
    a, b = joined["smoothed_GC_bias_port"].to_numpy(float), joined["smoothed_GC_bias_upstream"].to_numpy(float)
    print("GC bias rows", len(joined), "same NaN pattern", bool(np.array_equal(np.isnan(a), np.isnan(b))),
          "max abs diff", float(np.nanmax(np.abs(a - b))))
    port_tsv = next((work / "ont" / "attempt").glob("*.GC_corrected.coverage.tsv")).read_text().splitlines()
    upstream_tsv = (work / "upstream/results/s/s.GC_corrected.coverage.tsv").read_text().splitlines()
    names = port_tsv[0].split("\t")
    differing = [name for name, x, y in zip(names, port_tsv[1].split("\t"), upstream_tsv[1].split("\t")) if x != y]
    print("corrected TSV header identical", port_tsv[0] == upstream_tsv[0],
          "rows", len(port_tsv) - 1, "differing fields", differing)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("step", choices=("lab", "upstream"))
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--upstream", type=Path, help="clean checkout of the pinned Griffin commit")
    args = parser.parse_args()
    if args.step == "lab":
        lab(args.work_dir)
    elif args.upstream is None:
        parser.error("upstream step needs --upstream")
    else:
        upstream(args.work_dir, args.upstream)


if __name__ == "__main__":
    main()
