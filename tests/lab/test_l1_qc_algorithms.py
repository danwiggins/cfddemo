"""L1.1: modbase-qc, long-tail, sex-check and mtdna on planted synthetic BAMs."""

from __future__ import annotations

from pathlib import Path

import pysam
import pytest

from traceback_lab.algorithms.long_tail import LONG_TAIL
from traceback_lab.algorithms.modbase_qc import MODBASE_QC
from traceback_lab.algorithms.mtdna import MTDNA
from traceback_lab.algorithms.sex_check import HG38_CHRY_LENGTH, SEX_CHECK, _non_n_bases, call_sex
from traceback_lab.modbase import mm_codes, mm_skip_modes
from traceback_lab.registry import Ready, Skip

from .bam_fixtures import Read, context_for, values


def _with_meth_calls(ctx):  # type: ignore[no-untyped-def]
    """Run the meth-calls intermediate on the parent once and wire it upstream (v2 modbase-qc)."""

    from traceback_lab.algorithms.meth_calls import METH_CALLS

    store = ctx.root / "meth-calls"
    if not (store / "calls" / "meta.json").exists():
        parent = context_for(ctx.root, MOD_CONTIGS, ())
        METH_CALLS.run(parent, store)
    ctx.extras["upstream"]["meth-calls"] = str(store)
    return ctx

# ------------------------------------------------------------------ modbase-qc
MOD_CONTIGS = [("chr1", 10_000)]


def _mod_reads(*, tags: bool = True) -> list[Read]:
    reads = [
        # forward, CpGs at 1 and 5; ML order follows MM: h1, h2, m1, m2
        Read("r1", "chr1", 100, "ACGTTCGAAA", mm="C+h?,0,0;C+m?,0,0;", ml=[10, 20, 230, 100]),
        # reverse: original read AACGTTCGAA, stored as its reverse complement
        Read("r2", "chr1", 200, "TTCGAACGTT", flag=16, mm="C+m?,0,0;", ml=[255, 0]),
        # a C+m call outside CpG context is not a CpG call
        Read("r3", "chr1", 300, "ACATTTTTTT", mm="C+m?,0;", ml=[200]),
        # MN mismatch (hard-clipped supplementary after -y): no calls, counted
        Read("r4", "chr1", 400, "ACGTTTTTTT", mm="C+m?,0;", ml=[200], mn=99),
        Read("r5", "chr1", 500, "ACGTTTTTTT"),
        # not D21-eligible (MAPQ 5)
        Read("r6", "chr1", 600, "ACGTTTTTTT", mapq=5, mm="C+m?,0;", ml=[0]),
    ]
    if not tags:
        for read in reads:
            read.mm, read.ml = None, ()
    return reads


def test_modbase_qc_planted_values(tmp_path: Path) -> None:
    ctx = _with_meth_calls(context_for(tmp_path, MOD_CONTIGS, _mod_reads()))
    assert isinstance(MODBASE_QC.preflight(ctx), Ready)
    out = MODBASE_QC.run(ctx, tmp_path / "out")
    got = values(out)
    assert got["mod_codes"] == ["C+h", "C+m"]
    assert got["mm_tagged_fraction"] == pytest.approx(4 / 5)
    assert got["ml_mean"] == pytest.approx((10 + 230 + 20 + 100 + 255 + 0) / (255 * 6))
    # r1 pos5: h=20, m=100, canonical=135 -> max 0.53 < 0.7; the other 3 calls are confident
    assert got["ml_below_filter_fraction"] == pytest.approx(1 / 4)
    assert got["invalid_mm_records"] == 1


def test_missing_ml_is_invalid_and_opposite_strand_g_calls_count(tmp_path: Path) -> None:
    from traceback_lab.modbase import InvalidMM, cpg_calls

    header = pysam.AlignmentHeader.from_dict({"SQ": [{"SN": "chr1", "LN": 100}]})
    record = pysam.AlignedSegment(header)
    record.query_sequence = "ACGT"
    record.set_tag("MM", "C+m?,0;")
    with pytest.raises(InvalidMM):
        list(cpg_calls(record))
    record.set_tag("MM", "G-m?,0;")
    record.set_tag("ML", __import__("array").array("B", [200]))
    calls = list(cpg_calls(record))
    assert [(c.qpos, c.probabilities) for c in calls] == [(2, {"m": 200})]


def test_modbase_qc_skips_without_mm_tags(tmp_path: Path) -> None:
    ctx = context_for(tmp_path, MOD_CONTIGS, _mod_reads(tags=False))
    decision = MODBASE_QC.preflight(ctx)
    assert isinstance(decision, Skip) and decision.reason_code == "missing_tags"


def test_modbase_qc_split_halves_partition_the_calls(tmp_path: Path) -> None:
    full = MODBASE_QC.run(_with_meth_calls(context_for(tmp_path, MOD_CONTIGS, _mod_reads())), tmp_path / "o")
    halves = [
        MODBASE_QC.run(
            _with_meth_calls(context_for(tmp_path, MOD_CONTIGS, (), derived={"transform": "split", "half": h})), tmp_path / "o"
        )
        for h in "AB"
    ]

    def n(output, metric_id):  # type: ignore[no-untyped-def]
        return next(m["n"] for m in output.metrics if m["metric_id"] == metric_id)

    for metric_id in ("ml_below_filter_fraction", "mm_tagged_fraction", "ml_mean"):
        # every call is reached from exactly one half
        assert n(halves[0], metric_id) + n(halves[1], metric_id) == n(full, metric_id)


def test_mm_code_and_skip_mode_parsing() -> None:
    assert mm_codes("C+hm?,1,2;") == ["C+h", "C+m"]
    assert mm_codes("C+m.,0;A+a,1;") == ["C+m", "A+a"]
    assert mm_codes("") == []
    assert mm_skip_modes("C+m?,0;C+h,1;") == {"C+m": "?", "C+h": "."}


# ------------------------------------------------------------------- long-tail
def test_long_tail_shares_and_split_reads_use_read_length(tmp_path: Path) -> None:
    reads = [Read(f"s{i}", "chr1", 1000 + 10 * i, "A" * 150) for i in range(6)]
    reads += [Read("m1", "chr1", 5000, "A" * 400), Read("m2", "chr1", 6000, "A" * 400)]
    reads += [Read("k1", "chr1", 10_000, "A" * 2000), Read("l1", "chr1", 20_000, "A" * 8000)]
    # split molecule: primary spans 200 bp of a 3,000 bp read; its supplementary piece elsewhere
    reads.append(Read("sp", "chr1", 40_000, "A" * 3000, cigar=[(0, 200), (4, 2800)]))
    reads.append(Read("sp", "chr1", 60_000, "A" * 2800, flag=2048, cigar=[(5, 200), (0, 2800)]))
    reads.append(Read("lowq", "chr1", 70_000, "A" * 9000, mapq=0))
    ctx = context_for(tmp_path, [("chr1", 100_000)], reads)
    got = values(LONG_TAIL.run(ctx, tmp_path / "o"))
    assert got["gt300_fraction"] == pytest.approx(5 / 11)
    assert got["kb1_4_fraction"] == pytest.approx(2 / 11)
    assert got["gt7500_fraction"] == pytest.approx(1 / 11)


def test_long_tail_empty_input_is_null_not_an_error(tmp_path: Path) -> None:
    ctx = context_for(tmp_path, [("chr1", 1000)], [Read("u", "chr1", 0, "A" * 50, flag=4)])
    out = LONG_TAIL.run(ctx, tmp_path / "o")
    assert all(m["value"] is None and m["n"] == 0 for m in out.metrics)


# ------------------------------------------------------------------- sex-check
HG38_X = 156_040_895
SEX_CONTIGS = [("chr1", 1_000_000), ("chrX", HG38_X), ("chrY", HG38_CHRY_LENGTH)]


def _sex_reads(y_reads: int, par_reads: int = 0) -> list[Read]:
    reads = [Read(f"x{i}", "chrX", 10_000_000 + 1000 * i, "A" * 100) for i in range(100)]
    reads += [Read(f"y{i}", "chrY", 10_000_000 + 1000 * i, "A" * 100) for i in range(y_reads)]
    reads += [Read(f"p{i}", "chrY", 100_000 + 1000 * i, "A" * 100) for i in range(par_reads)]
    return reads


def test_sex_check_ratio_excludes_pars_and_calls_male(tmp_path: Path) -> None:
    ctx = context_for(tmp_path, SEX_CONTIGS, _sex_reads(20, par_reads=5), eligible_reads=600_000)
    assert isinstance(SEX_CHECK.preflight(ctx), Ready)
    got = values(SEX_CHECK.run(ctx, tmp_path / "o"))
    y_mb = (HG38_CHRY_LENGTH - (2_781_479 - 10_000) - (57_217_415 - 56_887_902)) / 1e6
    assert got["chry_chrx_ratio"] == pytest.approx((20 / y_mb) / (100 / (HG38_X / 1e6)))
    assert got["call"] == "male"


def test_sex_check_zero_chry_reads_is_female(tmp_path: Path) -> None:
    ctx = context_for(tmp_path, SEX_CONTIGS, _sex_reads(0), eligible_reads=600_000)
    got = values(SEX_CHECK.run(ctx, tmp_path / "o"))
    assert got["chry_chrx_ratio"] == 0.0 and got["call"] == "female"


def test_sex_check_skips_below_depth_and_without_chry(tmp_path: Path) -> None:
    shallow = context_for(tmp_path / "a", SEX_CONTIGS, _sex_reads(3), eligible_reads=120)
    decision = SEX_CHECK.preflight(shallow)
    assert isinstance(decision, Skip) and decision.reason_code == "depth"
    no_y = context_for(tmp_path / "b", [("chr1", 1000), ("chrX", 5000)], [Read("x", "chrX", 0, "A" * 50)], eligible_reads=600_000)
    decision = SEX_CHECK.preflight(no_y)
    assert isinstance(decision, Skip) and decision.reason_code == "reference"


def test_sex_call_thresholds() -> None:
    assert [call_sex(v) for v in (None, 0.0, 0.049, 0.05, 0.3, 0.31)] == [
        None, "female", "female", "indeterminate", "indeterminate", "male"
    ]


def test_mappable_length_counts_non_n_bases_outside_excluded(tmp_path: Path) -> None:
    fasta = tmp_path / "ref.fa"
    fasta.write_text(">chrY\n" + "N" * 10 + "ACGT" * 5 + "N" * 10 + "\n")
    pysam.faidx(str(fasta))
    assert _non_n_bases(str(fasta), "chrY", 40, ()) == 20
    assert _non_n_bases(str(fasta), "chrY", 40, ((10, 15),)) == 15
    assert _non_n_bases(None, "chrY", 40, ((10, 15),)) == 35


# ----------------------------------------------------------------------- mtdna
def test_mtdna_fraction_and_mode(tmp_path: Path) -> None:
    reads = [Read(f"n{i}", "chr1", 100 * i, "A" * 170) for i in range(90)]
    reads += [Read(f"m{i}", "chrM", 50 * i, "A" * 80) for i in range(8)]
    reads += [Read(f"q{i}", "chrM", 5000 + 50 * i, "A" * 120) for i in range(2)]
    reads.append(Read("numt", "chrM", 9000, "A" * 80, mapq=0))
    ctx = context_for(tmp_path, [("chr1", 100_000), ("chrM", 16_569)], reads)
    assert isinstance(MTDNA.preflight(ctx), Ready)
    out = MTDNA.run(ctx, tmp_path / "o")
    got = values(out)
    assert got["chrm_fraction"] == pytest.approx(10 / 101)  # denominator: all primaries, incl. the MAPQ-0 one
    assert got["chrm_mode_bp"] == 80
    assert out.panels[0]["kind"] == "density" and "chrm_size_hist.json" in out.artifacts


def test_mtdna_mode_is_not_truncated_by_the_panel_range(tmp_path: Path) -> None:
    reads = [Read(f"l{i}", "chrM", 100 * i, "A" * 1200) for i in range(3)] + [Read("s", "chrM", 5000, "A" * 80)]
    ctx = context_for(tmp_path, [("chr1", 10_000), ("chrM", 16_569)], reads)
    assert values(MTDNA.run(ctx, tmp_path / "o"))["chrm_mode_bp"] == 1200


def test_mtdna_skips_without_chrm(tmp_path: Path) -> None:
    ctx = context_for(tmp_path, [("chr1", 1000)], [Read("a", "chr1", 0, "A" * 50)])
    decision = MTDNA.preflight(ctx)
    assert isinstance(decision, Skip) and decision.reason_code == "reference"


def test_every_l1_1_metric_has_a_citation_checked_library_entry() -> None:
    from traceback_lab.interpretation.engine import load_library

    library = load_library()
    expected = {
        "modbase-qc": ["mod_codes", "mm_tagged_fraction", "ml_mean", "ml_below_filter_fraction", "invalid_mm_records"],
        "long-tail": ["gt300_fraction", "kb1_4_fraction", "gt7500_fraction"],
        "sex-check": ["chry_chrx_ratio", "call"],
        "mtdna": ["chrm_fraction", "chrm_mode_bp"],
    }
    for algorithm_id, metric_ids in expected.items():
        assert library.headline(algorithm_id) is not None
        for metric_id in metric_ids:
            entry = library.entry(algorithm_id, metric_id)
            assert entry is not None and entry.citation_verified, (algorithm_id, metric_id)
    assert "PRIVACY" in library.entry("sex-check", "chry_chrx_ratio")["flags"]


def test_modbase_qc_v2_counts_only_aligned_cpgs_and_masks_invalid_rows(tmp_path: Path) -> None:
    reads = [
        Read("ok", "chr1", 100, "ACGTTTTTTT", mm="C+m?,0;", ml=[255]),
        # an adapter-like CpG in a soft clip: dropped by meth-calls, so not in the ML stats
        Read("clip", "chr1", 200, "CGAAAAAAAA", cigar=[(4, 2), (0, 8)], mm="C+m?,0;", ml=[10]),
        Read("bad", "chr1", 300, "ACGTTTTTTT", mm="C+m?,0;", ml=[10], mn=3),
    ]
    got = values(MODBASE_QC.run(_with_meth_calls(context_for(tmp_path, MOD_CONTIGS, reads)), tmp_path / "o"))
    assert got["ml_mean"] == pytest.approx(1.0) and got["ml_below_filter_fraction"] == 0.0
    assert got["invalid_mm_records"] == 1
    halves = [
        values(
            MODBASE_QC.run(
                _with_meth_calls(context_for(tmp_path, MOD_CONTIGS, (), derived={"transform": "split", "half": h})),
                tmp_path / "o",
            )
        )["invalid_mm_records"]
        for h in "AB"
    ]
    assert sorted(halves) == [0, 1]


def test_modbase_qc_ml_stats_ignore_implicit_canonical_calls(tmp_path: Path) -> None:
    reads = [Read("dot", "chr1", 100, "ACGTCG", mm="C+m.,0;", ml=[128])]
    got = values(MODBASE_QC.run(_with_meth_calls(context_for(tmp_path, MOD_CONTIGS, reads)), tmp_path / "o"))
    assert got["ml_mean"] == pytest.approx(128 / 255)
    assert got["ml_below_filter_fraction"] == pytest.approx(1.0)
