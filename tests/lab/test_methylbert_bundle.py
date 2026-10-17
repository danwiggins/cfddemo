"""A model bundle is checked before a trusted pickle can enter private scratch."""

from __future__ import annotations

import hashlib
import io
import json
import os
import sys
import tarfile
from pathlib import Path

import pytest

import traceback_lab.methylbert_bundle as bundle_module
import traceback_runner.references as references
from traceback_lab.methylbert_bundle import MAX_ARCHIVE_BYTES, BundleError, REQUIRED_FILES, SCHEMA, unpack_bundle, verify_bundle
from traceback_runner.references import AssetKind, ReferenceProblem, copy_registered_asset, register_asset


def _bundle(path, *, bad_digest=False, extra=False, link=False, model_id="test-tumour-normal"):
    files = {name: f"fixture {name}".encode() for name in REQUIRED_FILES}
    manifest = {
        "schema": SCHEMA, "model_id": model_id, "upstream_commit": "a" * 40,
        "source": "synthetic fixture", "terms": "test-only", "reference_sha256": "b" * 64,
        "n_mers": 3, "seq_len": 150,
        "files": {name: hashlib.sha256(body).hexdigest() for name, body in files.items()},
    }
    if bad_digest:
        manifest["files"]["train_seq.csv"] = "0" * 64
    files["bundle.json"] = json.dumps(manifest).encode()
    if extra:
        files["../escape"] = b"not allowed"
    with tarfile.open(path, mode="w:") as archive:
        for name, body in files.items():
            member = tarfile.TarInfo(name)
            if link and name == "train_seq.csv":
                member.type = tarfile.SYMTYPE
                member.linkname = "../escape"
            else:
                member.size = len(body)
            archive.addfile(member, None if member.issym() else io.BytesIO(body))
    return files


def test_bundle_verifies_all_files_before_private_copy(tmp_path):
    archive = tmp_path / "model.tar"
    files = _bundle(archive)
    bundle = verify_bundle(archive)
    assert bundle.model_id == "test-tumour-normal"
    destination = tmp_path / "private"
    assert unpack_bundle(archive, destination) == bundle
    for name in REQUIRED_FILES:
        assert (destination / name).read_bytes() == files[name]
    assert not (destination / "bundle.json").exists()
    with pytest.raises(BundleError, match="must not exist"):
        unpack_bundle(archive, destination)


def test_bundle_registers_by_digest_then_reverifies_worker_copy(tmp_path):
    archive = tmp_path / "model.tar"
    _bundle(archive)
    root = tmp_path / "root"
    result = register_asset(root, AssetKind.METHYLBERT_BUNDLE, "asset_methylbert_test", archive)
    assert result.registered.parse_check.format == "methylbert-bundle-tar"
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    copy = copy_registered_asset(
        root, "asset_methylbert_test", scratch, kind=AssetKind.METHYLBERT_BUNDLE,
    )
    assert unpack_bundle(copy, scratch / "model").model_id == "test-tumour-normal"


def test_registration_refuses_malformed_bundle_before_publication(tmp_path):
    archive = tmp_path / "model.tar"
    _bundle(archive, bad_digest=True)
    root = tmp_path / "root"
    with pytest.raises(ReferenceProblem) as raised:
        register_asset(root, AssetKind.METHYLBERT_BUNDLE, "asset_bad_model", archive)
    assert raised.value.code == "TBX-ASSET-005"
    assert not (root / "assets-local" / AssetKind.METHYLBERT_BUNDLE.value / "asset_bad_model").exists()


def test_registration_and_consumer_share_the_bundle_size_cap():
    assert references._MAX_BUNDLE_BYTES == MAX_ARCHIVE_BYTES


@pytest.mark.parametrize("damage,message", [
    ("bad_digest", "differs"), ("extra", "missing, extra"), ("link", "regular files"),
])
def test_bundle_rejects_bad_hash_membership_and_links(tmp_path, damage, message):
    archive = tmp_path / "model.tar"
    _bundle(archive, **{damage: True})
    with pytest.raises(BundleError, match=message):
        verify_bundle(archive)


def test_many_headers_are_rejected_before_unbounded_tar_parsing(tmp_path, monkeypatch):
    archive = tmp_path / "many.tar"
    with tarfile.open(archive, mode="w:") as output:
        for name in sorted(REQUIRED_FILES | {"bundle.json"}):
            member = tarfile.TarInfo(name)
            member.size = 1
            output.addfile(member, io.BytesIO(b"x"))
        for number in range(2000):
            body = b"x"
            member = tarfile.TarInfo(f"extra-{number}")
            member.size = len(body)
            output.addfile(member, io.BytesIO(body))
    parsed = 0
    original = tarfile.TarInfo.frombuf

    def counted(*args, **kwargs):
        nonlocal parsed
        parsed += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(tarfile.TarInfo, "frombuf", counted)
    monkeypatch.setattr(bundle_module.tarfile, "open", lambda *args, **kwargs: pytest.fail(
        "tarfile must not parse beyond the raw header bound",
    ))
    with pytest.raises(BundleError, match="missing, extra"):
        verify_bundle(archive)
    assert parsed == 8


def test_pax_extension_is_rejected_before_tarfile_parsing(tmp_path, monkeypatch):
    archive = tmp_path / "pax.tar"
    with tarfile.open(archive, mode="w:", format=tarfile.PAX_FORMAT) as output:
        member = tarfile.TarInfo("bundle.json")
        member.pax_headers = {"comment": "extended metadata"}
        member.size = 1
        output.addfile(member, io.BytesIO(b"x"))
    monkeypatch.setattr(bundle_module.tarfile, "open", lambda *args, **kwargs: pytest.fail(
        "PAX data must be rejected by the raw scanner",
    ))
    with pytest.raises(BundleError):
        verify_bundle(archive)


@pytest.mark.skipif(sys.platform == "win32", reason="Windows does not permit replacing an open TAR")
def test_private_copy_uses_verified_open_archive_when_path_is_replaced(tmp_path, monkeypatch):
    archive = tmp_path / "model.tar"
    replacement = tmp_path / "replacement.tar"
    files = _bundle(archive)
    _bundle(replacement, model_id="replacement-model")
    original = bundle_module._verified_members

    def swap_after_verification(open_archive):
        result = original(open_archive)
        os.replace(replacement, archive)
        return result

    monkeypatch.setattr(bundle_module, "_verified_members", swap_after_verification)
    destination = tmp_path / "private"
    assert unpack_bundle(archive, destination).model_id == "test-tumour-normal"
    assert (destination / "train_seq.csv").read_bytes() == files["train_seq.csv"]
    monkeypatch.setattr(bundle_module, "_verified_members", original)
    assert verify_bundle(archive).model_id == "replacement-model"


def test_private_copy_failure_removes_partial_destination(tmp_path, monkeypatch):
    archive = tmp_path / "model.tar"
    _bundle(archive)
    destination = tmp_path / "private"
    original = bundle_module._sha256

    def fail_after_copy(stream, size):
        if getattr(stream, "name", None) and destination in Path(stream.name).parents:
            raise OSError("injected private copy failure")
        return original(stream, size)

    monkeypatch.setattr(bundle_module, "_sha256", fail_after_copy)
    with pytest.raises(BundleError, match="private copy failed"):
        unpack_bundle(archive, destination)
    assert not destination.exists()
    monkeypatch.setattr(bundle_module, "_sha256", original)
    assert unpack_bundle(archive, destination).model_id == "test-tumour-normal"
