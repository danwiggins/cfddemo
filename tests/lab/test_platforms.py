"""Platform declarations control Lab alignment and result compatibility."""

from pathlib import Path

import pysam
import pytest

from traceback_lab.platforms import for_input, from_rg_lines


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
    import json

    from traceback_lab.synthetic import rg_line, write_chunks, write_reference
    from traceback_runner import cli as signed_cli
    from traceback_runner.references import register_reference

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
