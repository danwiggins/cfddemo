from __future__ import annotations

from pathlib import Path
import hashlib
import json
import os
import subprocess
import sys

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


def test_reads_minimal_corrected_profile_contract(tmp_path: Path) -> None:
    profile = read_corrected_profiles(
        _write(tmp_path / "S1.GC_corrected.coverage.tsv"), expected_sample="S1"
    )[0]
    assert profile.site_name == "CTCF"
    assert profile.site_count == 1000
    assert profile.offsets_bp == (-15, 0, 15)
    assert profile.coverage == (0.8, 0.5, 0.9)
    assert profile.central_coverage == 0.5
    assert profile.smoothing is True


def test_reads_pinned_upstream_healthy_demo_fixture() -> None:
    path = Path(__file__).parent / "fixtures" / "griffin" / "Healthy_demo.GC_corrected.coverage.tsv"
    assert hashlib.sha256(path.read_bytes()).hexdigest() == (
        "4e7b430bc98d67d704e506ee41d2650b76bad3cc0a55be919f7884d0137ecc88"
    )
    profile, = read_corrected_profiles(path, expected_sample="Healthy_demo")
    assert profile.site_name == "CTCF_demo"
    assert profile.site_count == 1000
    assert len(profile.offsets_bp) == 132
    assert profile.offsets_bp[0] == -990 and profile.offsets_bp[-1] == 975


def test_rejects_renamed_upstream_coordinate_column(tmp_path: Path) -> None:
    source = Path(__file__).parent / "fixtures" / "griffin" / "Healthy_demo.GC_corrected.coverage.tsv"
    path = tmp_path / source.name
    raw = source.read_text(encoding="utf-8")
    path.write_text(raw.replace("\t975\tmean_reads_per_bp", "\tlost_975\tmean_reads_per_bp", 1), encoding="utf-8")
    with pytest.raises(GriffinFormatError, match="unknown Griffin output column"):
        read_corrected_profiles(path, expected_sample="Healthy_demo")


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


@pytest.mark.skipif(os.name != "posix", reason="Griffin runner needs POSIX paths")
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
        (config / "cluster_slurm.yaml").write_text("GC_counts:\n  ncpus: 8\n", encoding="utf-8")
        (upstream / "snakemakes" / workflow / (workflow + ".snakefile")).write_text(
            "# test fixture\n", encoding="utf-8"
        )
        stale = upstream / "snakemakes" / workflow / "results" / "old.txt"
        stale.parent.mkdir(parents=True)
        stale.write_text("stale ignored output", encoding="utf-8")
    (upstream / "scripts").mkdir()
    (upstream / "scripts" / "profile.py").write_text("# pinned test script\n", encoding="utf-8")
    bytecode = upstream / "scripts" / "__pycache__" / "profile.cpython-311.pyc"
    bytecode.parent.mkdir()
    bytecode.write_bytes(b"stale bytecode")
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
    tool_dir = tmp_path / "tools"
    tool_dir.mkdir()
    for name in ("snakemake", "samtools", "bedtools"):
        (tool_dir / name).write_bytes(name.encode("ascii"))
    input_id = "abcdef012345"
    sample = f"lab_{input_id}"
    calls: list[Path] = []
    mutate_index = False
    mutate_script = False
    wrong_site = False
    snakemake_version = "5.19.2"

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if argv[:2] == ["git", "-C"]:
            return subprocess.CompletedProcess(argv, 0, GRIFFIN_COMMIT + "\n" if argv[-2:] == ["rev-parse", "HEAD"] else "")
        if argv[-1] == "--version":
            assert argv[:2] == [sys.executable, str(tool_dir / "snakemake")]
            return subprocess.CompletedProcess(argv, 0, snakemake_version + "\n")
        assert Path(argv[0]).name == "python" and Path(argv[0]).parent.name == "bin"
        assert Path(argv[0]).read_text(encoding="ascii") == (
            f'#!/bin/sh\nexec {Path(sys.executable).absolute()} "$@"\n'
        )
        assert argv[1] == str(tool_dir / "snakemake")
        assert kwargs["env"]["PYTHONDONTWRITEBYTECODE"] == "1"
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
            if wrong_site:
                output.write_text(output.read_text(encoding="utf-8").replace("\tCTCF\t", "\tOTHER\t"),
                                  encoding="utf-8")
            if mutate_index:
                bam.with_suffix(".bam.bai").write_bytes(b"changed index")
            if mutate_script:
                (directory.parent / "upstream_scripts" / "profile.py").write_text(
                    "# changed during run\n", encoding="utf-8"
                )
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(griffin_run, "_tool_path", lambda command, **_: tool_dir / command)
    monkeypatch.setattr(griffin_run, "_require_paired_input", lambda _: None)
    monkeypatch.setattr(griffin_run, "_require_reference_header", lambda *_: None)
    monkeypatch.setattr(griffin_run, "_require_chrom_sizes", lambda *_: ("chr1",))
    monkeypatch.setattr(griffin_run, "count_d21_pairs", lambda _: 5_000_000)
    attempt = tmp_path / "attempt"
    result = run_upstream(GriffinRun(
        input_id=input_id, bam=bam, reference_fasta=reference,
        reference_id="hg38", reference_sha256=hashlib.sha256(reference.read_bytes()).hexdigest(),
        eligible_autosomal_pairs=5_000_000, upstream_root=upstream, ref_assets=assets,
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
    assert manifest["eligibility_source"] == "bam_verified"
    assert manifest["snakemake_version"] == "5.19.2"
    assert manifest["runtime"]["packages"]["pysam"]
    assert (attempt / "bin" / "python").is_file()
    assert not (attempt / "bin" / "python").is_symlink()
    assert manifest["runtime"]["python_interpreter"]["path"] == str(Path(sys.executable).absolute())
    assert manifest["runtime"]["compat_shim"]["sha256"] == hashlib.sha256(
        (attempt / "compat" / "sitecustomize.py").read_bytes()
    ).hexdigest()
    assert manifest["runtime"]["environment"]["PYTHONPATH"].startswith(str(attempt / "compat"))
    assert "mappability_correction: false" in (
        attempt / "griffin_nucleosome_profiling" / "config" / "config.yaml"
    ).read_text(encoding="utf-8")
    assert "chroms: chr1" in (
        attempt / "griffin_nucleosome_profiling" / "config" / "config.yaml"
    ).read_text(encoding="utf-8")
    for name in ("snakemake", "samtools", "bedtools"):
        assert manifest["runtime"]["executables"][name]["sha256"] == hashlib.sha256(name.encode()).hexdigest()
    assert manifest["runtime"]["packages"]["numpy"]
    assert manifest["fingerprints"]["bam_sha256"] == hashlib.sha256(b"bam").hexdigest()
    assert manifest["fingerprints"]["bam_indexes_sha256"]["aligned.bam.bai"] == hashlib.sha256(b"index").hexdigest()
    assert manifest["fingerprints"]["reference_index_sha256"] == hashlib.sha256(b"index").hexdigest()
    assert len(manifest["fingerprints"]["generated_configs_sha256"]) == 7
    assert len(manifest["fingerprints"]["snakefiles_sha256"]) == 2
    assert len(manifest["fingerprints"]["upstream_configs_sha256"]) == 2
    assert "ncpus: 1" in (
        attempt / "griffin_GC_and_mappability_correction" / "config" / "cluster_slurm.yaml"
    ).read_text(encoding="utf-8")
    assert manifest["fingerprints"]["upstream_scripts_sha256"] != hashlib.sha256().hexdigest()
    assert not (attempt / "upstream_scripts" / "__pycache__").exists()
    assert not (attempt / "griffin_nucleosome_profiling" / "results" / "old.txt").exists()
    assert str((attempt / "upstream_scripts").resolve()) in (
        attempt / "griffin_nucleosome_profiling" / "config" / "config.yaml"
    ).read_text(encoding="utf-8")
    with pytest.raises(FileExistsError):
        run_upstream(GriffinRun(
            input_id=input_id, bam=bam, reference_fasta=reference,
            reference_id="hg38", reference_sha256=hashlib.sha256(reference.read_bytes()).hexdigest(),
            eligible_autosomal_pairs=5_000_000, upstream_root=upstream, ref_assets=assets,
            site_lists={"CTCF": sites}, attempt_dir=attempt,
        ))
    mutate_index = True
    with pytest.raises(RuntimeError, match="index changed"):
        run_upstream(GriffinRun(
            input_id=input_id, bam=bam, reference_fasta=reference,
            reference_id="hg38", reference_sha256=hashlib.sha256(reference.read_bytes()).hexdigest(),
            eligible_autosomal_pairs=5_000_000, upstream_root=upstream, ref_assets=assets,
            site_lists={"CTCF": sites}, attempt_dir=tmp_path / "changed-index-attempt",
        ))
    bam.with_suffix(".bam.bai").write_bytes(b"index")
    mutate_index = False
    mutate_script = True
    with pytest.raises(RuntimeError, match="workflow source or generated configuration changed"):
        run_upstream(GriffinRun(
            input_id=input_id, bam=bam, reference_fasta=reference,
            reference_id="hg38", reference_sha256=hashlib.sha256(reference.read_bytes()).hexdigest(),
            eligible_autosomal_pairs=5_000_000, upstream_root=upstream, ref_assets=assets,
            site_lists={"CTCF": sites}, attempt_dir=tmp_path / "changed-script-attempt",
        ))
    mutate_script = False
    wrong_site = True
    with pytest.raises(GriffinFormatError, match="site lists do not match"):
        run_upstream(GriffinRun(
            input_id=input_id, bam=bam, reference_fasta=reference,
            reference_id="hg38", reference_sha256=hashlib.sha256(reference.read_bytes()).hexdigest(),
            eligible_autosomal_pairs=5_000_000, upstream_root=upstream, ref_assets=assets,
            site_lists={"CTCF": sites}, attempt_dir=tmp_path / "wrong-site-attempt",
        ))
    monkeypatch.setattr(griffin_run, "count_d21_pairs", lambda _: 4_999_999)
    with pytest.raises(ValueError, match="differs from the BAM"):
        run_upstream(GriffinRun(
            input_id=input_id, bam=bam, reference_fasta=reference,
            reference_id="hg38", reference_sha256=hashlib.sha256(reference.read_bytes()).hexdigest(),
            eligible_autosomal_pairs=5_000_000, upstream_root=upstream, ref_assets=assets,
            site_lists={"CTCF": sites}, attempt_dir=tmp_path / "wrong-pair-count-attempt",
        ))
    assert not (tmp_path / "wrong-pair-count-attempt").exists()
    def mutate_bam_during_count(_: Path) -> int:
        bam.write_bytes(b"changed while counting")
        return 5_000_000

    monkeypatch.setattr(griffin_run, "count_d21_pairs", mutate_bam_during_count)
    with pytest.raises(RuntimeError, match="changed while Griffin input eligibility was measured"):
        run_upstream(GriffinRun(
            input_id=input_id, bam=bam, reference_fasta=reference,
            reference_id="hg38", reference_sha256=hashlib.sha256(reference.read_bytes()).hexdigest(),
            eligible_autosomal_pairs=5_000_000, upstream_root=upstream, ref_assets=assets,
            site_lists={"CTCF": sites}, attempt_dir=tmp_path / "changed-during-count-attempt",
        ))
    assert not (tmp_path / "changed-during-count-attempt").exists()
    bam.write_bytes(b"bam")
    monkeypatch.setattr(griffin_run, "count_d21_pairs", lambda _: 5_000_000)
    snakemake_version = "6.0"
    with pytest.raises(ValueError, match="pinned Snakemake 5.19.2"):
        run_upstream(GriffinRun(
            input_id=input_id, bam=bam, reference_fasta=reference,
            reference_id="hg38", reference_sha256=hashlib.sha256(reference.read_bytes()).hexdigest(),
            eligible_autosomal_pairs=5_000_000, upstream_root=upstream, ref_assets=assets,
            site_lists={"CTCF": sites}, attempt_dir=tmp_path / "wrong-snakemake-attempt",
        ))
    assert not (tmp_path / "wrong-snakemake-attempt").exists()


def test_griffin_depth_gate_precedes_attempt(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="5,000,000"):
        run_upstream(GriffinRun(
            input_id="abcdef012345", bam=tmp_path / "aligned.bam",
            reference_fasta=tmp_path / "hg38.fa", reference_id="hg38",
            reference_sha256="0" * 64, eligible_autosomal_pairs=4_999_999,
            upstream_root=tmp_path, ref_assets=tmp_path,
            site_lists={"CTCF": tmp_path / "sites.txt"}, attempt_dir=tmp_path / "attempt",
        ))


@pytest.mark.skipif(os.name != "posix", reason="Griffin runner needs POSIX paths")
@pytest.mark.parametrize(("head", "status", "message"), [
    ("0" * 40, "", "not the pinned"),
    (GRIFFIN_COMMIT, " M scripts/profile.py", "uncommitted changes"),
])
def test_pinned_checkout_gate_precedes_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, head: str, status: str, message: str,
) -> None:
    upstream = tmp_path / "upstream"
    upstream.mkdir()

    def fake_git(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 0, head if argv[-2:] == ["rev-parse", "HEAD"] else status)

    monkeypatch.setattr(subprocess, "run", fake_git)
    attempt = tmp_path / "attempt"
    with pytest.raises(ValueError, match=message):
        run_upstream(GriffinRun(
            input_id="abcdef012345", bam=tmp_path / "aligned.bam",
            reference_fasta=tmp_path / "hg38.fa", reference_id="hg38",
            reference_sha256="0" * 64, eligible_autosomal_pairs=5_000_000,
            upstream_root=upstream, ref_assets=tmp_path,
            site_lists={"CTCF": tmp_path / "sites.txt"}, attempt_dir=attempt,
        ))
    assert not attempt.exists()


@pytest.mark.parametrize("read_groups", [[], [{"ID": "rg1", "PL": "ILLUMINA"}]])
def test_native_ont_bam_fails_closed(tmp_path: Path, read_groups: list[dict[str, str]]) -> None:
    pysam = pytest.importorskip("pysam")

    from traceback_lab.adapters.griffin_run import _require_paired_input

    bam = tmp_path / "ont.bam"
    with pysam.AlignmentFile(str(bam), "wb", header={"HD": {"VN": "1.6"},
                                                      "SQ": [{"SN": "chr1", "LN": 10000}],
                                                      "RG": read_groups}) as out:
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


@pytest.mark.parametrize("platforms", [("ONT",), ("ILLUMINA", "ONT"), ()])
def test_paired_bam_without_unanimous_illumina_declaration_is_refused(
    tmp_path: Path, platforms: tuple[str, ...],
) -> None:
    pysam = pytest.importorskip("pysam")

    from traceback_lab.adapters.griffin_run import _require_paired_input

    bam = tmp_path / "paired.bam"
    header = {"HD": {"VN": "1.6"}, "SQ": [{"SN": "chr1", "LN": 10000}],
              "RG": [{"ID": f"group-{index}", "PL": platform}
                     for index, platform in enumerate(platforms)]}
    with pysam.AlignmentFile(str(bam), "wb", header=header) as out:
        for index in range(100):
            read = pysam.AlignedSegment()
            read.query_name = f"pair{index}"
            read.query_sequence = "A" * 100
            read.flag = 0x1 | 0x2 | 0x40
            read.reference_id = 0
            read.reference_start = index * 10
            read.next_reference_id = 0
            read.next_reference_start = index * 10 + 50
            read.mapping_quality = 60
            read.cigarstring = "100M"
            read.template_length = 150
            out.write(read)
    with pytest.raises(ValueError, match="PL:ILLUMINA"):
        _require_paired_input(bam)


def test_late_positive_tlen_is_considered_after_first_ten_thousand(tmp_path: Path) -> None:
    pysam = pytest.importorskip("pysam")

    from traceback_lab.adapters.griffin_run import _require_paired_input

    bam = tmp_path / "late-pairs.bam"
    header = {"HD": {"VN": "1.6"}, "SQ": [{"SN": "chr1", "LN": 20000}],
              "RG": [{"ID": "library-1", "PL": "ILLUMINA"}]}
    with pysam.AlignmentFile(str(bam), "wb", header=header) as out:
        for index in range(10_001):
            read = pysam.AlignedSegment()
            read.query_name = f"pair{index}"
            read.query_sequence = "A" * 100
            read.flag = 0x1 | 0x2 | 0x40
            read.reference_id = 0
            read.reference_start = index
            read.next_reference_id = 0
            read.next_reference_start = index + (50 if index == 10_000 else 0)
            read.mapping_quality = 60
            read.cigarstring = "100M"
            read.template_length = 150 if index == 10_000 else 0
            read.set_tag("RG", "library-1")
            out.write(read)
    _require_paired_input(bam)


def test_paired_input_gate_scans_past_first_10000_records(tmp_path: Path) -> None:
    pysam = pytest.importorskip("pysam")

    from traceback_lab.adapters.griffin_run import _require_paired_input

    bam = tmp_path / "illumina.bam"
    header = {"HD": {"VN": "1.6"}, "SQ": [{"SN": "chr1", "LN": 100000}],
              "RG": [{"ID": "rg1", "PL": "ILLUMINA"}]}
    with pysam.AlignmentFile(str(bam), "wb", header=header) as out:
        for index in range(10_001):
            read = pysam.AlignedSegment()
            read.query_name = f"pair{index}"
            read.query_sequence = "A" * 150
            read.flag = 99
            read.reference_id = 0
            read.reference_start = index
            read.mapping_quality = 60 if index == 10_000 else 0
            read.cigarstring = "150M"
            read.template_length = 150
            out.write(read)
    _require_paired_input(bam)


def test_paired_input_gate_requires_a_profile_window_fragment(tmp_path: Path) -> None:
    pysam = pytest.importorskip("pysam")

    from traceback_lab.adapters.griffin_run import _require_paired_input

    bam = tmp_path / "out-of-window.bam"
    header = {"HD": {"VN": "1.6"}, "SQ": [{"SN": "chr1", "LN": 100000}],
              "RG": [{"ID": "rg1", "PL": "ILLUMINA"}]}
    with pysam.AlignmentFile(str(bam), "wb", header=header) as out:
        for index in range(100):
            read = pysam.AlignedSegment()
            read.query_name = f"pair{index}"
            read.query_sequence = "A" * 150
            read.flag = 99
            read.reference_id = 0
            read.reference_start = index * 10
            read.mapping_quality = 60
            read.cigarstring = "150M"
            read.template_length = 500
            out.write(read)
    with pytest.raises(ValueError, match="positive-TLEN"):
        _require_paired_input(bam)


def test_griffin_bam_header_must_match_reference(tmp_path: Path) -> None:
    pysam = pytest.importorskip("pysam")

    from traceback_lab.adapters.griffin_run import _require_chrom_sizes, _require_reference_header

    fasta = tmp_path / "hg38.fa"
    fasta.write_text(">chr1\n" + "A" * 100 + "\n", encoding="ascii")
    pysam.faidx(str(fasta))
    chrom_sizes = tmp_path / "hg38.standard.chrom.sizes"
    chrom_sizes.write_text("chr1\t100\n", encoding="ascii")
    _require_chrom_sizes(fasta, chrom_sizes)
    two_autosomes = tmp_path / "two-autosomes.fa"
    two_autosomes.write_text(">chr1\n" + "A" * 100 + "\n>chr2\n" + "C" * 100 + "\n", encoding="ascii")
    pysam.faidx(str(two_autosomes))
    with pytest.raises(ValueError, match="omit autosomes"):
        _require_chrom_sizes(two_autosomes, chrom_sizes)
    chrom_sizes.write_text("chr1\t101\n", encoding="ascii")
    with pytest.raises(ValueError, match="differ from the registered FASTA"):
        _require_chrom_sizes(fasta, chrom_sizes)
    for length, accepted in ((100, True), (101, False)):
        bam = tmp_path / f"length-{length}.bam"
        with pysam.AlignmentFile(str(bam), "wb", header={
            "HD": {"VN": "1.6"}, "SQ": [{"SN": "chr1", "LN": length}],
        }):
            pass
        if accepted:
            _require_reference_header(bam, fasta)
        else:
            with pytest.raises(ValueError, match="@SQ does not match"):
                _require_reference_header(bam, fasta)


def test_griffin_d21_gate_counts_pairs_once_and_excludes_ineligible_reads(tmp_path: Path) -> None:
    pysam = pytest.importorskip("pysam")

    from traceback_lab.adapters.griffin_run import count_d21_pairs

    bam = tmp_path / "pairs.bam"
    header = {"HD": {"VN": "1.6"},
              "SQ": [{"SN": "chr1", "LN": 10000}, {"SN": "chrX", "LN": 10000}],
              "RG": [{"ID": "rg1", "PL": "ILLUMINA"}]}
    with pysam.AlignmentFile(str(bam), "wb", header=header) as out:
        for index, (contig, mapq, duplicate) in enumerate(((0, 60, False), (0, 60, True), (1, 60, False), (0, 0, False))):
            for first in (True, False):
                read = pysam.AlignedSegment()
                read.query_name = f"pair{index}"
                read.query_sequence = "A" * 150
                read.flag = (99 if first else 147) | (1024 if duplicate else 0)
                read.reference_id = contig
                read.reference_start = index * 200 + (0 if first else 150)
                read.next_reference_id = contig
                read.next_reference_start = index * 200 + (150 if first else 0)
                read.mapping_quality = mapq
                read.cigarstring = "150M"
                read.template_length = 300 if first else -300
                out.write(read)
    assert count_d21_pairs(bam) == 1
