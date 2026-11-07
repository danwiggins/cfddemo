"""Copy number as a signed local record (signal methods CN3).

The stage runs end to end through ``traceback run --analysis copy-number``
with a fake toolchain: a fake ``readCounter`` (bins the counting BAM with
pysam) and a fake ``Rscript`` that writes the recorded synthetic ichorCNA
outputs from ``tests/fixtures/ichor/``.  Every input is generated in
``tmp_path``; every record is generated test data: unqualified, local, not
for clinical use.  Tests that run the real toolchain skip without it.
"""

from __future__ import annotations

import builtins
import hashlib
import io
import json
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pysam
import pytest

import traceback_runner.analyses as analyses_module
from traceback_runner import cli
from traceback_runner.analyses import COPY_NUMBER
from traceback_runner.contracts import JobState
from traceback_runner.copy_number import (
    LIMITATION_STATEMENTS,
    MEASUREMENT_SCHEMA,
    CopyNumberAnalysis,
    CopyNumberLimitationsV1,
    CopyNumberMeasurementV1,
    CopyNumberSolution,
    build_chart,
    render_report,
)
from traceback_runner.copy_number_method import (
    AUTOSOMES,
    LOCKED_BIN_SIZE_BP,
    METHOD_ID,
    copy_number_asset_files,
    copy_number_method_definition,
    default_parameters,
    parameters_sha256,
    registered_copy_number_assets,
)
from traceback_runner.references import (
    ICHOR_CENTROMERE_FILE,
    AssetKind,
    load_reference,
    register_ichor_toolchain_directory,
)
from traceback_runner.runner import Runner
from traceback_runner.toolchain import (
    IchorInstalled,
    ToolProblem,
    _expected_ichor_identity,
    _ichor_missing,
    _ichor_wrong,
    pin_for,
)

FIXTURES = Path(__file__).parent / "fixtures" / "ichor"
CHR1_LENGTH = 3_000_001  # four 1 Mb bins, the last one partial, like a real contig end
OTHER_LENGTH = 1_000

try:
    PIN = pin_for("ichor")
except ToolProblem:  # pragma: no cover - platforms without a lock
    PIN = None
pytestmark = pytest.mark.skipif(PIN is None, reason="no ichorCNA lock for this platform")


# ---------------------------------------------------------------------------
# Generated inputs
# ---------------------------------------------------------------------------


def _write_inputs(directory: Path, *, ucsc: bool = True, reads: int = 400) -> tuple[Path, Path]:
    """A chr1-chr22 FASTA (+ .fai) and a sorted, indexed BAM with reads on chr1."""

    directory.mkdir(parents=True, exist_ok=True)
    names = [f"chr{number}" if ucsc else str(number) for number in range(1, 23)]
    lengths = {name: CHR1_LENGTH if index == 0 else OTHER_LENGTH for index, name in enumerate(names)}
    fasta = directory / "reference.fa"
    with fasta.open("w") as handle:
        for name, length in lengths.items():
            handle.write(f">{name}\n")
            sequence = ("ACGT" * (length // 4 + 1))[:length]
            for start in range(0, length, 60):
                handle.write(sequence[start : start + 60] + "\n")
    pysam.faidx(str(fasta))
    header = pysam.AlignmentHeader.from_dict(
        {
            "HD": {"VN": "1.6", "SO": "coordinate"},
            "SQ": [{"SN": name, "LN": length} for name, length in lengths.items()],
        }
    )
    unsorted = directory / "unsorted.bam"
    with pysam.AlignmentFile(str(unsorted), "wb", header=header) as writer:
        for index in range(reads):
            segment = pysam.AlignedSegment(header)
            segment.query_name = f"synthetic-{index:05d}"
            segment.reference_id = 0
            segment.reference_start = (index * 7_411) % (CHR1_LENGTH - 200)
            segment.mapping_quality = 60
            segment.query_sequence = "ACGT" * 25
            segment.query_qualities = pysam.qualitystring_to_array("I" * 100)
            segment.cigarstring = "100M"
            # Every tenth alignment exercises one exclusion.
            segment.flag = {1: 0x400, 3: 0x800, 5: 0x100, 7: 0x200}.get(index % 10, 0)
            if index % 10 == 9:
                segment.mapping_quality = 5
            writer.write(segment)
    bam = directory / "sample.bam"
    pysam.sort("-o", str(bam), str(unsorted))
    unsorted.unlink()
    pysam.index(str(bam))
    return fasta, bam


def _eligible(reads: int = 400) -> int:
    return sum(1 for index in range(reads) if index % 10 not in (1, 3, 5, 7, 9))


def _write_assets(directory: Path) -> Path:
    """gc/map wigs over chr1-chr22 (chr1: 4 bins) and a centromere table."""

    directory.mkdir(parents=True, exist_ok=True)
    gc = []
    mappability = []
    for name in AUTOSOMES:
        header = f"fixedStep chrom={name} start=1 step=1000000 span=1000000"
        bins = 4 if name == "chr1" else 1
        gc.append(header)
        mappability.append(header)
        gc.extend(["0.41"] * bins if name == "chr1" else ["-1"])
        # Only chr1 passes the map-score threshold; the rest are masked.
        mappability.extend(["1"] * bins if name == "chr1" else ["0"])
    kb = f"{LOCKED_BIN_SIZE_BP // 1000}kb"
    (directory / f"gc_hg38_{kb}.wig").write_text("\n".join(gc) + "\n")
    (directory / f"map_hg38_{kb}.wig").write_text("\n".join(mappability) + "\n")
    (directory / ICHOR_CENTROMERE_FILE).write_text(
        "Chr\tStart\tEnd\tGapType\nchr2\t100\t200\tcentromere\n"
    )
    return directory


_FAKE_READCOUNTER = """#!{python}
import math, pathlib, sys
import pysam
here = pathlib.Path(__file__).resolve().parent
mode = (here / "rc_mode").read_text().strip() if (here / "rc_mode").exists() else "ok"
args = sys.argv[1:]
window = int(args[args.index("--window") + 1])
quality = int(args[args.index("--quality") + 1])
contigs = args[args.index("--chromosome") + 1].split(",")
bam = args[-1]
with pysam.AlignmentFile(bam, "rb") as reader:
    for contig in contigs:
        length = reader.get_reference_length(contig)
        counts = [0] * math.ceil(length / window)
        for record in reader.fetch(contig):
            if record.mapping_quality >= quality:
                counts[record.reference_start // window] += 1
        if mode == "fail":
            sys.exit(2)
        if mode == "extra":
            counts[0] += 1
        if mode == "short" and contig == "chr1":
            counts = counts[:-1]
        print(f"fixedStep chrom={{contig}} start=1 step={{window}} span={{window}}")
        for value in counts:
            print(value)
"""

_FAKE_RSCRIPT = """#!{python}
import pathlib, shutil, sys, time
here = pathlib.Path(__file__).resolve().parent
mode, fixture = (here / "mode").read_text().split()
args = sys.argv[3:]  # --vanilla BOOTSTRAP, then the driver's flags
out = pathlib.Path(args[args.index("--outDir") + 1])
sample = args[args.index("--id") + 1]
if mode == "fail":
    sys.exit(3)
if mode == "sleep":
    time.sleep(60)
out.mkdir(parents=True, exist_ok=True)
for path in pathlib.Path(fixture).iterdir():
    shutil.copyfile(path, out / path.name.replace("sample", sample))
(out / sample).mkdir(exist_ok=True)
(out / sample / f"{{sample}}_genomeWide.pdf").write_bytes(b"%PDF-1.4 not byte-stable")
if mode == "tamper":
    params = out / f"{{sample}}.params.txt"
    params.write_text(params.read_text().replace("Tumor Fraction\\tPloidy", "TF\\tPloidy"))
"""


@dataclass(frozen=True)
class FakeToolchain:
    prefix: Path
    identity: Any
    installed: IchorInstalled

    @property
    def readcounter(self) -> Path:
        return self.prefix / "bin" / "readCounter"

    @property
    def rscript(self) -> Path:
        return self.prefix / "bin" / "Rscript"

    @property
    def driver(self) -> Path:
        return self.prefix / "share" / "runIchorCNA.R"

    @property
    def r_library(self) -> Path:
        return self.prefix / "lib" / "R" / "library"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fake_toolchain(directory: Path) -> FakeToolchain:
    for sub in ("bin", "share", "lib/R/library"):
        (directory / sub).mkdir(parents=True, exist_ok=True)
    readcounter = directory / "bin" / "readCounter"
    readcounter.write_text(_FAKE_READCOUNTER.format(python=sys.executable))
    rscript = directory / "bin" / "Rscript"
    rscript.write_text(_FAKE_RSCRIPT.format(python=sys.executable))
    for path in (readcounter, rscript):
        path.chmod(0o755)
    driver = directory / "share" / "runIchorCNA.R"
    driver.write_text("# fake driver\n")
    (directory / "bin" / "mode").write_text(f"ok {FIXTURES / 'arm_loss'}\n")
    return FakeToolchain(
        prefix=directory,
        identity=_expected_ichor_identity(PIN),
        installed=IchorInstalled(
            readcounter_sha256=_sha(readcounter),
            rscript_sha256=_sha(rscript),
            driver_sha256=_sha(driver),
            conda_meta_paths_data_sha256="0" * 64,
            installed_paths_sha256="0" * 64,
            post_link_libraries=(),
            post_link_libraries_sha256="0" * 64,
        ),
    )


def _set_mode(toolchain: FakeToolchain, mode: str, fixture: str = "arm_loss") -> None:
    (toolchain.prefix / "bin" / "mode").write_text(f"{mode} {FIXTURES / fixture}\n")


def _json(capsys: pytest.CaptureFixture[str], *argv: object) -> tuple[int, dict]:
    code = cli.main([*map(str, argv), "--json"])
    return code, json.loads(capsys.readouterr().out)


@dataclass
class Setup:
    root: Path
    bam: Path
    toolchain: FakeToolchain
    analysis: CopyNumberAnalysis
    state: dict[str, Any]


@pytest.fixture(autouse=True)
def registry(monkeypatch: pytest.MonkeyPatch) -> dict:
    private: dict = {}
    monkeypatch.setattr(analyses_module, "_REGISTRY", private)
    return private


def _setup(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    *,
    ucsc: bool = True,
    floor: int = 10,
    timeout: int = 60,
) -> Setup:
    fasta, bam = _write_inputs(tmp_path / "inputs", ucsc=ucsc)
    root = tmp_path / "root"
    code, payload = _json(
        capsys, "reference", "register", "--fasta", fasta, "--id", "ref", "--root", root
    )
    assert code == 0, payload
    register_ichor_toolchain_directory(
        root,
        _write_assets(tmp_path / "extdata"),
        bin_size_bp=LOCKED_BIN_SIZE_BP,
        toolchain_tag=PIN.lock_sha256[:12],
    )
    toolchain = _fake_toolchain(tmp_path / "toolchain")
    state: dict[str, Any] = {"installed": True, "damaged": False}

    def resolve() -> FakeToolchain:
        if not state["installed"]:
            raise _ichor_missing("no complete install in the per-user toolchain cache")
        return toolchain

    parameters = default_parameters().model_copy(
        update={"min_counted_reads": floor, "ichor_timeout_seconds": timeout}
    )
    def deep() -> None:
        if state["damaged"]:
            raise _ichor_wrong("the installed packages differ from the lock")

    analysis = CopyNumberAnalysis(
        parameters=lambda: parameters, toolchain=resolve, verify_toolchain_deep=deep
    )
    analyses_module.register_analysis_stages(analysis.spec)
    return Setup(root=root, bam=bam, toolchain=toolchain, analysis=analysis, state=state)


def _run(capsys, setup: Setup) -> tuple[int, dict]:
    code, payload = _json(
        capsys,
        "run",
        setup.bam,
        "--reference",
        "ref",
        "--analysis",
        "copy-number",
        "--root",
        setup.root,
    )
    return code, payload["data"]["analyses"][0]


def _measurement(setup: Setup, record_id: str) -> tuple[bytes, CopyNumberMeasurementV1]:
    content = (setup.root / "records" / record_id / MEASUREMENT_SCHEMA.measurement_path).read_bytes()
    return content, CopyNumberMeasurementV1.model_validate_json(content)


# ---------------------------------------------------------------------------
# End to end with the fake toolchain
# ---------------------------------------------------------------------------


def test_arm_loss_makes_a_verified_cataloged_record_and_never_opens_rdata(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    setup = _setup(tmp_path, capsys)
    opened: list[str] = []
    real_open, real_io_open, real_os_open = builtins.open, io.open, os.open

    def spy(opener: Any) -> Any:
        def wrapped(file: Any, *args: Any, **kwargs: Any) -> Any:
            if isinstance(file, (str, os.PathLike)) and os.fspath(file).endswith(".RData"):
                opened.append(os.fspath(file))
            return opener(file, *args, **kwargs)

        return wrapped

    monkeypatch.setattr(builtins, "open", spy(real_open))
    monkeypatch.setattr(io, "open", spy(real_io_open))
    monkeypatch.setattr(os, "open", spy(real_os_open))
    code, row = _run(capsys, setup)
    monkeypatch.undo()
    assert code == 0, row
    assert opened == []
    record = row["record_id"]
    _, measurement = _measurement(setup, record)
    assert measurement.solution.identifiable is True
    assert measurement.solution.model_fraction == pytest.approx(0.1)
    assert measurement.solution.pon_mode == "none_development"
    counts = measurement.counts
    assert counts.counted_reads == _eligible()
    assert counts.records_scanned == 400
    assert counts.alignment_exclusions.total == 400 - _eligible()
    # chr1 has 4 bins with values; chr2-chr22 are masked (map 0, or centromere).
    assert (counts.bins_total, counts.bins_used, counts.bins_masked) == (25, 4, 21)
    assert [segment.call for segment in measurement.segments] == ["HETD", "NEUT"]
    assert measurement.stated_lower_limit.value == 0.03
    # Signed, verified and imported; the .RData and PDFs are in no output.
    code, verified = _json(capsys, "verify", record, "--root", setup.root)
    assert code == 0, verified
    code, imported = _json(capsys, "catalog", "import", record, "--root", setup.root)
    assert code == 0, imported
    published = list((setup.root / "runner" / "artifacts").rglob("*"))
    assert not [path for path in published if path.suffix in (".RData", ".pdf", ".bam")]
    assert not [path for path in published if "ichor-staging" in path.parts]
    report = (setup.root / "records" / record / "report.html").read_text()
    assert "0.1" not in report.split("Limitations")[0]


def test_neutral_is_a_record_with_no_identifiable_solution(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    setup = _setup(tmp_path, capsys)
    _set_mode(setup.toolchain, "ok", "neutral")
    code, row = _run(capsys, setup)
    assert code == 0, row
    _, measurement = _measurement(setup, row["record_id"])
    assert measurement.solution.identifiable is False
    assert measurement.solution.model_fraction == 0


def test_two_runs_give_byte_identical_measurements(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    contents = []
    for name in ("first", "second"):
        setup = _setup(tmp_path / name, capsys)
        code, row = _run(capsys, setup)
        assert code == 0, row
        contents.append(_measurement(setup, row["record_id"])[0])
        analyses_module._REGISTRY.clear()
    assert contents[0] == contents[1]


@pytest.mark.parametrize(
    ("mode", "fixture", "cause"),
    [
        ("fail", "arm_loss", "exited with status 3"),
        ("sleep", "arm_loss", "timed out"),
        ("tamper", "arm_loss", "failed validation"),
    ],
)
def test_ichor_failure_timeout_and_invalid_output_are_cna_004(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], mode: str, fixture: str, cause: str
) -> None:
    setup = _setup(tmp_path, capsys, timeout=3 if mode == "sleep" else 120)
    _set_mode(setup.toolchain, mode, fixture)
    code, row = _run(capsys, setup)
    assert code == cli.ExitCode.BLOCKED, row
    assert row["code"] == "TBX-CNA-004"
    assert cause in row["cause"]
    job = Runner(setup.root / "runner", local_unqualified_enabled=True).status(row["job_id"])
    assert job.state == JobState.TERMINAL_FAILURE


@pytest.mark.parametrize(
    ("rc_mode", "cause"),
    [("extra", "readCounter binned"), ("short", "are not one grid")],
)
def test_read_counts_must_match_the_prefilter_and_the_grid(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], rc_mode: str, cause: str
) -> None:
    setup = _setup(tmp_path, capsys)
    (setup.toolchain.prefix / "bin" / "rc_mode").write_text(rc_mode)
    code, row = _run(capsys, setup)
    assert code == cli.ExitCode.BLOCKED, row
    assert row["code"] == "TBX-CNA-004"
    assert cause in row["cause"]


@dataclass
class _Stage:
    attempt_dir: Path
    prior_stage_dirs: tuple[Path, ...] = ()
    sealed_input_dir: Path = Path("/nonexistent")

    def heartbeat(self) -> None:
        return None


def _context(setup: Setup) -> Any:
    from traceback_runner.analyses import AnalysisStageContext

    return AnalysisStageContext(
        root=setup.root,
        loaded=load_reference(setup.root, "ref"),
        bam_name="sample.bam",
        index_name="sample.bam.bai",
        config={},
        signing_key=None,
        progress=lambda line: None,
    )


def test_an_asset_that_is_not_the_definitions_refuses_with_job_003(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    setup = _setup(tmp_path, capsys)
    context = _context(setup)
    definition = setup.analysis.definition(context.loaded, {}, root=setup.root)
    _, gc_id = copy_number_asset_files(PIN.lock_sha256)[AssetKind.ICHOR_GC_WIG]
    altered = definition.model_copy(
        update={
            "assets": tuple(
                item.model_copy(update={"content_sha256": "f" * 64})
                if item.asset_id == gc_id
                else item
                for item in definition.assets
            )
        }
    )
    attempt = tmp_path / "attempt"
    attempt.mkdir()
    with pytest.raises(cli.LocalStageRefusal) as refused:
        setup.analysis._validate(_Stage(attempt), context, altered, setup.analysis.parameters())
    assert refused.value.code == "TBX-JOB-003"


def test_a_toolchain_that_is_not_the_definitions_refuses_with_job_003(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    setup = _setup(tmp_path, capsys)
    context = _context(setup)
    definition = setup.analysis.definition(context.loaded, {}, root=setup.root)
    prior = tmp_path / "validate"
    prior.mkdir()
    (prior / "copy-number-preflight.json").write_text('{"asset_copies": {}}')
    other = FakeToolchain(
        prefix=setup.toolchain.prefix,
        identity=setup.toolchain.identity.model_copy(update={"lock_sha256": "e" * 64}),
        installed=setup.toolchain.installed,
    )
    analysis = CopyNumberAnalysis(parameters=setup.analysis.parameters, toolchain=lambda: other)
    attempt = tmp_path / "attempt"
    attempt.mkdir()
    with pytest.raises(cli.LocalStageRefusal) as refused:
        analysis._measure(_Stage(attempt, (prior,)), context, definition, analysis.parameters())
    assert refused.value.code == "TBX-JOB-003"


def test_an_r_failure_from_a_damaged_toolchain_is_retryable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    setup = _setup(tmp_path, capsys)
    _set_mode(setup.toolchain, "fail")
    setup.state["damaged"] = True  # e.g. an R package removed after install
    code, row = _run(capsys, setup)
    assert code == cli.ExitCode.RETRYABLE_FAILURE, row
    assert row["code"] == "TBX-TOOL-002"


def test_a_readcounter_failure_from_a_damaged_toolchain_is_retryable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    setup = _setup(tmp_path, capsys)
    (setup.toolchain.prefix / "bin" / "rc_mode").write_text("fail")
    setup.state["damaged"] = True  # e.g. a shared library removed after install
    code, row = _run(capsys, setup)
    assert code == cli.ExitCode.RETRYABLE_FAILURE, row
    assert row["code"] == "TBX-TOOL-002"
    setup.state["damaged"] = False
    analyses_module._REGISTRY.clear()
    other = _setup(tmp_path / "intact", capsys)
    (other.toolchain.prefix / "bin" / "rc_mode").write_text("fail")
    code, row = _run(capsys, other)
    assert code == cli.ExitCode.BLOCKED, row
    assert row["code"] == "TBX-CNA-004" and "readCounter exited" in row["cause"]


def test_a_readcounter_launch_failure_is_retryable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from traceback_runner import contained_process

    setup = _setup(tmp_path, capsys)
    real = contained_process.run_contained

    def launch_fails(argv: Any, *args: Any, **kwargs: Any) -> Any:
        if Path(argv[0]).name == "readCounter":
            raise contained_process.ContainedProcessError(
                "could not start the executable: [Errno 35] Resource temporarily unavailable"
            )
        return real(argv, *args, **kwargs)

    monkeypatch.setattr(contained_process, "run_contained", launch_fails)
    code, row = _run(capsys, setup)
    assert code == cli.ExitCode.RETRYABLE_FAILURE, row
    assert row["code"] == "TBX-TOOL-002"


def test_an_explicit_index_is_used_by_every_step(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    setup = _setup(tmp_path, capsys)
    index = setup.bam.parent / "elsewhere.bai"
    Path(f"{setup.bam}.bai").rename(index)
    code, payload = _json(
        capsys, "run", setup.bam, "--index", index, "--reference", "ref",
        "--analysis", "copy-number", "--root", setup.root,
    )
    row = payload["data"]["analyses"][0]
    assert code == 0, row


def test_preflight_reports_the_index_depth_floor(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    setup = _setup(tmp_path, capsys)
    code, payload = _json(
        capsys, "preflight", setup.bam, "--reference", "ref", "--analysis", "copy-number",
        "--root", setup.root,
    )
    (block,) = payload["data"]["analyses"]
    rows = {row["code"]: row for row in block["checks"]}
    assert rows["TBX-CNA-001"]["outcome"] == "blocked"  # 400 mapped < the locked 1,000,000
    assert "400 mapped records" in rows["TBX-CNA-001"]["detail"]
    assert block["readiness"] == "blocked"
    from traceback_runner.copy_number import depth_readiness

    assert depth_readiness(setup.bam, floor=400).outcome == "ready"


def test_index_floor_refuses_before_counting(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    setup = _setup(tmp_path, capsys, floor=401)  # the index holds 400 mapped records
    code, row = _run(capsys, setup)
    assert code == cli.ExitCode.BLOCKED, row
    assert row["code"] == "TBX-CNA-001"
    assert "400 mapped records" in row["cause"]


def test_counted_floor_refuses_with_cna_002(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The index's 400 mapped records pass; only the eligible ones count.
    setup = _setup(tmp_path, capsys, floor=_eligible() + 1)
    code, row = _run(capsys, setup)
    assert code == cli.ExitCode.BLOCKED, row
    assert row["code"] == "TBX-CNA-002"
    assert f"{_eligible()} eligible primary alignments" in row["cause"]


def test_the_floor_is_inclusive(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    setup = _setup(tmp_path, capsys, floor=_eligible())
    code, row = _run(capsys, setup)
    assert code == 0, row


def test_non_ucsc_contigs_are_cna_003(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    setup = _setup(tmp_path, capsys, ucsc=False)
    code, row = _run(capsys, setup)
    assert code == cli.ExitCode.BLOCKED, row
    assert row["code"] == "TBX-CNA-003"
    assert "chr1, chr2, chr3 and 19 more" in row["cause"]


def test_missing_toolchain_is_retryable_then_resumes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    setup = _setup(tmp_path, capsys)
    setup.state["installed"] = False
    code, row = _run(capsys, setup)
    assert code == cli.ExitCode.RETRYABLE_FAILURE, row
    assert row["code"] == "TBX-TOOL-002"
    job_id = row["job_id"]
    job = Runner(setup.root / "runner", local_unqualified_enabled=True).status(job_id)
    assert job.state == JobState.RETRYABLE_FAILURE
    setup.state["installed"] = True
    code, resumed = _json(capsys, "resume", job_id, "--root", setup.root)
    assert code == 0, resumed


def test_a_changed_rscript_is_retryable_not_terminal(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    setup = _setup(tmp_path, capsys)
    with setup.toolchain.rscript.open("a") as handle:
        handle.write("# changed after verification\n")
    code, row = _run(capsys, setup)
    assert code == cli.ExitCode.RETRYABLE_FAILURE, row
    assert row["code"] == "TBX-TOOL-002"


def test_an_asset_changed_after_registration_is_retryable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    setup = _setup(tmp_path, capsys)
    gc = tmp_path / "extdata" / f"gc_hg38_{LOCKED_BIN_SIZE_BP // 1000}kb.wig"
    gc.write_text(gc.read_text().replace("0.41", "0.42", 1))
    code, row = _run(capsys, setup)
    assert code == cli.ExitCode.RETRYABLE_FAILURE, row
    assert row["code"] == "TBX-ASSET-002"


def test_readiness_rows(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    setup = _setup(tmp_path, capsys)
    loaded = load_reference(setup.root, "ref")
    rows = setup.analysis.readiness(loaded, {}, root=setup.root)
    assert [(row.code, row.outcome) for row in rows] == [
        ("TBX-TOOL-002", "ready"),
        ("TBX-ASSET-004", "ready"),
        ("TBX-CNA-003", "ready"),
    ]
    setup.state["installed"] = False
    shutil.rmtree(setup.root / "assets-local")
    rows = setup.analysis.readiness(loaded, {}, root=setup.root)
    assert [(row.code, row.outcome) for row in rows][:2] == [
        ("TBX-TOOL-002", "not_set_up"),
        ("TBX-ASSET-004", "not_set_up"),
    ]
    assert "--from-toolchain copy-number" in rows[1].detail


# ---------------------------------------------------------------------------
# Definition and contracts
# ---------------------------------------------------------------------------


def test_definition_binds_package_digests_assets_and_parameters(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    setup = _setup(tmp_path, capsys)
    loaded = load_reference(setup.root, "ref")
    assets = registered_copy_number_assets(setup.root, PIN.lock_sha256)
    parameters = default_parameters()
    definition = copy_number_method_definition(loaded.registered, assets, PIN, parameters)
    assert definition.method_id == METHOD_ID
    assert definition.family.value == "copy_number"
    tools = {tool.tool_id: tool.artifact_sha256 for tool in definition.tools}
    assert tools == {
        "tool_hmmcopy_bin_counter": PIN.readcounter.package_binary_sha256,
        "tool_ichorcna": PIN.ichorcna_package_sha256,
        "tool_ichorcna_driver": PIN.driver_sha256,
        "tool_ichorcna_lock": PIN.lock_sha256,
        "tool_rscript": PIN.rscript.package_binary_sha256,
    }
    bound = {item.asset_id for item in definition.assets}
    assert {asset_id for _, asset_id in copy_number_asset_files(PIN.lock_sha256).values()} < bound
    assert definition.parameter_schema_sha256 == parameters_sha256(parameters)
    other = parameters.model_copy(update={"min_counted_reads": 5})
    assert parameters_sha256(other) != parameters_sha256(parameters)
    wrong = dict(assets)
    wrong[AssetKind.ICHOR_GC_WIG] = assets[AssetKind.ICHOR_MAP_WIG]
    with pytest.raises(ValueError, match="every ichorCNA asset kind"):
        copy_number_method_definition(loaded.registered, wrong, PIN, parameters)
    renamed = dict(assets)
    renamed[AssetKind.ICHOR_GC_WIG] = assets[AssetKind.ICHOR_GC_WIG].model_copy(
        update={"asset_id": "asset_other_gc"}
    )
    with pytest.raises(ValueError, match="not the one this toolchain registers"):
        copy_number_method_definition(loaded.registered, renamed, PIN, parameters)


def test_locked_parameters() -> None:
    parameters = default_parameters()
    assert parameters.bin_size_bp == LOCKED_BIN_SIZE_BP
    assert parameters.pon_mode == "none_development"
    assert parameters.ichor.normal_fraction_starts == (0.95, 0.99, 0.995, 0.999)
    assert parameters.ichor.max_copy_number == 3
    assert parameters.counting_contigs == AUTOSOMES
    assert parameters.min_counted_reads == 1_000_000
    assert parameters.r_workspace_policy == "never_opened_never_signed"


def test_an_unidentifiable_solution_carries_no_fraction() -> None:
    with pytest.raises(ValueError, match="unidentifiable"):
        CopyNumberSolution(
            model_fraction=0.2,
            ploidy=2,
            pon_mode="none_development",
            identifiable=False,
            selection_resolved=True,
        )


def test_limitations_are_fixed_and_the_report_prints_no_estimate(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(ValueError, match="fixed statements"):
        CopyNumberLimitationsV1(
            reference_match="registered_digests", statements=LIMITATION_STATEMENTS[:-1]
        )
    setup = _setup(tmp_path, capsys)
    code, row = _run(capsys, setup)
    assert code == 0, row
    content, measurement = _measurement(setup, row["record_id"])
    report = render_report(
        measurement, CopyNumberLimitationsV1(reference_match="name_and_length_only")
    ).decode()
    assert "tumour-fraction estimate and its stated lower limit are in the signed" in report
    assert str(measurement.solution.model_fraction) not in report.split("<h2>Limitations")[0]
    chart = build_chart(measurement, hashlib.sha256(content).hexdigest())
    assert len(chart.points) == measurement.counts.bins_used


def test_copy_number_is_registered_for_run_analysis() -> None:
    from traceback_runner.copy_number import DEFAULT_ANALYSIS

    assert DEFAULT_ANALYSIS.spec.analysis == COPY_NUMBER
    assert DEFAULT_ANALYSIS.spec.takes_root
    assert MEASUREMENT_SCHEMA.measurement_path == "measurements/copy-number.v1.json"


# ---------------------------------------------------------------------------
# The real toolchain (skipped without it)
# ---------------------------------------------------------------------------


def _real_toolchain() -> Any:
    from traceback_runner.toolchain import resolve_copy_number_toolchain

    try:
        return resolve_copy_number_toolchain()
    except ToolProblem:
        return None


@pytest.mark.slow
def test_real_toolchain_measurement_is_byte_identical_across_two_runs(tmp_path: Path) -> None:
    """Real readCounter-free path: real ichorCNA on synthetic counts over the real grid."""

    from traceback_runner.copy_number import AlignmentExclusions, build_measurement
    from traceback_runner.copy_number_method import register_toolchain_assets

    toolchain = _real_toolchain()
    if toolchain is None:
        pytest.skip("the pinned ichorCNA toolchain is not installed")
    root = tmp_path / "root"
    register_toolchain_assets(root, toolchain=toolchain)
    analysis = CopyNumberAnalysis(toolchain=lambda: toolchain)
    parameters = default_parameters()
    files = copy_number_asset_files(toolchain.identity.lock_sha256)
    from traceback_runner.references import load_asset

    copies = {
        kind.value: Path(load_asset(root, asset_id, kind=kind).source.file_path)
        for kind, (_, asset_id) in files.items()
    }
    # Deterministic synthetic counts on the gc wig's chr1-chr22 grid.
    from evidence_inspector.ichor_adapter import parse_fixed_step_wig

    gc = parse_fixed_step_wig(copies["ichor-gc-wig"], "gc_wig")
    lines: list[str] = []
    total = 0
    current = None
    for index, row in enumerate(gc.grid.bins):
        if row.contig not in AUTOSOMES:
            continue
        if row.contig != current:
            current = row.contig
            lines.append(f"fixedStep chrom={row.contig} start=1 step=1000000 span=1000000")
        value = 400 + (index * 37) % 50
        total += value
        lines.append(str(value))
    outputs = []
    for name in ("first", "second"):
        attempt = tmp_path / name
        attempt.mkdir()
        counts = attempt / "read-counts.wig"
        counts.write_text("\n".join(lines) + "\n")

        @dataclass
        class Stage:
            attempt_dir: Path

            def heartbeat(self) -> None:
                return None

        grid, run_sha256 = analysis._grid("d" * 64, parameters, copies, counts, total)
        result = analysis._ichor(
            Stage(attempt), toolchain, parameters, copies, counts, grid, run_sha256
        )
        measurement = build_measurement(
            result,
            reference_id="ref",
            records_scanned=total,
            exclusions=AlignmentExclusions(
                unmapped=0, secondary=0, supplementary=0, qc_failure=0, duplicate=0,
                low_mapping_quality=0,
            ),
            counted_reads=total,
        )
        outputs.append(measurement.model_dump_json())
        assert not list(attempt.rglob("*.RData"))
    assert outputs[0] == outputs[1]



@pytest.mark.parametrize("index", ["matching", "contradictory"])
def test_preflight_includes_the_shared_bam_checks(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    index: str,
) -> None:
    import traceback_runner.copy_number as copy_number_module
    from traceback_runner.analyses import ReadinessRow

    setup = _setup(tmp_path, capsys)
    # The index depth floor is tested on its own; here it must not decide.
    monkeypatch.setattr(
        copy_number_module,
        "depth_readiness",
        lambda *args, **kwargs: ReadinessRow("TBX-CNA-001", "ready", "patched"),
    )
    if index == "contradictory":
        _, other = _write_inputs(tmp_path / "other", ucsc=False)
        Path(f"{setup.bam}.bai").write_bytes(Path(f"{other}.bai").read_bytes())
    _, payload = _json(
        capsys, "preflight", setup.bam, "--reference", "ref", "--analysis", "copy-number",
        "--root", setup.root,
    )
    (block,) = payload["data"]["analyses"]
    first = block["checks"][0]
    assert first["code"].startswith("TBX-BAM-"), block
    if index == "matching":
        assert block["readiness"] == "ready", block
        assert first["outcome"] == "ready"
    else:
        assert block["readiness"] == "blocked", block
        assert first["outcome"] == "blocked"
