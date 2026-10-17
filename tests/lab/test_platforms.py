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


def test_each_unaligned_chunk_must_declare_the_platform() -> None:
    from traceback_lab.ingest import Chunk, ChunkSet, _refuse_mixed_models
    from traceback_lab.problems import LabProblem
    from traceback_lab.run_command import _declared_platform

    declared = Chunk(Path("declared.bam"), "declared.bam", 1, 1,
                     rg_lines=("@RG\tID:one\tPL:ONT",))
    missing = Chunk(Path("missing.bam"), "missing.bam", 2, 1)
    # Neither header declares a basecall model, so the older model check
    # cannot substitute for per-chunk platform verification.
    _refuse_mixed_models((declared, missing))
    with pytest.raises(LabProblem, match="TBX-LAB-024"):
        _declared_platform(ChunkSet([declared, missing]))

    second = Chunk(Path("second.bam"), "second.bam", 2, 1,
                   rg_lines=("@RG\tID:two\tPL:OXFORD_NANOPORE",))
    assert _declared_platform(ChunkSet([declared, second])) == "ont"


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
    with pytest.raises(ValueError, match="does not declare ONT"):
        for_input({"kind": "aligned", "platform": "ont"}, bam_path)


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


def test_prealigned_writer_refuses_unknown_platform_before_publishing(tmp_path: Path) -> None:
    from traceback_lab.ingest import ingest_prealigned, inspect_chunks, resolve_reference
    from traceback_lab.problems import LabProblem
    from traceback_lab.store import LabStore

    root, bam_path = _prealigned_world(tmp_path, (None,))
    store = LabStore(root)
    chunkset = inspect_chunks([bam_path], store, write_cache=False)
    reference = resolve_reference(root, "hg38")
    with pytest.raises(LabProblem, match="TBX-LAB-024"):
        ingest_prealigned(store, chunkset, reference)
    assert not store.inputs_dir.exists() or not list(store.inputs_dir.iterdir())


@pytest.mark.parametrize("tamper", ["platform", "kind"])
@pytest.mark.parametrize("dry_run", [False, True])
def test_stored_platform_mismatch_is_same_typed_refusal(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], dry_run: bool, tamper: str,
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
    if tamper == "kind":
        changed["kind"] = "aligned"
    manifest.write_text(json.dumps(changed))
    argv = ["lab", "run", "latest", "--root", str(root), "--json"]
    if dry_run:
        argv.append("--dry-run")
    code = signed_cli.main(argv)
    payload = json.loads(capsys.readouterr().out)
    assert code == 3
    assert payload["data"]["code"] == "TBX-LAB-024"
    assert payload["data"]["input_id"] == input_id
    assert "fresh ROOT" in payload["data"]["fix"]


def test_cohort_refuses_mismatched_stored_platform(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from traceback_lab.cohorts import member_from_store
    from traceback_lab.problems import LabProblem
    from traceback_lab.store import LabStore

    root, bam_path = _prealigned_world(tmp_path, ("ILLUMINA",))
    code = signed_cli.main([
        "lab", "run", str(bam_path), "--root", str(root), "--reference", "hg38",
        "--only", "short-read-fragment-size", "--no-downsample", "--no-controls", "--json",
    ])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0, payload
    sample = payload["data"]["sample"]
    manifest = root / "lab" / "inputs" / sample["input_id"] / "manifest.json"
    changed = json.loads(manifest.read_text())
    changed["platform"] = "ont"
    manifest.write_text(json.dumps(changed))
    with pytest.raises(LabProblem, match="TBX-LAB-024") as error:
        member_from_store(LabStore(root), sample["source_id"])
    assert error.value.data["input_id"] == sample["input_id"]


def test_valid_dry_run_reports_platform_in_sample(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root, bam_path = _prealigned_world(tmp_path, ("ILLUMINA",))
    code = signed_cli.main([
        "lab", "run", str(bam_path), "--root", str(root), "--reference", "hg38",
        "--dry-run", "--json",
    ])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0, payload
    assert payload["data"]["sample"]["platform"] == "illumina"


def test_simulate_cna_refuses_illumina_before_work(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    from traceback_lab import simulate_command

    root, bam_path = _prealigned_world(tmp_path, ("ILLUMINA",))
    code = signed_cli.main([
        "lab", "run", str(bam_path), "--root", str(root), "--reference", "hg38",
        "--only", "short-read-fragment-size", "--no-downsample", "--no-controls", "--json",
    ])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0, payload

    def unexpected(*_args, **_kwargs):
        pytest.fail("simulation work started before platform refusal")

    monkeypatch.setattr(simulate_command, "run_plan", unexpected)
    monkeypatch.setattr(simulate_command, "cna_specs", unexpected)
    code = signed_cli.main(["lab", "simulate", "cna", "latest", "--root", str(root), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 3
    assert payload["data"]["code"] == "TBX-LAB-024"
    assert payload["data"]["platform"] == "illumina"
