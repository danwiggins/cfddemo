"""Method asset registration, ``method-asset register|show`` and job copies (signal SH2).

Every file here is a tiny generated stand-in written to ``tmp_path``; no real
atlas, wig or panel is read.
"""

from __future__ import annotations

import bz2
import gzip
import lzma
import hashlib
import io
import json
import os
import stat
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from traceback_runner import cli
from traceback_runner.references import (
    LOYFER_DIRECTORY_FILES,
    AssetKind,
    ReferenceProblem,
    copy_registered_asset,
    list_assets,
    load_asset,
    register_asset,
    register_loyfer_directory,
    verify_registered_asset,
)

ATLAS = (
    "chr\tstart\tend\tstartCpG\tendCpG\ttarget\tname\tdirection\tTypeA\tTypeB\n"
    "chr1\t100\t200\t1\t6\tTypeA\tchr1:100-200\tU\t0.1\t0.9\n"
    "chr2\t300\t420\t7\t12\tTypeB\tchr2:300-420\tU\t0.8\t0.2\n"
)
MARKERS = (
    "#chr\tstart\tend\tstartCpG\tendCpG\ttarget\tregion\tlenCpG\tbp\ttg_mean\tbg_mean"
    "\tdelta_means\tdelta_quants\tdelta_maxmin\tttest\tdirection\n"
    "chr1\t100\t200\t1\t6\tTypeA\tchr1:100-200\t5CpGs\t100bp\t0.1\t0.9\t0.8\t0.5"
    "\t0.4\t1e-9\tU\n"
)
REGIONS = "chr1\t100\t200\nchr2\t300\t420\n"
WIG = (
    "fixedStep chrom=chr1 start=1 step=1000 span=1000\n0.5\n-1\n0.42\n"
    "fixedStep chrom=chr2 start=1 step=1000 span=1000\n0.61\n"
)
MAP_WIG = (
    "fixedStep chrom=chr1 start=1 step=1000 span=1000\n0.5\n0\n0.42\n"
    "fixedStep chrom=chr2 start=1 step=1000 span=1000\n1\n"
)
CENTROMERE = "Chr\tStart\tEnd\tGapType\nchr1\t1000\t3000\tcentromere\n"
PON = gzip.compress(mtime=0, data=b"X\n\x00\x00\x00\x03synthetic")

VALID: dict[AssetKind, bytes] = {
    AssetKind.LOYFER_ATLAS: ATLAS.encode(),
    AssetKind.LOYFER_MARKERS: MARKERS.encode(),
    AssetKind.LOYFER_REGIONS: REGIONS.encode(),
    AssetKind.ICHOR_GC_WIG: WIG.encode(),
    AssetKind.ICHOR_MAP_WIG: MAP_WIG.encode(),
    AssetKind.ICHOR_CENTROMERE: CENTROMERE.encode(),
    AssetKind.ICHOR_PON: PON,
}
FORMATS = {
    AssetKind.LOYFER_ATLAS: "loyfer-atlas-tsv",
    AssetKind.LOYFER_MARKERS: "loyfer-markers-tsv",
    AssetKind.LOYFER_REGIONS: "bed",
    AssetKind.ICHOR_GC_WIG: "wig-fixed-step",
    AssetKind.ICHOR_MAP_WIG: "wig-fixed-step",
    AssetKind.ICHOR_CENTROMERE: "ichor-centromere-tsv",
    AssetKind.ICHOR_PON: "r-serialized",
}


def _file(tmp_path: Path, name: str, content: bytes) -> Path:
    path = tmp_path / "files" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def _main(*argv: object) -> tuple[int, dict]:
    stream = io.StringIO()
    with redirect_stdout(stream):
        code = cli.main([*map(str, argv), "--json"])
    return code, json.loads(stream.getvalue())


def _loyfer_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "loyfer"
    directory.mkdir()
    contents = {
        AssetKind.LOYFER_ATLAS: ATLAS,
        AssetKind.LOYFER_MARKERS: MARKERS,
        AssetKind.LOYFER_REGIONS: REGIONS,
    }
    for kind, (name, _) in LOYFER_DIRECTORY_FILES.items():
        (directory / name).write_text(contents[kind])
    return directory


@pytest.mark.parametrize("kind", list(AssetKind))
def test_each_kind_registers_write_once_with_its_parse_check(tmp_path: Path, kind: AssetKind) -> None:
    root = tmp_path / "root"
    path = _file(tmp_path, "asset.bin", VALID[kind])
    result = register_asset(root, kind, "asset_x", path)
    assert result.created
    registered = result.registered
    assert registered.file_sha256 == hashlib.sha256(VALID[kind]).hexdigest()
    assert registered.byte_size == len(VALID[kind])
    assert registered.parse_check.format == FORMATS[kind]
    if kind in (AssetKind.ICHOR_GC_WIG, AssetKind.ICHOR_MAP_WIG):
        assert registered.parse_check.bin_size_bp == 1000
    directory = root / "assets-local" / kind.value / "asset_x"
    assert sorted(entry.name for entry in directory.iterdir()) == [
        "registered-asset.json",
        "source.json",
    ]
    for entry in directory.iterdir():
        assert stat.S_IMODE(entry.stat().st_mode) == 0o600
    # The content identity never carries the local locator.
    assert str(tmp_path) not in (directory / "registered-asset.json").read_text()
    again = register_asset(root, kind.value, "asset_x", path)
    assert not again.created and again.registered == registered
    assert load_asset(root, "asset_x").registered == registered
    assert list_assets(root) == ((kind, "asset_x"),)


def _consumer_accepts(kind: AssetKind, path: Path) -> bool:
    """Whether the analysis's own parser accepts ``path`` for ``kind``."""

    from evidence_inspector import cell_origin_pipeline as pipeline
    from evidence_inspector import ichor_adapter as ichor

    try:
        if kind is AssetKind.LOYFER_ATLAS:
            pipeline.read_atlas_u250(path)
        elif kind is AssetKind.LOYFER_MARKERS:
            pipeline.read_marker_metadata(path)
        elif kind is AssetKind.LOYFER_REGIONS:
            pipeline.read_marker_regions(path)
        elif kind is AssetKind.ICHOR_CENTROMERE:
            ichor.parse_centromere_table(path)
        else:
            role = "gc_wig" if kind is AssetKind.ICHOR_GC_WIG else "map_wig"
            ichor.parse_fixed_step_wig(path, role)
    except (pipeline.CellOriginPipelineError, ichor.IchorOutputError):
        return False
    return True


def _registers(tmp_path: Path, kind: AssetKind, path: Path) -> bool:
    root = tmp_path / "root"
    try:
        register_asset(root, kind, "asset_parity", path)
    except ReferenceProblem as problem:
        assert problem.code == "TBX-ASSET-005", problem.code
        assert not (root / "assets-local" / kind.value / "asset_parity").exists()
        return False
    return True


_ATLAS_HEADER = "chr\tstart\tend\tstartCpG\tendCpG\ttarget\tname\tdirection"
_ROW_CAP_REGIONS = "".join(f"chr1\t{10 * i}\t{10 * i + 5}\n" for i in range(20_001))

# (kind, content, the consumer is expected to accept it).  Each case is checked
# against the consumer itself too, so a wrong expectation fails loudly.
PARITY_CASES = [
    (AssetKind.LOYFER_REGIONS, REGIONS, True),
    (AssetKind.LOYFER_REGIONS, '"chr1"\t100\t200\n', False),  # the prefilter reader keeps quotes
    (AssetKind.LOYFER_REGIONS, 'chr1\t"100"\t200\n', False),
    (AssetKind.LOYFER_REGIONS, '"chr1\t100"\t200\n', False),  # a quoted tab joins fields
    (AssetKind.LOYFER_REGIONS, "chr1\t100\n", False),
    (AssetKind.LOYFER_REGIONS, "chr1\t100\t200\tx\n", False),
    (AssetKind.LOYFER_REGIONS, "track name=a\n" + REGIONS, False),
    (AssetKind.LOYFER_REGIONS, "chr1\t0100\t200\n", False),
    (AssetKind.LOYFER_REGIONS, REGIONS + "chr1\t100\t200\n", False),
    (AssetKind.LOYFER_REGIONS, _ROW_CAP_REGIONS, False),  # the 20,000-row cap
    (AssetKind.LOYFER_REGIONS, "chr1\t-5\t200\n", False),  # GenomicMarker start0 >= 0
    (AssetKind.LOYFER_REGIONS, "chrUn_x\t100\t200\n", False),  # GenomicMarker chromosome
    (AssetKind.LOYFER_MARKERS, MARKERS, True),
    (AssetKind.LOYFER_MARKERS, MARKERS.replace("\nchr1\t100\t", "\nchr1\t-100\t"), False),
    (AssetKind.LOYFER_MARKERS, MARKERS.replace("\tTypeA\t", "\t.TypeA\t"), False),  # target ID
    (AssetKind.LOYFER_MARKERS, "#chr\tstart\tend\tstartCpG\tendCpG\ttarget\nchr1\t1\t2\t1\t2\tA\n", False),
    (AssetKind.LOYFER_MARKERS, MARKERS + MARKERS.split("\n")[1].replace("\tU", "\tM") + "\n", False),
    (AssetKind.LOYFER_ATLAS, ATLAS, True),
    (AssetKind.LOYFER_ATLAS, ATLAS.split("\n", 1)[1], False),  # no header
    (AssetKind.LOYFER_ATLAS, ATLAS.replace("\t0.9\n", "\t1.5\n"), False),  # U fraction > 1
    (AssetKind.LOYFER_ATLAS, ATLAS.replace("\t0.9\n", "\tabc\n"), False),
    (AssetKind.LOYFER_ATLAS, ATLAS.replace("\tU\t0.1", "\tM\t0.1"), False),
    (AssetKind.LOYFER_ATLAS, ATLAS.replace("TypeA\tTypeB\n", "Type A\tType-A\n", 1), False),  # labels collide
    (AssetKind.LOYFER_ATLAS, ATLAS.replace("\tTypeA\tTypeB\n", "\t.TypeA\tTypeB\n", 1), False),  # not a model ID
    (AssetKind.LOYFER_ATLAS, ATLAS.replace("\tTypeA\tTypeB\n", "\tmarker_id\tTypeB\n", 1), False),  # reserved key
    (AssetKind.LOYFER_ATLAS, ATLAS.replace("\t0.9\n", "\t0.9 \n"), False),  # padded fraction
    (AssetKind.LOYFER_ATLAS, ATLAS.replace("\tTypeB\n", "\tTypeB.bam\n", 1), False),  # unpublishable label
    (AssetKind.LOYFER_ATLAS, f"{_ATLAS_HEADER}\tTypeA\nchr1\t1\t2\t1\t2\tTypeA\tchr1:1-2\tU\tNA\n", False),
    (AssetKind.ICHOR_CENTROMERE, CENTROMERE, True),
    (AssetKind.ICHOR_CENTROMERE, "Chr\tStart\tEnd\tGapType\nchr1\t1000\t1000\tcentromere\n", True),  # single base
    (AssetKind.ICHOR_CENTROMERE, "Chr\tStart\tEnd\tGapType\nchr1\t1000\t3000\tgap\n", False),
    (AssetKind.ICHOR_CENTROMERE, "Chr\tStart\tEnd\tGapType\nchr1\t0\t3000\tcentromere\n", False),
    (AssetKind.ICHOR_CENTROMERE, "Chr\tStart\tEnd\tGapType\tNotes\nchr1\t1\t2\tcentromere\tx\n", False),
    (AssetKind.ICHOR_CENTROMERE, CENTROMERE + "chr1\t1000\t3000\tcentromere\n", False),  # repeated
    (AssetKind.ICHOR_CENTROMERE, "Chr\tStart\tEnd\tGapType\nchr1\t1_000\t3000\tcentromere\n", False),
    (AssetKind.ICHOR_CENTROMERE, "Chr\tStart\tEnd\tGapType\nNA\t1000\t3000\tcentromere\n", False),
    (AssetKind.ICHOR_CENTROMERE, "Chr\tStart\tEnd\tGapType\nchr1\t1e3\t3000\tcentromere\n", False),
    (AssetKind.ICHOR_CENTROMERE, "Chr\tStart\tEnd\tGapType\nchr1\t1000\t2147483648\tcentromere\n", False),
    (AssetKind.ICHOR_GC_WIG, WIG, True),
    (AssetKind.ICHOR_GC_WIG, "fixedStep start=1 chrom=chr1 step=1000 span=1000\n0.5\n", False),  # reordered
    (AssetKind.ICHOR_GC_WIG, "fixedStep chrom=chr1 start=1 step=1000 span=500\n0.5\n", False),
    (AssetKind.ICHOR_GC_WIG, "fixedStepBAD chrom=chr1 start=1 step=1000 span=1000\n0.5\n", False),
    (AssetKind.ICHOR_GC_WIG, "variableStep chrom=chr1\n1 0.5\n", False),
    (AssetKind.ICHOR_GC_WIG, "fixedStep chrom=chr1 start=2147483648 step=1000 span=1000\n0.5\n", False),
    (AssetKind.ICHOR_MAP_WIG, "fixedStep chrom=chr1 start=1 step=1000 span=1000\n0.5_0\n", False),
    (AssetKind.ICHOR_GC_WIG, "fixedStep chrom=chr1 start=1 step=1000 span=1000\n0x1A\n", False),
    (AssetKind.ICHOR_GC_WIG, "fixedStep chrom=chr1 start=1 step=1000 span=1000\n-1e-3\n.5\n", True),
    (AssetKind.ICHOR_GC_WIG, WIG + "fixedStep chrom=chr3 start=1 step=1000 span=1000\n", False),  # empty block
    (AssetKind.ICHOR_GC_WIG, "fixedStep chrom=chr1 start=1 step=1000 span=1000\n" + WIG, False),  # empty block
    (AssetKind.ICHOR_MAP_WIG, "fixedStep chrom=chr1 start=2147483000 step=1000 span=1000\n0.5\n", False),
    (AssetKind.ICHOR_GC_WIG, "fixedStep chrom=chr1 start=1 step=" + "9" * 5000 + " span=1\n0.5\n", False),
    (AssetKind.ICHOR_MAP_WIG, MAP_WIG, True),
    (AssetKind.ICHOR_MAP_WIG, WIG, False),  # a -1 is outside the map range
]


@pytest.mark.parametrize(("kind", "content", "accepted"), PARITY_CASES)
def test_registration_agrees_with_the_consumer_parser(
    tmp_path: Path, kind: AssetKind, content: str, accepted: bool
) -> None:
    path = _file(tmp_path, "asset", content.encode())
    assert _consumer_accepts(kind, path) is accepted
    assert _registers(tmp_path, kind, path) is accepted


def test_every_text_kind_has_parity_cases() -> None:
    covered = {(kind, accepted) for kind, _, accepted in PARITY_CASES}
    for kind in AssetKind:
        if kind is not AssetKind.ICHOR_PON:
            assert {(kind, True), (kind, False)} <= covered, kind


def test_a_line_over_the_cap_is_refused_before_parsing(tmp_path: Path) -> None:
    content = ("chr1\t100\t" + "2" * (1024 * 1024) + "\n").encode()
    with pytest.raises(ReferenceProblem) as raised:
        register_asset(tmp_path / "root", AssetKind.LOYFER_REGIONS, "asset_l", _file(tmp_path, "l", content))
    assert raised.value.code == "TBX-ASSET-005" and "implausibly long" in raised.value.cause
    # A long line followed by short ones is caught too (not only the last line).
    content = ("x" * (1024 * 1024 + 1) + "\n" + REGIONS).encode()
    with pytest.raises(ReferenceProblem) as raised:
        register_asset(tmp_path / "root", AssetKind.LOYFER_REGIONS, "asset_l", _file(tmp_path, "m", content))
    assert "implausibly long" in raised.value.cause


@pytest.mark.parametrize(
    ("content", "cause"),
    [
        (b"not an rds file", "R serialized"),
        (b"\x1f\x8b", "truncated"),
        (b"\x1f\x8b\x08\x00garbage-not-deflate", "corrupt"),
        (PON[:-6], "truncated"),
        (gzip.compress(mtime=0, data=b"plain text, not R"), "R serialized"),
        (PON + b"trailing", "after its stream"),
        # EOF lands exactly on an output-slice boundary with trailing input
        # still in zlib's unconsumed tail: refused, never drained forever.
        (
            gzip.compress(mtime=0, data=b"X\n\x00\x00\x00\x03" + b"\x00" * 1024 * 1024) + b"TRAILING" * 10,
            "after its stream",
        ),
        # A whole second gzip member after the first stream is trailing data too.
        (PON + gzip.compress(mtime=0, data=b"\x00" * 4 * 1024 * 1024), "after its stream"),
        (gzip.compress(mtime=0, data=b"X\n\x00\x00\x00\x09rest"), "version"),
        (b"", "R serialized"),
    ],
)
@pytest.mark.timeout(60)  # a drain-after-EOF regression loops forever
def test_pon_envelope_is_refused(tmp_path: Path, content: bytes, cause: str) -> None:
    root = tmp_path / "root"
    with pytest.raises(ReferenceProblem) as raised:
        register_asset(root, AssetKind.ICHOR_PON, "asset_pon", _file(tmp_path, "pon", content))
    assert raised.value.code == "TBX-ASSET-005"
    assert cause in raised.value.cause
    assert not (root / "assets-local").exists()


@pytest.mark.parametrize("compress", [gzip.compress, bz2.compress, lzma.compress, lambda b: b])
def test_pon_envelope_accepts_each_compression(tmp_path: Path, compress) -> None:
    body = b"X\n\x00\x00\x00\x03" + b"\x00" * 4096
    path = _file(tmp_path, "pon", compress(body))
    result = register_asset(tmp_path / "root", AssetKind.ICHOR_PON, "asset_pon", path)
    assert result.registered.parse_check.parser.endswith("r_serialized_envelope")


def test_pon_decompression_is_bounded(tmp_path: Path, monkeypatch) -> None:
    from traceback_runner import references

    monkeypatch.setattr(references, "_MAX_PON_DECOMPRESSED_BYTES", 64 * 1024)
    body = b"X\n\x00\x00\x00\x03" + b"\x00" * (1024 * 1024)
    path = _file(tmp_path, "pon", gzip.compress(mtime=0, data=body))
    with pytest.raises(ReferenceProblem) as raised:
        register_asset(tmp_path / "root", AssetKind.ICHOR_PON, "asset_pon", path)
    assert "implausibly large" in raised.value.cause


def test_different_bytes_or_kind_under_one_id_conflict(tmp_path: Path) -> None:
    root = tmp_path / "root"
    path = _file(tmp_path, "regions.bed", REGIONS.encode())
    register_asset(root, AssetKind.LOYFER_REGIONS, "asset_r", path)
    before = (root / "assets-local" / "loyfer-regions" / "asset_r" / "registered-asset.json").read_bytes()
    other = _file(tmp_path, "regions2.bed", b"chr3\t1\t2\n")
    with pytest.raises(ReferenceProblem) as raised:
        register_asset(root, AssetKind.LOYFER_REGIONS, "asset_r", other)
    assert raised.value.code == "TBX-ASSET-001" and raised.value.exit_code == 3
    with pytest.raises(ReferenceProblem) as raised:
        register_asset(root, AssetKind.ICHOR_CENTROMERE, "asset_r", _file(tmp_path, "c", CENTROMERE.encode()))
    assert raised.value.code == "TBX-ASSET-001"
    after = (root / "assets-local" / "loyfer-regions" / "asset_r" / "registered-asset.json").read_bytes()
    assert after == before
    assert not (root / "assets-local" / "ichor-centromere" / "asset_r").exists()


def test_rehash_catches_a_one_byte_edit_and_a_missing_file(tmp_path: Path) -> None:
    root = tmp_path / "root"
    path = _file(tmp_path, "atlas.tsv", ATLAS.encode())
    register_asset(root, AssetKind.LOYFER_ATLAS, "asset_a", path)
    assert verify_registered_asset(root, "asset_a").registered.asset_id == "asset_a"
    path.write_bytes(ATLAS.encode().replace(b"0.1", b"0.2", 1))  # same size, one byte
    with pytest.raises(ReferenceProblem) as raised:
        verify_registered_asset(root, "asset_a")
    assert raised.value.code == "TBX-ASSET-002"
    path.unlink()
    with pytest.raises(ReferenceProblem) as raised:
        verify_registered_asset(root, "asset_a")
    assert raised.value.code == "TBX-ASSET-003" and raised.value.exit_code == 4


def test_job_copy_is_hashed_in_the_job_directory(tmp_path: Path) -> None:
    root = tmp_path / "root"
    path = _file(tmp_path, "gc.wig", WIG.encode())
    register_asset(root, AssetKind.ICHOR_GC_WIG, "asset_gc", path)
    job = tmp_path / "job"
    job.mkdir()
    copy = copy_registered_asset(root, "asset_gc", job, kind="ichor-gc-wig")
    assert copy == job / "asset_gc.wig"
    assert copy.read_bytes() == WIG.encode()
    assert stat.S_IMODE(copy.stat().st_mode) == 0o600
    assert sorted(entry.name for entry in job.iterdir()) == ["asset_gc.wig"]
    # A resumed job reuses its verified copy.
    assert copy_registered_asset(root, "asset_gc", job) == copy
    # A copy changed inside the job is refused and left for inspection.
    os.chmod(copy, 0o600)
    copy.write_bytes(WIG.encode().replace(b"0.5", b"0.6"))
    with pytest.raises(ReferenceProblem) as raised:
        copy_registered_asset(root, "asset_gc", job)
    assert raised.value.code == "TBX-ASSET-002"


def test_job_copy_of_an_edited_source_is_refused_and_removed(tmp_path: Path) -> None:
    root = tmp_path / "root"
    path = _file(tmp_path, "gc.wig", WIG.encode())
    register_asset(root, AssetKind.ICHOR_GC_WIG, "asset_gc", path)
    path.write_bytes(WIG.encode().replace(b"0.5", b"0.6"))
    job = tmp_path / "job"
    job.mkdir()
    with pytest.raises(ReferenceProblem) as raised:
        copy_registered_asset(root, "asset_gc", job)
    assert raised.value.code == "TBX-ASSET-002"
    assert list(job.iterdir()) == []
    path.unlink()
    with pytest.raises(ReferenceProblem) as raised:
        copy_registered_asset(root, "asset_gc", job)
    assert raised.value.code == "TBX-ASSET-003"


def test_unregistered_asset_fix_prints_the_exact_command(tmp_path: Path) -> None:
    root = tmp_path / "root"
    with pytest.raises(ReferenceProblem) as raised:
        load_asset(root, "asset_missing", kind=AssetKind.ICHOR_CENTROMERE)
    assert raised.value.code == "TBX-ASSET-004" and raised.value.exit_code == 4
    assert (
        "traceback method-asset register --kind ichor-centromere --id asset_missing "
        "--file PATH --root ROOT"
    ) in raised.value.fix
    with pytest.raises(ReferenceProblem) as raised:
        copy_registered_asset(root, "asset_missing", tmp_path, kind="loyfer-atlas")
    assert "--from-dir LOYFER_DIR" in raised.value.fix
    code, payload = _main("method-asset", "show", "asset_missing", "--root", root)
    assert code == cli.ExitCode.NOT_FOUND
    assert payload["data"]["code"] == "TBX-ASSET-004"
    assert payload["data"]["docs"].endswith("#tbx-asset-004")


def test_from_dir_registers_the_three_loyfer_files(tmp_path: Path) -> None:
    root = tmp_path / "root"
    directory = _loyfer_dir(tmp_path)
    code, payload = _main("method-asset", "register", "--from-dir", directory, "--root", root)
    assert code == cli.ExitCode.OK, payload
    assert payload["data"]["qualified"] is False
    assert [(item["kind"], item["asset_id"], item["created"]) for item in payload["data"]["assets"]] == [
        (kind.value, asset_id, True) for kind, (_, asset_id) in LOYFER_DIRECTORY_FILES.items()
    ]
    assert str(tmp_path) not in json.dumps(payload)  # no local path in output
    code, payload = _main("method-asset", "register", "--from-dir", directory, "--root", root)
    assert code == cli.ExitCode.OK
    assert all(item["created"] is False for item in payload["data"]["assets"])
    code, payload = _main("method-asset", "show", "asset_loyfer_atlas_u250_l4_hg38", "--root", root)
    assert code == cli.ExitCode.OK
    assert payload["data"]["kind"] == "loyfer-atlas"
    assert payload["data"]["parse_check"] == "loyfer-atlas-tsv"
    assert str(tmp_path) not in json.dumps(payload)


def test_from_dir_with_a_missing_file_registers_nothing(tmp_path: Path) -> None:
    root = tmp_path / "root"
    directory = _loyfer_dir(tmp_path)
    (directory / LOYFER_DIRECTORY_FILES[AssetKind.LOYFER_REGIONS][0]).unlink()
    with pytest.raises(ReferenceProblem) as raised:
        register_loyfer_directory(root, directory)
    assert raised.value.code == "TBX-ASSET-003"
    assert str(directory) not in raised.value.cause
    assert not (root / "assets-local").exists()


def test_cli_register_file_conflict_and_usage(tmp_path: Path) -> None:
    root = tmp_path / "root"
    path = _file(tmp_path, "c.txt", CENTROMERE.encode())
    argv = ("method-asset", "register", "--kind", "ichor-centromere", "--id", "asset_c", "--file", path, "--root", root)
    code, payload = _main(*argv)
    assert code == cli.ExitCode.OK and payload["data"]["created"] is True
    code, payload = _main(*argv)
    assert code == cli.ExitCode.OK and payload["data"]["created"] is False
    path.write_bytes(CENTROMERE.encode() + b"chr2\t5000\t6000\tcentromere\n")
    code, payload = _main(*argv)
    assert code == cli.ExitCode.BLOCKED and payload["data"]["code"] == "TBX-ASSET-001"
    code, payload = _main("method-asset", "register", "--file", path, "--root", root)
    assert code == cli.ExitCode.USAGE
    code, payload = _main(
        "method-asset", "register", "--from-dir", tmp_path, "--kind", "loyfer-atlas", "--root", root
    )
    assert code == cli.ExitCode.USAGE


def test_copy_and_rehash_are_bounded_by_the_registered_size(tmp_path: Path) -> None:
    root = tmp_path / "root"
    path = _file(tmp_path, "gc.wig", WIG.encode())
    register_asset(root, AssetKind.ICHOR_GC_WIG, "asset_gc", path)
    with path.open("ab") as handle:
        handle.write(b"0.5\n")
    job = tmp_path / "job"
    job.mkdir()
    with pytest.raises(ReferenceProblem) as raised:
        copy_registered_asset(root, "asset_gc", job)
    assert raised.value.code == "TBX-ASSET-002"
    assert "longer than its registration" in raised.value.cause
    assert list(job.iterdir()) == []  # refused before anything was published
    with pytest.raises(ReferenceProblem) as raised:
        verify_registered_asset(root, "asset_gc")
    assert raised.value.code == "TBX-ASSET-002"
    path.write_bytes(WIG.encode()[:-1])  # one byte shorter
    with pytest.raises(ReferenceProblem) as raised:
        verify_registered_asset(root, "asset_gc")
    assert raised.value.code == "TBX-ASSET-002"


def test_bounded_rehash_stops_one_byte_past_the_registered_size() -> None:
    from traceback_runner.references import _bounded_sha256

    stream = io.BytesIO(b"a" * 10_000)
    assert _bounded_sha256(stream, 100) is None
    assert stream.tell() <= 101  # never reads the rest of a longer file
    exact = io.BytesIO(b"a" * 100)
    assert _bounded_sha256(exact, 100) == hashlib.sha256(b"a" * 100).hexdigest()
