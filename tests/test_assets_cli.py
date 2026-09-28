"""CLI integration tests for authority-bound offline synthetic assets."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from traceback_runner.assets import build_synthetic_asset_package
from traceback_runner.cli import ExitCode, main
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
    development_trust_bytes,
    generate_development_keypair,
)

PAYLOAD = b"tiny CLI synthetic asset bytes\n"
ASSET_ID = "synthetic-cli-reference"
VERSION = "v1"


def _invoke(capsys, *argv: object) -> tuple[int, dict[str, object]]:
    code = main([*(str(value) for value in argv), "--json"])
    return code, json.loads(capsys.readouterr().out)


def _reference() -> AssetReference:
    return AssetReference(
        content=AssetContentIdentity(
            asset_id=ASSET_ID,
            version=VERSION,
            kind=AssetKind.REFERENCE,
            content_sha256=hashlib.sha256(PAYLOAD).hexdigest(),
            content_size_bytes=len(PAYLOAD),
        ),
        provenance=AssetProvenance(
            source_authority="Synthetic CLI fixture authority only.",
            license_id="synthetic-only",
        ),
        lifecycle=AssetLifecycle(status=AssetStatus.ACTIVE),
    )


def _release(reference: AssetReference):
    profile = WorkstationProfile(
        profile_id="synthetic-cli-host",
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
        protocol_id="synthetic-cli-protocol",
        version="v1",
        document_sha256="2" * 64,
        reported_approval_status=ProtocolApprovalStatus.PENDING,
    )
    evidence = QualificationEvidenceManifest(
        manifest_id="synthetic-cli-evidence",
        version="v1",
        items=tuple(
            QualificationEvidenceItem(requirement=item, status=EvidenceStatus.UNKNOWN)
            for item in EvidenceRequirementKind
        ),
    )
    return build_release_evidence_package(
        release_id="synthetic-cli-release",
        version="v1",
        workflow_release_sha256="3" * 64,
        workstation_profile=profile,
        protocol_reference=protocol,
        evidence_manifest=evidence,
        assets=(reference,),
    )


def _write_inputs(
    tmp_path: Path,
    *,
    expired_head: bool = False,
    untrusted: bool = False,
    ambiguous_binding: bool = False,
) -> dict[str, Path]:
    now = datetime.now(UTC)
    reference = _reference()
    release = _release(reference)
    binding = qualification_binding(release)
    key = generate_development_keypair(KeyPurpose.RELEASE)
    envelope = sign_development_release_evidence(
        release, key, signer_role=ApproverRole.RELEASE_REVIEWER
    )
    package_digest = domain_digest(DigestDomain.RELEASE_EVIDENCE, release)
    grants = [
        SignerRoleGrant(
            scope=AuthorityScope.RELEASE_EVIDENCE,
            role=ApproverRole.RELEASE_REVIEWER,
            key_id=key.key_id,
            binding=binding,
            valid_from=now - timedelta(hours=1),
            expires_at=now + timedelta(days=1),
            status=GrantStatus.ACTIVE,
        )
    ]
    if ambiguous_binding:
        grants.append(
            SignerRoleGrant(
                scope=AuthorityScope.RELEASE_EVIDENCE,
                role=ApproverRole.RELEASE_REVIEWER,
                key_id="zz-alternate-development-key",
                binding=binding.model_copy(
                    update={"workstation_profile_id": "other-synthetic-host"}
                ),
                valid_from=now - timedelta(hours=1),
                expires_at=now + timedelta(days=1),
                status=GrantStatus.ACTIVE,
            )
        )
    policy = QualificationTrustPolicy(
        policy_id="synthetic-cli-policy",
        version="v1",
        issued_at=now - timedelta(hours=1),
        expires_at=now + timedelta(days=1),
        grants=tuple(
            sorted(
                grants,
                key=lambda grant: (
                    grant.scope.value,
                    grant.role.value,
                    grant.key_id,
                    grant.binding.release_evidence_sha256,
                ),
            )
        ),
    )
    head = ReleaseAuthorityHead(
        release_id=release.release_id,
        release_version=release.version,
        package_sha256=package_digest,
        as_of=now - timedelta(hours=2 if expired_head else 1),
        expires_at=now - timedelta(hours=1) if expired_head else now + timedelta(hours=1),
    )

    authority = tmp_path / "independent-authority"
    packages = tmp_path / "untrusted-packages"
    authority.mkdir()
    packages.mkdir()
    paths = {
        "release_evidence": authority / "release-envelope.json",
        "trust_store": authority / "public-trust.json",
        "role_policy": authority / "role-policy.json",
        "authority_head": authority / "authority-head.json",
        "package": packages / "asset.tbxasset",
    }
    paths["release_evidence"].write_bytes(canonical_json_bytes(envelope))
    paths["trust_store"].write_bytes(
        development_trust_bytes() if untrusted else development_trust_bytes(key)
    )
    paths["role_policy"].write_bytes(canonical_json_bytes(policy))
    paths["authority_head"].write_bytes(canonical_json_bytes(head))
    build_synthetic_asset_package(
        paths["package"], reference=reference, payload=PAYLOAD
    )
    return paths


def _asset_args(paths: dict[str, Path], root: Path) -> tuple[object, ...]:
    return (
        "--release-evidence",
        paths["release_evidence"],
        "--trust-store",
        paths["trust_store"],
        "--role-policy",
        paths["role_policy"],
        "--authority-head",
        paths["authority_head"],
        "--asset",
        ASSET_ID,
        "--version",
        VERSION,
        "--root",
        root,
    )


def test_assets_install_and_verify_are_offline_path_free_and_not_authority_to_run(
    tmp_path: Path, capsys
) -> None:
    paths = _write_inputs(tmp_path)
    root = tmp_path / "product-root"

    code, installed = _invoke(
        capsys,
        "assets",
        "install",
        *_asset_args(paths, root),
        "--package",
        paths["package"],
    )
    assert code == ExitCode.OK
    data = installed["data"]
    assert data["installed"] is True
    assert data["integrity"] == "valid"
    assert data["authority"] == "verified"
    assert data["lifecycle"] == "active"
    assert data["fresh_until"] is not None
    assert data["execution_authorized"] is False
    assert data["real_data_authorized"] is False
    assert data["qualification_probe_authorized"] is False
    assert str(tmp_path) not in json.dumps(installed)

    code, verified = _invoke(
        capsys, "assets", "verify", *_asset_args(paths, root)
    )
    assert code == ExitCode.OK
    assert verified["data"]["integrity"] == "valid"
    assert verified["data"]["authority"] == "verified"
    assert str(tmp_path) not in json.dumps(verified)


def test_corrupt_installed_asset_is_verification_failure(
    tmp_path: Path, capsys
) -> None:
    paths = _write_inputs(tmp_path)
    root = tmp_path / "product-root"
    code, _ = _invoke(
        capsys,
        "assets",
        "install",
        *_asset_args(paths, root),
        "--package",
        paths["package"],
    )
    assert code == ExitCode.OK
    digest = hashlib.sha256(PAYLOAD).hexdigest()
    object_path = root / "assets" / "objects" / "sha256" / digest[:2] / digest
    object_path.chmod(0o600)
    object_path.write_bytes(b"tampered")

    code, result = _invoke(
        capsys, "assets", "verify", *_asset_args(paths, root)
    )
    assert code == ExitCode.VERIFICATION_FAILED
    assert result["data"]["integrity"] == "invalid"
    assert str(tmp_path) not in json.dumps(result)


def test_expired_authority_blocks_install_before_registry_mutation(
    tmp_path: Path, capsys
) -> None:
    paths = _write_inputs(tmp_path, expired_head=True)
    root = tmp_path / "product-root"

    code, result = _invoke(
        capsys,
        "assets",
        "install",
        *_asset_args(paths, root),
        "--package",
        paths["package"],
    )
    assert code == ExitCode.BLOCKED
    assert result["data"]["authority"] == "unknown"
    assert result["data"]["integrity"] == "absent"
    assert not (root / "assets").exists()


def test_invalid_trust_and_ambiguous_policy_fail_closed_without_paths(
    tmp_path: Path, capsys
) -> None:
    for label, options in (
        ("untrusted", {"untrusted": True}),
        ("ambiguous", {"ambiguous_binding": True}),
    ):
        case = tmp_path / label
        case.mkdir()
        paths = _write_inputs(case, **options)
        root = case / "product-root"
        code, result = _invoke(
            capsys,
            "assets",
            "install",
            *_asset_args(paths, root),
            "--package",
            paths["package"],
        )
        assert code == ExitCode.VERIFICATION_FAILED
        assert not (root / "assets").exists()
        assert str(case) not in json.dumps(result)


def test_verify_keeps_integrity_distinct_when_authority_later_expires(
    tmp_path: Path, capsys
) -> None:
    paths = _write_inputs(tmp_path)
    root = tmp_path / "product-root"
    code, _ = _invoke(
        capsys,
        "assets",
        "install",
        *_asset_args(paths, root),
        "--package",
        paths["package"],
    )
    assert code == ExitCode.OK

    current_head = json.loads(paths["authority_head"].read_bytes())
    now = datetime.now(UTC)
    current_head["as_of"] = (now - timedelta(hours=2)).isoformat()
    current_head["expires_at"] = (now - timedelta(hours=1)).isoformat()
    from traceback_runner.qualification import ReleaseAuthorityHead

    expired = ReleaseAuthorityHead.model_validate(current_head)
    paths["authority_head"].write_bytes(canonical_json_bytes(expired))
    code, result = _invoke(
        capsys, "assets", "verify", *_asset_args(paths, root)
    )
    assert code == ExitCode.BLOCKED
    assert result["data"]["installed"] is True
    assert result["data"]["integrity"] == "valid"
    assert result["data"]["authority"] == "unknown"
