"""`traceback run` on a generated local BAM (golden-path B4).

Every record here is generated test data labelled unqualified and local.
"""

from __future__ import annotations

import errno
import hashlib
import gc
import json
import re
import stat
from pathlib import Path

import pytest

from traceback_runner import cli
from traceback_runner.bundles import verify_bundle
from traceback_runner.contracts import ApprovalState, JobState
from traceback_runner.export import LOCAL_REPORT_BANNER
from traceback_runner.fixtures import (
    LocalHeaderDigests,
    create_local_golden_path_inputs,
    synthetic_registered_reference,
)
from evidence_inspector.method_registry import method_definition_sha256
from traceback_runner.local_authority import (
    local_fragment_policy,
    local_method_definition,
    local_method_identity,
)
from traceback_runner.contracts import ReferenceContig, RegisteredReference
from traceback_runner.runner import Runner
from traceback_runner.signing import (
    TrustNamespace,
    load_development_trust,
    parse_development_trust_document,
)
from traceback_runner.store import JobStore

FROZEN_DEMO = Path(__file__).parent / "fixtures" / "cli" / "demo-output.v1.json"


def _json(capsys: pytest.CaptureFixture[str], *argv: object) -> tuple[int, dict]:
    code = cli.main([*map(str, argv), "--json"])
    return code, json.loads(capsys.readouterr().out)


@pytest.fixture
def inputs(tmp_path: Path):
    return create_local_golden_path_inputs(tmp_path / "inputs")


def _register(capsys, root: Path, fasta: Path) -> None:
    code, payload = _json(capsys, "reference", "register", "--fasta", fasta, "--id", "ref", "--root", root)
    assert code == cli.ExitCode.OK, payload


def _run(capsys, root: Path, bam: Path) -> tuple[int, dict]:
    return _json(capsys, "run", bam, "--reference", "ref", "--root", root)


def _records(root: Path) -> list[Path]:
    records = root / "records"
    return sorted(path for path in records.iterdir() if not path.name.startswith(".")) if records.exists() else []


def _job_ids(root: Path) -> list[str]:
    import sqlite3

    with sqlite3.connect(root / "runner" / "runner.sqlite3") as connection:
        return [row[0] for row in connection.execute("SELECT job_id FROM jobs")]


def test_run_signs_verifies_and_labels_a_local_record(tmp_path: Path, inputs, capsys) -> None:
    root = tmp_path / "root"
    _register(capsys, root, inputs.fasta_path)
    code, payload = _run(capsys, root, inputs.bam_path)

    assert code == cli.ExitCode.OK, payload
    assert payload["schema_version"] == "traceback.cli-result.v2"
    assert payload["data_origin"] == "local_unqualified"
    assert "synthetic_only" not in payload
    data = payload["data"]
    assert data["eligible_alignments"] == inputs.expected_eligible_alignments
    assert data["records_scanned"] == inputs.reads
    assert data["preflight_outcome"] == "partial"
    assert data["reference_match"] == "name_and_length_only"
    assert data["qualified"] is False
    bundle = Path(data["bundle_path"])
    assert data["bundle"] == f"records/{data['record_id']}"
    assert bundle.is_absolute() and bundle == (root / data["bundle"]).absolute()
    assert Path(data["trust_store_path"]) == (root / "trust/development-result-trust.json").absolute()

    verified = verify_bundle(bundle, load_development_trust(Path(data["trust_store_path"]).read_bytes()))
    assert verified.manifest.schema_version == "traceback.result-bundle.v3"
    assert verified.measurement.approval_state == ApprovalState.UNAPPROVED_LOCAL
    assert verified.signature.namespace == TrustNamespace.DEVELOPMENT_LOCAL
    assert verified.manifest.method == local_method_identity(_registered(root))
    report = (bundle / "report.html").read_text()
    assert LOCAL_REPORT_BANNER in report
    assert "synthetic" not in report.lower()

    # The trust document now holds the development-local key (v2).
    document = parse_development_trust_document(Path(data["trust_store_path"]).read_bytes())
    assert document.schema_version == "traceback.development-trust.v2"

    code, verify = _json(capsys, "verify", bundle, "--trust-store", data["trust_store_path"])
    assert code == cli.ExitCode.OK and verify["data"]["verified"] is True
    assert verify["data_origin"] == "local_unqualified"
    code, by_id = _json(capsys, "verify", data["record_id"], "--root", root)
    assert code == cli.ExitCode.OK and by_id["data"]["record_id"] == data["record_id"]

    code, status = _json(capsys, "status", data["job_id"], "--root", root)
    assert code == cli.ExitCode.OK
    assert status["data"]["operator_state"]["state"] == "complete"
    assert status["data"]["operator_state"]["category"] == "completed_records"
    assert status["data_origin"] == "local_unqualified"


def _registered(root: Path):
    from traceback_runner.references import load_reference

    return load_reference(root, "ref").registered


def test_rerun_returns_the_existing_record(tmp_path: Path, inputs, capsys) -> None:
    root = tmp_path / "root"
    _register(capsys, root, inputs.fasta_path)
    _, first = _run(capsys, root, inputs.bam_path)
    code, second = _run(capsys, root, inputs.bam_path)
    assert code == cli.ExitCode.OK
    for key in ("job_id", "record_id", "bundle"):
        assert second["data"][key] == first["data"][key]
    assert len(_records(root)) == 1


def test_blocked_preflight_fails_the_job_with_tbx_bam_002_and_no_record(
    tmp_path: Path, capsys
) -> None:
    root = tmp_path / "root"
    good = create_local_golden_path_inputs(tmp_path / "good")
    wrong = create_local_golden_path_inputs(
        tmp_path / "wrong", header_digests=LocalHeaderDigests.WRONG_M5
    )
    _register(capsys, root, good.fasta_path)
    code, payload = _run(capsys, root, wrong.bam_path)
    assert code == cli.ExitCode.BLOCKED
    assert payload["data"]["code"] == "TBX-BAM-002"
    assert set(payload["data"]) >= {"code", "cause", "fix", "retryable", "docs"}
    assert payload["data_origin"] == "local_unqualified"
    assert _records(root) == []
    (job_id,) = _job_ids(root)
    assert JobStore(root / "runner" / "runner.sqlite3").get(job_id).state == JobState.TERMINAL_FAILURE
    # Re-running the same input reports the same refusal without re-running.
    code, again = _run(capsys, root, wrong.bam_path)
    assert code == cli.ExitCode.BLOCKED and again["data"]["code"] == "TBX-BAM-002"


def test_zero_eligible_alignments_is_tbx_run_005_and_no_record(tmp_path: Path, capsys) -> None:
    root = tmp_path / "root"
    empty = create_local_golden_path_inputs(tmp_path / "inputs", eligible=False)
    _register(capsys, root, empty.fasta_path)
    code, payload = _run(capsys, root, empty.bam_path)
    assert code == cli.ExitCode.BLOCKED
    assert payload["data"]["code"] == "TBX-RUN-005"
    assert payload["data"]["retryable"] is False
    assert "MAPQ 20" in payload["data"]["fix"]
    assert _records(root) == []
    (job_id,) = _job_ids(root)
    assert JobStore(root / "runner" / "runner.sqlite3").get(job_id).state == JobState.TERMINAL_FAILURE


def test_insufficient_space_is_tbx_run_004_before_any_copy(
    tmp_path: Path, inputs, capsys, monkeypatch
) -> None:
    root = tmp_path / "root"
    _register(capsys, root, inputs.fasta_path)
    size = inputs.bam_path.stat().st_size + inputs.index_path.stat().st_size
    usage = type("Usage", (), {"total": 10**12, "used": 0, "free": 2 * size - 1})()
    monkeypatch.setattr(cli.shutil, "disk_usage", lambda path: usage)
    code, payload = _run(capsys, root, inputs.bam_path)
    assert code == cli.ExitCode.BLOCKED
    assert payload["data"]["code"] == "TBX-RUN-004"
    assert payload["data"]["required_bytes"] == 2 * size
    assert payload["data"]["available_bytes"] == 2 * size - 1
    assert payload["data"]["retryable"] is True
    assert not (root / "runner").exists()


def test_disk_full_while_sealing_maps_to_tbx_run_004(
    tmp_path: Path, inputs, capsys, monkeypatch
) -> None:
    from traceback_runner import snapshots

    root = tmp_path / "root"
    _register(capsys, root, inputs.fasta_path)

    def full(*args, **kwargs):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(snapshots, "_copy_and_hash", full)
    code, payload = _run(capsys, root, inputs.bam_path)
    assert code == cli.ExitCode.BLOCKED
    assert payload["data"]["code"] == "TBX-RUN-004"
    assert payload["data"]["retryable"] is False
    assert _records(root) == []
    # The unsealed job cannot be resealed on this ROOT; a rerun says so
    # instead of failing terminally on a missing snapshot.
    monkeypatch.undo()
    code, again = _run(capsys, root, inputs.bam_path)
    assert code == cli.ExitCode.BLOCKED and again["data"]["code"] == "TBX-RUN-004"
    (job_id,) = _job_ids(root)
    assert JobStore(root / "runner" / "runner.sqlite3").get(job_id).state == JobState.RETRYABLE_FAILURE
    # retry + resume of the unsealed job is refused the same way.
    assert cli.main(["retry", job_id, "--root", str(root), "--json"]) == cli.ExitCode.OK
    capsys.readouterr()
    code, resumed = _json(capsys, "resume", job_id, "--root", root)
    assert code == cli.ExitCode.BLOCKED and resumed["data"]["code"] == "TBX-RUN-004"
    code, again = _run(capsys, root, inputs.bam_path)
    assert code == cli.ExitCode.BLOCKED and again["data"]["code"] == "TBX-RUN-004"


def test_shared_readable_provenance_key_is_refused(tmp_path: Path, inputs, capsys) -> None:
    root = tmp_path / "root"
    _register(capsys, root, inputs.fasta_path)
    key = root / "trust" / "provenance-hmac.key"
    key.parent.mkdir(parents=True, exist_ok=True)
    key.write_bytes(b"k" * 32)
    key.chmod(0o644)
    code, payload = _run(capsys, root, inputs.bam_path)
    assert code == cli.ExitCode.BLOCKED and payload["data"]["code"] == "TBX-RUN-006"
    assert _records(root) == []


def test_local_job_status_headline_is_not_synthetic() -> None:
    from datetime import UTC, datetime

    from traceback_runner.operator import build_job_view

    for state in (JobState.RUNNING, JobState.QUEUED):
        view = build_job_view(
            job_id="job", state=state, observed_at=datetime.now(UTC), local_unqualified=True
        )
        assert "synthetic" not in view.headline.lower()
        assert "Local unqualified job" in view.headline


def test_a_second_run_on_one_root_is_operator_busy(tmp_path: Path, inputs, capsys) -> None:
    root = tmp_path / "root"
    _register(capsys, root, inputs.fasta_path)
    with cli._operator_lock(root):
        code, payload = _run(capsys, root, inputs.bam_path)
    assert code == cli.ExitCode.BLOCKED
    assert payload["status"] == "blocked"
    assert "A local action or unexpired worker lease is active" in payload["summary"]
    assert not (root / "runner").exists()


def test_two_roots_give_unlinkable_provider_commitments(tmp_path: Path, inputs, capsys) -> None:
    commitments = []
    for name in ("one", "two"):
        root = tmp_path / name
        _register(capsys, root, inputs.fasta_path)
        code, payload = _run(capsys, root, inputs.bam_path)
        assert code == cli.ExitCode.OK
        provenance = json.loads((Path(payload["data"]["bundle_path"]) / "provenance.json").read_text())
        commitments.append(provenance["artifacts"][0]["provider_hmac_sha256"])
        key = root / "trust" / "provenance-hmac.key"
        assert stat.S_IMODE(key.stat().st_mode) == 0o600 and key.stat().st_size == 32
    assert commitments[0] != commitments[1]


def test_tampered_provenance_key_is_refused(tmp_path: Path, inputs, capsys) -> None:
    root = tmp_path / "root"
    _register(capsys, root, inputs.fasta_path)
    key = root / "trust" / "provenance-hmac.key"
    key.parent.mkdir(parents=True, exist_ok=True)
    key.write_bytes(b"short")
    code, payload = _run(capsys, root, inputs.bam_path)
    assert code != cli.ExitCode.OK
    assert _records(root) == []


def test_demo_output_is_byte_identical_to_the_frozen_pre_change_output(
    tmp_path: Path, capsys
) -> None:
    assert cli.main(["demo", "--root", str(tmp_path / "root"), "--json"]) == cli.ExitCode.OK
    output = capsys.readouterr().out
    normalized = re.sub(r'"job_id":"[0-9a-f]{32}"', '"job_id":"<JOB_ID>"', output)
    normalized = re.sub(r"dev-result-[0-9a-f]{24}", "dev-result-<KEY>", normalized)
    assert normalized == FROZEN_DEMO.read_text()
    # An all-synthetic trust document stays v1.
    trust = tmp_path / "root" / "trust" / "development-result-trust.json"
    assert parse_development_trust_document(trust.read_bytes()).schema_version == (
        "traceback.development-trust.v1"
    )


def test_demo_and_run_share_one_root_and_both_verify(tmp_path: Path, inputs, capsys) -> None:
    root = tmp_path / "root"
    assert cli.main(["demo", "--root", str(root), "--json"]) == cli.ExitCode.OK
    demo = json.loads(capsys.readouterr().out)
    _register(capsys, root, inputs.fasta_path)
    code, run = _run(capsys, root, inputs.bam_path)
    assert code == cli.ExitCode.OK
    trust = root / "trust" / "development-result-trust.json"
    document = parse_development_trust_document(trust.read_bytes())
    assert {key.namespace for key in document.keys} == {
        TrustNamespace.DEVELOPMENT_SYNTHETIC,
        TrustNamespace.DEVELOPMENT_LOCAL,
    }
    for bundle in (root / demo["data"]["bundle"], root / run["data"]["bundle"]):
        assert cli.main(["verify", str(bundle), "--trust-store", str(trust)]) == cli.ExitCode.OK
    capsys.readouterr()


def test_pause_between_stages_then_resume_completes(
    tmp_path: Path, inputs, capsys, monkeypatch
) -> None:
    root = tmp_path / "root"
    _register(capsys, root, inputs.fasta_path)
    original = cli._local_stages

    def pausing(*args, **kwargs):
        stages = list(original(*args, **kwargs))
        first = stages[0]

        def callback(context):
            result = first.callback(context)
            cli._existing_runner(root).request_pause(context.job_id)
            return result

        stages[0] = type(first)(
            name=first.name, version=first.version, callback=callback, parameters=first.parameters
        )
        return tuple(stages)

    monkeypatch.setattr(cli, "_local_stages", pausing)
    code, paused = _run(capsys, root, inputs.bam_path)
    assert code == cli.ExitCode.OK
    assert paused["data"]["state"] == "paused"
    assert _records(root) == []
    monkeypatch.setattr(cli, "_local_stages", original)

    # A damaged local method authority refuses the resume before any signing.
    registry_file = root / "authority" / "ref" / "method-registry.json"
    good = registry_file.read_bytes()
    registry_file.write_bytes(good.replace(b"registry_traceback_local", b"registry_traceback_locax"))
    code, refused = _json(capsys, "resume", paused["data"]["job_id"], "--root", root)
    assert code == cli.ExitCode.BLOCKED, refused
    assert refused["data"]["code"] == "TBX-AUTH-LOCAL-001"
    assert _records(root) == []
    registry_file.write_bytes(good)

    code, resumed = _json(capsys, "resume", paused["data"]["job_id"], "--root", root)
    assert code == cli.ExitCode.OK, resumed
    assert resumed["data_origin"] == "local_unqualified"
    assert resumed["data"]["state"] == "complete"
    assert len(_records(root)) == 1
    code, verify = _json(capsys, "verify", resumed["data"]["record_id"], "--root", root)
    assert code == cli.ExitCode.OK and verify["data"]["verified"] is True


def test_input_locators_never_leave_the_operator(tmp_path: Path, inputs, capsys) -> None:
    root = tmp_path / "root"
    _register(capsys, root, inputs.fasta_path)
    outputs = [capsys.readouterr().out]
    for argv in (
        ["preflight", inputs.bam_path, "--reference", "ref", "--root", root],
        ["run", inputs.bam_path, "--reference", "ref", "--root", root],
        ["run", inputs.bam_path, "--reference", "ref", "--root", root, "--json"],
    ):
        cli.main([str(item) for item in argv])
        outputs.append(capsys.readouterr().out)
    job_id = _job_ids(root)[0]
    for command in ("status", "logs"):
        cli.main([command, job_id, "--root", str(root), "--json"])
        outputs.append(capsys.readouterr().out)
    (record,) = _records(root)
    record_bytes = b"".join(path.read_bytes() for path in record.rglob("*") if path.is_file())
    locators = {
        str(inputs.bam_path.absolute()),
        str(inputs.fasta_path.absolute()),
        str(inputs.bam_path.parent.absolute()),
    }
    for locator in locators:
        assert all(locator not in text for text in outputs)
        assert locator.encode() not in record_bytes
    # The reference's assembly label (its ID when --assembly was not given) is
    # never presented as an assembly in the record.
    assert b"assembly" not in record_bytes
    # Human mode shows the locked policy and stage lines before the result.
    human = outputs[2]
    assert "POLICY  aligned-reference-span-local-v2.ref" in human
    assert "STAGE  measure:" in human
    assert "traceback verify" in human


def test_run_without_reference_keeps_tbx_run_003_and_unknown_reference_is_refused(
    tmp_path: Path, inputs, capsys
) -> None:
    code, payload = _json(capsys, "run", inputs.bam_path, "--root", tmp_path / "root")
    assert code == cli.ExitCode.BLOCKED and payload["data"]["code"] == "TBX-RUN-003"
    code, payload = _json(capsys, "run", inputs.bam_path, "--reference", "absent", "--root", tmp_path / "root")
    assert code != cli.ExitCode.OK
    assert payload["data"]["code"].startswith("TBX-REF-")
    assert not (tmp_path / "root" / "runner").exists()


def test_local_policy_selects_primary_contigs_or_all() -> None:
    def reference(*names: str) -> RegisteredReference:
        return RegisteredReference(
            reference_id="hg-local",
            assembly="hg-local",
            asset_sha256="a" * 64,
            contigs=tuple(ReferenceContig(name=name, length=1000, md5="b" * 32) for name in names),
        )

    hg = reference("chr1", "chr2", "chrX", "chrY", "chrM", "chr1_KI270706v1_random", "chrEBV")
    policy = local_fragment_policy(hg)
    assert policy.contigs == ("chr1", "chr2", "chrX", "chrY")
    assert policy.min_mapping_quality == 20
    assert policy.definition_id == "aligned-reference-span-local-v2.hg-local"
    assert [(b.lower_inclusive, b.upper_exclusive) for b in policy.bins] == [
        (0, 100), (100, 150), (150, 200), (200, 300), (300, 500), (500, 1000), (1000, None)
    ]
    assert local_fragment_policy(reference("tiny_a", "tiny_b")).contigs == ("tiny_a", "tiny_b")
    identity = local_method_identity(hg)
    assert identity.version == "1.0.0-local-hg-local"
    # The bundle carries exactly the E01 definition digest the local authority
    # registers, so catalog import can bind the record to it.
    assert identity.method_definition_sha256 == method_definition_sha256(
        local_method_definition(hg)
    )
    other = local_method_identity(synthetic_registered_reference())
    assert other.version != identity.version
    assert other.method_definition_sha256 != identity.method_definition_sha256


def test_runner_executes_one_origin_and_labels_transitions(tmp_path: Path, inputs, capsys) -> None:
    with pytest.raises(ValueError, match="one data origin"):
        Runner(tmp_path / "both", synthetic_enabled=True, local_unqualified_enabled=True)
    root = tmp_path / "root"
    _register(capsys, root, inputs.fasta_path)
    _, payload = _run(capsys, root, inputs.bam_path)
    reasons = [
        event["reason"]
        for event in JobStore(root / "runner" / "runner.sqlite3").audit(payload["data"]["job_id"])
    ]
    assert "local unqualified execution started" in reasons
    assert "local unqualified workflow complete" in reasons
    assert not any("synthetic" in str(reason) for reason in reasons)


def test_store_lifetime_anchor_keeps_wal_identity_across_another_store_closing(
    tmp_path: Path, inputs, capsys
) -> None:
    """Deterministic form of a CI failure: a second store (for example
    `traceback pause` in another process) closing last deleted -wal/-shm, so
    the first store's pinned sidecar identity no longer matched the files
    SQLite recreated. Every store now holds an anchor for its lifetime."""

    root = tmp_path / "root"
    _register(capsys, root, inputs.fasta_path)
    _, payload = _run(capsys, root, inputs.bam_path)
    database = root / "runner" / "runner.sqlite3"
    wal = Path(f"{database}-wal")
    job_id = payload["data"]["job_id"]

    # Stores release their anchor when garbage-collected, so collect explicitly
    # to make the "last close" deterministic.
    first = JobStore(database)
    before = wal.stat().st_ino
    JobStore(database).get(job_id)  # another store opens and closes
    gc.collect()
    assert wal.exists() and wal.stat().st_ino == before
    first.get(job_id)  # the pinned identity still matches
    with first.journal_anchor(), first.journal_anchor():  # compatibility no-op
        first.get(job_id)

    # Control: once no store is alive, the last close removes the WAL.
    first.close()
    gc.collect()
    assert not wal.exists()


def _fake_caffeinate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Stand in for /usr/bin/caffeinate: record argv and pid, then wait to be killed."""

    from traceback_runner import awake

    log = tmp_path / "caffeinate"
    log.mkdir()
    script = log / "caffeinate"
    script.write_text(
        "#!/bin/sh\n"
        f'echo "$@" > "{log}/argv"\n'
        f'echo $$ > "{log}/pid"\n'
        "exec sleep 600\n"
    )
    script.chmod(0o755)
    monkeypatch.setattr(awake, "CAFFEINATE", script)
    # Patch only the platform check, never the global sys module: faking
    # sys.platform process-wide sends Linux down macOS-only code paths.
    monkeypatch.setattr(awake, "_assertion_available", lambda: True)
    return log


def _alive(pid: int) -> bool:
    import os

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_run_keeps_the_host_awake_while_stages_hold_the_lease(
    tmp_path: Path, inputs, capsys, monkeypatch
) -> None:
    # Regression (TBX-JOB-001 on the real 2 GB BAM): the worker lease is wall
    # clock, so a host that idles to sleep mid-stage wakes with an expired
    # lease and the run ends without a record.  The run must hold a sleep
    # assertion, tied to its own pid, for as long as the runner executes.
    import os
    import time

    log = _fake_caffeinate(tmp_path, monkeypatch)
    root = tmp_path / "root"
    _register(capsys, root, inputs.fasta_path)
    seen: list[tuple[list[str], bool]] = []
    original = Runner.execute

    def execute(self, *args, **kwargs):
        deadline = time.monotonic() + 10
        while not (log / "pid").exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        pid = int((log / "pid").read_text())
        seen.append(((log / "argv").read_text().split(), _alive(pid)))
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Runner, "execute", execute)
    code, payload = _run(capsys, root, inputs.bam_path)
    assert code == cli.ExitCode.OK, payload
    assert seen == [(["-i", "-s", "-w", str(os.getpid())], True)]
    # Released when the command ends, not left running after it.
    pid = int((log / "pid").read_text())
    deadline = time.monotonic() + 5
    while _alive(pid) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not _alive(pid)


def test_a_lost_lease_names_the_lease_and_resume(
    tmp_path: Path, inputs, capsys, monkeypatch
) -> None:
    from traceback_runner.store import StaleLease

    root = tmp_path / "root"
    _register(capsys, root, inputs.fasta_path)

    def lost(self, *args, **kwargs):
        raise StaleLease("lease expired or was superseded")

    monkeypatch.setattr(Runner, "execute", lost)
    code, payload = _run(capsys, root, inputs.bam_path)
    assert code == cli.ExitCode.RETRYABLE_FAILURE
    assert payload["status"] == "retryable_failure"
    assert "worker lease" in payload["summary"]
    (job_id,) = _job_ids(root)
    data = payload["data"]
    assert (data["code"], data["retryable"], data["job_id"]) == ("TBX-JOB-001", True, job_id)
    assert data["next_action"] == f"traceback resume {job_id} --root <same-root>"
    assert _records(root) == []


# --- B1: the job key names the method ----------------------------------------

_LEGACY_LOCAL_WORKFLOW_SHA256 = hashlib.sha256(b"local-unqualified-v0").hexdigest()


def _request(root: Path, job_id: str):
    with JobStore(root / "runner" / "runner.sqlite3") as store:
        return store.request(job_id)


def test_local_workflow_hash_names_the_method_and_recognition_ignores_it() -> None:
    from traceback_runner.contracts import InputKind, JobRequest

    method = "a" * 64
    assert cli._local_workflow_sha256(method) == hashlib.sha256(
        b"local-unqualified-v0:" + method.encode("ascii")
    ).hexdigest()
    assert cli._local_workflow_sha256(method) != cli._local_workflow_sha256("b" * 64)
    with pytest.raises(ValueError):
        cli._local_workflow_sha256("A" * 64)

    def request(token: str, workflow: str) -> JobRequest:
        return JobRequest(
            sample_token=token,
            input_kind=InputKind.MODBAM,
            input_tree_sha256_local="0" * 64,
            workflow_release_sha256=workflow,
        )

    synthetic = cli._synthetic_workflow_sha256()
    # Legacy constant rows, current rows and rows under any later method.
    for workflow in (_LEGACY_LOCAL_WORKFLOW_SHA256, cli._local_workflow_sha256(method), "c" * 64):
        assert cli._is_local_request(request("local-ref", workflow))
    assert not cli._is_local_request(request("local-ref", synthetic))
    assert not cli._is_local_request(request("synthetic-sample-token", synthetic))
    assert not cli._is_local_request(request("synthetic-sample-token", "c" * 64))


def test_a_changed_method_is_a_new_job_not_the_old_record(
    tmp_path: Path, inputs, capsys, monkeypatch
) -> None:
    from traceback_runner import local_authority

    root = tmp_path / "root"
    _register(capsys, root, inputs.fasta_path)
    code, first = _run(capsys, root, inputs.bam_path)
    assert code == cli.ExitCode.OK, first
    stored = _request(root, first["data"]["job_id"])
    assert stored.schema_version == "traceback.job-request.v1"
    assert stored.workflow_release_sha256 == cli._local_workflow_sha256(
        method_definition_sha256(local_method_definition(_registered(root)))
    )

    original = local_authority.local_method_definition

    def changed(reference):
        return original(reference).model_copy(update={"parameter_schema_sha256": "e" * 64})

    monkeypatch.setattr(local_authority, "local_method_definition", changed)
    monkeypatch.setattr(local_authority, "ensure_local_method_authority", lambda *a, **k: None)
    code, second = _run(capsys, root, inputs.bam_path)
    assert code == cli.ExitCode.OK, second
    assert second["data"]["job_id"] != first["data"]["job_id"]
    assert second["data"]["record_id"] != first["data"]["record_id"]
    assert len(_job_ids(root)) == 2 and len(_records(root)) == 2
    verified = verify_bundle(
        Path(second["data"]["bundle_path"]),
        load_development_trust(Path(second["data"]["trust_store_path"]).read_bytes()),
    )
    assert verified.manifest.method.method_definition_sha256 == method_definition_sha256(
        changed(_registered(root))
    )


def test_the_same_method_twice_is_one_job_with_no_twin(tmp_path: Path, inputs, capsys) -> None:
    root = tmp_path / "root"
    _register(capsys, root, inputs.fasta_path)
    _, first = _run(capsys, root, inputs.bam_path)
    code, second = _run(capsys, root, inputs.bam_path)
    assert code == cli.ExitCode.OK
    assert second["data"]["job_id"] == first["data"]["job_id"]
    assert len(_job_ids(root)) == 1
    assert "same_measurement_as" not in first["data"]
    assert "same_measurement_as" not in second["data"]


def _legacy_key(monkeypatch) -> None:
    monkeypatch.setattr(cli, "_local_workflow_sha256", lambda method: _LEGACY_LOCAL_WORKFLOW_SHA256)


def test_a_legacy_key_job_is_still_local_and_resumes(
    tmp_path: Path, inputs, capsys, monkeypatch
) -> None:
    root = tmp_path / "root"
    _register(capsys, root, inputs.fasta_path)
    original = cli._local_stages

    def pausing(*args, **kwargs):
        stages = list(original(*args, **kwargs))
        first = stages[0]

        def callback(context):
            result = first.callback(context)
            cli._existing_runner(root).request_pause(context.job_id)
            return result

        stages[0] = type(first)(
            name=first.name, version=first.version, callback=callback, parameters=first.parameters
        )
        return tuple(stages)

    with monkeypatch.context() as patch:
        _legacy_key(patch)
        patch.setattr(cli, "_local_stages", pausing)
        code, paused = _run(capsys, root, inputs.bam_path)
    assert code == cli.ExitCode.OK and paused["data"]["state"] == "paused", paused
    job_id = paused["data"]["job_id"]
    assert _request(root, job_id).workflow_release_sha256 == _LEGACY_LOCAL_WORKFLOW_SHA256

    # Upgraded code: the legacy row is still a local job and resumes.
    code, status = _json(capsys, "status", job_id, "--root", root)
    assert code == cli.ExitCode.OK
    assert status["data_origin"] == "local_unqualified"
    assert "synthetic" not in status["summary"].lower()
    code, resumed = _json(capsys, "resume", job_id, "--root", root)
    assert code == cli.ExitCode.OK, resumed
    assert resumed["data"]["state"] == "complete"
    assert len(_records(root)) == 1


def test_a_legacy_key_rerun_makes_a_second_record_marked_same_measurement(
    tmp_path: Path, inputs, capsys, monkeypatch
) -> None:
    root = tmp_path / "root"
    _register(capsys, root, inputs.fasta_path)
    with monkeypatch.context() as patch:
        _legacy_key(patch)
        code, legacy = _run(capsys, root, inputs.bam_path)
    assert code == cli.ExitCode.OK, legacy
    assert "same_measurement_as" not in legacy["data"]

    code, upgraded = _run(capsys, root, inputs.bam_path)
    assert code == cli.ExitCode.OK, upgraded
    assert upgraded["data"]["job_id"] != legacy["data"]["job_id"]
    assert upgraded["data"]["record_id"] != legacy["data"]["record_id"]
    assert upgraded["data"]["same_measurement_as"] == legacy["data"]["record_id"]
    assert len(_records(root)) == 2
    with JobStore(root / "runner" / "runner.sqlite3") as store:
        assert cli._measurement_twins(root, store) == {
            upgraded["data"]["record_id"]: legacy["data"]["record_id"]
        }
    # Human output names it too.
    code = cli.main(["run", str(inputs.bam_path), "--reference", "ref", "--root", str(root)])
    assert code == cli.ExitCode.OK
    assert f"SAME_MEASUREMENT_AS  {legacy['data']['record_id']}" in capsys.readouterr().out


def test_the_original_is_found_past_a_thousand_later_jobs(
    tmp_path: Path, inputs, capsys, monkeypatch
) -> None:
    from traceback_runner.contracts import InputKind, JobRequest

    root = tmp_path / "root"
    _register(capsys, root, inputs.fasta_path)
    with monkeypatch.context() as patch:
        _legacy_key(patch)
        _, legacy = _run(capsys, root, inputs.bam_path)
    with JobStore(root / "runner" / "runner.sqlite3") as store:
        for index in range(1_001):
            store.submit(
                JobRequest(
                    sample_token="local-ref",
                    input_kind=InputKind.MODBAM,
                    input_tree_sha256_local=f"{index:064x}",
                    workflow_release_sha256="d" * 64,
                )
            )
        assert store.created_at_by_prefix(legacy["data"]["job_id"][:16]) is not None
        assert store.created_at_by_prefix("0" * 16) is None
    code, upgraded = _run(capsys, root, inputs.bam_path)
    assert code == cli.ExitCode.OK, upgraded
    assert upgraded["data"]["same_measurement_as"] == legacy["data"]["record_id"]


def test_a_tampered_twin_is_not_named(tmp_path: Path, inputs, capsys, monkeypatch) -> None:
    root = tmp_path / "root"
    _register(capsys, root, inputs.fasta_path)
    with monkeypatch.context() as patch:
        _legacy_key(patch)
        _, legacy = _run(capsys, root, inputs.bam_path)
    report = root / "records" / legacy["data"]["record_id"] / "report.html"
    report.chmod(0o600)
    report.write_text(report.read_text() + "<!-- edited -->")
    code, upgraded = _run(capsys, root, inputs.bam_path)
    assert code == cli.ExitCode.OK, upgraded
    assert "same_measurement_as" not in upgraded["data"]


# --- D1: crash-safe provenance HMAC key --------------------------------------


def test_a_short_provenance_key_with_no_records_is_replaced(
    tmp_path: Path, inputs, capsys
) -> None:
    root = tmp_path / "root"
    _register(capsys, root, inputs.fasta_path)
    key = root / "trust" / "provenance-hmac.key"
    key.parent.mkdir(parents=True, exist_ok=True)
    key.write_bytes(b"")  # an older version crashed between create and write
    key.chmod(0o600)
    code, payload = _run(capsys, root, inputs.bam_path)
    assert code == cli.ExitCode.OK, payload
    assert len(key.read_bytes()) == 32
    assert stat.S_IMODE(key.stat().st_mode) == 0o600
    assert not list(key.parent.glob(".provenance-hmac.key.*"))


def test_a_short_provenance_key_with_a_record_is_refused(
    tmp_path: Path, inputs, capsys
) -> None:
    root = tmp_path / "root"
    _register(capsys, root, inputs.fasta_path)
    code, first = _run(capsys, root, inputs.bam_path)
    assert code == cli.ExitCode.OK, first
    key = root / "trust" / "provenance-hmac.key"
    key.write_bytes(b"k" * 7)
    other = create_local_golden_path_inputs(tmp_path / "other", seed=7)
    code, payload = _run(capsys, root, other.bam_path)
    assert code == cli.ExitCode.BLOCKED, payload
    assert payload["data"]["code"] == "TBX-RUN-006"
    assert "ROOT/trust/provenance-hmac.key" in payload["data"]["fix"]
    assert "already holds records" in payload["data"]["cause"]
    assert key.read_bytes() == b"k" * 7
    assert len(_records(root)) == 1


def test_an_existing_read_only_provenance_key_is_accepted(
    tmp_path: Path, inputs, capsys
) -> None:
    root = tmp_path / "root"
    _register(capsys, root, inputs.fasta_path)
    key = root / "trust" / "provenance-hmac.key"
    key.parent.mkdir(parents=True, exist_ok=True)
    key.write_bytes(b"r" * 32)
    key.chmod(0o400)
    code, payload = _run(capsys, root, inputs.bam_path)
    assert code == cli.ExitCode.OK, payload
    assert key.read_bytes() == b"r" * 32


def test_provenance_key_creation_never_leaves_a_short_key(tmp_path: Path, monkeypatch) -> None:
    import os

    root = tmp_path / "root"

    def crash(*args, **kwargs):
        raise OSError("simulated crash before publication")

    monkeypatch.setattr(os, "link", crash)
    with pytest.raises(OSError):
        cli._provenance_hmac_key(root)
    key = root / "trust" / "provenance-hmac.key"
    assert not key.exists()
    assert not list(key.parent.glob(".provenance-hmac.key.*"))
    monkeypatch.undo()
    assert len(cli._provenance_hmac_key(root)) == 32


# --- D2: the runner's lease keeper alone keeps a long local stage alive ------


def test_a_local_stage_three_leases_long_completes_through_the_lease_keeper(
    tmp_path: Path, inputs, capsys, monkeypatch
) -> None:
    import threading

    from traceback_runner import runner as runner_module

    class FakeClock:
        def __init__(self) -> None:
            self.now = 1_000.0

        def __call__(self) -> float:
            return self.now

    clock = FakeClock()
    lease = 5.0
    renewals = threading.Condition()
    counts = {"renewed": 0, "failed": 0}
    real_runner = runner_module.Runner

    def runner_factory(*args, **kwargs):
        built = real_runner(
            *args, clock=clock, lease_seconds=lease, heartbeat_seconds=0.01, **kwargs
        )
        real = built.store.heartbeat

        def heartbeat(current, seconds):
            try:
                result = real(current, seconds)
            except BaseException:
                with renewals:
                    counts["failed"] += 1
                    renewals.notify_all()
                raise
            with renewals:
                counts["renewed"] += 1
                renewals.notify_all()
            return result

        built.store.heartbeat = heartbeat
        return built

    monkeypatch.setattr(runner_module, "Runner", runner_factory)
    original = cli._local_stages
    slow_stages: list[str] = []

    def slow(*args, **kwargs):
        stages = list(original(*args, **kwargs))
        measure = stages[1]

        def callback(context):
            # The stage itself never heartbeats; three lease lengths pass.
            for _ in range(6):
                with renewals:
                    seen = counts["renewed"]
                clock.now += lease / 2
                with renewals:
                    assert renewals.wait_for(
                        lambda: counts["renewed"] >= seen + 2 or counts["failed"], 10.0
                    )
                    assert counts["failed"] == 0
            slow_stages.append(context.job_id)
            return measure.callback(context)

        stages[1] = type(measure)(
            name=measure.name, version=measure.version, callback=callback,
            parameters=measure.parameters,
        )
        return tuple(stages)

    monkeypatch.setattr(cli, "_local_stages", slow)
    root = tmp_path / "root"
    _register(capsys, root, inputs.fasta_path)
    code, payload = _run(capsys, root, inputs.bam_path)
    assert code == cli.ExitCode.OK, payload
    assert payload["data"]["state"] == "complete"
    assert slow_stages == [payload["data"]["job_id"]]
    assert counts["failed"] == 0
    assert not hasattr(cli, "_stage_heartbeat")
