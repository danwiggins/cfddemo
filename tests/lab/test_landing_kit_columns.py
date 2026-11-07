"""TASTE T1: kit columns on the landing samples table, from stored header provenance."""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import pytest

from tests.lab.test_landing import _body, _dom, needs_chrome
from traceback_lab.ingest import header_provenance, merge_header_provenance, stored_header_provenance
from traceback_lab.store import LabStore
from traceback_lab.synthetic import SYNTHETIC_MODEL, write_chunks, write_reference
from traceback_lab.web.api import LabApi
from traceback_lab.web.server import RunningLabServer
from traceback_runner import cli as signed_cli


def _rg(run: str, flow_cell: str, kit: str) -> dict[str, str]:
    return {
        "ID": f"{run}_model",
        "DS": f"runid={run} basecall_model={SYNTHETIC_MODEL} flow_cell_product_code={flow_cell} sequencing_kit={kit}",
        "PL": "ONT", "LB": "synthlib", "SM": "synthsample", "PU": "SYNFC0001", "PM": "SYNDEV1",
    }


KIT_RG = _rg("synthrun", "FLO-SYN114", "SQK-SYN114")
BARE_RG = {"ID": "bare", "PL": "ONT"}
COLUMNS = ["ID", "Run date", "Approx depth", "Flow cell", "Library kit", "Basecall model", "Software",
           "OK / skipped / failed", "Outside a reference", "Controls", ""]
NOT = [None]
SYNTH_SOFTWARE = [{"program": "synthetic", "version": "0"}]


def test_header_provenance_keeps_shaped_codes_and_versions_only() -> None:
    header = "\n".join([
        "@HD\tVN:1.6",
        "@RG\tID:run9_m\tPU:SYNFC0001\tPM:SYNDEV1\tSM:someone\tLB:lib\tDT:2026-01-01"
        "\tDS:runid=run9 basecall_model=dna_r10.4.1_e8.2_400bps_sup@v5.0.0 flow_cell_type=FLO-SYN114"
        " library_kit=SQK-SYN114",
        "@PG\tID:basecaller\tPN:dorado\tVN:0.9.1+abc\tCL:dorado basecaller /some/path/model",
        "@PG\tID:basecaller.1\tPN:dorado\tVN:0.9.1+abc",
        "@PG\tID:samtools\tPN:samtools\tVN:1.21",
    ])
    assert header_provenance(header) == {
        "flow_cell": ["FLO-SYN114"], "kit": ["SQK-SYN114"], "basecall_model": ["dna_r10.4.1_e8.2_400bps_sup@v5.0.0"],
        "software": [{"program": "dorado", "version": "0.9.1+abc"}, {"program": "samtools", "version": "1.21"}],
    }
    rendered = json.dumps(header_provenance(header))
    for identifier in ("SYNFC0001", "SYNDEV1", "someone", "lib", "run9", "2026-01-01", "/some/path"):
        assert identifier not in rendered


def test_identifier_shaped_values_are_not_recorded() -> None:
    header = ("@RG\tID:x\tDS:flow_cell_type=PAW12345 kit=JaneDoe_P07 sequencing_kit=/Users/x/kit"
              " basecall_model=jane@example.org\n"
              "@PG\tID:p\tPN:my tool\tVN:1\n@PG\tID:q\tPN:tool\tVN:jane.doe@example.org\n"
              "@PG\tID:r\tPN:other\tVN:C:\\\\x")
    assert header_provenance(header) == {
        "flow_cell": NOT, "kit": NOT, "basecall_model": NOT,
        "software": [{"program": "other", "version": None}, {"program": "tool", "version": None}],
    }
    assert "@" not in json.dumps(header_provenance(header))
    assert header_provenance("") == {"flow_cell": NOT, "kit": NOT, "basecall_model": NOT, "software": []}


def test_read_groups_and_chunks_that_disagree_keep_every_value() -> None:
    one = header_provenance(
        "@RG\tID:a\tDS:flow_cell_type=FLO-AAA1 sequencing_kit=SQK-AAA1 basecall_model=dna_a@v1\n"
        "@RG\tID:b\tDS:flow_cell_type=FLO-BBB1 basecall_model=dna_a@v1\n@PG\tID:d\tPN:dorado\tVN:0.8.0")
    assert one["flow_cell"] == ["FLO-AAA1", "FLO-BBB1"]
    assert one["kit"] == ["SQK-AAA1", None]
    assert one["basecall_model"] == ["dna_a@v1"]
    two = header_provenance("@RG\tID:c\tDS:flow_cell_type=FLO-AAA1 sequencing_kit=SQK-AAA1 basecall_model=dna_b@v2\n"
                            "@PG\tID:d\tPN:dorado\tVN:0.9.0")
    merged = merge_header_provenance([one, two])
    assert merged["flow_cell"] == ["FLO-AAA1", "FLO-BBB1"]
    assert merged["kit"] == ["SQK-AAA1", None]
    assert merged["basecall_model"] == ["dna_a@v1", "dna_b@v2"]
    assert merged["software"] == [{"program": "dorado", "version": "0.8.0"}, {"program": "dorado", "version": "0.9.0"}]


def test_stored_header_provenance_revalidates_and_uses_legacy_model_only_when_absent() -> None:
    assert stored_header_provenance(None) is None
    legacy = stored_header_provenance({"basecall_model": "dna_a@v1|dna_b@v1"})
    assert legacy == {"flow_cell": NOT, "kit": NOT, "basecall_model": ["dna_a@v1", "dna_b@v1"], "software": []}
    recorded = stored_header_provenance({"basecall_model": "dna_a@v1", "header_provenance": {
        "flow_cell": ["FLO-SYN114", "PAW12345"], "kit": ["../../etc"], "basecall_model": [None],
        "software": [{"program": "dorado", "version": "has space"}, "junk", {"program": None}],
    }})
    assert recorded == {"flow_cell": ["FLO-SYN114", None], "kit": NOT, "basecall_model": NOT,
                        "software": [{"program": "dorado", "version": None}]}


@pytest.fixture(scope="module")
def kit_world(tmp_path_factory) -> dict[str, Path]:
    from traceback_runner.references import register_reference

    base = tmp_path_factory.mktemp("kit-cols")
    reference = write_reference(base / "ref.fa", length=30_000)
    write_chunks(base / "with-kit", reference, chunks=2, reads_per_chunk=600, seed=21, rg=KIT_RG)
    write_chunks(base / "bare", reference, chunks=2, reads_per_chunk=600, seed=22, rg=BARE_RG)
    write_chunks(base / "mixed", reference, chunks=2, reads_per_chunk=600, seed=23,
                 rg=_rg("synthA", "FLO-SYNA01", "SQK-SYNA01"))
    write_chunks(base / "mixed-b", reference, chunks=2, reads_per_chunk=600, seed=23,
                 rg=_rg("synthB", "FLO-SYNB01", "SQK-SYNB01"))
    shutil.copy2(base / "mixed-b" / "SYNTH0001_pass_synthetic_1.bam", base / "mixed" / "SYNTH0001_pass_synthetic_1.bam")
    root = base / "root"
    register_reference(root, reference.fasta, "synth")
    for name in ("with-kit", "bare", "mixed"):
        assert signed_cli.main(["lab", "run", str(base / name), "--root", str(root), "--json"]) == 0
    return {"base": base, "root": root, "fasta": reference.fasta}


def _rows(root: Path) -> dict[str, dict]:
    rows = LabApi(LabStore(root)).samples()["samples"]
    assert len(rows) == 3
    named = {"FLO-SYN114": "with-kit", None: "bare", "FLO-SYNA01": "mixed"}
    return {named[row["kit"]["flow_cell"][0]]: row for row in rows}


def test_manifest_records_provenance_and_samples_projection_carries_it(kit_world) -> None:
    rows = _rows(kit_world["root"])
    assert rows["with-kit"]["kit"] == {"flow_cell": ["FLO-SYN114"], "kit": ["SQK-SYN114"],
                                       "basecall_model": [SYNTHETIC_MODEL], "software": SYNTH_SOFTWARE}
    assert rows["bare"]["kit"] == {"flow_cell": NOT, "kit": NOT, "basecall_model": NOT, "software": SYNTH_SOFTWARE}
    assert rows["mixed"]["kit"] == {"flow_cell": ["FLO-SYNA01", "FLO-SYNB01"], "kit": ["SQK-SYNA01", "SQK-SYNB01"],
                                    "basecall_model": [SYNTHETIC_MODEL], "software": SYNTH_SOFTWARE}
    stored = [json.loads(path.read_text()) for path in LabStore(kit_world["root"]).inputs_dir.glob("*/manifest.json")]
    assert ["FLO-SYNA01", "FLO-SYNB01"] in [m.get("header_provenance", {}).get("flow_cell") for m in stored]
    projection = json.dumps(LabApi(LabStore(kit_world["root"])).samples())
    for identifier in ("SYNFC0001", "SYNDEV1", "synthsample", "synthlib", "synthrun_model", "runid"):
        assert identifier not in projection


def test_header_provenance_does_not_enter_input_or_source_identity(kit_world, tmp_path, monkeypatch) -> None:
    from traceback_lab import ingest
    from traceback_runner.references import register_reference

    def manifest_ids(root: Path, flow_cell: str | None) -> tuple[str, str]:
        found = [json.loads(path.read_text()) for path in LabStore(root).inputs_dir.glob("*/manifest.json")]
        match = [m for m in found if (m.get("header_provenance") or {}).get("flow_cell", [None])[0] == flow_cell
                 and m.get("kind") == "aligned"]
        assert len(match) == 1, match
        return match[0]["input_id"], match[0]["source_id"]

    with_field = manifest_ids(kit_world["root"], "FLO-SYN114")
    root = tmp_path / "root"
    register_reference(root, kit_world["fasta"], "synth")
    monkeypatch.setattr(ingest, "header_provenance", lambda text: {})
    assert signed_cli.main(["lab", "run", str(kit_world["base"] / "with-kit"), "--root", str(root), "--json"]) == 0
    without_field = manifest_ids(root, None)
    assert without_field == with_field


def test_projection_is_read_from_the_manifest_not_the_bam(kit_world, monkeypatch) -> None:
    from traceback_lab import ingest

    def refuse(*args, **kwargs):
        raise AssertionError("samples() re-parsed header provenance from a BAM")

    monkeypatch.setattr(ingest, "header_provenance", refuse)
    monkeypatch.setattr(ingest, "read_header_lines", refuse)
    assert _rows(kit_world["root"])["with-kit"]["kit"]["kit"] == ["SQK-SYN114"]


def _samples_table(dom: str) -> str:
    match = re.search(r'<section[^>]*aria-labelledby="samples-title".*?</section>', dom, re.S)
    assert match, "samples table missing"
    return match.group(0)


@needs_chrome
def test_landing_samples_table_shows_kit_columns(kit_world, tmp_path) -> None:
    with RunningLabServer(LabStore(kit_world["root"])) as server:
        dom = _dom(server, tmp_path, want=lambda page: "FLO-SYN114" in page)
    section = _samples_table(dom)
    assert 'class="table-wrap"' in section
    head = re.search(r"<thead>(.*?)</thead>", section, re.S).group(1)
    assert re.findall(r'<th scope="col">(.*?)</th>', head) == COLUMNS
    body = re.search(r"<tbody>(.*?)</tbody>", section, re.S).group(1)
    rows = re.findall(r"<tr>(.*?)</tr>", body, re.S)
    assert len(rows) == 3
    cells = {tuple(re.findall(r'<td class="kit-cell[^"]*">(.*?)</td>', row)) for row in rows}
    assert cells == {
        ("FLO-SYN114", "SQK-SYN114", SYNTHETIC_MODEL, "synthetic 0"),
        ("not recorded", "not recorded", "not recorded", "synthetic 0"),
        ("mixed (2 values)", "mixed (2 values)", SYNTHETIC_MODEL, "synthetic 0"),
    }
    for identifier in ("SYNFC0001", "SYNDEV1", "synthsample", "synthlib"):
        assert identifier not in section


@needs_chrome
def test_kit_columns_scroll_inside_the_table_wrap_at_phone_width(kit_world, tmp_path) -> None:
    with RunningLabServer(LabStore(kit_world["root"])) as server:
        dom = _dom(server, tmp_path, size="360,740")
    assert _body(dom)["page-scroll-x"] == "false"
    assert "FLO-SYN114" in _samples_table(dom)
