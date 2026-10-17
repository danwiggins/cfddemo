"""Pinned GENCODE gene geometry and exact overlap-base TSS coverage."""

from __future__ import annotations

import gzip
import hashlib
import io
import json

import numpy as np
import pytest

from traceback_lab import assets
from traceback_lab.algorithms import tss_profile
from traceback_lab.interpretation.engine import load_library
from traceback_lab.registry import InputContext, Ready, Skip, execution_identity
from traceback_lab.stability import Side, stable
from traceback_lab.store import LabStore


def _gtf() -> bytes:
    lines = [
        'chr1\tHAVANA\tgene\t2001\t2500\t.\t+\t.\tgene_id "ENSG1"; gene_type "protein_coding";',
        'chr1\tHAVANA\tgene\t5501\t6001\t.\t-\t.\tgene_id "ENSG2"; gene_type "protein_coding";',
        'chr1\tHAVANA\tgene\t7001\t7500\t.\t+\t.\tgene_id "ENSG3"; gene_type "lncRNA";',
        'chrX\tHAVANA\tgene\t8001\t8500\t.\t+\t.\tgene_id "ENSG4"; gene_type "protein_coding";',
    ]
    return gzip.compress(("\n".join(lines) + "\n").encode())


def test_annotation_uses_one_autosomal_gene_tss_and_correct_strand() -> None:
    genes = tss_profile.read_tss(io.BytesIO(_gtf()))
    assert [(gene.gene_id, gene.pos0, gene.strand) for gene in genes] == [
        ("ENSG1", 2000, "+"), ("ENSG2", 6000, "-"),
    ]


def test_exact_overlap_bases_and_reverse_orientation() -> None:
    forward = (tss_profile.Tss("A", "chr1", 2000, "+"),)
    reverse = (tss_profile.Tss("B", "chr1", 6000, "-"),)
    plus = tss_profile.coverage_profile(
        forward, ["chr1"], [10_000], np.zeros(3, dtype=np.int16),
        np.asarray([1000, 1850, 2500]), np.asarray([1500, 2050, 3000]),
    )
    minus = tss_profile.coverage_profile(
        reverse, ["chr1"], [10_000], np.zeros(3, dtype=np.int16),
        np.asarray([5001, 5951, 6501]), np.asarray([5501, 6151, 7001]),
    )
    assert plus["gene_n"] == minus["gene_n"] == 1
    assert plus["central_flank_ratio"] == pytest.approx(1.0)
    assert minus["central_flank_ratio"] == pytest.approx(1.0)
    assert plus["coverage_x"][0] == minus["coverage_x"][0] == 1.0
    assert plus["coverage_x"][20] == minus["coverage_x"][20] == 1.0
    assert plus["coverage_x"][21] == minus["coverage_x"][21] == 0.0


def test_zero_flank_and_boundary_gene_do_not_invent_ratio() -> None:
    genes = (tss_profile.Tss("edge", "chr1", 100, "+"),
             tss_profile.Tss("interior", "chr1", 2000, "+"))
    profile = tss_profile.coverage_profile(
        genes, ["chr1"], [10_000], np.asarray([0], dtype=np.int16),
        np.asarray([1900]), np.asarray([2100]),
    )
    assert profile["gene_n"] == 1
    assert profile["flank_depth_x"] == 0
    assert profile["central_flank_ratio"] is None


def test_partial_bin_overlap_counts_bases_not_read_starts() -> None:
    profile = tss_profile.coverage_profile(
        (tss_profile.Tss("one", "chr1", 2000, "+"),),
        ["chr1"], [10_000], np.asarray([0], dtype=np.int16),
        np.asarray([1990]), np.asarray([2010]),
    )
    assert profile["coverage_x"][19] == pytest.approx(0.2)
    assert profile["coverage_x"][20] == pytest.approx(0.2)
    assert sum(profile["coverage_x"]) == pytest.approx(0.4)


@pytest.mark.parametrize("zero_flank", [False, True])
def test_pinned_registration_gate_and_output(tmp_path, monkeypatch, zero_flank: bool) -> None:
    content = _gtf()
    digest = hashlib.sha256(content).hexdigest()
    monkeypatch.setattr(assets, "TSS_SHA256", digest)
    monkeypatch.setattr(tss_profile, "TSS_SHA256", digest)
    monkeypatch.setattr(tss_profile, "MIN_READS", 3)
    algorithm = tss_profile.TssProfile("tss-profile")
    algorithm.parameters["annotation_sha256"] = digest
    store = LabStore(tmp_path / "root")
    source = tmp_path / assets.TSS_FILENAME
    source.write_bytes(content)
    ctx = InputContext(
        root=store.root, source_id="a" * 64, input_id="b" * 64,
        reference_id="hg38", reference_sha256="c" * 64,
        input_dir=tmp_path,
    )

    class Summary:
        contigs = ["chr1"]
        lengths = [tss_profile.HG38_CHR1_LENGTH]
        data = {
            "contig": np.zeros(3, dtype=np.int16),
            "start": np.asarray([1900, 1950, 2000] if zero_flank else [1000, 1900, 2500]),
            "end": np.asarray([1950, 2000, 2050] if zero_flank else [1500, 2100, 3000]),
        }

        def autosome_ids(self):
            return np.asarray([0], dtype=np.int16)

        def eligible(self, *, contigs):
            return np.asarray([True, True, True])

        def column(self, name):
            return self.data[name]

    monkeypatch.setattr(tss_profile, "load_summary", lambda ctx: (Summary(), None))
    assert isinstance(algorithm.preflight(ctx), Skip)
    with store.writer("asset add", "r-20261016-1200-0000", step=assets.TSS_KIND):
        record = assets.add_asset(store, assets.TSS_KIND, source,
                                  source_label="GENCODE v46 basic CHR GTF", terms="unknown")
    assert record.sha256 == digest
    assert isinstance(algorithm.preflight(ctx), Ready)
    assert algorithm.asset_sha256s(store.root) == [digest]
    identity = execution_identity(algorithm, reference_sha256=ctx.reference_sha256,
                                  asset_sha256s=algorithm.asset_sha256s(store.root))
    assert len(identity) == 64
    result = algorithm.run(ctx, tmp_path)
    panel = json.loads(result.artifacts["tss_profile.json"])
    assert result.metrics[0]["value"] == panel["central_flank_ratio"]
    if zero_flank:
        assert result.metrics[0]["value"] is None
        assert panel["flank_depth_x"] == 0
        entry = load_library().entry("tss-profile", "central_flank_ratio")
        assert entry.stability == {"kind": "relative", "tolerance": 0.20}
        assert stable(entry.stability, Side(result.metrics[0]["value"]), Side(1.0)) is None
    assert panel["annotation_sha256"] == digest
    assert panel["eligible_autosomal_reads"] == 3
    assert panel["gene_n"] == 2
    assert panel["offset_bp"] == list(range(-1000, 1000, 50))
    assert result.panels[0]["panel_id"] == "tss-coverage"
    assert assets.inspect_asset(store, assets.TSS_KIND).sha256 == digest
