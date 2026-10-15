"""S4: the Lab ichor adapter (parity with the signed mapper), derived inputs and controls."""

from __future__ import annotations

import json
import random
from pathlib import Path

import pysam
import pytest

from tests.test_copy_number_stage import (
    PIN,
    _eligible,
    _fake_toolchain,
    _write_assets,
    _write_inputs,
)
from traceback_lab import derived as derived_module
from traceback_lab.adapters import ichor
from traceback_lab.derived import Arm, cna, downsample, materialize, split
from traceback_lab.store import LabStore
from traceback_runner import cli as signed_cli
from traceback_runner.copy_number import build_measurement, write_counting_bam
from traceback_runner.copy_number_method import AUTOSOMES

pytestmark = pytest.mark.skipif(PIN is None, reason="no ichorCNA lock for this platform")


def _assets(directory: Path) -> ichor.IchorAssets:
    extdata = _write_assets(directory)
    return ichor.IchorAssets(
        gc_wig=next(extdata.glob("gc_hg38_*.wig")),
        map_wig=next(extdata.glob("map_hg38_*.wig")),
        centromere=next(path for path in extdata.iterdir() if "centromere" in path.name.lower()),
    )


def test_lab_mapper_matches_the_signed_build_measurement(tmp_path: Path) -> None:
    _, bam = _write_inputs(tmp_path / "inputs")
    toolchain = _fake_toolchain(tmp_path / "toolchain")
    assets = _assets(tmp_path / "extdata")
    parameters = ichor.default_parameters(min_counted_reads=10)
    work = tmp_path / "work"
    work.mkdir()
    scanned, exclusions, counted = write_counting_bam(
        bam, work / "counting.bam", AUTOSOMES, min_mapq=parameters.signed.min_mapq
    )
    assert counted == _eligible()
    ichor.read_counter(toolchain, parameters, work / "counting.bam", work / "counts.wig")
    grid, run_sha256 = ichor.analysis_grid(parameters, assets, work / "counts.wig", counted)
    result = ichor.run_ichor(toolchain, parameters, assets, work / "counts.wig", grid, run_sha256, work)
    lab = ichor.map_result(result, counted_reads=counted, records_scanned=scanned)
    signed = build_measurement(
        result, reference_id="ref", records_scanned=scanned, exclusions=exclusions, counted_reads=counted
    )
    assert lab.ploidy == signed.solution.ploidy
    assert lab.tumor_fraction == (signed.solution.model_fraction if signed.solution.identifiable else None)
    assert lab.identifiable == signed.solution.identifiable
    assert lab.pon_mode == signed.solution.pon_mode == "none_development"
    assert [(b["chr"], b["start"], b["end"], b["log2"], b["mask"]) for b in lab.bins] == [
        (b.chr, b.start, b.end, b.log2_corrected, b.mask) for b in signed.bins
    ]
    assert [(s["chr"], s["start"], s["end"], s["median_log2"], s["copy_number"], s["call"]) for s in lab.segments] == [
        (s.chr, s.start, s.end, s.median_log2, s.copy_number, s.call) for s in signed.segments
    ]


def test_measure_end_to_end_and_floor(tmp_path: Path) -> None:
    _, bam = _write_inputs(tmp_path / "inputs")
    toolchain = _fake_toolchain(tmp_path / "toolchain")
    assets = _assets(tmp_path / "extdata")
    parameters = ichor.default_parameters(min_counted_reads=10)
    work = tmp_path / "work"
    work.mkdir()
    scanned, _, counted = ichor.write_counting(bam, work / "counting.bam", parameters)
    result = ichor.measure(work / "counting.bam", counted, scanned, work, toolchain=toolchain, assets=assets, parameters=parameters)
    assert result.counted_reads == counted and len(result.bins) > 0
    with pytest.raises(ichor.CopyNumberError, match="below the floor"):
        ichor.measure(
            work / "counting.bam", counted, scanned, tmp_path, toolchain=toolchain, assets=assets,
            parameters=ichor.default_parameters(min_counted_reads=10**9),
        )


def test_lab_contract_carries_pon_policy() -> None:
    contract = ichor.default_parameters().contract()
    assert contract["schema"] == "traceback.lab.copy-number-parameters.v1"
    assert contract["pon_policy"] == "none" and contract["pon_sha256"] is None
    assert contract["signed"]["pon_mode"] == "none_development"


# ------------------------------------------------------------ derived inputs
LENGTH = 2_000_000


def _uniform_bam(path: Path, *, reads: int = 40_000, seed: int = 3) -> Path:
    rng = random.Random(seed)
    contigs = [(f"chr{n}", LENGTH) for n in range(1, 23)]
    header = {"HD": {"VN": "1.6", "SO": "coordinate"}, "SQ": [{"SN": name, "LN": size} for name, size in contigs]}
    rows = sorted((rng.randrange(0, 4), rng.randrange(0, LENGTH - 200), i) for i in range(reads))
    names = ["chr1", "chr8", "chr13", "chr17"]
    unsorted = path.with_suffix(".u.bam")
    with pysam.AlignmentFile(str(unsorted), "wb", header=header) as out:
        for which, start, serial in rows:
            record = pysam.AlignedSegment(out.header)
            record.query_name = f"u-{serial}"
            record.reference_id = out.header.get_tid(names[which])
            record.reference_start = start
            record.mapping_quality = 60
            record.cigartuples = [(0, 150)]
            record.query_sequence = "A" * 150
            out.write(record)
    pysam.sort("-o", str(path), str(unsorted))
    pysam.index(str(path))
    unsorted.unlink()
    return path


ARMS = [
    Arm("1q", "chr1", 1_000_000, LENGTH, "gain"),
    Arm("8q", "chr8", 1_000_000, LENGTH, "gain"),
    Arm("13q", "chr13", 1_000_000, LENGTH, "loss"),
    Arm("17p", "chr17", 0, 1_000_000, "loss"),
]


def _counts(bam: Path) -> dict[tuple[str, bool], int]:
    out: dict[tuple[str, bool], int] = {}
    with pysam.AlignmentFile(str(bam)) as reader:
        for record in reader.fetch(until_eof=True):
            key = (record.reference_name, record.reference_start >= 1_000_000)
            out[key] = out.get(key, 0) + 1
    return out


@pytest.fixture(scope="module")
def derived_world(tmp_path_factory) -> dict:
    base = tmp_path_factory.mktemp("derived")
    bam = _uniform_bam(base / "aligned.bam")
    store = LabStore(base / "root")
    store.ensure()
    parent = "c" * 64
    specs = [cna(0.30, ARMS), cna(0.10, ARMS), split("A"), split("B"), downsample(0.5), downsample(0.25)]
    made = materialize(store, parent, bam, specs, counting_contigs=AUTOSOMES, min_mapq=20)
    return {"store": store, "parent": parent, "bam": bam, "made": made, "specs": specs}


def test_cna_spike_gives_one_plus_tf_over_two(derived_world) -> None:
    made = derived_world["made"]
    base = _counts(derived_world["bam"])
    for tf in (0.30, 0.10):
        spiked = _counts(made[f"cna-{tf!r}"].counting_bam)
        gain = spiked[("chr1", True)] / base[("chr1", True)]
        loss = spiked[("chr13", True)] / base[("chr13", True)]
        neutral = spiked[("chr1", False)] / base[("chr1", False)]
        sd = (tf / 2 * (1 - tf / 2) / base[("chr1", True)]) ** 0.5
        assert abs(gain - (1 + tf / 2)) < 4 * sd + 1e-9
        assert abs(loss - (1 - tf / 2)) < 4 * sd + 1e-9
        assert neutral == 1.0
        assert spiked[("chr17", False)] < base[("chr17", False)]  # 17p loss
        manifest = made[f"cna-{tf!r}"].manifest
        assert manifest["duplicated"] > 0 and manifest["removed"] > 0


def test_split_halves_partition_and_downsample_nests(derived_world) -> None:
    made = derived_world["made"]
    a, b = made["split-A"], made["split-B"]
    full = sum(_counts(derived_world["bam"]).values())
    assert a.counted_reads + b.counted_reads == full
    assert made["0.25"].counted_reads < made["0.5"].counted_reads < full
    assert abs(made["0.5"].counted_reads / full - 0.5) < 0.02


def test_derived_ids_are_distinct_and_materialize_is_idempotent(derived_world, monkeypatch) -> None:
    ids = {item.input_id for item in derived_world["made"].values()}
    assert len(ids) == len(derived_world["specs"])
    called = []
    monkeypatch.setattr(derived_module, "_predicate", lambda spec: called.append(spec) or (lambda *a: 1))
    again = materialize(
        derived_world["store"], derived_world["parent"], derived_world["bam"], derived_world["specs"],
        counting_contigs=AUTOSOMES, min_mapq=20,
    )
    assert called == [] and {i.input_id for i in again.values()} == ids
    assert derived_world["store"].orphaned_staging() == []


# ------------------------------------------------------------- simulate CLI
def _lab(capsys, *argv):
    code = signed_cli.main(["lab", *argv, "--json"])
    return code, json.loads(capsys.readouterr().out)


def test_simulate_split_runs_every_summary_algorithm_on_both_halves(lab_world, capsys, shared_mmi) -> None:
    root = lab_world["root"]
    assert _lab(capsys, "run", str(lab_world["chunks"]), "--root", str(root), "--mmi", str(shared_mmi))[0] == 0
    code, payload = _lab(capsys, "simulate", "split", "latest", "--root", str(root))
    assert code == 0, payload
    steps = payload["data"]["steps"]
    for half in ("split-A", "split-B"):
        ran = {s["algorithm_id"]: s["status"] for s in steps if s["input"] == half}
        assert ran["bam-qc"] == ran["depth"] == ran["fragment-size"] == "ok"
        assert "read-summary" not in ran
        assert ran["copy-number"] == "skipped"
    record = json.loads(next((root / "lab" / "sources").glob("*.json")).read_text())
    assert {d["label"] for d in record["derived"]} == {"split-A", "split-B"}
    # A rerun is fully cached.
    code, again = _lab(capsys, "simulate", "split", "latest", "--root", str(root))
    assert all(s["cached"] for s in again["data"]["steps"] if s["status"] == "ok")


def test_simulate_cna_without_assets_says_why(lab_world, capsys, shared_mmi) -> None:
    root = lab_world["root"]
    _lab(capsys, "run", str(lab_world["chunks"]), "--root", str(root), "--mmi", str(shared_mmi))
    code, payload = _lab(capsys, "simulate", "cna", "latest", "--root", str(root))
    assert code == 0
    assert payload["data"]["skipped"] in {"missing_toolchain", "missing_asset"}


@pytest.mark.parametrize("value", ["0", "1", "0.1,abc", ""])
def test_bad_tf_is_tbx_lab_002(lab_world, capsys, value) -> None:
    code, payload = _lab(capsys, "simulate", "cna", "latest", "--tf", value, "--root", str(lab_world["root"]))
    assert code == 2 and payload["data"]["code"] == "TBX-LAB-002"


def test_simulate_takes_the_writer_lock(lab_world, capsys, shared_mmi) -> None:
    root = lab_world["root"]
    _lab(capsys, "run", str(lab_world["chunks"]), "--root", str(root), "--mmi", str(shared_mmi))
    with LabStore(root).writer("run", "r-20261015-0000-abcd"):
        code, payload = _lab(capsys, "simulate", "split", "latest", "--root", str(root))
    assert code == 3 and payload["data"]["code"] == "TBX-LAB-012"


def test_copy_number_algorithm_run_in_process(tmp_path, monkeypatch) -> None:
    from traceback_lab.algorithms import copy_number as cn_module
    from traceback_lab.interpretation.engine import interpret_result, load_library
    from traceback_lab.registry import InputContext
    from traceback_runner.copy_number_method import LOCKED_BIN_SIZE_BP
    from traceback_runner.references import register_ichor_toolchain_directory

    _, bam = _write_inputs(tmp_path / "inputs")
    toolchain = _fake_toolchain(tmp_path / "toolchain")
    monkeypatch.setattr(cn_module, "_toolchain", lambda: toolchain)
    monkeypatch.setattr(cn_module, "toolchain_or_none", lambda: toolchain)
    root = tmp_path / "root"
    register_ichor_toolchain_directory(
        root, _write_assets(tmp_path / "extdata"), bin_size_bp=LOCKED_BIN_SIZE_BP, toolchain_tag=PIN.lock_sha256[:12]
    )
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    (input_dir / "aligned.bam").symlink_to(bam)
    (input_dir / "aligned.bam.bai").symlink_to(Path(str(bam) + ".bai"))
    ctx = InputContext(root=root, source_id="a" * 64, input_id="b" * 64, reference_id="ref",
                       reference_sha256="c" * 64, input_dir=input_dir, eligible_reads=3_000_000)
    assert type(cn_module.COPY_NUMBER.preflight(ctx)).__name__ == "Ready"
    out = tmp_path / "attempt" / "artifacts"
    out.mkdir(parents=True)
    output = cn_module.COPY_NUMBER.run(ctx, out)
    values = {m["metric_id"]: m["value"] for m in output.metrics}
    assert values["pon_policy"] == "none" and values["counted_reads"] == _eligible()
    assert json.loads(output.artifacts["cn_bins.json"])["bins"]
    from traceback_lab.envelope import ResultEnvelope, provenance

    envelope = ResultEnvelope.model_validate({
        "algorithm_id": "copy-number", "algorithm_version": "1", "status": "ok",
        "provenance": provenance(tool="t", parameters_sha256="d" * 64, source_id="a" * 64, input_id="b" * 64,
                                 reference_id="ref", timestamp_utc="2026-10-15T00:00:00Z"),
        "metrics": output.metrics,
    })
    rows = {r.metric_id: r.interpretation for r in interpret_result(envelope, load_library(), depth_x=0.3)}
    assert (rows["tumor_fraction"].state, rows["tumor_fraction"].reason) == ("not_interpretable", "no_pon")
    small = InputContext(**{**ctx.__dict__, "eligible_reads": 10})
    assert cn_module.COPY_NUMBER.preflight(small).reason_code == "depth"
    example = InputContext(**{**ctx.__dict__, "example": True})
    assert cn_module.COPY_NUMBER.preflight(example).reason_code == "example_reference"


def test_duplicate_specs_materialize_once(tmp_path) -> None:
    bam = _uniform_bam(tmp_path / "aligned.bam", reads=2000)
    store = LabStore(tmp_path / "root")
    store.ensure()
    made = materialize(store, "e" * 64, bam, [cna(0.1, ARMS), cna(0.10, ARMS)], counting_contigs=AUTOSOMES, min_mapq=20)
    assert list(made) == ["cna-0.1"]
    with pytest.raises(ValueError, match="other contigs"):
        materialize(store, "e" * 64, bam, [cna(0.1, ARMS)], counting_contigs=AUTOSOMES[:5], min_mapq=20)


def test_parse_fractions_dedupes() -> None:
    from traceback_lab.simulate_command import parse_fractions

    assert parse_fractions("0.1,0.10,0.3", "--tf") == (0.1, 0.3)


def test_copy_number_identity_binds_asset_digests(tmp_path, monkeypatch) -> None:
    from traceback_lab.algorithms import copy_number as cn_module
    from traceback_lab.registry import execution_identity
    from traceback_runner.copy_number_method import LOCKED_BIN_SIZE_BP
    from traceback_runner.references import register_ichor_toolchain_directory

    toolchain = _fake_toolchain(tmp_path / "toolchain")
    monkeypatch.setattr(cn_module, "toolchain_or_none", lambda: toolchain)
    root = tmp_path / "root"
    assert cn_module.COPY_NUMBER.asset_sha256s(root) == []
    register_ichor_toolchain_directory(
        root, _write_assets(tmp_path / "extdata"), bin_size_bp=LOCKED_BIN_SIZE_BP, toolchain_tag=PIN.lock_sha256[:12]
    )
    digests = cn_module.COPY_NUMBER.asset_sha256s(root)
    assert len(digests) == 3
    with_assets = execution_identity(cn_module.COPY_NUMBER, reference_sha256="a" * 64, asset_sha256s=digests)
    without = execution_identity(cn_module.COPY_NUMBER, reference_sha256="a" * 64)
    assert with_assets != without
