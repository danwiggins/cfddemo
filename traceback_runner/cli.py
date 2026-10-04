"""Local Traceback operator CLI.

``demo`` runs the signed synthetic development workflow.  ``run`` measures a
local BAM against a registered reference with a locked, unqualified method and
signs the record with a development-local key: unqualified, local, not for
clinical use.  Nothing is uploaded.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import platform
import re
import shutil
import stat
import sys
import tempfile
import uuid
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager, nullcontext
from datetime import UTC, datetime
from enum import IntEnum, StrEnum
from pathlib import Path
from typing import Any

from .contracts import JobState
from .filesystem import rename_directory_exclusive_at
from .operator import build_job_view, support_payload
from .protocol import render_protocol, synthetic_protocol_manifest
from .references import DOCS_ANCHOR, ReferenceProblem, validate_reference_id
from .awake import stay_awake
from .runner import TerminalStageError
from .store import StaleLease
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


def _reference_id_argument(value: str) -> str:
    try:
        return validate_reference_id(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _assembly_argument(value: str) -> str:
    import re

    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", value):
        raise argparse.ArgumentTypeError(
            "assembly name must match ^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$"
        )
    return value


def _trust_registry_identity_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--trust-registry-id")
    parser.add_argument("--trust-registry-epoch")
    parser.add_argument("--trust-registry-head")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="traceback")
    commands = parser.add_subparsers(dest="command", required=True)

    doctor = commands.add_parser("doctor", help="check the local runtime and ROOT")
    _root_argument(doctor)
    doctor.add_argument(
        "--deep",
        action="store_true",
        help="also re-hash each registered FASTA (slow: reads every byte)",
    )
    doctor.add_argument("--json", action="store_true", dest="as_json")

    reference = commands.add_parser(
        "reference", help="register a local reference FASTA (unqualified)"
    )
    reference_commands = reference.add_subparsers(dest="reference_command", required=True)
    register = reference_commands.add_parser(
        "register", help="record contig names, lengths and M5 digests of a FASTA"
    )
    register.add_argument("--fasta", required=True, type=Path)
    register.add_argument(
        "--id", required=True, dest="reference_id", type=_reference_id_argument
    )
    register.add_argument(
        "--assembly",
        type=_assembly_argument,
        help="assembly name a BAM's @SQ AS must equal; without it AS is not compared",
    )
    _root_argument(register)
    register.add_argument("--json", action="store_true", dest="as_json")

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
    preflight.add_argument(
        "--reference",
        dest="reference_id",
        type=_reference_id_argument,
        help="registered reference ID under ROOT (default: the synthetic reference)",
    )
    _root_argument(preflight)
    preflight.add_argument("--json", action="store_true", dest="as_json")

    run = commands.add_parser(
        "run",
        help="measure a local BAM against a registered reference "
        "(unqualified, local, not for clinical use)",
    )
    run.add_argument("input", type=Path)
    run.add_argument("--index", type=Path, help="BAM index (default: BAM.bai)")
    run.add_argument(
        "--reference",
        dest="reference_id",
        type=_reference_id_argument,
        help="registered reference ID under ROOT (required)",
    )
    _root_argument(run)
    run.add_argument("--json", action="store_true", dest="as_json")

    for name in ("status", "logs", "pause", "resume", "retry"):
        command = commands.add_parser(name)
        command.add_argument("job_id")
        _root_argument(command)
        command.add_argument("--json", action="store_true", dest="as_json")
        if name == "status":
            command.add_argument(
                "--trust-registry",
                type=Path,
                help="protected result-trust registry root: check the record "
                "against its current trust (needs the retained ID, epoch, head)",
            )
            _trust_registry_identity_arguments(command)

    catalog = commands.add_parser(
        "catalog", help="catalog local records for the explorer (unqualified)"
    )
    catalog_commands = catalog.add_subparsers(dest="catalog_command", required=True)
    catalog_import = catalog_commands.add_parser(
        "import",
        help="import one local record under ROOT as development_unqualified",
    )
    catalog_import.add_argument("bundle", type=Path, help="record directory (ROOT/records/ID)")
    _root_argument(catalog_import)
    catalog_import.add_argument("--json", action="store_true", dest="as_json")

    serve = commands.add_parser(
        "serve",
        help="serve ROOT's jobs and catalog on loopback to an operator browser session "
        "(unqualified, local, not for clinical use)",
    )
    _root_argument(serve)
    serve.add_argument("--ipv6", action="store_true", help="bind ::1 instead of 127.0.0.1")

    inspect = commands.add_parser("inspect", help="inspect an unverified local bundle")
    inspect.add_argument("bundle", type=Path)
    inspect.add_argument("--json", action="store_true", dest="as_json")

    verify = commands.add_parser("verify", help="verify a bundle against local trust")
    verify.add_argument(
        "bundle", type=Path, help="bundle directory, or a record ID with --root"
    )
    trust_source = verify.add_mutually_exclusive_group(required=True)
    trust_source.add_argument("--trust-store", type=Path)
    trust_source.add_argument(
        "--root",
        dest="verify_root",
        type=Path,
        help="resolve the record ID under ROOT/records and use ROOT's trust store",
    )
    trust_source.add_argument(
        "--trust-registry",
        type=Path,
        help="protected result-trust registry root (needs the retained ID, epoch, head)",
    )
    _trust_registry_identity_arguments(verify)
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


LOCAL_DATA_ORIGIN = "local_unqualified"


def _local_envelope(payload: dict[str, Any]) -> dict[str, Any]:
    """Re-label a v1 envelope as ``traceback.cli-result.v2`` for local data.

    v1 hard-codes ``synthetic_only: true``; a result about a real local input
    carries ``data_origin: "local_unqualified"`` instead.  Commands that touch
    only synthetic material keep their v1 bytes.
    """

    if payload.get("schema_version") != "traceback.cli-result.v1":
        return payload
    relabelled = {key: value for key, value in payload.items() if key != "synthetic_only"}
    relabelled["schema_version"] = "traceback.cli-result.v2"
    relabelled["data_origin"] = LOCAL_DATA_ORIGIN
    return relabelled


class RunProblem(ReferenceProblem):
    """A ``traceback run`` failure with the same six operator fields.

    ``data`` adds measured facts (for example required and available bytes);
    it never carries a host path.
    """

    def __init__(
        self,
        code: str,
        summary: str,
        *,
        cause: str,
        fix: str,
        exit_code: int = 3,
        retryable: bool = False,
        data: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(
            code, summary, cause=cause, fix=fix, exit_code=exit_code, retryable=retryable
        )
        self.data = dict(data or {})


def _problem(
    command: str,
    problem: ReferenceProblem,
) -> dict[str, Any]:
    """Uniform operator problem: code, summary, cause, fix, retryable, docs."""

    return _result(
        command,
        "not_found"
        if problem.exit_code == ExitCode.NOT_FOUND
        else "retryable_failure"
        if problem.exit_code == ExitCode.RETRYABLE_FAILURE
        else "blocked",
        problem.summary,
        data={
            **getattr(problem, "data", {}),
            "code": problem.code,
            "cause": problem.cause,
            "fix": problem.fix,
            "retryable": problem.retryable,
            "docs": _docs_anchor(problem.code),
        },
    )


def _docs_anchor(code: str) -> str:
    """The operator guide's troubleshooting anchor for one ``TBX-*`` code."""

    return f"{DOCS_ANCHOR.split('#', 1)[0]}#{code.lower()}"


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


_DOCTOR_MIN_FREE_BYTES = 10 * 1024**3


def _doctor_check(name: str, status: str, detail: str, **extra: Any) -> dict[str, Any]:
    return {"name": name, "status": status, "detail": detail, **extra}


def _doctor_samtools() -> dict[str, Any]:
    import re
    import subprocess

    executable = shutil.which("samtools")
    if executable is None:
        return _doctor_check(
            "samtools",
            "warn",
            "samtools not on PATH; only needed to index BAM/FASTA (brew install samtools)",
        )
    try:
        completed = subprocess.run(
            [executable, "--version"],
            capture_output=True,
            text=True,
            errors="replace",  # Debian's samtools prints Latin-1 bytes
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return _doctor_check("samtools", "warn", "samtools --version could not be run")
    match = re.match(r"samtools (\d+\.\d+(?:\.\d+)?)", completed.stdout)
    if completed.returncode != 0 or match is None:
        return _doctor_check("samtools", "warn", "samtools --version output did not parse")
    return _doctor_check("samtools", "pass", f"samtools {match.group(1)}")


def _doctor_minimap2() -> dict[str, Any]:
    """WARN, never block: minimap2 is only for aligning MinKNOW output."""

    if shutil.which("minimap2") is None:
        return _doctor_check(
            "minimap2",
            "warn",
            "minimap2 not on PATH; only needed to align unaligned MinKNOW/Dorado BAMs "
            "(brew install minimap2)",
        )
    return _doctor_check("minimap2", "pass", "minimap2 on PATH")


def _existing_ancestor(path: Path) -> Path:
    candidate = path
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    return candidate


def _doctor_root(root: Path) -> dict[str, Any]:
    if root.is_symlink() or (root.exists() and not root.is_dir()):
        return _doctor_check("root", "warn", "ROOT exists but is not a directory")
    if not root.exists():
        ancestor = _existing_ancestor(root)
        if ancestor.is_dir() and os.access(ancestor, os.W_OK | os.X_OK):
            return _doctor_check("root", "pass", "ROOT does not exist yet and can be created")
        return _doctor_check("root", "warn", "ROOT does not exist and cannot be created here")
    metadata = root.stat()
    if metadata.st_uid != os.geteuid():
        return _doctor_check("root", "warn", "ROOT is owned by another user")
    if stat.S_IMODE(metadata.st_mode) & 0o022:
        return _doctor_check(
            "root", "warn", "ROOT is group- or other-writable; run chmod go-w ROOT"
        )
    return _doctor_check("root", "pass", "ROOT exists, is owned by you and is not shared-writable")


def _doctor_disk(root: Path) -> dict[str, Any]:
    try:
        free = shutil.disk_usage(_existing_ancestor(root)).free
    except OSError:
        return _doctor_check("disk", "warn", "free space on ROOT's volume could not be read")
    gib = free / 1024**3
    if free < _DOCTOR_MIN_FREE_BYTES:
        return _doctor_check(
            "disk",
            "warn",
            f"{gib:.1f} GiB free on ROOT's volume; under 10 GiB",
            free_bytes=free,
        )
    return _doctor_check("disk", "pass", f"{gib:.1f} GiB free on ROOT's volume", free_bytes=free)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(16 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _readable_sha256(path: Path) -> str | None:
    try:
        return _file_sha256(path)
    except OSError:
        return None


def _doctor_references(root: Path, *, deep: bool) -> list[dict[str, Any]]:
    from .references import list_reference_ids, load_reference

    try:
        identifiers = list_reference_ids(root)
    except OSError:
        return [
            _doctor_check(
                "reference", "warn", "ROOT/references cannot be read; check its permissions"
            )
        ]
    if not identifiers:
        return [
            _doctor_check(
                "reference",
                "warn",
                "no reference registered under ROOT; run traceback reference register",
            )
        ]
    checks = []
    for identifier in identifiers:
        try:
            loaded = load_reference(root, identifier)
        except ReferenceProblem as problem:
            checks.append(
                _doctor_check("reference", "warn", problem.summary, reference_id=identifier)
            )
            continue
        fasta = Path(loaded.source.fasta_path)
        try:
            metadata = fasta.stat()
        except OSError:
            metadata = None
        if metadata is None or not stat.S_ISREG(metadata.st_mode):
            status, detail = "warn", "registered FASTA is missing; existing records stay valid"
        elif metadata.st_size != loaded.source.fasta_size_bytes:
            status, detail = "warn", "registered FASTA size changed since registration"
        elif deep and (digest := _readable_sha256(fasta)) is None:
            status, detail = "warn", "registered FASTA could not be read"
        elif deep and digest != loaded.registered.asset_sha256:
            status, detail = "warn", "registered FASTA bytes changed since registration"
        else:
            status = "pass"
            detail = (
                "FASTA present; SHA-256 matches the registration"
                if deep
                else "FASTA present with the registered size (--deep re-hashes it)"
            )
        checks.append(_doctor_check("reference", status, detail, reference_id=identifier))
    return checks


def _doctor_trust(root: Path) -> dict[str, Any]:
    from .signing import load_development_trust

    records = root / "records"
    has_records = records.is_dir() and any(
        not entry.name.startswith(".") for entry in records.iterdir()
    )
    if not has_records:
        return _doctor_check(
            "trust", "pass", "no records under ROOT; trust is created by the first signed run"
        )
    try:
        load_development_trust((root / _TRUST_RELATIVE).read_bytes())
    except Exception:
        return _doctor_check(
            "trust",
            "blocked",
            "records exist but trust/development-result-trust.json is missing or invalid; "
            "every later verify would fail",
        )
    registry = root / "trust" / "result-trust-registry"
    if not (registry.exists() or registry.is_symlink()):
        return _doctor_check(
            "trust",
            "pass",
            "development trust parses; no result-trust registry under ROOT yet",
        )
    if registry.is_symlink() or not registry.is_dir():
        return _doctor_check(
            "trust", "blocked", "result-trust registry under ROOT/trust is not a directory"
        )
    from evidence_inspector.result_trust_registry import (
        ResultTrustRegistry,
        ResultTrustRegistryUnsafe,
    )

    # Doctor does not hold the independently retained ID, epoch and head, so a
    # full open is impossible. The registry's own constructor validates lock,
    # journal and metadata first and only then compares the expected identity;
    # reaching that identity refusal therefore proves the structure is sound.
    # `verify --trust-registry` performs the identity-bound check.
    try:
        ResultTrustRegistry(registry).close()
    except ResultTrustRegistryUnsafe as exc:
        if str(exc) == _REGISTRY_IDENTITY_REQUIRED:
            return _doctor_check(
                "trust",
                "pass",
                "development trust parses; result-trust registry structure opens "
                "(identity is checked by verify --trust-registry)",
            )
    except Exception:
        pass
    return _doctor_check(
        "trust", "blocked", "result-trust registry under ROOT/trust does not open"
    )


_REGISTRY_IDENTITY_REQUIRED = (
    "result trust registry expected identity and head are required and must match"
)


def _doctor(args: argparse.Namespace) -> tuple[ExitCode, dict[str, Any]]:
    root = Path(os.path.abspath(args.root))
    checks = [
        _doctor_check(
            "python",
            "pass" if sys.version_info[:2] == (3, 11) else "blocked",
            "Python 3.11 runtime",
        ),
        _doctor_check("platform", "pass", f"{platform.system()} {platform.machine()}"),
        _doctor_check("data_boundary", "pass", "local execution; network not required"),
        _doctor_samtools(),
        _doctor_minimap2(),
        _doctor_root(root),
        _doctor_disk(root),
        *_doctor_references(root, deep=args.deep),
        _doctor_trust(root),
    ]
    runtime_blocked = checks[0]["status"] == "blocked"
    trust_blocked = any(
        check["name"] == "trust" and check["status"] == "blocked" for check in checks
    )
    warnings = sum(check["status"] == "warn" for check in checks)
    if runtime_blocked:
        summary = "Synthetic runtime is blocked"
    elif trust_blocked:
        summary = "Records exist under ROOT but their trust material is missing or invalid"
    else:
        summary = (
            "Synthetic local runtime is available; local runs "
            "(traceback run --reference) are unqualified and not for clinical use"
        )
        if warnings:
            summary += f" ({warnings} warning{'s' if warnings != 1 else ''})"
    blocked = runtime_blocked or trust_blocked
    return (
        ExitCode.BLOCKED if blocked else ExitCode.OK,
        _result(
            "doctor",
            "blocked" if blocked else "ok",
            summary,
            data={"root": str(root), "checks": checks},
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
        "A local run needs a registered reference; no job was created",
        data={
            "code": "TBX-RUN-003",
            "docs": _docs_anchor("TBX-RUN-003"),
            "fix": (
                "Register the FASTA with traceback reference register, then pass "
                "--reference ID; use traceback demo for the synthetic workflow"
            ),
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
    All-synthetic trust stays a v1 document (unchanged bytes); adding a
    ``development-local`` key writes a v2 document holding every key.
    """
    from .signing import (
        development_trust_document_bytes,
        merge_development_trust_documents,
        parse_development_trust_document,
    )

    documents = [parse_development_trust_document(content)]
    if path.exists():
        documents.insert(0, parse_development_trust_document(path.read_bytes()))
    merged = merge_development_trust_documents(*documents)
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
    # Local (v3) records publish at ROOT/records/<record_id>, the path the
    # golden path names; synthetic records keep their key-suffixed name.
    name = (
        verified.manifest.record_id
        if verified.manifest.schema_version == "traceback.result-bundle.v3"
        else f"{verified.manifest.record_id}-{verified.manifest.signing_key_id}"
    )
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


def _execute_signed_run(
    root: Path,
    runner: Any,
    request: Any,
    source: Path,
    relative_files: tuple[str, ...],
    stages: Callable[[Any], tuple[Any, ...]],
    *,
    namespace: Any,
    worker_id: str,
    on_submitted: Callable[[Any], None] | None = None,
) -> Any:
    """Seal the input, then run the signed stages unless the job is complete.

    Shared by ``demo`` (synthetic) and ``run`` (local).  A signing key in
    ``namespace`` is taken only when stages must run (a fresh ephemeral key for
    ``demo``; ROOT's one persistent development-local key for ``run``), and its
    public half is appended to ``ROOT``'s development trust first.
    """
    from .signing import development_trust_bytes

    record = runner.submit(request, source, relative_files)
    if on_submitted is not None:
        on_submitted(record)
    if record.state != JobState.COMPLETE:
        _reject_live_worker(runner, record.job_id)
        signing_key = _signing_key_for(root, namespace)
        _append_development_trust(root / _TRUST_RELATIVE, development_trust_bytes(signing_key))
        job_stages = stages(signing_key)
        if record.state in {JobState.PAUSED, JobState.RETRYABLE_FAILURE}:
            record = runner.resume(record.job_id, job_stages, worker_id=worker_id)
        else:
            record = runner.execute(record.job_id, job_stages, worker_id=worker_id)
    return record


def _publish_signed_record(root: Path, runner: Any, job_id: str) -> tuple[Any, Path]:
    """Verify the runner's signed bundle, publish it under ROOT/records, re-verify."""
    from .bundles import verify_bundle
    from .signing import load_development_trust

    trust_store = load_development_trust((root / _TRUST_RELATIVE).read_bytes())
    runner_bundle = _signed_bundle_from_outputs(runner, job_id)
    verified = verify_bundle(runner_bundle, trust_store)
    published = _publish_verified_record(root, verified, runner_bundle)
    verify_bundle(published, trust_store)
    return verified, published


def _demo(args: argparse.Namespace) -> tuple[ExitCode, dict[str, Any]]:
    from .contracts import InputKind, JobRequest
    from .runner import Runner
    from .signing import TrustNamespace
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
    record = _execute_signed_run(
        root,
        runner,
        request,
        source,
        relative_files,
        _demo_stages,
        namespace=TrustNamespace.DEVELOPMENT_SYNTHETIC,
        worker_id="synthetic-cli",
    )
    verified, published = _publish_signed_record(root, runner, record.job_id)
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


# --- traceback run: one local BAM, a registered reference, unqualified ------

_LOCAL_WORKFLOW_ID = "local-unqualified-v0"
_LOCAL_SAMPLE_PREFIX = "local-"
_LOCAL_PREFLIGHT_POLICY = "local-unqualified-preflight-v1"
_PROVENANCE_KEY_RELATIVE = Path("trust/provenance-hmac.key")
_INDEX_SUFFIXES = (".bai", ".csi")
_STAGE_HEARTBEAT_SECONDS = 5.0
_PROBLEM_CODE = re.compile(r"^(TBX-[A-Z]+-[0-9]{3})\b")


class LocalStageRefusal(TerminalStageError):
    """A local stage refused its sealed input; retrying cannot change that.

    The message starts with the operator code, so the runner's stored
    failure reason names it without naming any input locator.
    """

    def __init__(self, code: str, summary: str, *, cause: str, fix: str) -> None:
        super().__init__(f"{code}: {summary}")
        self.code = code
        self.summary = summary
        self.cause = cause
        self.fix = fix


def _local_workflow_sha256() -> str:
    return hashlib.sha256(_LOCAL_WORKFLOW_ID.encode("ascii")).hexdigest()


def _is_local_request(request: Any) -> bool:
    return request.workflow_release_sha256 == _local_workflow_sha256() and (
        request.sample_token.startswith(_LOCAL_SAMPLE_PREFIX)
    )


def _provenance_hmac_key(root: Path) -> bytes:
    """Return ROOT's 32-byte provenance HMAC key, creating it once (0600).

    A per-root random key means the same BAM run under two roots yields
    unlinkable ``provider_hmac_sha256`` commitments.
    """

    path = root / _PROVENANCE_KEY_RELATIVE
    path.parent.mkdir(parents=True, exist_ok=True)
    nofollow = getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(
            path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | nofollow, 0o600
        )
    except FileExistsError:
        pass
    else:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(os.urandom(32))
            stream.flush()
            os.fsync(stream.fileno())
        _fsync_directory(path.parent)
    try:
        descriptor = os.open(path, os.O_RDONLY | nofollow)
    except OSError:
        descriptor = -1
    key = b""
    regular = False
    if descriptor >= 0:
        with os.fdopen(descriptor, "rb") as stream:
            metadata = os.fstat(stream.fileno())
            regular = (
                stat.S_ISREG(metadata.st_mode)
                and metadata.st_uid == os.geteuid()
                and not stat.S_IMODE(metadata.st_mode) & 0o077
            )
            key = stream.read(33)
    if not regular or len(key) != 32:
        raise RunProblem(
            "TBX-RUN-006",
            "The provenance key under ROOT/trust is not a private 32-byte file",
            cause=(
                "ROOT/trust/provenance-hmac.key was edited, truncated, replaced, "
                "or is readable by other users (it must be 0600 and yours)"
            ),
            fix="Use a fresh --root; never edit files under ROOT/trust",
        )
    return key


_LOCAL_SIGNING_KEY_RELATIVE = Path("trust/development-local-signing.key")


def _local_signing_key(root: Path) -> Any:
    """Return ROOT's one development-local result signing key, creating it once (0600).

    Every local record under ROOT is signed by this key, so ROOT's trust
    document and result-trust registry carry one local key, not one per record
    (the registry holds at most ``MAX_TRUST_KEYS``).  Same pattern as the
    provenance HMAC key: the 32-byte Ed25519 seed must be a regular file owned
    by the user with no group or other access, or ``run`` refuses (TBX-RUN-007).
    """
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from .signing import DevelopmentSigningKey, KeyPurpose, TrustNamespace, trusted_key_id

    path = root / _LOCAL_SIGNING_KEY_RELATIVE
    path.parent.mkdir(parents=True, exist_ok=True)
    nofollow = getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    if not (path.exists() or path.is_symlink()):
        # Write and fsync a private temporary, then publish it with a
        # no-replace link: a crash never leaves a short key at the final path.
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        temporary.unlink(missing_ok=True)
        descriptor = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | nofollow, 0o600
        )
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(os.urandom(32))
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, path)
            except FileExistsError:
                pass
            _fsync_directory(path.parent)
        finally:
            temporary.unlink(missing_ok=True)
    try:
        descriptor = os.open(path, os.O_RDONLY | nofollow)
    except OSError:
        descriptor = -1
    seed = b""
    private = False
    if descriptor >= 0:
        with os.fdopen(descriptor, "rb") as stream:
            metadata = os.fstat(stream.fileno())
            private = (
                stat.S_ISREG(metadata.st_mode)
                and metadata.st_uid == os.geteuid()
                and stat.S_IMODE(metadata.st_mode) == 0o600
            )
            seed = stream.read(33)
    if not private or len(seed) != 32:
        raise RunProblem(
            "TBX-RUN-007",
            "The local signing key under ROOT/trust is not a private 32-byte file",
            cause=(
                "ROOT/trust/development-local-signing.key was edited, truncated, "
                "replaced, or is readable by other users (it must be 0600 and yours)"
            ),
            fix="Use a fresh --root; never edit files under ROOT/trust",
        )
    private_key = Ed25519PrivateKey.from_private_bytes(seed)
    public = private_key.public_key().public_bytes_raw()
    return DevelopmentSigningKey(
        key_id=trusted_key_id(
            public, KeyPurpose.RESULT, namespace=TrustNamespace.DEVELOPMENT_LOCAL
        ),
        purpose=KeyPurpose.RESULT,
        private_key=private_key,
        namespace=TrustNamespace.DEVELOPMENT_LOCAL,
    )


def _signing_key_for(root: Path, namespace: Any) -> Any:
    """ROOT's persistent key for local records; a fresh ephemeral key for synthetic ones."""
    from .signing import KeyPurpose, TrustNamespace, generate_development_keypair

    if namespace == TrustNamespace.DEVELOPMENT_LOCAL:
        return _local_signing_key(root)
    return generate_development_keypair(KeyPurpose.RESULT, namespace=namespace)


@contextmanager
def _stage_heartbeat(context: Any) -> Iterator[None]:
    """Keep a long stage's worker lease alive while it reads a large BAM."""
    import threading

    stop = threading.Event()

    def beat() -> None:
        while not stop.wait(_STAGE_HEARTBEAT_SECONDS):
            try:
                context.heartbeat()
            except Exception:
                return  # the runner's own post-stage heartbeat reports a lost lease

    thread = threading.Thread(target=beat, name="traceback-stage-heartbeat", daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join()


def _policy_lines(policy: Any) -> list[str]:
    bins = " ".join(
        f"[{item.lower_inclusive},{'inf' if item.upper_exclusive is None else item.upper_exclusive})"
        for item in policy.bins
    )
    return [
        f"POLICY  {policy.definition_id} (locked; unqualified, local, not for clinical use)",
        f"CONTIGS  {len(policy.contigs)}: {' '.join(policy.contigs)}",
        f"FILTERS  MAPQ >= {policy.min_mapping_quality}; primary, mapped, non-duplicate, "
        "non-QC-fail alignments only",
        f"BINS  {bins} bp",
    ]


def _local_stages(
    root: Path,
    loaded: Any,
    bam_name: str,
    index_name: str,
    signing_key: Any,
    progress: Callable[[str], None],
) -> tuple[Any, ...]:
    from .bundles import build_result_bundle
    from .contracts import (
        ArtifactCommitment,
        ExportRunProvenance,
        InputKind,
        PreflightOutcome,
        PreflightReport,
        StageName,
        parse_fragment_measurement,
    )
    from .local_authority import local_fragment_policy, local_method_identity
    from .measurement import (
        MeasurementUnavailableError,
        finalize_measurement,
        scan_aligned_reference_spans,
    )
    from .preflight import BamPreflightPolicy, validate_bam_snapshot
    from .runner import StageResult, StageSpec

    registered = loaded.registered
    policy = local_fragment_policy(registered)
    preflight_policy = BamPreflightPolicy(policy_id=_LOCAL_PREFLIGHT_POLICY)

    def reference_match(report: Any) -> str:
        outcome = next(
            check.outcome for check in report.checks if check.code == "TBX-BAM-002"
        )
        return (
            "registered_digests"
            if outcome == PreflightOutcome.PASS
            else "name_and_length_only"
        )

    def preflight_stage(context: Any) -> Any:
        progress("STAGE  preflight: inspecting the sealed BAM copy")
        try:
            with _stage_heartbeat(context):
                report = validate_bam_snapshot(
                    context.sealed_input_dir / bam_name,
                    context.sealed_input_dir / index_name,
                    registered,
                    preflight_policy,
                    compare_assembly=loaded.source.assembly_declared,
                )
        except (StaleLease, TerminalStageError):
            raise
        except Exception as exc:
            # Not a BAM read/format error (those are TBX-BAM-001 checks): a
            # sealed input cannot change, so retrying would only loop.
            raise LocalStageRefusal(
                "TBX-INTERNAL-001",
                "Preflight stopped on an unexpected internal error; no record was made",
                cause=f"{type(exc).__name__} while inspecting the sealed BAM copy",
                fix="Retrying will not change it; write `traceback support-bundle` and report the code",
            ) from exc
        if (
            report.outcome == PreflightOutcome.BLOCKED
            or not report.fragment_measurement_eligible
        ):
            blocked = [
                check for check in report.checks if check.outcome == PreflightOutcome.BLOCKED
            ]
            codes = sorted({check.code for check in blocked})
            code = "TBX-BAM-002" if "TBX-BAM-002" in codes else (codes or ["TBX-BAM-001"])[0]
            raise LocalStageRefusal(
                code,
                "Preflight blocked fragment measurement; no record was made",
                cause="; ".join(check.problem for check in blocked)
                or "the input is not eligible for fragment measurement",
                fix="Run traceback preflight BAM --reference ID for each check's remediation",
            )
        output = context.attempt_dir / "preflight.json"
        output.write_bytes(canonical_json_bytes(report))
        progress(f"STAGE  preflight {report.outcome.value}: fragment measurement eligible")
        return StageResult(
            outputs={"preflight_report": output.name},
            metadata={
                "fragment_measurement_eligible": True,
                "future_methylation_eligible": report.future_methylation_eligible,
                "preflight_outcome": report.outcome.value,
                "reference_match": reference_match(report),
                "data_origin": LOCAL_DATA_ORIGIN,
            },
        )

    def measurement_stage(context: Any) -> Any:
        progress("STAGE  measure: scanning every record of the sealed BAM copy")
        with _stage_heartbeat(context):
            scan = scan_aligned_reference_spans(context.sealed_input_dir / bam_name, policy)
        try:
            measurement = finalize_measurement(scan)
        except MeasurementUnavailableError as exc:
            raise LocalStageRefusal(
                "TBX-RUN-005",
                "Measurement unavailable: no complete eligible denominator",
                cause=(
                    f"scan {scan.completion.value}; {scan.records_scanned} records "
                    f"scanned, {scan.eligible_alignments} eligible"
                ),
                fix=(
                    "Check that the BAM's contig names match the policy contigs, that "
                    "alignments reach MAPQ 20, and that they are not all duplicate, "
                    "secondary, supplementary or QC-fail"
                ),
            ) from exc
        output = context.attempt_dir / "measurement.json"
        output.write_bytes(canonical_json_bytes(measurement))
        progress(
            f"STAGE  measure: {measurement.eligible_alignments} eligible alignments "
            f"of {measurement.records_scanned} records"
        )
        return StageResult(
            outputs={"fragment_measurement": output.name},
            metadata={
                "records_scanned": measurement.records_scanned,
                "eligible_alignments": measurement.eligible_alignments,
                "complete": True,
                "data_origin": LOCAL_DATA_ORIGIN,
            },
        )

    def signing_stage(context: Any) -> Any:
        progress("STAGE  sign: development-local key (development trust only)")
        report = PreflightReport.model_validate_json(
            (context.prior_stage_dirs[0] / "preflight.json").read_bytes()
        )
        measurement = parse_fragment_measurement(
            (context.prior_stage_dirs[-1] / "measurement.json").read_bytes()
        )
        manifest = json.loads(
            (context.sealed_input_dir / "input-manifest.local.json").read_bytes()
        )
        sealed = {item["relative_path"]: item for item in manifest["files"]}
        provider_hmac = hmac.new(
            _provenance_hmac_key(root),
            b"traceback.provider-artifact.v1|"
            + bytes.fromhex(sealed[bam_name]["sha256_local"]),
            hashlib.sha256,
        ).hexdigest()
        provenance = ExportRunProvenance(
            run_token=f"local-run-{context.job_id[:16]}",
            input_kind=InputKind.MODBAM,
            protocol_run_token="no-approved-protocol",
            workflow_release_id=_LOCAL_WORKFLOW_ID,
            artifacts=(
                ArtifactCommitment(
                    role="analysis_bam",
                    artifact_token="local-analysis-bam",
                    size_bytes=int(sealed[bam_name]["size_bytes"]),
                    provider_hmac_sha256=provider_hmac,
                ),
            ),
        )
        bundle = build_result_bundle(
            context.attempt_dir / "bundle",
            measurement=measurement,
            provenance=provenance,
            method=local_method_identity(registered),
            signing_key=signing_key,
            reference_match=reference_match(report),
        )
        bundle_files = sorted(path for path in bundle.rglob("*") if path.is_file())
        outputs = {
            f"bundle_{index:02d}": path.relative_to(context.attempt_dir).as_posix()
            for index, path in enumerate(bundle_files)
        }
        return StageResult(
            outputs=outputs,
            metadata={
                "signed": True,
                "development_trust_only": True,
                "data_origin": LOCAL_DATA_ORIGIN,
            },
        )

    return (
        StageSpec(
            name=StageName.VALIDATE,
            version="1",
            callback=preflight_stage,
            parameters={
                "policy": preflight_policy.policy_id,
                "reference_id": registered.reference_id,
                "reference_asset_sha256": registered.asset_sha256,
                "compare_assembly": loaded.source.assembly_declared,
                "data_origin": LOCAL_DATA_ORIGIN,
            },
        ),
        StageSpec(
            name=StageName.MEASURE,
            version="1",
            callback=measurement_stage,
            parameters={
                "definition": policy.definition_id,
                "policy_sha256": hashlib.sha256(canonical_json_bytes(policy)).hexdigest(),
                "data_origin": LOCAL_DATA_ORIGIN,
            },
        ),
        StageSpec(
            name=StageName.SIGN,
            version="1",
            callback=signing_stage,
            parameters={
                "key_id": signing_key.key_id,
                "development_trust_only": True,
                "data_origin": LOCAL_DATA_ORIGIN,
            },
        ),
    )


def _refuse_unaligned_or_empty(bam: Path, *, fasta: str | None) -> None:
    """A1: refuse an unaligned (TBX-BAM-003) or record-less (TBX-BAM-004) BAM
    before any job, authority or copy exists. ``fasta`` (human output only)
    replaces the ``REF.fa`` placeholder in the alignment command."""

    import shlex

    from .preflight import intake_refusal, unaligned_remediation

    path = Path(os.path.abspath(bam))
    if path.is_symlink() or not path.is_file():
        return  # the input-file check below reports it
    check = intake_refusal(path)
    if check is None:
        return
    fix = check.remediation
    if fasta is not None and check.code == "TBX-BAM-003":
        fix = unaligned_remediation(shlex.quote(fasta))
    raise RunProblem(check.code, check.problem, cause=check.problem, fix=fix)


def _local_input_files(bam: Path, index: Path) -> tuple[Path, tuple[str, str]]:
    """Return the snapshot source root and the BAM and index names under it.

    Paths are only resolved here; they are never echoed.
    """

    bam_abs = Path(os.path.abspath(bam))
    index_abs = Path(os.path.abspath(index))
    if index_abs.suffix not in _INDEX_SUFFIXES or bam_abs.suffix in _INDEX_SUFFIXES:
        raise RunProblem(
            "TBX-BAM-001",
            "The BAM index must be a .bai or .csi file beside a BAM",
            cause="the --index file name does not end in .bai or .csi",
            fix="Index the BAM with samtools index and pass that file with --index",
        )
    for path in (bam_abs, index_abs):
        if path.is_symlink() or not path.is_file():
            raise FileNotFoundError("local input is missing or not a regular file")
    source = Path(os.path.commonpath([bam_abs.parent, index_abs.parent]))
    return source, (
        bam_abs.relative_to(source).as_posix(),
        index_abs.relative_to(source).as_posix(),
    )


def _require_free_space(root: Path, input_bytes: int) -> None:
    required = 2 * input_bytes
    available = shutil.disk_usage(_existing_ancestor(root)).free
    if available < required:
        raise RunProblem(
            "TBX-RUN-004",
            "Not enough space under ROOT; need 2x the input size",
            cause=(
                f"ROOT's volume has {available} bytes free; sealing the BAM and "
                f"index needs {required} bytes"
            ),
            fix="Free space on ROOT's volume, or pass a --root on a larger volume",
            retryable=True,
            data={"required_bytes": required, "available_bytes": available},
        )


def _no_space_problem(exc: Exception) -> RunProblem | None:
    import errno
    import sqlite3

    sqlite_full = isinstance(exc, sqlite3.OperationalError) and (
        "database or disk is full" in str(exc)
    )
    os_full = isinstance(exc, OSError) and exc.errno in {errno.ENOSPC, errno.EDQUOT}
    if not (sqlite_full or os_full):
        return None
    # The job that hit ENOSPC may hold no sealed snapshot, and the runner never
    # reseals a failed job, so this ROOT cannot finish that input.
    return RunProblem(
        "TBX-RUN-004",
        "ROOT's volume filled during the run; no record was made",
        cause="ROOT's volume ran out of space while sealing, measuring or publishing",
        fix="Free space, then run again under a fresh --root (need 2x the input size)",
        retryable=False,
    )


@contextmanager
def _no_space_mapped() -> Iterator[None]:
    import sqlite3

    try:
        yield
    except (OSError, sqlite3.OperationalError) as exc:
        problem = _no_space_problem(exc)
        if problem is None:
            raise
        raise problem from exc


def _refusal_problem(refusal: LocalStageRefusal) -> RunProblem:
    return RunProblem(refusal.code, refusal.summary, cause=refusal.cause, fix=refusal.fix)


_SEALING_STATES = frozenset({JobState.DISCOVERED, JobState.SNAPSHOTTING, JobState.VALIDATING})


def _refuse_unsealed_job(runner: Any, record: Any) -> None:
    """Refuse a job that left sealing without a snapshot (e.g. ROOT filled up).

    The runner never reseals such a job, so retry/resume/run would only end it
    in a terminal snapshot failure.
    """

    stored = runner.store.get(record.job_id)
    if record.state in _SEALING_STATES or record.state == JobState.TERMINAL_FAILURE:
        return
    if stored.snapshot_id is None:
        raise RunProblem(
            "TBX-RUN-004",
            "This input's earlier run failed before it was sealed; no record was made",
            cause=f"job {record.job_id} has no sealed input (for example ROOT filled up)",
            fix="Free space, then run again under a fresh --root (need 2x the input size)",
            data={"job_id": record.job_id, "state": record.state.value},
        )


def _refuse_failed_job(runner: Any) -> Callable[[Any], None]:
    def check(record: Any) -> None:
        _refuse_unsealed_job(runner, record)
        stored = runner.store.get(record.job_id)
        if record.state != JobState.TERMINAL_FAILURE:
            return
        last_error = stored.last_error or ""
        match = _PROBLEM_CODE.match(last_error)
        raise RunProblem(
            match.group(1) if match else "TBX-JOB-001",
            "This input already failed terminally on this ROOT; no record was made",
            cause=f"job {record.job_id} ended in terminal_failure",
            fix=(
                f"Inspect traceback status {record.job_id}; fix the input and run "
                "it again under a fresh --root"
            ),
            data={"job_id": record.job_id, "state": record.state.value},
        )

    return check


def _local_run_result(
    root: Path, runner: Any, record: Any, reference_id: str
) -> tuple[ExitCode, dict[str, Any]]:
    import shlex

    from .contracts import PreflightReport

    verified, published = _publish_signed_record(root, runner, record.job_id)
    report = PreflightReport.model_validate_json(
        runner.outputs(record.job_id, "validate")["preflight_report"].read_bytes()
    )
    measurement = verified.measurement
    absolute_root = Path(os.path.abspath(root))
    bundle_path = absolute_root / published.relative_to(root)
    trust_path = absolute_root / _TRUST_RELATIVE
    return ExitCode.OK, _result(
        "run",
        "ok",
        "Signed local record ready (development trust, unqualified, not for clinical "
        "use); nothing was uploaded",
        data={
            "job_id": record.job_id,
            "state": record.state.value,
            "record_id": verified.manifest.record_id,
            "reference_id": reference_id,
            "preflight_outcome": report.outcome.value,
            "reference_match": verified.limitations.reference_match,
            "records_scanned": measurement.records_scanned,
            "eligible_alignments": measurement.eligible_alignments,
            "bundle": published.relative_to(root).as_posix(),
            "trust_store": _TRUST_RELATIVE.as_posix(),
            "bundle_path": str(bundle_path),
            "trust_store_path": str(trust_path),
            "report_path": str(bundle_path / "report.html"),
            "next_commands": [
                f"traceback verify {shlex.quote(str(bundle_path))} "
                f"--trust-store {shlex.quote(str(trust_path))}",
                f"traceback catalog import {shlex.quote(str(bundle_path))} "
                f"--root {shlex.quote(str(root.absolute()))}",
            ],
            "verification": "verified",
            "development_trust_only": True,
            "qualified": False,
        },
    )


def _run(
    args: argparse.Namespace, progress: Callable[[str], None]
) -> tuple[ExitCode, dict[str, Any]]:
    from .contracts import InputKind, JobRequest
    from .local_authority import ensure_local_method_authority, local_fragment_policy
    from .references import load_reference
    from .snapshots import input_tree_sha256

    if args.reference_id is None:
        return _real_run_blocked()
    root = args.root
    loaded = load_reference(root, args.reference_id)
    _refuse_unaligned_or_empty(
        args.input,
        # Human output names the registered FASTA; --json never does.
        fasta=None if getattr(args, "as_json", False) else loaded.source.fasta_path,
    )
    # Create (once) or validate the local method authority before any copy:
    # a damaged ROOT/authority refuses the run with TBX-AUTH-LOCAL-001.
    ensure_local_method_authority(root, loaded.registered)
    source, relative_files = _local_input_files(
        args.input, args.index or Path(f"{args.input}.bai")
    )
    _require_free_space(
        root, sum((source / name).stat().st_size for name in relative_files)
    )
    for line in _policy_lines(local_fragment_policy(loaded.registered)):
        progress(line)
    progress("STAGE  seal: copying the BAM and index under ROOT")
    request = JobRequest(
        sample_token=f"{_LOCAL_SAMPLE_PREFIX}{args.reference_id}",
        input_kind=InputKind.MODBAM,
        input_tree_sha256_local=input_tree_sha256(source, relative_files),
        workflow_release_sha256=_local_workflow_sha256(),
    )
    return _run_sealed(root, request, source, relative_files, loaded, progress)


def _run_sealed(
    root: Path,
    request: Any,
    source: Path,
    relative_files: tuple[str, str],
    loaded: Any,
    progress: Callable[[str], None],
) -> tuple[ExitCode, dict[str, Any]]:
    from .runner import Runner

    runner = Runner(root / "runner", local_unqualified_enabled=True)
    return _run_with_runner(root, runner, request, source, relative_files, loaded, progress)


def _run_with_runner(
    root: Path,
    runner: Any,
    request: Any,
    source: Path,
    relative_files: tuple[str, str],
    loaded: Any,
    progress: Callable[[str], None],
) -> tuple[ExitCode, dict[str, Any]]:
    from .signing import TrustNamespace

    bam_name, index_name = relative_files
    refuse_failed = _refuse_failed_job(runner)
    submitted: list[str] = []

    def on_submitted(record: Any) -> None:
        submitted.append(record.job_id)
        refuse_failed(record)

    try:
        record = _execute_signed_run(
            root,
            runner,
            request,
            source,
            relative_files,
            lambda key: _local_stages(root, loaded, bam_name, index_name, key, progress),
            namespace=TrustNamespace.DEVELOPMENT_LOCAL,
            worker_id="local-cli",
            on_submitted=on_submitted,
        )
    except LocalStageRefusal as refusal:
        raise _refusal_problem(refusal) from refusal
    except StaleLease as lost:
        if not submitted:
            raise
        # The fenced store recorded nothing; the job keeps its sealed input and
        # committed stages, and resume adopts or re-runs from there.
        job_id = submitted[0]
        next_action = f"traceback resume {job_id} --root <same-root>"
        raise RunProblem(
            "TBX-JOB-001",
            "Local run lost its worker lease (for example the host slept or stalled "
            "past it); no record was made",
            cause=f"job {job_id}: {lost}",
            fix=f"Run {next_action}",
            exit_code=ExitCode.RETRYABLE_FAILURE,
            retryable=True,
            data={"job_id": job_id, "next_action": next_action},
        ) from lost
    if record.state == JobState.PAUSED:
        return ExitCode.OK, _result(
            "run",
            "ok",
            "Run paused at a stage boundary; no record yet",
            data={
                "job_id": record.job_id,
                "state": record.state.value,
                "next_action": f"traceback resume {record.job_id} --root <same-root>",
            },
        )
    return _local_run_result(root, runner, record, loaded.registered.reference_id)


def _catalog_import(args: argparse.Namespace) -> tuple[ExitCode, dict[str, Any]]:
    """Catalog one local record and persist its explorer view (B5a/B5b)."""

    from .local_catalog import import_local_record

    root = args.root
    outcome = import_local_record(root, args.bundle)
    ref = outcome.reference
    return ExitCode.OK, _result(
        "catalog import",
        "ok",
        "Record cataloged as development_unqualified (unqualified, local, not for "
        "clinical use); its explorer view is saved under ROOT/explorer",
        data={
            "result_id": ref.result_id,
            "record_id": outcome.record_id,
            "method_id": ref.method_ref.method_id,
            "method_version": ref.method_ref.version,
            "qualification_state": ref.qualification_state.value,
            "display_role": ref.display_role.value if ref.display_role else None,
            "trust_state": ref.trust_state.value,
            "current_provider_eligible": ref.current_provider_eligible,
            "explorer_artifact": outcome.explorer_artifact,
            "authority_binding": outcome.authority_binding,
            "catalog": "catalog",
            "qualified": False,
        },
    )


class ServeProblem(ReferenceProblem):
    """A ``traceback serve`` refusal with the six operator fields; no listener started."""


def _no_catalog() -> ServeProblem:
    return ServeProblem(
        "TBX-SERVE-002",
        "No catalog under ROOT; nothing was started",
        cause="ROOT/catalog does not exist: no record has been imported",
        fix="Run `traceback catalog import ROOT/records/RECORD_ID --root ROOT` first",
        exit_code=ExitCode.NOT_FOUND,
    )


def _serve_material(root: Path) -> Path:
    """Refuse before any listener unless ROOT holds a runner database and a valid catalog.

    Returns the runner database path.  Read-only: nothing under ROOT is created
    or changed (the trust registry and authority stores are only reopened).
    """

    from .local_authority import validate_local_method_authorities

    database = root / "runner" / "runner.sqlite3"
    if not database.is_file():
        raise ServeProblem(
            "TBX-SERVE-001",
            "No runner database under ROOT; nothing was started",
            cause="ROOT/runner/runner.sqlite3 does not exist (wrong --root, or no run yet)",
            fix="Run `traceback run BAM --reference ID --root ROOT` first, or pass the right --root",
            exit_code=ExitCode.NOT_FOUND,
        )
    catalog_database = root / "catalog" / "catalog.sqlite3"
    if not (root / "catalog").is_dir() or not (
        catalog_database.is_file() and not catalog_database.is_symlink()
    ):
        # ResultCatalog() would create an empty catalog here; a directory left
        # by a failed import, or a deleted database, is refused instead.
        raise _no_catalog()
    # A catalog without a valid authority is partial state: refuse, exit 3.
    validate_local_method_authorities(root)
    return database


def _serve_start_problem(exc: Exception) -> ServeProblem:
    """Map a listener start failure; its message is a fixed string with no path."""

    message = str(exc)
    busy = message == "local web startup lock is busy"
    return ServeProblem(
        "TBX-SERVE-003",
        "The local web service did not start; nothing was served",
        cause=(
            f"{message}: another `traceback serve` already serves this ROOT"
            if message == "local web service is already running"
            else f"{message}: another local web service is starting right now"
            if busy
            else message
        ),
        fix=(
            "Use the running service, or stop it (Ctrl-C in its terminal) and start again"
            if not busy
            else "Wait a few seconds and start again"
        ),
        retryable=busy,
    )


def _serve(
    args: argparse.Namespace,
    *,
    out: Any,
    stdin: Any,
    stop: Any,
    ready: Callable[[Any], None] = lambda service: None,
) -> int:
    """Run the loopback service for ROOT until ``stop`` is set (B6).

    The first line on ``out`` is the one-use operator launch link (bootstrap
    in the URL fragment only).  Stdin is read only when it is a TTY: Enter
    prints a fresh link and end of input stops.  Otherwise only ``stop`` (set
    by SIGINT/SIGTERM in ``main``) ends the service, so it can run in the
    background.  The service, store, catalog and trust registry are closed in
    that order on every exit, which releases the web anchor and lease.
    """

    import threading

    from .local_catalog import open_local_explorer
    from .store import JobStore
    from .web.auth import BootstrapBroker
    from .web.server import LocalWebServerError, LocalWebServerStopped, RunningLocalWebService

    root = args.root
    database = _serve_material(root)
    with open_local_explorer(root) as explorer:
        if explorer is None:  # removed between the check and the open
            raise _no_catalog()
        store = JobStore(database)
        try:
            try:
                service = RunningLocalWebService.start(
                    store=store,
                    state_directory=root / "web",
                    ipv6=args.ipv6,
                    explorer=explorer.source,
                )
            except LocalWebServerError as exc:
                raise _serve_start_problem(exc) from None
            with service:
                print(service.launch_url, file=out, flush=True)
                print(
                    "Open this one-use operator link in a browser on this machine "
                    "within 60 seconds; do not share it.",
                    file=out,
                )
                print(
                    f"Serving {explorer.views} cataloged record view(s)"
                    + (
                        f"; {explorer.skipped} invalid explorer file(s) skipped"
                        if explorer.skipped
                        else ""
                    )
                    + ". Unqualified, local, not for clinical use.",
                    file=out,
                )
                interactive = False
                try:
                    interactive = bool(stdin.isatty())
                except (AttributeError, OSError, ValueError):
                    interactive = False
                print(
                    "Press Enter for a fresh link; Ctrl-C stops the server."
                    if interactive
                    else "Ctrl-C or SIGTERM stops the server.",
                    file=out,
                    flush=True,
                )
                stopped_by_watchdog = threading.Event()

                def read_terminal() -> None:
                    while not stop.is_set():
                        try:
                            line = stdin.readline()
                        except (OSError, ValueError):
                            line = ""
                        if not line:
                            stop.set()
                            return
                        try:
                            code = service.issue_bootstrap()
                        except LocalWebServerStopped:
                            stopped_by_watchdog.set()
                            stop.set()
                            return
                        link = f"{service.base_url}/{BootstrapBroker.launch_fragment(code)}"
                        print(link, file=out, flush=True)

                if interactive:
                    threading.Thread(
                        target=read_terminal, name="traceback-serve-stdin", daemon=True
                    ).start()
                ready(service)
                while not stop.wait(0.2):
                    if not service.is_running:
                        stopped_by_watchdog.set()
                        break
                if stopped_by_watchdog.is_set():
                    raise ServeProblem(
                        "TBX-SERVE-004",
                        "The local web service stopped itself; no further links are issued",
                        cause="a security check of the running service failed "
                        "(its state directory or listener changed)",
                        fix="Restart `traceback serve --root ROOT`",
                    )
        finally:
            store.close()
    print("Stopped; the web lock for ROOT is released.", file=out, flush=True)
    return int(ExitCode.OK)


def _serve_main(args: argparse.Namespace) -> int:
    """Foreground ``traceback serve``: SIGINT and SIGTERM stop it cleanly."""

    import signal
    import threading

    stop = threading.Event()
    previous: dict[int, Any] = {}
    if threading.current_thread() is threading.main_thread():
        for number in (signal.SIGINT, signal.SIGTERM):
            previous[number] = signal.signal(number, lambda *_: stop.set())
    try:
        try:
            return _serve(args, out=sys.stdout, stdin=sys.stdin, stop=stop)
        except KeyboardInterrupt:
            return int(ExitCode.OK)
    except ReferenceProblem as problem:
        _emit(_local_envelope(_problem("serve", problem)), as_json=False)
        return int(problem.exit_code)
    except Exception:
        # For example a damaged catalog database.  No detail: it may name a path.
        _emit(
            _local_envelope(
                _result(
                    "serve",
                    "blocked",
                    "ROOT's catalog or runner database could not be opened; nothing was "
                    "started (check it with `traceback doctor --root ROOT`)",
                )
            ),
            as_json=False,
        )
        return int(ExitCode.BLOCKED)
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


def _preflight(args: argparse.Namespace) -> tuple[ExitCode, dict[str, Any]]:
    from .contracts import PreflightOutcome
    from .fixtures import SYNTHETIC_MODIFIED_BASE_MODEL, synthetic_registered_reference
    from .preflight import BamPreflightPolicy, validate_bam_snapshot

    index = args.index or Path(f"{args.input}.bai")
    if args.reference_id is not None:
        return _preflight_registered(args, index)
    _refuse_implicit_synthetic_reference(args)
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


def _refuse_implicit_synthetic_reference(args: argparse.Namespace) -> None:
    """D12: no silent synthetic default once ROOT has registered references.

    An unaligned BAM is told to align first (TBX-BAM-003 comes before
    TBX-REF-004), so the header is read before refusing.
    """

    from .preflight import header_is_unaligned, read_bam_header
    from .references import list_reference_ids

    try:
        identifiers = list_reference_ids(args.root)
    except OSError:
        return
    if not identifiers:
        return
    try:
        unaligned = header_is_unaligned(read_bam_header(args.input))
    except (OSError, ValueError):
        unaligned = False  # the registered preflight reports the unreadable BAM
    if unaligned:
        return
    raise RunProblem(
        "TBX-REF-004",
        "preflight needs --reference: this ROOT has registered references",
        cause="registered reference IDs: " + ", ".join(identifiers),
        fix=f"Add --reference {identifiers[0]} (or another registered ID)",
        exit_code=ExitCode.USAGE,
        data={"reference_ids": list(identifiers)},
    )


def _preflight_registered(
    args: argparse.Namespace, index: Path
) -> tuple[ExitCode, dict[str, Any]]:
    import shlex

    from .contracts import PreflightOutcome
    from .preflight import BamPreflightPolicy, alignment_command, validate_bam_snapshot
    from .references import load_reference

    loaded = load_reference(args.root, args.reference_id)
    report = validate_bam_snapshot(
        args.input,
        index,
        loaded.registered,
        BamPreflightPolicy(policy_id="local-unqualified-preflight-v1"),
        compare_assembly=loaded.source.assembly_declared,
    )
    # TBX-BAM-003's command names the registered FASTA in human output only;
    # --json and the report carry the reference ID, never a path.
    align = (
        {"align_command": alignment_command(shlex.quote(loaded.source.fasta_path))}
        if not getattr(args, "as_json", False)
        and any(check.code == "TBX-BAM-003" for check in report.checks)
        else {}
    )
    blocked = report.outcome == PreflightOutcome.BLOCKED
    return (
        ExitCode.BLOCKED if blocked else ExitCode.OK,
        _result(
            "preflight",
            "blocked" if blocked else "ok",
            (
                "Technical inspection blocked this input; "
                "unqualified, local, not for clinical use"
                if blocked
                else f"Technical inspection {report.outcome.value} against registered "
                f"reference {args.reference_id}; unqualified, local, not for clinical use"
            ),
            data={
                "report": report.model_dump(mode="json"),
                "qualified": False,
                "reference_id": args.reference_id,
                **align,
            },
        ),
    )


def _reference_register(args: argparse.Namespace) -> tuple[ExitCode, dict[str, Any]]:
    from .references import REGISTRATION_FILE, references_root, register_reference

    result = register_reference(
        args.root, args.fasta, args.reference_id, assembly=args.assembly
    )
    registered = result.registered
    return ExitCode.OK, _result(
        "reference register",
        "ok",
        (
            f"Reference {registered.reference_id} registered "
            f"({len(registered.contigs)} contigs); unqualified, local"
            if result.created
            else f"Reference {registered.reference_id} already registered with identical bytes"
        ),
        data={
            "reference_id": registered.reference_id,
            "created": result.created,
            "contigs": len(registered.contigs),
            "asset_sha256": registered.asset_sha256,
            "assembly_compared": args.assembly is not None,
            "registration": (
                references_root(args.root) / registered.reference_id / REGISTRATION_FILE
            ).relative_to(args.root).as_posix(),
            "qualified": False,
        },
    )


def _existing_runner(
    root: Path,
    *,
    synthetic_enabled: bool = False,
    local_unqualified_enabled: bool = False,
) -> Any:
    from .runner import Runner

    runner_root = root / "runner"
    if not (runner_root / "runner.sqlite3").is_file():
        raise FileNotFoundError("runner database not found")
    return Runner(
        runner_root,
        synthetic_enabled=synthetic_enabled,
        local_unqualified_enabled=local_unqualified_enabled,
    )


class TrustState(StrEnum):
    """What ``traceback status`` can truthfully say about a record's signature."""

    VERIFIED = "verified"
    NOT_VERIFIED = "not_verified"
    UNKNOWN = "unknown"


class TrustSource(StrEnum):
    """Which trust ``traceback status`` used (H2-status-min).

    ``trust_registry`` is live: a registry revocation reaches it.
    ``development_file`` is the fixed ``ROOT/trust`` document, which a registry
    revocation never reaches.  ``none``: no trust was available, so the state is
    ``unknown``.  ``no_record``: the job has no complete signed record.
    """

    TRUST_REGISTRY = "trust_registry"
    TRUST_REGISTRY_ERROR = "trust_registry_error"
    DEVELOPMENT_FILE = "development_file"
    DEVELOPMENT_FILE_ERROR = "development_file_error"
    NONE = "none"
    NO_RECORD = "no_record"


_STATUS_TRUST_HINTS = {
    TrustSource.TRUST_REGISTRY: "Checked against the current result-trust registry",
    TrustSource.TRUST_REGISTRY_ERROR: (
        "The result-trust registry did not open at the retained ID, epoch and "
        "head; pass its current head, or run traceback doctor"
    ),
    TrustSource.DEVELOPMENT_FILE: (
        "Checked against the fixed development trust file, which registry "
        "revocations do not reach; pass --trust-registry for current trust"
    ),
    TrustSource.DEVELOPMENT_FILE_ERROR: (
        "The development trust file under ROOT could not be read; run traceback doctor"
    ),
    TrustSource.NONE: (
        "No trust is available under ROOT; pass --trust-registry with its retained "
        "ID, epoch and head"
    ),
    TrustSource.NO_RECORD: "The job has no complete signed record to check",
}


def _registry_record_trust(args: argparse.Namespace, bundle: Path) -> TrustState:
    """Check ``bundle`` under the registry's read fence.

    Raises when the registry cannot be opened or read; a bundle that fails
    verification against the current trust is ``not_verified``.
    """

    from evidence_inspector.result_trust_registry import ResultTrustRegistry

    from .bundles import verify_bundle
    from .signing import development_trust_document_bytes, load_development_trust

    root = args.trust_registry
    if root.is_symlink() or not root.is_dir():
        # Never let a read-only command create a registry root.
        raise FileNotFoundError("result trust registry is absent")
    with ResultTrustRegistry(
        root,
        expected_registry_id=args.trust_registry_id,
        expected_registry_epoch_sha256=args.trust_registry_epoch,
        expected_state_head_sha256=args.trust_registry_head,
    ) as registry:
        # Hold the read fence through verification so the answer is
        # consistent with the head the registry was opened at.
        with registry.read_fence() as snapshot:
            trust = load_development_trust(
                development_trust_document_bytes(snapshot.document)
            )
            try:
                verify_bundle(bundle, trust)
            except Exception:
                state = TrustState.NOT_VERIFIED
            else:
                state = TrustState.VERIFIED
    return state


def _record_trust(
    args: argparse.Namespace, runner: Any, job_id: str
) -> tuple[TrustState, TrustSource]:
    """Report a complete record's signature state and the trust it used.

    Never raises: ``status`` reports state and does not gate it (taste T6).
    """

    from .bundles import verify_bundle
    from .signing import load_development_trust

    try:
        bundle = _signed_bundle_from_outputs(runner, job_id)
    except Exception:
        return TrustState.NOT_VERIFIED, TrustSource.NO_RECORD
    if args.trust_registry is not None:
        try:
            return _registry_record_trust(args, bundle), TrustSource.TRUST_REGISTRY
        except Exception:
            return TrustState.NOT_VERIFIED, TrustSource.TRUST_REGISTRY_ERROR
    trust_path = args.root / _TRUST_RELATIVE
    if not (trust_path.exists() or trust_path.is_symlink()):
        return TrustState.UNKNOWN, TrustSource.NONE
    try:
        trust = load_development_trust(trust_path.read_bytes())
    except Exception:
        return TrustState.NOT_VERIFIED, TrustSource.DEVELOPMENT_FILE_ERROR
    try:
        verify_bundle(bundle, trust)
    except Exception:
        return TrustState.NOT_VERIFIED, TrustSource.DEVELOPMENT_FILE
    return TrustState.VERIFIED, TrustSource.DEVELOPMENT_FILE


def _status(args: argparse.Namespace) -> tuple[ExitCode, dict[str, Any]]:
    runner = _existing_runner(args.root)
    record = runner.status(args.job_id)
    if record.state == JobState.COMPLETE:
        trust_state, trust_source = _record_trust(args, runner, record.job_id)
    else:
        trust_state, trust_source = TrustState.NOT_VERIFIED, TrustSource.NO_RECORD
    view = build_job_view(
        job_id=record.job_id,
        state=record.state,
        observed_at=datetime.now(UTC),
        signature_verified=trust_state == TrustState.VERIFIED,
        local_unqualified=_is_local_request(runner.store.request(record.job_id)),
    )
    return ExitCode.OK, _result(
        "status",
        "ok",
        view.headline,
        data={
            "operator_state": view.model_dump(mode="json"),
            "trust_state": trust_state.value,
            "trust_source": trust_source.value,
            "trust_hint": _STATUS_TRUST_HINTS[trust_source],
        },
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


def _sealed_local_names(runner: Any, job_id: str) -> tuple[str, str] | None:
    """Recover a local job's sealed BAM and index names from its snapshot."""

    names = [str(item["relative_path"]) for item in runner.snapshot(job_id)["files"]]
    indexes = [name for name in names if Path(name).suffix in _INDEX_SUFFIXES]
    bams = [name for name in names if Path(name).suffix not in _INDEX_SUFFIXES]
    if len(names) != 2 or len(indexes) != 1 or len(bams) != 1:
        return None
    return bams[0], indexes[0]


def _resume(
    args: argparse.Namespace, progress: Callable[[str], None] = lambda line: None
) -> tuple[ExitCode, dict[str, Any]]:
    from .references import load_reference
    from .signing import (
        TrustNamespace,
        development_trust_bytes,
    )

    probe = _existing_runner(args.root)
    record = probe.status(args.job_id)
    request = probe.store.request(record.job_id)
    local = _is_local_request(request)
    if not local and (
        request.workflow_release_sha256 != hashlib.sha256(_WORKFLOW_ID.encode("ascii")).hexdigest()
        or request.sample_token != "synthetic-sample-token"
    ):
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
    _reject_live_worker(probe, record.job_id)
    if local:
        runner = _existing_runner(args.root, local_unqualified_enabled=True)
        _refuse_unsealed_job(runner, record)
        reference_id = request.sample_token[len(_LOCAL_SAMPLE_PREFIX):]
        loaded = load_reference(args.root, reference_id)
        # The same authority check as `run`: a damaged store refuses the resume.
        from .local_authority import ensure_local_method_authority

        ensure_local_method_authority(args.root, loaded.registered)
        names = _sealed_local_names(runner, record.job_id)
        if names is None:
            return ExitCode.BLOCKED, _result(
                "resume", "blocked", "The local job's sealed input is not one BAM and one index",
            )
        namespace = TrustNamespace.DEVELOPMENT_LOCAL

        def make_stages(key: Any) -> tuple[Any, ...]:
            return _local_stages(args.root, loaded, names[0], names[1], key, progress)
    else:
        runner = _existing_runner(args.root, synthetic_enabled=True)
        namespace = TrustNamespace.DEVELOPMENT_SYNTHETIC
        make_stages = _demo_stages
    signing_key = _signing_key_for(args.root, namespace)
    _append_development_trust(args.root / _TRUST_RELATIVE, development_trust_bytes(signing_key))
    stages = make_stages(signing_key)
    worker_id = "local-cli" if local else "synthetic-cli"
    return _resume_execute(args, runner, record, stages, worker_id, local=local)


def _resume_execute(
    args: argparse.Namespace,
    runner: Any,
    record: Any,
    stages: tuple[Any, ...],
    worker_id: str,
    *,
    local: bool,
) -> tuple[ExitCode, dict[str, Any]]:
    try:
        if record.state == JobState.QUEUED:
            record = runner.execute(record.job_id, stages, worker_id=worker_id)
        else:
            record = runner.resume(record.job_id, stages, worker_id=worker_id)
    except LocalStageRefusal as refusal:
        raise _refusal_problem(refusal) from refusal
    if local and record.state == JobState.PAUSED:
        return ExitCode.OK, _result(
            "resume",
            "ok",
            "Run paused at a stage boundary; no record yet",
            data={"job_id": record.job_id, "state": record.state.value},
        )
    verified, published = _publish_signed_record(args.root, runner, record.job_id)
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


def _verify_with_trust_registry(
    args: argparse.Namespace,
) -> tuple[ExitCode, dict[str, Any]]:
    """Verify against the current trust of a protected result-trust registry.

    Opening requires the independently retained registry ID, epoch, and head,
    and the registry rejects any older head, so a revoked key cannot be
    revived by an old trust file or an older copy of the registry.
    """

    from evidence_inspector.result_trust_registry import ResultTrustRegistry

    from .bundles import verify_bundle
    from .signing import development_trust_document_bytes, load_development_trust

    root = args.trust_registry
    if root.is_symlink() or not root.is_dir():
        # Never let a read-only command create a registry root.
        raise FileNotFoundError("result trust registry is absent")
    with ResultTrustRegistry(
        root,
        expected_registry_id=args.trust_registry_id,
        expected_registry_epoch_sha256=args.trust_registry_epoch,
        expected_state_head_sha256=args.trust_registry_head,
    ) as registry:
        # Hold the read fence through verification so the result is consistent
        # with the reported head.
        with registry.read_fence() as snapshot:
            trust = load_development_trust(
                development_trust_document_bytes(snapshot.document)
            )
            verified = verify_bundle(args.bundle, trust)
            data = {
                "verified": True,
                "record_id": verified.manifest.record_id,
                "development_trust_only": True,
                "trust_registry_id": snapshot.registry_id,
                "trust_registry_state_version": snapshot.state_version,
                "trust_registry_state_head_sha256": snapshot.state_head_sha256,
            }
    return ExitCode.OK, _result(
        "verify",
        "ok",
        "Signed local record verified with development trust; nothing was uploaded",
        data=data,
    )


def _record_under_root(root: Path, record: str) -> Path:
    """Resolve a record ID (or a record directory name) under ROOT/records.

    Local records live at ``<record_id>``; synthetic ones at
    ``<record_id>-<signing key ID>``.  Otherwise a record ID resolves only when
    exactly one published directory (recovered copies included) carries it.
    """

    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", record):
        raise FileNotFoundError("record ID is not a plain record name")
    records = root / "records"
    exact = records / record
    if exact.is_dir() and not exact.is_symlink():
        return exact
    candidates = sorted(
        path
        for path in records.glob(f"{record}-*")
        if path.is_dir() and not path.is_symlink()
    )
    if len(candidates) != 1:
        raise FileNotFoundError("record ID does not name exactly one published record")
    return candidates[0]


def _manifest_is_local(bundle: Path) -> bool:
    """Peek (unverified) at a bundle's manifest version, only to label output."""

    try:
        manifest = json.loads((bundle / "bundle-manifest.json").read_bytes())
    except Exception:
        return False
    return isinstance(manifest, dict) and (
        manifest.get("schema_version") == "traceback.result-bundle.v3"
    )


def _verify(args: argparse.Namespace) -> tuple[ExitCode, dict[str, Any]]:
    from .bundles import verify_bundle
    from .signing import load_development_trust

    if args.trust_registry is not None:
        return _verify_with_trust_registry(args)
    if args.verify_root is not None:
        bundle = _record_under_root(args.verify_root, str(args.bundle))
        trust_path = args.verify_root / _TRUST_RELATIVE
    else:
        bundle = args.bundle
        trust_path = args.trust_store
        if trust_path.is_dir():
            trust_path = trust_path / _TRUST_RELATIVE.name
    trust = load_development_trust(trust_path.read_bytes())
    verified = verify_bundle(bundle, trust)
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


def _dispatch(
    args: argparse.Namespace, progress: Callable[[str], None] = lambda line: None
) -> tuple[ExitCode, dict[str, Any]]:
    if args.command == "doctor":
        return _doctor(args)
    if args.command == "protocol":
        return _protocol_show()
    if args.command == "demo":
        return _demo(args)
    if args.command == "preflight":
        return _preflight(args)
    if args.command == "reference":
        return _reference_register(args)
    if args.command == "run":
        return _run(args, progress)
    if args.command == "status":
        return _status(args)
    if args.command == "logs":
        return _logs(args)
    if args.command == "pause":
        return _pause(args)
    if args.command == "resume":
        return _resume(args, progress)
    if args.command == "retry":
        return _retry(args)
    if args.command == "catalog":
        return _catalog_import(args)
    if args.command == "inspect":
        return _inspect(args)
    if args.command == "verify":
        return _verify(args)
    if args.command == "assets":
        return _assets(args)
    if args.command == "support-bundle":
        return _support_bundle(args)
    raise AssertionError(f"unhandled command {args.command}")


def _require_trust_registry_identity(
    parser: argparse.ArgumentParser, args: argparse.Namespace
) -> None:
    if args.command not in {"verify", "status"}:
        return
    identity = (
        args.trust_registry_id,
        args.trust_registry_epoch,
        args.trust_registry_head,
    )
    if args.trust_registry is not None and any(item is None for item in identity):
        parser.error(
            "--trust-registry requires --trust-registry-id, "
            "--trust-registry-epoch, and --trust-registry-head"
        )
    if args.trust_registry is None and any(item is not None for item in identity):
        parser.error("--trust-registry-id/epoch/head require --trust-registry")


_JOB_COMMANDS = frozenset({"status", "logs", "pause", "resume", "retry", "support-bundle"})


def _concerns_local_data(args: argparse.Namespace) -> bool:
    """Whether this command's result is about a real local input.

    Such results use ``traceback.cli-result.v2`` with ``data_origin``; every
    other command keeps its v1 bytes.  This only labels output.
    """

    command = args.command
    if command in {"run", "reference", "catalog"}:
        return True
    if command == "preflight":
        return args.reference_id is not None
    if command == "verify":
        if args.trust_registry is not None:
            return _manifest_is_local(args.bundle)
        try:
            bundle = (
                _record_under_root(args.verify_root, str(args.bundle))
                if args.verify_root is not None
                else args.bundle
            )
        except Exception:
            return False
        return _manifest_is_local(bundle)
    if command in _JOB_COMMANDS:
        try:
            from .store import JobStore

            database = args.root / "runner" / "runner.sqlite3"
            if not database.is_file():
                return False
            return _is_local_request(JobStore(database).request(args.job_id))
        except Exception:
            return False
    return False


def main(argv: Sequence[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    if raw[:1] == ["reader"]:  # local operator reader authority (E12)
        from .reader_cli import main as reader_main

        return reader_main(raw[1:])
    parser = _parser()
    args = parser.parse_args(argv)
    _require_trust_registry_identity(parser, args)
    if args.command == "serve":  # long-running; its first stdout line is the link
        return _serve_main(args)
    as_json = getattr(args, "as_json", False)

    def progress(line: str) -> None:
        if not as_json:
            print(line, flush=True)

    try:
        mutation = (
            _operator_lock(args.root)
            if args.command in {"demo", "resume", "retry"}
            # The TBX-RUN-003 refusal (no --reference) touches nothing on disk.
            or (args.command == "run" and args.reference_id is not None)
            or (args.command == "assets" and args.asset_command == "install")
            or (args.command == "reference" and args.reference_command == "register")
            or (args.command == "catalog" and args.catalog_command == "import")
            else nullcontext()
        )
        # Local work maps a full ROOT volume (OS or SQLite) to TBX-RUN-004.
        no_space = (
            _no_space_mapped()
            if args.command in {"run", "resume"} and _concerns_local_data(args)
            else nullcontext()
        )
        # A wall-clock worker lease cannot survive the host sleeping through
        # it, so commands that execute stages keep the host awake.
        awake = (
            stay_awake()
            if args.command in {"resume", "retry"}
            or (args.command == "run" and args.reference_id is not None)
            else nullcontext()
        )
        with mutation, no_space, awake:
            code, payload = _dispatch(args, progress)
    except ReferenceProblem as problem:
        command = (
            f"{args.command} {args.reference_command}"
            if args.command == "reference"
            else f"{args.command} {args.catalog_command}"
            if args.command == "catalog"
            else args.command
        )
        code, payload = ExitCode(problem.exit_code), _problem(command, problem)
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
            "ResultTrustRegistryError",
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
        elif args.command == "run":
            code, payload = ExitCode.RETRYABLE_FAILURE, _result(
                args.command,
                "retryable_failure",
                "Local run failed without a record; inspect traceback status and "
                "logs before retrying",
                data={"code": "TBX-JOB-001", "retryable": True},
            )
        elif args.command == "preflight":
            # BAM read/format errors are TBX-BAM-001 checks; anything reaching
            # here is a defect, not a property of the input.
            code, payload = ExitCode.INTERNAL_ERROR, _result(
                args.command,
                "internal_error",
                "Preflight stopped on an unexpected internal error; nothing was changed",
                data={"code": "TBX-INTERNAL-001", "retryable": False},
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
    if _concerns_local_data(args):
        payload = _local_envelope(payload)
    _emit(payload, as_json=as_json)
    return int(code)


__all__ = ["ExitCode", "main"]
