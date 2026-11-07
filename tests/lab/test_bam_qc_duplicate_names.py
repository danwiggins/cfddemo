"""bam-qc v2: duplicate read-name detection on the read summary's name hashes."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.lab.bam_fixtures import Read, context_for
from tests.lab.test_landing import _dom, needs_chrome
from traceback_lab.algorithms.bam_qc import BAM_QC, BamQc
from traceback_lab.interpretation.engine import load_library
from traceback_lab.registry import execution_identity
from traceback_lab.store import LabStore
from traceback_lab.synthetic import write_chunks
from traceback_lab.web.api import LabApi
from traceback_lab.web.server import RunningLabServer
from traceback_runner import cli as signed_cli

CONTIGS = [("chr1", 5000)]
V1_METRICS = {
    "total_reads", "mapped_fraction", "mapq_ge20_fraction", "supplementary_fraction",
    "mean_read_q", "genome_ge1x_fraction", "gc_bias_slope", "duplicate_fraction",
}
NEW_METRICS = {"duplicate_read_name_count", "duplicate_read_name_share"}
FLAG_TEXT = "FLAG: duplicate read names — possible re-basecalled chunk"


def _reads(rows: list[tuple[str, int]]) -> list[Read]:
    return [Read(name, "chr1", 100 * index, "ACGT" * 10, flag=flag) for index, (name, flag) in enumerate(rows)]


def _metrics(tmp_path: Path, rows: list[tuple[str, int]]) -> dict[str, dict]:
    context = context_for(tmp_path, CONTIGS, _reads(rows))
    return {row["metric_id"]: row for row in BAM_QC.run(context, tmp_path).metrics}


def test_repeated_names_count_every_primary_copy_and_skip_secondary_and_supplementary(tmp_path: Path) -> None:
    metrics = _metrics(tmp_path, [
        ("synthetic-a", 0), ("synthetic-a", 0),
        ("synthetic-b", 0), ("synthetic-b", 0), ("synthetic-b", 0),
        ("synthetic-c", 0), ("synthetic-c", 0x100), ("synthetic-c", 0x800),  # one primary: not a duplicate
        ("synthetic-d", 0), ("synthetic-d", 0x4),  # an unmapped primary copy still repeats the name
    ])
    assert set(metrics) == V1_METRICS | NEW_METRICS
    assert metrics["total_reads"]["value"] == 8
    count = metrics["duplicate_read_name_count"]
    share = metrics["duplicate_read_name_share"]
    assert (count["value"], count["unit"], count["n"]) == (7, "reads", 8)
    assert share["value"] == pytest.approx(7 / 8) and (share["unit"], share["n"]) == ("fraction", 8)
    assert isinstance(count["value"], int)


def test_clean_input_is_a_measured_zero_and_an_empty_input_has_no_share(tmp_path: Path) -> None:
    metrics = _metrics(tmp_path / "clean", [("synthetic-a", 0), ("synthetic-b", 0), ("synthetic-c", 0x800), ("synthetic-d", 0x4)])
    assert (metrics["duplicate_read_name_count"]["value"], metrics["duplicate_read_name_count"]["n"]) == (0, 3)
    assert (metrics["duplicate_read_name_share"]["value"], metrics["duplicate_read_name_share"]["n"]) == (0.0, 3)
    empty = _metrics(tmp_path / "empty", [])
    assert (empty["duplicate_read_name_count"]["value"], empty["duplicate_read_name_share"]["value"]) == (0, None)


def test_paired_end_mates_share_a_name_by_design_and_are_not_duplicates(tmp_path: Path) -> None:
    metrics = _metrics(tmp_path, [
        ("synthetic-p", 0x41), ("synthetic-p", 0x81),  # first + second mate: not a duplicate
        ("synthetic-q", 0x41), ("synthetic-q", 0x41),  # two first mates: a duplicate pair
        ("synthetic-r", 0x41), ("synthetic-r", 0x81),
    ])
    assert metrics["duplicate_read_name_count"]["value"] == 2
    assert metrics["duplicate_read_name_share"]["value"] == pytest.approx(2 / 6)


def test_derived_row_mask_scopes_the_count(tmp_path: Path) -> None:
    rows = [(f"synthetic-{index}", 0) for index in range(40)] + [("synthetic-3", 0), ("synthetic-7", 0)]
    full = context_for(tmp_path, CONTIGS, _reads(rows))
    half_a = context_for(tmp_path, CONTIGS, _reads(rows), derived={"transform": "split", "half": "A"})
    half_b = context_for(tmp_path, CONTIGS, _reads(rows), derived={"transform": "split", "half": "B"})
    counts = {}
    for key, context in (("full", full), ("A", half_a), ("B", half_b)):
        counts[key] = {row["metric_id"]: row["value"] for row in BAM_QC.run(context, tmp_path).metrics}
    assert counts["full"]["duplicate_read_name_count"] == 4
    # A repeated name hashes identically, so both copies land in the same half.
    assert counts["A"]["duplicate_read_name_count"] + counts["B"]["duplicate_read_name_count"] == 4
    assert counts["A"]["duplicate_read_name_count"] % 2 == 0 and counts["B"]["duplicate_read_name_count"] % 2 == 0


def test_version_two_re_keys_the_execution_identity() -> None:
    assert BAM_QC.version == "2"
    v1 = BamQc("bam-qc")
    assert execution_identity(v1, reference_sha256="a" * 64) != execution_identity(BAM_QC, reference_sha256="a" * 64)


def test_library_entries_carry_the_nonzero_flag_and_no_published_range() -> None:
    library = load_library()
    for metric_id in NEW_METRICS:
        entry = library.entries[("bam-qc", metric_id)]
        assert entry["nonzero_flag"] == FLAG_TEXT
        assert entry["reference"] == {"kind": "none"}
        assert entry["performance_missing_reason"] == "none published"
        assert entry["flags"] == [] and entry["headline"] is False
        assert "3e-6" in entry["meaning_low"] and "10 million reads" in entry["meaning_low"]
    share = library.entries[("bam-qc", "duplicate_read_name_share")]
    assert share["qc_band"]["direction"] == "lower_is_better"


# ------------------------------------------------------------- ingest path (two chunks)
@pytest.fixture(scope="module")
def rebasecalled_root(tmp_path_factory, synthetic_world, shared_mmi) -> dict:
    """Chunk 1 re-basecalls the first half of chunk 0: same read names, different bytes."""

    from traceback_runner.references import register_reference

    base = tmp_path_factory.mktemp("dup-names")
    root = base / "root"
    register_reference(root, synthetic_world["fasta"], "synth")
    from traceback_lab.synthetic import write_reference

    reference = write_reference(base / "ref.fa", length=30_000)
    assert reference.fasta.read_bytes() == synthetic_world["fasta"].read_bytes()
    chunks = base / "chunks"
    stem = "SYNTH0002_pass_synthetic"
    write_chunks(chunks, reference, chunks=1, reads_per_chunk=60, seed=11, stem=stem)
    other = base / "other"
    write_chunks(other, reference, chunks=1, reads_per_chunk=30, seed=11, stem=stem, methylated=0.2)
    (other / f"{stem}_0.bam").rename(chunks / f"{stem}_1.bam")
    assert (chunks / f"{stem}_0.bam").read_bytes() != (chunks / f"{stem}_1.bam").read_bytes()
    code = signed_cli.main([
        "lab", "run", str(chunks), "--root", str(root), "--mmi", str(shared_mmi), "--only", "bam-qc",
        "--no-downsample", "--no-controls", "--json",
    ])
    assert code == 0
    store = LabStore(root)
    id12 = LabApi(store).samples()["samples"][0]["id12"]
    return {"root": root, "id12": id12}


def test_two_chunks_with_the_same_read_names_are_counted_exactly(rebasecalled_root) -> None:
    api = LabApi(LabStore(rebasecalled_root["root"]))
    sample = api.sample(rebasecalled_root["id12"])
    bam_qc = next(row for row in sample["algorithms"] if row["algorithm_id"] == "bam-qc")
    assert bam_qc["status"] == "ok" and bam_qc["algorithm_version"] == "2"
    metrics = {row["metric_id"]: row for row in bam_qc["metrics"]}
    assert metrics["total_reads"]["value"] == 90
    assert metrics["duplicate_read_name_count"]["value"] == 60
    assert metrics["duplicate_read_name_share"]["value"] == pytest.approx(2 / 3)
    assert metrics["duplicate_read_name_share"]["qc_band"] == "fail"
    assert metrics["duplicate_read_name_share"]["state"] == "no_reference"
    assert metrics["duplicate_read_name_share"]["reason"] == "none_published"
    assert metrics["duplicate_read_name_share"]["entry"]["nonzero_flag"] == FLAG_TEXT
    assert metrics["duplicate_read_name_count"]["entry"]["nonzero_flag"] == FLAG_TEXT


@needs_chrome
def test_trust_qc_shows_the_duplicate_name_flag_pill(rebasecalled_root, tmp_path) -> None:
    id12 = rebasecalled_root["id12"]
    with RunningLabServer(LabStore(rebasecalled_root["root"])) as server:
        dom = _dom(server, tmp_path, route=f"/sample/{id12}/trust-qc", want=lambda page: "QC bands" in page)
    bands = dom.split("QC bands", 1)[1].split("</section>", 1)[0]
    row = re.search(r"<tr><th scope=\"row\">Share of primary reads with a repeated read name</th>(.*?)</tr>", bands, re.S)
    assert row, bands
    assert "✕ fail" in row.group(1)
    assert f'<span class="flag-pill">{FLAG_TEXT}</span>' in row.group(1)
    assert dom.count(FLAG_TEXT) == 1
