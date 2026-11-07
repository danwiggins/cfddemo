"""Bounded synthetic run of the production Griffin adapter configuration."""

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import argparse
from pathlib import Path

import pysam

from traceback_lab.adapters import griffin_run


parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--proof-dir", required=True, type=Path)
parser.add_argument("--upstream", required=True, type=Path)
parser.add_argument("--snakemake", required=True, type=Path)
parser.add_argument("--tool-dir", action="append", default=[], type=Path)
parser.add_argument("--work-parent", type=Path, default=Path(tempfile.gettempdir()))
args = parser.parse_args()
proof = args.proof_dir.resolve()
upstream = args.upstream.resolve()
root = Path(tempfile.mkdtemp(prefix="cfdna-s5-griffin-adapter-", dir=args.work_parent))
assert subprocess.run(["git", "-C", str(upstream), "status", "--porcelain"],
                      check=True, capture_output=True, text=True).stdout == ""
assert subprocess.run(["git", "-C", str(upstream), "rev-parse", "HEAD"],
                      check=True, capture_output=True, text=True).stdout.strip() == griffin_run.GRIFFIN_COMMIT
assets = root / "ref-assets"
assets.mkdir()

source_files = {
    "hg38.standard.chrom.sizes": "chrom.sizes",
    "k100.Umap.MultiTrackMappability.bw": "mappability.bw",
    "k100_minus_exclusion_lists.mappable_regions.hg38.bed": "mappable.bed",
}
for name in griffin_run._REF_FILES:
    if name in source_files:
        shutil.copy2(proof / source_files[name], assets / name)
    else:
        # The toy generator does not emit an exclusion list. Keep the
        # production-config smoke self-contained with an inert toy interval.
        (assets / name).write_text("chr1\t0\t1\n", encoding="ascii")

frequency = assets / "genome_GC_frequency"
frequency.mkdir()
sequence = "".join(proof.joinpath("toy.fa").read_text().splitlines()[1:]).upper()
prefix = "k100_minus_exclusion_lists.mappable_regions.hg38."
for length in range(15, 501):
    counts = [0] * (length + 1)
    gc = sum(base in "GC" for base in sequence[:length])
    counts[gc] += 1
    for start in range(1, len(sequence) - length + 1):
        gc += (sequence[start + length - 1] in "GC") - (sequence[start - 1] in "GC")
        counts[gc] += 1
    path = frequency / f"{prefix}{length}bp.GC_frequency.txt"
    with path.open("w", encoding="ascii") as stream:
        stream.write("length\tnum_GC\tnumber_of_fragments\n")
        stream.writelines(f"{length}\t{index}\t{count}\n" for index, count in enumerate(counts))

reference = root / "toy.fa"
shutil.copy2(proof / "toy.fa", reference)
shutil.copy2(proof / "toy.fa.fai", root / "toy.fa.fai")
sites = root / "sites.tsv"
shutil.copy2(proof / "sites.tsv", sites)

bam = root / "toy.bam"
with pysam.AlignmentFile(str(proof / "toy.bam"), "rb") as source:
    header = source.header.to_dict()
    header["RG"] = [{"ID": "toy", "PL": "ILLUMINA"}]
    with pysam.AlignmentFile(str(bam), "wb", header=header) as target:
        for record in source.fetch(until_eof=True):
            record.set_tag("RG", "toy")
            target.write(record)
pysam.index(str(bam))

os.environ["PATH"] = os.pathsep.join((
    str(args.snakemake.resolve().parent),
    *(str(path.resolve()) for path in args.tool_dir),
    os.environ.get("PATH", ""),
))
# Proof-only reduction: the production D21 minimum remains five million pairs.
griffin_run.MIN_ELIGIBLE_AUTOSOMAL_PAIRS = 500
config = griffin_run.GriffinRun(
    input_id="abcdef012345", bam=bam, reference_fasta=reference,
    reference_id="hg38", reference_sha256=hashlib.sha256(reference.read_bytes()).hexdigest(),
    eligible_autosomal_pairs=griffin_run.count_d21_pairs(bam),
    upstream_root=upstream, ref_assets=assets, site_lists={"ToySite": sites},
    attempt_dir=root / "adapter-attempt",
    snakemake=str(args.snakemake.resolve()), cores=1,
)
print(json.dumps({"smoke_root": str(root), "pairs": config.eligible_autosomal_pairs}), flush=True)
try:
    profiles = griffin_run.run_upstream(config)
except Exception as exc:
    print(json.dumps({"status": "failed", "error": f"{type(exc).__name__}: {exc}"}), flush=True)
    for path in sorted(config.attempt_dir.glob("*/workflow.log")):
        print(f"--- {path} ---", flush=True)
        print("\n".join(path.read_text(errors="replace").splitlines()[-30:]), flush=True)
    raise
else:
    corrected = next(config.attempt_dir.rglob("*.GC_corrected.coverage.tsv"))
    print(json.dumps({
        "status": "complete", "profile_count": len(profiles),
        "site_names": [profile.site_name for profile in profiles],
        "central_coverage": [profile.central_coverage for profile in profiles],
        "corrected_tsv_sha256": hashlib.sha256(corrected.read_bytes()).hexdigest(),
    }), flush=True)
