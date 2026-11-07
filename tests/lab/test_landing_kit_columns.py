"""TASTE T1: kit columns on the landing samples table, from stored header provenance."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from tests.lab.test_landing import _dom, needs_chrome
from traceback_lab.ingest import header_provenance, stored_header_provenance
from traceback_lab.store import LabStore
from traceback_lab.synthetic import write_chunks, write_reference
from traceback_lab.web.api import LabApi
from traceback_lab.web.server import RunningLabServer
from traceback_runner import cli as signed_cli

KIT_RG = {
    "ID": "synthrun_model",
    "DS": ("runid=synthrun basecall_model=dna_r10.4.1_e8.2_400bps_sup@v5.0.0 "
           "flow_cell_product_code=FLO-SYN114 sequencing_kit=SQK-SYN114"),
    "PL": "ONT", "LB": "synthlib", "SM": "synthsample", "PU": "SYNFC0001", "PM": "SYNDEV1",
}
BARE_RG = {"ID": "bare", "PL": "ONT"}
COLUMNS = ["ID", "Run date", "Approx depth", "Flow cell", "Library kit", "Basecall model", "Software",
           "OK / skipped / failed", "Outside a reference", "Controls", ""]


def test_header_provenance_keeps_codes_and_versions_only() -> None:
    header = "\n".join([
        "@HD\tVN:1.6",
        "@RG\tID:run9_m\tPU:SYNFC0001\tPM:SYNDEV1\tSM:someone\tLB:lib\tDT:2026-01-01"
        "\tDS:runid=run9 basecall_model=m@v1 flow_cell_type=FLO-SYN114 library_kit=SQK-SYN114",
        "@PG\tID:basecaller\tPN:dorado\tVN:0.9.1+abc\tCL:dorado basecaller /some/path/model",
        "@PG\tID:basecaller.1\tPN:dorado\tVN:0.9.1+abc",
        "@PG\tID:samtools\tPN:samtools\tVN:1.21",
    ])
    assert header_provenance(header) == {
        "flow_cell": "FLO-SYN114", "kit": "SQK-SYN114", "basecall_model": "m@v1",
        "software": [{"program": "dorado", "version": "0.9.1+abc"}, {"program": "samtools", "version": "1.21"}],
    }
    rendered = json.dumps(header_provenance(header))
    for identifier in ("SYNFC0001", "SYNDEV1", "someone", "lib", "run9", "2026-01-01", "/some/path"):
        assert identifier not in rendered


def test_header_provenance_drops_path_and_free_text_values() -> None:
    header = ("@RG\tID:x\tDS:sequencing_kit=/Users/x/kit flow_cell_type=a/b basecall_model=ok\n"
              "@PG\tID:p\tPN:my tool\tVN:1\n@PG\tID:q\tPN:tool\tVN:C:\\\\x")
    assert header_provenance(header) == {
        "flow_cell": None, "kit": None, "basecall_model": "ok", "software": [{"program": "tool", "version": None}],
    }
    assert header_provenance("") == {"flow_cell": None, "kit": None, "basecall_model": None, "software": []}


def test_stored_header_provenance_revalidates_and_handles_legacy_manifests() -> None:
    assert stored_header_provenance(None) is None
    legacy = stored_header_provenance({"basecall_model": "mA@v1|mB@v1"})
    assert legacy == {"flow_cell": None, "kit": None, "basecall_model": "mA@v1 | mB@v1", "software": []}
    edited = stored_header_provenance({"header_provenance": {
        "flow_cell": "FLO-SYN114", "kit": "../../etc", "basecall_model": None,
        "software": [{"program": "dorado", "version": "has space"}, "junk", {"program": None}],
    }})
    assert edited == {"flow_cell": "FLO-SYN114", "kit": None, "basecall_model": None,
                      "software": [{"program": "dorado", "version": None}]}


@pytest.fixture(scope="module")
def kit_root(tmp_path_factory) -> Path:
    from traceback_runner.references import register_reference

    base = tmp_path_factory.mktemp("kit-cols")
    reference = write_reference(base / "ref.fa", length=30_000)
    write_chunks(base / "with-kit", reference, chunks=2, reads_per_chunk=600, seed=21, rg=KIT_RG)
    write_chunks(base / "bare", reference, chunks=2, reads_per_chunk=600, seed=22, rg=BARE_RG)
    root = base / "root"
    register_reference(root, reference.fasta, "synth")
    for name in ("with-kit", "bare"):
        assert signed_cli.main(["lab", "run", str(base / name), "--root", str(root), "--json"]) == 0
    return root


def _rows_by_flow_cell(root: Path) -> dict[str | None, dict]:
    rows = LabApi(LabStore(root)).samples()["samples"]
    assert len(rows) == 2
    return {row["kit"]["flow_cell"]: row for row in rows}


def test_manifest_records_provenance_and_samples_projection_carries_it(kit_root) -> None:
    rows = _rows_by_flow_cell(kit_root)
    kit = rows["FLO-SYN114"]["kit"]
    assert kit == {"flow_cell": "FLO-SYN114", "kit": "SQK-SYN114",
                   "basecall_model": "dna_r10.4.1_e8.2_400bps_sup@v5.0.0",
                   "software": [{"program": "synthetic", "version": "0"}]}
    assert rows[None]["kit"] == {"flow_cell": None, "kit": None, "basecall_model": None,
                                 "software": [{"program": "synthetic", "version": "0"}]}
    stored = [json.loads(path.read_text()) for path in LabStore(kit_root).inputs_dir.glob("*/manifest.json")]
    assert {(m.get("header_provenance") or {}).get("flow_cell") for m in stored} >= {"FLO-SYN114", None}
    projection = json.dumps(LabApi(LabStore(kit_root)).samples())
    for identifier in ("SYNFC0001", "SYNDEV1", "synthsample", "synthlib", "synthrun_model", "runid"):
        assert identifier not in projection


def test_projection_is_read_from_the_manifest_not_the_bam(kit_root, monkeypatch) -> None:
    from traceback_lab import ingest

    def refuse(*args, **kwargs):
        raise AssertionError("samples() re-parsed header provenance from a BAM")

    monkeypatch.setattr(ingest, "header_provenance", refuse)
    monkeypatch.setattr(ingest, "read_header_lines", refuse)
    assert _rows_by_flow_cell(kit_root)["FLO-SYN114"]["kit"]["kit"] == "SQK-SYN114"


def _samples_table(dom: str) -> str:
    match = re.search(r'<section[^>]*aria-labelledby="samples-title".*?</section>', dom, re.S)
    assert match, "samples table missing"
    return match.group(0)


@needs_chrome
def test_landing_samples_table_shows_kit_columns(kit_root, tmp_path) -> None:
    with RunningLabServer(LabStore(kit_root)) as server:
        dom = _dom(server, tmp_path, want=lambda page: "FLO-SYN114" in page)
    section = _samples_table(dom)
    assert 'class="table-wrap"' in section
    head = re.search(r"<thead>(.*?)</thead>", section, re.S).group(1)
    headers = re.findall(r'<th scope="col">(.*?)</th>', head)
    assert headers == COLUMNS
    body = re.search(r"<tbody>(.*?)</tbody>", section, re.S).group(1)
    rows = re.findall(r"<tr>(.*?)</tr>", body, re.S)
    assert len(rows) == 2
    with_kit = next(row for row in rows if "FLO-SYN114" in row)
    bare = next(row for row in rows if "FLO-SYN114" not in row)
    cells = lambda row: re.findall(r'<td class="kit-cell[^"]*">(.*?)</td>', row)  # noqa: E731
    assert cells(with_kit) == ["FLO-SYN114", "SQK-SYN114", "dna_r10.4.1_e8.2_400bps_sup@v5.0.0", "synthetic 0"]
    assert cells(bare) == ["not recorded", "not recorded", "not recorded", "synthetic 0"]
    for identifier in ("SYNFC0001", "SYNDEV1", "synthsample", "synthlib"):
        assert identifier not in section


@needs_chrome
def test_kit_columns_scroll_inside_the_table_wrap_at_phone_width(kit_root, tmp_path) -> None:
    from tests.lab.test_landing import _body

    with RunningLabServer(LabStore(kit_root)) as server:
        dom = _dom(server, tmp_path, size="360,740")
    assert _body(dom)["page-scroll-x"] == "false"
    assert "FLO-SYN114" in _samples_table(dom)
