"""A model bundle is checked before a trusted pickle can enter private scratch."""

from __future__ import annotations

import hashlib
import io
import json
import tarfile

import pytest

from traceback_lab.methylbert_bundle import BundleError, REQUIRED_FILES, SCHEMA, unpack_bundle, verify_bundle
from traceback_runner.references import AssetKind, copy_registered_asset, register_asset


def _bundle(path, *, bad_digest=False, extra=False, link=False):
    files = {name: f"fixture {name}".encode() for name in REQUIRED_FILES}
    manifest = {
        "schema": SCHEMA, "model_id": "test-tumour-normal", "upstream_commit": "a" * 40,
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


@pytest.mark.parametrize("damage,message", [
    ("bad_digest", "differs"), ("extra", "missing, extra"), ("link", "regular files"),
])
def test_bundle_rejects_bad_hash_membership_and_links(tmp_path, damage, message):
    archive = tmp_path / "model.tar"
    _bundle(archive, **{damage: True})
    with pytest.raises(BundleError, match=message):
        verify_bundle(archive)
