"""Platform declarations control Lab alignment and result compatibility."""

from pathlib import Path
import json

import pysam
import pytest

from traceback_lab.platforms import for_input, from_rg_lines
from traceback_runner import cli as signed_cli
from traceback_runner.references import register_reference


@pytest.mark.parametrize("lines,expected", [
    (["@RG\tID:a\tPL:ONT"], "ont"),
    (["@RG\tID:a\tPL:OXFORD_NANOPORE"], "ont"),
    (["@RG\tID:a\tPL:ILLUMINA"], "illumina"),
    (["@RG\tID:a\tPL:ONT", "@RG\tID:b\tPL:ILLUMINA"], "unknown"),
    (["@RG\tID:a"], "unknown"),
    ([], "unknown"),
])
def test_read_group_platform(lines: list[str], expected: str) -> None:
    assert from_rg_lines(lines) == expected


def test_stored_platform_must_match_bam_header(tmp_path: Path) -> None:
    bam_path = tmp_path / "aligned.bam"
    header = {"HD": {"VN": "1.6"}, "SQ": [{"SN": "chr1", "LN": 1000}],
              "RG": [{"ID": "one", "PL": "ILLUMINA"}]}
    with pysam.AlignmentFile(str(bam_path), "wb", header=header):
        pass
    assert for_input({"kind": "prealigned"}, bam_path) == "illumina"
    assert for_input({"kind": "prealigned", "platform": "illumina"}, bam_path) == "illumina"
    with pytest.raises(ValueError, match="differs"):
        for_input({"kind": "prealigned", "platform": "ont"}, bam_path)


@pytest.mark.parametrize("platform", ["ILLUMINA", None])
def test_unaligned_non_ont_is_refused_before_alignment(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], platform: str | None,
) -> None:
    from traceback_lab.synthetic import rg_line, write_chunks, write_reference

    reference = write_reference(tmp_path / "reference.fa", length=1000)
    root = tmp_path / "root"
    register_reference(root, reference.fasta, "synth")
    group = rg_line()
    if platform is None:
        group.pop("PL")
    else:
        group["PL"] = platform
    chunk = write_chunks(tmp_path / "chunks", reference, chunks=1, reads_per_chunk=1, rg=group)[0]
    code = signed_cli.main(["lab", "run", str(chunk), "--root", str(root), "--dry-run", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 3
    assert payload["data"]["code"] == "TBX-LAB-024"


def _prealigned_world(tmp_path: Path, platforms: tuple[str | None, ...]) -> tuple[Path, Path]:
    reference = tmp_path / "reference.fa"
    reference.write_text(">chr1\n" + "A" * 1000 + "\n")
    pysam.faidx(str(reference))
    root = tmp_path / "root"
    register_reference(root, reference, "hg38")
    bam_path = tmp_path / "aligned.bam"
    header = {"HD": {"VN": "1.6"}, "SQ": [{"SN": "chr1", "LN": 1000}],
              "RG": [{"ID": f"group-{index}", **({"PL": platform} if platform else {})}
                     for index, platform in enumerate(platforms)]}
    with pysam.AlignmentFile(str(bam_path), "wb", header=header) as bam:
        read = pysam.AlignedSegment()
        read.query_name = "synthetic-read"
        read.query_sequence = "A" * 100
        read.flag = 0
        read.reference_id = 0
        read.reference_start = 100
        read.mapping_quality = 60
        read.cigarstring = "100M"
        bam.write(read)
    return root, bam_path


@pytest.mark.parametrize("platforms", [(), (None,), ("ONT", "ILLUMINA"), ("PACBIO",)])
@pytest.mark.parametrize("dry_run", [False, True])
def test_unknown_prealigned_platform_is_blocked_in_normal_and_dry_runs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], platforms: tuple[str | None, ...], dry_run: bool,
) -> None:
    root, bam_path = _prealigned_world(tmp_path, platforms)
    argv = ["lab", "run", str(bam_path), "--root", str(root), "--reference", "hg38", "--json"]
    if dry_run:
        argv.append("--dry-run")
    code = signed_cli.main(argv)
    payload = json.loads(capsys.readouterr().out)
    assert code == 3
    assert payload["data"]["code"] == "TBX-LAB-024"


@pytest.mark.parametrize("dry_run", [False, True])
def test_stored_platform_mismatch_is_same_typed_refusal(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], dry_run: bool,
) -> None:
    root, bam_path = _prealigned_world(tmp_path, ("ILLUMINA",))
    code = signed_cli.main([
        "lab", "run", str(bam_path), "--root", str(root), "--reference", "hg38",
        "--only", "short-read-fragment-size", "--no-downsample", "--no-controls", "--json",
    ])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0, payload
    input_id = payload["data"]["sample"]["input_id"]
    manifest = root / "lab" / "inputs" / input_id / "manifest.json"
    changed = json.loads(manifest.read_text())
    changed["platform"] = "ont"
    manifest.write_text(json.dumps(changed))
    argv = ["lab", "run", "latest", "--root", str(root), "--json"]
    if dry_run:
        argv.append("--dry-run")
    code = signed_cli.main(argv)
    payload = json.loads(capsys.readouterr().out)
    assert code == 3
    assert payload["data"]["code"] == "TBX-LAB-024"
