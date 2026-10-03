"""Adversarial offline tests for the synthetic asset registry."""

from __future__ import annotations

import hashlib
import json
import os
import struct
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from traceback_runner.assets import (
    PACKAGE_MAGIC,
    AssetAuthorityError,
    AssetCapacityError,
    AssetConflictError,
    AssetFilesystemError,
    AssetIntegrityError,
    AssetPackageError,
    AssetRegistry,
    IntegrityStatus,
    ReleaseAuthorization,
    build_synthetic_asset_package,
)
from traceback_runner.qualification import (
    ApproverRole,
    AuthorityScope,
    GrantStatus,
    QualificationTrustPolicy,
    ReleaseAuthorityHead,
    SignerRoleGrant,
    qualification_binding,
    sign_development_release_evidence,
)
from traceback_runner.release_evidence import (
    AssetContentIdentity,
    AssetKind,
    AssetLifecycle,
    AssetProvenance,
    AssetReference,
    AssetStatus,
    DigestDomain,
    EvidenceRequirementKind,
    EvidenceStatus,
    ProtocolApprovalStatus,
    ProtocolReference,
    QualificationEvidenceItem,
    QualificationEvidenceManifest,
    WorkstationProfile,
    build_release_evidence_package,
    domain_digest,
)
from traceback_runner.serialization import canonical_json_bytes
from traceback_runner.signing import (
    KeyPurpose,
    TrustStore,
    generate_development_keypair,
)

NOW = datetime(2026, 9, 27, 16, 0, tzinfo=UTC)
PAYLOAD = b"tiny synthetic reference bytes\n"


def _asset(
    *,
    payload: bytes = PAYLOAD,
    asset_id: str = "synthetic-reference",
    version: str = "v1",
    status: AssetStatus = AssetStatus.ACTIVE,
    source_authority: str = "Synthetic fixture authority; no scientific authority.",
) -> AssetReference:
    lifecycle = (
        AssetLifecycle(status=status)
        if status == AssetStatus.ACTIVE
        else AssetLifecycle(
            status=status,
            revocation_reference="synthetic-revocation-1",
            revoked_at=NOW,
        )
    )
    return AssetReference(
        content=AssetContentIdentity(
            asset_id=asset_id,
            version=version,
            kind=AssetKind.REFERENCE,
            content_sha256=hashlib.sha256(payload).hexdigest(),
            content_size_bytes=len(payload),
        ),
        provenance=AssetProvenance(
            source_authority=source_authority,
            license_id="synthetic-only",
        ),
        lifecycle=lifecycle,
    )


def _release(asset: AssetReference):
    profile = WorkstationProfile(
        profile_id="synthetic-host",
        version="v1",
        operating_system="linux",
        architecture="x86_64",
        kernel_version="synthetic-kernel.v1",
        runtime_id="synthetic-runtime",
        runtime_version="v1",
        minimum_cpu_cores=2,
        minimum_memory_bytes=1024,
        minimum_free_disk_bytes=2048,
    )
    protocol = ProtocolReference(
        protocol_id="synthetic-protocol",
        version="v1",
        document_sha256="2" * 64,
        reported_approval_status=ProtocolApprovalStatus.PENDING,
    )
    evidence = QualificationEvidenceManifest(
        manifest_id="synthetic-evidence",
        version="v1",
        items=tuple(
            QualificationEvidenceItem(requirement=item, status=EvidenceStatus.UNKNOWN)
            for item in EvidenceRequirementKind
        ),
    )
    return build_release_evidence_package(
        release_id="synthetic-release",
        version="v1",
        workflow_release_sha256="3" * 64,
        workstation_profile=profile,
        protocol_reference=protocol,
        evidence_manifest=evidence,
        assets=(asset,),
    )


def _authorization(
    asset: AssetReference,
    *,
    head: bool = True,
    trusted: bool = True,
) -> ReleaseAuthorization:
    package = _release(asset)
    binding = qualification_binding(package)
    key = generate_development_keypair(KeyPurpose.RELEASE)
    envelope = sign_development_release_evidence(
        package, key, signer_role=ApproverRole.RELEASE_REVIEWER
    )
    trust = TrustStore()
    if trusted:
        trust.add_signing_key(key)
    policy = QualificationTrustPolicy(
        policy_id="synthetic-policy",
        version="v1",
        issued_at=NOW - timedelta(hours=1),
        expires_at=NOW + timedelta(days=1),
        grants=(
            SignerRoleGrant(
                scope=AuthorityScope.RELEASE_EVIDENCE,
                role=ApproverRole.RELEASE_REVIEWER,
                key_id=key.key_id,
                binding=binding,
                valid_from=NOW - timedelta(hours=1),
                expires_at=NOW + timedelta(days=1),
                status=GrantStatus.ACTIVE,
            ),
        ),
    )
    package_digest = domain_digest(DigestDomain.RELEASE_EVIDENCE, package)
    authority_head = (
        ReleaseAuthorityHead(
            release_id=package.release_id,
            release_version=package.version,
            package_sha256=package_digest,
            as_of=NOW - timedelta(minutes=1),
            expires_at=NOW + timedelta(hours=1),
        )
        if head
        else None
    )
    return ReleaseAuthorization(
        envelope=envelope,
        trust_store=trust,
        role_policy=policy,
        authority_head=authority_head,
        expected_binding=binding,
        expected_package_sha256=package_digest,
        now=NOW,
    )


def _fixture(tmp_path: Path, *, asset: AssetReference | None = None):
    reference = asset or _asset()
    package = build_synthetic_asset_package(
        tmp_path / "asset.tbxasset", reference=reference, payload=PAYLOAD
    )
    authorization = _authorization(reference)
    return reference, package, authorization


def _install(
    registry: AssetRegistry,
    package: Path,
    authorization: ReleaseAuthorization,
):
    return registry.install(
        package,
        asset_id="synthetic-reference",
        version="v1",
        authorization=authorization,
    )


def test_install_and_independent_verify_keep_permissions_false(tmp_path: Path) -> None:
    reference, package, authorization = _fixture(tmp_path)
    registry = AssetRegistry(tmp_path / "registry")

    estimate = registry.plan_install(package)
    installed = _install(registry, package, authorization)
    verified = registry.verify(
        asset_id="synthetic-reference", version="v1", authorization=authorization
    )

    assert estimate.admitted
    assert installed.newly_registered
    assert installed.content_sha256 == reference.content.content_sha256
    assert installed.execution_authorized is False
    assert verified.installed
    assert verified.integrity_status == IntegrityStatus.VALID
    assert verified.authority_status == "verified"
    assert verified.lifecycle_status == "active"
    assert verified.registration_matches_current_reference
    assert verified.execution_authorized is False
    assert verified.qualification_established is False
    assert str(tmp_path).encode() not in b"".join(
        path.read_bytes()
        for path in (tmp_path / "registry").rglob("*")
        if path.is_file()
    )


def test_identical_install_is_idempotent_and_concurrent_safe(tmp_path: Path) -> None:
    _, package, authorization = _fixture(tmp_path)
    registry = AssetRegistry(tmp_path / "registry")

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda _: _install(registry, package, authorization),
                range(2),
            )
        )

    assert sorted(item.newly_registered for item in results) == [False, True]
    objects = [path for path in registry.objects.rglob("*") if path.is_file()]
    registrations = [path for path in registry.references.rglob("*") if path.is_file()]
    assert len(objects) == 1
    assert len(registrations) == 1


def test_interrupted_publish_leaves_unregistered_orphan_then_adopts_it(
    tmp_path: Path,
) -> None:
    _, package, authorization = _fixture(tmp_path)
    raised = False

    def fail_after_object(point: str) -> None:
        nonlocal raised
        if point == "after_object_publish" and not raised:
            raised = True
            raise OSError("synthetic rename interruption")

    first = AssetRegistry(tmp_path / "registry", fault_injector=fail_after_object)
    with pytest.raises(OSError, match="synthetic rename interruption"):
        _install(first, package, authorization)
    assert not [path for path in first.references.rglob("*") if path.is_file()]
    assert len([path for path in first.objects.rglob("*") if path.is_file()]) == 1

    second = AssetRegistry(tmp_path / "registry")
    installed = _install(second, package, authorization)
    assert installed.newly_registered
    assert (
        second.verify(
            asset_id="synthetic-reference", version="v1", authorization=authorization
        ).integrity_status
        == IntegrityStatus.VALID
    )


def test_failure_before_object_publish_cleans_private_stage(tmp_path: Path) -> None:
    _, package, authorization = _fixture(tmp_path)

    def fail(point: str) -> None:
        if point == "after_staging_fsync":
            raise OSError("synthetic fsync boundary failure")

    registry = AssetRegistry(tmp_path / "registry", fault_injector=fail)
    with pytest.raises(OSError, match="synthetic fsync boundary failure"):
        _install(registry, package, authorization)

    assert list(registry.staging.iterdir()) == []
    assert not [path for path in registry.objects.rglob("*") if path.is_file()]
    assert not [path for path in registry.references.rglob("*") if path.is_file()]


def test_corrupt_payload_and_installed_object_are_detected(tmp_path: Path) -> None:
    _, package, authorization = _fixture(tmp_path)
    content = bytearray(package.read_bytes())
    content[-1] ^= 1
    corrupt_package = tmp_path / "corrupt.tbxasset"
    corrupt_package.write_bytes(content)
    registry = AssetRegistry(tmp_path / "registry")
    with pytest.raises(AssetIntegrityError, match="staged payload"):
        _install(registry, corrupt_package, authorization)

    _install(registry, package, authorization)
    object_path = next(path for path in registry.objects.rglob("*") if path.is_file())
    object_path.chmod(0o600)
    object_path.write_bytes(b"x" * len(PAYLOAD))
    result = registry.verify(
        asset_id="synthetic-reference", version="v1", authorization=authorization
    )
    assert result.installed
    assert result.integrity_status == IntegrityStatus.INVALID
    assert result.authority_status == "verified"


@pytest.mark.parametrize("mutation", ["truncated", "trailing", "tag"])
def test_partial_trailing_and_tag_substitution_are_rejected(
    tmp_path: Path, mutation: str
) -> None:
    _, package, authorization = _fixture(tmp_path)
    content = package.read_bytes()
    if mutation == "truncated":
        content = content[:-1]
    elif mutation == "trailing":
        content += b"unexpected"
    else:
        content = b"X" + content[1:]
    hostile = tmp_path / f"{mutation}.tbxasset"
    hostile.write_bytes(content)
    registry = AssetRegistry(tmp_path / f"registry-{mutation}")
    with pytest.raises(AssetPackageError):
        _install(registry, hostile, authorization)


def test_archive_features_are_outside_package_language(tmp_path: Path) -> None:
    _, package, authorization = _fixture(tmp_path)
    raw = package.read_bytes()
    header_size = struct.unpack(">I", raw[len(PACKAGE_MAGIC) : len(PACKAGE_MAGIC) + 4])[
        0
    ]
    header_start = len(PACKAGE_MAGIC) + 4
    header = json.loads(raw[header_start : header_start + header_size])
    payload = raw[header_start + header_size :]

    for label, update in (
        ("compression", {"encoding": "gzip"}),
        ("path", {"path": "../../escape"}),
        ("link", {"symlink": "/private/target"}),
    ):
        hostile_header = canonical_json_bytes({**header, **update})
        hostile = tmp_path / f"hostile-{label}.tbxasset"
        hostile.write_bytes(
            PACKAGE_MAGIC
            + struct.pack(">I", len(hostile_header))
            + hostile_header
            + payload
        )
        registry = AssetRegistry(tmp_path / f"registry-{label}")
        with pytest.raises(AssetPackageError, match="header"):
            _install(registry, hostile, authorization)


def test_wrong_version_symlink_and_conflicting_duplicate_are_rejected(
    tmp_path: Path,
) -> None:
    _, package, authorization = _fixture(tmp_path)
    registry = AssetRegistry(tmp_path / "registry")
    with pytest.raises(AssetPackageError, match="identifier/version"):
        registry.install(
            package,
            asset_id="synthetic-reference",
            version="v2",
            authorization=authorization,
        )

    link = tmp_path / "link.tbxasset"
    link.symlink_to(package)
    with pytest.raises(AssetFilesystemError, match="opened safely"):
        _install(registry, link, authorization)

    _install(registry, package, authorization)
    changed_payload = b"different synthetic bytes\n"
    changed = _asset(payload=changed_payload)
    changed_package = build_synthetic_asset_package(
        tmp_path / "changed.tbxasset", reference=changed, payload=changed_payload
    )
    changed_authorization = _authorization(changed)
    with pytest.raises(AssetConflictError, match="registered differently"):
        _install(registry, changed_package, changed_authorization)


def test_unknown_and_revoked_authority_block_before_staging(tmp_path: Path) -> None:
    active = _asset()
    package = build_synthetic_asset_package(
        tmp_path / "active.tbxasset", reference=active, payload=PAYLOAD
    )
    registry = AssetRegistry(tmp_path / "registry")

    with pytest.raises(AssetAuthorityError, match="unknown"):
        _install(registry, package, _authorization(active, head=False))
    assert list(registry.staging.iterdir()) == []

    revoked = _asset(status=AssetStatus.REVOKED)
    revoked_package = build_synthetic_asset_package(
        tmp_path / "revoked.tbxasset", reference=revoked, payload=PAYLOAD
    )
    with pytest.raises(AssetAuthorityError, match="revoked"):
        _install(registry, revoked_package, _authorization(revoked))
    assert list(registry.staging.iterdir()) == []
    assert not [path for path in registry.references.rglob("*") if path.is_file()]


def test_revocation_keeps_historical_byte_integrity_separate(tmp_path: Path) -> None:
    _, package, authorization = _fixture(tmp_path)
    registry = AssetRegistry(tmp_path / "registry")
    _install(registry, package, authorization)

    revoked = _asset(status=AssetStatus.REVOKED)
    result = registry.verify(
        asset_id="synthetic-reference",
        version="v1",
        authorization=_authorization(revoked),
    )

    assert result.installed
    assert result.integrity_status == IntegrityStatus.VALID
    assert result.authority_status == "verified"
    assert result.lifecycle_status == "revoked"
    assert not result.registration_matches_current_reference
    assert result.execution_authorized is False


def test_capacity_floor_boundary_is_checked_under_install_lock(tmp_path: Path) -> None:
    _, package, authorization = _fixture(tmp_path)
    total = 1_000_000
    required = 64 * 1024 + len(PAYLOAD)
    exactly_admitted = AssetRegistry(
        tmp_path / "admitted",
        capacity_provider=lambda _: (total, 200_000 + required),
    )
    assert exactly_admitted.plan_install(package).predicted_free_bytes == 200_000
    assert _install(exactly_admitted, package, authorization).newly_registered

    blocked = AssetRegistry(
        tmp_path / "blocked",
        capacity_provider=lambda _: (total, 200_000 + required - 1),
    )
    assert not blocked.plan_install(package).admitted
    with pytest.raises(AssetCapacityError, match="free-space floor"):
        _install(blocked, package, authorization)
    assert list(blocked.staging.iterdir()) == []


def test_capacity_and_authority_are_rechecked_before_publication(
    tmp_path: Path,
) -> None:
    _, package, authorization = _fixture(tmp_path)
    total = 1_000_000
    calls = 0

    def changing_capacity(_: Path) -> tuple[int, int]:
        nonlocal calls
        calls += 1
        if calls == 1:
            return total, 300_000
        return total, 200_000 + 64 * 1024 - 1

    capacity_registry = AssetRegistry(
        tmp_path / "capacity-registry", capacity_provider=changing_capacity
    )
    with pytest.raises(AssetCapacityError, match="changed during staging"):
        _install(capacity_registry, package, authorization)
    assert not [path for path in capacity_registry.objects.rglob("*") if path.is_file()]
    assert not [
        path for path in capacity_registry.references.rglob("*") if path.is_file()
    ]

    times = iter((NOW, NOW, NOW + timedelta(hours=2)))
    expiring = replace(authorization, now=lambda: next(times))
    authority_registry = AssetRegistry(tmp_path / "authority-registry")
    with pytest.raises(AssetAuthorityError, match="unknown"):
        _install(authority_registry, package, expiring)
    assert not [
        path for path in authority_registry.objects.rglob("*") if path.is_file()
    ]
    assert not [
        path for path in authority_registry.references.rglob("*") if path.is_file()
    ]


def test_registry_rejects_symlink_ancestors_and_fifo_without_blocking(
    tmp_path: Path,
) -> None:
    root = tmp_path / "linked-registry"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "objects").symlink_to(outside, target_is_directory=True)
    with pytest.raises(AssetFilesystemError, match="symlinks"):
        AssetRegistry(root)
    assert list(outside.iterdir()) == []

    fifo = tmp_path / "asset.fifo"
    fifo_registry = tmp_path / "fifo-registry"
    os.mkfifo(fifo)
    script = (
        "from traceback_runner.assets import AssetRegistry, AssetFilesystemError; "
        f"r=AssetRegistry({str(fifo_registry)!r}); "
        "\ntry: r.plan_install(" + repr(str(fifo)) + ")"
        "\nexcept AssetFilesystemError: raise SystemExit(0)"
        "\nraise SystemExit(1)"
    )
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).parents[1],
        timeout=30,
        check=False,
    )
    assert completed.returncode is not None
    assert completed.returncode == 0


def test_missing_install_can_report_valid_authority_without_claiming_install(
    tmp_path: Path,
) -> None:
    reference = _asset()
    authorization = _authorization(reference)
    result = AssetRegistry(tmp_path / "registry").verify(
        asset_id="synthetic-reference", version="v1", authorization=authorization
    )
    assert not result.installed
    assert result.integrity_status == IntegrityStatus.ABSENT
    assert result.authority_status == "verified"
    assert result.lifecycle_status == "active"
    assert result.execution_authorized is False
