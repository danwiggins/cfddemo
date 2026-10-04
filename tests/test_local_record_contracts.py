"""Local (``unapproved_local``) records: measurement v2, bundle v3, local trust.

Golden-path items B3a (measurement v2 contracts, result-bundle v3, the v3
catalog reader, the local report) and B3b (the ``development-local`` trust
namespace and the v2 result-trust registry).  The frozen fixtures under
``tests/fixtures/`` were committed before this change and must keep
verifying byte-identically.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import shutil
from pathlib import Path

import pytest

from evidence_inspector.result_catalog import (
    DEFAULT_RESULT_BUNDLE_READER_REGISTRY,
    LOCAL_RESULT_BUNDLE_READER_REGISTRY,
    CatalogConflict,
    CatalogError,
    CatalogUnsupportedSchema,
    ResultCatalog,
    registry_trust_snapshot_sha256,
)
from evidence_inspector.result_trust_registry import (
    ResultTrustEventReceiptV2,
    ResultTrustRegistry,
    ResultTrustRegistryConflict,
    ResultTrustRegistryUnsafe,
    ResultTrustSnapshot,
    ResultTrustSnapshotV2,
    result_trust_backup_from_bytes,
)
from tests.test_bundles import _METHOD, _measurement, _provenance
from tests.test_result_catalog import ALIASES, _authority, _bundle_method
from traceback_runner.bundles import (
    CHECKSUMS_PATH,
    LIMITATIONS_PATH,
    MANIFEST_PATH,
    MEASUREMENT_PATH,
    REPORT_PATH,
    SIGNATURE_PATH,
    BundleError,
    BundleFormatError,
    VerifiedBundle,
    _checksums_bytes,
    _CHECKSUM_PATHS,
    _signing_payload,
    build_result_bundle,
    verify_bundle,
)
from traceback_runner.cli import ExitCode, main
from traceback_runner.contracts import (
    ApprovalState,
    FragmentMeasurement,
    FragmentMeasurementPolicy,
    FragmentMeasurementPolicyV2,
    FragmentMeasurementV2,
    canonical_json_bytes,
    parse_fragment_measurement,
)
from traceback_runner.export import (
    LOCAL_REPORT_BANNER,
    ExportBoundaryError,
    ExportLimitations,
    ExportLimitationsV2,
    render_local_report,
    render_report,
)
from traceback_runner.fixtures import synthetic_fragment_policy
from traceback_runner.measurement import finalize_measurement, scan_records
from traceback_runner.signing import (
    DevelopmentSigningKey,
    DevelopmentTrustDocument,
    DevelopmentTrustDocumentV2,
    KeyPurpose,
    PublicTrustedKey,
    PublicTrustedKeyV2,
    RevokedKeyError,
    SigningError,
    TrustNamespace,
    TrustNamespaceError,
    TrustStore,
    development_trust_bytes,
    generate_development_keypair,
    load_development_trust,
    merge_development_trust_documents,
    parse_development_trust_document,
    sign_bytes,
)

FIXTURES = Path(__file__).parent / "fixtures"
FROZEN_BUNDLE = FIXTURES / "bundles" / "v2-synthetic"
FROZEN_TRUST = FIXTURES / "bundles" / "v2-synthetic.trust.json"
FROZEN_DIGESTS = FIXTURES / "bundles" / "v2-synthetic.sha256"
FROZEN_REGISTRY = FIXTURES / "result_trust_registry" / "v1"
LOCAL = TrustNamespace.DEVELOPMENT_LOCAL
SYNTHETIC = TrustNamespace.DEVELOPMENT_SYNTHETIC


def _local_measurement(**updates: object) -> dict[str, object]:
    value = _measurement(
        schema_version="traceback.fragment-measurement.v2",
        approval_state="unapproved_local",
        definition_id="aligned-reference-span-local-v2.ref-local",
        reference_id="ref-local",
    )
    value.update(updates)
    return value


def _local_key() -> DevelopmentSigningKey:
    return generate_development_keypair(KeyPurpose.RESULT, namespace=LOCAL)


def _local_bundle(
    path: Path,
    *,
    method: object = _METHOD,
    reference_match: str = "name_and_length_only",
) -> tuple[Path, DevelopmentSigningKey, TrustStore]:
    key = _local_key()
    store = TrustStore()
    store.add_signing_key(key)
    built = build_result_bundle(
        path,
        measurement=_local_measurement(),
        provenance=_provenance(),
        method=method,
        signing_key=key,
        reference_match=reference_match,  # type: ignore[arg-type]
    )
    return built, key, store


def _resign(path: Path, key: DevelopmentSigningKey, bundle_schema: str) -> None:
    """Recompute checksums and re-sign with ``key`` over ``bundle_schema``'s payload."""

    content = {relative: (path / relative).read_bytes() for relative in _CHECKSUM_PATHS}
    checksums = _checksums_bytes(content)
    (path / CHECKSUMS_PATH).write_bytes(checksums)
    signature = sign_bytes(
        canonical_json_bytes(_signing_payload(checksums, bundle_schema)),
        key,
        purpose=KeyPurpose.RESULT,
    )
    (path / SIGNATURE_PATH).write_bytes(canonical_json_bytes(signature))


def _rewrite_manifest(path: Path, **updates: object) -> dict[str, object]:
    manifest = json.loads((path / MANIFEST_PATH).read_bytes())
    manifest.update(updates)
    for item in manifest["contents"]:
        item["size_bytes"] = len((path / item["relative_path"]).read_bytes())
        item["sha256"] = hashlib.sha256(
            (path / item["relative_path"]).read_bytes()
        ).hexdigest()
    (path / MANIFEST_PATH).write_bytes(canonical_json_bytes(manifest))
    return manifest


def _public_v2(key: DevelopmentSigningKey) -> PublicTrustedKeyV2:
    return PublicTrustedKeyV2(
        key_id=key.key_id,
        namespace=key.namespace,
        purpose=key.purpose,
        public_key_base64=base64.b64encode(key.public_key_bytes()).decode("ascii"),
    )


def _public_v1(key: DevelopmentSigningKey) -> PublicTrustedKey:
    return PublicTrustedKey(
        key_id=key.key_id,
        purpose=key.purpose,
        public_key_base64=base64.b64encode(key.public_key_bytes()).decode("ascii"),
    )


def _copy_frozen_registry(destination: Path) -> dict[str, str]:
    destination.mkdir(mode=0o700)
    destination.chmod(0o700)
    for name in ("registry-metadata.json", "registry-journal.jsonl"):
        target = destination / name
        shutil.copyfile(FROZEN_REGISTRY / name, target)
        target.chmod(0o600)
    lock = destination / ".registry.lock"
    lock.touch(mode=0o600)
    lock.chmod(0o600)
    return json.loads((FROZEN_REGISTRY / "identity.json").read_text())


def _opened(root: Path, identity: dict[str, str]) -> ResultTrustRegistry:
    return ResultTrustRegistry(
        root,
        expected_registry_id=identity["registry_id"],
        expected_registry_epoch_sha256=identity["registry_epoch_sha256"],
        expected_state_head_sha256=identity["state_head_sha256"],
    )


# --------------------------------------------------------------------------
# B3a: frozen v2-synthetic bundle (acceptance 1)
# --------------------------------------------------------------------------


def test_frozen_v2_synthetic_bundle_file_hashes_match_committed_digests() -> None:
    expected = {}
    for line in FROZEN_DIGESTS.read_text(encoding="ascii").splitlines():
        digest, relative = line.split("  ", 1)
        expected[relative] = digest
    observed = {
        path.relative_to(FROZEN_DIGESTS.parent).as_posix(): hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
        for path in [*FROZEN_BUNDLE.rglob("*"), FROZEN_TRUST]
        if path.is_file()
    }
    assert observed == expected
    assert len(expected) == 9


def test_frozen_v2_synthetic_bundle_verifies_byte_identically() -> None:
    trust = load_development_trust(FROZEN_TRUST.read_bytes())
    verified = verify_bundle(FROZEN_BUNDLE, trust)

    assert verified.manifest.schema_version == "traceback.result-bundle.v2"
    assert type(verified.measurement) is FragmentMeasurement
    assert verified.measurement.approval_state == ApprovalState.UNAPPROVED_SYNTHETIC
    assert type(verified.limitations) is ExportLimitations
    assert verified.signature.namespace == SYNTHETIC
    # The current renderers reproduce the frozen bytes exactly.
    assert render_report(verified.measurement) == (FROZEN_BUNDLE / REPORT_PATH).read_bytes()
    assert canonical_json_bytes(ExportLimitations()) == (
        FROZEN_BUNDLE / LIMITATIONS_PATH
    ).read_bytes()
    assert canonical_json_bytes(verified.measurement) == (
        FROZEN_BUNDLE / MEASUREMENT_PATH
    ).read_bytes()
    assert DEFAULT_RESULT_BUNDLE_READER_REGISTRY.select(verified).reader_id == (
        "reader_result_bundle_v2"
    )


def test_frozen_v2_synthetic_bundle_imports_through_the_v2_reader(
    tmp_path: Path,
) -> None:
    import_root = tmp_path / "imports"
    shutil.copytree(FROZEN_BUNDLE, import_root / "frozen")
    registry, _, _, _, head, head_sha256, capability = _authority()
    catalog = ResultCatalog(
        tmp_path / "catalog",
        import_roots={"root_primary": import_root},
        trust_store=load_development_trust(FROZEN_TRUST.read_bytes()),
    )
    # The demo bundle binds a placeholder method definition digest, so the
    # catalog verifies it, selects reader_result_bundle_v2, and only then
    # refuses the method identity: the reader dispatch accepted it.
    assert capability.method_definition_sha256 != "c" * 64
    with pytest.raises(CatalogConflict, match="method identity"):
        catalog.import_bundle(
            root_id="root_primary",
            relative_path="frozen",
            registry=registry,
            authority_head=head,
            expected_authority_head_sha256=head_sha256,
            capability=capability,
            aliases=ALIASES,
        )


def test_demo_path_still_builds_v2_synthetic_bundles(tmp_path: Path) -> None:
    key = generate_development_keypair(KeyPurpose.RESULT)
    store = TrustStore()
    store.add_signing_key(key)
    path = build_result_bundle(
        tmp_path / "record",
        measurement=_measurement(),
        provenance=_provenance(),
        method=_METHOD,
        signing_key=key,
    )
    verified = verify_bundle(path, store)
    assert verified.manifest.schema_version == "traceback.result-bundle.v2"
    assert (path / LIMITATIONS_PATH).read_bytes() == (
        FROZEN_BUNDLE / LIMITATIONS_PATH
    ).read_bytes()
    assert development_trust_bytes(key).startswith(b'{"keys":')
    assert json.loads(development_trust_bytes(key))["schema_version"] == (
        "traceback.development-trust.v1"
    )


# --------------------------------------------------------------------------
# B3a: measurement v2 contracts (acceptance 4)
# --------------------------------------------------------------------------


def _policy_v2(approval: ApprovalState) -> FragmentMeasurementPolicyV2:
    synthetic = synthetic_fragment_policy()
    return FragmentMeasurementPolicyV2(
        definition_id="aligned-reference-span-local-v2.ref-local",
        approval_state=approval,
        reference_id="ref-local",
        contigs=synthetic.contigs,
        min_mapping_quality=synthetic.min_mapping_quality,
        bins=synthetic.bins,
    )


class _Record:
    is_unmapped = False
    is_secondary = False
    is_supplementary = False
    is_qcfail = False
    is_duplicate = False
    mapping_quality = 60
    cigartuples = ((0, 150),)

    def __init__(self, contig: str) -> None:
        self.reference_name = contig


def test_finalize_v2_local_policy_returns_v2_local_measurement() -> None:
    policy = _policy_v2(ApprovalState.UNAPPROVED_LOCAL)
    scan = scan_records([_Record(policy.contigs[0])] * 3, policy)
    assert scan.policy_schema_version == "traceback.fragment-policy.v2"
    assert scan.approval_state == ApprovalState.UNAPPROVED_LOCAL

    measurement = finalize_measurement(scan)

    assert type(measurement) is FragmentMeasurementV2
    assert measurement.approval_state == "unapproved_local"
    assert measurement.schema_version == "traceback.fragment-measurement.v2"
    assert measurement.eligible_alignments == 3


def test_finalize_v1_policy_still_returns_v1_synthetic_measurement() -> None:
    policy = synthetic_fragment_policy()
    measurement = finalize_measurement(
        scan_records([_Record(policy.contigs[0])], policy)
    )
    assert type(measurement) is FragmentMeasurement
    assert measurement.approval_state == ApprovalState.UNAPPROVED_SYNTHETIC


def test_v1_contracts_never_carry_the_local_label() -> None:
    with pytest.raises(ValueError):
        FragmentMeasurement.model_validate(_measurement(approval_state="unapproved_local"))
    with pytest.raises(ValueError):
        FragmentMeasurementPolicy.model_validate(
            {
                **synthetic_fragment_policy().model_dump(mode="json"),
                "approval_state": "unapproved_local",
            }
        )
    # v2 requires the label explicitly: no synthetic default.
    without_label = _local_measurement()
    without_label.pop("approval_state")
    with pytest.raises(ValueError):
        FragmentMeasurementV2.model_validate(without_label)


def test_parse_fragment_measurement_dispatches_on_schema_version() -> None:
    v1 = canonical_json_bytes(FragmentMeasurement.model_validate(_measurement()))
    v2 = canonical_json_bytes(FragmentMeasurementV2.model_validate(_local_measurement()))
    assert type(parse_fragment_measurement(v1)) is FragmentMeasurement
    assert type(parse_fragment_measurement(v2)) is FragmentMeasurementV2
    for content in (
        canonical_json_bytes(_measurement(schema_version="traceback.fragment-measurement.v3")),
        canonical_json_bytes(_local_measurement(schema_version="traceback.fragment-measurement.v1")),
        b"[]",
        v1.replace(b"{", b"{ ", 1),
    ):
        with pytest.raises(ValueError):
            parse_fragment_measurement(content)


# --------------------------------------------------------------------------
# B3a: bundle v3 build, verify, catalog import (acceptance 2, 3)
# --------------------------------------------------------------------------


def test_v3_local_bundle_builds_and_verifies(tmp_path: Path) -> None:
    path, key, store = _local_bundle(tmp_path / "record")
    verified = verify_bundle(path, store)

    assert verified.manifest.schema_version == "traceback.result-bundle.v3"
    assert verified.manifest.measurement_schema_versions == (
        "traceback.fragment-measurement.v2",
    )
    assert type(verified.measurement) is FragmentMeasurementV2
    assert verified.measurement.approval_state == ApprovalState.UNAPPROVED_LOCAL
    assert verified.limitations == ExportLimitationsV2(
        template_id="local-fragment-length-research-use.v1",
        reference_match="name_and_length_only",
    )
    assert verified.signature.namespace == LOCAL
    assert verified.signature.key_id.startswith("devlocal-result-")
    assert LOCAL_REPORT_BANNER.encode() in (path / REPORT_PATH).read_bytes()
    assert b"ynthetic" not in (path / REPORT_PATH).read_bytes()


def test_v3_local_bundle_imports_through_the_v3_reader(tmp_path: Path) -> None:
    import_root = tmp_path / "imports"
    registry, _, _, _, head, head_sha256, capability = _authority()
    _, _, store = _local_bundle(
        import_root / "incoming", method=_bundle_method(capability)
    )
    catalog = ResultCatalog(
        tmp_path / "catalog",
        import_roots={"root_primary": import_root},
        trust_store=store,
        reader_registry=LOCAL_RESULT_BUNDLE_READER_REGISTRY,
    )
    reference = catalog.import_bundle(
        root_id="root_primary",
        relative_path="incoming",
        registry=registry,
        authority_head=head,
        expected_authority_head_sha256=head_sha256,
        capability=capability,
        aliases=ALIASES,
    )
    verified, reader = catalog.verify_reference(reference)
    assert reader.reader_id == "reader_result_bundle_v3"
    assert type(verified.measurement) is FragmentMeasurementV2


def test_reader_registry_rejects_crossed_bundle_and_measurement_versions(
    tmp_path: Path,
) -> None:
    synthetic_key = generate_development_keypair(KeyPurpose.RESULT)
    synthetic_store = TrustStore()
    synthetic_store.add_signing_key(synthetic_key)
    v2 = verify_bundle(
        build_result_bundle(
            tmp_path / "v2",
            measurement=_measurement(),
            provenance=_provenance(),
            method=_METHOD,
            signing_key=synthetic_key,
        ),
        synthetic_store,
    )
    path, _, store = _local_bundle(tmp_path / "v3")
    v3 = verify_bundle(path, store)
    crossed = (
        # A v3 manifest that lists fragment-measurement.v1.
        v3.manifest.model_copy(
            update={"measurement_schema_versions": ("traceback.fragment-measurement.v1",)}
        ),
        # A v2 manifest that lists fragment-measurement.v2.
        v2.manifest.model_copy(
            update={"measurement_schema_versions": ("traceback.fragment-measurement.v2",)}
        ),
    )
    for manifest in crossed:
        forged = VerifiedBundle.model_construct(**{**dict(v3), "manifest": manifest})
        with pytest.raises(CatalogUnsupportedSchema, match="measurement schema"):
            LOCAL_RESULT_BUNDLE_READER_REGISTRY.select(forged)
    assert LOCAL_RESULT_BUNDLE_READER_REGISTRY.select(v2).reader_id == (
        "reader_result_bundle_v2"
    )
    assert LOCAL_RESULT_BUNDLE_READER_REGISTRY.select(v3).reader_id == (
        "reader_result_bundle_v3"
    )
    # The default registry is unchanged and never selects a v3 bundle.
    with pytest.raises(CatalogUnsupportedSchema, match="bundle schema"):
        DEFAULT_RESULT_BUNDLE_READER_REGISTRY.select(v3)


def test_crossed_bundle_and_measurement_versions_fail_verification(
    tmp_path: Path,
) -> None:
    # A v2 bundle (synthetic namespace) carrying a v2 measurement.
    path, _, _ = _local_bundle(tmp_path / "v2-with-v2")
    synthetic_key = generate_development_keypair(KeyPurpose.RESULT)
    store = TrustStore()
    store.add_signing_key(synthetic_key)
    _rewrite_manifest(
        path,
        schema_version="traceback.result-bundle.v2",
        signing_key_id=synthetic_key.key_id,
    )
    _resign(path, synthetic_key, "traceback.result-bundle.v2")
    with pytest.raises(BundleError):
        verify_bundle(path, store)

    # A v3 bundle (local namespace) carrying a v1 measurement.
    local_key = _local_key()
    local_store = TrustStore()
    local_store.add_signing_key(local_key)
    original = generate_development_keypair(KeyPurpose.RESULT)
    path = build_result_bundle(
        tmp_path / "v3-with-v1",
        measurement=_measurement(),
        provenance=_provenance(),
        method=_METHOD,
        signing_key=original,
    )
    _rewrite_manifest(
        path,
        schema_version="traceback.result-bundle.v3",
        signing_key_id=local_key.key_id,
    )
    _resign(path, local_key, "traceback.result-bundle.v3")
    with pytest.raises(BundleFormatError, match="measurement schema"):
        verify_bundle(path, local_store)


def test_v3_bundle_carries_only_local_records_from_local_keys(tmp_path: Path) -> None:
    local_key = _local_key()
    synthetic_key = generate_development_keypair(KeyPurpose.RESULT)
    base = {"provenance": _provenance(), "method": _METHOD}
    cases = (
        # A synthetic-labelled v2 measurement has no v3 bundle.
        (
            {
                "measurement": _local_measurement(approval_state="unapproved_synthetic"),
                "signing_key": local_key,
                "reference_match": "registered_digests",
            },
            "unapproved_local",
        ),
        # The local record states how the reference was matched.
        ({"measurement": _local_measurement(), "signing_key": local_key}, "reference match"),
        # A local record is never signed under development-synthetic.
        (
            {
                "measurement": _local_measurement(),
                "signing_key": synthetic_key,
                "reference_match": "registered_digests",
            },
            "development-local",
        ),
        # A synthetic record is never signed under development-local.
        ({"measurement": _measurement(), "signing_key": local_key}, "development-synthetic"),
        (
            {
                "measurement": _measurement(),
                "signing_key": synthetic_key,
                "reference_match": "registered_digests",
            },
            "no reference match",
        ),
    )
    for index, (values, message) in enumerate(cases):
        with pytest.raises(BundleFormatError, match=message):
            build_result_bundle(tmp_path / f"case-{index}", **base, **values)  # type: ignore[arg-type]
        assert not (tmp_path / f"case-{index}").exists()


def test_v3_verification_rejects_swapped_limitations(tmp_path: Path) -> None:
    path, key, store = _local_bundle(tmp_path / "record")
    (path / LIMITATIONS_PATH).write_bytes(
        canonical_json_bytes(
            ExportLimitationsV2(
                template_id="local-fragment-length-research-use.v1",
                reference_match="registered_digests",
            )
        )
    )
    _rewrite_manifest(path)
    _resign(path, key, "traceback.result-bundle.v3")
    # The limitation is signed and consistent, but the report no longer is.
    with pytest.raises(BundleError, match="report"):
        verify_bundle(path, store)

    (path / LIMITATIONS_PATH).write_bytes(canonical_json_bytes(ExportLimitations()))
    _rewrite_manifest(path)
    _resign(path, key, "traceback.result-bundle.v3")
    with pytest.raises(BundleFormatError, match="limitations"):
        verify_bundle(path, store)


# --------------------------------------------------------------------------
# B3a: local report (acceptance 5)
# --------------------------------------------------------------------------


@pytest.mark.parametrize("reference_match", ["registered_digests", "name_and_length_only"])
def test_local_report_has_the_banner_and_no_synthetic_or_qualified_language(
    reference_match: str,
) -> None:
    measurement = FragmentMeasurementV2.model_validate(_local_measurement())
    report = render_local_report(
        measurement,
        ExportLimitationsV2(
            template_id="local-fragment-length-research-use.v1",
            reference_match=reference_match,  # type: ignore[arg-type]
        ),
    ).decode("utf-8")

    assert LOCAL_REPORT_BANNER == (
        "Unqualified. Local development record. Not for clinical use. "
        "Development signing key only."
    )
    assert report.count(LOCAL_REPORT_BANNER) == 1
    remainder = report.replace(LOCAL_REPORT_BANNER, "")
    assert "synthetic" not in remainder.lower()
    assert re.search(r"(?<![Uu]n)qualified", remainder) is None
    assert "E0 approval" in remainder
    assert ("name and length only" in remainder) == (
        reference_match == "name_and_length_only"
    )


def test_synthetic_report_refuses_a_local_record() -> None:
    measurement = FragmentMeasurementV2.model_validate(_local_measurement())
    with pytest.raises(ExportBoundaryError, match="synthetic report"):
        render_report(measurement)
    with pytest.raises(ExportBoundaryError, match="local report"):
        render_local_report(
            FragmentMeasurementV2.model_validate(
                _local_measurement(approval_state="unapproved_synthetic")
            ),
            ExportLimitationsV2(
                template_id="local-fragment-length-research-use.v1",
                reference_match="registered_digests",
            ),
        )


# --------------------------------------------------------------------------
# B3a: every dispatch table accepts the new versions, at runtime (acceptance 6)
# --------------------------------------------------------------------------


def test_every_schema_dispatch_table_accepts_the_new_versions(tmp_path: Path) -> None:
    import traceback_runner.bundles as bundles_module
    import traceback_runner.contracts as contracts_module
    import traceback_runner.signing as signing_module

    # Exact membership: a missing comma would concatenate two literals and
    # drop both, which a length-and-content check catches.
    assert tuple(contracts_module.FRAGMENT_MEASUREMENT_MODELS) == (
        "traceback.fragment-measurement.v1",
        "traceback.fragment-measurement.v2",
    )
    assert tuple(contracts_module.FRAGMENT_POLICY_MEASUREMENT_SCHEMAS) == (
        "traceback.fragment-policy.v1",
        "traceback.fragment-policy.v2",
    )
    assert bundles_module.RESULT_BUNDLE_SCHEMA_VERSIONS == (
        "traceback.result-bundle.v1",
        "traceback.result-bundle.v2",
        "traceback.result-bundle.v3",
        "traceback.result-bundle.v4",
    )
    assert tuple(bundles_module._MANIFEST_MODELS) == bundles_module.RESULT_BUNDLE_SCHEMA_VERSIONS
    assert tuple(signing_module.DEVELOPMENT_TRUST_DOCUMENT_MODELS) == (
        "traceback.development-trust.v1",
        "traceback.development-trust.v2",
    )
    # Signing payload per bundle version, each with its namespace.
    # (v4 is signed over a per-schema layout; tests/test_bundle_v4.py covers it.)
    payloads = {
        version: bundles_module._signing_payload(b"x", version)
        for version in bundles_module.RESULT_BUNDLE_SCHEMA_VERSIONS[:3]
    }
    assert {
        version: (payload.schema_version, payload.trust_namespace)
        for version, payload in payloads.items()
    } == {
        "traceback.result-bundle.v1": ("traceback.bundle-signing-payload.v1", SYNTHETIC),
        "traceback.result-bundle.v2": ("traceback.bundle-signing-payload.v2", SYNTHETIC),
        "traceback.result-bundle.v3": ("traceback.bundle-signing-payload.v3", LOCAL),
    }
    # Catalog readers: exactly one per version, exact measurement tuples.
    assert [
        (reader.reader_id, reader.minimum_version, reader.maximum_version,
         reader.measurement_schema_versions)
        for reader in LOCAL_RESULT_BUNDLE_READER_REGISTRY.readers
    ] == [
        ("reader_result_bundle_v2", 2, 2, ("traceback.fragment-measurement.v1",)),
        ("reader_result_bundle_v3", 3, 3, ("traceback.fragment-measurement.v2",)),
    ]
    # End to end, by calling: each measurement schema parses, each new bundle
    # version builds, verifies, and selects its reader.
    path, _, store = _local_bundle(tmp_path / "v3")
    verified = verify_bundle(path, store)
    assert LOCAL_RESULT_BUNDLE_READER_REGISTRY.select(verified).reader_id.endswith("v3")
    for version, model in contracts_module.FRAGMENT_MEASUREMENT_MODELS.items():
        sample = _local_measurement() if version.endswith("v2") else _measurement()
        assert type(parse_fragment_measurement(canonical_json_bytes(sample))) is model


# --------------------------------------------------------------------------
# B3b: development-local trust namespace
# --------------------------------------------------------------------------


def test_development_keys_are_bound_to_exactly_one_namespace() -> None:
    local = _local_key()
    synthetic = generate_development_keypair(KeyPurpose.RESULT)
    assert local.key_id.startswith("devlocal-result-")
    assert synthetic.key_id.startswith("dev-result-")
    with pytest.raises(SigningError):
        DevelopmentSigningKey(
            key_id=local.key_id,
            purpose=KeyPurpose.RESULT,
            private_key=local.private_key,
            namespace=SYNTHETIC,
        )
    with pytest.raises(SigningError):
        generate_development_keypair(
            KeyPurpose.RESULT, namespace=TrustNamespace.EXTERNAL_RELEASE
        )
    # The same public key under the other namespace is a different key ID, so
    # a trust document cannot relabel a key.
    forged = _public_v2(local).model_copy(update={"namespace": SYNTHETIC})
    with pytest.raises(SigningError):
        load_development_trust(
            canonical_json_bytes(DevelopmentTrustDocumentV2(keys=(forged,))) + b"\n"
        )


def test_v3_bundle_signed_by_a_synthetic_key_fails_on_namespace(tmp_path: Path) -> None:
    path, _, _ = _local_bundle(tmp_path / "record")
    synthetic_key = generate_development_keypair(KeyPurpose.RESULT)
    store = TrustStore()
    store.add_signing_key(synthetic_key)
    _rewrite_manifest(path, signing_key_id=synthetic_key.key_id)
    _resign(path, synthetic_key, "traceback.result-bundle.v3")
    with pytest.raises(TrustNamespaceError):
        verify_bundle(path, store)


def test_v2_bundle_signed_by_a_local_key_fails_on_namespace(tmp_path: Path) -> None:
    synthetic_key = generate_development_keypair(KeyPurpose.RESULT)
    path = build_result_bundle(
        tmp_path / "record",
        measurement=_measurement(),
        provenance=_provenance(),
        method=_METHOD,
        signing_key=synthetic_key,
    )
    local_key = _local_key()
    store = TrustStore()
    store.add_signing_key(local_key)
    _rewrite_manifest(path, signing_key_id=local_key.key_id)
    _resign(path, local_key, "traceback.result-bundle.v2")
    with pytest.raises(TrustNamespaceError):
        verify_bundle(path, store)


def test_trust_documents_stay_v1_unless_a_local_key_is_present() -> None:
    synthetic = generate_development_keypair(KeyPurpose.RESULT)
    local = _local_key()
    v1_bytes = development_trust_bytes(synthetic)
    v2_bytes = development_trust_bytes(synthetic, local)

    assert type(parse_development_trust_document(v1_bytes)) is DevelopmentTrustDocument
    v2 = parse_development_trust_document(v2_bytes)
    assert type(v2) is DevelopmentTrustDocumentV2
    assert {key.namespace for key in v2.keys} == {SYNTHETIC, LOCAL}
    store = load_development_trust(v2_bytes)
    assert store.resolve(local.key_id).namespace == LOCAL
    assert store.resolve(synthetic.key_id).namespace == SYNTHETIC

    merged = merge_development_trust_documents(
        parse_development_trust_document(v1_bytes),
        parse_development_trust_document(development_trust_bytes(local)),
    )
    assert type(merged) is DevelopmentTrustDocumentV2
    assert [key.key_id for key in merged.keys] == sorted([synthetic.key_id, local.key_id])
    assert type(
        merge_development_trust_documents(parse_development_trust_document(v1_bytes))
    ) is DevelopmentTrustDocument
    revoked = merged.model_copy(
        update={"keys": tuple(key.model_copy(update={"revoked": True}) for key in merged.keys)}
    )
    with pytest.raises(SigningError, match="cannot be changed"):
        merge_development_trust_documents(merged, revoked)


def test_frozen_v1_registry_reopens_and_verifies_the_frozen_v2_bundle(
    tmp_path: Path,
) -> None:
    root = tmp_path / "registry"
    identity = _copy_frozen_registry(root)
    before = {
        name: (root / name).read_bytes()
        for name in ("registry-metadata.json", "registry-journal.jsonl")
    }
    with _opened(root, identity) as registry:
        snapshot, store = registry.current_trust_store()
        assert type(snapshot) is ResultTrustSnapshot
        assert snapshot.synthetic_only is True
        assert verify_bundle(FROZEN_BUNDLE, store).manifest.record_id
        # A v1 registry never takes a development-local key.
        with pytest.raises(ResultTrustRegistryConflict, match="namespace"):
            registry.add_key(_public_v2(_local_key()))
        with pytest.raises(ResultTrustRegistryConflict, match="identifier"):
            registry.revoke_key(_local_key().key_id)
    after = {name: (root / name).read_bytes() for name in before}
    assert after == before


def test_v2_registry_accepts_both_namespaces_per_key(tmp_path: Path) -> None:
    local = _local_key()
    synthetic = generate_development_keypair(KeyPurpose.RESULT)
    with ResultTrustRegistry(tmp_path / "registry", create_version=2) as registry:
        empty = registry.current_trust()
        assert type(empty) is ResultTrustSnapshotV2
        assert empty.data_origin == ()
        receipt = registry.add_key(_public_v2(local))
        assert type(receipt) is ResultTrustEventReceiptV2
        assert receipt.namespace == LOCAL
        registry.add_key(_public_v1(synthetic))
        snapshot, store = registry.current_trust_store()
        assert snapshot.data_origin == ("local_unqualified", "synthetic")
        assert {key.key_id: key.namespace for key in snapshot.document.keys} == {
            local.key_id: LOCAL,
            synthetic.key_id: SYNTHETIC,
        }
        assert store.resolve(local.key_id).namespace == LOCAL
        # The catalog binds a v2 snapshot under its own domain tag.
        assert registry_trust_snapshot_sha256(snapshot) != registry_trust_snapshot_sha256(
            ResultTrustSnapshot.model_construct(
                **{
                    **dict(snapshot),
                    "schema_version": "traceback.result-trust-snapshot.v1",
                    "document": None,
                }
            )
        )
        registry.revoke_key(local.key_id)
        revoked = registry.current_trust()
        assert [key.revoked for key in revoked.document.keys if key.key_id == local.key_id] == [True]
        with pytest.raises(ResultTrustRegistryConflict, match="revoked"):
            registry.add_key(_public_v2(local))
        backup = registry.backup_bytes()
        retained = (revoked.registry_id, revoked.registry_epoch_sha256, revoked.state_head_sha256)
    parsed = result_trust_backup_from_bytes(backup)
    assert parsed.schema_version == "traceback.result-trust-backup.v2"
    journal = (tmp_path / "registry" / "registry-journal.jsonl").read_bytes().splitlines()
    assert {json.loads(line)["schema_version"] for line in journal} == {
        "traceback.result-trust-journal-entry.v2"
    }
    with ResultTrustRegistry(
        tmp_path / "registry",
        expected_registry_id=retained[0],
        expected_registry_epoch_sha256=retained[1],
        expected_state_head_sha256=retained[2],
    ) as reopened:
        assert reopened.current_trust() == revoked


def test_registry_versions_never_mix_journal_entries(tmp_path: Path) -> None:
    v2_root = tmp_path / "v2"
    with ResultTrustRegistry(v2_root, create_version=2) as registry:
        registry.add_key(_public_v2(_local_key()))
        v2_line = (v2_root / "registry-journal.jsonl").read_bytes()
    v1_root = tmp_path / "v1"
    identity = _copy_frozen_registry(v1_root)
    with (v1_root / "registry-journal.jsonl").open("ab") as stream:
        stream.write(v2_line)
    with pytest.raises(ResultTrustRegistryUnsafe):
        _opened(v1_root, identity)
    with pytest.raises(ResultTrustRegistryUnsafe, match="create version"):
        ResultTrustRegistry(tmp_path / "v3", create_version=3)


def test_verify_cli_with_a_trust_registry_works_for_both_namespaces(
    tmp_path: Path, capsys
) -> None:
    local_path, local_key, _ = _local_bundle(tmp_path / "local")
    synthetic_key = generate_development_keypair(KeyPurpose.RESULT)
    synthetic_path = build_result_bundle(
        tmp_path / "synthetic",
        measurement=_measurement(),
        provenance=_provenance(),
        method=_METHOD,
        signing_key=synthetic_key,
    )
    root = tmp_path / "trust"
    with ResultTrustRegistry(root, create_version=2) as registry:
        registry.add_key(_public_v2(local_key))
        registry.add_key(_public_v2(synthetic_key))
        current = registry.current_trust()
    options = [
        "--trust-registry", str(root),
        "--trust-registry-id", current.registry_id,
        "--trust-registry-epoch", current.registry_epoch_sha256,
        "--trust-registry-head", current.state_head_sha256,
        "--json",
    ]
    for bundle in (local_path, synthetic_path):
        assert main(["verify", str(bundle), *options]) == ExitCode.OK
        assert json.loads(capsys.readouterr().out)["data"]["verified"] is True
    # The trust-store path verifies a local bundle from a v2 trust document.
    trust_file = tmp_path / "trust.json"
    trust_file.write_bytes(development_trust_bytes(local_key))
    assert main(["verify", str(local_path), "--trust-store", str(trust_file), "--json"]) == (
        ExitCode.OK
    )
    capsys.readouterr()


def test_catalog_imports_a_v3_bundle_through_a_v2_trust_registry(tmp_path: Path) -> None:
    import_root = tmp_path / "imports"
    registry, _, _, _, head, head_sha256, capability = _authority()
    _, key, _ = _local_bundle(import_root / "incoming", method=_bundle_method(capability))
    with ResultTrustRegistry(tmp_path / "trust", create_version=2) as trust:
        trust.add_key(_public_v2(key))
        catalog = ResultCatalog(
            tmp_path / "catalog",
            import_roots={"root_primary": import_root},
            result_trust_registry=trust,
            reader_registry=LOCAL_RESULT_BUNDLE_READER_REGISTRY,
        )
        reference = catalog.import_bundle(
            root_id="root_primary",
            relative_path="incoming",
            registry=registry,
            authority_head=head,
            expected_authority_head_sha256=head_sha256,
            capability=capability,
            aliases=ALIASES,
        )
        _, reader = catalog.verify_reference(reference)
        assert reader.reader_id == "reader_result_bundle_v3"
        authority = catalog.authority_snapshot()
        assert authority.trust_snapshot_sha256 == registry_trust_snapshot_sha256(
            trust.current_trust()
        )
        trust.revoke_key(key.key_id)
        with pytest.raises(RevokedKeyError):
            catalog.verify_reference(reference)


def test_default_reader_registry_digest_is_unchanged() -> None:
    # Pinned: the pre-change default registry (v2 reader only).  Changing it
    # would re-digest every existing catalog authority and retained binding.
    assert hashlib.sha256(
        canonical_json_bytes(DEFAULT_RESULT_BUNDLE_READER_REGISTRY)
    ).hexdigest() == "4fab926e039de0c27a67fe8f62e984a7750d4b1c3250c157fc716d5aaba8d733"
    assert [reader.reader_id for reader in DEFAULT_RESULT_BUNDLE_READER_REGISTRY.readers] == [
        "reader_result_bundle_v2"
    ]


def test_v2_journal_entry_namespace_must_match_its_key_id() -> None:
    import evidence_inspector.result_trust_registry as trust_module

    local = _local_key()
    revocation = trust_module._build_journal_entry(
        entry_model=trust_module.ResultTrustJournalEntryV2,
        sequence=1,
        previous_entry_sha256="0" * 64,
        event=trust_module.ResultTrustEventKind.REVOKE_KEY,
        key_id=local.key_id,
        public_key_base64=None,
    )
    assert revocation.namespace == LOCAL
    with pytest.raises(ValueError, match="another namespace"):
        trust_module.ResultTrustJournalEntryV2.model_validate(
            {**revocation.model_dump(mode="json"), "namespace": "development-synthetic"}
        )


def test_finalize_revalidates_a_crossed_scan() -> None:
    policy = _policy_v2(ApprovalState.UNAPPROVED_LOCAL)
    scan = scan_records([_Record(policy.contigs[0])], policy)
    crossed = scan.model_copy(
        update={"policy_schema_version": "traceback.fragment-policy.v1"}
    )
    with pytest.raises(ValueError, match="synthetic only"):
        finalize_measurement(crossed)


def test_measurement_subclass_is_normalized_to_the_exact_contract(tmp_path: Path) -> None:
    class Sneaky(FragmentMeasurementV2):
        pass

    sneaky = Sneaky.model_validate(_local_measurement())
    synthetic_key = generate_development_keypair(KeyPurpose.RESULT)
    with pytest.raises(BundleFormatError, match="development-local"):
        build_result_bundle(
            tmp_path / "record",
            measurement=sneaky,
            provenance=_provenance(),
            method=_METHOD,
            signing_key=synthetic_key,
            reference_match="registered_digests",
        )
    path, _, store = _local_bundle(tmp_path / "ok")
    assert type(verify_bundle(path, store).measurement) is FragmentMeasurementV2


def test_crossed_schema_literals_are_rejected_at_each_boundary() -> None:
    import evidence_inspector.result_trust_registry as trust_module

    crossed_limitations = ExportLimitationsV2(
        template_id="local-fragment-length-research-use.v1",
        reference_match="registered_digests",
    ).model_copy(update={"schema_version": "traceback.limitations.v1"})
    with pytest.raises(ExportBoundaryError, match="invalid"):
        render_local_report(
            FragmentMeasurementV2.model_validate(_local_measurement()), crossed_limitations
        )

    document = parse_development_trust_document(development_trust_bytes(_local_key()))
    crossed_document = document.model_copy(
        update={"schema_version": "traceback.development-trust.v1"}
    )
    with pytest.raises(SigningError):
        merge_development_trust_documents(crossed_document)
    with pytest.raises(ResultTrustRegistryConflict):
        trust_module.project_result_trust_document(crossed_document, ())

    snapshot = ResultTrustSnapshot.model_construct(
        schema_version="traceback.result-trust-snapshot.v2",
        registry_id="result_trust_registry_" + "0" * 32,
        registry_epoch_sha256="0" * 64,
        state_version=0,
        state_head_sha256="0" * 64,
        document_sha256="0" * 64,
    )
    with pytest.raises(CatalogError, match="unsupported"):
        registry_trust_snapshot_sha256(snapshot)
