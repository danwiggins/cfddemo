"""The operator guide's real-BAM section (golden-path B8) cannot drift from the CLI.

1. Every docs anchor the CLI emits resolves to a heading or ``<a id>`` in
   ``docs/OPERATOR-GUIDE.md``: literal anchors in the problem modules, and the
   per-code anchor ``_problem`` builds for every ``TBX-*`` code they raise.
2. The guide's copy-paste journey block runs verbatim on generated inputs.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from traceback_runner import cli
from traceback_runner.fixtures import create_local_golden_path_inputs
from traceback_runner.references import ReferenceProblem

REPO = Path(__file__).resolve().parents[1]
GUIDE = REPO / "docs" / "OPERATOR-GUIDE.md"
GUIDE_PATH = "docs/OPERATOR-GUIDE.md"
# Modules whose ReferenceProblem codes reach ``cli._problem``.
PROBLEM_MODULES = (
    "traceback_runner/cli.py",
    "traceback_runner/references.py",
    "traceback_runner/local_authority.py",
    "traceback_runner/local_catalog.py",
    "traceback_runner/preflight.py",
)
_CODE = re.compile(r'"(TBX-[A-Z]+(?:-[A-Z]+)?-\d{3})"')


def _slug(heading: str) -> str:
    """GitHub's heading anchor: lowercase, drop punctuation, spaces to hyphens."""

    text = re.sub(r"[^\w\- ]", "", heading.strip().lower())
    return text.replace(" ", "-")


def _guide_anchors() -> set[str]:
    text = GUIDE.read_text(encoding="utf-8")
    anchors = set(re.findall(r'<a id="([a-z0-9-]+)"></a>', text))
    anchors.update(
        _slug(match) for match in re.findall(r"^#{1,6} (.+)$", text, flags=re.MULTILINE)
    )
    return anchors


def test_every_literal_docs_anchor_in_the_cli_resolves() -> None:
    anchors = _guide_anchors()
    found = []
    for module in PROBLEM_MODULES:
        source = (REPO / module).read_text(encoding="utf-8")
        found += re.findall(r"OPERATOR-GUIDE\.md#([A-Za-z0-9_-]+)", source)
    assert found, "expected at least the section anchor"
    missing = sorted(set(found) - anchors)
    assert not missing, f"anchors missing from {GUIDE_PATH}: {missing}"


def test_every_problem_code_has_a_troubleshooting_anchor() -> None:
    anchors = _guide_anchors()
    codes: set[str] = set()
    for module in PROBLEM_MODULES:
        codes.update(_CODE.findall((REPO / module).read_text(encoding="utf-8")))
    # The codes the brief requires the table to cover are all among them.
    required = {
        "TBX-REF-001", "TBX-REF-002", "TBX-REF-003", "TBX-BAM-002",
        "TBX-RUN-003", "TBX-RUN-004", "TBX-RUN-005", "TBX-RUN-006", "TBX-RUN-007",
        "TBX-CAT-001", "TBX-CAT-002", "TBX-AUTH-LOCAL-001", "TBX-AUTH-LOCAL-002",
        "TBX-JOB-001", "TBX-SERVE-001", "TBX-SERVE-002", "TBX-SERVE-003",
    }
    assert required <= codes
    missing = []
    for code in sorted(codes):
        anchor = cli._docs_anchor(code)
        path, fragment = anchor.split("#", 1)
        assert path == GUIDE_PATH
        if fragment not in anchors:
            missing.append(code)
    assert not missing, f"codes without a troubleshooting row: {missing}"
    text = GUIDE.read_text(encoding="utf-8")
    for message in ("startup lock is busy", "already running"):
        assert message in text


def test_problem_output_points_at_its_row() -> None:
    payload = cli._problem(
        "serve", ReferenceProblem("TBX-SERVE-002", "s", cause="c", fix="f", exit_code=4)
    )
    assert payload["data"]["docs"] == f"{GUIDE_PATH}#tbx-serve-002"
    assert "tbx-serve-002" in _guide_anchors()


def _journey_block() -> str:
    text = GUIDE.read_text(encoding="utf-8")
    match = re.search(
        r"<!-- golden-path-journey:begin -->\n```bash\n(.*?)```\n<!-- golden-path-journey:end -->",
        text,
        flags=re.DOTALL,
    )
    assert match, "journey block markers are missing"
    return match.group(1)


def _fake_uv_bin(tmp_path: Path) -> Path:
    """A stand-in `uv` so `uv run traceback ...` runs this checkout's CLI."""

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_uv = bin_dir / "uv"
    fake_uv.write_text(
        "#!/usr/bin/env bash\n"
        '[ "$1" = run ] || { echo "unexpected uv $1" >&2; exit 2; }\n'
        "shift\n"
        'if [ "$1" = traceback ]; then shift; exec '
        f'"{sys.executable}" -m traceback_runner "$@"; fi\n'
        f'if [ "$1" = python ]; then shift; exec "{sys.executable}" "$@"; fi\n'
        'echo "unexpected uv run $1" >&2; exit 2\n',
        encoding="utf-8",
    )
    fake_uv.chmod(fake_uv.stat().st_mode | stat.S_IXUSR)
    return bin_dir


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_guide_journey_runs_verbatim_on_generated_inputs(tmp_path: Path) -> None:
    block = _journey_block()
    assert "uv run traceback catalog import" in block
    inputs = create_local_golden_path_inputs(tmp_path / "inputs")
    bin_dir = _fake_uv_bin(tmp_path)
    root = tmp_path / "root"
    completed = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", block],
        cwd=REPO,
        env={
            **os.environ,
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "FASTA": str(inputs.fasta_path),
            "BAM": str(inputs.bam_path),
            "R": str(root),
        },
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    out = completed.stdout
    assert "Technical inspection partial" in out
    assert "Signed local record ready (development trust, unqualified, not for clinical use)" in out
    assert "Signed local record verified with development trust" in out
    assert "QUALIFICATION_STATE  development_unqualified" in out
    assert "CURRENT_PROVIDER_ELIGIBLE  False" in out
    assert len(list((root / "records").iterdir())) == 1


# Stands in for minimap2 when it is not installed (CI): reads the FASTQ that
# `samtools fastq -T MM,ML,MN` writes, places each generated read at the
# position its name encodes (`synthetic-<contig>-<start>-<n>`), and carries
# the FASTQ comment tags through as `-y` does.
_MINIMAP2_STUB = r'''
import sys

args = sys.argv[1:]
assert args[:3] == ["-ax", "map-ont", "-y"] and args[-1] == "-" and len(args) == 5, args
reference = args[3]
print("@HD\tVN:1.6\tSO:unsorted")
with open(reference + ".fai", encoding="ascii") as fai:
    for line in fai:
        name, length = line.split("\t")[:2]
        print(f"@SQ\tSN:{name}\tLN:{length}")
print("@PG\tID:minimap2-stub\tPN:minimap2-stub\tVN:0")
lines = sys.stdin.read().splitlines()
for offset in range(0, len(lines), 4):
    fields = lines[offset][1:].split("\t")
    name, tags = fields[0], fields[1:]
    sequence, quality = lines[offset + 1], lines[offset + 3]
    contig, start = name.removeprefix("synthetic-").rsplit("-", 2)[:2]
    print("\t".join([name, "0", contig, str(int(start) + 1), "60",
                     f"{len(sequence)}M", "*", "0", "0", sequence, quality, *tags]))
'''


def _batch_block() -> str:
    text = GUIDE.read_text(encoding="utf-8")
    match = re.search(
        r"<!-- minknow-batch:begin -->\n```bash\n(.*?)```\n<!-- minknow-batch:end -->",
        text,
        flags=re.DOTALL,
    )
    assert match, "MinKNOW batch block markers are missing"
    return match.group(1)


@pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("samtools") is None,
    reason="needs bash and samtools",
)
@pytest.mark.parametrize("aligner", ["stub", "minimap2"])
def test_guide_minknow_batch_runs_verbatim_per_barcode(tmp_path: Path, aligner: str) -> None:
    if aligner == "minimap2" and shutil.which("minimap2") is None:
        pytest.skip("minimap2 not installed; the stub variant covers the block")
    import pysam

    from traceback_runner.fixtures import create_unaligned_ont_bam

    block = _batch_block()
    assert "minimap2 -ax map-ont -y" in block
    inputs = create_local_golden_path_inputs(tmp_path / "inputs")
    with pysam.FastaFile(str(inputs.fasta_path)) as fasta:
        contigs = {name: fasta.fetch(name) for name in fasta.references}
    run_dir = tmp_path / "minknow-run"
    chunks = {"barcode01": 2, "barcode02": 1, "unclassified": 1}
    for seed, (directory, count) in enumerate(chunks.items()):
        for chunk in range(count):
            create_unaligned_ont_bam(
                run_dir / "bam_pass" / directory / f"chunk_{chunk}.bam",
                reads=60,
                contigs=contigs,
                seed=seed * 10 + chunk,
            )
    # A damaged chunk fails only its own barcode; the loop goes on.
    damaged = run_dir / "bam_pass" / "barcode03" / "chunk_0.bam"
    damaged.parent.mkdir(parents=True)
    damaged.write_bytes(b"not a BAM")
    bin_dir = _fake_uv_bin(tmp_path)
    if aligner == "stub":
        stub = bin_dir / "minimap2"
        stub.write_text(f"#!{sys.executable}\n" + _MINIMAP2_STUB, encoding="utf-8")
        stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
    root = tmp_path / "root"
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "FASTA": str(inputs.fasta_path),
        "R": str(root),
        "MINKNOW_RUN": str(run_dir),
        "ALIGNED": str(tmp_path / "aligned"),
    }
    registered = subprocess.run(
        [sys.executable, "-m", "traceback_runner", "reference", "register",
         "--fasta", str(inputs.fasta_path), "--id", "ref", "--root", str(root)],
        cwd=REPO, env=env, capture_output=True, text=True, timeout=120, check=False,
    )
    assert registered.returncode == 0, registered.stdout + registered.stderr
    completed = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", block],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    out = completed.stdout
    assert completed.returncode == 0, out + completed.stderr
    assert "FAILED barcode03" in out, out
    assert "FAILED barcode01" not in out and "FAILED barcode02" not in out, out
    # One merged, aligned BAM per barcode; `unclassified` is never merged in.
    assert sorted(p.name for p in (tmp_path / "aligned").glob("*.sorted.bam")) == [
        "barcode01.sorted.bam",
        "barcode02.sorted.bam",
    ]
    with pysam.AlignmentFile(str(tmp_path / "aligned" / "barcode01.sorted.bam")) as bam:
        assert sum(1 for _ in bam.fetch(until_eof=True)) == 120  # both chunks
    assert out.count("Signed local record ready") == 2
    assert "preflight warn" in out  # valid tags, no @RG after alignment: WARN
    assert "re-basecall" not in out.lower()
    assert len(list((root / "records").iterdir())) == 2
    assert out.count("QUALIFICATION_STATE  development_unqualified") == 2  # imported
