"""`traceback status` reports its trust state and source truthfully (H2-status-min).

Every record here is generated test data labelled unqualified and local.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evidence_inspector.result_trust_registry import ResultTrustRegistry
from traceback_runner import cli
from traceback_runner.fixtures import create_local_golden_path_inputs
from traceback_runner.signing import parse_development_trust_document

_TRUST_FILE = Path("trust/development-result-trust.json")


def _json(capsys: pytest.CaptureFixture[str], *argv: object) -> tuple[int, dict]:
    code = cli.main([*map(str, argv), "--json"])
    return code, json.loads(capsys.readouterr().out)


@pytest.fixture
def completed(tmp_path: Path, capsys) -> tuple[Path, str]:
    """A ROOT holding one complete, signed local record (``traceback run``)."""

    inputs = create_local_golden_path_inputs(tmp_path / "inputs")
    root = tmp_path / "root"
    code, payload = _json(
        capsys, "reference", "register", "--fasta", inputs.fasta_path,
        "--id", "ref", "--root", root,
    )
    assert code == cli.ExitCode.OK, payload
    code, payload = _json(
        capsys, "run", inputs.bam_path, "--reference", "ref", "--root", root
    )
    assert code == cli.ExitCode.OK, payload
    return root, payload["data"]["job_id"]


def _registry_from_development_trust(root: Path, registry_root: Path) -> ResultTrustRegistry:
    """A v2 registry trusting exactly the keys the run wrote to ROOT/trust."""

    document = parse_development_trust_document((root / _TRUST_FILE).read_bytes())
    registry = ResultTrustRegistry(registry_root, create_version=2)
    for key in document.keys:
        registry.add_key(key)
    return registry


def _pins(registry_root: Path, snapshot) -> list[str]:
    return [
        "--trust-registry", str(registry_root),
        "--trust-registry-id", snapshot.registry_id,
        "--trust-registry-epoch", snapshot.registry_epoch_sha256,
        "--trust-registry-head", snapshot.state_head_sha256,
    ]


def _trust(payload: dict) -> tuple[str, str]:
    return payload["data"]["trust_state"], payload["data"]["trust_source"]


def test_registry_revocation_flips_status_without_a_file_edit(
    completed, tmp_path: Path, capsys
) -> None:
    # Acceptance 7: revoking the key in the registry flips status from verified
    # to not verified; the development trust file under ROOT is not touched.
    root, job_id = completed
    trust_file_bytes = (root / _TRUST_FILE).read_bytes()
    registry_root = tmp_path / "result-trust"
    registry = _registry_from_development_trust(root, registry_root)
    try:
        before = registry.current_trust()
        code, payload = _json(capsys, "status", job_id, "--root", root, *_pins(registry_root, before))
        assert code == cli.ExitCode.OK
        assert _trust(payload) == ("verified", "trust_registry")
        assert payload["data"]["operator_state"]["record_availability"] == "ready"
        assert payload["data_origin"] == "local_unqualified"

        for key in parse_development_trust_document(trust_file_bytes).keys:
            registry.revoke_key(key.key_id)
        after = registry.current_trust()

        code, payload = _json(capsys, "status", job_id, "--root", root, *_pins(registry_root, after))
        assert code == cli.ExitCode.OK  # T6: status reports, it does not gate
        assert _trust(payload) == ("not_verified", "trust_registry")
        assert payload["data"]["operator_state"]["record_availability"] == "verifying"
        assert payload["data"]["operator_state"]["category"] != "completed_records"

        # The retained pre-revocation head is refused by the registry, so an old
        # head cannot revive the revoked key either.
        code, payload = _json(capsys, "status", job_id, "--root", root, *_pins(registry_root, before))
        assert code == cli.ExitCode.OK
        assert _trust(payload) == ("not_verified", "trust_registry_error")

        # Without the registry, status names the fixed file it used, which a
        # registry revocation never reaches.
        code, payload = _json(capsys, "status", job_id, "--root", root)
        assert _trust(payload) == ("verified", "development_file")
        assert "registry revocations do not reach" in payload["data"]["trust_hint"]
    finally:
        registry.close()
    assert (root / _TRUST_FILE).read_bytes() == trust_file_bytes


def test_status_is_unknown_when_no_trust_is_available(completed, capsys) -> None:
    root, job_id = completed
    (root / _TRUST_FILE).unlink()
    code, payload = _json(capsys, "status", job_id, "--root", root)
    assert code == cli.ExitCode.OK
    assert _trust(payload) == ("unknown", "none")
    assert payload["data"]["operator_state"]["record_availability"] == "verifying"
    assert "--trust-registry" in payload["data"]["trust_hint"]


def test_unreadable_trust_sources_report_errors_and_never_raise(
    completed, tmp_path: Path, capsys
) -> None:
    root, job_id = completed
    registry_root = tmp_path / "result-trust"
    registry = _registry_from_development_trust(root, registry_root)
    pins = _pins(registry_root, registry.current_trust())
    registry.close()

    missing = tmp_path / "absent"
    missing_pins = [str(missing) if item == str(registry_root) else item for item in pins]
    code, payload = _json(capsys, "status", job_id, "--root", root, *missing_pins)
    assert code == cli.ExitCode.OK
    assert _trust(payload) == ("not_verified", "trust_registry_error")
    assert not missing.exists()  # a read-only command never creates a registry

    (registry_root / "registry-journal.jsonl").write_bytes(b"not a journal\n")
    code, payload = _json(capsys, "status", job_id, "--root", root, *pins)
    assert code == cli.ExitCode.OK
    assert _trust(payload) == ("not_verified", "trust_registry_error")

    (root / _TRUST_FILE).write_bytes(b"{")
    code, payload = _json(capsys, "status", job_id, "--root", root)
    assert code == cli.ExitCode.OK
    assert _trust(payload) == ("not_verified", "development_file_error")


def test_a_job_without_a_complete_record_reports_no_record(completed, capsys) -> None:
    import sqlite3

    root, job_id = completed
    with sqlite3.connect(root / "runner" / "runner.sqlite3") as db:
        db.execute("UPDATE jobs SET state='paused'")
    code, payload = _json(capsys, "status", job_id, "--root", root)
    assert code == cli.ExitCode.OK
    assert _trust(payload) == ("not_verified", "no_record")


def test_status_trust_registry_options_are_exact(completed, capsys) -> None:
    root, job_id = completed
    for extra in (
        ["--trust-registry", "x"],
        ["--trust-registry-id", "x"],
        ["--trust-registry", "x", "--trust-registry-id", "a", "--trust-registry-epoch", "b"],
    ):
        with pytest.raises(SystemExit) as exited:
            cli.main(["status", job_id, "--root", str(root), *extra])
        assert exited.value.code == cli.ExitCode.USAGE
    capsys.readouterr()


def test_status_text_output_prints_the_trust_words(completed, capsys) -> None:
    root, job_id = completed
    assert cli.main(["status", job_id, "--root", str(root)]) == cli.ExitCode.OK
    lines = capsys.readouterr().out.splitlines()
    assert "TRUST_STATE  verified" in lines
    assert "TRUST_SOURCE  development_file" in lines
