"""Griffin registration, input gates and result publication boundaries."""

from __future__ import annotations

import hashlib
import json
import subprocess
from argparse import Namespace
from pathlib import Path

import pytest

from traceback_lab.adapters.griffin import GriffinProfile
from traceback_lab.adapters.griffin_run import GRIFFIN_COMMIT, _REF_FILES
from traceback_lab.adapters.griffin_run import count_d21_pairs
from traceback_lab.algorithms.griffin import GRIFFIN
from traceback_lab.griffin_assets import GriffinAssetError, inspect_griffin_assets, register_griffin_assets
from traceback_lab.registry import InputContext, Skip
from traceback_lab.store import LabStore
from traceback_lab.asset_command import _griffin_inspect, _griffin_register


def _registered(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    import traceback_lab.griffin_assets as asset_module

    upstream = tmp_path / "upstream"
    scripts = upstream / "scripts"
    scripts.mkdir(parents=True)
    for name in ("griffin_GC_counts.py", "griffin_GC_bias.py", "griffin_coverage.py", "griffin_merge_sites.py"):
        (scripts / name).write_bytes(b"script")
    assets = tmp_path / "ref-assets"
    assets.mkdir()
    for name in _REF_FILES:
        (assets / name).write_bytes(b"asset")
    frequency = assets / "genome_GC_frequency"
    frequency.mkdir()
    for length in range(15, 501):
        (frequency / f"k100_minus_exclusion_lists.mappable_regions.hg38.{length}bp.GC_frequency.txt").write_bytes(b"freq")
    sites = tmp_path / "CTCF.txt"
    sites.write_text("Chrom\tposition\tStrand\nchr1\t100\t+\n", encoding="utf-8")

    def fake_git(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        assert argv[:2] == ["git", "-C"]
        return subprocess.CompletedProcess(
            argv, 0, GRIFFIN_COMMIT + "\n" if argv[-2:] == ["rev-parse", "HEAD"] else ""
        )

    monkeypatch.setattr(asset_module.subprocess, "run", fake_git)
    root = tmp_path / "root"
    register_griffin_assets(
        root, upstream_root=upstream, ref_assets=assets,
        site_lists={"CTCF": sites}, primary_site="CTCF",
        source_label="operator supplied TF sites", terms="unknown",
    )
    return root, sites


def test_registered_assets_pin_site_bytes_and_execution_identity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, sites = _registered(tmp_path, monkeypatch)
    record = inspect_griffin_assets(root)
    assert record.primary_site == "CTCF"
    assert GRIFFIN.asset_sha256s(root) == [record.sha256]
    sites.write_text("Chrom\tposition\tStrand\nchr1\t101\t+\n", encoding="utf-8")
    with pytest.raises(GriffinAssetError, match="changed"):
        inspect_griffin_assets(root)
    assert GRIFFIN.asset_sha256s(root) == []


def test_griffin_asset_commands_register_and_inspect(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, sites = _registered(tmp_path, monkeypatch)
    store = LabStore(root)
    arguments = Namespace(
        upstream_root=tmp_path / "upstream", ref_assets=tmp_path / "ref-assets",
        site_list=[f"CTCF={sites}"], primary_site="CTCF",
        source_label="operator supplied TF sites", terms="unknown", license_id=None,
    )
    code, payload = _griffin_register(arguments, store)
    assert code == 0 and payload["data"]["primary_site"] == "CTCF"
    code, payload = _griffin_inspect(Namespace(), store)
    assert code == 0 and payload["data"]["sha256"] == inspect_griffin_assets(root).sha256


def test_griffin_preflight_depth_and_native_ont_skip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import pysam
    import traceback_lab.algorithms.griffin as module

    input_dir = tmp_path / "input"
    input_dir.mkdir()
    bam = input_dir / "aligned.bam"
    with pysam.AlignmentFile(str(bam), "wb", header={"HD": {"VN": "1.6"}, "SQ": [{"SN": "chr1", "LN": 10000}]}) as out:
        for index in range(100):
            read = pysam.AlignedSegment()
            read.query_name = f"ont{index}"
            read.query_sequence = "A" * 150
            read.reference_id = 0
            read.reference_start = index * 10
            read.mapping_quality = 60
            read.cigarstring = "150M"
            out.write(read)
    base = dict(
        root=tmp_path, source_id="1" * 64, input_id="2" * 64,
        reference_id="hg38", reference_sha256="3" * 64,
        input_dir=input_dir, platform="illumina",
        extras={"reference_fasta": str(tmp_path / "hg38.fa")},
    )
    depth = GRIFFIN.preflight(InputContext(**base, eligible_reads=4_999_999))
    assert isinstance(depth, Skip) and depth.reason_code == "depth"
    unsupported = GRIFFIN.preflight(InputContext(**base, eligible_reads=5_000_000))
    assert isinstance(unsupported, Skip) and unsupported.reason_code == "unsupported_platform"
    assert "single-end" in unsupported.message
    platform = GRIFFIN.preflight(InputContext(**{**base, "platform": "ont"}, eligible_reads=5_000_000))
    assert isinstance(platform, Skip) and platform.reason_code == "platform"
    monkeypatch.setattr(module, "_require_paired_input", lambda _: None)
    monkeypatch.setattr(module, "count_d21_pairs", lambda _: 4_999_999)
    short_pairs = GRIFFIN.preflight(InputContext(**base, eligible_reads=10_000_000))
    assert isinstance(short_pairs, Skip) and short_pairs.reason_code == "depth"
    assert "4,999,999" in short_pairs.message


def test_paired_350_bp_fragments_are_depth_skip_not_platform_skip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import pysam
    import traceback_lab.algorithms.griffin as module

    input_dir = tmp_path / "input"
    input_dir.mkdir()
    bam = input_dir / "aligned.bam"
    header = {"HD": {"VN": "1.6"}, "SQ": [{"SN": "chr1", "LN": 10000}],
              "RG": [{"ID": "rg1", "PL": "ILLUMINA"}]}
    with pysam.AlignmentFile(str(bam), "wb", header=header) as out:
        for index in range(100):
            for first in (True, False):
                read = pysam.AlignedSegment()
                read.query_name = f"pair{index}"
                read.query_sequence = "A" * 75
                read.flag = 99 if first else 147
                read.reference_id = 0
                read.reference_start = index * 20 + (0 if first else 275)
                read.mapping_quality = 60
                read.cigarstring = "75M"
                read.next_reference_id = 0
                read.next_reference_start = index * 20 + (275 if first else 0)
                read.template_length = 350 if first else -350
                out.write(read)
    monkeypatch.setattr(module, "count_d21_pairs", lambda _: 5_000_000)
    ctx = InputContext(root=tmp_path, source_id="1" * 64, input_id="2" * 64,
                       reference_id="hg38", reference_sha256="3" * 64,
                       input_dir=input_dir, platform="illumina", eligible_reads=10_000_000,
                       extras={"reference_fasta": str(tmp_path / "hg38.fa")})
    decision = GRIFFIN.preflight(ctx)
    assert decision == Skip("depth", "Griffin's pinned 100–200 bp profile window has no qualifying fragments")


def test_pair_gate_counts_molecules_once_and_requires_illumina_header(tmp_path: Path) -> None:
    import pysam

    def write_bam(path: Path, platform: str) -> None:
        header = {
            "HD": {"VN": "1.6"}, "SQ": [{"SN": "chr1", "LN": 10000}],
            "RG": [{"ID": "rg1", "PL": platform}],
        }
        with pysam.AlignmentFile(str(path), "wb", header=header) as out:
            for index in range(100):
                for first in (True, False):
                    read = pysam.AlignedSegment()
                    read.query_name = f"pair{index}"
                    read.query_sequence = "A" * 75
                    read.flag = (0x1 | 0x2 | (0x40 if first else 0x80) |
                                 (0 if first else 0x10))
                    read.reference_id = 0
                    read.reference_start = index * 10 + (0 if first else 75)
                    read.next_reference_id = 0
                    read.next_reference_start = index * 10 + (75 if first else 0)
                    read.mapping_quality = 60
                    read.cigarstring = "75M"
                    read.template_length = 150 if first else -150
                    out.write(read)

    illumina = tmp_path / "illumina.bam"
    write_bam(illumina, "ILLUMINA")
    assert count_d21_pairs(illumina) == 100
    unknown = tmp_path / "unknown.bam"
    write_bam(unknown, "ONT")
    with pytest.raises(ValueError, match="Illumina"):
        count_d21_pairs(unknown)


def test_algorithm_publishes_primary_profile_and_input_bound_provenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import traceback_lab.algorithms.griffin as module

    root, _ = _registered(tmp_path, monkeypatch)
    registered_sha = inspect_griffin_assets(root).sha256
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    (input_dir / "aligned.bam").write_bytes(b"paired BAM in workflow stub")
    reference = tmp_path / "hg38.fa"
    reference.write_bytes(b">chr1\nACGT\n")
    ctx = InputContext(
        root=root, source_id="1" * 64, input_id="2" * 64,
        reference_id="hg38", reference_sha256=hashlib.sha256(reference.read_bytes()).hexdigest(),
        input_dir=input_dir, platform="illumina", eligible_reads=5_000_000,
        extras={"reference_fasta": str(reference)},
    )
    sample = "lab_" + ctx.input_id

    def fake_workflow(config: object) -> tuple[GriffinProfile, ...]:
        assert config.input_id == ctx.input_id
        assert config.registration_sha256 == registered_sha
        assert config.attempt_dir.parent == output.parent
        corrected = config.attempt_dir / "griffin_nucleosome_profiling" / "results" / sample / f"{sample}.GC_corrected.coverage.tsv"
        corrected.parent.mkdir(parents=True)
        corrected.write_bytes(b"official corrected TSV")
        (config.attempt_dir / "provenance.json").write_text(
            json.dumps({
                "status": "complete", "input_id": ctx.input_id,
                "upstream_commit": GRIFFIN_COMMIT,
                "platform": "illumina", "registration_sha256": registered_sha,
                "eligible_autosomal_pairs": 5_000_000, "snakemake_version": "5.19.2",
                "output_sha256": hashlib.sha256(b"official corrected TSV").hexdigest(),
            }), encoding="utf-8"
        )
        return (GriffinProfile(sample, "CTCF", 1000, (-15, 0, 15), (0.9, 0.5, 0.8), 0.5, 2.1, True, False),)

    monkeypatch.setattr(module, "run_upstream", fake_workflow)
    monkeypatch.setattr(module, "count_d21_pairs", lambda _: 5_000_000)
    output = tmp_path / "scratch" / "artifacts"
    output.mkdir(parents=True)
    result = GRIFFIN.run(ctx, output)
    assert result.metrics[0]["metric_id"] == "central_coverage"
    assert result.metrics[0]["value"] == 0.5
    assert result.metrics[0]["n"] == 1000
    assert result.panels[0]["data_ref"] == "artifacts/griffin_profile.json"
    panel = json.loads(result.artifacts["griffin_profile.json"])
    assert panel["primary_site"] == "CTCF"
    assert panel["profiles"][0]["bin_start_bp"] == [-15, 0, 15]
    assert result.artifacts["griffin_corrected_coverage.tsv"] == b"official corrected TSV"
    assert json.loads(result.artifacts["griffin_provenance.json"])["input_id"] == ctx.input_id
