"""TASTE T1: kit columns on the landing samples table, from stored header provenance."""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import pytest

from tests.lab.test_landing import _body, _dom, needs_chrome
from traceback_lab import ingest
from traceback_lab.ingest import header_provenance, merge_header_provenance, stored_header_provenance
from traceback_lab.store import LabStore
from traceback_lab.synthetic import SYNTHETIC_MODBASE, SYNTHETIC_MODEL, write_chunks, write_reference
from traceback_lab.web.api import LabApi
from traceback_lab.web.server import RunningLabServer
from traceback_runner import cli as signed_cli

OTHER_MODEL = "dna_r10.4.1_e8.2_400bps_hac@v4.3.0"


def _rg(run: str, instrument: str, model: str = SYNTHETIC_MODEL, modbase: str = SYNTHETIC_MODBASE) -> dict[str, str]:
    return {
        "ID": f"{run}_model", "DS": f"runid={run} basecall_model={model} modbase_models={modbase}",
        "PL": "ONT", "LB": "synthlib", "SM": "synthsample", "PU": "SYNFC0001", "PM": instrument,
        "DT": "2026-01-01T00:00:00Z",
    }


KIT_RG = _rg("synthrun", "GridION")
BARE_RG = {"ID": "bare", "PL": "ONT", "PM": "SYNDEV1"}
COLUMNS = ["ID", "Run date", "Approx depth", "Instrument", "Basecall model", "Modified-base models", "Software",
           "OK / skipped / failed", "Outside a reference", "Controls", ""]
NOT = [None]
SYNTH_SOFTWARE = [{"program": "synthetic", "version": "0"}]
IDENTIFIERS = ("SYNFC0001", "SYNDEV1", "synthsample", "synthlib", "synthrun", "runid", "2026-01-01")


def _fields(**overrides):
    base = {"instrument": NOT, "basecall_model": NOT, "modbase_models": NOT, "software": []}
    return {**base, **overrides}


def test_header_provenance_keeps_allowlisted_and_shaped_values_only() -> None:
    header = "\n".join([
        "@HD\tVN:1.6",
        "@RG\tID:run9_m\tPU:SYNFC0001\tPM:PromethION 2 Solo\tSM:someone\tLB:lib\tDT:2026-01-01"
        f"\tDS:runid=run9 basecall_model={SYNTHETIC_MODEL} modbase_models={SYNTHETIC_MODBASE},"
        "dna_r10.4.1_e8.2_400bps_sup@v5.0.0_6mA@v1",
        "@PG\tID:minknow\tPN:minknow\tVN:24.06.10\tCL:/opt/ont/minknow --flag",
        "@PG\tID:samtools\tPN:samtools\tVN:1.21",
        "@PG\tID:samtools.1\tPN:samtools\tVN:1.21",
    ])
    assert header_provenance(header) == _fields(
        instrument=["PromethION 2 Solo"], basecall_model=[SYNTHETIC_MODEL],
        modbase_models=[f"{SYNTHETIC_MODBASE},dna_r10.4.1_e8.2_400bps_sup@v5.0.0_6mA@v1"],
        software=[{"program": "minknow", "version": "24.06.10"}, {"program": "samtools", "version": "1.21"}],
    )
    rendered = json.dumps(header_provenance(header))
    for identifier in ("SYNFC0001", "someone", "lib", "run9", "2026-01-01", "/opt/ont"):
        assert identifier not in rendered


def test_identifier_shaped_values_are_not_recorded() -> None:
    long_model = "dna_" + "r" * 60 + "@v5"
    header = ("@RG\tID:x\tPM:PC24B243\tDS:basecall_model=jane@example.org"
              " modbase_models=dna_r10.4.1_e8.2_400bps_sup@v5.0.0_JANEDOE@v1\n"
              f"@RG\tID:y\tPM:Jane Doe GridION\tDS:basecall_model={SYNTHETIC_MODEL}_JANEDOE_P07 "
              f"modbase_models={long_model[:-3]}_5mCG@v1\n"
              f"@RG\tID:z\tPM:gridion\tDS:basecall_model={long_model}\n"
              "@PG\tID:p\tPN:my tool\tVN:1\n@PG\tID:q\tPN:tool\tVN:jane.doe@example.org\n"
              "@PG\tID:r\tPN:other\tVN:C:\\\\x")
    assert len(long_model) > 64
    assert header_provenance(header) == _fields(
        software=[{"program": "other", "version": None}, {"program": "tool", "version": None}])
    assert "@" not in json.dumps(header_provenance(header)["software"])
    assert header_provenance("") == _fields()


def test_read_groups_and_chunks_that_disagree_keep_every_value() -> None:
    one = header_provenance(
        f"@RG\tID:a\tPM:GridION\tDS:basecall_model={SYNTHETIC_MODEL}\n"
        f"@RG\tID:b\tPM:MinION\tDS:basecall_model={SYNTHETIC_MODEL} modbase_models={SYNTHETIC_MODBASE}\n"
        "@PG\tID:d\tPN:minknow\tVN:24.06.10")
    assert one["instrument"] == ["GridION", "MinION"]
    assert one["modbase_models"] == [SYNTHETIC_MODBASE, None]
    two = header_provenance(f"@RG\tID:c\tPM:GridION\tDS:basecall_model={OTHER_MODEL}\n@PG\tID:d\tPN:minknow\tVN:24.11.2")
    merged = merge_header_provenance([one, two])
    assert merged["instrument"] == ["GridION", "MinION"]
    assert merged["basecall_model"] == [OTHER_MODEL, SYNTHETIC_MODEL]
    assert merged["modbase_models"] == [SYNTHETIC_MODBASE, None]
    assert merged["software"] == [{"program": "minknow", "version": "24.06.10"},
                                  {"program": "minknow", "version": "24.11.2"}]
    assert merged["distinct"] == {"instrument": 2, "basecall_model": 2, "modbase_models": 2, "software": 2}
    assert merged["truncated"] is False


def test_cap_counts_distinct_values_first_and_keeps_none() -> None:
    parts = [header_provenance(f"@RG\tID:{n}\tDS:basecall_model=dna_m{n:02d}@v1\n@PG\tID:p\tPN:tool{n:02d}\tVN:1")
             for n in range(20)]
    parts.append(header_provenance("@RG\tID:none\tPL:ONT"))
    merged = merge_header_provenance(parts)
    assert len(merged["basecall_model"]) == 17 and merged["basecall_model"][-1] is None
    assert merged["distinct"]["basecall_model"] == 21
    assert len(merged["software"]) == 16 and merged["distinct"]["software"] == 20
    assert merged["truncated"] is True
    stored = stored_header_provenance({"header_provenance": merged})
    assert stored["basecall_model"] == merged["basecall_model"]
    assert stored["distinct"]["basecall_model"] == 21 and stored["truncated"] is True


def test_excluded_chunks_do_not_contribute_provenance(tmp_path) -> None:
    reference = write_reference(tmp_path / "ref.fa", length=5_000, contigs=("chr1",))
    chunks = tmp_path / "chunks"
    write_chunks(chunks, reference, chunks=2, reads_per_chunk=50, rg=KIT_RG)
    other = tmp_path / "other"
    write_chunks(other, reference, chunks=3, reads_per_chunk=50, seed=99,
                 rg=_rg("otherrun", "PromethION", model=OTHER_MODEL, modbase=f"{OTHER_MODEL}_6mA@v1"))
    bad = chunks / "SYNTH0001_pass_synthetic_2.bam"
    data = (other / "SYNTH0001_pass_synthetic_2.bam").read_bytes()
    bad.write_bytes(data[: len(data) // 2])
    store = LabStore(tmp_path / "root")
    store.ensure()
    chunkset = ingest.inspect_chunks(ingest.collect_paths([str(chunks)]), store, skip_bad_chunks=True)
    assert chunkset.excluded_indexes == [2]
    assert chunkset.header_provenance == {
        **_fields(instrument=["GridION"], basecall_model=[SYNTHETIC_MODEL], modbase_models=[SYNTHETIC_MODBASE],
                  software=SYNTH_SOFTWARE),
        "distinct": {"instrument": 1, "basecall_model": 1, "modbase_models": 1, "software": 1}, "truncated": False,
    }


def test_stored_header_provenance_revalidates_and_uses_legacy_model_only_when_absent() -> None:
    assert stored_header_provenance(None) is None
    legacy = stored_header_provenance({"basecall_model": "dna_a@v1|dna_b@v1"})
    assert {k: legacy[k] for k in ("instrument", "basecall_model", "modbase_models", "software")} == _fields(
        basecall_model=["dna_a@v1", "dna_b@v1"])
    recorded = stored_header_provenance({"basecall_model": "dna_a@v1", "header_provenance": {
        "instrument": ["GridION", "PAW12345"], "basecall_model": [None], "modbase_models": ["../../etc"],
        "software": [{"program": "minknow", "version": "has space"}, "junk", {"program": None}],
        "distinct": {"instrument": 999}, "truncated": False,
    }})
    assert {k: recorded[k] for k in ("instrument", "basecall_model", "modbase_models", "software")} == _fields(
        instrument=["GridION", None], software=[{"program": "minknow", "version": None}])
    assert recorded["distinct"]["instrument"] == 2 and recorded["truncated"] is False


@pytest.fixture(scope="module")
def kit_world(tmp_path_factory) -> dict[str, Path]:
    from traceback_runner.references import register_reference

    base = tmp_path_factory.mktemp("kit-cols")
    reference = write_reference(base / "ref.fa", length=30_000)
    write_chunks(base / "with-kit", reference, chunks=2, reads_per_chunk=600, seed=21, rg=KIT_RG)
    write_chunks(base / "bare", reference, chunks=2, reads_per_chunk=600, seed=22, rg=BARE_RG)
    write_chunks(base / "mixed", reference, chunks=2, reads_per_chunk=600, seed=23, rg=_rg("synthA", "MinION"))
    write_chunks(base / "mixed-b", reference, chunks=2, reads_per_chunk=600, seed=23, rg=_rg("synthB", "PromethION"))
    shutil.copy2(base / "mixed-b" / "SYNTH0001_pass_synthetic_1.bam", base / "mixed" / "SYNTH0001_pass_synthetic_1.bam")
    root = base / "root"
    register_reference(root, reference.fasta, "synth")
    for name in ("with-kit", "bare", "mixed"):
        assert signed_cli.main(["lab", "run", str(base / name), "--root", str(root), "--json"]) == 0
    return {"base": base, "root": root, "fasta": reference.fasta}


def _rows(root: Path) -> dict[str, dict]:
    rows = LabApi(LabStore(root)).samples()["samples"]
    assert len(rows) == 3
    named = {"GridION": "with-kit", None: "bare", "MinION": "mixed"}
    return {named[row["kit"]["instrument"][0]]: row for row in rows}


def _kit(row: dict) -> dict:
    return {key: row["kit"][key] for key in ("instrument", "basecall_model", "modbase_models", "software")}


def test_manifest_records_provenance_and_samples_projection_carries_it(kit_world) -> None:
    rows = _rows(kit_world["root"])
    assert _kit(rows["with-kit"]) == _fields(instrument=["GridION"], basecall_model=[SYNTHETIC_MODEL],
                                             modbase_models=[SYNTHETIC_MODBASE], software=SYNTH_SOFTWARE)
    assert _kit(rows["bare"]) == _fields(software=SYNTH_SOFTWARE)
    assert _kit(rows["mixed"]) == _fields(instrument=["MinION", "PromethION"], basecall_model=[SYNTHETIC_MODEL],
                                          modbase_models=[SYNTHETIC_MODBASE], software=SYNTH_SOFTWARE)
    stored = [json.loads(path.read_text()) for path in LabStore(kit_world["root"]).inputs_dir.glob("*/manifest.json")]
    assert ["MinION", "PromethION"] in [m.get("header_provenance", {}).get("instrument") for m in stored]
    projection = json.dumps(LabApi(LabStore(kit_world["root"])).samples())
    for identifier in IDENTIFIERS:
        assert identifier not in projection


def _aligned_manifests(root: Path) -> list[tuple[Path, dict]]:
    found = [(path, json.loads(path.read_text())) for path in LabStore(root).inputs_dir.glob("*/manifest.json")]
    return [(path, manifest) for path, manifest in found if manifest.get("kind") == "aligned"]


def test_header_provenance_does_not_enter_input_or_source_identity(kit_world, tmp_path, monkeypatch) -> None:
    from traceback_runner.references import register_reference

    with_field = [(m["input_id"], m["source_id"]) for _, m in _aligned_manifests(kit_world["root"])
                  if (m.get("header_provenance") or {}).get("instrument") == ["GridION"]]
    assert len(with_field) == 1
    root = tmp_path / "root"
    register_reference(root, kit_world["fasta"], "synth")
    monkeypatch.setattr(ingest, "header_provenance", lambda text: {})
    assert signed_cli.main(["lab", "run", str(kit_world["base"] / "with-kit"), "--root", str(root), "--json"]) == 0
    assert [(m["input_id"], m["source_id"]) for _, m in _aligned_manifests(root)] == with_field


def test_reingest_over_a_manifest_that_predates_header_provenance(kit_world, tmp_path) -> None:
    from traceback_runner.references import register_reference

    root = tmp_path / "root"
    register_reference(root, kit_world["fasta"], "synth")
    chunks = kit_world["base"] / "with-kit"
    assert signed_cli.main(["lab", "run", str(chunks), "--root", str(root), "--json"]) == 0
    [(path, manifest)] = _aligned_manifests(root)
    legacy = {key: value for key, value in manifest.items() if key != "header_provenance"}
    path.write_text(json.dumps(legacy, sort_keys=True))
    assert signed_cli.main(["lab", "run", str(chunks), "--root", str(root), "--json"]) == 0
    [(again_path, again)] = _aligned_manifests(root)
    assert again_path == path and again["input_id"] == manifest["input_id"]
    assert "header_provenance" not in again
    [row] = LabApi(LabStore(root)).samples()["samples"]
    assert _kit(row) == _fields(basecall_model=[SYNTHETIC_MODEL])


def test_projection_is_read_from_the_manifest_not_the_bam(kit_world, monkeypatch) -> None:
    def refuse(*args, **kwargs):
        raise AssertionError("samples() re-parsed header provenance from a BAM")

    monkeypatch.setattr(ingest, "header_provenance", refuse)
    monkeypatch.setattr(ingest, "read_header_lines", refuse)
    assert _rows(kit_world["root"])["with-kit"]["kit"]["instrument"] == ["GridION"]


def _samples_table(dom: str) -> str:
    match = re.search(r'<section[^>]*aria-labelledby="samples-title".*?</section>', dom, re.S)
    assert match, "samples table missing"
    return match.group(0)


@needs_chrome
def test_landing_samples_table_shows_kit_columns(kit_world, tmp_path) -> None:
    with RunningLabServer(LabStore(kit_world["root"])) as server:
        dom = _dom(server, tmp_path, want=lambda page: "GridION" in page)
    section = _samples_table(dom)
    assert 'class="table-wrap"' in section
    head = re.search(r"<thead>(.*?)</thead>", section, re.S).group(1)
    assert re.findall(r'<th scope="col">(.*?)</th>', head) == COLUMNS
    body = re.search(r"<tbody>(.*?)</tbody>", section, re.S).group(1)
    rows = re.findall(r"<tr>(.*?)</tr>", body, re.S)
    assert len(rows) == 3
    cells = {tuple(re.findall(r'<td class="kit-cell[^"]*">(.*?)</td>', row)) for row in rows}
    assert cells == {
        ("GridION", SYNTHETIC_MODEL, SYNTHETIC_MODBASE, "synthetic 0"),
        ("not recorded", "not recorded", "not recorded", "synthetic 0"),
        ("mixed (2 values)", SYNTHETIC_MODEL, SYNTHETIC_MODBASE, "synthetic 0"),
    }
    for identifier in IDENTIFIERS:
        assert identifier not in section


@needs_chrome
def test_kit_columns_scroll_inside_the_table_wrap_at_phone_width(kit_world, tmp_path) -> None:
    with RunningLabServer(LabStore(kit_world["root"])) as server:
        dom = _dom(server, tmp_path, size="360,740")
    assert _body(dom)["page-scroll-x"] == "false"
    assert "GridION" in _samples_table(dom)
