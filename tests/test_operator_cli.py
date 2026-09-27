"""Stable CLI presentation tests."""

from __future__ import annotations

import json

from traceback_runner.cli import ExitCode, main


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
