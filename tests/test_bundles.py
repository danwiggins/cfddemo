"""Adversarial tests for canonical aggregate result bundles."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from traceback_runner.bundles import (
    BundleFilesystemError,
    BundleFormatError,
    BundleIntegrityError,
    build_result_bundle,
    inspect_bundle,
    verify_bundle,
)
from traceback_runner.export import ExportBoundaryError, validate_measurement
from traceback_runner.fixtures import synthetic_fragment_policy
from traceback_runner.signing import (
    InvalidSignatureError,
    KeyPurpose,
    RevokedKeyError,
    TrustStore,
    UnknownKeyError,
    generate_development_keypair,
)


def _measurement(**updates: object) -> dict[str, object]:
    bins = synthetic_fragment_policy().bins
    value: dict[str, object] = {
        "schema_version": "traceback.fragment-measurement.v1",
        "definition_id": "aligned-reference-span.v1",
        "approval_state": "unapproved_synthetic",
        "reference_id": "synthetic-reference.v1",
        "completion": "complete",
        "records_scanned": 4,
        "eligible_alignments": 3,
        "exclusions": {
            "unmapped": 1,
            "secondary": 0,
            "supplementary": 0,
            "qc_failure": 0,
            "duplicate": 0,
            "low_mapping_quality": 0,
            "unregistered_contig": 0,
            "no_reference_span": 0,
        },
        "histogram": [
            {
                "bin": item.model_dump(mode="json"),
                "count": 1 if index == 0 else 2 if index == 1 else 0,
            }
            for index, item in enumerate(bins)
        ],
    }
    value.update(updates)
    return value


def _provenance(**updates: object) -> dict[str, object]:
    value: dict[str, object] = {
        "schema_version": "traceback.run-provenance.v1",
        "run_token": "synthetic.run.v1",
        "input_kind": "modbam",
        "protocol_run_token": "synthetic.protocol.v1",
        "workflow_release_id": "synthetic-workflow.v1",
        "artifacts": [
            {
                "role": "analysis_input",
                "size_bytes": 100,
                "provider_hmac_sha256": "a" * 64,
            }
        ],
    }
    value.update(updates)
    return value


def _bundle(tmp_path: Path):
    key = generate_development_keypair(KeyPurpose.RESULT)
    store = TrustStore()
    store.add_signing_key(key)
    path = build_result_bundle(
        tmp_path / "record",
        measurement=_measurement(),
        provenance=_provenance(),
        signing_key=key,
    )
    return path, key, store


def test_build_and_verify_canonical_bundle_without_embedded_trust_root(tmp_path: Path) -> None:
    path, key, store = _bundle(tmp_path)

    verified = verify_bundle(path, store)

    assert verified.measurement.eligible_alignments == 3
    assert verified.signature.key_id == key.key_id
    assert "public" not in (path / "bundle.sig").read_text()
    assert "private" not in "".join(item.name for item in path.rglob("*"))
    assert inspect_bundle(path) == verified.manifest


def test_measurement_and_chart_bytes_are_deterministic_and_signature_separate(
    tmp_path: Path,
) -> None:
    first_key = generate_development_keypair(KeyPurpose.RESULT)
    second_key = generate_development_keypair(KeyPurpose.RESULT)
    first = build_result_bundle(
        tmp_path / "first",
        measurement=_measurement(),
        provenance=_provenance(),
        signing_key=first_key,
    )
    second = build_result_bundle(
        tmp_path / "second",
        measurement=_measurement(),
        provenance=_provenance(),
        signing_key=second_key,
    )

    assert (first / "measurements/fragment-length.v1.json").read_bytes() == (
        second / "measurements/fragment-length.v1.json"
    ).read_bytes()
    assert (first / "charts/fragment-length.v1.json").read_bytes() == (
        second / "charts/fragment-length.v1.json"
    ).read_bytes()
    assert (first / "bundle.sig").read_bytes() != (second / "bundle.sig").read_bytes()


def test_verifier_checks_actual_content_not_declared_digest(tmp_path: Path) -> None:
    path, _, store = _bundle(tmp_path)
    measurement = path / "measurements/fragment-length.v1.json"
    measurement.write_bytes(measurement.read_bytes().replace(b'"count":2', b'"count":1'))

    with pytest.raises(BundleIntegrityError, match="checksum mismatch"):
        verify_bundle(path, store)


def test_wrong_missing_and_revoked_trust_fail(tmp_path: Path) -> None:
    path, key, store = _bundle(tmp_path)
    with pytest.raises(UnknownKeyError):
        verify_bundle(path, TrustStore())

    wrong_store = TrustStore()
    wrong_store.add_signing_key(generate_development_keypair(KeyPurpose.RESULT))
    with pytest.raises(UnknownKeyError):
        verify_bundle(path, wrong_store)

    store.revoke(key.key_id)
    with pytest.raises(RevokedKeyError):
        verify_bundle(path, store)


def test_bundle_rejects_wrong_purpose_key_and_tampered_signature(tmp_path: Path) -> None:
    release_key = generate_development_keypair(KeyPurpose.RELEASE)
    with pytest.raises(BundleFormatError, match="result-purpose"):
        build_result_bundle(
            tmp_path / "wrong-purpose",
            measurement=_measurement(),
            provenance=_provenance(),
            signing_key=release_key,
        )

    path, _, store = _bundle(tmp_path / "tampered")
    signature_path = path / "bundle.sig"
    signature = json.loads(signature_path.read_text())
    signature["signature_base64"] = "eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eA=="
    signature_path.write_text(json.dumps(signature, sort_keys=True, separators=(",", ":")))
    with pytest.raises(InvalidSignatureError):
        verify_bundle(path, store)


@pytest.mark.parametrize("relative", ["unexpected.txt", "measurements/extra.json"])
def test_extra_files_are_rejected(tmp_path: Path, relative: str) -> None:
    path, _, store = _bundle(tmp_path)
    extra = path / relative
    extra.parent.mkdir(exist_ok=True)
    extra.write_text("extra")
    with pytest.raises(BundleFilesystemError, match="unexpected file"):
        verify_bundle(path, store)


def test_missing_files_and_symlinks_are_rejected(tmp_path: Path) -> None:
    path, _, store = _bundle(tmp_path)
    (path / "report.html").unlink()
    with pytest.raises(BundleFilesystemError, match="missing"):
        verify_bundle(path, store)

    path2, _, store2 = _bundle(tmp_path / "other")
    target = path2 / "provenance.json"
    target.unlink()
    target.symlink_to(path2 / "limitations.json")
    with pytest.raises(BundleFilesystemError, match="symlink"):
        verify_bundle(path2, store2)


def test_traversal_inventory_and_unsupported_schema_are_rejected(tmp_path: Path) -> None:
    path, _, store = _bundle(tmp_path)
    checksums = path / "checksums.sha256"
    checksums.write_text(checksums.read_text().replace("provenance.json", "../provenance.json"))
    with pytest.raises(BundleFormatError, match="unknown or duplicate path"):
        verify_bundle(path, store)

    path2, _, store2 = _bundle(tmp_path / "other")
    manifest_path = path2 / "bundle-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["schema_version"] = "traceback.result-bundle.v2"
    manifest_path.write_text(json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n")
    with pytest.raises(BundleFormatError, match="manifest"):
        verify_bundle(path2, store2)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("read_id", "read-123"),
        ("sequence", "ACGTACGTACGTACGTACGTACGT"),
        ("path", "/private/provider/sample.bam"),
        ("sample_label", "Jane Doe"),
        ("secret", "AWS_SECRET_ACCESS_KEY=example"),
        ("raw_hash", "b" * 64),
        ("unknown", "surprise"),
    ],
)
def test_unknown_sensitive_fields_never_cross_allowlist(field: str, value: str) -> None:
    with pytest.raises(ExportBoundaryError):
        validate_measurement(_measurement(**{field: value}))


@pytest.mark.parametrize(
    "field_value",
    [
        "/private/provider/sample.bam",
        "ACGTACGTACGTACGTACGTACGT",
        "normal-result",
        "cancer-screening",
        "<script>alert(1)</script>",
        "b" * 64,
    ],
)
def test_sensitive_or_claim_values_in_allowlisted_identifier_are_rejected(
    field_value: str,
) -> None:
    with pytest.raises(ExportBoundaryError):
        validate_measurement(_measurement(reference_id=field_value))


def test_incomplete_zero_or_unreconciled_measurements_cannot_publish() -> None:
    for update in (
        {"completion": "interrupted"},
        {"eligible_alignments": 0},
        {"records_scanned": 5},
        {"histogram": [{"bin": {"lower_inclusive": 0, "upper_exclusive": None}, "count": 2}]},
    ):
        with pytest.raises(ExportBoundaryError):
            validate_measurement(_measurement(**update))
