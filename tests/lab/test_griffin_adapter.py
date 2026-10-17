from __future__ import annotations

from pathlib import Path
import hashlib
import json
import subprocess

import pytest

from traceback_lab.adapters.griffin import GriffinFormatError, read_corrected_profiles
from traceback_lab.adapters.griffin_run import GRIFFIN_COMMIT, GriffinRun, run_upstream


def _write(path: Path, *, correction: str = "GC_corrected", sample: str = "S1") -> Path:
    path.write_text(
        "-15\t0\t15\tcentral_coverage\tamplitude\tcorrection\t"
        "number_of_sites\tsite_name\tsmoothing\tCNA_normalization\tsample\n"
        f"0.8\t0.5\t0.9\t0.5\t2.1\t{correction}\t1000\tCTCF\tTrue\tFalse\t{sample}\n",
        encoding="utf-8",
    )
    return path


def test_reads_genuine_corrected_profile_contract(tmp_path: Path) -> None:
    profile = read_corrected_profiles(
        _write(tmp_path / "S1.GC_corrected.coverage.tsv"), expected_sample="S1"
    )[0]
    assert profile.site_name == "CTCF"
    assert profile.site_count == 1000
    assert profile.offsets_bp == (-15, 0, 15)
    assert profile.coverage == (0.8, 0.5, 0.9)
    assert profile.central_coverage == 0.5
    assert profile.smoothing is True


@pytest.mark.parametrize(
    ("filename", "correction", "sample"),
    [
        ("S1.uncorrected.coverage.tsv", "uncorrected", "S1"),
        ("S1.GC_corrected.coverage.tsv", "uncorrected", "S1"),
        ("S1.GC_corrected.coverage.tsv", "GC_corrected", "S2"),
    ],
)
def test_rejects_false_or_misattributed_measurement(
    tmp_path: Path, filename: str, correction: str, sample: str
) -> None:
    with pytest.raises(GriffinFormatError):
        read_corrected_profiles(
            _write(tmp_path / filename, correction=correction, sample=sample),
            expected_sample="S1",
        )


def test_rejects_non_finite_coverage(tmp_path: Path) -> None:
    path = _write(tmp_path / "S1.GC_corrected.coverage.tsv")
    path.write_text(path.read_text(encoding="utf-8").replace("0.8", "nan"), encoding="utf-8")
    with pytest.raises(GriffinFormatError, match="finite"):
        read_corrected_profiles(path)


def test_rejects_mixed_samples_without_an_expected_sample(tmp_path: Path) -> None:
    path = _write(tmp_path / "S1.GC_corrected.coverage.tsv")
    path.write_text(path.read_text(encoding="utf-8") +
                    "0.8\t0.5\t0.9\t0.5\t2.1\tGC_corrected\t1000\tOTHER\tTrue\tFalse\tS2\n",
                    encoding="utf-8")
    with pytest.raises(GriffinFormatError, match="mixed sample"):
        read_corrected_profiles(path)


def test_rejects_non_ascii_coordinate_header(tmp_path: Path) -> None:
    path = _write(tmp_path / "S1.GC_corrected.coverage.tsv")
    path.write_text(path.read_text(encoding="utf-8").replace("-15\t0\t15", "-15\t٠\t15"),
                    encoding="utf-8")
    with pytest.raises(GriffinFormatError, match="ASCII"):
        read_corrected_profiles(path)


@pytest.mark.parametrize("header", ["+15", "015", "-0"])
def test_rejects_noncanonical_coordinate_header(tmp_path: Path, header: str) -> None:
    path = _write(tmp_path / "S1.GC_corrected.coverage.tsv")
    path.write_text(path.read_text(encoding="utf-8").replace("-15\t0\t15", f"-15\t0\t{header}"),
                    encoding="utf-8")
    with pytest.raises(GriffinFormatError, match="canonical"):
        read_corrected_profiles(path)


def test_pinned_upstream_attempt_binds_input_and_assets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Exercise orchestration with a fake process, not the scientific computation."""

    from traceback_lab.adapters import griffin_run

    upstream = tmp_path / "upstream"
    for workflow in (
        "griffin_GC_and_mappability_correction", "griffin_nucleosome_profiling"
    ):
        config = upstream / "snakemakes" / workflow / "config"
        config.mkdir(parents=True)
        (config / "config.yaml").write_text("{}\n", encoding="utf-8")
        (upstream / "snakemakes" / workflow / (workflow + ".snakefile")).write_text(
            "# test fixture\n", encoding="utf-8"
        )
    (upstream / "scripts").mkdir()
    (upstream / "scripts" / "profile.py").write_text("# pinned test script\n", encoding="utf-8")
    assets = tmp_path / "assets"
    assets.mkdir()
    for name in griffin_run._REF_FILES:
        (assets / name).write_bytes(b"asset")
    frequency = assets / "genome_GC_frequency"
    frequency.mkdir()
    prefix = "k100_minus_exclusion_lists.mappable_regions.hg38."
    for length in range(15, 501):
        (frequency / f"{prefix}{length}bp.GC_frequency.txt").write_bytes(b"frequency")
    bam = tmp_path / "aligned.bam"
    bam.write_bytes(b"bam")
    bam.with_suffix(".bam.bai").write_bytes(b"index")
    reference = tmp_path / "hg38.fa"
    reference.write_bytes(b">chr1\nACGT\n")
    reference.with_suffix(".fa.fai").write_bytes(b"index")
    sites = tmp_path / "CTCF.txt"
    sites.write_bytes(b"Chrom\tposition\nchr1\t100\n")
    input_id = "abcdef012345"
    sample = f"lab_{input_id}"
    calls: list[Path] = []
    mutate_index = False
    mutate_script = False

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if argv[:2] == ["git", "-C"]:
            return subprocess.CompletedProcess(argv, 0, GRIFFIN_COMMIT + "\n" if argv[-2:] == ["rev-parse", "HEAD"] else "")
        directory = Path(kwargs["cwd"])
        calls.append(directory)
        if directory.name.startswith("griffin_GC_and"):
            output = directory / "results" / "GC_bias" / f"{sample}.GC_bias.txt"
            output.parent.mkdir(parents=True)
            output.write_bytes(b"GC bias")
        else:
            output = directory / "results" / sample / f"{sample}.GC_corrected.coverage.tsv"
            output.parent.mkdir(parents=True)
            _write(output, sample=sample)
            if mutate_index:
                bam.with_suffix(".bam.bai").write_bytes(b"changed index")
            if mutate_script:
                (directory.parent / "upstream_scripts" / "profile.py").write_text(
                    "# changed during run\n", encoding="utf-8"
                )
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(griffin_run, "_require_paired_input", lambda _: None)
    attempt = tmp_path / "attempt"
    result = run_upstream(GriffinRun(
        input_id=input_id, bam=bam, reference_fasta=reference,
        reference_id="hg38", reference_sha256=hashlib.sha256(reference.read_bytes()).hexdigest(),
        eligible_reads=5_000_000, upstream_root=upstream, ref_assets=assets,
        site_lists={"CTCF": sites}, attempt_dir=attempt,
    ))
    assert result[0].site_name == "CTCF"
    assert len(calls) == 2
    manifest = json.loads((attempt / "provenance.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "complete"
    assert manifest["input_id"] == input_id
    assert manifest["sample"] == sample
    assert manifest["eligible_autosomal_pairs"] == 5_000_000
    assert manifest["minimum_eligible_autosomal_pairs"] == 5_000_000
    assert manifest["eligibility_source"] == "caller_supplied"
    assert manifest["fingerprints"]["bam_sha256"] == hashlib.sha256(b"bam").hexdigest()
    assert manifest["fingerprints"]["bam_indexes_sha256"]["aligned.bam.bai"] == hashlib.sha256(b"index").hexdigest()
    assert manifest["fingerprints"]["reference_index_sha256"] == hashlib.sha256(b"index").hexdigest()
    assert len(manifest["fingerprints"]["generated_configs_sha256"]) == 5
    assert manifest["fingerprints"]["upstream_scripts_sha256"] != hashlib.sha256().hexdigest()
    assert str(attempt / "upstream_scripts") in (
        attempt / "griffin_nucleosome_profiling" / "config" / "config.yaml"
    ).read_text(encoding="utf-8")
    with pytest.raises(FileExistsError):
        run_upstream(GriffinRun(
            input_id=input_id, bam=bam, reference_fasta=reference,
            reference_id="hg38", reference_sha256=hashlib.sha256(reference.read_bytes()).hexdigest(),
            eligible_reads=5_000_000, upstream_root=upstream, ref_assets=assets,
            site_lists={"CTCF": sites}, attempt_dir=attempt,
        ))
    mutate_index = True
    with pytest.raises(RuntimeError, match="index changed"):
        run_upstream(GriffinRun(
            input_id=input_id, bam=bam, reference_fasta=reference,
            reference_id="hg38", reference_sha256=hashlib.sha256(reference.read_bytes()).hexdigest(),
            eligible_reads=5_000_000, upstream_root=upstream, ref_assets=assets,
            site_lists={"CTCF": sites}, attempt_dir=tmp_path / "changed-index-attempt",
        ))
    bam.with_suffix(".bam.bai").write_bytes(b"index")
    mutate_index = False
    mutate_script = True
    with pytest.raises(RuntimeError, match="scripts or generated configuration changed"):
        run_upstream(GriffinRun(
            input_id=input_id, bam=bam, reference_fasta=reference,
            reference_id="hg38", reference_sha256=hashlib.sha256(reference.read_bytes()).hexdigest(),
            eligible_reads=5_000_000, upstream_root=upstream, ref_assets=assets,
            site_lists={"CTCF": sites}, attempt_dir=tmp_path / "changed-script-attempt",
        ))


def test_griffin_depth_gate_precedes_attempt(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="5,000,000"):
        run_upstream(GriffinRun(
            input_id="abcdef012345", bam=tmp_path / "aligned.bam",
            reference_fasta=tmp_path / "hg38.fa", reference_id="hg38",
            reference_sha256="0" * 64, eligible_reads=4_999_999,
            upstream_root=tmp_path, ref_assets=tmp_path,
            site_lists={"CTCF": tmp_path / "sites.txt"}, attempt_dir=tmp_path / "attempt",
        ))


def test_native_ont_bam_fails_closed(tmp_path: Path) -> None:
    import pysam

    from traceback_lab.adapters.griffin_run import _require_paired_input

    bam = tmp_path / "ont.bam"
    with pysam.AlignmentFile(str(bam), "wb", header={"HD": {"VN": "1.6"}, "SQ": [{"SN": "chr1", "LN": 10000}]}) as out:
        for index in range(100):
            read = pysam.AlignedSegment()
            read.query_name = f"ont{index}"
            read.query_sequence = "A" * 150
            read.flag = 0
            read.reference_id = 0
            read.reference_start = index * 10
            read.mapping_quality = 60
            read.cigarstring = "150M"
            read.template_length = 0
            out.write(read)
    with pytest.raises(ValueError, match="native ONT single-end"):
        _require_paired_input(bam)
