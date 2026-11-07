"""ichorCNA asset registration from the installed toolchain (signal CN2).

The extdata directories here are tiny generated stand-ins in ``tmp_path``.
One test reads the real installed package and runs only when the pinned
toolchain is installed in the per-user cache.
"""

from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from dataclasses import dataclass
from pathlib import Path

import pytest

from traceback_runner import cli, copy_number_method
from traceback_runner.copy_number_method import (
    LOCKED_BIN_SIZE_BP,
    copy_number_asset_files,
    register_toolchain_assets,
)
from traceback_runner.references import (
    ICHOR_CENTROMERE_FILE,
    ICHOR_EXTDATA_RELPATH,
    AssetKind,
    ReferenceProblem,
    ichor_toolchain_files,
    list_assets,
    load_asset,
    register_ichor_toolchain_directory,
)

LOCK = "ab" * 32
TAG = LOCK[:12]
CENTROMERE = "Chr\tStart\tEnd\tGapType\nchr1\t3000000\t4000000\tcentromere\n"


def _wig(step: int, values: tuple[str, ...] = ("0.4", "-1", "0.5")) -> str:
    return f"fixedStep chrom=chr1 start=1 step={step} span={step}\n" + "\n".join(values) + "\n"


def _extdata(base: Path, *, bin_size: int = LOCKED_BIN_SIZE_BP, gc_step: int | None = None) -> Path:
    directory = base / "lib" / "R" / "library" / ICHOR_EXTDATA_RELPATH
    directory.mkdir(parents=True)
    kb = f"{bin_size // 1000}kb"
    (directory / f"gc_hg38_{kb}.wig").write_text(_wig(gc_step or bin_size))
    (directory / f"map_hg38_{kb}.wig").write_text(_wig(bin_size, ("0.9", "0", "1")))
    (directory / ICHOR_CENTROMERE_FILE).write_text(CENTROMERE)
    return directory


@dataclass(frozen=True)
class _Identity:
    lock_sha256: str


@dataclass(frozen=True)
class _Toolchain:
    prefix: Path
    identity: _Identity

    @property
    def r_library(self) -> Path:
        return self.prefix / "lib" / "R" / "library"


def _main(*argv: object) -> tuple[int, dict]:
    stream = io.StringIO()
    with redirect_stdout(stream):
        code = cli.main([*map(str, argv), "--json"])
    return code, json.loads(stream.getvalue())


def test_asset_ids_carry_the_bin_size_and_the_toolchain_tag() -> None:
    files = ichor_toolchain_files(1_000_000, TAG)
    assert files == {
        AssetKind.ICHOR_GC_WIG: ("gc_hg38_1000kb.wig", f"asset_ichor_gc_hg38_1000kb_{TAG}"),
        AssetKind.ICHOR_MAP_WIG: ("map_hg38_1000kb.wig", f"asset_ichor_map_hg38_1000kb_{TAG}"),
        AssetKind.ICHOR_CENTROMERE: (ICHOR_CENTROMERE_FILE, f"asset_ichor_centromere_grch38_{TAG}"),
    }
    assert copy_number_asset_files(LOCK) == files
    with pytest.raises(ValueError, match="no hg38 wig"):
        ichor_toolchain_files(250_000, TAG)
    with pytest.raises(ValueError, match="12 lowercase hex"):
        ichor_toolchain_files(1_000_000, "XYZ")


def test_registers_three_assets_from_the_package_directory(tmp_path: Path) -> None:
    root = tmp_path / "root"
    extdata = _extdata(tmp_path / "env")
    results = register_ichor_toolchain_directory(
        root, extdata, bin_size_bp=LOCKED_BIN_SIZE_BP, toolchain_tag=TAG
    )
    assert [item.registered.kind for item in results] == [
        AssetKind.ICHOR_GC_WIG,
        AssetKind.ICHOR_MAP_WIG,
        AssetKind.ICHOR_CENTROMERE,
    ]
    assert all(item.created for item in results)
    assert [item.registered.parse_check.bin_size_bp for item in results[:2]] == [
        LOCKED_BIN_SIZE_BP
    ] * 2
    # Idempotent: the same files at the same place re-register as no-ops.
    again = register_ichor_toolchain_directory(
        root, extdata, bin_size_bp=LOCKED_BIN_SIZE_BP, toolchain_tag=TAG
    )
    assert not any(item.created for item in again)
    assert len(list_assets(root)) == 3


def test_a_missing_package_file_registers_nothing(tmp_path: Path) -> None:
    root = tmp_path / "root"
    extdata = _extdata(tmp_path / "env")
    (extdata / ICHOR_CENTROMERE_FILE).unlink()
    with pytest.raises(ReferenceProblem) as caught:
        register_ichor_toolchain_directory(
            root, extdata, bin_size_bp=LOCKED_BIN_SIZE_BP, toolchain_tag=TAG
        )
    assert caught.value.code == "TBX-ASSET-003"
    assert ICHOR_CENTROMERE_FILE in caught.value.cause
    assert str(tmp_path) not in caught.value.cause
    assert list_assets(root) == ()


def test_a_wig_of_another_bin_size_registers_nothing(tmp_path: Path) -> None:
    root = tmp_path / "root"
    # The file is named for 1 Mb but its header says 500 kb.
    extdata = _extdata(tmp_path / "env", gc_step=500_000)
    with pytest.raises(ReferenceProblem) as caught:
        register_ichor_toolchain_directory(
            root, extdata, bin_size_bp=LOCKED_BIN_SIZE_BP, toolchain_tag=TAG
        )
    assert caught.value.code == "TBX-ASSET-005"
    assert "500000 bp bins" in caught.value.cause
    assert list_assets(root) == ()


def test_a_malformed_centromere_table_registers_nothing(tmp_path: Path) -> None:
    root = tmp_path / "root"
    extdata = _extdata(tmp_path / "env")
    (extdata / ICHOR_CENTROMERE_FILE).write_text("Chr\tStart\n1\t2\n")
    with pytest.raises(ReferenceProblem) as caught:
        register_ichor_toolchain_directory(
            root, extdata, bin_size_bp=LOCKED_BIN_SIZE_BP, toolchain_tag=TAG
        )
    assert caught.value.code == "TBX-ASSET-005"
    assert list_assets(root) == ()


def test_the_bin_size_comes_from_the_locked_method(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A package with only 500 kb files cannot satisfy the 1 Mb locked method.
    env = tmp_path / "env"
    _extdata(env, bin_size=500_000)
    toolchain = _Toolchain(prefix=env, identity=_Identity(lock_sha256=LOCK))
    with pytest.raises(ReferenceProblem) as caught:
        register_toolchain_assets(tmp_path / "root", toolchain=toolchain)
    assert caught.value.code == "TBX-ASSET-003"
    assert "gc_hg38_1000kb.wig" in caught.value.cause
    # If the locked method said 500 kb, the same package would register.
    monkeypatch.setattr(copy_number_method, "LOCKED_BIN_SIZE_BP", 500_000)
    results = register_toolchain_assets(tmp_path / "root", toolchain=toolchain)
    assert results[0].registered.asset_id == f"asset_ichor_gc_hg38_500kb_{TAG}"


def test_cli_from_toolchain_registers_and_refuses_kind_or_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from traceback_runner import toolchain as toolchain_module

    env = tmp_path / "env"
    _extdata(env)
    fake = _Toolchain(prefix=env, identity=_Identity(lock_sha256=LOCK))
    monkeypatch.setattr(toolchain_module, "resolve_copy_number_toolchain", lambda: fake)
    root = tmp_path / "root"
    code, result = _main(
        "method-asset", "register", "--from-toolchain", "copy-number", "--root", root
    )
    assert code == 0, result
    assert [item["asset_id"] for item in result["data"]["assets"]] == [
        f"asset_ichor_gc_hg38_1000kb_{TAG}",
        f"asset_ichor_map_hg38_1000kb_{TAG}",
        f"asset_ichor_centromere_grch38_{TAG}",
    ]
    assert "3 of 3 ichorCNA assets" in result["summary"]
    assert str(tmp_path) not in json.dumps(result)
    code, result = _main("method-asset", "register", "--from-toolchain", "ichor", "--root", root)
    assert code == 0 and "0 of 3" in result["summary"]
    code, result = _main(
        "method-asset",
        "register",
        "--from-toolchain",
        "copy-number",
        "--kind",
        "ichor-gc-wig",
        "--root",
        root,
    )
    assert code == 2 and "--from-toolchain derives" in result["summary"]


def test_cli_from_toolchain_without_the_toolchain_is_tool_002(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from traceback_runner import toolchain as toolchain_module

    def missing() -> object:
        raise toolchain_module._ichor_missing("no complete install in the per-user cache")

    monkeypatch.setattr(toolchain_module, "resolve_copy_number_toolchain", missing)
    code, result = _main(
        "method-asset", "register", "--from-toolchain", "copy-number", "--root", tmp_path / "root"
    )
    assert code == 3 and result["status"] == "blocked"
    assert result["data"]["code"] == "TBX-TOOL-002" and result["data"]["retryable"]
    assert not (tmp_path / "root" / "assets-local").exists()


def test_an_unregistered_ichor_asset_names_the_toolchain_command(tmp_path: Path) -> None:
    with pytest.raises(ReferenceProblem) as caught:
        load_asset(tmp_path, f"asset_ichor_gc_hg38_1000kb_{TAG}", kind=AssetKind.ICHOR_GC_WIG)
    assert caught.value.code == "TBX-ASSET-004"
    assert "--from-toolchain copy-number" in caught.value.fix
    with pytest.raises(ReferenceProblem) as loyfer:
        load_asset(tmp_path, "asset_x", kind=AssetKind.LOYFER_ATLAS)
    assert "--from-dir" in loyfer.value.fix


def _installed_toolchain() -> object | None:
    from traceback_runner.toolchain import ToolProblem, resolve_copy_number_toolchain

    try:
        return resolve_copy_number_toolchain()
    except ToolProblem:
        return None


def test_real_installed_package_registers(tmp_path: Path) -> None:
    toolchain = _installed_toolchain()
    if toolchain is None:
        pytest.skip("the pinned ichorCNA toolchain is not installed")
    results = register_toolchain_assets(tmp_path / "root", toolchain=toolchain)
    assert [item.registered.kind for item in results] == [
        AssetKind.ICHOR_GC_WIG,
        AssetKind.ICHOR_MAP_WIG,
        AssetKind.ICHOR_CENTROMERE,
    ]
    assert {item.registered.parse_check.bin_size_bp for item in results[:2]} == {
        LOCKED_BIN_SIZE_BP
    }
    assert results[2].registered.parse_check.format == "ichor-centromere-tsv"
