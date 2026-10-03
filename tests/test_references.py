"""Reference registration and `preflight --reference` (golden-path B2)."""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import stat
from pathlib import Path

import pysam
import pytest

from traceback_runner.cli import ExitCode, main
from traceback_runner.contracts import PreflightOutcome
from traceback_runner.fixtures import synthetic_registered_reference
from traceback_runner.preflight import _reference_matches
from traceback_runner.references import (
    ReferenceProblem,
    digest_fasta,
    load_reference,
    register_reference,
    validate_reference_id,
)

# Mixed case and uneven wrapping: M5 must uppercase and drop all whitespace.
_CONTIGS = {
    "chrA": "ACGTacgtNN" * 300,
    "chrB": "ggggCCCCaaaaTTTT" * 125,
}


def _write_fasta(directory: Path, contigs: dict[str, str] = _CONTIGS) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    fasta = directory / "tiny.fa"
    lines = []
    for name, sequence in contigs.items():
        lines.append(f">{name} test contig")
        lines.extend(sequence[i : i + 60] for i in range(0, len(sequence), 60))
    fasta.write_text("\n".join(lines) + "\n")
    pysam.faidx(str(fasta))
    return fasta


def _sam_md5(sequence: str) -> str:
    return hashlib.md5(sequence.upper().encode()).hexdigest()


def _write_bam(directory: Path, sq: list[dict[str, object]]) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    bam = directory / "tiny.bam"
    header = pysam.AlignmentHeader.from_dict(
        {"HD": {"VN": "1.6", "SO": "coordinate"}, "SQ": sq}
    )
    with pysam.AlignmentFile(str(bam), "wb", header=header) as output:
        for index in range(20):
            segment = pysam.AlignedSegment(header)
            segment.query_name = f"read-{index}"
            segment.query_sequence = "A" * 50
            segment.flag = 0
            segment.reference_id = 0
            segment.reference_start = 10 + index * 5
            segment.mapping_quality = 60
            segment.cigartuples = [(0, 50)]
            segment.query_qualities = pysam.qualitystring_to_array("I" * 50)
            output.write(segment)
    pysam.index(str(bam))
    return bam


def _plain_sq() -> list[dict[str, object]]:
    return [{"SN": name, "LN": len(sequence)} for name, sequence in _CONTIGS.items()]


def _run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, dict]:
    code = main([*argv, "--json"])
    return code, json.loads(capsys.readouterr().out)


@pytest.mark.parametrize(
    "value", ["../x", "a..b", "A-upper", "-lead", "", "a/b", "x" * 65, ".hidden"]
)
def test_reference_id_rejects_traversal_and_unsafe_names(value: str) -> None:
    with pytest.raises(ValueError):
        validate_reference_id(value)
    assert validate_reference_id("hg38-local") == "hg38-local"


def test_cli_rejects_traversal_id_as_usage_error(tmp_path: Path) -> None:
    fasta = _write_fasta(tmp_path / "ref")
    with pytest.raises(SystemExit) as raised:
        main(["reference", "register", "--fasta", str(fasta), "--id", "../escape",
              "--root", str(tmp_path / "root")])
    assert raised.value.code == ExitCode.USAGE
    assert not (tmp_path / "root").exists()
    assert not (tmp_path / "escape").exists()


def test_register_records_sam_m5_definition_and_private_write_once_files(
    tmp_path: Path,
) -> None:
    fasta = _write_fasta(tmp_path / "ref")
    root = tmp_path / "root"
    result = register_reference(root, fasta, "tiny")

    assert result.created
    registered = result.registered
    assert [(c.name, c.length, c.md5) for c in registered.contigs] == [
        (name, len(sequence), _sam_md5(sequence)) for name, sequence in _CONTIGS.items()
    ]
    assert registered.asset_sha256 == hashlib.sha256(fasta.read_bytes()).hexdigest()
    directory = root / "references" / "tiny"
    for name in ("registered-reference.json", "source.json"):
        assert stat.S_IMODE((directory / name).stat().st_mode) == 0o600
    source = json.loads((directory / "source.json").read_text())
    assert source["fasta_path"] == str(fasta.resolve())
    assert source["fasta_size_bytes"] == fasta.stat().st_size
    assert source["assembly_declared"] is False
    assert load_reference(root, "tiny").registered == registered


def test_digest_handles_chunk_boundaries_and_unwrapped_lines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import traceback_runner.references as references

    fasta = tmp_path / "unwrapped.fa"
    fasta.write_text(">one\n" + "acgt" * 100 + "\n>two desc\n" + "TTGCA" * 7 + "\n")
    monkeypatch.setattr(references, "_CHUNK_BYTES", 7)
    _, size, contigs = digest_fasta(fasta)
    assert size == fasta.stat().st_size
    assert [(c.name, c.length, c.md5) for c in contigs] == [
        ("one", 400, _sam_md5("acgt" * 100)),
        ("two", 35, _sam_md5("TTGCA" * 7)),
    ]


def test_register_refuses_gzip_fasta(tmp_path: Path) -> None:
    fasta = tmp_path / "ref.fa.gz"
    fasta.write_bytes(gzip.compress(b">chrA\nACGT\n"))
    with pytest.raises(ReferenceProblem) as raised:
        register_reference(tmp_path / "root", fasta, "gz")
    assert raised.value.code == "TBX-REF-001"
    assert "decompress" in raised.value.fix.lower()
    assert not (tmp_path / "root" / "references" / "gz").exists()


def test_register_refuses_missing_or_contradictory_fai(tmp_path: Path) -> None:
    fasta = _write_fasta(tmp_path / "ref")
    fai = Path(f"{fasta}.fai")
    rows = fai.read_text().splitlines()
    fields = rows[1].split("\t")
    fields[1] = str(int(fields[1]) + 1)
    fai.write_text("\n".join([rows[0], "\t".join(fields)]) + "\n")
    with pytest.raises(ReferenceProblem) as mismatch:
        register_reference(tmp_path / "root", fasta, "tiny")
    assert mismatch.value.code == "TBX-REF-001"

    fai.unlink()
    with pytest.raises(ReferenceProblem) as missing:
        register_reference(tmp_path / "root", fasta, "tiny")
    assert missing.value.code == "TBX-REF-001"
    assert not (tmp_path / "root" / "references" / "tiny").exists()


def test_reregister_identical_is_noop_and_different_bytes_conflict(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fasta = _write_fasta(tmp_path / "ref")
    root = tmp_path / "root"
    argv = ["reference", "register", "--fasta", str(fasta), "--id", "tiny", "--root", str(root)]
    code, first = _run(capsys, *argv)
    assert code == ExitCode.OK and first["data"]["created"] is True
    registration = root / "references" / "tiny" / "registered-reference.json"
    before = registration.read_bytes()

    code, again = _run(capsys, *argv)
    assert code == ExitCode.OK and again["data"]["created"] is False

    code, conflict = _run(capsys, *argv, "--assembly", "GRCh38")
    assert code == ExitCode.BLOCKED
    assert conflict["data"]["code"] == "TBX-REF-002"
    assert set(conflict["data"]) == {"code", "cause", "fix", "retryable", "docs"}
    assert registration.read_bytes() == before


def test_matcher_warns_on_absent_m5_and_blocks_on_wrong_m5() -> None:
    reference = synthetic_registered_reference()
    complete = [
        {"SN": c.name, "LN": c.length, "M5": c.md5, "AS": reference.assembly}
        for c in reference.contigs
    ]
    assert _reference_matches({"SQ": complete}, reference) == PreflightOutcome.PASS

    absent = [dict(line) for line in complete]
    del absent[1]["M5"]
    assert _reference_matches({"SQ": absent}, reference) == PreflightOutcome.WARN
    no_as = [{k: v for k, v in line.items() if k != "AS"} for line in complete]
    assert _reference_matches({"SQ": no_as}, reference) == PreflightOutcome.WARN

    wrong = [dict(line) for line in absent]
    wrong[0]["M5"] = "0" * 32
    assert _reference_matches({"SQ": wrong}, reference) == PreflightOutcome.BLOCKED
    wrong_as = [dict(line) for line in complete]
    wrong_as[0]["AS"] = "other"
    assert _reference_matches({"SQ": wrong_as}, reference) == PreflightOutcome.BLOCKED
    # Undeclared assembly: AS is never compared, so a full header can only WARN.
    assert (
        _reference_matches({"SQ": wrong_as}, reference, compare_assembly=False)
        == PreflightOutcome.WARN
    )
    renamed = [dict(line) for line in complete]
    renamed[0]["SN"] = "other"
    assert _reference_matches({"SQ": renamed}, reference) == PreflightOutcome.BLOCKED
    assert _reference_matches({"SQ": complete[:1]}, reference) == PreflightOutcome.BLOCKED


def test_cli_register_then_preflight_warns_without_m5(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fasta = _write_fasta(tmp_path / "ref")
    bam = _write_bam(tmp_path / "bam", _plain_sq())
    root = tmp_path / "root"
    code, registered = _run(
        capsys, "reference", "register", "--fasta", str(fasta), "--id", "tiny",
        "--root", str(root),
    )
    assert code == ExitCode.OK
    assert registered["data"]["contigs"] == 2

    code, payload = _run(
        capsys, "preflight", str(bam), "--reference", "tiny", "--root", str(root)
    )
    assert code == ExitCode.OK
    report = payload["data"]["report"]
    reference_check = next(c for c in report["checks"] if c["code"] == "TBX-BAM-002")
    assert reference_check["outcome"] == "warn"
    assert "lacks M5/AS" in reference_check["problem"]
    assert report["outcome"] == "partial"  # TBX-MOD-001 is PARTIAL without MM/ML
    assert report["fragment_measurement_eligible"] is True
    assert payload["data"]["qualified"] is False
    assert "not for clinical use" in payload["summary"]


def test_cli_preflight_blocks_bam_whose_m5_differs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fasta = _write_fasta(tmp_path / "ref")
    sq = _plain_sq()
    sq[0]["M5"] = _sam_md5("T" * len(_CONTIGS["chrA"]))
    bam = _write_bam(tmp_path / "bam", sq)
    root = tmp_path / "root"
    register_reference(root, fasta, "tiny")

    code, payload = _run(
        capsys, "preflight", str(bam), "--reference", "tiny", "--root", str(root)
    )
    assert code == ExitCode.BLOCKED
    checks = payload["data"]["report"]["checks"]
    assert any(c["code"] == "TBX-BAM-002" and c["outcome"] == "blocked" for c in checks)


def test_cli_assembly_rule_compares_as_only_when_declared(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fasta = _write_fasta(tmp_path / "ref")
    sq = [
        {**line, "M5": _sam_md5(_CONTIGS[str(line["SN"])]), "AS": "GRCh38"}
        for line in _plain_sq()
    ]
    bam = _write_bam(tmp_path / "bam", sq)
    root = tmp_path / "root"
    register_reference(root, fasta, "plain")
    register_reference(root, fasta, "grch38", assembly="GRCh38")
    register_reference(root, fasta, "other", assembly="hg19")

    def outcome(reference_id: str) -> str:
        _, payload = _run(
            capsys, "preflight", str(bam), "--reference", reference_id, "--root", str(root)
        )
        checks = payload["data"]["report"]["checks"]
        return next(c["outcome"] for c in checks if c["code"] == "TBX-BAM-002")

    assert outcome("plain") == "warn"
    assert outcome("grch38") == "pass"
    assert outcome("other") == "blocked"


def test_cli_preflight_unknown_reference_is_tbx_ref_003(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bam = _write_bam(tmp_path / "bam", _plain_sq())
    code, payload = _run(
        capsys, "preflight", str(bam), "--reference", "missing", "--root", str(tmp_path / "r")
    )
    assert code == ExitCode.BLOCKED
    assert payload["data"]["code"] == "TBX-REF-003"
    assert payload["summary"] == "Reference not registered under ROOT"


# SHA-256 of `preflight BAM --json` stdout (one canonical line) recorded on
# main before B2; synthetic preflight without --reference must not change.
_SYNTHETIC_PREFLIGHT_SHA256 = {
    "ordinary": "59a4249421b3135791e2f225103b3cc5d18a3c3d7cc8dc5e3e5596b2e89739fd",
    "valid_modbam": "0f045966765ce34e5f730cb8a7d8d0c6b65a7a61a7e90e0986567977434c3c14",
    "wrong_reference": "a9a1f01429a4f80431b8117d0688a6eda47b19e2c150e55bd6ba13d57516e00b",
}


@pytest.mark.parametrize("kind", sorted(_SYNTHETIC_PREFLIGHT_SHA256))
def test_synthetic_preflight_without_reference_is_byte_identical(
    kind: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from traceback_runner.fixtures import SyntheticBamKind, create_synthetic_bam

    fixture = create_synthetic_bam(tmp_path / "synthetic", SyntheticBamKind(kind))
    main(["preflight", str(fixture.bam_path), "--root", str(tmp_path / "root"), "--json"])
    line = capsys.readouterr().out.rstrip("\n")
    assert hashlib.sha256(line.encode()).hexdigest() == _SYNTHETIC_PREFLIGHT_SHA256[kind]


def test_fasta_locator_is_never_exported(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fasta = _write_fasta(tmp_path / "secret-fasta-dir")
    bam = _write_bam(tmp_path / "bam", _plain_sq())
    root = tmp_path / "root"
    locator = str(fasta.resolve())

    outputs = []
    for argv in (
        ("reference", "register", "--fasta", str(fasta), "--id", "tiny", "--root", str(root)),
        ("preflight", str(bam), "--reference", "tiny", "--root", str(root)),
        ("demo", "--root", str(root)),
    ):
        main([*argv, "--json"])
        outputs.append(capsys.readouterr().out)
    assert all(locator not in output and "secret-fasta-dir" not in output for output in outputs)

    records = root / "records"
    assert records.is_dir()
    for path in records.rglob("*"):
        if path.is_file():
            assert b"secret-fasta-dir" not in path.read_bytes(), path


def test_register_takes_the_operator_lock(tmp_path: Path, capsys) -> None:
    import fcntl

    fasta = _write_fasta(tmp_path / "ref")
    root = tmp_path / "root"
    root.mkdir()
    with (root / ".operator.lock").open("a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        code, payload = _run(
            capsys, "reference", "register", "--fasta", str(fasta), "--id", "tiny",
            "--root", str(root),
        )
    assert code == ExitCode.BLOCKED
    assert payload["data"] == {"retryable": True}
    assert not (root / "references" / "tiny").exists()


def test_load_rejects_registration_whose_id_differs(tmp_path: Path) -> None:
    fasta = _write_fasta(tmp_path / "ref")
    root = tmp_path / "root"
    register_reference(root, fasta, "tiny")
    os.rename(root / "references" / "tiny", root / "references" / "renamed")
    with pytest.raises(ReferenceProblem) as raised:
        load_reference(root, "renamed")
    assert raised.value.code == "TBX-REF-003"


def test_register_refuses_unreadable_fai_and_endless_header(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import traceback_runner.references as references

    fasta = _write_fasta(tmp_path / "ref")
    fai = Path(f"{fasta}.fai")
    fai.unlink()
    fai.mkdir()
    with pytest.raises(ReferenceProblem) as unreadable:
        register_reference(tmp_path / "root", fasta, "tiny")
    assert unreadable.value.code == "TBX-REF-001"

    endless = tmp_path / "endless.fa"
    endless.write_bytes(b">" + b"x" * 200)
    monkeypatch.setattr(references, "_CHUNK_BYTES", 16)
    monkeypatch.setattr(references, "_MAX_HEADER_BYTES", 64)
    with pytest.raises(ReferenceProblem) as header:
        digest_fasta(endless)
    assert header.value.code == "TBX-REF-001"


def test_index_rebuild_runs_outside_the_process_holding_the_lease(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """In-process pysam.index holds the GIL and starves the stage heartbeat.

    On a 2 GB BAM the rebuild took 65 s, past the runner's 30 s lease, so
    ``traceback run`` failed at validate. The rebuild must use a child process.
    """

    fasta = _write_fasta(tmp_path / "ref")
    bam = _write_bam(tmp_path / "bam", _plain_sq())
    root = tmp_path / "root"
    code, _ = _run(
        capsys, "reference", "register", "--fasta", str(fasta), "--id", "tiny",
        "--root", str(root),
    )
    assert code == ExitCode.OK

    def in_process_index(*_: object, **__: object) -> None:
        raise AssertionError("pysam.index ran in the lease-holding process")

    monkeypatch.setattr(pysam, "index", in_process_index)
    code, payload = _run(
        capsys, "preflight", str(bam), "--reference", "tiny", "--root", str(root)
    )
    assert code == ExitCode.OK
    checks = payload["data"]["report"]["checks"]
    index_check = next(c for c in checks if "index reconciles" in c["problem"])
    assert index_check["outcome"] == "pass"
