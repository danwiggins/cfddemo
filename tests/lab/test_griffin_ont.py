"""Griffin's single-end ONT port on a synthetic, locally generated genome.

Every sequence, fragment and site below is generated from fixed seeds; no
observed sample values are used.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from traceback_lab.adapters import griffin_ont
from traceback_lab.adapters.griffin import read_corrected_profiles
from traceback_lab.adapters.griffin_run import _REF_FILES, _digest
from traceback_lab.adapters.griffin_ont import (
    Fragments, GriffinOntDepth, GriffinOntRun, SAVE_COLUMNS, fragment_gc, gc_bias, run_ont,
)

CHROM_LENGTH = 120_000
SITES = (15_000, 30_000, 45_000, 60_000, 75_000, 90_000, 105_000)
INPUT_ID = "ab" * 32
_FREQ = "k100_minus_exclusion_lists.mappable_regions.hg38."


def _genome(seed: int = 7) -> str:
    """Alternating GC-poor and GC-rich 2 kb blocks with a short N run."""

    rng = np.random.default_rng(seed)
    blocks = []
    for index in range(CHROM_LENGTH // 2000):
        gc = 0.35 if index % 2 == 0 else 0.55
        probabilities = [(1 - gc) / 2, gc / 2, gc / 2, (1 - gc) / 2]
        blocks.append("".join(rng.choice(list("ACGT"), size=2000, p=probabilities)))
    sequence = "".join(blocks)
    return sequence[:50_000] + "N" * 40 + sequence[50_040:]


def write_world(base: Path, *, seed: int = 7) -> dict[str, Any]:
    """Reference, Griffin reference assets and a site list for one toy chr1."""

    import pysam

    sequence = _genome(seed)
    base.mkdir(parents=True, exist_ok=True)
    fasta = base / "toy.fa"
    fasta.write_text(">chr1\n" + "\n".join(sequence[i:i + 60] for i in range(0, len(sequence), 60)) + "\n")
    pysam.faidx(str(fasta))
    assets = base / "ref"
    assets.mkdir()
    for name in _REF_FILES:
        (assets / name).write_text("")
    (assets / "hg38.standard.chrom.sizes").write_text(f"chr1\t{CHROM_LENGTH}\n")
    (assets / "k100_minus_exclusion_lists.mappable_regions.hg38.bed").write_text(f"chr1\t0\t{CHROM_LENGTH}\n")
    # One excluded interval, far from every site window's centre.
    (assets / "hg38_gaps.bed").write_text("chr1\t118000\t119000\n")
    codes = np.frombuffer(sequence.encode(), dtype=np.uint8)
    strong = np.isin(codes, list(b"CG")).astype(np.int64)
    cumulative = np.concatenate(([0], np.cumsum(strong)))
    frequency = assets / "genome_GC_frequency"
    frequency.mkdir()
    for length in range(15, 501):
        windows = cumulative[length:] - cumulative[:-length]
        counts = np.bincount(windows, minlength=length + 1)
        rows = "".join(f"{length}\t{gc}\t{counts[gc]}\n" for gc in range(length + 1))
        (frequency / f"{_FREQ}{length}bp.GC_frequency.txt").write_text("length\tnum_GC\tnumber_of_fragments\n" + rows)
    sites = base / "ToyTF.txt"
    strands = ["+", "-", "+", "-", "+", "-", "+"]
    sites.write_text("Chrom\tposition\tStrand\n" + "".join(
        f"chr1\t{position}\t{strand}\n" for position, strand in zip(SITES, strands)))
    return {"fasta": fasta, "assets": assets, "sites": sites, "sequence": sequence, "strong": cumulative}


def plant_fragments(world: dict[str, Any], *, n: int = 220_000, seed: int = 11,
                    dip: bool = True, gc_bias_strength: float = 1.0) -> tuple[np.ndarray, np.ndarray]:
    """ONT-like single-end spans: fewer midpoints at site centres, GC-rich favoured."""

    rng = np.random.default_rng(seed)
    lengths = np.clip(np.round(rng.normal(167, 25, size=n)), 40, 480).astype(np.int64)
    midpoints = rng.integers(600, CHROM_LENGTH - 600, size=n)
    starts = midpoints - lengths // 2
    ends = starts + lengths
    keep = np.ones(n, dtype=bool)
    if dip:
        distance = np.min(np.abs(midpoints[:, None] - np.asarray(SITES)[None, :]), axis=1)
        keep &= ~((distance < 75) & (rng.random(n) < 0.6))
    content = (world["strong"][ends] - world["strong"][starts]) / lengths
    keep &= rng.random(n) < np.clip(0.2 + gc_bias_strength * (content - 0.2) * 2, 0.05, 1.0)
    return starts[keep], ends[keep]


def _ones(chrom: str, start: int, end: int) -> np.ndarray:
    return np.ones(end - start)


def _config(tmp_path: Path, world: dict[str, Any], starts: np.ndarray, ends: np.ndarray,
            **overrides: Any) -> GriffinOntRun:
    values: dict[str, Any] = dict(
        input_id=INPUT_ID, fragments=Fragments({"chr1": (starts, ends)}, len(starts)),
        contig_lengths={"chr1": CHROM_LENGTH}, reference_fasta=world["fasta"], reference_id="hg38",
        reference_sha256=_digest(world["fasta"]), ref_assets=world["assets"],
        site_lists={"ToyTF": world["sites"]}, attempt_dir=tmp_path / "attempt",
        registration_sha256="c" * 64, minimum_reads=1000,
    )
    values.update(overrides)
    return GriffinOntRun(**values)


@pytest.fixture(scope="module")
def world(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    return write_world(tmp_path_factory.mktemp("griffin-ont-world"))


def test_planted_site_dip_is_recovered_in_upstream_schema(tmp_path: Path, world: dict[str, Any]) -> None:
    starts, ends = plant_fragments(world)
    profiles = run_ont(_config(tmp_path, world, starts, ends), mappability=_ones)
    (profile,) = profiles
    assert profile.site_name == "ToyTF" and profile.site_count == len(SITES)
    assert profile.offsets_bp == tuple(int(value) for value in SAVE_COLUMNS)
    assert profile.offsets_bp[0] == -990 and profile.offsets_bp[-1] == 975 and len(profile.offsets_bp) == 132
    assert profile.smoothing is True and profile.cna_normalization is False
    coverage = np.asarray(profile.coverage)
    flank = np.r_[coverage[:20], coverage[-20:]].mean()
    assert profile.central_coverage < 0.8 * flank
    assert coverage[66] == coverage.min() or coverage[65] == coverage.min()
    # Same file contract as Illumina: re-parse the published TSV strictly.
    tsv = tmp_path / "attempt" / f"lab_{INPUT_ID}.GC_corrected.coverage.tsv"
    assert read_corrected_profiles(tsv, expected_sample=f"lab_{INPUT_ID}") == profiles
    header = tsv.read_text().splitlines()[0].split("\t")
    assert header[132:] == [
        "mean_reads_per_bp_in_normalization_window", "mean_reads_per_bp_in_saved_window",
        "mean_coverage", "central_coverage", "amplitude", "outlier_cutoff", "exclude_zero_mappability",
        "correction", "number_of_sites", "site_name", "smoothing", "CNA_normalization", "sample",
    ]
    provenance = json.loads((tmp_path / "attempt" / "provenance.json").read_text())
    assert provenance["platform"] == "ont" and provenance["method"] == griffin_ont.ONT_METHOD
    assert provenance["output_sha256"] == _digest(tsv)
    assert "/" not in json.dumps({key: value for key, value in provenance.items() if key != "eligibility_predicate"})


def test_port_matches_pinned_upstream_scripts_on_the_same_fragments(tmp_path: Path, world: dict[str, Any]) -> None:
    """Golden: upstream Griffin's own TSV for these spans presented as proper pairs.

    docs/proofs/griffin/ont_port_crosscheck.py regenerates the fixture by running
    the pinned upstream GC-count, GC-bias, coverage and merge-sites scripts.
    """

    golden = Path(__file__).parent / "fixtures" / "griffin" / "OntPortUpstream.GC_corrected.coverage.tsv"
    starts, ends = plant_fragments(world)
    run_ont(_config(tmp_path, world, starts, ends), mappability=_ones)
    produced = (tmp_path / "attempt" / f"lab_{INPUT_ID}.GC_corrected.coverage.tsv").read_text()
    expected = golden.read_text().replace("\ts\n", f"\tlab_{INPUT_ID}\n")
    assert produced == expected


def test_no_planted_dip_gives_a_flat_centre(tmp_path: Path, world: dict[str, Any]) -> None:
    starts, ends = plant_fragments(world, dip=False)
    (profile,) = run_ont(_config(tmp_path, world, starts, ends), mappability=_ones)
    assert 0.85 < profile.central_coverage < 1.15


def test_gc_correction_learns_and_applies_the_planted_bias(tmp_path: Path, world: dict[str, Any]) -> None:
    import pysam

    starts, ends = plant_fragments(world)
    with pysam.FastaFile(str(world["fasta"])) as fasta:
        gc = fragment_gc(fasta, "chr1", starts, ends)
        lengths = ends - starts
        counts = np.zeros((486, 501), dtype=np.int64)
        keep = (lengths >= 15) & (lengths <= 500)
        np.add.at(counts, (lengths[keep] - 15, gc[keep]), 1)
    frequency = griffin_ont._frequency(world["assets"] / "genome_GC_frequency")
    bias = gc_bias(counts, frequency)
    row = bias[167 - 15]
    # GC-rich fragments were sampled more often, so the learned bias rises with GC.
    assert np.nanmean(row[45:60]) < np.nanmean(row[85:100])
    assert np.nanmean(row[~np.isnan(row)]) == pytest.approx(1.0)
    # The corrected profile is weighted by 1/bias, not a copy of raw counts.
    run_ont(_config(tmp_path, world, starts, ends), mappability=_ones)
    bias_table = (tmp_path / "attempt" / f"lab_{INPUT_ID}.GC_bias.txt").read_text().splitlines()
    assert bias_table[0] == "length\tnum_GC\tsmoothed_GC_bias" and len(bias_table) == 1 + sum(
        length + 1 for length in range(15, 501))
    flat_starts, flat_ends = plant_fragments(world, gc_bias_strength=0.0)
    (tmp_path / "b").mkdir()
    (tmp_path / "u").mkdir()
    (biased,) = run_ont(_config(tmp_path / "b", world, starts, ends), mappability=_ones)
    (unbiased,) = run_ont(_config(tmp_path / "u", world, flat_starts, flat_ends), mappability=_ones)
    # The site list alternates GC-poor and GC-rich neighbourhoods; correction
    # keeps the biased sample's mean-normalised profile close to an unbiased one.
    assert np.max(np.abs(np.subtract(biased.coverage, unbiased.coverage))) < 0.25


def test_fragment_gc_counts_reference_bases_and_seeds_ambiguous_draws(world: dict[str, Any]) -> None:
    import pysam

    starts = np.array([0, 49_990, 100])
    ends = np.array([150, 50_060, 101])
    with pysam.FastaFile(str(world["fasta"])) as fasta:
        observed = fragment_gc(fasta, "chr1", starts, ends)
    sequence = world["sequence"]
    assert observed[0] == sum(base in "GC" for base in sequence[0:150])
    strong = sum(base in "GC" for base in sequence[49_990:50_060])
    drawn = int(np.random.default_rng(49_990).integers(2, size=40).sum())
    assert observed[1] == strong + drawn
    assert observed[2] == int(sequence[100] in "GC")


def test_savgol_matches_the_interp_edge_definition() -> None:
    rng = np.random.default_rng(3)
    values = rng.random(40)
    smoothed = griffin_ont._savgol(values, 11, 3)
    x = np.arange(-5, 6)
    interior = np.polyval(np.polyfit(x, values[10:21], 3), 0)
    assert smoothed[15] == pytest.approx(interior)
    edge = np.polyval(np.polyfit(np.arange(11), values[:11], 3), 2)
    assert smoothed[2] == pytest.approx(edge)
    cubic = 0.5 * np.arange(40) ** 3 - np.arange(40)
    assert np.allclose(griffin_ont._savgol(cubic.astype(float), 11, 3), cubic)


def test_depth_and_scope_refusals(tmp_path: Path, world: dict[str, Any]) -> None:
    starts, ends = plant_fragments(world, n=5000)
    with pytest.raises(GriffinOntDepth):
        run_ont(_config(tmp_path, world, starts, ends, minimum_reads=len(starts) + 1), mappability=_ones)
    with pytest.raises(GriffinOntDepth):
        run_ont(_config(tmp_path, world, starts[:40], ends[:40], minimum_reads=1), mappability=_ones)
    with pytest.raises(ValueError, match="reference FASTA differs"):
        run_ont(_config(tmp_path, world, starts, ends, reference_sha256="0" * 64), mappability=_ones)
    assert not (tmp_path / "attempt").exists()


# ----------------------------------------------------- algorithm boundary
def _ont_input(tmp_path: Path, world: dict[str, Any], starts: np.ndarray, ends: np.ndarray) -> Path:
    """A single-end ONT BAM of the planted spans and its Lab read summary."""

    import pysam

    from traceback_lab.read_summary import write_summary

    input_dir = tmp_path / "input"
    input_dir.mkdir()
    bam = input_dir / "aligned.bam"
    header = {"HD": {"VN": "1.6", "SO": "unsorted"}, "SQ": [{"SN": "chr1", "LN": CHROM_LENGTH}],
              "RG": [{"ID": "rg1", "PL": "ONT"}]}
    with pysam.AlignmentFile(str(bam), "wb", header=header) as out:
        for index, (start, end) in enumerate(zip(starts.tolist(), ends.tolist())):
            read = pysam.AlignedSegment()
            read.query_name = f"read{index}"
            read.query_sequence = "A" * (end - start)
            read.flag = 0x10 if index % 2 else 0
            read.reference_id = 0
            read.reference_start = start
            read.mapping_quality = 60
            read.cigarstring = f"{end - start}M"
            out.write(read)
    write_summary(bam, tmp_path / "read-summary" / "columns")
    return input_dir


def _ont_algorithm(monkeypatch: pytest.MonkeyPatch, world: dict[str, Any]) -> Any:
    import traceback_lab.algorithms.griffin as module
    from traceback_lab.griffin_assets import GriffinAssets

    assets = GriffinAssets(
        upstream_root=world["fasta"].parent, ref_assets=world["assets"], site_lists={"ToyTF": world["sites"]},
        primary_site="ToyTF", sha256="d" * 64, source_label="synthetic", terms="unknown", license_id=None,
    )
    monkeypatch.setattr(module, "MIN_ELIGIBLE_AUTOSOMAL_READS", 1000)
    monkeypatch.setattr(module, "inspect_griffin_assets", lambda _: assets)
    monkeypatch.setattr(module, "registration_available", lambda _: True)
    monkeypatch.setattr(module.importlib.util, "find_spec", lambda name: object())
    monkeypatch.setattr(griffin_ont, "_open_mappability", lambda _: (_ones, lambda: None))
    return module


def _ont_context(tmp_path: Path, world: dict[str, Any], input_dir: Path, **overrides: Any) -> Any:
    from traceback_lab.registry import InputContext

    values: dict[str, Any] = dict(
        root=tmp_path, source_id="1" * 64, input_id=INPUT_ID, reference_id="hg38",
        reference_sha256=_digest(world["fasta"]), input_dir=input_dir, platform="ont",
        eligible_reads=100_000,
        extras={"reference_fasta": str(world["fasta"]),
                "upstream": {"read-summary": str(tmp_path / "read-summary")}},
    )
    values.update(overrides)
    return InputContext(**values)


def test_ont_input_runs_and_publishes_the_illumina_artifact_schema(
    tmp_path: Path, world: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    from traceback_lab.algorithms.griffin import GRIFFIN
    from traceback_lab.registry import Ready

    starts, ends = plant_fragments(world, n=60_000)
    input_dir = _ont_input(tmp_path, world, starts, ends)
    _ont_algorithm(monkeypatch, world)
    assert "ont" in GRIFFIN.requires.platforms and "illumina" in GRIFFIN.requires.platforms
    ctx = _ont_context(tmp_path, world, input_dir)
    assert GRIFFIN.preflight(ctx) == Ready()
    out = tmp_path / "scratch" / "artifacts"
    out.mkdir(parents=True)
    result = GRIFFIN.run(ctx, out)
    assert [metric["metric_id"] for metric in result.metrics] == ["central_coverage", "site_count"]
    assert result.metrics[1]["value"] == len(SITES)
    assert result.panels == [{"panel_id": "tf-site-profile", "kind": "density",
                              "data_ref": "artifacts/griffin_profile.json"}]
    assert set(result.artifacts) == {"griffin_profile.json", "griffin_corrected_coverage.tsv", "griffin_provenance.json"}
    panel = json.loads(result.artifacts["griffin_profile.json"])
    assert panel["primary_site"] == "ToyTF"
    assert set(panel["profiles"][0]) == {
        "site_name", "site_count", "bin_start_bp", "gc_corrected_coverage", "central_coverage",
        "amplitude", "smoothing", "cna_normalization",
    }
    assert len(panel["profiles"][0]["bin_start_bp"]) == 132
    assert panel["profiles"][0]["central_coverage"] < 0.8
    provenance = json.loads(result.artifacts["griffin_provenance.json"])
    assert provenance["platform"] == "ont" and provenance["eligibility_source"] == "read_summary"
    assert provenance["eligible_autosomal_reads"] == len(starts)
    assert str(tmp_path) not in result.artifacts["griffin_provenance.json"].decode()


def test_ont_identity_is_separate_and_illumina_identity_is_unchanged(
    tmp_path: Path, world: dict[str, Any],
) -> None:
    from traceback_lab.algorithms.griffin import GRIFFIN

    illumina = _ont_context(tmp_path, world, tmp_path, platform="illumina")
    assert GRIFFIN.identity_parameters(illumina) is GRIFFIN.parameters
    ont = GRIFFIN.identity_parameters(_ont_context(tmp_path, world, tmp_path))
    assert {key: ont[key] for key in GRIFFIN.parameters} == GRIFFIN.parameters
    assert ont["ont_method"] == griffin_ont.ONT_METHOD and ont["input_mode"] == "ont_single_end"


def test_ont_derived_split_runs_on_its_half_and_gates_depth(
    tmp_path: Path, world: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    from traceback_lab.algorithms.griffin import GRIFFIN
    from traceback_lab.registry import Ready, Skip

    starts, ends = plant_fragments(world, n=60_000)
    input_dir = _ont_input(tmp_path, world, starts, ends)
    module = _ont_algorithm(monkeypatch, world)
    split = _ont_context(tmp_path, world, input_dir, derived={"transform": "split", "half": "A"})
    assert GRIFFIN.preflight(split) == Ready()
    out = tmp_path / "split" / "artifacts"
    out.mkdir(parents=True)
    provenance = json.loads(GRIFFIN.run(split, out).artifacts["griffin_provenance.json"])
    assert 0.4 * len(starts) < provenance["eligible_autosomal_reads"] < 0.6 * len(starts)
    monkeypatch.setattr(module, "MIN_ELIGIBLE_AUTOSOMAL_READS", len(starts))
    deep = tmp_path / "deep" / "artifacts"
    deep.mkdir(parents=True)
    decision = GRIFFIN.run(split, deep)
    assert isinstance(decision, Skip) and decision.reason_code == "depth"
    assert "D21-eligible autosomal reads" in decision.message
    shallow = GRIFFIN.preflight(_ont_context(tmp_path, world, input_dir, eligible_reads=10))
    assert isinstance(shallow, Skip) and shallow.reason_code == "depth"
    bad = GRIFFIN.preflight(_ont_context(tmp_path, world, input_dir, derived={"transform": "other"}))
    assert isinstance(bad, Skip) and bad.reason_code == "derived_input"


def test_paired_records_on_an_ont_declared_input_are_refused(
    tmp_path: Path, world: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    from traceback_lab.algorithms.griffin import GRIFFIN
    from traceback_lab.read_summary import ReadSummary
    from traceback_lab.registry import Skip

    starts, ends = plant_fragments(world, n=3000)
    input_dir = _ont_input(tmp_path, world, starts, ends)
    _ont_algorithm(monkeypatch, world)
    flags = tmp_path / "read-summary" / "columns" / "flag.bin"
    column = np.fromfile(flags, dtype="<u2")
    column[0] |= 0x1
    column.tofile(flags)
    assert ReadSummary.open(tmp_path / "read-summary" / "columns").column("flag")[0] & 0x1
    out = tmp_path / "scratch" / "artifacts"
    out.mkdir(parents=True)
    decision = GRIFFIN.run(_ont_context(tmp_path, world, input_dir), out)
    assert isinstance(decision, Skip) and decision.reason_code == "platform"
    assert "single-end" in decision.message
