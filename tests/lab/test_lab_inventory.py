"""``traceback lab inventory DIR``: chunk-set completeness before alignment (TBX-LAB-031)."""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from traceback_lab import ingest, inventory_command
from traceback_lab.inventory_command import (
    duplicates,
    estimate_records,
    group_label,
    header_provenance,
    missing_ranges,
    parse_final_summary,
    parse_sequencing_summary,
    sanitize_chunk_name,
    scan_directory,
)
from traceback_lab.store import LabStore
from traceback_lab.synthetic import rg_line, write_chunks, write_reference
from traceback_runner import cli as signed_cli

STEM = "SYNTH0001_pass_synthetic"
SAMPLE_ID = "synthetic-sample-never-printed"
RUN_ID = "synthetic-run"  # the synthetic @RG's runid= value
OTHER_RUN_ID = "another-synthetic-run"


@pytest.fixture(scope="module")
def reference(tmp_path_factory):
    base = tmp_path_factory.mktemp("inventory-ref")
    return write_reference(base / "ref.fa")


@pytest.fixture
def run_dir(tmp_path: Path, reference) -> Path:
    """A MinKNOW-like run folder: ``bam_pass/`` with four chunks, reports beside it."""

    run = tmp_path / "run"
    write_chunks(run / "bam_pass", reference, chunks=4, reads_per_chunk=50)
    return run


def _final_summary(run: Path, bam_files: int, *, fallback: int = 0, run_id: str = RUN_ID) -> Path:
    path = run / "final_summary_SYNTH_abcdef.txt"
    path.write_text(
        f"sample_id={SAMPLE_ID}\nprotocol_run_id=00000000-0000-0000-0000-000000000000\n"
        f"acquisition_run_id={run_id}\n"
        f"basecalling_enabled=1\nbam_files_in_final_dest={bam_files}\nbam_files_in_fallback={fallback}\n"
    )
    return path


def _sequencing_summary(
    run: Path, names: list[str], reads_per_chunk: int, *, fail_rows: int = 0, run_id: str = RUN_ID,
    with_pass_column: bool = True, prefix: str = "",
) -> Path:
    path = run / "sequencing_summary_SYNTH_abcdef.txt"
    columns = ["filename_bam", "read_id", "run_id", "sequence_length_template"]
    if with_pass_column:
        columns.insert(2, "passes_filtering")
    rows = ["\t".join(columns)]
    serial = 0

    def row(name: str, passes: str, length: int) -> str:
        cells = [prefix + name, f"read-{serial}", run_id, str(length)]
        if with_pass_column:
            cells.insert(2, passes)
        return "\t".join(cells)

    for name in names:
        for _ in range(reads_per_chunk):
            rows.append(row(name, "TRUE", 170))
            serial += 1
    for _ in range(fail_rows):
        rows.append(row(f"{STEM}_fail_0.bam", "FALSE", 90))
        serial += 1
    path.write_text("\n".join(rows) + "\n")
    return path


def _run(capsys, *argv: str) -> tuple[int, str]:
    code = signed_cli.main(["lab", "inventory", *argv])
    return code, capsys.readouterr().out


def _json(capsys, *argv: str) -> tuple[int, dict]:
    code, out = _run(capsys, *argv, "--json")
    return code, json.loads(out)


def _public(payload: dict) -> str:
    return json.dumps({key: value for key, value in payload["data"].items() if key not in {"root", "dir"}})


def test_complete_when_final_summary_agrees(run_dir: Path, tmp_path: Path, capsys) -> None:
    _final_summary(run_dir, 4)
    code, out = _run(capsys, str(run_dir / "bam_pass"), "--root", str(tmp_path / "root"))
    assert code == 0, out
    lines = out.splitlines()
    assert lines[0] == f"ROOT  {tmp_path / 'root'}"
    assert lines[1].startswith("PASS  inventory COMPLETE")
    assert lines[2] == f"DIR   {run_dir / 'bam_pass'}"
    assert "CHUNKS      4  (" in out and "INDICES     0-3 contiguous" in out
    assert "TRUNCATED   none" in out and "DUPLICATES  none" in out
    assert "READS       200 (exact)" in out
    assert "REPORT      final_summary_SYNTH_abcdef.txt (parent directory): declares 4 BAM file(s); 4 on disk" in out
    assert "VERDICT     COMPLETE" in out
    assert "basecall dna_r10.4.1_e8.2_400bps_sup@v5.0.0" in out
    assert "sample id present (sha256 " in out and "run id present (sha256 " in out
    assert SAMPLE_ID not in out and RUN_ID not in out
    # Nothing was written: the inventory is read-only and takes no ROOT.
    assert not (tmp_path / "root").exists()


def test_missing_indices_are_incomplete_with_ranges(run_dir: Path, tmp_path: Path, capsys, reference) -> None:
    write_chunks(run_dir / "bam_pass", reference, chunks=8, reads_per_chunk=10)
    for index in (2, 5, 6):
        (run_dir / "bam_pass" / f"{STEM}_{index}.bam").unlink()
    code, payload = _json(capsys, str(run_dir / "bam_pass"), "--root", str(tmp_path / "root"))
    assert code == 8
    data = payload["data"]
    assert payload["status"] == "partial_failure"
    assert data["verdict"] == "INCOMPLETE"
    [group] = data["groups"]
    assert group["missing_indexes"] == [[2, 2], [5, 6]] and group["missing_index_count"] == 3
    assert group["label"] == "DIR" and group["index_min"] == 0 and group["index_max"] == 7
    assert data["missing_index_count"] == 3 and data["reasons"] == ["missing 3 index(es): 2, 5-6"]
    assert data["chunks"] == 5
    code, out = _run(capsys, str(run_dir / "bam_pass"), "--root", str(tmp_path / "root"))
    assert code == 8 and "INDICES     0-7 missing 2, 5-6 (3 index(es))" in out
    assert out.splitlines()[1].startswith("PARTIAL_FAILURE  inventory INCOMPLETE")


def test_truncated_chunk_is_damaged(run_dir: Path, tmp_path: Path, capsys) -> None:
    _final_summary(run_dir, 4)
    victim = run_dir / "bam_pass" / f"{STEM}_1.bam"
    data = victim.read_bytes()
    victim.write_bytes(data[: len(data) // 2])
    code, payload = _json(capsys, str(run_dir / "bam_pass"), "--root", str(tmp_path / "root"))
    assert code == 8
    data = payload["data"]
    assert data["verdict"] == "DAMAGED" and data["truncated"] == [{"group": 1, "name": f"{STEM}_1.bam"}]
    entry = next(e for e in data["entries"] if e["index"] == 1)
    assert entry["eof_marker"] is False and entry["quickcheck"] is False and entry["truncated"] is True
    assert entry["records"] is None and entry["sha256"] is not None
    assert data["reasons"][0] == "1 truncated chunk(s)"
    code, out = _run(capsys, str(run_dir / "bam_pass"), "--root", str(tmp_path / "root"))
    assert f"TRUNCATED   {STEM}_1.bam" in out and "VERDICT     DAMAGED" in out
    assert "  NO   FAIL" in out


def test_duplicate_content_is_damaged(run_dir: Path, tmp_path: Path, capsys) -> None:
    chunks = run_dir / "bam_pass"
    shutil.copyfile(chunks / f"{STEM}_0.bam", chunks / f"{STEM}_4.bam")
    code, payload = _json(capsys, str(chunks), "--root", str(tmp_path / "root"))
    assert code == 8
    data = payload["data"]
    assert data["verdict"] == "DAMAGED" and data["duplicate_content_checked"] is True
    assert data["duplicates"] == [{
        "kind": "same_content",
        "chunks": [{"group": 1, "name": f"{STEM}_0.bam"}, {"group": 1, "name": f"{STEM}_4.bam"}],
        "sha256_12": data["entries"][0]["sha256"][:12],
    }]
    assert data["groups"][0]["missing_indexes"] == []
    code, out = _run(capsys, str(chunks), "--root", str(tmp_path / "root"))
    assert f"DUPLICATES  {STEM}_0.bam = {STEM}_4.bam (same content, sha256 " in out


def test_no_hash_skips_content_duplicates_and_says_so(run_dir: Path, tmp_path: Path, capsys, monkeypatch) -> None:
    chunks = run_dir / "bam_pass"
    shutil.copyfile(chunks / f"{STEM}_0.bam", chunks / f"{STEM}_4.bam")
    _final_summary(run_dir, 5)
    monkeypatch.setattr(inventory_command, "_sha256_file", lambda path: (_ for _ in ()).throw(AssertionError("hashed")))
    code, payload = _json(capsys, str(chunks), "--no-hash", "--root", str(tmp_path / "root"))
    assert code == 0, payload
    data = payload["data"]
    assert data["hashing"] is False and data["duplicate_content_checked"] is False
    assert data["duplicates"] == [] and data["verdict"] == "COMPLETE"
    assert all(entry["sha256"] is None and entry["records"] == 50 for entry in data["entries"])
    code, out = _run(capsys, str(chunks), "--no-hash", "--root", str(tmp_path / "root"))
    assert "DUPLICATES  content not checked (--no-hash); the verdict cannot be DAMAGED by duplicate content" in out


def test_same_index_twice_in_one_group_only() -> None:
    def entry(group: int, name: str, index: int, digest: str) -> inventory_command.Entry:
        return inventory_command.Entry(name=name, path=Path(name), group=group, index=index, size=1, sha256=digest)

    one = inventory_command.Group(id=1, parts=(), entries=[entry(1, "x_1.bam", 1, "a" * 64), entry(1, "y_1.bam", 1, "b" * 64)])
    assert duplicates([one]) == [{
        "kind": "same_index", "chunks": [{"group": 1, "name": "x_1.bam"}, {"group": 1, "name": "y_1.bam"}], "index": 1,
    }]
    two = inventory_command.Group(id=2, parts=("barcode02",), entries=[entry(2, "z_1.bam", 1, "c" * 64)])
    one.entries.pop()
    assert duplicates([one, two]) == []
    # Without hashes the same index twice is still a duplicate; same content across groups is not checked.
    assert duplicates([one, two], hashing=False) == []
    one.entries.append(entry(1, "y_1.bam", 1, None))
    assert duplicates([one], hashing=False)[0]["kind"] == "same_index"


def test_final_summary_mismatch_is_incomplete(run_dir: Path, tmp_path: Path, capsys) -> None:
    _final_summary(run_dir, 6)
    code, payload = _json(capsys, str(run_dir / "bam_pass"), "--root", str(tmp_path / "root"))
    assert code == 8
    data = payload["data"]
    assert data["verdict"] == "INCOMPLETE" and data["groups"][0]["missing_indexes"] == []
    assert data["reasons"] == ["final_summary_SYNTH_abcdef.txt declares 6 BAM file(s); 4 on disk"]
    comparison = data["comparisons"][0]
    assert comparison["agrees"] is False and comparison["comparable"] is True and comparison["run_id_match"] is True
    report = data["reports"][0]
    assert report["sample_id_present"] is True and report["run_id"]["present"] is True
    assert SAMPLE_ID not in _public(payload) and RUN_ID not in _public(payload)


def test_final_summary_declaring_fewer_files_is_damaged(run_dir: Path, tmp_path: Path, capsys) -> None:
    _final_summary(run_dir, 3)
    code, payload = _json(capsys, str(run_dir / "bam_pass"), "--root", str(tmp_path / "root"))
    assert code == 8 and payload["data"]["verdict"] == "DAMAGED"
    assert payload["data"]["reasons"] == ["4 BAM file(s) on disk; final_summary_SYNTH_abcdef.txt declares 3"]


def test_final_summary_counts_the_bam_fail_sibling(run_dir: Path, tmp_path: Path, capsys) -> None:
    (run_dir / "bam_fail").mkdir()
    (run_dir / "bam_fail" / f"{STEM}_fail_0.bam").write_bytes(b"not inspected")
    _final_summary(run_dir, 5)
    code, payload = _json(capsys, str(run_dir / "bam_pass"), "--root", str(tmp_path / "root"))
    assert code == 0
    data = payload["data"]
    assert data["verdict"] == "COMPLETE"
    assert data["fail_groups"] == [{"id": 2, "label": "bam_fail", "bam_files": 1, "unindexed": 0, "where": "sibling"}]
    assert data["comparisons"][0]["on_disk_bam_files"] == 5 and data["on_disk_bam_files"] == 5
    code, out = _run(capsys, str(run_dir / "bam_pass"), "--root", str(tmp_path / "root"))
    assert "FAIL        bam_fail (sibling): 1 indexed .bam file(s), fail output, not inventoried" in out


def test_report_from_another_run_is_not_compared(run_dir: Path, tmp_path: Path, capsys) -> None:
    _final_summary(run_dir, 4, run_id=OTHER_RUN_ID)
    code, payload = _json(capsys, str(run_dir / "bam_pass"), "--root", str(tmp_path / "root"))
    assert code == 0
    data = payload["data"]
    assert data["verdict"] == "UNKNOWN"
    assert data["reasons"] == ["final_summary_SYNTH_abcdef.txt: report run id does not match chunks; not compared"]
    comparison = data["comparisons"][0]
    assert comparison["run_id_match"] is False and comparison["comparable"] is False and comparison["agrees"] is None
    code, out = _run(capsys, str(run_dir / "bam_pass"), "--root", str(tmp_path / "root"))
    assert "REPORT      final_summary_SYNTH_abcdef.txt (parent directory): run id does not match the chunks; not compared" in out
    assert OTHER_RUN_ID not in out
    names = [f"{STEM}_{index}.bam" for index in range(4)]
    (run_dir / "final_summary_SYNTH_abcdef.txt").unlink()
    _sequencing_summary(run_dir, names, 50, run_id=OTHER_RUN_ID)
    code, payload = _json(capsys, str(run_dir / "bam_pass"), "--root", str(tmp_path / "root"))
    assert code == 0 and payload["data"]["verdict"] == "UNKNOWN"
    assert payload["data"]["comparisons"][0]["run_id_match"] is False


def test_sequencing_summary_names_and_reads_are_compared(run_dir: Path, tmp_path: Path, capsys) -> None:
    names = [f"{STEM}_{index}.bam" for index in range(4)]
    _sequencing_summary(run_dir, names, 50, fail_rows=7)
    code, payload = _json(capsys, str(run_dir / "bam_pass"), "--root", str(tmp_path / "root"))
    assert code == 0, payload
    data = payload["data"]
    assert data["verdict"] == "COMPLETE"
    report = data["reports"][0]
    assert report["kind"] == "sequencing_summary" and report["where"] == "parent directory"
    assert report["declared_reads"] == 207 and report["declared_pass_reads"] == 200
    assert report["declared_pass_bam_files"] == names and report["run_id"]["present"] is True
    comparison = data["comparisons"][0]
    assert comparison["agrees"] is True and comparison["missing_on_disk"] == [] and comparison["not_declared"] == []
    assert comparison["on_disk_reads"] == 200 and comparison["run_id_match"] is True
    code, out = _run(capsys, str(run_dir / "bam_pass"), "--root", str(tmp_path / "root"))
    assert ("REPORT      sequencing_summary_SYNTH_abcdef.txt (parent directory): declares 207 read(s), 200 pass, "
            "4 pass chunk(s); names match; 200 read(s) on disk") in out
    (run_dir / "bam_pass" / names[3]).unlink()
    code, payload = _json(capsys, str(run_dir / "bam_pass"), "--root", str(tmp_path / "root"))
    assert code == 8 and payload["data"]["verdict"] == "INCOMPLETE"
    assert payload["data"]["comparisons"][0]["missing_on_disk"] == [names[3]]
    assert payload["data"]["reasons"] == ["sequencing_summary_SYNTH_abcdef.txt lists 1 pass chunk(s) not on disk"]


def test_sequencing_summary_exact_read_mismatch_is_incomplete(run_dir: Path, tmp_path: Path, capsys) -> None:
    names = [f"{STEM}_{index}.bam" for index in range(4)]
    _sequencing_summary(run_dir, names, 60)
    code, payload = _json(capsys, str(run_dir / "bam_pass"), "--root", str(tmp_path / "root"))
    assert code == 8 and payload["data"]["verdict"] == "INCOMPLETE"
    assert payload["data"]["reasons"] == [
        "sequencing_summary_SYNTH_abcdef.txt declares 240 pass read(s); 200 in the chunks on disk"
    ]


def test_sequencing_summary_read_totals_not_compared_when_estimated(run_dir: Path, tmp_path: Path, capsys, monkeypatch, reference) -> None:
    write_chunks(run_dir / "bam_pass", reference, chunks=1, reads_per_chunk=1_500, stem=f"{STEM}_big")
    (run_dir / "bam_pass" / f"{STEM}_big_0.bam").rename(run_dir / "bam_pass" / f"{STEM}_4.bam")
    names = [f"{STEM}_{index}.bam" for index in range(5)]
    _sequencing_summary(run_dir, names, 1)  # wrong totals on purpose
    monkeypatch.setattr(inventory_command, "SAMPLE_RECORDS", 300)
    monkeypatch.setattr(inventory_command, "SAMPLE_BYTES", 1)
    code, payload = _json(capsys, str(run_dir / "bam_pass"), "--root", str(tmp_path / "root"))
    assert code == 0 and payload["data"]["verdict"] == "COMPLETE"
    comparison = payload["data"]["comparisons"][0]
    assert "on_disk_reads" not in comparison
    assert comparison["notes"] == ["read totals not compared (1 chunk count(s) are estimates)"]


def test_sequencing_summary_extra_chunks_on_disk_are_damaged(run_dir: Path, tmp_path: Path, capsys) -> None:
    names = [f"{STEM}_{index}.bam" for index in range(3)]
    _sequencing_summary(run_dir, names, 50)
    code, payload = _json(capsys, str(run_dir / "bam_pass"), "--root", str(tmp_path / "root"))
    assert code == 8 and payload["data"]["verdict"] == "DAMAGED"
    assert payload["data"]["comparisons"][0]["not_declared"] == [f"{STEM}_3.bam"]
    assert payload["data"]["reasons"] == ["1 chunk(s) on disk that sequencing_summary_SYNTH_abcdef.txt does not list"]


def test_sequencing_summary_without_passes_filtering_is_not_comparable(run_dir: Path, tmp_path: Path, capsys) -> None:
    names = [f"{STEM}_{index}.bam" for index in range(4)]
    _sequencing_summary(run_dir, names, 50, with_pass_column=False)
    code, payload = _json(capsys, str(run_dir / "bam_pass"), "--root", str(tmp_path / "root"))
    assert code == 0
    data = payload["data"]
    assert data["verdict"] == "UNKNOWN"
    assert data["reasons"] == ["sequencing_summary_SYNTH_abcdef.txt: no passes_filtering column; not comparable"]
    report = data["reports"][0]
    assert report["declared_pass_reads"] is None and report["declared_pass_bam_files"] is None
    assert report["passes_filtering_column"] is False and report["declared_reads"] == 200
    code, out = _run(capsys, str(run_dir / "bam_pass"), "--root", str(tmp_path / "root"))
    assert "declares 200 read(s); no passes_filtering column; not comparable" in out


def test_report_chunk_names_are_sanitized_to_basenames(run_dir: Path, tmp_path: Path, capsys) -> None:
    names = [f"{STEM}_{index}.bam" for index in range(4)]
    _sequencing_summary(run_dir, names, 50, prefix="/Volumes/donor name/bam_pass/")
    code, payload = _json(capsys, str(run_dir / "bam_pass"), "--root", str(tmp_path / "root"))
    assert code == 0 and payload["data"]["verdict"] == "COMPLETE"
    assert payload["data"]["reports"][0]["declared_pass_bam_files"] == names
    assert "donor" not in _public(payload) and "Volumes" not in _public(payload)
    assert sanitize_chunk_name("C:\\runs\\x_1.bam") == "x_1.bam"
    assert sanitize_chunk_name("x 1.bam") is None and sanitize_chunk_name("x_1.txt") is None
    assert sanitize_chunk_name("../x_1.bam") == "x_1.bam"


def test_explicit_report_path_is_used_and_only_its_name_printed(run_dir: Path, tmp_path: Path, capsys) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    report = _final_summary(elsewhere, 4)
    code, out = _run(capsys, str(run_dir / "bam_pass"), "--report", str(report), "--root", str(tmp_path / "root"))
    assert code == 0
    assert f"REPORT      {report.name} (--report): declares 4 BAM file(s); 4 on disk" in out
    assert "elsewhere" not in out
    code, payload = _json(capsys, str(run_dir / "bam_pass"), "--report", str(report), "--root", str(tmp_path / "root"))
    assert str(tmp_path) not in _public(payload) and "elsewhere" not in _public(payload)


def test_no_report_is_unknown_and_exit_zero(run_dir: Path, tmp_path: Path, capsys) -> None:
    code, payload = _json(capsys, str(run_dir / "bam_pass"), "--root", str(tmp_path / "root"))
    assert code == 0 and payload["status"] == "ok"
    assert payload["data"]["verdict"] == "UNKNOWN" and payload["data"]["reports"] == []
    code, out = _run(capsys, str(run_dir / "bam_pass"), "--root", str(tmp_path / "root"))
    assert "REPORT      none found" in out and "VERDICT     UNKNOWN: no MinKNOW report" in out


def test_json_shape(run_dir: Path, tmp_path: Path, capsys) -> None:
    _final_summary(run_dir, 4)
    code, payload = _json(capsys, str(run_dir / "bam_pass"), "--root", str(tmp_path / "root"))
    assert code == 0
    assert payload["schema_version"] == "traceback.cli-result.v2"
    assert payload["command"] == "lab inventory" and payload["status"] == "ok"
    assert payload["data_origin"] == "local_unqualified" and "_human" not in payload
    data = payload["data"]
    assert data["root"] == str(tmp_path / "root") and data["dir"] == str(run_dir / "bam_pass")
    assert set(data) == {
        "root", "dir", "recursive", "hashing", "verdict", "reasons", "chunks", "total_bytes", "groups",
        "missing_index_count", "unindexed", "truncated", "duplicates", "duplicate_content_checked", "hash_failures",
        "records", "provenance", "hash_cached_chunks", "fail_groups", "on_disk_bam_files", "reports", "comparisons",
        "skipped", "entries",
    }
    assert data["records"] == {
        "total": 200, "kind": "exact", "counted_chunks": 4, "unreadable_chunks": 0,
        "sample_records": 2000, "sample_bytes": 1024 * 1024,
    }
    assert data["hash_failures"] == 0 and data["duplicate_content_checked"] is True
    assert data["total_bytes"] == sum(entry["bytes"] for entry in data["entries"])
    [group] = data["groups"]
    assert set(group) == {
        "id", "label", "known", "depth", "path_sha256_12", "chunks", "bytes", "index_min", "index_max",
        "missing_indexes", "missing_index_count", "unindexed",
    }
    assert group["id"] == 1 and group["label"] == "DIR" and group["path_sha256_12"] is None
    entry = data["entries"][0]
    assert set(entry) == {
        "name", "group", "index", "bytes", "eof_marker", "quickcheck", "header_ok", "truncated", "aligned",
        "sha256", "hash_failed", "records", "records_kind", "sampled_records", "hash_cached", "provenance",
    }
    report = data["reports"][0]
    assert report["name"] == "final_summary_SYNTH_abcdef.txt" and report["explicit"] is False
    assert report["name_sha256_12"] == hashlib.sha256(b"final_summary_SYNTH_abcdef.txt").hexdigest()[:12]
    assert len(entry["sha256"]) == 64 and entry["aligned"] is False and entry["group"] == 1
    assert "/" not in entry["name"]
    prov = data["provenance"]
    assert prov["basecall_models"] == ["dna_r10.4.1_e8.2_400bps_sup@v5.0.0"]
    assert prov["programs"] == [{"name": "synthetic", "version": "0"}]
    assert prov["run_id"]["present"] and len(prov["run_id"]["sha256_12"][0]) == 12
    assert prov["sample_id"]["present"] and len(prov["sample_id"]["sha256_12"][0]) == 12
    comparison = data["comparisons"][0]
    assert set(comparison) >= {"name", "kind", "run_id_match", "comparable", "agrees", "incomplete", "damaged", "notes"}
    # No value of an identifier and no host path beyond ROOT and DIR as given.
    assert RUN_ID not in _public(payload) and str(tmp_path) not in _public(payload)


def test_empty_dir_is_tbx_lab_031(tmp_path: Path, capsys) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    (empty / "notes.txt").write_text("not a chunk\n")
    (empty / "merged.bam").write_bytes(b"")
    code, payload = _json(capsys, str(empty), "--root", str(tmp_path / "root"))
    assert code == 3
    assert payload["status"] == "blocked" and payload["data"]["code"] == "TBX-LAB-031"
    assert payload["data"]["docs"] == "docs/LAB-GUIDE.md#tbx-lab-031"
    assert payload["data"]["dir"] == str(empty) and payload["data"]["skipped"] == []
    assert payload["data"]["unindexed"] == [{"group": 1, "name_sha256_12": hashlib.sha256(b"merged.bam").hexdigest()[:12], "bytes": 0}]
    code, out = _run(capsys, str(empty), "--root", str(tmp_path / "root"))
    assert code == 3 and "CODE       TBX-LAB-031" in out
    assert "CAUSE      DIR (empty) holds no readable indexed .bam chunk" in out
    assert str(tmp_path) not in out.replace(str(tmp_path / "root"), "")


def test_missing_and_non_directory_are_tbx_lab_005(tmp_path: Path, capsys) -> None:
    code, payload = _json(capsys, str(tmp_path / "nowhere"), "--root", str(tmp_path / "root"))
    assert code == 3 and payload["data"]["code"] == "TBX-LAB-005" and payload["data"]["kind"] == "missing"
    plain = tmp_path / "plain.bam"
    plain.write_bytes(b"")
    code, payload = _json(capsys, str(plain), "--root", str(tmp_path / "root"))
    assert code == 3 and payload["data"]["code"] == "TBX-LAB-005" and payload["data"]["kind"] == "not a directory"


def test_symlinked_chunks_are_skipped_not_counted(run_dir: Path, tmp_path: Path, capsys) -> None:
    chunks = run_dir / "bam_pass"
    (chunks / f"{STEM}_9.bam").symlink_to(chunks / f"{STEM}_0.bam")
    code, payload = _json(capsys, str(chunks), "--root", str(tmp_path / "root"))
    assert code == 0
    data = payload["data"]
    assert data["chunks"] == 4 and data["skipped"] == [{"group": "DIR", "name": f"{STEM}_9.bam", "kind": "a symlink"}]
    assert data["duplicates"] == [] and data["groups"][0]["index_max"] == 3
    code, out = _run(capsys, str(chunks), "--root", str(tmp_path / "root"))
    assert f"SKIPPED     DIR: {STEM}_9.bam (a symlink); lab run refuses these" in out


def test_unindexed_bams_are_listed_and_excluded_from_totals(run_dir: Path, tmp_path: Path, capsys) -> None:
    chunks = run_dir / "bam_pass"
    shutil.copyfile(chunks / f"{STEM}_0.bam", chunks / "merged.bam")
    _final_summary(run_dir, 4)
    code, payload = _json(capsys, str(chunks), "--root", str(tmp_path / "root"))
    assert code == 0, payload
    data = payload["data"]
    assert data["verdict"] == "COMPLETE" and data["chunks"] == 4
    digest = hashlib.sha256(b"merged.bam").hexdigest()[:12]
    assert data["unindexed"] == [{"group": 1, "name_sha256_12": digest, "bytes": (chunks / "merged.bam").stat().st_size}]
    assert data["groups"][0]["unindexed"] == [{"name_sha256_12": digest, "bytes": (chunks / "merged.bam").stat().st_size}]
    assert "merged" not in _public(payload)
    assert data["total_bytes"] == sum(e["bytes"] for e in data["entries"])
    assert data["records"]["total"] == 200 and data["comparisons"][0]["on_disk_bam_files"] == 4
    assert data["duplicates"] == []
    code, out = _run(capsys, str(chunks), "--root", str(tmp_path / "root"))
    assert f"UNINDEXED   1 .bam file(s) without a _<n>.bam index, not counted (sha256 {digest})" in out
    assert "merged" not in out


def test_recursive_groups_pass_fail_and_barcodes(run_dir: Path, tmp_path: Path, capsys, reference) -> None:
    """bam_pass/ + bam_fail/ + barcode01/: fail output counted by name only, barcodes judged separately."""

    write_chunks(run_dir / "bam_fail", reference, chunks=2, reads_per_chunk=5, stem=f"{STEM}_fail")
    write_chunks(run_dir / "bam_pass" / "barcode01", reference, chunks=3, reads_per_chunk=5,
                 stem="SYNTH0001_pass_barcode01", seed=5)
    _final_summary(run_dir, 9)
    code, payload = _json(capsys, str(run_dir), "--root", str(tmp_path / "root"))
    assert code == 3 and payload["data"]["code"] == "TBX-LAB-031"
    assert "searched recursively" not in payload["data"]["cause"]
    code, payload = _json(capsys, str(run_dir), "--recursive", "--root", str(tmp_path / "root"))
    assert code == 0, payload
    data = payload["data"]
    assert data["recursive"] is True and data["chunks"] == 7 and data["verdict"] == "COMPLETE"
    assert [(g["label"], g["known"], g["chunks"], g["missing_indexes"]) for g in data["groups"]] == [
        ("bam_pass", True, 4, []), ("bam_pass/barcode01", True, 3, []),
    ]
    assert [(g["label"], g["bam_files"], g["unindexed"], g["where"]) for g in data["fail_groups"]] == [("bam_fail", 2, 0, "DIR")]
    ids = [g["id"] for g in data["groups"]] + [g["id"] for g in data["fail_groups"]]
    assert len(set(ids)) == len(ids) == 3  # one counter across pass and fail groups
    assert data["duplicates"] == []  # index 0-2 exist in both groups: not a collision
    assert data["records"]["total"] == 200 + 15
    assert data["comparisons"][0]["on_disk_bam_files"] == 9
    assert all("/" not in entry["name"] for entry in data["entries"])
    assert data["reports"][0]["where"] == "DIR"
    code, out = _run(capsys, str(run_dir), "--recursive", "--root", str(tmp_path / "root"))
    assert "INDICES     bam_pass: 0-3 contiguous" in out and "INDICES     bam_pass/barcode01: 0-2 contiguous" in out
    assert "FAIL        bam_fail: 2 indexed .bam file(s), fail output, not inventoried" in out
    assert f"CHUNKS      7  (bam_pass/{STEM}_0.bam … bam_pass/barcode01/SYNTH0001_pass_barcode01_2.bam)" in out
    assert " in 2 group(s)" in out


def test_recursive_sequencing_summary_compares_basenames(run_dir: Path, tmp_path: Path, capsys) -> None:
    names = [f"{STEM}_{index}.bam" for index in range(4)]
    _sequencing_summary(run_dir, names, 50)
    code, payload = _json(capsys, str(run_dir), "--recursive", "--root", str(tmp_path / "root"))
    assert code == 0, payload
    data = payload["data"]
    assert data["verdict"] == "COMPLETE" and data["groups"][0]["label"] == "bam_pass"
    comparison = data["comparisons"][0]
    assert comparison["missing_on_disk"] == [] and comparison["not_declared"] == [] and comparison["on_disk_reads"] == 200


def test_unknown_directory_names_are_never_printed(run_dir: Path, tmp_path: Path, capsys, reference) -> None:
    secret = "DonorSurname_plasma_2026"
    write_chunks(run_dir / secret / "bam_pass", reference, chunks=2, reads_per_chunk=5, stem="OTHER_pass_x", seed=3)
    (run_dir / secret / "bam_pass" / "OTHER_pass_x_7.bam").symlink_to(run_dir / secret / "bam_pass" / "OTHER_pass_x_0.bam")
    locked = run_dir / secret / "locked"
    locked.mkdir()
    (locked / "x_0.bam").write_bytes(b"")
    (run_dir / secret / "link").symlink_to(run_dir / "bam_pass")
    locked.chmod(0o000)
    try:
        code, payload = _json(capsys, str(run_dir), "--recursive", "--root", str(tmp_path / "root"))
        code2, out = _run(capsys, str(run_dir), "--recursive", "--root", str(tmp_path / "root"))
    finally:
        locked.chmod(0o700)
    assert code == 0 and code2 == 0, payload
    data = payload["data"]
    digest = hashlib.sha256(f"{secret}/bam_pass".encode()).hexdigest()[:12]
    unknown = next(group for group in data["groups"] if not group["known"])
    assert sorted(group["label"] for group in data["groups"]) == sorted(["bam_pass", f"group {unknown['id']} (sha256 {digest})"])
    assert unknown["path_sha256_12"] == digest and unknown["depth"] == 2
    kinds = sorted((s["kind"], s["name"]) for s in data["skipped"])
    assert kinds == [("a symlink", "OTHER_pass_x_7.bam"), ("a symlink, not followed", None), ("unreadable directory", None)]
    assert all(secret not in (s["group"] or "") for s in data["skipped"])
    assert secret not in _public(payload) and secret not in out.replace(str(run_dir), "")
    assert group_label(3, ("bam_pass", "barcode07")) == "bam_pass/barcode07"
    assert group_label(3, ("unclassified",)) == "unclassified"
    assert group_label(3, ("bam_pass", "x")) == "group 3 (sha256 " + hashlib.sha256(b"bam_pass/x").hexdigest()[:12] + ")"


def test_scan_directory_lists_unreadable_and_symlinked_subdirectories(tmp_path: Path, reference) -> None:
    write_chunks(tmp_path / "bam_pass", reference, chunks=1, reads_per_chunk=5)
    (tmp_path / "bam_pass" / "sub").mkdir()
    (tmp_path / "bam_pass" / "sub" / "x_0.bam").write_bytes(b"")
    (tmp_path / "bam_pass" / "sub").chmod(0o000)
    (tmp_path / "bam_pass" / "lnk").symlink_to(tmp_path / "bam_pass")
    try:
        scan = scan_directory(tmp_path / "bam_pass", recursive=True)
    finally:
        (tmp_path / "bam_pass" / "sub").chmod(0o700)
    assert [group.label for group in scan.groups] == ["DIR"]
    assert sorted(s.kind for s in scan.skipped) == ["a symlink, not followed", "unreadable directory"]
    flat = scan_directory(tmp_path / "bam_pass", recursive=False)
    assert flat.skipped == [] and len(flat.entries) == 1


def test_hash_cache_is_read_but_never_written(run_dir: Path, tmp_path: Path, capsys, monkeypatch) -> None:
    root = tmp_path / "root"
    store = LabStore(root)
    store.ensure()
    chunks = run_dir / "bam_pass"
    ingest.inspect_chunks(ingest.collect_paths([str(chunks)]), store)
    before = store.hash_cache_file.read_bytes()
    monkeypatch.setattr(inventory_command, "_sha256_file", lambda path: (_ for _ in ()).throw(AssertionError("rehashed")))
    code, payload = _json(capsys, str(chunks), "--root", str(root))
    assert code == 0
    data = payload["data"]
    assert data["hash_cached_chunks"] == 4
    assert all(entry["hash_cached"] and entry["records_kind"] == "exact" and entry["records"] == 50 for entry in data["entries"])
    assert store.hash_cache_file.read_bytes() == before
    code, out = _run(capsys, str(chunks), "--root", str(root))
    assert "HASHES      4 of 4 chunk(s) from the Lab hash cache (read-only)" in out


def test_estimate_extrapolates_from_the_first_records(tmp_path: Path, reference, monkeypatch) -> None:
    [chunk] = write_chunks(tmp_path / "big", reference, chunks=1, reads_per_chunk=1_500)
    exact, kind, sampled = estimate_records(chunk, chunk.stat().st_size)
    assert (exact, kind, sampled) == (1_500, "exact", 1_500)
    monkeypatch.setattr(inventory_command, "SAMPLE_RECORDS", 300)
    monkeypatch.setattr(inventory_command, "SAMPLE_BYTES", 1)
    estimate, kind, sampled = estimate_records(chunk, chunk.stat().st_size)
    assert kind == "estimate" and 300 <= sampled < 1_500
    assert 1_500 * 0.6 <= estimate <= 1_500 * 1.4, estimate


def test_estimated_total_is_labelled_in_both_outputs(run_dir: Path, tmp_path: Path, capsys, monkeypatch, reference) -> None:
    write_chunks(run_dir / "bam_pass", reference, chunks=1, reads_per_chunk=1_500, stem=f"{STEM}_big")
    (run_dir / "bam_pass" / f"{STEM}_big_0.bam").rename(run_dir / "bam_pass" / f"{STEM}_4.bam")
    monkeypatch.setattr(inventory_command, "SAMPLE_RECORDS", 300)
    monkeypatch.setattr(inventory_command, "SAMPLE_BYTES", 1)
    code, payload = _json(capsys, str(run_dir / "bam_pass"), "--root", str(tmp_path / "root"))
    assert code == 0
    records = payload["data"]["records"]
    assert records["kind"] == "estimate" and records["counted_chunks"] == 5
    assert 200 + 900 <= records["total"] <= 200 + 2_100
    code, out = _run(capsys, str(run_dir / "bam_pass"), "--root", str(tmp_path / "root"))
    sampled = min(e["sampled_records"] for e in payload["data"]["entries"] if e["records_kind"] == "estimate")
    assert sampled >= 300
    assert "READS       ~" in out
    assert f"(estimate: each estimated chunk's first records, at least {sampled:,}, extrapolated by compressed size; 4 chunk(s) exact)" in out


def test_missing_ranges() -> None:
    assert missing_ranges([]) == []
    assert missing_ranges([0, 1, 2]) == []
    assert missing_ranges([1, 2]) == [(0, 0)]
    assert missing_ranges([0, 3, 4, 8]) == [(1, 2), (5, 7)]
    assert inventory_command.format_ranges([(1, 2), (5, 5)]) == "1-2, 5"


def test_header_provenance_hides_identifier_values() -> None:
    rg = ("@RG\tID:run1_model\tDS:runid=run1 basecall_model=modelA modbase_models=modA,modB\tSM:donor\tLB:lib\tPL:ONT",)
    pg = ("@PG\tID:basecaller\tPN:dorado\tVN:7.3.9\tCL:dorado basecaller /Users/someone/pod5",
          "@PG\tID:samtools\tPN:samtools\tVN:1.20")
    prov = header_provenance(rg, pg)
    assert prov["basecall_models"] == ["modelA"] and prov["modbase_models"] == ["modA", "modB"]
    assert prov["programs"] == [{"name": "dorado", "version": "7.3.9"}, {"name": "samtools", "version": "1.20"}]
    assert prov["run_id"]["present"] and prov["sample_id"]["present"]
    text = json.dumps(prov)
    for secret in ("run1", "donor", "lib", "/Users", "pod5"):
        assert secret not in text
    assert len(prov["sample_id"]["sha256_12"]) == 2  # SM and LB each hashed
    assert prov["run_id"]["sha256_12"] == [hashlib.sha256(b"run1").hexdigest()[:12]]
    assert rg_line()["DS"].startswith(f"runid={RUN_ID} ")


def test_report_parsers(tmp_path: Path) -> None:
    final = _final_summary(tmp_path, 12, fallback=1)
    parsed = parse_final_summary(final)
    assert parsed["declared_bam_files"] == 13 and parsed["basecalling_enabled"] == "1"
    assert parsed["run_id"]["present"] and len(parsed["run_id"]["sha256_12"]) == 2
    assert SAMPLE_ID not in json.dumps(parsed) and RUN_ID not in json.dumps(parsed)
    summary = _sequencing_summary(tmp_path, ["a_0.bam", "a_1.bam"], 3, fail_rows=2)
    parsed = parse_sequencing_summary(summary)
    assert parsed == {
        "kind": "sequencing_summary", "declared_reads": 8, "declared_pass_reads": 6,
        "declared_bam_files": 3, "declared_pass_bam_files": ["a_0.bam", "a_1.bam"],
        "unparseable_names": 0, "passes_filtering_column": True,
        "run_id": {"present": True, "sha256_12": [hashlib.sha256(RUN_ID.encode()).hexdigest()[:12]]},
    }
    bare = tmp_path / "sequencing_summary_bare.txt"
    bare.write_text("read_id\tsequence_length_template\nr1\t100\nr2\t120\n")
    parsed = parse_sequencing_summary(bare)
    assert parsed["declared_pass_bam_files"] is None and parsed["declared_reads"] == 2
    assert parsed["passes_filtering_column"] is False and parsed["run_id"] == {"present": False, "sha256_12": []}


def test_report_without_run_id_is_compared_only_when_explicit(run_dir: Path, tmp_path: Path, capsys) -> None:
    report = run_dir / "final_summary_SYNTH_abcdef.txt"
    report.write_text("bam_files_in_final_dest=4\nbam_files_in_fallback=0\n")
    code, payload = _json(capsys, str(run_dir / "bam_pass"), "--root", str(tmp_path / "root"))
    assert code == 0
    data = payload["data"]
    assert data["verdict"] == "UNKNOWN"
    assert data["reasons"] == ["final_summary_SYNTH_abcdef.txt: run id not declared on report; not compared"]
    comparison = data["comparisons"][0]
    assert comparison["run_id_match"] is None and comparison["comparable"] is False and comparison["agrees"] is None
    code, out = _run(capsys, str(run_dir / "bam_pass"), "--root", str(tmp_path / "root"))
    assert "REPORT      final_summary_SYNTH_abcdef.txt (parent directory): run id not declared on report; not compared" in out
    code, payload = _json(capsys, str(run_dir / "bam_pass"), "--report", str(report), "--root", str(tmp_path / "root"))
    assert code == 0, payload
    data = payload["data"]
    assert data["verdict"] == "COMPLETE" and data["reports"][0]["explicit"] is True
    comparison = data["comparisons"][0]
    assert comparison["run_id_match"] is None and comparison["comparable"] is True and comparison["agrees"] is True
    assert comparison["notes"] == ["final_summary_SYNTH_abcdef.txt: run id not verified (explicit --report)"]
    code, out = _run(capsys, str(run_dir / "bam_pass"), "--report", str(report), "--root", str(tmp_path / "root"))
    assert "declares 4 BAM file(s); 4 on disk; run id not verified (explicit --report)" in out


def test_chunks_without_run_id_are_not_compared_unless_explicit(tmp_path: Path, capsys, reference) -> None:
    run = tmp_path / "run"
    rg = {"ID": "bare", "DS": "basecall_model=modelX modbase_models=modX", "PL": "ONT"}
    write_chunks(run / "bam_pass", reference, chunks=2, reads_per_chunk=5, rg=rg)
    report = _final_summary(run, 2)
    code, payload = _json(capsys, str(run / "bam_pass"), "--root", str(tmp_path / "root"))
    assert code == 0 and payload["data"]["verdict"] == "UNKNOWN"
    assert payload["data"]["provenance"]["run_id"] == {"present": False, "sha256_12": []}
    assert payload["data"]["reasons"] == ["final_summary_SYNTH_abcdef.txt: run id not declared on chunks; not compared"]
    code, payload = _json(capsys, str(run / "bam_pass"), "--report", str(report), "--root", str(tmp_path / "root"))
    assert code == 0 and payload["data"]["verdict"] == "COMPLETE"


def test_barcoded_bam_fail_sibling_is_counted_recursively(run_dir: Path, tmp_path: Path, capsys, reference) -> None:
    write_chunks(run_dir / "bam_fail" / "barcode01", reference, chunks=2, reads_per_chunk=5, stem="SYNTH0001_fail_barcode01", seed=9)
    (run_dir / "bam_fail" / "merged_fail.bam").write_bytes(b"unindexed, not counted")
    (run_dir / "bam_fail" / "barcode01" / "SYNTH0001_fail_barcode01_9.bam").symlink_to(
        run_dir / "bam_fail" / "barcode01" / "SYNTH0001_fail_barcode01_0.bam"
    )
    _final_summary(run_dir, 6)
    code, payload = _json(capsys, str(run_dir / "bam_pass"), "--root", str(tmp_path / "root"))
    assert code == 0, payload
    data = payload["data"]
    assert data["verdict"] == "COMPLETE" and data["on_disk_bam_files"] == 6
    assert [(g["label"], g["bam_files"], g["unindexed"], g["where"]) for g in data["fail_groups"]] == [
        ("bam_fail", 0, 1, "sibling"), ("bam_fail/barcode01", 2, 0, "sibling"),
    ]
    assert {(s["group"], s["name"], s["kind"]) for s in data["skipped"]} == {
        ("bam_fail/barcode01", "SYNTH0001_fail_barcode01_9.bam", "a symlink"),
    }
    assert "merged_fail" not in _public(payload)
    code, out = _run(capsys, str(run_dir / "bam_pass"), "--root", str(tmp_path / "root"))
    assert "FAIL        bam_fail (sibling): 0 indexed .bam file(s), fail output, not inventoried; 1 unindexed not counted" in out
    assert "FAIL        bam_fail/barcode01 (sibling): 2 indexed .bam file(s), fail output, not inventoried" in out


def test_operator_named_report_is_printed_as_a_digest(run_dir: Path, tmp_path: Path, capsys) -> None:
    report = tmp_path / "donor notes final.txt"
    report.write_text(f"acquisition_run_id={RUN_ID}\nbam_files_in_final_dest=4\n")
    code, payload = _json(capsys, str(run_dir / "bam_pass"), "--report", str(report), "--root", str(tmp_path / "root"))
    assert code == 0, payload
    digest = hashlib.sha256(report.name.encode()).hexdigest()[:12]
    entry = payload["data"]["reports"][0]
    assert entry["name"] == f"final_summary (sha256 {digest})" and entry["name_sha256_12"] == digest
    assert payload["data"]["verdict"] == "COMPLETE"
    assert "donor" not in _public(payload)
    code, out = _run(capsys, str(run_dir / "bam_pass"), "--report", str(report), "--root", str(tmp_path / "root"))
    assert f"REPORT      final_summary (sha256 {digest}) (--report): declares 4 BAM file(s); 4 on disk" in out
    assert "donor" not in out
    assert inventory_command.report_label("sequencing_summary_FAX12345_abc.txt", "sequencing_summary") == "sequencing_summary_FAX12345_abc.txt"
    assert inventory_command.report_label("final_summary.txt", "final_summary").startswith("final_summary (sha256 ")


def test_hash_failure_marks_duplicate_check_incomplete(run_dir: Path, tmp_path: Path, capsys, monkeypatch) -> None:
    real = inventory_command._sha256_file

    def flaky(path: Path) -> str:
        if path.name.endswith("_1.bam"):
            raise OSError(5, "input/output error")
        return real(path)

    monkeypatch.setattr(inventory_command, "_sha256_file", flaky)
    _final_summary(run_dir, 4)
    code, payload = _json(capsys, str(run_dir / "bam_pass"), "--root", str(tmp_path / "root"))
    assert code == 0
    data = payload["data"]
    assert data["hashing"] is True and data["duplicate_content_checked"] is False and data["hash_failures"] == 1
    failed = next(e for e in data["entries"] if e["index"] == 1)
    assert failed["sha256"] is None and failed["hash_failed"] is True and failed["records"] == 50
    code, out = _run(capsys, str(run_dir / "bam_pass"), "--root", str(tmp_path / "root"))
    assert "DUPLICATES  1 chunk(s) could not be hashed; duplicate content not fully checked" in out
    assert "  FAILED  " in out


def test_report_discovery_ignores_symlinks_and_refuses_ambiguity(run_dir: Path, tmp_path: Path, capsys) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    real = _final_summary(elsewhere, 4)
    (run_dir / "final_summary_SYNTH_link.txt").symlink_to(real)
    code, payload = _json(capsys, str(run_dir / "bam_pass"), "--root", str(tmp_path / "root"))
    assert code == 0 and payload["data"]["reports"] == [] and payload["data"]["verdict"] == "UNKNOWN"
    (run_dir / "final_summary_SYNTH_link.txt").unlink()
    _final_summary(run_dir, 4)
    shutil.copyfile(run_dir / "final_summary_SYNTH_abcdef.txt", run_dir / "final_summary_SYNTH_second.txt")
    code, payload = _json(capsys, str(run_dir / "bam_pass"), "--root", str(tmp_path / "root"))
    assert code == 0
    data = payload["data"]
    assert data["verdict"] == "UNKNOWN"
    [report] = data["reports"]
    assert report["kind"] == "ambiguous" and report["candidates"] == 2 and report["pattern"] == "final_summary"
    assert report["name"] == "final_summary (ambiguous)" and report["where"] == "parent directory"
    comparison = data["comparisons"][0]
    assert comparison["comparable"] is False and comparison["agrees"] is None
    assert data["reasons"] == ["ambiguous reports: 2 final_summary files in parent directory; not compared"]
    code, out = _run(capsys, str(run_dir / "bam_pass"), "--root", str(tmp_path / "root"))
    assert "REPORT      final_summary (ambiguous) (parent directory): 2 final_summary files; ambiguous, not compared" in out


def test_mistyped_report_is_tbx_lab_005(run_dir: Path, tmp_path: Path, capsys) -> None:
    code, payload = _json(capsys, str(run_dir / "bam_pass"), "--report", str(tmp_path / "final_sumary.txt"), "--root", str(tmp_path / "root"))
    assert code == 3 and payload["data"]["code"] == "TBX-LAB-005"
    assert payload["data"]["argument"] == "--report" and payload["data"]["kind"] == "missing"
    assert str(tmp_path) not in _public(payload)
    real = _final_summary(tmp_path, 4)
    link = tmp_path / "final_summary_link.txt"
    link.symlink_to(real)
    code, payload = _json(capsys, str(run_dir / "bam_pass"), "--report", str(link), "--root", str(tmp_path / "root"))
    assert code == 3 and payload["data"]["kind"] == "a symlink"
    code, payload = _json(capsys, str(run_dir / "bam_pass"), "--report", str(tmp_path), "--root", str(tmp_path / "root"))
    assert code == 3 and payload["data"]["kind"] == "not a regular file"


def test_help_and_guide_list_the_command(capsys) -> None:
    with pytest.raises(SystemExit):
        signed_cli.main(["lab", "inventory", "--help"])
    help_text = " ".join(capsys.readouterr().out.split())
    assert "--recursive" in help_text and "--report PATH" in help_text and "--no-hash" in help_text
    guide = (Path(__file__).resolve().parents[2] / "docs" / "LAB-GUIDE.md").read_text()
    assert "traceback lab inventory DIR" in guide
    assert "## Inventory before you align" in guide
    assert guide.index("## Inventory before you align") < guide.index("`traceback lab run <chunk dir>` takes one file")
    assert "--no-hash" in guide and "bam_fail" in guide
