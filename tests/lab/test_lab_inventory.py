"""``traceback lab inventory DIR``: chunk-set completeness before alignment (TBX-LAB-031)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from traceback_lab import ingest, inventory_command
from traceback_lab.inventory_command import (
    duplicates,
    estimate_records,
    header_provenance,
    missing_ranges,
    parse_final_summary,
    parse_sequencing_summary,
)
from traceback_lab.store import LabStore
from traceback_lab.synthetic import write_chunks, write_reference
from traceback_runner import cli as signed_cli

STEM = "SYNTH0001_pass_synthetic"
SAMPLE_ID = "synthetic-sample-never-printed"


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


def _final_summary(run: Path, bam_files: int, *, fallback: int = 0) -> Path:
    path = run / "final_summary_SYNTH_abcdef.txt"
    path.write_text(
        f"sample_id={SAMPLE_ID}\nprotocol_run_id=00000000-0000-0000-0000-000000000000\n"
        f"basecalling_enabled=1\nbam_files_in_final_dest={bam_files}\nbam_files_in_fallback={fallback}\n"
    )
    return path


def _sequencing_summary(run: Path, names: list[str], reads_per_chunk: int, *, fail_rows: int = 0) -> Path:
    path = run / "sequencing_summary_SYNTH_abcdef.txt"
    rows = ["filename_bam\tread_id\tpasses_filtering\tsequence_length_template"]
    serial = 0
    for name in names:
        for _ in range(reads_per_chunk):
            rows.append(f"{name}\tread-{serial}\tTRUE\t170")
            serial += 1
    for _ in range(fail_rows):
        rows.append(f"{STEM}_fail_0.bam\tread-{serial}\tFALSE\t90")
        serial += 1
    path.write_text("\n".join(rows) + "\n")
    return path


def _run(capsys, *argv: str) -> tuple[int, str]:
    code = signed_cli.main(["lab", "inventory", *argv])
    return code, capsys.readouterr().out


def _json(capsys, *argv: str) -> tuple[int, dict]:
    code, out = _run(capsys, *argv, "--json")
    return code, json.loads(out)


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
    assert SAMPLE_ID not in out and "synthetic-run" not in out
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
    assert data["missing_indexes"] == [[2, 2], [5, 6]] and data["missing_index_count"] == 3
    assert data["reasons"] == ["missing 3 index(es): 2, 5-6"]
    assert data["chunks"] == 5 and data["index_min"] == 0 and data["index_max"] == 7
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
    assert data["verdict"] == "DAMAGED" and data["truncated"] == [f"{STEM}_1.bam"]
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
    assert data["verdict"] == "DAMAGED"
    assert data["duplicates"] == [{
        "kind": "same_content", "names": [f"{STEM}_0.bam", f"{STEM}_4.bam"],
        "sha256_12": data["entries"][0]["sha256"][:12],
    }]
    assert data["missing_indexes"] == []
    code, out = _run(capsys, str(chunks), "--root", str(tmp_path / "root"))
    assert f"DUPLICATES  {STEM}_0.bam = {STEM}_4.bam (same content, sha256 " in out


def test_same_index_different_content_is_a_duplicate() -> None:
    a = inventory_command.Entry(name="x_1.bam", path=Path("x_1.bam"), index=1, size=1, sha256="a" * 64)
    b = inventory_command.Entry(name="sub/x_1.bam", path=Path("sub/x_1.bam"), index=1, size=1, sha256="b" * 64)
    assert duplicates([a, b]) == [{"kind": "same_index", "names": ["x_1.bam", "sub/x_1.bam"], "index": 1}]


def test_final_summary_mismatch_is_incomplete(run_dir: Path, tmp_path: Path, capsys) -> None:
    _final_summary(run_dir, 6)
    code, payload = _json(capsys, str(run_dir / "bam_pass"), "--root", str(tmp_path / "root"))
    assert code == 8
    data = payload["data"]
    assert data["verdict"] == "INCOMPLETE" and data["missing_indexes"] == []
    assert data["reasons"] == ["final_summary_SYNTH_abcdef.txt declares 6 BAM file(s); 4 on disk"]
    assert data["comparisons"][0]["agrees"] is False
    report = data["reports"][0]
    assert report["sample_id_present"] is True and report["protocol_run_id_present"] is True
    assert SAMPLE_ID not in json.dumps(payload)


def test_final_summary_counts_the_bam_fail_sibling(run_dir: Path, tmp_path: Path, capsys) -> None:
    (run_dir / "bam_fail").mkdir()
    (run_dir / "bam_fail" / f"{STEM}_fail_0.bam").write_bytes(b"not inspected")
    _final_summary(run_dir, 5)
    code, payload = _json(capsys, str(run_dir / "bam_pass"), "--root", str(tmp_path / "root"))
    assert code == 0
    data = payload["data"]
    assert data["verdict"] == "COMPLETE" and data["sibling"] == {"name": "bam_fail", "bam_files": 1}
    assert data["comparisons"][0]["on_disk_bam_files"] == 5


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
    assert report["declared_pass_bam_files"] == names
    comparison = data["comparisons"][0]
    assert comparison["agrees"] is True and comparison["missing_on_disk"] == []
    code, out = _run(capsys, str(run_dir / "bam_pass"), "--root", str(tmp_path / "root"))
    assert "REPORT      sequencing_summary_SYNTH_abcdef.txt (parent directory): declares 207 read(s), 200 pass, 4 pass chunk(s); names match" in out
    (run_dir / "bam_pass" / names[3]).unlink()
    code, payload = _json(capsys, str(run_dir / "bam_pass"), "--root", str(tmp_path / "root"))
    assert code == 8 and payload["data"]["verdict"] == "INCOMPLETE"
    assert payload["data"]["comparisons"][0]["missing_on_disk"] == [names[3]]
    assert payload["data"]["reasons"] == ["sequencing_summary_SYNTH_abcdef.txt lists 1 pass chunk(s) not on disk"]


def test_explicit_report_path_is_used_and_only_its_name_printed(run_dir: Path, tmp_path: Path, capsys) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    report = _final_summary(elsewhere, 4)
    code, out = _run(capsys, str(run_dir / "bam_pass"), "--report", str(report), "--root", str(tmp_path / "root"))
    assert code == 0
    assert f"REPORT      {report.name} (--report): declares 4 BAM file(s); 4 on disk" in out
    assert "elsewhere" not in out
    code, payload = _json(capsys, str(run_dir / "bam_pass"), "--report", str(report), "--root", str(tmp_path / "root"))
    public = json.dumps({key: value for key, value in payload["data"].items() if key not in {"root", "dir"}})
    assert str(tmp_path) not in public and "elsewhere" not in public


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
    assert set(data) >= {
        "verdict", "reasons", "chunks", "total_bytes", "index_min", "index_max", "missing_indexes",
        "missing_index_count", "unindexed", "truncated", "duplicates", "records", "provenance",
        "hash_cached_chunks", "sibling", "reports", "comparisons", "skipped", "entries", "recursive",
    }
    assert data["records"] == {"total": 200, "kind": "exact", "counted_chunks": 4, "sample_records": 2000}
    assert data["total_bytes"] == sum(entry["bytes"] for entry in data["entries"])
    entry = data["entries"][0]
    assert set(entry) == {
        "name", "index", "bytes", "eof_marker", "quickcheck", "header_ok", "truncated", "aligned",
        "sha256", "records", "records_kind", "hash_cached", "provenance",
    }
    assert len(entry["sha256"]) == 64 and entry["aligned"] is False
    prov = data["provenance"]
    assert prov["basecall_models"] == ["dna_r10.4.1_e8.2_400bps_sup@v5.0.0"]
    assert prov["programs"] == [{"name": "synthetic", "version": "0"}]
    assert prov["run_id"]["present"] and len(prov["run_id"]["sha256_12"][0]) == 12
    assert prov["sample_id"]["present"] and len(prov["sample_id"]["sha256_12"][0]) == 12
    # No value of an identifier and no host path beyond ROOT and DIR as given.
    public = json.dumps({key: value for key, value in data.items() if key not in {"root", "dir"}})
    assert "synthetic-run" not in public
    assert str(tmp_path) not in public


def test_empty_dir_is_tbx_lab_030(tmp_path: Path, capsys) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    (empty / "notes.txt").write_text("not a chunk\n")
    code, payload = _json(capsys, str(empty), "--root", str(tmp_path / "root"))
    assert code == 3
    assert payload["status"] == "blocked" and payload["data"]["code"] == "TBX-LAB-031"
    assert payload["data"]["docs"] == "docs/LAB-GUIDE.md#tbx-lab-031"
    assert payload["data"]["dir"] == str(empty) and payload["data"]["skipped"] == []
    code, out = _run(capsys, str(empty), "--root", str(tmp_path / "root"))
    assert code == 3 and "CODE       TBX-LAB-031" in out and "CAUSE      DIR (empty) holds no readable .bam chunk" in out
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
    assert data["chunks"] == 4 and data["skipped"] == [{"name": f"{STEM}_9.bam", "kind": "a symlink"}]
    assert data["duplicates"] == [] and data["index_max"] == 3
    code, out = _run(capsys, str(chunks), "--root", str(tmp_path / "root"))
    assert f"SKIPPED     {STEM}_9.bam (a symlink); lab run refuses these" in out


def test_recursive_scans_subdirectories_with_relative_names(run_dir: Path, tmp_path: Path, capsys) -> None:
    _final_summary(run_dir, 4)
    code, payload = _json(capsys, str(run_dir), "--root", str(tmp_path / "root"))
    assert code == 3 and payload["data"]["code"] == "TBX-LAB-031"
    assert "searched recursively" not in payload["data"]["cause"]
    code, payload = _json(capsys, str(run_dir), "--recursive", "--root", str(tmp_path / "root"))
    assert code == 0, payload
    data = payload["data"]
    assert data["recursive"] is True and data["chunks"] == 4
    assert [entry["name"] for entry in data["entries"]] == [f"bam_pass/{STEM}_{index}.bam" for index in range(4)]
    assert data["reports"][0]["where"] == "DIR" and data["verdict"] == "COMPLETE"


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
    exact, kind = estimate_records(chunk, chunk.stat().st_size)
    assert (exact, kind) == (1_500, "exact")
    monkeypatch.setattr(inventory_command, "SAMPLE_RECORDS", 300)
    monkeypatch.setattr(inventory_command, "SAMPLE_BYTES", 1)
    estimate, kind = estimate_records(chunk, chunk.stat().st_size)
    assert kind == "estimate"
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
    assert "READS       ~" in out and "(estimate from the first 300 records of each chunk; 4 chunk(s) exact)" in out


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


def test_report_parsers(tmp_path: Path) -> None:
    final = _final_summary(tmp_path, 12, fallback=1)
    parsed = parse_final_summary(final)
    assert parsed["declared_bam_files"] == 13 and parsed["basecalling_enabled"] == "1"
    assert SAMPLE_ID not in json.dumps(parsed)
    summary = _sequencing_summary(tmp_path, ["a_0.bam", "a_1.bam"], 3, fail_rows=2)
    parsed = parse_sequencing_summary(summary)
    assert parsed == {
        "kind": "sequencing_summary", "declared_reads": 8, "declared_pass_reads": 6,
        "declared_bam_files": 3, "declared_pass_bam_files": ["a_0.bam", "a_1.bam"],
    }
    bare = tmp_path / "sequencing_summary_bare.txt"
    bare.write_text("read_id\tsequence_length_template\nr1\t100\nr2\t120\n")
    assert parse_sequencing_summary(bare)["declared_pass_bam_files"] is None
    assert parse_sequencing_summary(bare)["declared_reads"] == 2


def test_help_and_guide_list_the_command(capsys) -> None:
    with pytest.raises(SystemExit):
        signed_cli.main(["lab", "inventory", "--help"])
    help_text = " ".join(capsys.readouterr().out.split())
    assert "--recursive" in help_text and "--report PATH" in help_text
    guide = (Path(__file__).resolve().parents[2] / "docs" / "LAB-GUIDE.md").read_text()
    assert "traceback lab inventory DIR" in guide
    assert "## Inventory before you align" in guide
    assert guide.index("## Inventory before you align") < guide.index("`traceback lab run <chunk dir>` takes one file")
