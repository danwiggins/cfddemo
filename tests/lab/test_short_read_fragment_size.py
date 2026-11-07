"""Synthetic paired-BAM checks for the opt-in Illumina TLEN slice."""

from __future__ import annotations

import json
from pathlib import Path

import pysam
import pytest

from traceback_lab.algorithms.fragment_size import FRAGMENT_SIZE
from traceback_lab.algorithms.short_read_fragment_size import (
    SHORT_READ_FRAGMENT_SIZE,
    _tlen_counts,
)
from traceback_lab.interpretation.engine import load_library
from traceback_lab.registry import InputContext, PlanItem, Ready, Skip, execution_identity
from traceback_lab.runner import RunTarget, run_plan
from traceback_lab.scrub import Scrubber
from traceback_lab.store import LabStore, new_run_id


def _context(input_dir: Path, *, derived: dict | None = None) -> InputContext:
    return InputContext(
        root=input_dir.parent, source_id="a" * 64, input_id="b" * 64,
        reference_id="hg38", reference_sha256="c" * 64,
        input_dir=input_dir, derived=derived,
    )


def _write_bam(
    input_dir: Path, *, kind: str = "prealigned", platforms: tuple[str, ...] = ("ILLUMINA",),
    pairs: tuple[tuple[str, int, int, int], ...] = (),
    references: tuple[str, ...] = ("chr1", "chr2"),
) -> None:
    input_dir.mkdir()
    (input_dir / "manifest.json").write_text(json.dumps({"kind": kind}))
    header = {
        "HD": {"VN": "1.6"},
        "SQ": [{"SN": name, "LN": 10000} for name in references],
        "RG": [{"ID": f"group-{i}", "PL": platform} for i, platform in enumerate(platforms)],
    }
    with pysam.AlignmentFile(str(input_dir / "aligned.bam"), "wb", header=header) as bam:
        for name, length, flags, mapq in pairs:
            contig = references.index("MT") if name == "mito" else (
                references.index("decoy") if name == "decoy" else 0
            )
            first = pysam.AlignedSegment()
            first.query_name = name
            first.query_sequence = "A" * 100
            first.flag = flags | 0x40
            first.mapping_quality = mapq
            first.reference_id = contig
            first.reference_start = 100
            first.cigarstring = "100M"
            first.next_reference_id = 1 if name == "other-contig" else contig
            first.next_reference_start = 200
            first.template_length = length
            if platforms:
                first.set_tag("RG", "group-0")
            bam.write(first)
            second = pysam.AlignedSegment()
            second.query_name = name
            second.query_sequence = "A" * 100
            second.flag = flags | 0x80
            second.mapping_quality = mapq
            second.reference_id = contig
            second.reference_start = 200
            second.cigarstring = "100M"
            second.next_reference_id = contig
            second.next_reference_start = 100
            second.template_length = -length
            if platforms:
                second.set_tag("RG", "group-0")
            bam.write(second)


def test_tlen_is_absolute_one_template_per_pair_and_not_ont_span(tmp_path: Path) -> None:
    input_dir = tmp_path / "input"
    proper = 0x1 | 0x2
    _write_bam(input_dir, pairs=(
        ("good-1", 180, proper, 60),
        ("good-2", 180, proper, 60),
        ("good-3", 180, proper, 60),
        ("short", 120, proper, 60),
        ("di-1", -300, proper, 60),
        ("di-2", 300, proper, 60),
        ("long-overflow", 1500, proper, 60),
        ("low-mapq", 170, proper, 19),
        ("duplicate", 170, proper | 0x400, 60),
        ("qc-fail", 170, proper | 0x200, 60),
        ("improper", 170, 0x1, 60),
        ("mate-unmapped", 170, proper | 0x8, 60),
        ("other-contig", 170, proper, 60),
        ("zero-tlen", 0, proper, 60),
    ))
    ctx = _context(input_dir)
    assert isinstance(SHORT_READ_FRAGMENT_SIZE.preflight(ctx), Ready)
    assert _tlen_counts(ctx) == {180: 3, 120: 1, 300: 2, 1001: 1}
    output = SHORT_READ_FRAGMENT_SIZE.run(ctx, tmp_path)
    metrics = {item["metric_id"]: item for item in output.metrics}
    assert metrics["mode_bp"]["value"] == 180
    assert metrics["short_fraction_lt150"]["value"] == pytest.approx(1 / 6)
    assert metrics["short_fraction_lt150"]["n"] == 6
    assert metrics["peak_di_bp"]["value"] == 300
    hist = json.loads(output.artifacts["tlen_hist.json"])
    assert hist["count"][119] == 1
    assert hist["count"][179] == 3
    assert hist["count"][299] == 2
    assert "span_bp" not in hist
    # The first mate's aligned span is 100 bp, proving this is a separate definition.
    with pysam.AlignmentFile(str(input_dir / "aligned.bam"), "rb") as bam:
        assert next(bam.fetch(until_eof=True)).reference_length == 100
    assert FRAGMENT_SIZE.id == "fragment-size"
    assert execution_identity(SHORT_READ_FRAGMENT_SIZE, reference_sha256="c" * 64) != execution_identity(
        FRAGMENT_SIZE, reference_sha256="c" * 64,
    )


@pytest.mark.parametrize("kind,platforms,reason", [
    ("aligned", ("ILLUMINA",), "platform"),
    ("prealigned", ("ONT",), "platform"),
    ("prealigned", ("ILLUMINA", "ONT"), "platform"),
    ("prealigned", (), "platform"),
])
def test_preflight_requires_prealigned_declared_illumina(
    tmp_path: Path, kind: str, platforms: tuple[str, ...], reason: str,
) -> None:
    input_dir = tmp_path / "input"
    _write_bam(input_dir, kind=kind, platforms=platforms)
    decision = SHORT_READ_FRAGMENT_SIZE.preflight(_context(input_dir))
    assert isinstance(decision, Skip)
    assert decision.reason_code == reason


def test_platform_skip_is_published_by_real_runner(tmp_path: Path) -> None:
    store = LabStore(tmp_path / "root")
    store.ensure()
    input_dir = tmp_path / "input"
    _write_bam(input_dir, kind="aligned", platforms=("ONT",))
    target = RunTarget(
        input_id="b" * 64, source_id="a" * 64, input_dir=input_dir,
        reference_id="hg38", reference_sha256="c" * 64,
    )
    run_id = new_run_id()
    log = store.run_log(run_id)
    result = run_plan(
        store, None, log, target, [PlanItem(SHORT_READ_FRAGMENT_SIZE, False)],
        {"short-read-fragment-size": "traceback_lab.algorithms.short_read_fragment_size:SHORT_READ_FRAGMENT_SIZE"},
        run_id=run_id, scrubber=Scrubber(root=store.root),
    )[0]
    assert result.status == "skipped" and result.skip_reason == "platform"
    stored = store.read_result(target.input_id, result.algorithm_id, result.parameters_sha256)
    assert stored is not None and stored.status == "skipped" and stored.skip_reason == "platform"
    assert any(event["event"] == "step_end" and event["skip_reason"] == "platform" for event in log.events())


def test_derived_pair_selection_uses_read_name_predicate(tmp_path: Path) -> None:
    from traceback_lab.algorithms._records import name_predicate

    input_dir = tmp_path / "input"
    names = tuple(f"pair-{i}" for i in range(50))
    _write_bam(input_dir, pairs=tuple((name, 180, 0x1 | 0x2, 60) for name in names))
    full = _tlen_counts(_context(input_dir))
    half_a = {"transform": "split", "half": "A"}
    half_b = {"transform": "split", "half": "B"}
    a = _tlen_counts(_context(input_dir, derived=half_a))
    b = _tlen_counts(_context(input_dir, derived=half_b))
    assert full == {180: 50}
    assert a[180] + b[180] == 50
    assert a[180] == sum(name_predicate(half_a)(name) for name in names)


def test_unprefixed_nuclear_contigs_exclude_mt_and_decoy(tmp_path: Path) -> None:
    input_dir = tmp_path / "input"
    proper = 0x1 | 0x2
    _write_bam(
        input_dir,
        references=("1", "MT", "decoy"),
        pairs=(("nuclear", 180, proper, 60), ("mito", 120, proper, 60),
               ("decoy", 300, proper, 60)),
    )
    ctx = _context(input_dir)
    assert isinstance(SHORT_READ_FRAGMENT_SIZE.preflight(ctx), Ready)
    assert _tlen_counts(ctx) == {180: 1}


def test_all_filtered_pairs_have_null_metrics_and_fixed_empty_histogram(tmp_path: Path) -> None:
    input_dir = tmp_path / "input"
    proper = 0x1 | 0x2
    _write_bam(input_dir, pairs=(("low-mapq", 180, proper, 19),
                                 ("duplicate", 180, proper | 0x400, 60),
                                 ("zero-tlen", 0, proper, 60)))
    ctx = _context(input_dir)
    assert isinstance(SHORT_READ_FRAGMENT_SIZE.preflight(ctx), Ready)
    assert _tlen_counts(ctx) == {}
    output = SHORT_READ_FRAGMENT_SIZE.run(ctx, tmp_path)
    assert all(item["value"] is None and item["n"] == 0 for item in output.metrics)
    histogram = json.loads(output.artifacts["tlen_hist.json"])
    assert len(histogram["tlen_bp"]) == len(histogram["count"]) == 1000
    assert sum(histogram["count"]) == 0


def test_no_canonical_nuclear_contigs_skips_explicitly(tmp_path: Path) -> None:
    input_dir = tmp_path / "input"
    _write_bam(
        input_dir, references=("MT", "decoy"),
        pairs=(("mito", 120, 0x1 | 0x2, 60),),
    )
    decision = SHORT_READ_FRAGMENT_SIZE.preflight(_context(input_dir))
    assert isinstance(decision, Skip)
    assert decision.reason_code == "reference"


def test_short_read_library_has_no_borrowed_ont_range() -> None:
    library = load_library()
    for metric_id in (
        "mode_bp", "short_fraction_lt150", "peak_mono_bp", "peak_di_bp", "periodicity_10bp_power",
    ):
        entry = library.entry("short-read-fragment-size", metric_id)
        assert entry is not None
        assert entry["reference"] == {"kind": "none"}
        assert "Illumina" in entry["origin_platform"]
    assert library.headline("short-read-fragment-size")["metric_id"] == "mode_bp"


def test_opt_in_lab_run_accepts_synthetic_prealigned_illumina_bam(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from traceback_runner import cli as signed_cli
    from traceback_runner.references import register_reference

    reference = tmp_path / "reference.fa"
    reference.write_text(">chr1\n" + "A" * 10000 + "\n>chr2\n" + "A" * 10000 + "\n")
    pysam.faidx(str(reference))
    root = tmp_path / "root"
    register_reference(root, reference, "hg38")
    source_dir = tmp_path / "source"
    _write_bam(source_dir, pairs=(("template-a", 180, 0x1 | 0x2, 60),))
    sorted_bam = tmp_path / "illumina_aligned.bam"
    pysam.sort("-o", str(sorted_bam), str(source_dir / "aligned.bam"))
    code = signed_cli.main([
        "lab", "run", str(sorted_bam), "--root", str(root), "--reference", "hg38",
        "--only", "short-read-fragment-size", "--no-downsample", "--no-controls", "--json",
    ])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0, payload
    assert payload["data"]["counts"]["ok"] == 1
    assert payload["data"]["steps"][0]["algorithm_id"] == "short-read-fragment-size"


def test_normal_lab_run_dispatches_illumina_without_ont_results(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    from traceback_runner import cli as signed_cli
    from traceback_runner.references import register_reference

    reference = tmp_path / "reference.fa"
    reference.write_text(">chr1\n" + "A" * 10000 + "\n>chr2\n" + "A" * 10000 + "\n")
    pysam.faidx(str(reference))
    root = tmp_path / "root"
    register_reference(root, reference, "hg38")
    source_dir = tmp_path / "source"
    _write_bam(source_dir, pairs=(("template-a", 180, 0x1 | 0x2, 60),))
    sorted_bam = tmp_path / "illumina_aligned.bam"
    pysam.sort("-o", str(sorted_bam), str(source_dir / "aligned.bam"))
    code = signed_cli.main([
        "lab", "run", str(sorted_bam), "--root", str(root), "--reference", "hg38",
        "--downsample", "0.5", "--json",
    ])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0, payload
    assert payload["data"]["sample"]["platform"] == "illumina"
    steps = {step["algorithm_id"]: step for step in payload["data"]["steps"]}
    for algorithm_id in ("read-summary", "depth", "bam-qc", "short-read-fragment-size"):
        assert steps[algorithm_id]["status"] == "ok", steps[algorithm_id]
    for algorithm_id in ("fragment-size", "copy-number", "cell-origin", "long-tail", "meth-calls"):
        assert steps[algorithm_id]["status"] == "skipped"
        assert steps[algorithm_id]["skip_reason"] == "platform"
    from traceback_lab.store import LabStore
    from traceback_lab.views import depth_curve, load_sample

    sample = load_sample(LabStore(root), payload["data"]["sample"]["source_id"])
    assert sample is not None
    assert sample.full.results["cell-origin"].skip_reason == "platform"
    fragment_row = next(row for row in depth_curve(sample)["rows"]
                        if row["algorithm_id"] == "fragment-size")
    assert all(cell["state"] == "skipped" and cell["reason"] == "platform"
               for cell in fragment_row["cells"])
    assert payload["data"]["counts"]["failed"] == 0
    assert payload["data"]["controls"]["cna_skipped"] == "platform"
    assert payload["data"]["controls"]["split"] is True
    assert any(step["algorithm_id"] == "short-read-fragment-size" and
               step["input"] != "full" and step["status"] == "ok"
               for step in payload["data"]["steps"])
    assert any(step["algorithm_id"] == "fragment-size" and
               step["input"] != "full" and step["status"] == "skipped" and
               step["skip_reason"] == "platform"
               for step in payload["data"]["steps"])

    from traceback_lab.cohorts import add_sample, build_cohort

    store = LabStore(root)
    source_id = payload["data"]["sample"]["source_id"]
    input_id = payload["data"]["sample"]["input_id"]
    result = sample.full.results["short-read-fragment-size"]
    attempt = store.selected_attempt(input_id, result.algorithm_id, result.provenance.parameters_sha256)
    assert attempt is not None
    plan_path = attempt / "execution_plan.json"
    plan = json.loads(plan_path.read_bytes())
    assert plan["platform"] == "illumina"
    assert add_sample(store, "illumina", source_id)
    generation = build_cohort(store, "illumina")
    assert "short-read-fragment-size" in {
        row.algorithm_id for row in generation.members[0].results
    }

    plan_path.write_text(json.dumps({**plan, "platform": "ont"}))
    tampered = build_cohort(store, "illumina")
    assert "short-read-fragment-size" not in {
        row.algorithm_id for row in tampered.members[0].results
    }


def test_normal_lab_run_dispatches_ont_without_tlen_result(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    from traceback_runner import cli as signed_cli
    from traceback_runner.references import register_reference

    reference = tmp_path / "reference.fa"
    reference.write_text(">chr1\n" + "A" * 10000 + "\n>chr2\n" + "A" * 10000 + "\n")
    pysam.faidx(str(reference))
    root = tmp_path / "root"
    register_reference(root, reference, "hg38")
    source_dir = tmp_path / "source"
    _write_bam(source_dir, platforms=("ONT",), pairs=(("template-a", 180, 0x1 | 0x2, 60),))
    sorted_bam = tmp_path / "ont_aligned.bam"
    pysam.sort("-o", str(sorted_bam), str(source_dir / "aligned.bam"))
    code = signed_cli.main([
        "lab", "run", str(sorted_bam), "--root", str(root), "--reference", "hg38",
        "--no-downsample", "--no-controls", "--json",
    ])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0, payload
    assert payload["data"]["sample"]["platform"] == "ont"
    steps = {step["algorithm_id"]: step for step in payload["data"]["steps"]}
    assert steps["fragment-size"]["status"] == "ok"
    assert steps["short-read-fragment-size"]["status"] == "skipped"
    assert steps["short-read-fragment-size"]["skip_reason"] == "platform"
