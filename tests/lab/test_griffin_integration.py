"""Griffin registration, input gates and result publication boundaries."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from argparse import Namespace
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from traceback_lab.adapters.griffin import GriffinProfile
from traceback_lab.adapters.griffin_run import GRIFFIN_COMMIT, _REF_FILES
from traceback_lab.adapters.griffin_run import count_d21_pairs
from traceback_lab.algorithms.griffin import GRIFFIN
from traceback_lab.griffin_assets import GriffinAssetError, inspect_griffin_assets, register_griffin_assets
from traceback_lab.problems import EXIT_BLOCKED
from traceback_lab.registry import InputContext, PlanItem, Ready, Skip, execution_identity
from traceback_lab.runner import RunTarget, run_plan
from traceback_lab.scrub import Scrubber
from traceback_lab.store import LabStore, new_run_id
from traceback_lab.asset_command import _griffin_inspect, _griffin_register


def _available_toolchain(monkeypatch: pytest.MonkeyPatch) -> None:
    import traceback_lab.algorithms.griffin as module

    monkeypatch.setattr(module.shutil, "which", lambda name: f"/tools/{name}")
    monkeypatch.setattr(module.importlib.util, "find_spec", lambda name: object())
    monkeypatch.setattr(module.subprocess, "run", lambda *args, **kwargs:
                        subprocess.CompletedProcess(args[0], 0, "5.19.2\n"))
    monkeypatch.setattr(module, "registration_available", lambda root: True)


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

    def fake_git(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert Path(argv[0]).name == "git" and argv[1] == "-C"
        assert "core.fsmonitor=false" in argv and f"core.hooksPath={os.devnull}" in argv
        assert kwargs["env"]["GIT_CONFIG_GLOBAL"] == os.devnull
        return subprocess.CompletedProcess(
            argv, 0, (GRIFFIN_COMMIT + "\n" if argv[-2:] == ["rev-parse", "HEAD"]
                      else str(upstream.resolve()) + "\n" if argv[-2:] == ["rev-parse", "--show-toplevel"]
                      else "")
        )

    monkeypatch.setattr(asset_module.subprocess, "run", fake_git)
    root = tmp_path / "root"
    register_griffin_assets(
        root, upstream_root=upstream, ref_assets=assets,
        site_lists={"CTCF": sites}, primary_site="CTCF",
        source_label="operator supplied TF sites", terms="unknown",
    )
    return root, sites


def test_registration_rejects_ignored_checkout_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import traceback_lab.griffin_assets as asset_module

    upstream = tmp_path / "upstream"
    upstream.mkdir()
    commands: list[list[str]] = []

    def fake_git(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert Path(argv[0]).name == "git" and "core.fsmonitor=false" in argv
        assert kwargs["env"]["GIT_CONFIG_GLOBAL"] == os.devnull
        commands.append(argv)
        if argv[-2:] == ["rev-parse", "--show-toplevel"]:
            output = str(upstream.resolve()) + "\n"
        elif argv[-2:] == ["rev-parse", "HEAD"]:
            output = GRIFFIN_COMMIT + "\n"
        else:
            output = "!! .snakemake/\n"
        return subprocess.CompletedProcess(argv, 0, output)

    monkeypatch.setattr(asset_module.subprocess, "run", fake_git)
    with pytest.raises(GriffinAssetError, match="must be clean"):
        asset_module._checkout(upstream)
    assert commands[-1][-4:] == ["status", "--porcelain", "--ignored", "--untracked-files=all"]


def test_registered_assets_pin_site_bytes_and_execution_identity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, sites = _registered(tmp_path, monkeypatch)
    record = inspect_griffin_assets(root)
    assert record.primary_site == "CTCF"
    assert GRIFFIN.asset_sha256s(root) == [record.sha256]
    sites.write_text("Chrom\tposition\tStrand\nchr1\t101\t+\n", encoding="utf-8")
    with pytest.raises(GriffinAssetError, match="changed"):
        inspect_griffin_assets(root)
    assert GRIFFIN.asset_sha256s(root) == []


def test_missing_registered_site_error_omits_operator_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, sites = _registered(tmp_path, monkeypatch)
    sites.unlink()
    with pytest.raises(GriffinAssetError) as caught:
        inspect_griffin_assets(root)
    assert "absent or a symlink" in str(caught.value)
    assert str(tmp_path) not in str(caught.value)


def test_oversized_registration_preserves_existing_verified_asset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import traceback_lab.griffin_assets as asset_module

    root, sites = _registered(tmp_path, monkeypatch)
    location = root / "lab" / "assets" / "griffin" / "registration.json"
    original = location.read_bytes()
    with monkeypatch.context() as patch:
        patch.setattr(asset_module, "_fingerprints", lambda *args: {"oversized": "x" * 130_000})
        with pytest.raises(GriffinAssetError, match="exceeds the supported size"):
            register_griffin_assets(
                root, upstream_root=tmp_path / "upstream", ref_assets=tmp_path / "ref-assets",
                site_lists={"CTCF": sites}, primary_site="CTCF",
                source_label="operator supplied TF sites", terms="unknown",
            )
    assert location.read_bytes() == original
    assert inspect_griffin_assets(root).primary_site == "CTCF"


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


def test_griffin_registration_write_failure_is_blocked_without_replacing_existing_asset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import traceback_lab.griffin_assets as asset_module

    root, sites = _registered(tmp_path, monkeypatch)
    registration = root / "lab" / "assets" / "griffin" / "registration.json"
    original = registration.read_bytes()
    store = LabStore(root)
    arguments = Namespace(
        upstream_root=tmp_path / "upstream", ref_assets=tmp_path / "ref-assets",
        site_list=[f"CTCF={sites}"], primary_site="CTCF",
        source_label="operator supplied TF sites", terms="unknown", license_id=None,
    )

    def deny_tempfile(*args: object, **kwargs: object) -> None:
        raise OSError("registration directory is full")

    monkeypatch.setattr(asset_module.tempfile, "mkstemp", deny_tempfile)
    code, payload = _griffin_register(arguments, store)
    assert code == EXIT_BLOCKED and payload["status"] == "blocked"
    assert "registration directory is full" in payload["data"]["cause"]
    assert registration.read_bytes() == original
    assert inspect_griffin_assets(root).primary_site == "CTCF"


def test_griffin_preflight_depth_and_native_ont_skip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import pysam
    import traceback_lab.algorithms.griffin as module
    from traceback_lab import views

    assert GRIFFIN.requires.min_eligible_reads == 0
    sample = views.SampleView("1" * 64, "1" * 12, False, views.InputView("full", "2" * 64, None, {}, {}))
    row = next(row for row in views.depth_curve(sample)["rows"] if row["algorithm_id"] == "griffin")
    assert row["input_gate"] == GRIFFIN.input_gate
    from traceback_lab.cli import _algorithms
    _, catalog = _algorithms(Namespace(), LabStore(tmp_path))
    griffin_catalog = next(row for row in catalog["data"]["algorithms"] if row["id"] == "griffin")
    assert griffin_catalog["min_eligible_reads"] == 0
    assert griffin_catalog["input_gate"] == GRIFFIN.input_gate

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
    assert "5,000,000 D21-eligible autosomal pairs" in depth.message
    assert "4,999,999 eligible reads" in depth.message
    _available_toolchain(monkeypatch)
    monkeypatch.setattr(module, "inspect_griffin_assets", lambda _: object())
    assert isinstance(GRIFFIN.preflight(InputContext(**base, eligible_reads=5_000_000)), Ready)
    _, unsupported = module._bam_gate(bam, derived=False)
    assert isinstance(unsupported, Skip) and unsupported.reason_code == "platform"
    assert "single-end" in unsupported.message
    platform = GRIFFIN.preflight(InputContext(**{**base, "platform": "ont"}, eligible_reads=5_000_000))
    assert isinstance(platform, Skip) and platform.reason_code == "platform"
    monkeypatch.setattr(module, "_require_paired_input", lambda _: None)
    monkeypatch.setattr(module, "count_d21_pairs", lambda _: 4_999_999)
    _, short_pairs = module._bam_gate(bam, derived=False)
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
    _, decision = module._bam_gate(bam, derived=False)
    assert decision == Skip("depth", "Griffin's pinned 100–200 bp profile window has no qualifying fragments")


def test_missing_toolchain_skips_before_scanning_bam(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import traceback_lab.algorithms.griffin as module

    monkeypatch.setattr(module.shutil, "which", lambda name: None)
    monkeypatch.setattr(module, "_require_paired_input", lambda bam:
                        pytest.fail("BAM must not be scanned before toolchain check"))
    ctx = InputContext(root=tmp_path, source_id="1" * 64, input_id="2" * 64,
                       reference_id="hg38", reference_sha256="3" * 64,
                       input_dir=tmp_path, platform="illumina", eligible_reads=5_000_000,
                       extras={"reference_fasta": str(tmp_path / "hg38.fa")})
    decision = GRIFFIN.preflight(ctx)
    assert isinstance(decision, Skip) and decision.reason_code == "missing_toolchain"


def test_preflight_probes_snakemake_with_execution_interpreter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import traceback_lab.algorithms.griffin as module

    _available_toolchain(monkeypatch)
    calls: list[tuple[list[str], dict[str, str]]] = []

    def probe(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append((argv, kwargs["env"]))
        return subprocess.CompletedProcess(argv, 0, "5.19.2\n")

    monkeypatch.setattr(module.subprocess, "run", probe)
    monkeypatch.setattr(module, "registration_available", lambda root: False)
    ctx = InputContext(root=tmp_path, source_id="1" * 64, input_id="2" * 64,
                       reference_id="hg38", reference_sha256="3" * 64,
                       input_dir=tmp_path, platform="illumina", eligible_reads=5_000_000,
                       extras={"reference_fasta": str(tmp_path / "hg38.fa")})
    assert GRIFFIN.preflight(ctx).reason_code == "missing_asset"
    assert calls[0][0] == [module.sys.executable, "/tools/snakemake", "--version"]
    assert calls[0][1]["PYTHONPATH"].split(module.os.pathsep)[0] == str(
        Path(module.__file__).parents[1] / "adapters" / "griffin_compat"
    )


def test_missing_registration_skips_before_scanning_bam(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import traceback_lab.algorithms.griffin as module

    _available_toolchain(monkeypatch)
    monkeypatch.setattr(module, "registration_available", lambda root: False)
    monkeypatch.setattr(module, "_require_paired_input", lambda bam:
                        pytest.fail("BAM must not be scanned before registration check"))
    ctx = InputContext(root=tmp_path, source_id="1" * 64, input_id="2" * 64,
                       reference_id="hg38", reference_sha256="3" * 64,
                       input_dir=tmp_path, platform="illumina", eligible_reads=5_000_000,
                       extras={"reference_fasta": str(tmp_path / "hg38.fa")})
    decision = GRIFFIN.preflight(ctx)
    assert isinstance(decision, Skip) and decision.reason_code == "missing_asset"


def test_ready_griffin_preflight_leaves_full_bam_scan_to_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import traceback_lab.algorithms.griffin as module

    _available_toolchain(monkeypatch)
    monkeypatch.setattr(module, "inspect_griffin_assets", lambda _: object())
    monkeypatch.setattr(module, "_require_paired_input", lambda _: pytest.fail("parent scanned BAM"))
    monkeypatch.setattr(module, "count_d21_pairs", lambda _: pytest.fail("parent counted BAM pairs"))
    ctx = InputContext(root=tmp_path, source_id="1" * 64, input_id="2" * 64,
                       reference_id="hg38", reference_sha256="3" * 64,
                       input_dir=tmp_path, platform="illumina", eligible_reads=5_000_000,
                       extras={"reference_fasta": str(tmp_path / "hg38.fa")})
    assert isinstance(GRIFFIN.preflight(ctx), Ready)


@pytest.mark.skipif(os.name == "nt", reason="worker timeouts use POSIX process groups")
def test_slow_griffin_bam_gate_obeys_worker_process_timeout(tmp_path: Path) -> None:
    from traceback_lab.pipeline import Stage, run_pipeline

    marker = tmp_path / "scan-started"
    script = (
        "import time\n"
        "from pathlib import Path\n"
        "import traceback_lab.algorithms.griffin as griffin\n"
        "def slow_scan(_):\n"
        f"    Path({str(marker)!r}).write_text('started')\n"
        "    time.sleep(30)\n"
        "griffin._require_paired_input = slow_scan\n"
        "griffin._bam_gate(Path('unused.bam'), derived=False)\n"
    )
    outcome = run_pipeline([Stage("griffin-gate", [sys.executable, "-c", script])],
                           timeout_s=2, cwd=tmp_path)
    assert marker.is_file()
    assert outcome.timed_out and not outcome.ok


@pytest.mark.parametrize("error", [OSError, ValueError])
def test_bam_scan_failure_omits_private_path_from_skip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: type[Exception],
) -> None:
    import traceback_lab.algorithms.griffin as module

    _available_toolchain(monkeypatch)
    private_path = tmp_path / "private-donor" / "aligned.bam"

    def fail_scan(_: Path) -> None:
        raise error(f"could not read {private_path}")

    monkeypatch.setattr(module, "_require_paired_input", fail_scan)
    _, decision = module._bam_gate(private_path, derived=False)
    assert isinstance(decision, Skip)
    assert decision.reason_code == ("input_unavailable" if error is OSError else "platform")
    assert str(tmp_path) not in decision.message
    assert "aligned BAM" in decision.message


def test_unreadable_bam_skip_is_published_without_private_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import traceback_lab.algorithms.griffin as module
    import traceback_lab.runner as runner
    from traceback_lab.pipeline import PipelineResult, StageOutcome

    private_path = tmp_path / "private-donor" / "aligned.bam"

    def fail_scan(_: Path) -> None:
        raise OSError(f"cannot open {private_path}")

    monkeypatch.setattr(module, "_require_paired_input", fail_scan)
    store = LabStore(tmp_path)
    store.ensure()
    run_id = new_run_id()
    target = RunTarget(
        input_id="2" * 64, source_id="1" * 64, input_dir=private_path.parent,
        reference_id="hg38", reference_sha256="3" * 64, platform="illumina",
        eligible_reads=5_000_000, extras={"reference_fasta": str(tmp_path / "hg38.fa")},
    )
    # The real read-summary upstream supplies the depth gate; this regression
    # isolates publication after that gate has populated the RunTarget.
    algorithm = replace(GRIFFIN, upstream=())
    monkeypatch.setattr(algorithm, "preflight", lambda _: Ready())
    monkeypatch.setattr(algorithm, "identity_tools", lambda **_: algorithm.tools)
    monkeypatch.setattr(algorithm, "asset_sha256s", lambda _: [])

    def worker_skip(stages: object, **kwargs: object) -> PipelineResult:
        _, decision = module._bam_gate(private_path, derived=False)
        assert isinstance(decision, Skip)
        scratch = Path(kwargs["cwd"])
        (scratch / "output.json").write_text(json.dumps({"skip": {
            "reason_code": decision.reason_code, "message": decision.message,
        }}))
        return PipelineResult([StageOutcome("worker", returncode=0)], 0.01, 0)

    monkeypatch.setattr(runner, "run_pipeline", worker_skip)
    result = run_plan(
        store, None, store.run_log(run_id), target, [PlanItem(algorithm, False)],
        {"griffin": "traceback_lab.algorithms.griffin:GRIFFIN"},
        run_id=run_id, scrubber=Scrubber(root=tmp_path),
    )[0]
    assert result.status == "skipped" and result.skip_reason == "input_unavailable"
    envelope = store.read_result(target.input_id, algorithm.id, result.parameters_sha256)
    assert envelope is not None and envelope.status == "skipped"
    assert envelope.skip_reason == "input_unavailable"
    assert "aligned BAM" in envelope.skip_message
    assert str(tmp_path) not in envelope.to_bytes().decode()


def test_missing_canonical_hg38_autosomes_is_reference_skip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import traceback_lab.algorithms.griffin as module

    _available_toolchain(monkeypatch)
    monkeypatch.setattr(module, "_require_paired_input", lambda _: 10)

    def no_autosomes(_: Path) -> int:
        raise ValueError("Griffin requires canonical hg38 autosomes")

    monkeypatch.setattr(module, "count_d21_pairs", no_autosomes)
    _, decision = module._bam_gate(tmp_path / "aligned.bam", derived=False)
    assert decision == Skip("reference", "Griffin requires canonical hg38 autosomes")


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
                "site_list_input_rows": {"CTCF": 10_000},
                "output_sha256": hashlib.sha256(b"official corrected TSV").hexdigest(),
                "runtime": {"environment": {"PATH": "/private/operator/bin"}},
                "fingerprints": {"bam_indexes_sha256": {"patient.bam.bai": "a" * 64}},
            }), encoding="utf-8"
        )
        return (GriffinProfile(sample, "CTCF", 1000, (-15, 0, 15), (0.9, 0.5, 0.8), 0.5, 2.1, True, False),)

    monkeypatch.setattr(module, "run_upstream", fake_workflow)
    monkeypatch.setattr(module, "count_d21_pairs", lambda _: 5_000_000)
    monkeypatch.setattr(module, "_require_paired_input", lambda _: 1)
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
    public_provenance = json.loads(result.artifacts["griffin_provenance.json"])
    assert public_provenance["input_id"] == ctx.input_id
    assert public_provenance["site_list_input_rows"] == {"CTCF": 10_000}
    assert panel["profiles"][0]["site_count"] == 1000
    assert public_provenance["output_sha256"] == hashlib.sha256(b"official corrected TSV").hexdigest()
    assert len(public_provenance["full_provenance_sha256"]) == 64
    assert b"/private/operator" not in result.artifacts["griffin_provenance.json"]
    assert b"patient.bam.bai" not in result.artifacts["griffin_provenance.json"]


def test_derived_split_counts_selected_pairs_and_uses_transient_bam(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import pysam
    import traceback_lab.algorithms.griffin as module
    from traceback_lab.algorithms.cell_origin import derived_name_predicate
    from traceback_lab.derived import split

    root, _ = _registered(tmp_path, monkeypatch)
    registered = inspect_griffin_assets(root)
    _available_toolchain(monkeypatch)
    monkeypatch.setattr(module, "inspect_griffin_assets", lambda _: registered)
    monkeypatch.setattr(module, "MIN_ELIGIBLE_AUTOSOMAL_PAIRS", 100)
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    source = input_dir / "aligned.bam"
    keep_a = derived_name_predicate({"transform": "split", "half": "A"})
    names_a: list[str] = []
    names_b: list[str] = []
    for index in range(1000):
        name = f"pair{index}"
        chosen = names_a if keep_a(name) else names_b
        if len(chosen) < (120 if chosen is names_a else 80):
            chosen.append(name)
        if len(names_a) == 120 and len(names_b) == 80:
            break
    assert len(names_a) == 120 and len(names_b) == 80
    header = {"HD": {"VN": "1.6"}, "SQ": [{"SN": "chr1", "LN": 100000}],
              "RG": [{"ID": "rg1", "PL": "ILLUMINA"}]}
    with pysam.AlignmentFile(str(source), "wb", header=header) as out:
        for index, name in enumerate(names_a + names_b):
            for first in (True, False):
                read = pysam.AlignedSegment()
                read.query_name = name
                read.query_sequence = "A" * 75
                read.flag = 99 if first else 147
                read.reference_id = 0
                read.reference_start = index * 200 + (0 if first else 75)
                read.mapping_quality = 60
                read.cigarstring = "75M"
                read.next_reference_id = 0
                read.next_reference_start = index * 200 + (75 if first else 0)
                read.template_length = 150 if first else -150
                out.write(read)
    reference = tmp_path / "hg38.fa"
    reference.write_bytes(b">chr1\nACGT\n")
    base = dict(root=root, source_id="1" * 64, input_id="2" * 64,
                reference_id="hg38", reference_sha256=hashlib.sha256(reference.read_bytes()).hexdigest(),
                input_dir=input_dir, platform="illumina", eligible_reads=5_000_000,
                extras={"reference_fasta": str(reference)})
    a = InputContext(**{**base, "input_id": split("A").input_id(base["input_id"])},
                     derived={"transform": "split", "half": "A"})
    b = InputContext(**{**base, "input_id": split("B").input_id(base["input_id"])},
                     derived={"transform": "split", "half": "B"})
    assert a.input_id != b.input_id
    decision = module.GRIFFIN.preflight(a)
    assert isinstance(decision, Ready), decision
    assert isinstance(module.GRIFFIN.preflight(b), Ready)

    def fake_workflow(config: object) -> tuple[GriffinProfile, ...]:
        assert config.bam.name == "selected.bam"
        assert config.bam.parent.name.startswith("griffin-derived-")
        assert config.eligible_autosomal_pairs == 120
        assert count_d21_pairs(config.bam) == 120
        assert Path(str(config.bam) + ".bai").is_file()
        sample = "lab_" + a.input_id
        corrected = (config.attempt_dir / "griffin_nucleosome_profiling" / "results" /
                     sample / f"{sample}.GC_corrected.coverage.tsv")
        corrected.parent.mkdir(parents=True)
        corrected.write_bytes(b"official corrected TSV")
        (config.attempt_dir / "provenance.json").write_text(json.dumps({
            "status": "complete", "input_id": a.input_id, "upstream_commit": GRIFFIN_COMMIT,
            "platform": "illumina", "registration_sha256": config.registration_sha256,
            "eligible_autosomal_pairs": 120, "snakemake_version": "5.19.2",
            "output_sha256": hashlib.sha256(b"official corrected TSV").hexdigest(),
        }), encoding="utf-8")
        return (GriffinProfile(sample, "CTCF", 1000, (-15, 0, 15), (0.9, 0.5, 0.8), 0.5, 2.1, True, False),)

    monkeypatch.setattr(module, "run_upstream", fake_workflow)
    out = tmp_path / "scratch" / "artifacts"
    out.mkdir(parents=True)
    result = module.GRIFFIN.run(a, out)
    assert result.metrics[0]["value"] == 0.5
    assert json.loads(result.artifacts["griffin_provenance.json"])["eligible_autosomal_pairs"] == 120
    assert not list(out.parent.glob("griffin-derived-*"))

    rejected = module.GRIFFIN.run(b, out)
    assert isinstance(rejected, Skip) and rejected.reason_code == "depth"
    assert "80" in rejected.message
    assert not list(out.parent.glob("griffin-derived-*"))

    def fail_workflow(_: object) -> None:
        raise RuntimeError("upstream failed")

    monkeypatch.setattr(module, "run_upstream", fail_workflow)
    with pytest.raises(RuntimeError, match="upstream failed"):
        module.GRIFFIN.run(a, out)
    assert not list(out.parent.glob("griffin-derived-*"))


def test_derived_name_selection_validates_and_nests_downsamples() -> None:
    import numpy as np
    from traceback_lab.algorithms.cell_origin import derived_name_predicate
    from traceback_lab.read_summary import DS_SALT, SPLIT_SALT, downsample_mask, h64, split_mask

    half = derived_name_predicate({"transform": "downsample", "fraction": 0.5})
    quarter = derived_name_predicate({"transform": "downsample", "fraction": 0.25})
    names = [f"pair{index}" for index in range(100)]
    assert {name for name in names if quarter(name)} < {name for name in names if half(name)}
    for fraction in (0.25, 0.5):
        keep = derived_name_predicate({"transform": "downsample", "fraction": fraction})
        hashes = np.asarray([h64(DS_SALT, name) for name in names], dtype=np.uint64)
        assert [keep(name) for name in names] == downsample_mask(hashes, fraction).tolist()
    for letter in ("A", "B"):
        keep = derived_name_predicate({"transform": "split", "half": letter})
        hashes = np.asarray([h64(SPLIT_SALT, name) for name in names], dtype=np.uint64)
        assert [keep(name) for name in names] == split_mask(hashes, letter).tolist()
    with pytest.raises(ValueError):
        derived_name_predicate({"transform": "split", "half": "invalid"})
    with pytest.raises(ValueError):
        derived_name_predicate({"transform": "cna", "tf": 0.1})


def test_runtime_identity_changes_cache_key_and_run_refuses_planned_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import traceback_lab.algorithms.griffin as module
    from types import SimpleNamespace

    monkeypatch.setattr(module, "planned_runtime_sha256", lambda **_: "a" * 64)
    tools_a = [tool.identity() for tool in GRIFFIN.identity_tools()]
    key_a = execution_identity(GRIFFIN, reference_sha256="3" * 64, tool_identities=tools_a)
    monkeypatch.setattr(module, "planned_runtime_sha256", lambda **_: "b" * 64)
    tools_b = [tool.identity() for tool in GRIFFIN.identity_tools()]
    key_b = execution_identity(GRIFFIN, reference_sha256="3" * 64, tool_identities=tools_b)
    assert key_a != key_b

    monkeypatch.setattr(module, "inspect_griffin_assets", lambda _: SimpleNamespace(sha256="c" * 64))
    base = dict(root=tmp_path, source_id="1" * 64, input_id="2" * 64,
                reference_id="hg38", reference_sha256="3" * 64,
                input_dir=tmp_path, platform="illumina", eligible_reads=5_000_000)
    with pytest.raises(RuntimeError, match="registration changed"):
        GRIFFIN.run(InputContext(**base, extras={"planned_asset_sha256s": ["d" * 64]}), tmp_path)
    with pytest.raises(RuntimeError, match="runtime changed"):
        GRIFFIN.run(InputContext(**base, extras={"planned_asset_sha256s": ["c" * 64],
                                                 "planned_tool_identities": tools_a}), tmp_path)


def test_planned_runtime_identity_matches_real_worker_environment(tmp_path: Path) -> None:
    from traceback_lab.adapters.griffin_run import planned_runtime_sha256
    from traceback_lab.runner import _worker_env

    worker_env = _worker_env()
    planned = planned_runtime_sha256(worker_pythonpath=worker_env["PYTHONPATH"])
    child = subprocess.run(
        [sys.executable, "-c", "from traceback_lab.adapters.griffin_run import planned_runtime_sha256; "
         "print(planned_runtime_sha256())"],
        cwd=tmp_path, env=worker_env, capture_output=True, text=True, check=True,
    )
    assert child.stdout.strip() == planned
