"""Offline source-mixture validation; no biological truth is manufactured."""

from __future__ import annotations

import array
import copy
import hashlib
import json
import sqlite3
from pathlib import Path

import numpy as np
import pysam
import pytest

from scripts.validation import mix_public_bams as mixer
from traceback_lab.read_summary import ReadSummary


def header():
    return {"HD": {"VN": "1.6", "SO": "coordinate"},
            "SQ": [{"SN": "chr2", "LN": 10000}, {"SN": "chr1", "LN": 10000}, {"SN": "chrM", "LN": 1000}],
            "RG": [{"ID": "shared", "PL": "ONT", "SM": "synthetic", "PG": "caller"}],
            "PG": [{"ID": "caller", "PN": "synthetic"}]}


def write_bam(path: Path, *, source: int = 0, h=None, mutate=None):
    h = copy.deepcopy(h or header())
    with pysam.AlignmentFile(str(path), "wb", header=h) as writer:
        for i in range(120):
            r = pysam.AlignedSegment(writer.header)
            # Intentionally overlapping names between sources, never within one.
            r.query_name = f"private-synthetic-{i}"
            r.query_sequence = "CG" * 20
            r.query_qualities = array.array("B", [30] * 40)
            r.reference_id = i // 60
            r.reference_start = i % 60 * 50
            r.mapping_quality = 30
            r.cigarstring = "40M"
            r.set_tag("RG", "shared")
            r.set_tag("PG", "caller")
            r.set_tag("MM", "C+m?,0,1;")
            r.set_tag("ML", array.array("B", [230, 240]))
            r.set_tag("MN", 40)
            r.set_tag("XY", source)
            if mutate:
                mutate(r, i)
            writer.write(r)
    return path


@pytest.fixture
def sources(tmp_path):
    return write_bam(tmp_path / "background.bam"), write_bam(tmp_path / "patient.bam", source=1)


def records(path):
    with pysam.AlignmentFile(str(path), "rb") as bam:
        return list(bam.fetch(until_eof=True))


def test_exact_counts_nested_selection_preserved_tags_sort_and_replay(sources, tmp_path):
    first = mixer.mix_sources(*sources, tmp_path / "first", total=100)
    again = mixer.mix_sources(*sources, tmp_path / "again", total=100)
    assert first == again
    assert first["true_tumour_fraction"] is None
    assert "NOT true tumour fraction" in first["fraction_semantics"]
    assert first["reference"]["both_sources_have_all_m5"] is False
    selections = []
    for row, target in zip(first["mixtures"], [1, 3, 10], strict=True):
        assert row["selected_counts"] == [100 - target, target]
        assert row["actual_q"] == {"numerator": target, "denominator": 100}
        path = tmp_path / "first" / row["file"]
        rr = records(path)
        assert len(rr) == len({r.query_name for r in rr}) == 100
        assert [(r.reference_id, r.reference_start) for r in rr] == sorted((r.reference_id, r.reference_start) for r in rr)
        with pysam.AlignmentFile(str(path), "rb") as bam:
            assert bam.has_index() and len(list(bam.fetch("chr2"))) > 0
            assert [r["ID"] for r in bam.header.to_dict()["RG"]] == ["s0.shared", "s1.shared"]
        chosen = [set(), set()]
        for r in rr:
            source = r.get_tag("ZS")
            chosen[source].add(r.get_tag("ZQ"))
            assert r.get_tag("RG") == f"s{source}.shared"
            assert r.get_tag("PG") == f"s{source}.caller"
            assert r.get_tag("XY") == source
            assert r.get_tag("MM") == "C+m?,0,1;"
            assert list(r.get_tag("ML")) == [230, 240]
            assert r.get_tag("MN") == 40 and r.query_sequence == "CG" * 20
            assert list(r.query_qualities) == [30] * 40
            assert r.modified_bases is not None
        selections.append(chosen)
    assert selections[0][1] < selections[1][1] < selections[2][1]
    assert selections[2][0] < selections[1][0] < selections[0][0]
    changed = mixer.mix_sources(*sources, tmp_path / "changed", total=100, seed=1234)
    assert changed["mixtures"][0]["sha256"] != first["mixtures"][0]["sha256"]
    assert not list(tmp_path.glob(".mixture-*"))
    assert not list((tmp_path / "first").glob("*.sqlite"))


def test_d21_matches_read_summary_nuclear_denominator(sources, tmp_path):
    def exclusions(r, i):
        if i < 5:
            r.flag = [0x4, 0x100, 0x200, 0x400, 0x800][i]
        if i == 5:
            r.mapping_quality = 19
        if i == 119:
            r.reference_id = 2
            r.reference_start = 10
    background = write_bam(tmp_path / "excluded.bam", mutate=exclusions)
    # Exercise the actual summary predicate over equivalent columns, no fake policy.
    rr = records(background)
    summary = ReadSummary(directory=tmp_path, meta={"contigs": ["chr2", "chr1", "chrM"]})
    columns = {"flag": np.array([r.flag for r in rr]), "mapq": np.array([r.mapping_quality for r in rr]),
               "contig": np.array([r.reference_id for r in rr])}
    summary.column = columns.__getitem__
    expected = int(summary.eligible().sum())
    manifest = mixer.mix_sources(background, sources[1], tmp_path / "out", total=100)
    assert expected == manifest["sources"][0]["unique_eligible_reads"] == 113
    assert manifest["eligibility"]["contigs"] == ["chr2", "chr1"]


@pytest.mark.parametrize("mutation,message", [
    (lambda r, i: setattr(r, "query_name", "private-synthetic-0") if i == 119 else None, "duplicate eligible"),
    (lambda r, i: setattr(r, "flag", 1) if i == 0 else None, "paired eligible"),
    (lambda r, i: r.set_tag("ZQ", "private") if i == 0 else None, "reserved provenance"),
    (lambda r, i: r.set_tag("RG", "missing") if i == 0 else None, "unresolved"),
    (lambda r, i: setattr(r, "reference_start", 0) if i == 59 else None, "not coordinate sorted"),
    (lambda r, i: setattr(r, "reference_id", -1) if i == 0 else None, "unmapped tail"),
])
def test_invalid_records_refuse_without_partial_output(sources, tmp_path, mutation, message):
    bad = write_bam(tmp_path / "bad.bam", mutate=mutation)
    with pytest.raises(mixer.MixtureError, match=message):
        mixer.mix_sources(bad, sources[1], tmp_path / "out", total=100)
    assert not (tmp_path / "out").exists() and not list(tmp_path.glob(".mixture-*"))


def test_rank_collisions_have_exact_deterministic_tie_break(sources, tmp_path, monkeypatch):
    monkeypatch.setattr(mixer, "rank", lambda *args: b"same")
    manifest = mixer.mix_sources(*sources, tmp_path / "out", total=100)
    rr = records(tmp_path / "out" / manifest["mixtures"][2]["file"])
    assert [r.get_tag("ZQ") for r in rr if r.get_tag("ZS") == 1] == [f"private-synthetic-{i}" for i in range(10)]


@pytest.mark.parametrize("kind", ["platform", "dictionary", "m5"])
def test_header_mismatch_refused(sources, tmp_path, kind):
    h = header()
    other = header()
    if kind == "platform":
        h["RG"][0]["PL"] = "ILLUMINA"
    elif kind == "dictionary":
        h["SQ"].reverse()
    else:
        h["SQ"][0]["M5"] = "a" * 32
        other["SQ"][0]["M5"] = "b" * 32
    a = write_bam(tmp_path / "a.bam", h=h)
    b = write_bam(tmp_path / "b.bam", h=other, source=1)
    with pytest.raises(mixer.MixtureError):
        mixer.mix_sources(a, b, tmp_path / "out", total=100)
    assert not (tmp_path / "out").exists()


def test_fasta_dictionary_and_m5_checked(sources, tmp_path):
    fasta = tmp_path / "reference.fa"
    fasta.write_text(">chr2\n" + "A" * 10000 + "\n>chr1\n" + "C" * 10000 + "\n>chrM\n" + "G" * 1000 + "\n")
    h = header()
    for row, base in zip(h["SQ"], "ACG", strict=True):
        row["M5"] = hashlib.md5((base * row["LN"]).encode(), usedforsecurity=False).hexdigest()
    a = write_bam(tmp_path / "with-m5.bam", h=h)
    result = mixer.mix_sources(a, sources[1], tmp_path / "out", total=100, reference_fasta=fasta)
    assert result["reference"]["fasta_sha256"] == mixer.sha256(fasta)
    with pysam.AlignmentFile(str(tmp_path / "out" / result["mixtures"][0]["file"]), "rb") as bam:
        published = bam.header.to_dict()
        assert all("M5" not in row for row in published["SQ"])
        assert any("not independently proven" in comment for comment in published["CO"])
    assert result["sources"][0]["header"]["SQ"][0]["M5"] == h["SQ"][0]["M5"]
    fasta.write_text(fasta.read_text().replace("AAAA", "TTTT"))
    with pytest.raises(mixer.MixtureError, match="disagrees"):
        mixer.mix_sources(a, sources[1], tmp_path / "wrong", total=100, reference_fasta=fasta)


def test_no_replacement_overwrite_or_fraction_rounding(sources, tmp_path):
    with pytest.raises(mixer.MixtureError, match="identical content"):
        mixer.mix_sources(sources[0], sources[0], tmp_path / "same", total=100)
    with pytest.raises(mixer.MixtureError, match="multiple of 100"):
        mixer.mix_sources(*sources, tmp_path / "rounded", total=101)
    with pytest.raises(mixer.MixtureError, match="exceeds unique"):
        mixer.mix_sources(*sources, tmp_path / "too-many", total=1000)
    existing = tmp_path / "existing"
    existing.mkdir()
    with pytest.raises(mixer.MixtureError, match="already exists"):
        mixer.mix_sources(*sources, existing, total=100)
    assert existing.is_dir() and not list(existing.iterdir())
    assert not list(tmp_path.glob(".mixture-*"))


def test_unwrapped_fasta_and_empty_header(tmp_path):
    fasta = tmp_path / "unwrapped.fa"
    bases = b"acgt" * 300000
    fasta.write_bytes(b">chr1\n" + bases)
    h = {"SQ": [{"SN": "chr1", "LN": len(bases),
                 "M5": hashlib.md5(bases.upper(), usedforsecurity=False).hexdigest()}]}
    evidence = mixer._reference([h, h], fasta)
    assert evidence["fasta_sha256"] == mixer.sha256(fasta)
    fasta.write_text(">\nACGT\n")
    with pytest.raises(mixer.MixtureError, match="invalid reference FASTA header"):
        mixer._reference([h, h], fasta)


def test_failure_after_first_output_is_atomic(sources, tmp_path, monkeypatch):
    actual = mixer._write
    calls = 0
    def fail(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("synthetic disk failure")
        return actual(*args, **kwargs)
    monkeypatch.setattr(mixer, "_write", fail)
    with pytest.raises(OSError):
        mixer.mix_sources(*sources, tmp_path / "out", total=100)
    assert not (tmp_path / "out").exists() and not list(tmp_path.glob(".mixture-*"))


def test_cli_manifest_and_private_error(sources, tmp_path, capsys):
    assert mixer.main([str(sources[0]), str(sources[1]), str(tmp_path / "out"), "--total", "100"]) == 0
    assert "not known tumour fractions" in capsys.readouterr().out
    saved = json.loads((tmp_path / "out" / "manifest.json").read_text())
    assert "private-synthetic-" not in json.dumps(saved)
    assert mixer.main([str(sources[0]), str(sources[1]), str(tmp_path / "bad"), "--total", "123"]) == 2
    assert str(tmp_path) not in capsys.readouterr().out


def test_concurrent_destination_is_never_overwritten(sources, tmp_path, monkeypatch):
    actual = mixer.rename_directory_exclusive_at
    destination = tmp_path / "out"
    def compete(parent, stage, name):
        destination.mkdir()
        (destination / "sentinel").write_text("other writer")
        actual(parent, stage, name)
    monkeypatch.setattr(mixer, "rename_directory_exclusive_at", compete)
    with pytest.raises(FileExistsError):
        mixer.mix_sources(*sources, destination, total=100)
    assert (destination / "sentinel").read_text() == "other writer"
    assert not list(tmp_path.glob(".mixture-*"))


def test_disk_budget_and_source_mutation_refuse_publication(sources, tmp_path, monkeypatch):
    actual = mixer._write
    def exhaust(*args, **kwargs):
        result = actual(*args, **kwargs)
        (args[-2] / "synthetic-growth").write_bytes(b"x" * (1024 * 1024 + 1))
        return result
    with monkeypatch.context() as patch:
        patch.setattr(mixer, "_write", exhaust)
        with pytest.raises(mixer.MixtureError, match="disk budget"):
            mixer.mix_sources(*sources, tmp_path / "budget", total=100, max_work_bytes=1024 * 1024)
    calls = 0
    def change_after_final_read(*args, **kwargs):
        nonlocal calls
        result = actual(*args, **kwargs)
        calls += 1
        if calls == 3:
            with sources[0].open("ab") as stream:
                stream.write(b"changed")
        return result
    monkeypatch.setattr(mixer, "_write", change_after_final_read)
    with pytest.raises(mixer.MixtureError, match="source changed"):
        mixer.mix_sources(*sources, tmp_path / "mutated", total=100)
    assert not (tmp_path / "budget").exists() and not (tmp_path / "mutated").exists()
    assert not list(tmp_path.glob(".mixture-*"))


def test_streaming_checkpoints_commit_index_and_check_during_merge(sources, tmp_path, monkeypatch):
    monkeypatch.setattr(mixer, "CHECKPOINT_READS", 4)
    actual = mixer._budget
    committed_scan_rows = []
    merge_checks = []
    def observe(stage, maximum):
        if not list(stage.glob("patient-source-*.bam")):
            # A separate connection sees only committed rows, verifying the checkpoint.
            with sqlite3.connect(stage / "selection.sqlite") as db:
                committed_scan_rows.append(db.execute("SELECT COUNT(*) FROM reads").fetchone()[0])
        else:
            merge_checks.append(True)
        actual(stage, maximum)
    monkeypatch.setattr(mixer, "_budget", observe)
    mixer.mix_sources(*sources, tmp_path / "out", total=100)
    assert committed_scan_rows == list(range(4, 241, 4))
    # Each output:25 in-loop checks plus post-index; one final manifest check.
    assert len(merge_checks) == 3 * (25 + 1) + 1
