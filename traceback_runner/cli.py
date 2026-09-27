"""Local, synthetic-only Traceback operator CLI."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import platform
import shutil
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from enum import IntEnum
from pathlib import Path
from typing import Any

from .contracts import JobState
from .operator import build_job_view, support_payload
from .protocol import render_protocol, synthetic_protocol_manifest
from .serialization import canonical_json_bytes

_TRUST_RELATIVE = Path("trust/development-result-trust.json")
_BAM_NAME = "valid_modbam.bam"
_INDEX_NAME = "valid_modbam.bam.bai"
_WORKFLOW_ID = "synthetic-development-v1"


class ExitCode(IntEnum):
    """Stable process exits shared by human and JSON command output."""

    OK = 0
    USAGE = 2
    BLOCKED = 3
    NOT_FOUND = 4
    VERIFICATION_FAILED = 5
    RETRYABLE_FAILURE = 6
    INTERNAL_ERROR = 7


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


def _replace_trust(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    if temporary.exists():
        temporary.unlink()
    with temporary.open("xb") as stream:
        stream.write(content)
        stream.flush()
    temporary.replace(path)


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


def _publish_verified_record(root: Path, verified: Any, source: Path) -> Path:
    destination = root / "records" / (
        f"{verified.manifest.record_id}-{verified.manifest.signing_key_id}"
    )
    if destination.exists():
        return destination
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, destination)
    return destination


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
        signing_key = generate_development_keypair(KeyPurpose.RESULT)
        _replace_trust(trust_path, development_trust_bytes(signing_key))
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
        observed_at=datetime.fromtimestamp(record.updated_at, UTC),
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
        "Retry queued; verified stage receipts remain reusable",
        data={"job_id": record.job_id, "state": record.state.value},
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
    if record.state not in {JobState.PAUSED, JobState.RETRYABLE_FAILURE, JobState.QUEUED}:
        return ExitCode.BLOCKED, _result(
            "resume",
            "blocked",
            f"Job cannot resume from state {record.state.value}",
            data={"job_id": record.job_id, "state": record.state.value},
        )
    signing_key = generate_development_keypair(KeyPurpose.RESULT)
    trust_path = args.root / _TRUST_RELATIVE
    _replace_trust(trust_path, development_trust_bytes(signing_key))
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
    if args.command == "support-bundle":
        return _support_bundle(args)
    raise AssertionError(f"unhandled command {args.command}")


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        code, payload = _dispatch(args)
    except (FileNotFoundError, KeyError):
        code, payload = ExitCode.NOT_FOUND, _result(
            args.command,
            "not_found",
            "Requested local job, bundle, or trust material was not found",
        )
    except Exception as exc:
        verification_error_names = {
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
            code, payload = ExitCode.VERIFICATION_FAILED, _result(
                args.command,
                "verification_failed",
                "Bundle or development trust verification failed",
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
