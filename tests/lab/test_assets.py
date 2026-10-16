"""Pinned local Lab assets: recovery, integrity, and publication boundaries."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from traceback_lab import assets
from traceback_lab.algorithms.pmd_score import PMD_SCORE
from traceback_lab.registry import execution_identity
from traceback_lab.store import LabStore


@pytest.fixture
def pinned(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[LabStore, Path, bytes]:
    data = b"synthetic pinned PMD fixture\n"
    monkeypatch.setattr(assets, "PMD_SHA256", hashlib.sha256(data).hexdigest())
    monkeypatch.setitem(PMD_SCORE.parameters, "asset_sha256", hashlib.sha256(data).hexdigest())
    source = tmp_path / "PMD_coordinates_hg38.bed.gz"
    source.write_bytes(data)
    return LabStore(tmp_path / "root"), source, data


def _add(store: LabStore, source: Path) -> assets.AssetRecord:
    with store.writer("asset add", "r-20261016-1200-0000", step="pmd"):
        return assets.add_asset(
            store, "pmd", source, source_label="Zhou-2018-operator-supplied",
            terms="unknown",
        )


def _identity(store: LabStore) -> str:
    return execution_identity(
        PMD_SCORE, reference_sha256="a" * 64, upstream=[],
        asset_sha256s=PMD_SCORE.asset_sha256s(store.root),
    )


def test_absent_wrong_valid_and_tampered_asset(pinned: tuple[LabStore, Path, bytes]) -> None:
    store, source, data = pinned
    with pytest.raises(assets.AssetError, match="absent"):
        assets.inspect_asset(store)
    missing_identity = _identity(store)
    source.write_bytes(b"wrong")
    with pytest.raises(assets.AssetError, match="pinned"):
        _add(store, source)
    assert not (store.lab / "assets" / "pmd").exists()
    source.write_bytes(data)
    record = _add(store, source)
    assert record.sha256 == hashlib.sha256(data).hexdigest()
    assert record.descriptor.source_label == "Zhou-2018-operator-supplied"
    assert record.descriptor.terms == "unknown"
    assert not record.legacy
    assert _identity(store) != missing_identity
    assert _add(store, source) == record
    record.file.write_bytes(b"tampered")
    with pytest.raises(assets.AssetError, match="differs"):
        assets.inspect_asset(store)
    assert _identity(store) == missing_identity
    with pytest.raises(assets.AssetError, match="differs"):
        _add(store, source)
    assert record.file.read_bytes() == b"tampered"


def test_exact_legacy_file_gains_manifest_without_byte_replacement(
    pinned: tuple[LabStore, Path, bytes],
) -> None:
    store, source, data = pinned
    directory = store.lab / "assets" / "pmd"
    directory.mkdir(parents=True)
    path = directory / assets.PMD_FILENAME
    path.write_bytes(data)
    before = path.stat().st_ino
    assert assets.inspect_asset(store).legacy
    registered = _add(store, source)
    assert not registered.legacy
    assert path.stat().st_ino == before
    assert path.read_bytes() == data
    with pytest.raises(assets.AssetError, match="different provenance"):
        with store.writer("asset add", "r-20261016-1200-0001", step="pmd"):
            assets.add_asset(
                store, "pmd", source, source_label="changed", terms="known", license_id="test-license",
            )


def test_interrupted_publication_leaves_no_partial_asset(
    pinned: tuple[LabStore, Path, bytes], monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, source, _ = pinned
    def interrupted(*args: object, **kwargs: object) -> None:
        raise KeyboardInterrupt
    monkeypatch.setattr(assets, "_manifest", interrupted)
    with pytest.raises(KeyboardInterrupt):
        _add(store, source)
    parent = store.lab / "assets"
    assert not (parent / "pmd").exists()
    assert not list(parent.glob(".staging-*"))


def test_unsafe_paths_and_manifest_are_rejected(pinned: tuple[LabStore, Path, bytes]) -> None:
    store, source, data = pinned
    link = source.parent / "link.bed.gz"
    link.symlink_to(source)
    with pytest.raises(assets.AssetError, match="symlink"):
        _add(store, link)
    directory = store.lab / "assets"
    directory.mkdir(parents=True)
    (directory / "pmd").symlink_to(source.parent, target_is_directory=True)
    with pytest.raises(assets.AssetError, match="symlink"):
        assets.inspect_asset(store)
    (directory / "pmd").unlink()
    record = _add(store, source)
    (record.file.parent / "manifest.json").write_text("{}")
    with pytest.raises(assets.AssetError, match="manifest"):
        assets.inspect_asset(store)
    assert record.file.read_bytes() == data


def test_asset_command_add_and_inspect_use_shared_descriptor(
    pinned: tuple[LabStore, Path, bytes],
) -> None:
    import argparse

    from traceback_lab.asset_command import add_asset_parser

    store, source, _ = pinned
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    add_asset_parser(commands, lambda child: child.add_argument("--root"))
    args = parser.parse_args([
        "asset", "add", "pmd", "--file", str(source),
        "--source-label", "synthetic-Zhou-file", "--terms", "unknown",
    ])
    code, payload = args.handler(args, store)
    assert code == 0
    descriptor = payload["data"]["asset"]
    assert descriptor["kind"] == "pmd"
    assert descriptor["asset_id"] == f"pmd:{descriptor['sha256']}"
    assert descriptor["origin"] == "user"
    assert descriptor["source_label"] == "synthetic-Zhou-file"
    assert descriptor["terms"] == "unknown"
    assert descriptor["size_bytes"] == source.stat().st_size
    assert descriptor["schema"] == "traceback.lab.asset.v1"

    args = parser.parse_args(["asset", "inspect", "pmd"])
    code, inspected = args.handler(args, store)
    assert code == 0 and inspected["data"]["asset"] == descriptor


def test_known_terms_require_license_and_source_ancestors_are_not_symlinks(
    pinned: tuple[LabStore, Path, bytes],
) -> None:
    store, source, _ = pinned
    with store.writer("asset add", "r-20261016-1200-0000", step="pmd"):
        with pytest.raises(assets.AssetError, match="descriptor"):
            assets.add_asset(
                store, "pmd", source, source_label="synthetic", terms="known",
            )
    with store.writer("asset add", "r-20261016-1200-0001", step="pmd"):
        with pytest.raises(assets.AssetError, match="not a path"):
            assets.add_asset(
                store, "pmd", source, source_label=str(source), terms="unknown",
            )
    linked_parent = source.parent / "linked-parent"
    linked_parent.symlink_to(source.parent, target_is_directory=True)
    with pytest.raises(assets.AssetError, match="symlink"):
        _add(store, linked_parent / source.name)


def test_interrupted_legacy_manifest_keeps_pinned_bytes_usable(
    pinned: tuple[LabStore, Path, bytes], monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, source, data = pinned
    directory = store.lab / "assets" / "pmd"
    directory.mkdir(parents=True)
    (directory / assets.PMD_FILENAME).write_bytes(data)

    def interrupted(*args: object, **kwargs: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(assets.os, "link", interrupted)
    with pytest.raises(KeyboardInterrupt):
        _add(store, source)
    record = assets.inspect_asset(store)
    assert record.legacy and record.file.read_bytes() == data
    assert not (directory / "manifest.json").exists()
    assert not list(directory.glob(".staging-*"))
