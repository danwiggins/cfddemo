"""Paired-end FinaleToolkit cross-check contracts and a real-tool parity fixture."""

from __future__ import annotations

import json
import os
import random
import shutil
from types import SimpleNamespace
from pathlib import Path

import pysam
import pytest

from traceback_lab.algorithms.finaletoolkit_xcheck import (
    FINALETOOLKIT_XCHECK, _command_base, _delfi, _motifs, _pearson, _tool,
)
from traceback_lab.registry import InputContext, Skip, load_registry


def _fixture(root: Path) -> InputContext:
    root.mkdir(exist_ok=True)
    input_dir = root / "input"
    input_dir.mkdir()
    reference = root / "hg38.fa"
    bases = "".join(random.Random(419).choices("ACGT", k=300_000))
    bases = bases[:10_000] + "N" + bases[10_001:]
    reference.write_text(f">chr1\n{bases}\n>chrX\n{bases[:100_000]}\n")
    pysam.faidx(str(reference))
    manifest = {"kind": "prealigned", "platform": "illumina"}
    (input_dir / "manifest.json").write_text(json.dumps(manifest))
    header = {"HD": {"VN": "1.6", "SO": "coordinate"},
              "SQ": [{"SN": "chr1", "LN": 300_000}, {"SN": "chrX", "LN": 100_000}],
              "RG": [{"ID": "rg1", "PL": "ILLUMINA"}]}
    records = []
    specs = [(0, 8, 4), (1, 5, 5), (2, 3, 9)]
    for bin_index, short_n, long_n in specs:
        for ordinal, span in enumerate([120] * short_n + [180] * long_n):
            start = bin_index * 100_000 + 10_000 + ordinal * 200
            name = f"pair-{bin_index}-{ordinal}"
            for read1 in (True, False):
                read = pysam.AlignedSegment()
                read.query_name = name
                read.query_sequence = "A" * 50
                read.flag = 99 if read1 else 147
                read.reference_id = read.next_reference_id = 0
                read.reference_start = start if read1 else start + span - 50
                read.next_reference_start = start + span - 50 if read1 else start
                read.mapping_quality = 60
                read.cigarstring = "50M"
                read.template_length = span if read1 else -span
                read.query_qualities = pysam.qualitystring_to_array("I" * 50)
                read.set_tag("RG", "rg1")
                records.append(read)
    for read1 in (True, False):
        read = pysam.AlignedSegment()
        read.query_name = "sex-chromosome-pair"
        read.query_sequence = "A" * 50
        read.flag = 99 if read1 else 147
        read.reference_id = read.next_reference_id = 1
        read.reference_start = 5000 if read1 else 5070
        read.next_reference_start = 5070 if read1 else 5000
        read.mapping_quality = 60
        read.cigarstring = "50M"
        read.template_length = 120 if read1 else -120
        read.query_qualities = pysam.qualitystring_to_array("I" * 50)
        read.set_tag("RG", "rg1")
        records.append(read)

    def add_pair(name: str, start: int, span: int, *, mapq: int = 60,
                 read1_right: bool = False, flag_add: int = 0, proper: bool = True) -> None:
        for read1 in (True, False):
            right = read1 == read1_right
            read = pysam.AlignedSegment()
            read.query_name = name
            read.query_sequence = "A" * 50
            read.flag = (83 if read1 else 163) if read1_right else (99 if read1 else 147)
            read.flag |= flag_add
            if not proper:
                read.flag &= ~0x2
            read.reference_id = read.next_reference_id = 0
            read.reference_start = start + span - 50 if right else start
            read.next_reference_start = start if right else start + span - 50
            read.mapping_quality = mapq
            read.cigarstring = "50M"
            read.template_length = -span if right else span
            read.query_qualities = pysam.qualitystring_to_array("I" * 50)
            read.set_tag("RG", "rg1")
            records.append(read)

    # Boundary lengths are inclusive; 99/221 bp still count for motifs but
    # not DELFI. MAPQ 19 and the flagged reads must disappear from both.
    for i, span in enumerate((100, 150, 151, 220, 99, 221)):
        add_pair(f"boundary-{span}", (i // 2) * 100_000 + 30_000 + i * 300, span)
    add_pair("negative-read1-tlen", 230_000, 150, read1_right=True)
    add_pair("mapq-20", 231_000, 120, mapq=20)
    add_pair("mapq-19", 232_000, 120, mapq=19)
    add_pair("duplicate", 233_000, 120, flag_add=0x400)
    add_pair("secondary", 234_000, 120, flag_add=0x100)
    add_pair("improper", 235_000, 120, proper=False)
    add_pair("midpoint-without-read1-overlap", 99_900, 220)
    add_pair("midpoint-with-read1-overlap", 99_960, 120)
    records.sort(key=lambda record: (record.reference_id, record.reference_start))
    bam_path = input_dir / "aligned.bam"
    with pysam.AlignmentFile(str(bam_path), "wb", header=header) as bam:
        for record in records:
            bam.write(record)
    pysam.index(str(bam_path))
    return InputContext(root=root, source_id="a" * 64, input_id="b" * 64,
                        reference_id="hg38", reference_sha256="c" * 64,
                        input_dir=input_dir, platform="illumina", eligible_reads=1_000_000,
                        extras={"reference_fasta": str(reference)})


def test_registered_and_platform_gate(tmp_path: Path) -> None:
    assert any(a.id == "finaletoolkit-xcheck" for a in load_registry())
    ctx = _fixture(tmp_path)
    ont = InputContext(**{**ctx.__dict__, "platform": "ont"})
    assert FINALETOOLKIT_XCHECK.requires.platforms == frozenset({"illumina"})
    # The runner publishes the platform skip before calling preflight.
    assert isinstance(FINALETOOLKIT_XCHECK.preflight(ont), Skip)
    derived = InputContext(**{**ctx.__dict__, "derived": {"transform": "downsample", "fraction": 0.5}})
    assert FINALETOOLKIT_XCHECK.preflight(derived) == Skip(
        "derived_not_supported", "FinaleToolkit cross-check uses the full paired-end input only")
    unknown_depth = InputContext(**{**ctx.__dict__, "eligible_reads": None})
    assert FINALETOOLKIT_XCHECK.preflight(unknown_depth).reason_code == "depth"
    shallow = InputContext(**{**ctx.__dict__, "eligible_reads": 999_999})
    assert FINALETOOLKIT_XCHECK.preflight(shallow).reason_code == "depth"


def test_parsers_reject_incomplete_or_changed_outputs(tmp_path: Path) -> None:
    motif = tmp_path / "motifs.tsv"
    motif.write_text("AAAA\t1.0\n")
    with pytest.raises(ValueError, match="incomplete"):
        _motifs(motif)
    delfi = tmp_path / "delfi.tsv"
    delfi.write_text("#contig\tstart\tshort\tlong\nchr1\t0\t-1\t2\n")
    with pytest.raises(ValueError, match="invalid"):
        _delfi(delfi, {("chr1", 0)})
    delfi.write_text("#contig\tstart\tshort\tlong\nchr1\t0\t0\t0\n")
    assert _delfi(delfi, {("chr1", 0)}) == {("chr1", 0): (0, 0)}


def test_invalid_launcher_is_missing_toolchain(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    launcher = tmp_path / "bad-launcher"
    launcher.write_bytes(b"\xff\xfe\x00\x00")
    monkeypatch.setenv("TRACEBACK_FINALETOOLKIT_BIN", str(launcher))
    assert _tool() is None


def test_preflight_does_not_publish_an_input_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import traceback_lab.algorithms.finaletoolkit_xcheck as module

    ctx = _fixture(tmp_path / "DonorName_2026")
    (ctx.input_dir / "manifest.json").unlink()
    monkeypatch.setattr(module, "_tool", lambda: ("pinned-fixture", "e" * 64))
    result = FINALETOOLKIT_XCHECK.preflight(ctx)
    assert result.reason_code == "input_kind"
    assert "DonorName_2026" not in result.message
    assert str(ctx.input_dir) not in result.message


def test_exact_correlation_stays_within_the_artifact_range() -> None:
    values = [0.8130181598113199, 0.8482792727877749, 0.2276530594558811]
    assert _pearson(values, values) == 1.0


def test_run_error_does_not_publish_an_input_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import traceback_lab.algorithms.finaletoolkit_xcheck as module

    ctx = _fixture(tmp_path / "DonorName_2026")
    (ctx.input_dir / "manifest.json").unlink()
    monkeypatch.setattr(module, "_tool", lambda: ("pinned-fixture", "e" * 64))
    ctx.extras["planned_tool_pins"] = [pin.identity() for pin in FINALETOOLKIT_XCHECK.tools]
    with pytest.raises(RuntimeError, match="input or output became unreadable") as error:
        FINALETOOLKIT_XCHECK.run(ctx, tmp_path / "out")
    assert "DonorName_2026" not in str(error.value)


def test_python_override_runs_the_attested_package() -> None:
    launcher = Path("/operator/bin/finaletoolkit")
    interpreter = Path("/operator/venv/bin/python")
    assert _command_base(launcher, interpreter, overridden=False) == [str(launcher)]
    assert _command_base(launcher, interpreter, overridden=True) == [
        str(interpreter), "-m", "finaletoolkit.cli.main_cli",
    ]


def test_pinned_tool_outputs_exercise_run_without_external_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import traceback_lab.algorithms.finaletoolkit_xcheck as module

    ctx = _fixture(tmp_path)
    monkeypatch.setattr(module, "_tool", lambda: ("pinned-fixture", "e" * 64))
    ctx.extras["planned_tool_pins"] = [pin.identity() for pin in FINALETOOLKIT_XCHECK.tools]
    fixtures = Path(__file__).parent / "data"

    def replay(command: list[str], **_kwargs: object) -> SimpleNamespace:
        name = "finaletoolkit_110_motifs.tsv" if "end-motifs" in command else "finaletoolkit_110_delfi.tsv"
        shutil.copyfile(fixtures / name, Path(command[command.index("-o") + 1]))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(module.subprocess, "run", replay)
    result = FINALETOOLKIT_XCHECK.run(ctx, tmp_path / "out")
    panel = json.loads(result.artifacts["finaletoolkit_xcheck.json"])
    assert panel["pairs"] == 45 and panel["motif_ends"] == 89
    assert panel["motif"]["r"] == pytest.approx(1.0)
    assert panel["delfi"]["r"] == pytest.approx(1.0)
    assert panel["delfi"]["upstream_counts_equal"]
    assert panel["verdict"] == "concordant"

    # Exact-count agreement is an additional gate even when ratios still
    # correlate perfectly. Keep this distinct from the correlation thresholds.
    def replay_count_mismatch(command: list[str], **kwargs: object) -> SimpleNamespace:
        completed = replay(command, **kwargs)
        if "delfi" in command:
            path = Path(command[command.index("-o") + 1])
            original = path.read_text()
            assert original.count("\t10\t4\t0.49954") == 1
            path.write_text(original.replace("\t10\t4\t0.49954", "\t20\t8\t0.49954"))
        return completed

    monkeypatch.setattr(module.subprocess, "run", replay_count_mismatch)
    mismatch = tmp_path / "mismatch"
    mismatch.mkdir()
    changed = json.loads(FINALETOOLKIT_XCHECK.run(ctx, mismatch / "out").artifacts["finaletoolkit_xcheck.json"])
    assert changed["motif"]["r"] == pytest.approx(1.0)
    assert changed["delfi"]["r"] == pytest.approx(1.0)
    assert changed["delfi"]["upstream_counts_equal"] is False
    assert changed["verdict"] == "discordant"


@pytest.mark.skipif(not os.environ.get("TRACEBACK_FINALETOOLKIT_BIN"),
                    reason="set TRACEBACK_FINALETOOLKIT_BIN to a pinned 1.1.0 executable")
def test_actual_finaletoolkit_110_pair_parity(tmp_path: Path) -> None:
    tool = _tool()
    assert tool is not None
    ctx = _fixture(tmp_path)
    ctx.extras["planned_tool_pins"] = [pin.identity() for pin in FINALETOOLKIT_XCHECK.tools]
    assert FINALETOOLKIT_XCHECK.preflight(ctx).__class__.__name__ == "Ready"
    result = FINALETOOLKIT_XCHECK.run(ctx, tmp_path / "out")
    assert {"finaletoolkit_xcheck.json", "finaletoolkit_motifs.tsv", "finaletoolkit_delfi.tsv",
            "reference_delfi.tsv"} == set(result.artifacts)
    panel = json.loads(result.artifacts["finaletoolkit_xcheck.json"])
    assert panel["version"] == "1.1.0"
    assert panel["pairs"] == 45
    assert panel["motif_ends"] == 89
    assert panel["motif"]["r"] > 0.95
    assert all(abs(panel["motif"]["upstream"][k] - panel["motif"]["reference"][k]) < 1e-12
               for k in panel["motif"]["upstream"])
    assert panel["delfi"]["r"] > 0.90
    assert panel["delfi"]["comparable_bins"] == 3
    assert panel["delfi"]["upstream_counts_equal"]
    counts = _delfi(tmp_path / "finaletoolkit-work" / "delfi.tsv",
                    {("chr1", start) for start in (0, 100_000, 200_000)})
    assert counts == {("chr1", 0): (10, 4), ("chr1", 100_000): (6, 7),
                      ("chr1", 200_000): (5, 9)}
    assert b"chr1\t0\t10\t4\t2.5\n" in result.artifacts["reference_delfi.tsv"]
    assert panel["verdict"] == "concordant"
