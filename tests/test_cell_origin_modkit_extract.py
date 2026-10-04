"""CO1: the cell-origin modkit stage is pinned, explicit and bounded.

A stand-in modkit script records its argv, cwd and TMPDIR and streams native
``extract calls`` rows to stdout.  Synthetic inputs only.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path

import pysam
import pytest

import evidence_inspector.cell_origin_pipeline as pipeline_module
from evidence_inspector.cell_origin_pipeline import (
    DEFAULT_MODKIT_FILTER_THRESHOLD,
    MODKIT_WORK_DIRECTORY,
    CellOriginPipelineError,
    PipelineConfig,
    _extract_modbam,
    modkit_extract_arguments,
    run_pipeline,
)
from traceback_runner.toolchain import PinnedTool, ToolIdentity

REPO = Path(__file__).resolve().parents[1]
HEADER = (
    "read_id\tref_position\tchrom\tmod_strand\tmodified_primary_base\tfail\t"
    "call_code\tcall_prob"
)

_FAKE_MODKIT = r'''
import json, os, sys, time
record = os.environ["FAKE_MODKIT_RECORD"]
mode = os.environ.get("FAKE_MODKIT_MODE", "rows")
with open(record, "w") as handle:
    args = sys.argv[1:]
    inputs = [args[args.index("--reference") + 1], args[args.index("--include-bed") + 1],
              args[-2]]
    json.dump({"argv": args, "cwd": os.getcwd(), "cwd_mode": os.stat(".").st_mode & 0o777,
               "inputs_found": [os.path.isfile(path) for path in inputs],
               "tmpdir": os.environ.get("TMPDIR"), "pid": os.getpid()}, handle)
print(HEADER, flush=True)
if mode == "rows":
    for index in range(3):
        print(f"r{index}\t{100 + index}\tchr1\t+\tC\tfalse\tm\t0.95")
elif mode == "many":
    for index in range(2000):
        print(f"r{index}\t{index}\tchr1\t+\tC\tfalse\t-\t0.95")
    time.sleep(30)  # still running when the cap is hit
elif mode == "hang":
    time.sleep(30)
elif mode == "fail":
    sys.exit(2)
'''.replace("HEADER", repr(HEADER))


def _fake_modkit(tmp_path: Path) -> PinnedTool:
    path = tmp_path / "toolchain" / "bin" / "modkit"
    path.parent.mkdir(parents=True)
    path.write_text(f"#!{sys.executable}\n{_FAKE_MODKIT}", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    identity = ToolIdentity(
        tool_id="modkit",
        version="0.6.4",
        platform="osx-arm64",
        lock_sha256="1" * 64,
        lock_line="https://conda.anaconda.org/bioconda/osx-arm64/x.conda#sha256:" + "2" * 64,
        package_sha256="2" * 64,
        package_binary_sha256="3" * 64,
        installed_binary_sha256=digest,
    )
    return PinnedTool(path=path, identity=identity)


def _modbam(path: Path) -> Path:
    header = {"HD": {"VN": "1.6", "SO": "coordinate"}, "SQ": [{"SN": "chr1", "LN": 10_000}]}
    with pysam.AlignmentFile(str(path), "wb", header=header) as bam:
        for name, flag, mapq, start in (
            ("keep", 0, 60, 100),
            ("duplicate", 0x400, 60, 100),
            ("low-mapq", 0, 3, 100),
        ):
            segment = pysam.AlignedSegment(bam.header)
            segment.query_name = name
            segment.query_sequence = "ACGT" * 25
            segment.query_qualities = pysam.qualitystring_to_array("I" * 100)
            segment.flag = flag
            segment.reference_id = 0
            segment.reference_start = start
            segment.cigar = [(0, 100)]
            segment.mapping_quality = mapq
            bam.write(segment)
    pysam.index(str(path))
    return path


def _config(tmp_path: Path, **overrides: object) -> PipelineConfig:
    inputs = tmp_path / "inputs"
    inputs.mkdir(exist_ok=True)
    bed = inputs / "regions.bed"
    bed.write_text("chr1\t120\t180\n", encoding="utf-8")
    for name in ("markers.tsv", "atlas.tsv"):
        (inputs / name).write_text("registered\n", encoding="utf-8")
    reference = inputs / "synthetic-reference.fa"
    reference.write_text(">chr1\n" + "ACGT" * 2500 + "\n", encoding="utf-8")
    values: dict[str, object] = {
        "marker_bed": bed,
        "marker_metadata": inputs / "markers.tsv",
        "atlas_u_matrix": inputs / "atlas.tsv",
        "output_path": tmp_path / "out" / "result.json",
        "aligned_modbam": _modbam(inputs / "sample.bam"),
        "reference_fasta": reference,
        "job_directory": tmp_path / "job",
    }
    values.update(overrides)
    return PipelineConfig(**values)  # type: ignore[arg-type]


@pytest.fixture
def record(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "modkit-record.json"
    monkeypatch.setenv("FAKE_MODKIT_RECORD", str(path))
    return path


def test_locked_arguments_are_explicit_and_stream_to_stdout(tmp_path: Path) -> None:
    arguments = modkit_extract_arguments(
        reference_fasta=Path("/ref/r.fa"),
        include_bed=Path("/a/regions.bed"),
        filter_threshold=DEFAULT_MODKIT_FILTER_THRESHOLD,
        modbam=Path("/job/in.bam"),
    )
    assert arguments[:2] == ("extract", "calls")
    assert arguments[arguments.index("--reference") + 1] == "/ref/r.fa"
    assert arguments[arguments.index("--filter-threshold") + 1] == "0.912109375"
    assert {"--cpg", "--mapped-only"} <= set(arguments)
    assert "--force" not in arguments and "--allow-non-primary" not in arguments
    assert arguments[-2:] == ("/job/in.bam", "-")
    for bad in (0.0, 1.0, -0.5):
        with pytest.raises(CellOriginPipelineError, match="threshold"):
            modkit_extract_arguments(
                reference_fasta=Path("r"),
                include_bed=Path("b"),
                filter_threshold=bad,
                modbam=Path("m"),
            )


def test_filter_threshold_is_the_exact_locked_value() -> None:
    # (233 + 0.5) / 256: exactly representable, so modkit's own estimate for a
    # qual-233 cut-off and this explicit value agree bit for bit.
    assert DEFAULT_MODKIT_FILTER_THRESHOLD == (233 + 0.5) / 256
    assert PipelineConfig.__dataclass_fields__["modkit_filter_threshold"].default == (
        DEFAULT_MODKIT_FILTER_THRESHOLD
    )


def test_extract_runs_pinned_modkit_inside_the_job_directory(
    tmp_path: Path, record: Path
) -> None:
    config = _config(tmp_path)
    tool = _fake_modkit(tmp_path)

    normalized, counts = _extract_modbam(config, modkit=tool)

    work = config.job_directory / MODKIT_WORK_DIRECTORY  # type: ignore[operator]
    seen = json.loads(record.read_text(encoding="utf-8"))
    argv = seen["argv"]
    assert argv[argv.index("--reference") + 1] == str(config.reference_fasta)
    assert argv[argv.index("--filter-threshold") + 1] == "0.912109375"
    assert argv[-2] == str(work / "prefiltered.bam") and argv[-1] == "-"
    assert Path(seen["cwd"]).resolve() == work.resolve()
    assert Path(seen["tmpdir"]).resolve() == (work / "tmp").resolve()
    assert seen["cwd_mode"] == 0o700  # private before read names are written
    assert seen["inputs_found"] == [True, True, True]
    assert normalized.parent == work
    with normalized.open(encoding="utf-8") as handle:
        assert len(handle.read().splitlines()) == 4  # header + 3 streamed rows
    assert (counts.records_scanned, counts.written) == (3, 1)
    with pysam.AlignmentFile(str(work / "prefiltered.bam"), "rb") as bam:
        assert [read.query_name for read in bam.fetch()] == ["keep"]


def test_relative_input_paths_still_resolve_for_modkit(
    tmp_path: Path, record: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The standalone script's defaults are relative; modkit runs with cwd=work.
    config = _config(tmp_path)
    monkeypatch.chdir(tmp_path)
    relative = PipelineConfig(
        **{
            **{name: getattr(config, name) for name in config.__dataclass_fields__},
            "marker_bed": config.marker_bed.relative_to(tmp_path),
            "reference_fasta": config.reference_fasta.relative_to(tmp_path),  # type: ignore[union-attr]
            "aligned_modbam": config.aligned_modbam.relative_to(tmp_path),  # type: ignore[union-attr]
            "job_directory": Path("job"),
        }
    )
    _extract_modbam(relative, modkit=_fake_modkit(tmp_path))
    seen = json.loads(record.read_text(encoding="utf-8"))
    assert seen["inputs_found"] == [True, True, True]
    assert all(Path(arg).is_absolute() for arg in seen["argv"] if arg.endswith((".fa", ".bed", ".bam")))


def _dead(pid: int) -> bool:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    return False


def test_call_cap_is_a_parameter_checked_while_streaming(
    tmp_path: Path, record: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_MODKIT_MODE", "many")
    config = _config(tmp_path, maximum_calls=50)
    tool = _fake_modkit(tmp_path)
    started = time.monotonic()

    with pytest.raises(CellOriginPipelineError, match="call cap"):
        _extract_modbam(config, modkit=tool)

    # Refused while modkit still runs (it sleeps after its rows), then killed.
    assert time.monotonic() - started < 20
    assert _dead(json.loads(record.read_text(encoding="utf-8"))["pid"])
    # The same rows under a larger cap are accepted.
    monkeypatch.setenv("FAKE_MODKIT_MODE", "rows")
    _extract_modbam(_config(tmp_path, maximum_calls=3), modkit=tool)


def test_modkit_timeout_kills_the_process_group(
    tmp_path: Path, record: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_MODKIT_MODE", "hang")
    started = time.monotonic()
    with pytest.raises(CellOriginPipelineError, match="timed out"):
        _extract_modbam(_config(tmp_path), modkit=_fake_modkit(tmp_path), timeout_seconds=5)
    assert time.monotonic() - started < 25  # the stand-in would sleep 30 s
    assert _dead(json.loads(record.read_text(encoding="utf-8"))["pid"])


def test_modkit_failure_is_refused(
    tmp_path: Path, record: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_MODKIT_MODE", "fail")
    with pytest.raises(CellOriginPipelineError, match="extraction failed"):
        _extract_modbam(_config(tmp_path), modkit=_fake_modkit(tmp_path))


@pytest.mark.parametrize("missing", ["reference_fasta", "job_directory"])
def test_modbam_input_requires_reference_and_job_directory(
    tmp_path: Path, missing: str
) -> None:
    config = _config(tmp_path, **{missing: None})
    with pytest.raises(CellOriginPipelineError, match="reference FASTA|job directory"):
        run_pipeline(config, fragment_hash_salt=b"s", modkit=_fake_modkit(tmp_path))


def test_run_removes_the_modkit_work_directory(
    tmp_path: Path, record: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_MODKIT_MODE", "fail")
    monkeypatch.setattr(pipeline_module, "_load_loyfer_resources", lambda _config: None)
    config = _config(tmp_path)
    with pytest.raises(CellOriginPipelineError):
        run_pipeline(config, fragment_hash_salt=b"s", modkit=_fake_modkit(tmp_path))
    assert config.job_directory is not None and config.job_directory.is_dir()
    assert not (config.job_directory / MODKIT_WORK_DIRECTORY).exists()


def test_run_resolves_only_the_pinned_modkit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    def resolver() -> PinnedTool:
        calls.append("resolved")
        raise CellOriginPipelineError("stop after resolution")

    monkeypatch.setattr(pipeline_module, "resolve_modkit", resolver)
    monkeypatch.setattr(pipeline_module, "_load_loyfer_resources", lambda _config: None)
    monkeypatch.setattr(pipeline_module.shutil, "which", lambda *_a, **_k: pytest.fail("PATH"))
    with pytest.raises(CellOriginPipelineError, match="stop after resolution"):
        run_pipeline(_config(tmp_path), fragment_hash_salt=b"s")
    assert calls == ["resolved"]


def test_no_hard_coded_local_paths_in_the_modkit_stage() -> None:
    for function in (
        pipeline_module._extract_modbam,
        pipeline_module.preflight,
        pipeline_module.run_pipeline,
        pipeline_module.modkit_extract_arguments,
    ):
        source = inspect.getsource(function)
        assert "data/local" not in source, function.__name__
        assert "shutil.which" not in source or function is pipeline_module.preflight


def test_standalone_script_still_parses_and_plans(tmp_path: Path) -> None:
    script = REPO / "scripts" / "regenerate_cell_origin.py"
    env = {**os.environ, "PYTHONPATH": str(REPO)}
    helped = subprocess.run(
        [sys.executable, str(script), "--help"],
        capture_output=True, text=True, timeout=60, env=env, check=False,
    )
    assert helped.returncode == 0, helped.stderr
    assert "--reference" in helped.stdout and "--job-dir" in helped.stdout
    reference = tmp_path / "custom.fa"
    planned = subprocess.run(
        [sys.executable, str(script), "--aligned-modbam", str(tmp_path / "in.bam"),
         "--reference", str(reference), "--print-alignment-plan"],
        capture_output=True, text=True, timeout=60, env=env, check=False, cwd=tmp_path,
    )
    assert planned.returncode == 0, planned.stderr
    plan = json.loads(planned.stdout)
    assert str(reference) + ".mmi" in plan["steps"][1]["argv"]
