"""FinaleToolkit cross-check contracts and real-tool parity fixtures (paired-end and ONT)."""

from __future__ import annotations

import json
import os
import random
import shutil
import subprocess
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


def _ont_fixture(root: Path) -> InputContext:
    """Synthetic native ONT single-end BAM: one primary record per molecule."""
    root.mkdir(exist_ok=True)
    input_dir = root / "input"
    input_dir.mkdir()
    reference = root / "hg38.fa"
    bases = "".join(random.Random(907).choices("ACGT", k=300_000))
    bases = bases[:10_000] + "N" + bases[10_001:]
    # chr2 holds no reads; chrUn_fixture holds only non-canonical reads.
    reference.write_text(f">chr1\n{bases}\n>chr2\n{bases[:150_000]}\n>chrX\n{bases[:100_000]}\n"
                         f">chrUn_fixture\n{bases[:20_000]}\n")
    pysam.faidx(str(reference))
    (input_dir / "manifest.json").write_text(json.dumps({"kind": "prealigned", "platform": "ont"}))
    lengths = {"chr1": 300_000, "chr2": 150_000, "chrX": 100_000, "chrUn_fixture": 20_000}
    header = {"HD": {"VN": "1.6", "SO": "coordinate"},
              "SQ": [{"SN": name, "LN": length} for name, length in lengths.items()],
              "RG": [{"ID": "rg1", "PL": "ONT"}]}
    tid = {name: index for index, name in enumerate(lengths)}
    records = []

    def add(name: str, contig: str, start: int, span: int, *, reverse: bool = False,
            mapq: int = 60, flag_add: int = 0) -> None:
        read = pysam.AlignedSegment()
        read.query_name = name
        read.query_sequence = "A" * span
        read.flag = (16 if reverse else 0) | flag_add
        read.reference_id = tid[contig]
        read.reference_start = start
        read.mapping_quality = mapq
        read.cigarstring = f"{span}M"
        read.query_qualities = pysam.qualitystring_to_array("I" * span)
        read.set_tag("RG", "rg1")
        records.append(read)

    specs = [(0, 8, 4), (1, 5, 5), (2, 3, 9)]
    for bin_index, short_n, long_n in specs:
        for ordinal, span in enumerate([120] * short_n + [180] * long_n):
            add(f"read-{bin_index}-{ordinal}", "chr1", bin_index * 100_000 + 10_000 + ordinal * 200,
                span, reverse=bool(ordinal % 2))
    # Inclusive DELFI boundaries; 99 and 221 bp still count for motifs.
    for i, span in enumerate((100, 150, 151, 220, 99, 221)):
        add(f"boundary-{span}", "chr1", (i // 2) * 100_000 + 30_000 + i * 300, span, reverse=bool(i % 2))
    # A long read (for example a concatemer) is one fragment for motifs only,
    # exactly as the Lab's end-motifs and delfi-ratio treat it.
    add("long-read", "chr1", 240_000, 2_500, reverse=True)
    add("mapq-20", "chr1", 231_000, 120, mapq=20)
    add("midpoint-next-bin", "chr1", 99_900, 220)
    add("sex-chromosome", "chrX", 5_000, 120)
    # Every D21 exclusion disappears from both sides.
    add("mapq-19", "chr1", 232_000, 120, mapq=19)
    add("duplicate", "chr1", 233_000, 120, flag_add=0x400)
    add("secondary", "chr1", 234_000, 120, flag_add=0x100)
    add("supplementary", "chr1", 235_000, 120, flag_add=0x800)
    add("qc-fail", "chr1", 236_000, 120, flag_add=0x200)
    add("non-canonical", "chrUn_fixture", 1_000, 150)
    records.sort(key=lambda record: (record.reference_id, record.reference_start))
    bam_path = input_dir / "aligned.bam"
    with pysam.AlignmentFile(str(bam_path), "wb", header=header) as bam:
        for record in records:
            bam.write(record)
    pysam.index(str(bam_path))
    return InputContext(root=root, source_id="a" * 64, input_id="b" * 64,
                        reference_id="hg38", reference_sha256="c" * 64,
                        input_dir=input_dir, platform="ont", eligible_reads=1_000_000,
                        extras={"reference_fasta": str(reference)})


def test_registered_and_platform_gate(tmp_path: Path) -> None:
    assert any(a.id == "finaletoolkit-xcheck" for a in load_registry())
    ctx = _fixture(tmp_path)
    unknown = InputContext(**{**ctx.__dict__, "platform": "unknown"})
    assert FINALETOOLKIT_XCHECK.requires.platforms == frozenset({"illumina", "ont"})
    # The runner publishes the platform skip before calling preflight.
    assert FINALETOOLKIT_XCHECK.preflight(unknown).reason_code == "platform"
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


@pytest.mark.parametrize("error_type", [ValueError, KeyError])
def test_run_parse_error_does_not_publish_an_input_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error_type: type[Exception],
) -> None:
    import traceback_lab.algorithms.finaletoolkit_xcheck as module

    ctx = _fixture(tmp_path / "DonorName_2026")
    monkeypatch.setattr(module, "_tool", lambda: ("pinned-fixture", "e" * 64))
    ctx.extras["planned_tool_pins"] = [pin.identity() for pin in FINALETOOLKIT_XCHECK.tools]

    def invalid_input(_ctx: InputContext) -> None:
        raise error_type(f"invalid {ctx.input_dir}")

    monkeypatch.setattr(module, "_inputs", invalid_input)
    with pytest.raises(RuntimeError, match="input or output became unreadable") as error:
        FINALETOOLKIT_XCHECK.run(ctx, tmp_path / "out")
    assert "DonorName_2026" not in str(error.value)


def test_tool_timeout_does_not_publish_an_input_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import traceback_lab.algorithms.finaletoolkit_xcheck as module

    ctx = _fixture(tmp_path / "DonorName_2026")
    monkeypatch.setattr(module, "_tool", lambda: ("pinned-fixture", "e" * 64))
    ctx.extras["planned_tool_pins"] = [pin.identity() for pin in FINALETOOLKIT_XCHECK.tools]

    def timed_out(command: list[str], **_kwargs: object) -> None:
        raise subprocess.TimeoutExpired(command, 7200)

    monkeypatch.setattr(module.subprocess, "run", timed_out)
    with pytest.raises(RuntimeError, match="execution timed out") as error:
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
    assert panel["motif"]["max_abs_frequency_difference"] < 1e-12
    assert panel["delfi"]["r"] == pytest.approx(1.0)
    assert panel["delfi"]["upstream_counts_equal"]
    assert panel["verdict"] == "concordant"

    # Direct retries under the same scratch parent must replace prior output.
    assert FINALETOOLKIT_XCHECK.run(ctx, tmp_path / "out").artifacts == result.artifacts

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

    # An exact count mismatch is discordant even when the upstream ratios
    # have no variance and Pearson cannot be computed.
    def replay_constant_ratios(command: list[str], **kwargs: object) -> SimpleNamespace:
        completed = replay(command, **kwargs)
        if "delfi" in command:
            path = Path(command[command.index("-o") + 1])
            rows = path.read_text().splitlines()
            path.write_text("\n".join([rows[0], *(f"chr1\t{start}\t{start + 100000}\tchr1\t1\t1\t0.5\t2\t1" for start in (0, 100000, 200000))]) + "\n")
        return completed

    monkeypatch.setattr(module.subprocess, "run", replay_constant_ratios)
    no_variation = tmp_path / "no-variation"
    no_variation.mkdir()
    changed = json.loads(FINALETOOLKIT_XCHECK.run(ctx, no_variation / "out").artifacts["finaletoolkit_xcheck.json"])
    assert changed["delfi"]["r"] is None
    assert changed["delfi"]["upstream_counts_equal"] is False
    assert changed["verdict"] == "discordant"

    # Pearson stays at one under affine smoothing, so the correlation-only
    # D13 verdict stays concordant even while absolute frequencies differ.
    def replay_smoothed_motifs(command: list[str], **kwargs: object) -> SimpleNamespace:
        completed = replay(command, **kwargs)
        if "end-motifs" in command:
            path = Path(command[command.index("-o") + 1])
            rows = [line.split("\t") for line in path.read_text().splitlines()]
            path.write_text("".join(f"{name}\t{0.5 * float(value) + 0.5 / 256}\n" for name, value in rows))
        return completed

    monkeypatch.setattr(module.subprocess, "run", replay_smoothed_motifs)
    smoothed = tmp_path / "smoothed"
    smoothed.mkdir()
    changed = json.loads(FINALETOOLKIT_XCHECK.run(ctx, smoothed / "out").artifacts["finaletoolkit_xcheck.json"])
    assert changed["motif"]["r"] == pytest.approx(1.0)
    assert changed["motif"]["max_abs_frequency_difference"] > 0.001
    assert changed["delfi"]["upstream_counts_equal"] is True
    assert changed["verdict"] == "concordant"


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


def _replay_ont(fixtures: Path, seen: list[list[str]], snapshot: dict | None = None, returncode: int = 0):
    def replay(command: list[str], **_kwargs: object) -> SimpleNamespace:
        seen.append(command)
        if snapshot is not None and "end-motifs" in command:
            # The fragment file and reference view exist only while the tool runs.
            with pysam.TabixFile(command[2]) as frags:
                snapshot["contigs"] = sorted(frags.contigs)
                snapshot["rows"] = [row.split("\t") for contig in frags.contigs for row in frags.fetch(contig)]
            with pysam.FastaFile(command[3]) as view:
                snapshot["view"] = dict(zip(view.references, view.lengths))
        if returncode:
            return SimpleNamespace(returncode=returncode)
        name = ("finaletoolkit_110_ont_motifs.tsv" if "end-motifs" in command
                else "finaletoolkit_110_ont_delfi.tsv")
        shutil.copyfile(fixtures / name, Path(command[command.index("-o") + 1]))
        return SimpleNamespace(returncode=0)
    return replay


def test_ont_mode_identity_and_gates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import traceback_lab.algorithms.finaletoolkit_xcheck as module

    illumina = _fixture(tmp_path / "illumina")
    assert FINALETOOLKIT_XCHECK.identity_parameters(illumina) == FINALETOOLKIT_XCHECK.parameters
    ont = _ont_fixture(tmp_path / "ont")
    identity = FINALETOOLKIT_XCHECK.identity_parameters(ont)
    assert identity["input_mode"] == "ont_single_end"
    assert identity["fragment"]["fragment"] == "aligned reference span [start, end) of the whole read"
    monkeypatch.setattr(module, "_tool", lambda: ("pinned-fixture", "e" * 64))
    assert FINALETOOLKIT_XCHECK.preflight(ont).__class__.__name__ == "Ready"
    derived = InputContext(**{**ont.__dict__, "derived": {"transform": "downsample", "fraction": 0.5}})
    assert FINALETOOLKIT_XCHECK.preflight(derived) == Skip(
        "derived_not_supported", "FinaleToolkit cross-check uses the full single-end input only")
    # An Illumina-declared BAM cannot run in ONT mode.
    mislabelled = InputContext(**{**illumina.__dict__, "platform": "ont"})
    assert FINALETOOLKIT_XCHECK.preflight(mislabelled).reason_code == "input_kind"


def test_ont_fragments_feed_finaletoolkit_and_match_the_lab_counts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import traceback_lab.algorithms.finaletoolkit_xcheck as module

    ctx = _ont_fixture(tmp_path)
    monkeypatch.setattr(module, "_tool", lambda: ("pinned-fixture", "e" * 64))
    ctx.extras["planned_tool_pins"] = [pin.identity() for pin in FINALETOOLKIT_XCHECK.tools]
    seen: list[list[str]] = []
    snapshot: dict = {}
    monkeypatch.setattr(module.subprocess, "run", _replay_ont(Path(__file__).parent / "data", seen, snapshot))
    result = FINALETOOLKIT_XCHECK.run(ctx, tmp_path / "out")
    work = tmp_path / "finaletoolkit-work"
    # FinaleToolkit receives the Lab's fragment file, never the ONT BAM, and a
    # reference view limited to contigs holding fragments.
    for command in seen:
        assert command[2].endswith("fragments.frag.gz")
        assert not any(part.endswith("aligned.bam") for part in command)
    assert snapshot["contigs"] == ["chr1", "chrX"]
    rows = snapshot["rows"]
    assert len(rows) == 44
    assert ["chr1", "240000", "242500", "60", "-"] in rows
    assert all(int(row[3]) >= 20 for row in rows)
    assert snapshot["view"] == {"chr1": 300_000, "chrX": 100_000}
    assert (work / "autosomes.chrom.sizes").read_text() == "chr1\t300000\n"
    # The fragment file and reference view are removed; small outputs remain.
    assert sorted(path.name for path in work.iterdir()) == [
        "autosomes.100kb.bed", "autosomes.chrom.sizes", "delfi.tsv", "motifs.tsv"]
    panel = json.loads(result.artifacts["finaletoolkit_xcheck.json"])
    assert panel["input_mode"] == "ont_single_end" and "pairs" not in panel
    assert panel["fragments"] == 44 and panel["motif_ends"] == 87
    assert panel["fragment_definition"] == module.ONT_FRAGMENT_DEFINITION
    assert panel["motif"]["fragments_crossing_1mb_windows"] == 0
    assert panel["motif"]["max_abs_frequency_difference"] < 1e-12
    assert panel["delfi"]["upstream_counts_equal"] and panel["delfi"]["comparable_bins"] == 3
    assert panel["verdict"] == "concordant"
    assert b"chr1\t100000\t5\t8\t0.625\n" in result.artifacts["reference_delfi.tsv"]
    # A direct retry rebuilds the fragment file and reference view in place.
    assert FINALETOOLKIT_XCHECK.run(ctx, tmp_path / "out").artifacts == result.artifacts


def test_illumina_panel_records_paired_end_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import traceback_lab.algorithms.finaletoolkit_xcheck as module

    ctx = _fixture(tmp_path)
    monkeypatch.setattr(module, "_tool", lambda: ("pinned-fixture", "e" * 64))
    ctx.extras["planned_tool_pins"] = [pin.identity() for pin in FINALETOOLKIT_XCHECK.tools]
    seen: list[list[str]] = []

    def replay(command: list[str], **_kwargs: object) -> SimpleNamespace:
        seen.append(command)
        name = "finaletoolkit_110_motifs.tsv" if "end-motifs" in command else "finaletoolkit_110_delfi.tsv"
        shutil.copyfile(Path(__file__).parent / "data" / name, Path(command[command.index("-o") + 1]))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(module.subprocess, "run", replay)
    panel = json.loads(FINALETOOLKIT_XCHECK.run(ctx, tmp_path / "out").artifacts["finaletoolkit_xcheck.json"])
    assert panel["input_mode"] == "paired_end" and "fragments" not in panel and "fragment_definition" not in panel
    assert len(seen) == 2
    assert all(command[2].endswith("aligned.bam") and ctx.extras["reference_fasta"] in command for command in seen)
    assert not (tmp_path / "finaletoolkit-work" / "fragments.frag.gz").exists()


def test_reference_view_copies_contigs_when_links_are_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from traceback_lab.algorithms.finaletoolkit_xcheck import _reference_view

    ctx = _ont_fixture(tmp_path)
    fasta = Path(ctx.extras["reference_fasta"])
    work = tmp_path / "work"
    work.mkdir()

    def refuse(*_args: object, **_kwargs: object) -> None:
        raise OSError("links unavailable")

    monkeypatch.setattr(Path, "symlink_to", refuse)
    view = _reference_view(fasta, [("chr1", 300_000), ("chrX", 100_000)], work)
    assert not view.is_symlink()
    with pysam.FastaFile(str(view)) as copy, pysam.FastaFile(str(fasta)) as original:
        assert list(copy.references) == ["chr1", "chrX"]
        assert copy.fetch("chrX") == original.fetch("chrX")


def test_ont_input_without_eligible_reads_fails_without_a_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import traceback_lab.algorithms.finaletoolkit_xcheck as module

    ctx = _ont_fixture(tmp_path / "DonorName_2026")
    bam_path = ctx.input_dir / "aligned.bam"
    with pysam.AlignmentFile(str(bam_path), "rb") as source:
        header = source.header.to_dict()
    bam_path.unlink()
    with pysam.AlignmentFile(str(bam_path), "wb", header=header):
        pass
    pysam.index(str(bam_path))
    monkeypatch.setattr(module, "_tool", lambda: ("pinned-fixture", "e" * 64))
    ctx.extras["planned_tool_pins"] = [pin.identity() for pin in FINALETOOLKIT_XCHECK.tools]
    with pytest.raises(RuntimeError, match="input or output became unreadable") as error:
        FINALETOOLKIT_XCHECK.run(ctx, tmp_path / "out")
    assert "DonorName_2026" not in str(error.value)


@pytest.mark.skipif(not os.environ.get("TRACEBACK_FINALETOOLKIT_BIN"),
                    reason="set TRACEBACK_FINALETOOLKIT_BIN to a pinned 1.1.0 executable")
def test_actual_finaletoolkit_110_ont_single_end_parity(tmp_path: Path) -> None:
    assert _tool() is not None
    ctx = _ont_fixture(tmp_path)
    ctx.extras["planned_tool_pins"] = [pin.identity() for pin in FINALETOOLKIT_XCHECK.tools]
    assert FINALETOOLKIT_XCHECK.preflight(ctx).__class__.__name__ == "Ready"
    result = FINALETOOLKIT_XCHECK.run(ctx, tmp_path / "out")
    panel = json.loads(result.artifacts["finaletoolkit_xcheck.json"])
    assert panel["input_mode"] == "ont_single_end"
    assert panel["fragments"] == 44 and panel["motif_ends"] == 87
    assert all(abs(panel["motif"]["upstream"][k] - panel["motif"]["reference"][k]) < 1e-12
               for k in panel["motif"]["upstream"])
    assert panel["delfi"]["upstream_counts_equal"] and panel["delfi"]["r"] == pytest.approx(1.0)
    assert _delfi(tmp_path / "finaletoolkit-work" / "delfi.tsv",
                  {("chr1", start) for start in (0, 100_000, 200_000)}) == {
        ("chr1", 0): (10, 4), ("chr1", 100_000): (5, 8), ("chr1", 200_000): (4, 9)}
    assert panel["verdict"] == "concordant"
    # The replay fixtures are this live output byte for byte.
    data = Path(__file__).parent / "data"
    assert result.artifacts["finaletoolkit_motifs.tsv"] == (data / "finaletoolkit_110_ont_motifs.tsv").read_bytes()
    assert result.artifacts["finaletoolkit_delfi.tsv"] == (data / "finaletoolkit_110_ont_delfi.tsv").read_bytes()


def test_ont_scratch_is_removed_when_finaletoolkit_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import traceback_lab.algorithms.finaletoolkit_xcheck as module

    ctx = _ont_fixture(tmp_path)
    monkeypatch.setattr(module, "_tool", lambda: ("pinned-fixture", "e" * 64))
    ctx.extras["planned_tool_pins"] = [pin.identity() for pin in FINALETOOLKIT_XCHECK.tools]
    seen: list[list[str]] = []
    snapshot: dict = {}
    monkeypatch.setattr(module.subprocess, "run",
                        _replay_ont(Path(__file__).parent / "data", seen, snapshot, returncode=1))
    with pytest.raises(RuntimeError, match="end-motifs failed"):
        FINALETOOLKIT_XCHECK.run(ctx, tmp_path / "out")
    assert snapshot["contigs"] == ["chr1", "chrX"]
    work = tmp_path / "finaletoolkit-work"
    assert sorted(path.name for path in work.iterdir()) == ["autosomes.100kb.bed", "autosomes.chrom.sizes"]


def test_legacy_illumina_result_without_plan_still_joins_a_cohort(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pre-ONT Illumina result has no execution_plan.json; its identity is static."""
    import traceback_lab.algorithms.finaletoolkit_xcheck as module
    from traceback_lab import cohorts
    from traceback_lab.cohorts import add_sample, build_cohort
    from traceback_lab.envelope import Metric, ResultEnvelope, provenance
    from traceback_lab.registry import execution_identity
    from traceback_lab.store import LabStore

    store = LabStore(tmp_path / "store")
    store.ensure()
    source_id, input_id, reference_sha256 = "1" * 64, "2" * 64, "3" * 64
    store.sources_dir.mkdir(parents=True, exist_ok=True)
    (store.sources_dir / f"{source_id}.json").write_text(json.dumps({
        "source_id": source_id, "example": False,
        "inputs": [{"input_id": input_id, "kind": "prealigned", "used_utc": "2026-10-16T00:00:00Z"}],
    }))
    input_dir = store.inputs_dir / input_id
    input_dir.mkdir(parents=True)
    (input_dir / "manifest.json").write_text(json.dumps({
        "kind": "prealigned", "platform": "illumina", "source_id": source_id, "input_id": input_id,
        "reference_sha256": reference_sha256, "tools": None, "mmi_sha256": None, "aligner_argv": None,
    }))
    header = {"HD": {"VN": "1.6"}, "SQ": [{"SN": "chr1", "LN": 1000}], "RG": [{"ID": "one", "PL": "ILLUMINA"}]}
    with pysam.AlignmentFile(str(input_dir / "aligned.bam"), "wb", header=header):
        pass
    monkeypatch.setattr(module, "_tool", lambda: ("pinned-fixture", "e" * 64))
    monkeypatch.setattr(cohorts, "load_registry", lambda: (FINALETOOLKIT_XCHECK,))
    identity = execution_identity(
        FINALETOOLKIT_XCHECK, reference_sha256=reference_sha256, upstream=[], asset_sha256s=(),
        platform="illumina", tool_pins=[pin.identity() for pin in FINALETOOLKIT_XCHECK.tools],
    )
    store.publish_result(ResultEnvelope(
        algorithm_id=FINALETOOLKIT_XCHECK.id, algorithm_version=FINALETOOLKIT_XCHECK.version, status="ok",
        provenance=provenance(tool="fixture", parameters_sha256=identity,
                              source_id=source_id, input_id=input_id, reference_id="hg38"),
        metrics=[Metric(metric_id="motif_pearson_r", value=0.99)],
    ), input_id=input_id)
    attempt = store.selected_attempt(input_id, FINALETOOLKIT_XCHECK.id, identity)
    assert attempt is not None and not (attempt / "execution_plan.json").exists()
    add_sample(store, "illumina", source_id)
    generation = build_cohort(store, "illumina")
    assert [row.algorithm_id for row in generation.members[0].results] == ["finaletoolkit-xcheck"]


def test_finaletoolkit_subprocesses_drop_the_lab_worker_pythonpath(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lab workers export PYTHONPATH for the Lab's own packages; FinaleToolkit
    runs in its own interpreter and must not import them (the pin would change
    between planning and the worker)."""

    from traceback_lab.algorithms import finaletoolkit_xcheck as ftk

    interpreter = tmp_path / "python"
    interpreter.write_text("")
    launcher = tmp_path / "finaletoolkit"
    launcher.write_text(f"#!{interpreter}\n")
    monkeypatch.setenv("TRACEBACK_FINALETOOLKIT_BIN", str(launcher))
    monkeypatch.setenv("PYTHONPATH", "/lab/site-packages")
    monkeypatch.setenv("PYTHONHOME", "/lab")
    seen: list[dict[str, str]] = []

    def fake_run(argv, **kwargs):  # noqa: ANN001, ANN202
        seen.append(kwargs.get("env") or dict(os.environ))
        return subprocess.CompletedProcess(argv, 1, "", "")

    monkeypatch.setattr(ftk.subprocess, "run", fake_run)
    assert ftk._tool() is None
    assert seen and all("PYTHONPATH" not in env and "PYTHONHOME" not in env for env in seen)
    assert ftk._tool_env().get("PATH") == os.environ.get("PATH")
