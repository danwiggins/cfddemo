"""Offline adversarial tests for development report bundles."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from traceback_runner.report_bundles import (
    ACCESSIBLE_TABLE_PATH,
    MANIFEST_PATH,
    METHOD_ID,
    PLOT_DATA_PATH,
    PLOT_DATA_SCHEMA,
    PLOT_SPEC_PATH,
    PLOT_SPEC_SCHEMA,
    PROVENANCE_PATH,
    PROVENANCE_SCHEMA,
    QUALIFICATION_STATUS,
    RESULT_PATH,
    RESULT_SCHEMA,
    ReportBundleFilesystemError,
    ReportBundleFormatError,
    ReportBundleIntegrityError,
    build_development_report_bundle,
    replay_development_report_bundle,
    verify_development_report_bundle,
)
from traceback_runner.serialization import canonical_json_bytes, sha256_bytes


def _json_artifact(schema_version: str, *, result: bool = False) -> bytes:
    identity = {
        "method_id": METHOD_ID,
        "qualification_status": QUALIFICATION_STATUS,
    }
    value: dict[str, object] = {
        "schema_version": schema_version,
        "values": [1, 2, 3],
    }
    if result:
        value["identity"] = identity
        value["analysis_status"] = "complete"
    else:
        value.update(identity)
    return canonical_json_bytes(value)


def _inputs() -> dict[str, bytes]:
    return {
        "result_bytes": _json_artifact(RESULT_SCHEMA, result=True),
        "plot_data_bytes": _json_artifact(PLOT_DATA_SCHEMA),
        "plot_spec_bytes": _json_artifact(PLOT_SPEC_SCHEMA),
        "provenance_bytes": _json_artifact(PROVENANCE_SCHEMA),
        "accessible_table_bytes": b"chromosome\trelative_dosage\nchr1\t2.0\n",
    }


def _build(tmp_path: Path, name: str = "report") -> Path:
    return build_development_report_bundle(tmp_path / name, **_inputs())


def test_build_is_byte_deterministic_and_manifest_binds_every_artifact(
    tmp_path: Path,
) -> None:
    first = _build(tmp_path, "first")
    second = _build(tmp_path, "second")

    first_files = {item.name: item.read_bytes() for item in first.iterdir()}
    second_files = {item.name: item.read_bytes() for item in second.iterdir()}
    assert first_files == second_files

    verified = verify_development_report_bundle(first)
    replayed = replay_development_report_bundle(first)
    assert replayed == verified
    assert verified.manifest.method_id == METHOD_ID
    assert verified.manifest.qualification_status == QUALIFICATION_STATUS
    assert verified.manifest.product_release_authorized is False
    assert tuple(item.relative_path for item in verified.manifest.artifacts) == tuple(
        sorted(
            (
                ACCESSIBLE_TABLE_PATH,
                PLOT_DATA_PATH,
                PLOT_SPEC_PATH,
                PROVENANCE_PATH,
                RESULT_PATH,
            )
        )
    )
    digests = {
        item.relative_path: item.sha256 for item in verified.manifest.artifacts
    }
    assert verified.manifest.bindings.result_sha256 == digests[RESULT_PATH]
    assert verified.manifest.bindings.plot_data_sha256 == digests[PLOT_DATA_PATH]
    assert verified.manifest.bindings.plot_spec_sha256 == digests[PLOT_SPEC_PATH]
    assert verified.manifest.bindings.provenance_sha256 == digests[PROVENANCE_PATH]
    assert (
        verified.manifest.bindings.accessible_table_sha256
        == digests[ACCESSIBLE_TABLE_PATH]
    )
    for artifact in verified.manifest.artifacts:
        content = (first / artifact.relative_path).read_bytes()
        assert artifact.size_bytes == len(content)
        assert artifact.sha256 == sha256_bytes(content)


@pytest.mark.parametrize(
    ("argument", "schema", "result"),
    (
        ("result_bytes", RESULT_SCHEMA, True),
        ("plot_data_bytes", PLOT_DATA_SCHEMA, False),
        ("plot_spec_bytes", PLOT_SPEC_SCHEMA, False),
        ("provenance_bytes", PROVENANCE_SCHEMA, False),
    ),
)
@pytest.mark.parametrize(
    ("field", "value", "message"),
    (
        ("schema_version", "unknown.v9", "unknown schema"),
        ("method_id", "other-method", "wrong method"),
        ("qualification_status", "qualified", "development-unqualified"),
    ),
)
def test_builder_rejects_unknown_or_wrong_artifact_identity(
    tmp_path: Path,
    argument: str,
    schema: str,
    result: bool,
    field: str,
    value: str,
    message: str,
) -> None:
    inputs = _inputs()
    parsed = json.loads(_json_artifact(schema, result=result))
    target = parsed["identity"] if result and field != "schema_version" else parsed
    target[field] = value
    inputs[argument] = canonical_json_bytes(parsed)

    with pytest.raises(ReportBundleFormatError, match=message):
        build_development_report_bundle(tmp_path / "rejected", **inputs)


@pytest.mark.parametrize(
    "argument",
    ("result_bytes", "plot_data_bytes", "plot_spec_bytes", "provenance_bytes"),
)
def test_builder_and_replay_reject_noncanonical_json(
    tmp_path: Path, argument: str
) -> None:
    inputs = _inputs()
    inputs[argument] = json.dumps(json.loads(inputs[argument]), indent=2).encode()
    with pytest.raises(ReportBundleFormatError, match="canonical JSON"):
        build_development_report_bundle(tmp_path / "noncanonical", **inputs)

    bundle = _build(tmp_path, "tampered")
    relative = {
        "result_bytes": RESULT_PATH,
        "plot_data_bytes": PLOT_DATA_PATH,
        "plot_spec_bytes": PLOT_SPEC_PATH,
        "provenance_bytes": PROVENANCE_PATH,
    }[argument]
    path = bundle / relative
    path.write_bytes(json.dumps(json.loads(path.read_bytes()), indent=2).encode())
    with pytest.raises(ReportBundleFormatError, match="canonical JSON"):
        replay_development_report_bundle(bundle)


def test_publication_is_atomic_and_never_overwrites_destination(tmp_path: Path) -> None:
    destination = tmp_path / "existing"
    destination.mkdir()
    sentinel = destination / "keep.txt"
    sentinel.write_text("keep")

    with pytest.raises(ReportBundleFilesystemError, match="already exists"):
        build_development_report_bundle(destination, **_inputs())
    assert sentinel.read_text() == "keep"
    assert not list(tmp_path.glob(".existing.*"))


@pytest.mark.parametrize("relative", ("extra.json", "nested"))
def test_replay_rejects_extra_files_and_directories(
    tmp_path: Path, relative: str
) -> None:
    bundle = _build(tmp_path)
    path = bundle / relative
    if relative == "nested":
        path.mkdir()
    else:
        path.write_text("extra")
    with pytest.raises(ReportBundleFilesystemError, match="unexpected|non-file"):
        replay_development_report_bundle(bundle)


def test_replay_rejects_missing_files_and_symlinks(tmp_path: Path) -> None:
    missing = _build(tmp_path, "missing")
    (missing / PLOT_SPEC_PATH).unlink()
    with pytest.raises(ReportBundleFilesystemError, match="missing"):
        verify_development_report_bundle(missing)

    linked = _build(tmp_path, "linked")
    target = linked / PLOT_DATA_PATH
    target.unlink()
    target.symlink_to(linked / RESULT_PATH)
    with pytest.raises(ReportBundleFilesystemError, match="symlink"):
        verify_development_report_bundle(linked)


def test_replay_rejects_digest_size_and_noncanonical_manifest(
    tmp_path: Path,
) -> None:
    digest_mismatch = _build(tmp_path, "digest")
    table = digest_mismatch / ACCESSIBLE_TABLE_PATH
    table.write_bytes(table.read_bytes().replace(b"2.0", b"2.1"))
    with pytest.raises(ReportBundleIntegrityError, match="digest mismatch"):
        replay_development_report_bundle(digest_mismatch)

    size_mismatch = _build(tmp_path, "size")
    manifest_path = size_mismatch / MANIFEST_PATH
    manifest = json.loads(manifest_path.read_bytes())
    manifest["artifacts"][0]["size_bytes"] += 1
    manifest_path.write_bytes(canonical_json_bytes(manifest))
    with pytest.raises(ReportBundleIntegrityError, match="size mismatch"):
        replay_development_report_bundle(size_mismatch)

    noncanonical = _build(tmp_path, "noncanonical-manifest")
    manifest_path = noncanonical / MANIFEST_PATH
    manifest_path.write_bytes(
        json.dumps(json.loads(manifest_path.read_bytes()), indent=2).encode()
    )
    with pytest.raises(ReportBundleFormatError, match="noncanonical"):
        replay_development_report_bundle(noncanonical)


@pytest.mark.parametrize("content", (b"", b"opaque\x00bytes", b"\xff\n"))
def test_already_validated_accessible_table_bytes_are_preserved(
    tmp_path: Path, content: bytes
) -> None:
    inputs = _inputs()
    inputs["accessible_table_bytes"] = content
    bundle = build_development_report_bundle(tmp_path / "opaque-table", **inputs)
    verified = verify_development_report_bundle(bundle)
    assert verified.accessible_table_bytes == content
