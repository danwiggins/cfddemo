"""Pinned local Lab assets: recovery, integrity, and publication boundaries."""

from __future__ import annotations

import hashlib
import errno
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

def test_oversized_manifest_is_rejected_before_parse(
    pinned: tuple[LabStore, Path, bytes],
) -> None:
    store, source, _ = pinned
    record = _add(store, source)
    (record.file.parent / "manifest.json").write_bytes(b"x" * 70_000)
    with pytest.raises(assets.AssetError, match="64 KiB"):
        assets.inspect_asset(store)


def test_legacy_publish_os_error_is_structured_and_keeps_bytes(
    pinned: tuple[LabStore, Path, bytes], monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, source, data = pinned
    directory = store.lab / "assets" / "pmd"
    directory.mkdir(parents=True)
    (directory / assets.PMD_FILENAME).write_bytes(data)

    def denied(*args: object, **kwargs: object) -> None:
        raise OSError("read-only")

    monkeypatch.setattr(assets.os, "link", denied)
    with pytest.raises(assets.AssetError, match="publish PMD metadata"):
        _add(store, source)
    assert assets.inspect_asset(store).legacy
    assert (directory / assets.PMD_FILENAME).read_bytes() == data

def test_linked_manifest_fsync_failure_reports_uncertainty(
    pinned: tuple[LabStore, Path, bytes], monkeypatch: pytest.MonkeyPatch,
) -> None:
    import argparse
    import shlex

    from traceback_lab import asset_command

    store, source, data = pinned
    directory = store.lab / "assets" / "pmd"
    directory.mkdir(parents=True)
    (directory / assets.PMD_FILENAME).write_bytes(data)

    def sync_fails(path: Path) -> None:
        assert path == directory
        raise OSError("disk sync failed")

    monkeypatch.setattr(assets, "_fsync_asset_directory", sync_fails)
    args = argparse.Namespace(
        kind="pmd", file=source, source_label="synthetic-Zhou-file",
        terms="unknown", license_id=None,
    )
    code, payload = asset_command._add(args, store)
    assert code == 3
    assert "incomplete" in payload["summary"].lower()
    assert "durability is unconfirmed" in payload["data"]["cause"]
    assert shlex.split(payload["data"]["fix"])[-1] == str(store.root)
    assert assets.inspect_asset(store).file.read_bytes() == data
    assert (directory / "manifest.json").is_file()


def test_copied_size_must_match_descriptor_before_publication(
    pinned: tuple[LabStore, Path, bytes], monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, source, _ = pinned
    real = assets._descriptor

    def wrong_size(
        size_bytes: int, source_label: str, terms: str,
        license_id: str | None, *, sha256: str | None = None,
    ) -> assets.AssetDescriptor:
        return real(size_bytes + 1, source_label, terms, license_id, sha256=sha256)

    monkeypatch.setattr(assets, "_descriptor", wrong_size)
    with pytest.raises(assets.AssetError, match="changed while being copied"):
        _add(store, source)
    assert not (store.lab / "assets" / "pmd").exists()


def test_asset_command_is_reachable_through_signed_cli(
    pinned: tuple[LabStore, Path, bytes], capsys: pytest.CaptureFixture[str],
) -> None:
    import json

    from traceback_runner import cli as signed_cli

    store, source, _ = pinned
    code = signed_cli.main([
        "lab", "asset", "add", "pmd", "--file", str(source),
        "--source-label", "synthetic-Zhou-file", "--terms", "unknown",
        "--root", str(store.root), "--json",
    ])
    added = json.loads(capsys.readouterr().out)
    assert code == 0
    assert added["command"] == "lab asset add"
    assert added["data"]["asset"]["kind"] == "pmd"

    code = signed_cli.main([
        "lab", "asset", "inspect", "pmd", "--root", str(store.root), "--json",
    ])
    inspected = json.loads(capsys.readouterr().out)
    assert code == 0
    assert inspected["command"] == "lab asset inspect"
    assert inspected["data"]["asset"] == added["data"]["asset"]


def test_legacy_manifest_size_uses_retained_file(
    pinned: tuple[LabStore, Path, bytes], monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    store, source, data = pinned
    directory = store.lab / "assets" / "pmd"
    directory.mkdir(parents=True)
    (directory / assets.PMD_FILENAME).write_bytes(data)
    real_stat = Path.stat

    def changed_source_stat(path: Path, *args: object, **kwargs: object):
        if path == source and kwargs.get("follow_symlinks", True):
            return SimpleNamespace(st_size=len(data) + 1)
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", changed_source_stat)
    added = _add(store, source)
    inspected = assets.inspect_asset(store)
    assert added.descriptor.size_bytes == len(data)
    assert inspected.descriptor == added.descriptor


def test_replaced_asset_between_check_and_open_is_not_followed(
    pinned: tuple[LabStore, Path, bytes], monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, source, _ = pinned
    record = _add(store, source)
    real_open = assets.os.open

    def replace_before_open(path: Path, flags: int, *args: object, **kwargs: object) -> int:
        if Path(path).name == record.file.name:
            record.file.unlink()
            record.file.symlink_to(source)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(assets.os, "open", replace_before_open)
    with pytest.raises(assets.AssetError, match="unreadable or changed"):
        assets.inspect_asset(store)


def test_replaced_asset_directory_is_not_followed(
    pinned: tuple[LabStore, Path, bytes], monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, source, _ = pinned
    record = _add(store, source)
    directory = record.file.parent
    moved = directory.parent / "old-pmd"
    real_open = assets.os.open

    def replace_before_directory_open(path: Path, flags: int, *args: object, **kwargs: object) -> int:
        if path == "pmd" and kwargs.get("dir_fd") is not None:
            directory.rename(moved)
            directory.symlink_to(moved, target_is_directory=True)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(assets.os, "open", replace_before_directory_open)
    with pytest.raises(assets.AssetError, match="unreadable or changed"):
        assets.inspect_asset(store)


def test_mutation_during_hash_cannot_verify_old_bytes(
    pinned: tuple[LabStore, Path, bytes], monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, source, _ = pinned
    record = _add(store, source)
    real_sha256 = hashlib.sha256

    class MutatingDigest:
        def __init__(self) -> None:
            self._digest = real_sha256()

        def update(self, block: bytes) -> None:
            self._digest.update(block)
            record.file.write_bytes(b"changed asset bytes")

        def hexdigest(self) -> str:
            return self._digest.hexdigest()

    monkeypatch.setattr(assets.hashlib, "sha256", MutatingDigest)
    with pytest.raises(assets.AssetError, match="changed while hashing"):
        assets.inspect_asset(store)


def test_source_replaced_after_initial_hash_cannot_be_copied(
    pinned: tuple[LabStore, Path, bytes], monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, source, _ = pinned
    retained = source.with_name("retained-pmd.gz")
    real_digest = assets._digest

    def replace_after_hash(path: Path) -> tuple[str, int]:
        result = real_digest(path)
        source.rename(retained)
        source.symlink_to(retained)
        return result

    monkeypatch.setattr(assets, "_digest", replace_after_hash)
    with pytest.raises(assets.AssetError, match="changed while being copied"):
        _add(store, source)
    assert not (store.lab / "assets" / "pmd").exists()
    assert retained.is_file()


def test_manifest_replaced_during_read_is_not_treated_as_verified(
    pinned: tuple[LabStore, Path, bytes], monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, source, _ = pinned
    manifest = _add(store, source).file.parent / "manifest.json"
    retained = manifest.with_name("retained-manifest.json")
    real_open = assets._open_regular

    def replace_after_open(path: Path) -> int:
        descriptor = real_open(path)
        if path == manifest:
            manifest.rename(retained)
            manifest.write_text("{}")
        return descriptor

    monkeypatch.setattr(assets, "_open_regular", replace_after_open)
    with pytest.raises(assets.AssetError, match="manifest changed while being read"):
        assets.inspect_asset(store)
    assert retained.is_file()


def test_verified_reader_rejects_mutation_and_replacement_during_use(
    pinned: tuple[LabStore, Path, bytes],
) -> None:
    store, source, data = pinned
    record = _add(store, source)
    with assets.open_verified_asset(store, record) as stream:
        assert stream.read() == data

    with pytest.raises(assets.AssetError, match="changed during consumption"):
        with assets.open_verified_asset(store, record) as stream:
            assert stream.read(1) == data[:1]
            record.file.write_bytes(b"changed but still present")

    record.file.write_bytes(data)
    replaced = record.file.with_name("original-pmd.gz")
    with pytest.raises(assets.AssetError, match="changed during consumption"):
        with assets.open_verified_asset(store, record) as stream:
            record.file.rename(replaced)
            record.file.write_bytes(data)
            assert stream.read() == data


def test_file_in_place_of_asset_directory_is_reported_without_replacement(
    pinned: tuple[LabStore, Path, bytes],
) -> None:
    store, source, _ = pinned
    directory = store.lab / "assets" / "pmd"
    directory.parent.mkdir(parents=True)
    directory.write_bytes(b"operator data")
    with pytest.raises(assets.AssetError, match="directory is not a directory"):
        assets.inspect_asset(store)
    with pytest.raises(assets.AssetError, match="directory is not a directory"):
        _add(store, source)
    assert directory.read_bytes() == b"operator data"


@pytest.mark.parametrize("phase", ["open", "write"])
def test_destination_write_failure_names_storage_and_publishes_nothing(
    pinned: tuple[LabStore, Path, bytes], monkeypatch: pytest.MonkeyPatch, phase: str,
) -> None:
    store, source, data = pinned
    real_open = Path.open

    class FailedWriter:
        def __init__(self, writer: object) -> None:
            self.writer = writer

        def __enter__(self) -> FailedWriter:
            return self

        def __exit__(self, *args: object) -> None:
            self.writer.close()

        def write(self, block: bytes) -> None:
            raise OSError(errno.EIO, "I/O error")

    def no_space(path: Path, mode: str = "r", *args: object, **kwargs: object):
        if path.name == assets.PMD_FILENAME and path.parent.name.startswith(".staging-") and mode == "xb":
            if phase == "open":
                raise OSError(errno.ENOSPC, "No space left on device")
            return FailedWriter(real_open(path, mode, *args, **kwargs))
        return real_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", no_space)
    with pytest.raises(assets.AssetError, match="could not write staged PMD asset; check free space"):
        _add(store, source)
    parent = store.lab / "assets"
    assert not (parent / "pmd").exists()
    assert not list(parent.glob(".staging-*"))
    assert source.read_bytes() == data
