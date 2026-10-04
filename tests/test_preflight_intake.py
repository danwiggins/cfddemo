"""Preflight intake for real MinKNOW/Dorado BAMs (usability A1, A5, A6).

Synthetic-only: every BAM and FASTA here is generated test data.
"""

from __future__ import annotations

import array
import json
import sqlite3
from pathlib import Path

import pysam
import pytest

from traceback_runner import cli, preflight
from traceback_runner.contracts import (
    JobState,
    PreflightOutcome,
    ReferenceContig,
    RegisteredReference,
)
from traceback_runner.fixtures import (
    SYNTHETIC_DORADO_MODBASE_MODEL,
    create_local_golden_path_inputs,
    create_unaligned_ont_bam,
)
from traceback_runner.preflight import BamPreflightPolicy, validate_bam_snapshot
from traceback_runner.store import JobStore

LOCAL_POLICY = BamPreflightPolicy(policy_id="local-unqualified-preflight-v1")


def _json(capsys: pytest.CaptureFixture[str], *argv: object) -> tuple[int, dict]:
    code = cli.main([*map(str, argv), "--json"])
    return code, json.loads(capsys.readouterr().out)


def _human(capsys: pytest.CaptureFixture[str], *argv: object) -> tuple[int, str]:
    code = cli.main([*map(str, argv)])
    return code, capsys.readouterr().out


@pytest.fixture
def golden(tmp_path: Path):
    return create_local_golden_path_inputs(tmp_path / "inputs", reads=200)


@pytest.fixture
def root(tmp_path: Path, golden, capsys) -> Path:
    root = tmp_path / "root"
    code, payload = _json(
        capsys, "reference", "register", "--fasta", golden.fasta_path, "--id", "ref",
        "--root", root,
    )
    assert code == cli.ExitCode.OK, payload
    return root


def _job_rows(root: Path) -> list[str]:
    database = root / "runner" / "runner.sqlite3"
    if not database.exists():
        return []
    with sqlite3.connect(database) as connection:
        return [row[0] for row in connection.execute("SELECT job_id FROM jobs")]


def _reference(*contigs: tuple[str, int]) -> RegisteredReference:
    return RegisteredReference(
        reference_id="synthetic-ref",
        assembly="synthetic-ref",
        asset_sha256="2" * 64,
        contigs=tuple(ReferenceContig(name=n, length=ln, md5="0" * 32) for n, ln in contigs),
    )


def _aligned_bam(
    directory: Path,
    sq: list[tuple[str, int]],
    *,
    stem: str = "aligned",
    reads: int = 3,
    tags: bool = False,
    read_groups: list[dict[str, str]] | None = None,
) -> tuple[Path, Path]:
    directory.mkdir(parents=True, exist_ok=True)
    header_dict: dict[str, object] = {
        "HD": {"VN": "1.6", "SO": "coordinate"},
        "SQ": [{"SN": name, "LN": length} for name, length in sq],
    }
    if read_groups:
        header_dict["RG"] = read_groups
    header = pysam.AlignmentHeader.from_dict(header_dict)
    bam = directory / f"{stem}.bam"
    with pysam.AlignmentFile(str(bam), "wb", header=header) as output:
        for index in range(reads):
            segment = pysam.AlignedSegment(header)
            segment.query_name = f"synthetic-{index}"
            segment.flag = 0
            segment.reference_id = 0
            segment.reference_start = 10 + index * 10
            segment.mapping_quality = 60
            segment.cigartuples = [(0, 50)]
            segment.query_sequence = "C" * 50
            segment.query_qualities = pysam.qualitystring_to_array("I" * 50)
            if tags:
                segment.set_tag("MM", "C+m,0;")
                segment.set_tag("ML", array.array("B", [200]))
                segment.set_tag("MN", 50)
            output.write(segment)
    pysam.index(str(bam))
    return bam, Path(f"{bam}.bai")


# --- A1: unaligned and empty BAMs -------------------------------------------


def test_unaligned_bam_is_tbx_bam_003_with_the_alignment_command(
    tmp_path: Path, golden, root: Path, capsys
) -> None:
    bam = create_unaligned_ont_bam(tmp_path / "minknow" / "chunk.bam", reads=50)

    report = validate_bam_snapshot(bam, None, _reference(("tiny_a", 20_000)), LOCAL_POLICY)
    assert [(c.code, c.outcome) for c in report.checks] == [
        ("TBX-BAM-003", PreflightOutcome.BLOCKED)
    ]
    assert "minimap2 -ax map-ont -y REF.fa" in report.checks[0].remediation

    code, payload = _json(capsys, "preflight", bam, "--reference", "ref", "--root", root)
    assert code == cli.ExitCode.BLOCKED
    serialized = json.dumps(payload)
    assert "minimap2 -ax map-ont" in serialized
    assert "align_command" not in payload["data"]  # --json never names the FASTA
    assert str(golden.fasta_path) not in serialized
    assert str(tmp_path) not in serialized

    code, out = _human(capsys, "preflight", bam, "--reference", "ref", "--root", root)
    assert code == cli.ExitCode.BLOCKED
    assert "TBX-BAM-003" in out
    assert f"minimap2 -ax map-ont -y {golden.fasta_path} -" in out  # human only


def test_run_refuses_unaligned_and_header_only_bams_before_any_job(
    tmp_path: Path, root: Path, capsys
) -> None:
    unaligned = create_unaligned_ont_bam(tmp_path / "minknow" / "chunk.bam", reads=5)
    header_only, _ = _aligned_bam(tmp_path / "empty", [("tiny_a", 20_000)], reads=0)

    for bam, expected in ((unaligned, "TBX-BAM-003"), (header_only, "TBX-BAM-004")):
        code, payload = _json(capsys, "run", bam, "--reference", "ref", "--root", root)
        assert code == cli.ExitCode.BLOCKED, payload
        assert payload["data"]["code"] == expected
        assert str(tmp_path) not in json.dumps(payload)
    assert _job_rows(root) == []
    assert not (root / "authority").exists()  # refused before any authority work


def test_header_only_bam_is_tbx_bam_004(tmp_path: Path, root: Path, capsys) -> None:
    bam, index = _aligned_bam(tmp_path / "empty", [("tiny_a", 20_000)], reads=0)
    report = validate_bam_snapshot(bam, index, _reference(("tiny_a", 20_000)), LOCAL_POLICY)
    assert [(c.code, c.outcome) for c in report.checks] == [
        ("TBX-BAM-004", PreflightOutcome.BLOCKED)
    ]
    assert "bam_pass" in report.checks[0].remediation
    code, payload = _json(capsys, "preflight", bam, "--reference", "ref", "--root", root)
    assert code == cli.ExitCode.BLOCKED
    assert payload["data"]["report"]["checks"][0]["code"] == "TBX-BAM-004"


def test_truncated_bam_is_still_tbx_bam_001(tmp_path: Path) -> None:
    bam, index = _aligned_bam(tmp_path / "t", [("tiny_a", 20_000)], reads=20)
    truncated = tmp_path / "truncated.bam"
    truncated.write_bytes(bam.read_bytes()[:-40])
    report = validate_bam_snapshot(
        truncated, index, _reference(("tiny_a", 20_000)), LOCAL_POLICY
    )
    assert report.checks[0].code == "TBX-BAM-001"
    assert report.checks[0].outcome == PreflightOutcome.BLOCKED
    assert "unreadable" in report.checks[0].problem


def test_internal_error_is_not_reported_as_a_bad_bam(
    tmp_path: Path, golden, root: Path, capsys, monkeypatch
) -> None:
    def boom(record: object) -> tuple[bool, bool]:
        raise RuntimeError("defect")

    monkeypatch.setattr(preflight, "_modification_tags_valid", boom)
    with pytest.raises(RuntimeError):
        validate_bam_snapshot(
            golden.bam_path, golden.index_path, _reference(("tiny_a", 20_000)), LOCAL_POLICY
        )

    code, payload = _json(
        capsys, "preflight", golden.bam_path, "--reference", "ref", "--root", root
    )
    assert code == cli.ExitCode.INTERNAL_ERROR
    assert payload["data"]["code"] == "TBX-INTERNAL-001"

    code, payload = _json(capsys, "run", golden.bam_path, "--reference", "ref", "--root", root)
    assert code == cli.ExitCode.BLOCKED, payload
    assert payload["data"]["code"] == "TBX-INTERNAL-001"
    assert payload["data"]["retryable"] is False
    (job_id,) = _job_rows(root)
    store = JobStore(root / "runner" / "runner.sqlite3")
    assert store.get(job_id).state == JobState.TERMINAL_FAILURE


def test_doctor_warns_when_minimap2_is_absent(tmp_path: Path, capsys, monkeypatch) -> None:
    real_which = cli.shutil.which
    monkeypatch.setattr(
        cli.shutil, "which", lambda name: None if name == "minimap2" else real_which(name)
    )
    code, payload = _json(capsys, "doctor", "--root", tmp_path / "root")
    assert code == cli.ExitCode.OK
    (check,) = [c for c in payload["data"]["checks"] if c["name"] == "minimap2"]
    assert check["status"] == "warn"
    assert "brew install minimap2" in check["detail"]


# --- A5: modification check for real Dorado BAMs ------------------------------

_DORADO_RG = [
    {
        "ID": "synthetic-rg",
        "DS": "runid=synthetic basecall_model=synthetic_basecall.v1 "
        f"modbase_models={SYNTHETIC_DORADO_MODBASE_MODEL}",
    }
]


def test_dorado_rg_declaration_with_valid_tags_passes(tmp_path: Path) -> None:
    bam, index = _aligned_bam(
        tmp_path, [("tiny_a", 20_000)], tags=True, read_groups=_DORADO_RG
    )
    report = validate_bam_snapshot(bam, index, _reference(("tiny_a", 20_000)), LOCAL_POLICY)
    (mod,) = [c for c in report.checks if c.code.startswith("TBX-MOD")]
    assert (mod.code, mod.outcome) == ("TBX-MOD-001", PreflightOutcome.PASS)
    assert f"{SYNTHETIC_DORADO_MODBASE_MODEL} declared by @RG DS" in mod.problem
    assert report.future_methylation_eligible is True


def test_valid_tags_without_declaration_warn_and_never_say_rebasecall(
    tmp_path: Path, golden, root: Path, capsys
) -> None:
    bam, index = _aligned_bam(tmp_path, [("tiny_a", 20_000), ("tiny_b", 6_000)], tags=True)
    report = validate_bam_snapshot(
        bam, index, _reference(("tiny_a", 20_000), ("tiny_b", 6_000)), LOCAL_POLICY
    )
    (mod,) = [c for c in report.checks if c.code.startswith("TBX-MOD")]
    assert (mod.code, mod.outcome) == ("TBX-MOD-001", PreflightOutcome.WARN)
    assert mod.problem == (
        "Modification tags present; basecall model not declared in the header."
    )
    assert mod.remediation == "No action needed for fragment length."

    code, out = _human(capsys, "preflight", bam, "--reference", "ref", "--root", root)
    assert code == cli.ExitCode.OK
    assert "re-basecall" not in out.lower()


def test_a_policy_model_must_match_the_rg_declaration(tmp_path: Path) -> None:
    bam, index = _aligned_bam(
        tmp_path, [("tiny_a", 20_000)], tags=True, read_groups=_DORADO_RG
    )
    report = validate_bam_snapshot(
        bam,
        index,
        _reference(("tiny_a", 20_000)),
        BamPreflightPolicy(policy_id="p", modified_base_model_id="other-model"),
    )
    (mod,) = [c for c in report.checks if c.code.startswith("TBX-MOD")]
    assert mod.outcome == PreflightOutcome.WARN


def test_declared_model_without_tags_still_advises_rebasecall(tmp_path: Path) -> None:
    bam, index = _aligned_bam(tmp_path, [("tiny_a", 20_000)], read_groups=_DORADO_RG)
    report = validate_bam_snapshot(bam, index, _reference(("tiny_a", 20_000)), LOCAL_POLICY)
    (mod,) = [c for c in report.checks if c.code.startswith("TBX-MOD")]
    assert mod.outcome == PreflightOutcome.PARTIAL
    assert "Re-basecall" in mod.remediation


def test_rg_model_parser_reads_dorado_form_only() -> None:
    parse = preflight._declared_modbase_models
    assert parse("runid=x basecall_model=b modbase_models=m1,m2") == ("m1", "m2")
    assert parse("basecall_model=b") == ()
    assert parse("xmodbase_models=m") == ()
    assert parse(None) == ()


# --- A6: contig-mismatch diff and the preflight reference rule ---------------

_HG38_LIKE = [("chr1", 248_956_422), ("chr2", 242_193_529), ("chr3", 198_295_559)]


def test_chr_prefix_rename_prints_diff_and_reheader_hint(tmp_path: Path) -> None:
    bam, index = _aligned_bam(tmp_path, [(n.removeprefix("chr"), ln) for n, ln in _HG38_LIKE])
    report = validate_bam_snapshot(bam, index, _reference(*_HG38_LIKE), LOCAL_POLICY)
    (check,) = [c for c in report.checks if c.code == "TBX-BAM-002"]
    assert check.outcome == PreflightOutcome.BLOCKED
    assert "at 3 of 3 positions" in check.problem
    assert "1 1 248956422 | chr1 248956422" in check.problem
    assert "samtools reheader" in check.remediation
    assert "sed -E 's/SN:/SN:chr/'" in check.remediation


def test_other_build_lengths_print_diff_without_reheader_hint(tmp_path: Path) -> None:
    grch37 = [("chr1", 249_250_621), ("chr2", 243_199_373), ("chr3", 198_022_430)]
    bam, index = _aligned_bam(tmp_path, grch37)
    report = validate_bam_snapshot(bam, index, _reference(*_HG38_LIKE), LOCAL_POLICY)
    (check,) = [c for c in report.checks if c.code == "TBX-BAM-002"]
    assert check.outcome == PreflightOutcome.BLOCKED
    assert "1 chr1 249250621 | chr1 248956422" in check.problem
    assert "reheader" not in check.remediation
    assert "different reference" in check.remediation


def test_missing_contig_shows_dash_and_total_count(tmp_path: Path) -> None:
    extra = [*_HG38_LIKE, ("chr4", 190_214_555), ("chr5", 181_538_259), ("chrX", 1)]
    bam, index = _aligned_bam(tmp_path, _HG38_LIKE[:2])
    report = validate_bam_snapshot(bam, index, _reference(*extra), LOCAL_POLICY)
    (check,) = [c for c in report.checks if c.code == "TBX-BAM-002"]
    assert "at 4 of 6 positions" in check.problem
    assert "3 - - | chr3 198295559" in check.problem
    assert "chrX" not in check.problem  # only the first three are shown


def test_preflight_without_reference_on_a_registered_root_is_tbx_ref_004(
    tmp_path: Path, golden, root: Path, capsys
) -> None:
    code, payload = _json(capsys, "preflight", golden.bam_path, "--root", root)
    assert code == cli.ExitCode.USAGE
    assert payload["data"]["code"] == "TBX-REF-004"
    assert payload["data"]["reference_ids"] == ["ref"]
    assert "--reference ref" in payload["data"]["fix"]

    # An unaligned BAM is told to align first (TBX-BAM-003 before TBX-REF-004).
    unaligned = create_unaligned_ont_bam(tmp_path / "minknow" / "chunk.bam", reads=3)
    code, payload = _json(capsys, "preflight", unaligned, "--root", root)
    assert code == cli.ExitCode.BLOCKED
    assert payload["data"]["report"]["checks"][0]["code"] == "TBX-BAM-003"
