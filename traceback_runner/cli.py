"""Local, synthetic-only Traceback operator CLI."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import platform
import shutil
import stat
import sys
import tempfile
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager, nullcontext
from datetime import UTC, datetime
from enum import IntEnum
from pathlib import Path
from typing import Any

from .contracts import JobState
from .filesystem import rename_directory_exclusive_at
from .operator import build_job_view, support_payload
from .protocol import render_protocol, synthetic_protocol_manifest
from .serialization import canonical_json_bytes

_TRUST_RELATIVE = Path("trust/development-result-trust.json")
_BAM_NAME = "valid_modbam.bam"
_INDEX_NAME = "valid_modbam.bam.bai"
_WORKFLOW_ID = "synthetic-development-v1"
_MAX_ASSET_AUTHORITY_INPUT_BYTES = 2 * 1024 * 1024


class ExitCode(IntEnum):
    """Stable process exits shared by human and JSON command output."""

    OK = 0
    USAGE = 2
    BLOCKED = 3
    NOT_FOUND = 4
    VERIFICATION_FAILED = 5
    RETRYABLE_FAILURE = 6
    INTERNAL_ERROR = 7


class AssetEvidenceInputError(ValueError):
    """Independent asset authority inputs are malformed or ambiguous."""


def _root_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(".traceback"),
        help="local runner data root (default: .traceback)",
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="traceback")
    commands = parser.add_subparsers(dest="command", required=True)

    doctor = commands.add_parser("doctor", help="check the local synthetic runtime")
    doctor.add_argument("--json", action="store_true", dest="as_json")

    protocol = commands.add_parser("protocol", help="show fail-closed setup content")
    protocol_commands = protocol.add_subparsers(dest="protocol_command", required=True)
    protocol_show = protocol_commands.add_parser("show", help="show Protocol & Setup")
    protocol_show.add_argument("--json", action="store_true", dest="as_json")

    demo = commands.add_parser("demo", help="run the signed synthetic workflow")
    _root_argument(demo)
    demo.add_argument("--json", action="store_true", dest="as_json")

    preflight = commands.add_parser(
        "preflight", help="perform unqualified technical BAM inspection"
    )
    preflight.add_argument("input", type=Path)
    preflight.add_argument("--index", type=Path)
    _root_argument(preflight)
    preflight.add_argument("--json", action="store_true", dest="as_json")

    run = commands.add_parser("run", help="process a real input (disabled)")
    run.add_argument("input", type=Path)
    run.add_argument("--json", action="store_true", dest="as_json")

    for name in ("status", "logs", "pause", "resume", "retry"):
        command = commands.add_parser(name)
        command.add_argument("job_id")
        _root_argument(command)
        command.add_argument("--json", action="store_true", dest="as_json")

    inspect = commands.add_parser("inspect", help="inspect an unverified local bundle")
    inspect.add_argument("bundle", type=Path)
    inspect.add_argument("--json", action="store_true", dest="as_json")

    verify = commands.add_parser("verify", help="verify a bundle against local trust")
    verify.add_argument("bundle", type=Path)
    verify.add_argument("--trust-store", required=True, type=Path)
    verify.add_argument("--json", action="store_true", dest="as_json")

    assets = commands.add_parser("assets", help="manage offline synthetic assets")
    asset_commands = assets.add_subparsers(dest="asset_command", required=True)
    for name in ("install", "verify"):
        asset_command = asset_commands.add_parser(
            name, help=f"{name} one authority-bound synthetic asset"
        )
        asset_command.add_argument("--release-evidence", required=True, type=Path)
        asset_command.add_argument("--trust-store", required=True, type=Path)
        asset_command.add_argument("--role-policy", required=True, type=Path)
        asset_command.add_argument("--authority-head", required=True, type=Path)
        asset_command.add_argument("--asset", required=True, dest="asset_id")
        asset_command.add_argument("--version", required=True)
        _root_argument(asset_command)
        asset_command.add_argument("--json", action="store_true", dest="as_json")
    asset_commands.choices["install"].add_argument(
        "--package", required=True, type=Path
    )

    commands.add_parser("reader", help="local operator reader authority (E12)")
    support = commands.add_parser(
        "support-bundle", help="write allowlisted redacted diagnostics"
    )
    support.add_argument("job_id")
    support.add_argument("--output", required=True, type=Path)
    _root_argument(support)
    support.add_argument("--json", action="store_true", dest="as_json")
    return parser


def _result(
    command: str,
    status: str,
    summary: str,
    *,
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": "traceback.cli-result.v1",
        "command": command,
        "status": status,
        "summary": summary,
        "synthetic_only": True,
        "data": data or {},
    }


def _emit(payload: dict[str, Any], *, as_json: bool) -> None:
    if as_json:
        print(canonical_json_bytes(payload).decode("utf-8"))
        return
    label = "PASS" if payload["status"] == "ok" else payload["status"].upper()
    print(f"{label}  {payload['summary']}")
    for key, value in payload.get("data", {}).items():
        rendered = (
            json.dumps(value, sort_keys=True, separators=(",", ":"))
            if isinstance(value, (dict, list))
            else str(value)
        )
        print(f"{key.upper()}  {rendered}")


def _doctor() -> tuple[ExitCode, dict[str, Any]]:
    checks = [
        {
            "name": "python",
            "status": "pass" if sys.version_info[:2] == (3, 11) else "blocked",
            "detail": "Python 3.11 runtime",
        },
        {
            "name": "platform",
            "status": "pass",
            "detail": f"{platform.system()} {platform.machine()}",
        },
        {
            "name": "data_boundary",
            "status": "pass",
            "detail": "synthetic local execution; network not required",
        },
        {
            "name": "real_data",
            "status": "blocked",
            "detail": "real-data execution is not enabled in this development wave",
        },
    ]
    runtime_blocked = checks[0]["status"] == "blocked"
    return (
        ExitCode.BLOCKED if runtime_blocked else ExitCode.OK,
        _result(
            "doctor",
            "blocked" if runtime_blocked else "ok",
            (
                "Synthetic runtime is blocked"
                if runtime_blocked
                else "Real-data execution is not enabled; synthetic local runtime is available"
            ),
            data={"checks": checks},
        ),
    )


def _protocol_show() -> tuple[ExitCode, dict[str, Any]]:
    manifest = synthetic_protocol_manifest()
    return ExitCode.OK, _result(
        "protocol show",
        "ok",
        "Synthetic setup content rendered; unapproved wet-lab instructions withheld",
        data={
            "workflow_release_id": manifest.workflow_release_id,
            "rows": render_protocol(manifest),
        },
    )


def _real_run_blocked() -> tuple[ExitCode, dict[str, Any]]:
    return ExitCode.BLOCKED, _result(
        "run",
        "blocked",
        "Real-data execution is not enabled; no job was created",
        data={
            "code": "TBX-RUN-003",
            "fix": "Use traceback demo for the synthetic development workflow",
            "retryable": False,
        },
    )


def _write_once(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != content:
            raise ValueError("existing local trust document differs")
        return
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("xb") as stream:
        stream.write(content)
        stream.flush()
    temporary.replace(path)


class OperatorBusy(RuntimeError):
    """Another CLI mutation or live worker owns this local workspace."""


@contextmanager
def _operator_lock(root: Path) -> Iterator[None]:
    """Serialize CLI mutations; OS locks release automatically after process death."""
    import fcntl

    root.mkdir(parents=True, exist_ok=True)
    with (root / ".operator.lock").open("a+b") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise OperatorBusy("local operator action already in progress") from exc
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _append_development_trust(path: Path, content: bytes) -> None:
    """Add public keys without replacing historical entries or revocations.

    The caller holds the workspace mutation lock. Private keys remain ephemeral.
    """
    from .signing import (
        DevelopmentTrustDocument,
        SigningError,
        development_trust_document_bytes,
        load_development_trust,
    )

    load_development_trust(content)
    incoming = DevelopmentTrustDocument.model_validate_json(content)
    keys = {}
    if path.exists():
        previous = path.read_bytes()
        load_development_trust(previous)
        keys = {key.key_id: key for key in DevelopmentTrustDocument.model_validate_json(previous).keys}
    for key in incoming.keys:
        if key.key_id in keys and keys[key.key_id] != key:
            raise SigningError("existing development trust entry cannot be changed")
        keys[key.key_id] = key
    merged = DevelopmentTrustDocument(keys=tuple(keys[key] for key in sorted(keys)))
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=".trust-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(development_trust_document_bytes(merged))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _reject_live_worker(runner: Any, job_id: str) -> None:
    record = runner.store.get(job_id)
    if (record.lease_owner is not None and record.lease_expires_at is not None
            and record.lease_expires_at >= runner.clock()):
        raise OperatorBusy("job still has a live worker lease")


def _ensure_synthetic_input(root: Path) -> tuple[Path, tuple[str, str]]:
    from .fixtures import SyntheticBamKind, create_synthetic_bam

    source = root / "synthetic-input"
    bam = source / _BAM_NAME
    index = source / _INDEX_NAME
    if not bam.exists() and not index.exists():
        create_synthetic_bam(source, SyntheticBamKind.VALID_MODBAM)
    if not bam.is_file() or not index.is_file():
        raise ValueError("synthetic input directory is incomplete")
    return source, (_BAM_NAME, _INDEX_NAME)


def _demo_stages(signing_key: Any) -> tuple[Any, ...]:
    from .bundles import build_result_bundle
    from .contracts import (
        ArtifactCommitment,
        ExportRunProvenance,
        FragmentMeasurement,
        InputKind,
        StageName,
    )
    from .fixtures import (
        SYNTHETIC_MODIFIED_BASE_MODEL,
        synthetic_fragment_policy,
        synthetic_registered_reference,
    )
    from .measurement import finalize_measurement, scan_aligned_reference_spans
    from .preflight import BamPreflightPolicy, validate_bam_snapshot
    from .runner import StageResult, StageSpec

    reference = synthetic_registered_reference()
    measurement_policy = synthetic_fragment_policy()
    preflight_policy = BamPreflightPolicy(
        policy_id="synthetic-preflight-v1",
        modified_base_model_id=SYNTHETIC_MODIFIED_BASE_MODEL,
    )

    def preflight_stage(context: Any) -> Any:
        report = validate_bam_snapshot(
            context.sealed_input_dir / _BAM_NAME,
            context.sealed_input_dir / _INDEX_NAME,
            reference,
            preflight_policy,
        )
        if not report.fragment_measurement_eligible:
            raise ValueError("synthetic BAM did not pass fragment preflight")
        output = context.attempt_dir / "preflight.json"
        output.write_bytes(canonical_json_bytes(report))
        return StageResult(
            outputs={"preflight_report": output.name},
            metadata={
                "fragment_measurement_eligible": True,
                "future_methylation_eligible": report.future_methylation_eligible,
                "synthetic_only": True,
            },
        )

    def measurement_stage(context: Any) -> Any:
        scan = scan_aligned_reference_spans(
            context.sealed_input_dir / _BAM_NAME,
            measurement_policy,
        )
        measurement = finalize_measurement(scan)
        output = context.attempt_dir / "measurement.json"
        output.write_bytes(canonical_json_bytes(measurement))
        return StageResult(
            outputs={"fragment_measurement": output.name},
            metadata={
                "records_scanned": measurement.records_scanned,
                "eligible_alignments": measurement.eligible_alignments,
                "complete": True,
                "synthetic_only": True,
            },
        )

    def signing_stage(context: Any) -> Any:
        measurement_path = context.prior_stage_dirs[-1] / "measurement.json"
        measurement = FragmentMeasurement.model_validate_json(measurement_path.read_bytes())
        provenance = ExportRunProvenance(
            run_token="synthetic-run-token",
            input_kind=InputKind.MODBAM,
            protocol_run_token="synthetic-protocol-run",
            workflow_release_id=_WORKFLOW_ID,
            artifacts=(
                ArtifactCommitment(
                    role="analysis_bam",
                    artifact_token="synthetic-analysis-bam",
                    size_bytes=(context.sealed_input_dir / _BAM_NAME).stat().st_size,
                    provider_hmac_sha256=hmac.new(
                        b"traceback-synthetic-development-only",
                        b"runtime-generated-valid-modbam",
                        hashlib.sha256,
                    ).hexdigest(),
                ),
            ),
        )
        bundle = build_result_bundle(
            context.attempt_dir / "bundle",
            measurement=measurement,
            provenance=provenance,
            method={
                "method_id": "mth_fragment_aligned_reference_span",
                "version": "1.0.0",
                "method_definition_sha256": "c" * 64,
            },
            signing_key=signing_key,
        )
        bundle_files = sorted(path for path in bundle.rglob("*") if path.is_file())
        if len(bundle_files) != 8:
            raise ValueError("synthetic signed bundle must contain exactly eight files")
        outputs = {
            f"bundle_{index:02d}": path.relative_to(context.attempt_dir).as_posix()
            for index, path in enumerate(bundle_files)
        }
        return StageResult(
            outputs=outputs,
            metadata={"signed": True, "development_trust_only": True},
        )

    return (
        StageSpec(
            name=StageName.VALIDATE,
            version="1",
            callback=preflight_stage,
            parameters={"policy": preflight_policy.policy_id},
        ),
        StageSpec(
            name=StageName.MEASURE,
            version="1",
            callback=measurement_stage,
            parameters={"definition": measurement_policy.definition_id},
        ),
        StageSpec(
            name=StageName.SIGN,
            version="1",
            callback=signing_stage,
            parameters={"key_id": signing_key.key_id, "development_trust_only": True},
        ),
    )


def _signed_bundle_from_outputs(runner: Any, job_id: str) -> Path:
    outputs = runner.outputs(job_id, "sign")
    manifest = next(
        path for path in outputs.values() if path.name == "bundle-manifest.json"
    )
    return manifest.parent


def _rename_directory_exclusive(source: Path, destination: Path) -> None:
    """Atomic directory publication that cannot overwrite even an empty target."""
    if source.parent != destination.parent:
        raise ValueError("exclusive directory publication requires one parent")
    parent_fd = os.open(
        source.parent,
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        rename_directory_exclusive_at(parent_fd, source.name, destination.name)
    finally:
        os.close(parent_fd)


def _publish_verified_record(root: Path, verified: Any, source: Path) -> Path:
    from .bundles import BundleError, verify_bundle
    from .signing import SigningError, load_development_trust

    trust = load_development_trust((root / _TRUST_RELATIVE).read_bytes())
    records = root / "records"
    records.mkdir(parents=True, exist_ok=True)
    name = f"{verified.manifest.record_id}-{verified.manifest.signing_key_id}"
    destination = records / name
    # Preserve invalid user-visible output. A previous valid recovery copy can
    # be reused, otherwise publish a new verified sibling without clobbering it.
    for candidate in [destination, *sorted(records.glob(f"{name}-recovered-*"))]:
        if candidate.exists() or candidate.is_symlink():
            try:
                if verify_bundle(candidate, trust).manifest == verified.manifest:
                    return candidate
            except (BundleError, SigningError):
                pass
    if destination.exists() or destination.is_symlink():
        destination = records / f"{name}-recovered-{uuid.uuid4().hex}"
    staging = Path(tempfile.mkdtemp(prefix=".record-", dir=records))
    try:
        shutil.copytree(source, staging, dirs_exist_ok=True)
        if verify_bundle(staging, trust).manifest != verified.manifest:
            raise ValueError("copied record differs from verified source")
        for path in sorted(staging.rglob("*"), reverse=True):
            if path.is_dir():
                _fsync_directory(path)
            else:
                with path.open("rb") as handle:
                    os.fsync(handle.fileno())
        _fsync_directory(staging)
        _rename_directory_exclusive(staging, destination)
        _fsync_directory(records)
        return destination
    finally:
        if staging.exists():
            staging.chmod(0o700)
            for path in staging.rglob("*"):
                if path.is_dir():
                    path.chmod(0o700)
            shutil.rmtree(staging)


def _demo(args: argparse.Namespace) -> tuple[ExitCode, dict[str, Any]]:
    from .bundles import verify_bundle
    from .contracts import InputKind, JobRequest
    from .runner import Runner
    from .signing import (
        KeyPurpose,
        development_trust_bytes,
        generate_development_keypair,
        load_development_trust,
    )
    from .snapshots import input_tree_sha256

    root = args.root
    source, relative_files = _ensure_synthetic_input(root)
    request = JobRequest(
        sample_token="synthetic-sample-token",
        input_kind=InputKind.MODBAM,
        input_tree_sha256_local=input_tree_sha256(source, relative_files),
        workflow_release_sha256=hashlib.sha256(_WORKFLOW_ID.encode("ascii")).hexdigest(),
    )
    runner = Runner(root / "runner", synthetic_enabled=True)
    record = runner.submit(request, source, relative_files)
    trust_path = root / _TRUST_RELATIVE

    if record.state != JobState.COMPLETE:
        _reject_live_worker(runner, record.job_id)
        signing_key = generate_development_keypair(KeyPurpose.RESULT)
        _append_development_trust(trust_path, development_trust_bytes(signing_key))
        stages = _demo_stages(signing_key)
        if record.state in {JobState.PAUSED, JobState.RETRYABLE_FAILURE}:
            record = runner.resume(record.job_id, stages, worker_id="synthetic-cli")
        else:
            record = runner.execute(record.job_id, stages, worker_id="synthetic-cli")

    trust_store = load_development_trust(trust_path.read_bytes())
    runner_bundle = _signed_bundle_from_outputs(runner, record.job_id)
    verified = verify_bundle(runner_bundle, trust_store)
    published = _publish_verified_record(root, verified, runner_bundle)
    verify_bundle(published, trust_store)
    bundle_token = published.relative_to(root).as_posix()
    return ExitCode.OK, _result(
        "demo",
        "ok",
        "Signed local record ready (development trust); nothing was uploaded",
        data={
            "job_id": record.job_id,
            "state": record.state.value,
            "record_id": verified.manifest.record_id,
            "bundle": bundle_token,
            "trust_store": _TRUST_RELATIVE.as_posix(),
            "verification": "verified",
            "development_trust_only": True,
        },
    )


def _preflight(args: argparse.Namespace) -> tuple[ExitCode, dict[str, Any]]:
    from .contracts import PreflightOutcome
    from .fixtures import SYNTHETIC_MODIFIED_BASE_MODEL, synthetic_registered_reference
    from .preflight import BamPreflightPolicy, validate_bam_snapshot

    index = args.index or Path(f"{args.input}.bai")
    report = validate_bam_snapshot(
        args.input,
        index,
        synthetic_registered_reference(),
        BamPreflightPolicy(
            policy_id="synthetic-preflight-v1",
            modified_base_model_id=SYNTHETIC_MODIFIED_BASE_MODEL,
        ),
    )
    blocked = report.outcome == PreflightOutcome.BLOCKED
    return (
        ExitCode.BLOCKED if blocked else ExitCode.OK,
        _result(
            "preflight",
            "blocked" if blocked else "ok",
            (
                "Technical inspection blocked this input; real execution remains disabled"
                if blocked
                else "Technical inspection passed for the synthetic reference; real execution remains disabled"
            ),
            data={"report": report.model_dump(mode="json"), "qualified": False},
        ),
    )


def _existing_runner(root: Path, *, synthetic_enabled: bool = False) -> Any:
    from .runner import Runner

    runner_root = root / "runner"
    if not (runner_root / "runner.sqlite3").is_file():
        raise FileNotFoundError("runner database not found")
    return Runner(runner_root, synthetic_enabled=synthetic_enabled)


def _record_is_verified(root: Path, runner: Any, job_id: str) -> bool:
    from .bundles import verify_bundle
    from .signing import load_development_trust

    try:
        trust = load_development_trust((root / _TRUST_RELATIVE).read_bytes())
        verify_bundle(_signed_bundle_from_outputs(runner, job_id), trust)
    except Exception:
        return False
    return True


def _status(args: argparse.Namespace) -> tuple[ExitCode, dict[str, Any]]:
    runner = _existing_runner(args.root)
    record = runner.status(args.job_id)
    verified = record.state == JobState.COMPLETE and _record_is_verified(
        args.root, runner, record.job_id
    )
    view = build_job_view(
        job_id=record.job_id,
        state=record.state,
        observed_at=datetime.now(UTC),
        signature_verified=verified,
    )
    return ExitCode.OK, _result(
        "status",
        "ok",
        view.headline,
        data={"operator_state": view.model_dump(mode="json")},
    )


def _logs(args: argparse.Namespace) -> tuple[ExitCode, dict[str, Any]]:
    runner = _existing_runner(args.root)
    runner.status(args.job_id)
    events = [
        {
            "sequence": event["sequence"],
            "occurred_at": event["occurred_at"],
            "previous_state": event["previous_state"],
            "next_state": event["next_state"],
            "lease_token": event["lease_token"],
        }
        for event in runner.store.audit(args.job_id)
    ]
    return ExitCode.OK, _result(
        "logs",
        "ok",
        "Redacted local transition log",
        data={"job_id": args.job_id, "events": events},
    )


def _pause(args: argparse.Namespace) -> tuple[ExitCode, dict[str, Any]]:
    runner = _existing_runner(args.root)
    record = runner.request_pause(args.job_id)
    return ExitCode.OK, _result(
        "pause",
        "ok",
        "Pause requested; it will take effect at the next stage boundary",
        data={"job_id": record.job_id, "state": record.state.value},
    )


def _retry(args: argparse.Namespace) -> tuple[ExitCode, dict[str, Any]]:
    runner = _existing_runner(args.root)
    record = runner.retry(args.job_id)
    return ExitCode.OK, _result(
        "retry",
        "ok",
        "Retry queued; run resume to execute it (no background worker is running)",
        data={"job_id": record.job_id, "state": record.state.value,
              "next_action": f"traceback resume {record.job_id} --root <same-root>"},
    )


def _resume(args: argparse.Namespace) -> tuple[ExitCode, dict[str, Any]]:
    from .bundles import verify_bundle
    from .signing import (
        KeyPurpose,
        development_trust_bytes,
        generate_development_keypair,
        load_development_trust,
    )

    runner = _existing_runner(args.root, synthetic_enabled=True)
    record = runner.status(args.job_id)
    request = runner.store.request(record.job_id)
    if (request.workflow_release_sha256 != hashlib.sha256(_WORKFLOW_ID.encode("ascii")).hexdigest()
            or request.sample_token != "synthetic-sample-token"):
        return ExitCode.BLOCKED, _result(
            "resume", "blocked", "Only the registered synthetic demo workflow can resume",
        )
    if record.state not in {JobState.PAUSED, JobState.RETRYABLE_FAILURE, JobState.QUEUED, JobState.RUNNING}:
        return ExitCode.BLOCKED, _result(
            "resume",
            "blocked",
            f"Job cannot resume from state {record.state.value}",
            data={"job_id": record.job_id, "state": record.state.value},
        )
    _reject_live_worker(runner, record.job_id)
    signing_key = generate_development_keypair(KeyPurpose.RESULT)
    trust_path = args.root / _TRUST_RELATIVE
    _append_development_trust(trust_path, development_trust_bytes(signing_key))
    stages = _demo_stages(signing_key)
    if record.state == JobState.QUEUED:
        record = runner.execute(record.job_id, stages, worker_id="synthetic-cli")
    else:
        record = runner.resume(record.job_id, stages, worker_id="synthetic-cli")
    trust = load_development_trust(trust_path.read_bytes())
    runner_bundle = _signed_bundle_from_outputs(runner, record.job_id)
    verified = verify_bundle(runner_bundle, trust)
    published = _publish_verified_record(args.root, verified, runner_bundle)
    verify_bundle(published, trust)
    return ExitCode.OK, _result(
        "resume",
        "ok",
        "Signed local record ready (development trust); nothing was uploaded",
        data={
            "job_id": record.job_id,
            "state": record.state.value,
            "record_id": verified.manifest.record_id,
            "bundle": published.relative_to(args.root).as_posix(),
        },
    )


def _inspect(args: argparse.Namespace) -> tuple[ExitCode, dict[str, Any]]:
    from .bundles import inspect_bundle

    manifest = inspect_bundle(args.bundle)
    return ExitCode.OK, _result(
        "inspect",
        "ok",
        "Bundle manifest inspected but not verified",
        data={"verified": False, "manifest": manifest.model_dump(mode="json")},
    )


def _verify(args: argparse.Namespace) -> tuple[ExitCode, dict[str, Any]]:
    from .bundles import verify_bundle
    from .signing import load_development_trust

    trust_path = args.trust_store
    if trust_path.is_dir():
        trust_path = trust_path / _TRUST_RELATIVE.name
    trust = load_development_trust(trust_path.read_bytes())
    verified = verify_bundle(args.bundle, trust)
    return ExitCode.OK, _result(
        "verify",
        "ok",
        "Signed local record verified with development trust; nothing was uploaded",
        data={
            "verified": True,
            "record_id": verified.manifest.record_id,
            "development_trust_only": True,
        },
    )


def _asset_authorization(args: argparse.Namespace) -> Any:
    """Load exact, independent authority inputs without trusting the envelope."""

    from .assets import ReleaseAuthorization
    from .qualification import (
        AuthorityScope,
        QualificationTrustPolicy,
        ReleaseAuthorityHead,
        ReleaseEvidenceEnvelope,
    )
    from .serialization import canonical_model_from_bytes
    from .signing import load_development_trust

    def read_authority_input(path: Path) -> bytes:
        flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        try:
            descriptor = os.open(path, flags)
        except FileNotFoundError:
            raise
        except OSError as exc:
            raise AssetEvidenceInputError(
                "independent authority input cannot be opened safely"
            ) from exc
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise AssetEvidenceInputError(
                    "independent authority input must be a regular file"
                )
            if metadata.st_size > _MAX_ASSET_AUTHORITY_INPUT_BYTES:
                raise AssetEvidenceInputError(
                    "independent authority input exceeds its bounded limit"
                )
            with os.fdopen(descriptor, "rb") as stream:
                descriptor = -1
                content = stream.read(_MAX_ASSET_AUTHORITY_INPUT_BYTES + 1)
            if len(content) > _MAX_ASSET_AUTHORITY_INPUT_BYTES:
                raise AssetEvidenceInputError(
                    "independent authority input exceeds its bounded limit"
                )
            return content
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    try:
        envelope = canonical_model_from_bytes(
            ReleaseEvidenceEnvelope, read_authority_input(args.release_evidence)
        )
        role_policy = canonical_model_from_bytes(
            QualificationTrustPolicy, read_authority_input(args.role_policy)
        )
        authority_head = canonical_model_from_bytes(
            ReleaseAuthorityHead, read_authority_input(args.authority_head)
        )
        trust_store = load_development_trust(
            read_authority_input(args.trust_store)
        )
    except FileNotFoundError:
        raise
    except AssetEvidenceInputError:
        raise
    except Exception as exc:
        raise AssetEvidenceInputError(
            "independent authority input is malformed or noncanonical"
        ) from exc

    matching_bindings: dict[bytes, Any] = {}
    for grant in role_policy.grants:
        binding = grant.binding
        if (
            grant.scope == AuthorityScope.RELEASE_EVIDENCE
            and binding.release_id == authority_head.release_id
            and binding.release_version == authority_head.release_version
            and binding.release_evidence_sha256 == authority_head.package_sha256
        ):
            matching_bindings[canonical_json_bytes(binding)] = binding
    if len(matching_bindings) != 1:
        raise AssetEvidenceInputError(
            "authority policy must contain one distinct matching release binding"
        )
    expected_binding = next(iter(matching_bindings.values()))
    return ReleaseAuthorization(
        envelope=envelope,
        trust_store=trust_store,
        role_policy=role_policy,
        authority_head=authority_head,
        expected_binding=expected_binding,
        expected_package_sha256=authority_head.package_sha256,
        now=lambda: datetime.now(UTC),
    )


def _asset_decision(
    authorization: Any, *, asset_id: str, version: str
) -> Any:
    from .assets import AssetAuthorityError
    from .qualification import verify_release_asset_authorization
    from .release_evidence import DigestDomain, domain_digest

    reference = next(
        (
            item
            for item in authorization.envelope.package.assets
            if (item.content.asset_id, item.content.version) == (asset_id, version)
        ),
        None,
    )
    if reference is None:
        raise AssetAuthorityError("selected asset is absent from release evidence")
    current = authorization.now()
    return verify_release_asset_authorization(
        authorization.envelope,
        authorization.trust_store,
        authorization.role_policy,
        authorization.authority_head,
        expected_binding=authorization.expected_binding,
        expected_package_sha256=authorization.expected_package_sha256,
        expected_asset_id=asset_id,
        expected_asset_version=version,
        expected_asset_reference_sha256=domain_digest(
            DigestDomain.ASSET_REFERENCE, reference
        ),
        now=current,
    )


def _asset_result_data(
    *,
    asset_id: str,
    version: str,
    decision: Any,
    verification: Any | None = None,
) -> dict[str, Any]:
    return {
        "asset_id": asset_id,
        "version": version,
        "installed": verification.installed if verification is not None else False,
        "integrity": (
            verification.integrity_status.value
            if verification is not None
            else "absent"
        ),
        "authority": decision.authority_status.value,
        "lifecycle": decision.lifecycle_status.value,
        "authority_failure": decision.failure.value,
        "registration_matches_current_reference": (
            verification.registration_matches_current_reference
            if verification is not None
            else False
        ),
        "verified_as_of": (
            decision.verified_as_of.isoformat()
            if decision.verified_as_of is not None
            else None
        ),
        "fresh_until": (
            decision.fresh_until.isoformat()
            if decision.fresh_until is not None
            else None
        ),
        "real_data_authorized": False,
        "execution_authorized": False,
        "qualification_probe_authorized": False,
    }


def _assets_install(args: argparse.Namespace) -> tuple[ExitCode, dict[str, Any]]:
    from .assets import AssetRegistry, IntegrityStatus
    from .qualification import AssetLifecycleStatus, AuthorityFailure, AuthorityStatus

    authorization = _asset_authorization(args)
    decision = _asset_decision(
        authorization, asset_id=args.asset_id, version=args.version
    )
    if decision.failure == AuthorityFailure.UNKNOWN_SIGNER:
        raise AssetEvidenceInputError("release authority signer is not trusted")
    if decision.authority_status == AuthorityStatus.INVALID:
        raise AssetEvidenceInputError("release authority verification is invalid")
    if (
        decision.authority_status != AuthorityStatus.VERIFIED
        or decision.lifecycle_status != AssetLifecycleStatus.ACTIVE
    ):
        return ExitCode.BLOCKED, _result(
            "assets install",
            "blocked",
            "Asset authority is not current and active; no install was performed",
            data=_asset_result_data(
                asset_id=args.asset_id,
                version=args.version,
                decision=decision,
            ),
        )

    registry = AssetRegistry(args.root / "assets")
    installed = registry.install(
        args.package,
        asset_id=args.asset_id,
        version=args.version,
        authorization=authorization,
    )
    verification = registry.verify(
        asset_id=args.asset_id,
        version=args.version,
        authorization=authorization,
    )
    decision = _asset_decision(
        authorization, asset_id=args.asset_id, version=args.version
    )
    data = _asset_result_data(
        asset_id=args.asset_id,
        version=args.version,
        decision=decision,
        verification=verification,
    )
    data["newly_registered"] = installed.newly_registered
    data["content_size_bytes"] = installed.content_size_bytes
    if (
        not verification.installed
        or verification.integrity_status != IntegrityStatus.VALID
    ):
        return ExitCode.VERIFICATION_FAILED, _result(
            "assets install",
            "verification_failed",
            "Installed synthetic asset failed post-publication integrity verification",
            data=data,
        )
    if decision.authority_status == AuthorityStatus.INVALID:
        return ExitCode.VERIFICATION_FAILED, _result(
            "assets install",
            "verification_failed",
            "Installed bytes are intact but post-publication authority is invalid",
            data=data,
        )
    if (
        decision.authority_status != AuthorityStatus.VERIFIED
        or decision.lifecycle_status != AssetLifecycleStatus.ACTIVE
        or not verification.registration_matches_current_reference
    ):
        return ExitCode.BLOCKED, _result(
            "assets install",
            "blocked",
            "Asset was published but is unavailable because current authority is not active",
            data=data,
        )
    return ExitCode.OK, _result(
        "assets install",
        "ok",
        "Synthetic asset installed and verified offline; execution remains disabled",
        data=data,
    )


def _assets_verify(args: argparse.Namespace) -> tuple[ExitCode, dict[str, Any]]:
    from .assets import AssetRegistry, IntegrityStatus
    from .qualification import AssetLifecycleStatus, AuthorityFailure, AuthorityStatus

    authorization = _asset_authorization(args)
    registry_root = args.root / "assets"
    if not registry_root.exists() and not registry_root.is_symlink():
        decision = _asset_decision(
            authorization, asset_id=args.asset_id, version=args.version
        )
        return ExitCode.NOT_FOUND, _result(
            "assets verify",
            "not_found",
            "Synthetic asset is not installed",
            data=_asset_result_data(
                asset_id=args.asset_id,
                version=args.version,
                decision=decision,
            ),
        )
    registry = AssetRegistry.open_existing(registry_root)
    verification = registry.verify(
        asset_id=args.asset_id,
        version=args.version,
        authorization=authorization,
    )
    decision = _asset_decision(
        authorization, asset_id=args.asset_id, version=args.version
    )
    data = _asset_result_data(
        asset_id=args.asset_id,
        version=args.version,
        decision=decision,
        verification=verification,
    )
    if decision.failure == AuthorityFailure.UNKNOWN_SIGNER:
        raise AssetEvidenceInputError("release authority signer is not trusted")
    if not verification.installed:
        return ExitCode.NOT_FOUND, _result(
            "assets verify", "not_found", "Synthetic asset is not installed", data=data
        )
    if verification.integrity_status != IntegrityStatus.VALID:
        return ExitCode.VERIFICATION_FAILED, _result(
            "assets verify",
            "verification_failed",
            "Installed synthetic asset failed integrity verification",
            data=data,
        )
    if decision.authority_status == AuthorityStatus.INVALID:
        return ExitCode.VERIFICATION_FAILED, _result(
            "assets verify",
            "verification_failed",
            "Installed bytes are intact but release authority is invalid",
            data=data,
        )
    if (
        decision.authority_status != AuthorityStatus.VERIFIED
        or decision.lifecycle_status != AssetLifecycleStatus.ACTIVE
        or not verification.registration_matches_current_reference
    ):
        return ExitCode.BLOCKED, _result(
            "assets verify",
            "blocked",
            "Installed bytes are intact but authority is not current and active",
            data=data,
        )
    return ExitCode.OK, _result(
        "assets verify",
        "ok",
        "Synthetic asset integrity and current authority verified; execution remains disabled",
        data=data,
    )


def _assets(args: argparse.Namespace) -> tuple[ExitCode, dict[str, Any]]:
    if args.asset_command == "install":
        return _assets_install(args)
    if args.asset_command == "verify":
        return _assets_verify(args)
    raise AssertionError(f"unhandled assets command {args.asset_command}")


def _support_bundle(args: argparse.Namespace) -> tuple[ExitCode, dict[str, Any]]:
    runner = _existing_runner(args.root)
    record = runner.status(args.job_id)
    events = [
        {
            "state": event["next_state"],
            "timestamp": event["occurred_at"],
            "attempt": event["sequence"],
        }
        for event in runner.store.audit(args.job_id)
    ]
    payload = support_payload(
        job_state=record.state,
        error_codes=("TBX-JOB-001",) if record.state == JobState.RETRYABLE_FAILURE else (),
        events=events,
        runner_version="0.1.0-synthetic",
    )
    destination = args.output
    if destination.suffix != ".json":
        destination.mkdir(parents=True, exist_ok=True)
        destination = destination / "support.json"
    else:
        destination.parent.mkdir(parents=True, exist_ok=True)
    _write_once(destination, canonical_json_bytes(payload))
    return ExitCode.OK, _result(
        "support-bundle",
        "ok",
        "Redacted local support bundle written; nothing was uploaded",
        data={"artifact": destination.name},
    )


def _dispatch(args: argparse.Namespace) -> tuple[ExitCode, dict[str, Any]]:
    if args.command == "doctor":
        return _doctor()
    if args.command == "protocol":
        return _protocol_show()
    if args.command == "demo":
        return _demo(args)
    if args.command == "preflight":
        return _preflight(args)
    if args.command == "run":
        return _real_run_blocked()
    if args.command == "status":
        return _status(args)
    if args.command == "logs":
        return _logs(args)
    if args.command == "pause":
        return _pause(args)
    if args.command == "resume":
        return _resume(args)
    if args.command == "retry":
        return _retry(args)
    if args.command == "inspect":
        return _inspect(args)
    if args.command == "verify":
        return _verify(args)
    if args.command == "assets":
        return _assets(args)
    if args.command == "support-bundle":
        return _support_bundle(args)
    raise AssertionError(f"unhandled command {args.command}")


def main(argv: Sequence[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    if raw[:1] == ["reader"]:  # local operator reader authority (E12)
        from .reader_cli import main as reader_main

        return reader_main(raw[1:])
    args = _parser().parse_args(argv)
    try:
        mutation = (
            _operator_lock(args.root)
            if args.command in {"demo", "resume", "retry"}
            or (args.command == "assets" and args.asset_command == "install")
            else nullcontext()
        )
        with mutation:
            code, payload = _dispatch(args)
    except OperatorBusy:
        code, payload = ExitCode.BLOCKED, _result(
            args.command, "blocked",
            "A local action or unexpired worker lease is active; wait before resuming",
            data={"retryable": True},
        )
    except (FileNotFoundError, KeyError):
        code, payload = ExitCode.NOT_FOUND, _result(
            args.command,
            "not_found",
            "Requested local job, bundle, or trust material was not found",
        )
    except Exception as exc:
        verification_error_names = {
            "AssetEvidenceInputError",
            "AssetIntegrityError",
            "AssetPackageError",
            "BundleError",
            "BundleFilesystemError",
            "BundleFormatError",
            "BundleIntegrityError",
            "SigningError",
            "UnknownKeyError",
            "RevokedKeyError",
            "WrongPurposeError",
            "InvalidSignatureError",
            "TrustNamespaceError",
        }
        if any(base.__name__ in verification_error_names for base in type(exc).mro()):
            asset_failure = args.command == "assets"
            code, payload = ExitCode.VERIFICATION_FAILED, _result(
                args.command,
                "verification_failed",
                (
                    "Asset package or independent authority input verification failed"
                    if asset_failure
                    else "Bundle or development trust verification failed"
                ),
            )
        elif any(
            base.__name__
            in {
                "AssetAuthorityError",
                "AssetCapacityError",
                "AssetConflictError",
                "AssetFilesystemError",
            }
            for base in type(exc).mro()
        ):
            code, payload = ExitCode.BLOCKED, _result(
                args.command,
                "blocked",
                "Synthetic asset operation was blocked before use",
            )
        elif args.command == "assets" and isinstance(exc, OSError):
            code, payload = ExitCode.RETRYABLE_FAILURE, _result(
                args.command,
                "retryable_failure",
                "Local asset storage operation failed; retry after checking the filesystem",
                data={"retryable": True},
            )
        elif args.command in {"demo", "resume", "retry", "pause"}:
            code, payload = ExitCode.RETRYABLE_FAILURE, _result(
                args.command,
                "retryable_failure",
                "Local synthetic runner action failed; inspect redacted logs before retrying",
                data={"code": "TBX-JOB-001", "retryable": True},
            )
        else:
            code, payload = ExitCode.BLOCKED, _result(
                args.command,
                "blocked",
                "Command was blocked without exposing local input details",
            )
    _emit(payload, as_json=getattr(args, "as_json", False))
    return int(code)


__all__ = ["ExitCode", "main"]
