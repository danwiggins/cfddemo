"""Stable CLI presentation tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evidence_inspector.result_trust_registry import ResultTrustRegistry
from tests.test_bundles import _bundle
from tests.test_result_catalog_trust_registry import public_result_key
from traceback_runner.cli import ExitCode, main
from traceback_runner.signing import development_trust_bytes


def _registry_args(root: Path, snapshot) -> list[str]:
    return [
        "--trust-registry",
        str(root),
        "--trust-registry-id",
        snapshot.registry_id,
        "--trust-registry-epoch",
        snapshot.registry_epoch_sha256,
        "--trust-registry-head",
        snapshot.state_head_sha256,
    ]


def test_verify_uses_the_current_trust_of_a_result_trust_registry(
    tmp_path: Path, capsys
) -> None:
    bundle, key, _ = _bundle(tmp_path / "bundle")
    root = tmp_path / "trust"
    trust = ResultTrustRegistry(root)
    trust.add_key(public_result_key(key))
    current = trust.current_trust()

    args = ["verify", str(bundle), *_registry_args(root, current), "--json"]
    assert main(args) == ExitCode.OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["data"]["verified"] is True
    assert payload["data"]["trust_registry_state_head_sha256"] == (
        current.state_head_sha256
    )

    trust.revoke_key(key.key_id)
    revoked = trust.current_trust()
    # The retained old head is refused: a revoked key cannot be revived.
    assert main(args) == ExitCode.VERIFICATION_FAILED
    assert "verification_failed" in capsys.readouterr().out
    # With the current head, verification sees the revocation.
    assert (
        main(["verify", str(bundle), *_registry_args(root, revoked), "--json"])
        == ExitCode.VERIFICATION_FAILED
    )
    capsys.readouterr()
    trust.close()


def test_verify_trust_store_path_still_works(tmp_path: Path, capsys) -> None:
    bundle, key, _ = _bundle(tmp_path / "bundle")
    trust_file = tmp_path / "trust.json"
    trust_file.write_bytes(development_trust_bytes(key))
    assert (
        main(["verify", str(bundle), "--trust-store", str(trust_file), "--json"])
        == ExitCode.OK
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["data"] == {
        "verified": True,
        "record_id": payload["data"]["record_id"],
        "development_trust_only": True,
    }


def test_verify_trust_registry_options_are_exact(tmp_path: Path, capsys) -> None:
    bundle, key, _ = _bundle(tmp_path / "bundle")
    trust_file = tmp_path / "trust.json"
    trust_file.write_bytes(development_trust_bytes(key))
    root = tmp_path / "trust"
    trust = ResultTrustRegistry(root)
    trust.add_key(public_result_key(key))
    current = trust.current_trust()
    registry_args = _registry_args(root, current)
    for argv in (
        ["verify", str(bundle)],
        ["verify", str(bundle), "--trust-store", str(trust_file), *registry_args],
        ["verify", str(bundle), *registry_args[:4]],
        ["verify", str(bundle), "--trust-store", str(trust_file), *registry_args[2:]],
    ):
        with pytest.raises(SystemExit) as exited:
            main(argv)
        assert exited.value.code == ExitCode.USAGE
    capsys.readouterr()

    missing = tmp_path / "absent"
    assert (
        main(["verify", str(bundle), *_registry_args(missing, current)])
        == ExitCode.NOT_FOUND
    )
    assert not missing.exists()
    trust.close()


def test_doctor_human_and_json_share_success_semantics(capsys) -> None:
    assert main(["doctor"]) == ExitCode.OK
    human = capsys.readouterr().out
    assert "synthetic local runtime is available" in human
    assert "Real-data execution is not enabled" in human

    assert main(["doctor", "--json"]) == ExitCode.OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "ok"
    assert payload["synthetic_only"] is True
    assert any(item["name"] == "real_data" for item in payload["data"]["checks"])


def test_protocol_cli_withholds_unapproved_instructions(capsys) -> None:
    assert main(["protocol", "show", "--json"]) == ExitCode.OK
    payload = json.loads(capsys.readouterr().out)
    wet_lab = next(
        row for row in payload["data"]["rows"] if row["category"] == "wet-lab"
    )

    assert wet_lab["approval_state"] == "unapproved_synthetic"
    assert wet_lab["content"] == "Instruction withheld pending scientific approval"


def test_real_run_fails_explicitly_without_echoing_input_path(capsys) -> None:
    private_path = "/private/provider/real-sample.bam"
    assert main(["run", private_path, "--json"]) == ExitCode.BLOCKED
    output = capsys.readouterr().out

    assert private_path not in output
    payload = json.loads(output)
    assert payload["status"] == "blocked"
    assert payload["data"]["code"] == "TBX-RUN-003"
