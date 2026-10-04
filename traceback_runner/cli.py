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


def _label_argument(value: str) -> str:
    from .labels import LabelError, validate_label

    try:
        return validate_label(value)
    except LabelError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _limit_argument(value: str) -> int:
    try:
        limit = int(value)
    except ValueError:
        limit = 0
    if not 1 <= limit <= 1000:
        raise argparse.ArgumentTypeError("--limit must be an integer from 1 to 1000")
    return limit


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
    run.add_argument(
        "--import",
        dest="do_import",
        action="store_true",
        help="catalog the record after it is published (same as catalog import)",
    )
    run.add_argument(
        "--label",
        type=_label_argument,
        help="operator note for the record (1-80 characters; not signed; never in --json; "
        "no donor names or identifiers)",
    )
    _root_argument(run)
    run.add_argument("--json", action="store_true", dest="as_json")

    jobs = commands.add_parser("jobs", help="list local jobs, newest first")
    jobs.add_argument("--limit", type=_limit_argument, default=20, help="rows (default 20)")
    _root_argument(jobs)
    jobs.add_argument("--json", action="store_true", dest="as_json")

    label = commands.add_parser(
        "label", help="set or replace a record's operator note (not part of the signed record)"
    )
    label.add_argument("record_id", help="record ID (or a unique prefix of it)")
    label.add_argument("text", type=_label_argument)
    _root_argument(label)
    label.add_argument("--json", action="store_true", dest="as_json")

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
    catalog_import.add_argument(
        "bundle",
        type=Path,
        help="record ID (or a unique prefix), or a record directory (ROOT/records/ID)",
    )
    _root_argument(catalog_import)
    catalog_import.add_argument("--json", action="store_true", dest="as_json")
    catalog_list = catalog_commands.add_parser(
        "list", help="list the records under ROOT and whether each is imported"
    )
    _root_argument(catalog_list)
    catalog_list.add_argument("--json", action="store_true", dest="as_json")
    catalog_export = catalog_commands.add_parser(
        "export", help="write one CSV row per (imported record, histogram bin)"
    )
    catalog_export.add_argument("--csv", required=True, type=Path, dest="csv_path")
    _root_argument(catalog_export)
    catalog_export.add_argument("--json", action="store_true", dest="as_json")

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

    def __str__(self) -> str:
        # A stage that raises a RunProblem stores this as the job's
        # ``last_error``; the code lets status and logs explain it later.
        return f"{self.code}: {self.summary}"


def _attach_job_id(exc: BaseException, job_id: str) -> None:
    """Name the job a failed command created, so status and logs can find it."""

    try:
        exc.job_id = job_id  # type: ignore[attr-defined]
    except AttributeError:
        pass


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
            **({"job_id": job_id} if (job_id := getattr(problem, "job_id", None)) else {}),
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


_HUMAN = "_human"


def _emit(payload: dict[str, Any], *, as_json: bool) -> None:
    """Print one result.  ``_human`` lines (tables, operator labels) are for
    human output only and never reach ``--json``."""

    human = payload.get(_HUMAN)
    if as_json:
        public = {key: value for key, value in payload.items() if key != _HUMAN}
        print(canonical_json_bytes(public).decode("utf-8"))
        return
    label = "PASS" if payload["status"] == "ok" else payload["status"].upper()
    print(f"{label}  {payload['summary']}")
    if human is not None and human.get("replace_data"):
        for line in human["lines"]:
            print(line)
        return
    for key, value in payload.get("data", {}).items():
        rendered = (
            json.dumps(value, sort_keys=True, separators=(",", ":"))
            if isinstance(value, (dict, list))
            else str(value)
        )
        print(f"{key.upper()}  {rendered}")
    for line in (human or {}).get("lines", ()):
        print(line)


def _with_human(
    payload: dict[str, Any], lines: Sequence[str], *, replace_data: bool = False
) -> dict[str, Any]:
    """Attach human-only lines (never printed by ``--json``)."""

    return {**payload, _HUMAN: {"lines": list(lines), "replace_data": replace_data}}


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
        raise RunProblem(
            "TBX-JOB-002",
            "Another traceback process holds this job; nothing was changed",
            cause=f"job {job_id} has an unexpired worker lease (a running or "
            "just-stopped traceback process)",
            fix=f"Wait for it, or check traceback status {job_id}",
            retryable=True,
            data={"job_id": job_id},
        )


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


def _local_workflow_sha256(method_definition_sha256: str) -> str:
    """The local job key's workflow hash: it names the exact method definition.

    The runner deduplicates on the canonical request, so a run under a changed
    method (another policy, reference bytes or tool) is a new job and never
    returns an earlier method's record.
    """

    if not re.fullmatch(r"[0-9a-f]{64}", method_definition_sha256):
        raise ValueError("method definition digest must be 64 lowercase hex characters")
    return hashlib.sha256(
        f"{_LOCAL_WORKFLOW_ID}:{method_definition_sha256}".encode("ascii")
    ).hexdigest()


def _synthetic_workflow_sha256() -> str:
    return hashlib.sha256(_WORKFLOW_ID.encode("ascii")).hexdigest()


def _is_local_request(request: Any) -> bool:
    """A local job: ``local-`` sample token and not the synthetic workflow.

    Deliberately independent of the current method: rows written under the
    legacy constant key, under today's method and under any later method all
    stay recognisable without loading a reference.
    """

    return request.sample_token.startswith(_LOCAL_SAMPLE_PREFIX) and (
        request.workflow_release_sha256 != _synthetic_workflow_sha256()
    )


def _local_method_sha256(registered: Any) -> str:
    """SHA-256 of the local method definition ``run`` measures this reference with."""

    from evidence_inspector.method_registry import method_definition_sha256

    from . import local_authority

    return method_definition_sha256(local_authority.local_method_definition(registered))


def _publish_private_key_bytes(path: Path, *, replace: bool) -> None:
    """Write 32 random bytes to ``path`` so a crash never leaves a short key.

    The bytes go to a fsynced private temporary first.  A new key is published
    with a no-replace ``link`` (a concurrent creator wins and its key is kept);
    ``replace=True`` atomically swaps out a short key left by an older crash.
    """

    nofollow = getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.unlink(missing_ok=True)
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | nofollow, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(os.urandom(32))
            stream.flush()
            os.fsync(stream.fileno())
        if replace:
            os.replace(temporary, path)
        else:
            try:
                os.link(temporary, path)
            except FileExistsError:
                pass
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _read_private_key(path: Path) -> tuple[bool, bytes]:
    """Read at most 33 bytes of a key; ``private`` means a regular file of
    yours with no group or other access (0600 or 0400)."""

    nofollow = getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(path, os.O_RDONLY | nofollow)
    except OSError:
        return False, b""
    with os.fdopen(descriptor, "rb") as stream:
        metadata = os.fstat(stream.fileno())
        private = (
            stat.S_ISREG(metadata.st_mode)
            and metadata.st_uid == os.geteuid()
            and not stat.S_IMODE(metadata.st_mode) & 0o077
        )
        return private, stream.read(33)


def _root_has_records(root: Path) -> bool:
    records = root / "records"
    if records.is_symlink():
        return True
    if not records.exists():
        return False
    if not records.is_dir():
        return True
    return any(not entry.name.startswith(".") for entry in records.iterdir())


def _provenance_hmac_key(root: Path) -> bytes:
    """Return ROOT's 32-byte provenance HMAC key, creating it once (private).

    A per-root random key means the same BAM run under two roots yields
    unlinkable ``provider_hmac_sha256`` commitments.  Created like the local
    signing key (fsynced temporary, no-replace link), so a crash never
    leaves a short key.  A short key left by an older version's crash is
    replaced only while ROOT/records holds no record: no published record
    carries a commitment under it yet.  Otherwise ``run`` refuses (TBX-RUN-006).
    """

    path = root / _PROVENANCE_KEY_RELATIVE
    path.parent.mkdir(parents=True, exist_ok=True)
    if not (path.exists() or path.is_symlink()):
        _publish_private_key_bytes(path, replace=False)
    private, key = _read_private_key(path)
    if private and len(key) < 32 and not _root_has_records(root):
        _publish_private_key_bytes(path, replace=True)
        private, key = _read_private_key(path)
    if not private or len(key) != 32:
        raise RunProblem(
            "TBX-RUN-006",
            "The provenance key ROOT/trust/provenance-hmac.key is not a private 32-byte file",
            cause=(
                "ROOT/trust/provenance-hmac.key was edited, truncated, replaced, "
                "or is readable by other users (it must be yours, 0600 or 0400)"
                + (
                    "; ROOT/records already holds records, so it is not replaced"
                    if private and len(key) < 32
                    else ""
                )
            ),
            fix=(
                "Use a fresh --root; never edit ROOT/trust/provenance-hmac.key "
                "or other files under ROOT/trust"
            ),
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
        _publish_private_key_bytes(path, replace=False)
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


_BGZF_MAGIC = b"\x1f\x8b\x08\x04"


def _regular_input(path: Path) -> bool:
    return not path.is_symlink() and path.is_file()


def _check_bam_input(bam: Path) -> None:
    """A2: the BAM exists as a regular file (TBX-RUN-008) and starts with the
    BGZF bytes (TBX-RUN-010).  Runs before any reference, job or copy work."""

    bam_abs = Path(os.path.abspath(bam))
    if not _regular_input(bam_abs):
        raise RunProblem(
            "TBX-RUN-008",
            "The BAM is missing or is not a regular file; no job was created",
            cause="the BAM path does not name a regular file (missing, a directory, "
            "or a symbolic link)",
            fix="Check the BAM path; pass the file itself, not a link or a directory",
            exit_code=ExitCode.NOT_FOUND,
        )
    try:
        with bam_abs.open("rb") as stream:
            magic = stream.read(len(_BGZF_MAGIC))
    except OSError:
        magic = b""
    if magic != _BGZF_MAGIC:
        raise RunProblem(
            "TBX-RUN-010",
            "The input is not a BAM (no BGZF header); no job was created",
            cause="the file's first four bytes are not the BGZF bytes every BAM starts with",
            fix="This is not a BAM; for FASTQ or POD5 see Aligning MinKNOW output in "
            "docs/OPERATOR-GUIDE.md",
        )


def _local_input_files(bam: Path, index: Path) -> tuple[Path, tuple[str, str]]:
    """Check the index; return the snapshot source and the BAM and index names.

    Runs after :func:`_check_bam_input` and the unaligned-BAM check (an
    unaligned MinKNOW BAM is told to align before it is told to index), and
    before any authority, job or copy work.  Paths are never echoed.
    """

    bam_abs = Path(os.path.abspath(bam))
    index_abs = Path(os.path.abspath(index))
    if not _regular_input(index_abs):
        raise RunProblem(
            "TBX-RUN-009",
            "The BAM index is missing; no job was created",
            cause="no regular index file at the --index path (default: BAM.bai beside the BAM)",
            fix="samtools index BAM, or pass --index",
            exit_code=ExitCode.NOT_FOUND,
        )
    if index_abs.suffix not in _INDEX_SUFFIXES or bam_abs.suffix in _INDEX_SUFFIXES:
        raise RunProblem(
            "TBX-BAM-001",
            "The BAM index must be a .bai or .csi file beside a BAM",
            cause="the --index file name does not end in .bai or .csi",
            fix="Index the BAM with samtools index and pass that file with --index",
        )
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
        if (job_id := getattr(exc, "job_id", None)) is not None:
            _attach_job_id(problem, job_id)
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


_LOCAL_RECORD_NAME = re.compile(r"record-[0-9a-f]{24}")
_LOCAL_RUN_TOKEN = re.compile(r"local-run-([0-9a-f]{16})")


def _peek_measurement_sha256(bundle: Path) -> str | None:
    """Unverified peek at a record's measurement digest; only a pre-filter."""

    from .bundles import MANIFEST_PATH, MEASUREMENT_PATH
    from .local_catalog import _read_peek

    try:
        manifest = json.loads(_read_peek(bundle / MANIFEST_PATH))
        contents = manifest["contents"]
        return next(
            str(item["sha256"]) for item in contents if item["relative_path"] == MEASUREMENT_PATH
        )
    except Exception:
        return None


def _measurement_twins(root: Path, store: Any | None) -> dict[str, str]:
    """Map each local record to the earliest record with the same measurement.

    A re-run under a different job (for example after the job key gained the
    method, B1) signs a new record whose measurement bytes equal an earlier
    record's.  Records are grouped by the signed measurement digest of their
    verified manifest; within a group the record whose job was created first
    is the original, and every other record maps to it ("same measurement as").
    Records that do not verify, or are not local, are left out.  Without a job
    store (or a job it cannot find) a record sorts last, then by record ID.
    """

    from .bundles import MEASUREMENT_PATH, verify_bundle
    from .signing import load_development_trust

    records = root / "records"
    if not records.is_dir():
        return {}
    candidates: dict[str, list[Path]] = {}
    for path in sorted(records.iterdir()):
        if not _LOCAL_RECORD_NAME.fullmatch(path.name) or path.is_symlink():
            continue
        digest = _peek_measurement_sha256(path)
        if digest is not None:
            candidates.setdefault(digest, []).append(path)
    groups = [paths for paths in candidates.values() if len(paths) > 1]
    if not groups:
        return {}
    trust = load_development_trust((root / _TRUST_RELATIVE).read_bytes())
    twins: dict[str, str] = {}
    for paths in groups:
        members: list[tuple[str, float, str]] = []
        for path in paths:
            try:
                verified = verify_bundle(path, trust)
            except Exception:
                continue
            manifest = verified.manifest
            if manifest.schema_version != "traceback.result-bundle.v3" or (
                manifest.record_id != path.name
            ):
                continue
            digest = next(
                item.sha256 for item in manifest.contents if item.relative_path == MEASUREMENT_PATH
            )
            token = _LOCAL_RUN_TOKEN.fullmatch(verified.provenance.run_token)
            when = (
                store.created_at_by_prefix(token.group(1))
                if token is not None and store is not None
                else None
            )
            members.append((digest, float("inf") if when is None else when, path.name))
        by_digest: dict[str, list[tuple[float, str]]] = {}
        for digest, when, name in members:
            by_digest.setdefault(digest, []).append((when, name))
        for ordered in by_digest.values():
            ordered.sort()
            original = ordered[0][1]
            for _, name in ordered[1:]:
                twins[name] = original
    return twins


def _same_measurement_as(root: Path, store: Any, record_id: str) -> str | None:
    """The earlier record whose measurement equals ``record_id``'s, if any.

    A display marker only: an unreadable sibling never fails the run.
    """

    try:
        return _measurement_twins(root, store).get(record_id)
    except Exception:
        return None


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
    twin = _same_measurement_as(root, runner.store, verified.manifest.record_id)
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
            **({"same_measurement_as": twin} if twin is not None else {}),
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
    # Inputs first (A2): a bad input creates no job, authority store or copy.
    _check_bam_input(args.input)
    loaded = load_reference(root, args.reference_id)
    _refuse_unaligned_or_empty(
        args.input,
        # Human output names the registered FASTA; --json never does.
        fasta=None if getattr(args, "as_json", False) else loaded.source.fasta_path,
    )
    source, relative_files = _local_input_files(
        args.input, args.index or Path(f"{args.input}.bai")
    )
    # Create (once) or validate the local method authority before any copy:
    # a damaged ROOT/authority refuses the run with TBX-AUTH-LOCAL-001.
    ensure_local_method_authority(root, loaded.registered)
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
        workflow_release_sha256=_local_workflow_sha256(
            _local_method_sha256(loaded.registered)
        ),
    )
    code, payload = _run_sealed(root, request, source, relative_files, loaded, progress)
    return _after_run(args, code, payload)


def _after_run(
    args: argparse.Namespace, code: ExitCode, payload: dict[str, Any]
) -> tuple[ExitCode, dict[str, Any]]:
    """``run --label`` and ``run --import`` once the record is published (A4)."""

    from .labels import LABEL_QUALIFIER, write_label

    data = payload["data"]
    record_id = data.get("record_id")
    if code != ExitCode.OK or record_id is None:
        return code, payload
    root = args.root
    lines: list[str] = []
    if args.label is not None:
        previous = write_label(root, record_id, args.label)
        data["label_set"] = True
        lines.append(f"LABEL  {args.label} ({LABEL_QUALIFIER})")
        if previous is not None and previous != args.label:
            lines.append(f"Label changed from {previous} to {args.label}")
    if args.do_import:
        try:
            outcome = _import_record(root, root / "records" / record_id)
        except ReferenceProblem as problem:
            # The record exists and verified; only the catalog step failed.
            problem.data = {  # type: ignore[attr-defined]
                **getattr(problem, "data", {}),
                "record_id": record_id,
                "next_action": f"traceback catalog import {record_id} --root <same-root>",
            }
            _attach_job_id(problem, data["job_id"])
            raise
        data["imported"] = True
        data["result_id"] = outcome.reference.result_id
        data["next_commands"] = [
            command for command in data.get("next_commands", ())
            if not command.startswith("traceback catalog import ")
        ]
        lines.append("View it: traceback serve --root <same-root> (a running serve shows it "
                     "on its next catalog request)")
    return code, _with_human(payload, lines) if lines else payload


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
    refuse_failed = _refuse_failed_job(runner)
    submitted: list[str] = []

    def on_submitted(record: Any) -> None:
        submitted.append(record.job_id)
        refuse_failed(record)

    try:
        return _run_submitted(
            root, runner, request, source, relative_files, loaded, progress, submitted,
            on_submitted,
        )
    except BaseException as exc:
        # Every refusal after submit names its job (A3), whatever maps it.
        if submitted:
            _attach_job_id(exc, submitted[0])
        raise


def _run_submitted(
    root: Path,
    runner: Any,
    request: Any,
    source: Path,
    relative_files: tuple[str, str],
    loaded: Any,
    progress: Callable[[str], None],
    submitted: list[str],
    on_submitted: Callable[[Any], None],
) -> tuple[ExitCode, dict[str, Any]]:
    from .signing import TrustNamespace

    bam_name, index_name = relative_files
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


_RECORD_PREFIX = re.compile(r"(?:record-)?([0-9a-f]{8,24})")


def _short_record(record_id: str) -> str:
    """The 12-character table form of a record ID (its first 12 hex digits)."""

    return record_id.removeprefix("record-")[:12]


def _local_record_names(root: Path) -> list[str]:
    records = root / "records"
    if not records.is_dir() or records.is_symlink():
        return []
    return sorted(
        path.name
        for path in records.iterdir()
        if _LOCAL_RECORD_NAME.fullmatch(path.name) and not path.is_symlink() and path.is_dir()
    )


def _resolve_record_id(root: Path, text: str) -> str | None:
    """A full local record ID, or a unique prefix of at least 8 hex digits."""

    match = _RECORD_PREFIX.fullmatch(text)
    if match is None:
        return None
    prefix = f"record-{match.group(1)}"
    names = [name for name in _local_record_names(root) if name.startswith(prefix)]
    return names[0] if len(names) == 1 else None


def _record_argument_path(root: Path, value: Path) -> Path:
    """``catalog import`` takes a record ID (or unique prefix) or a record path."""

    text = str(value)
    if "/" not in text and not value.exists():
        record_id = _resolve_record_id(root, text)
        if record_id is not None:
            return root / "records" / record_id
    return value


def _import_record(root: Path, bundle: Path) -> Any:
    from .local_catalog import import_local_record

    return import_local_record(root, bundle)


def _catalog_import(args: argparse.Namespace) -> tuple[ExitCode, dict[str, Any]]:
    """Catalog one local record and persist its explorer view (B5a/B5b)."""

    root = args.root
    outcome = _import_record(root, _record_argument_path(root, args.bundle))
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


# --- A4: list jobs and records, labels, CSV export ----------------------------


def _token_reference_policy(sample_token: str) -> tuple[str, str]:
    """(reference, policy) named by a job's sample token.

    ``local-<reference_id>`` is the built-in policy; ``local-<ref>:<policy>``
    (research policies, a later item) names its policy.  Demo jobs are synthetic.
    """

    if not sample_token.startswith(_LOCAL_SAMPLE_PREFIX):
        return "synthetic", "synthetic"
    reference, _, policy = sample_token[len(_LOCAL_SAMPLE_PREFIX):].partition(":")
    return reference, policy or "built-in"


def _record_job_prefixes(root: Path) -> dict[str, str]:
    """Map a job ID's first 16 hex digits to its published local record ID.

    An unverified peek at each record's provenance run token, used only to show
    a label next to a job in human output.
    """

    from .bundles import PROVENANCE_PATH
    from .local_catalog import _read_peek

    mapping: dict[str, str] = {}
    for name in _local_record_names(root):
        try:
            token = json.loads(_read_peek(root / "records" / name / PROVENANCE_PATH))["run_token"]
        except Exception:
            continue
        match = _LOCAL_RUN_TOKEN.fullmatch(str(token))
        if match is not None:
            mapping.setdefault(match.group(1), name)
    return mapping


def _local_minutes(epoch: float) -> str:
    return datetime.fromtimestamp(epoch).astimezone().strftime("%Y-%m-%d %H:%M")


def _table(header: Sequence[str], rows: Sequence[Sequence[str]]) -> list[str]:
    widths = [
        max(len(str(cell)) for cell in column) for column in zip(header, *rows, strict=True)
    ]
    return [
        "  ".join(str(cell).ljust(width) for cell, width in zip(row, widths, strict=True)).rstrip()
        for row in (header, *rows)
    ]


def _jobs(args: argparse.Namespace) -> tuple[ExitCode, dict[str, Any]]:
    """``traceback jobs``: every job under ROOT, newest first (read-only)."""

    from .labels import read_label
    from .problems import CODED_FAILURE
    from .store import JobStore

    root = args.root
    database = root / "runner" / "runner.sqlite3"
    if not database.is_file() or database.is_symlink():
        payload = _result(
            "jobs",
            "ok",
            "No jobs yet. Run traceback run BAM --reference ID --root ROOT",
            data={"jobs": []},
        )
        return ExitCode.OK, payload
    with JobStore(database) as store:
        records = sorted(
            store.list_jobs(limit=1000), key=lambda item: (-item.created_at, item.job_id)
        )[: args.limit]
        requests = {record.job_id: store.request(record.job_id) for record in records}
    by_prefix = _record_job_prefixes(root)
    rows: list[dict[str, Any]] = []
    table: list[list[str]] = []
    for record in records:
        reference, policy = _token_reference_policy(requests[record.job_id].sample_token)
        failure_code = None
        if record.state in _FAILED_STATES:
            match = CODED_FAILURE.match(record.last_error or "")
            failure_code = match.group(1) if match else None
        record_id = by_prefix.get(record.job_id[:16]) if record.state == JobState.COMPLETE else None
        rows.append(
            {
                "job_id": record.job_id,
                "state": record.state.value,
                "reference_id": reference,
                "policy": policy,
                "started_at": _iso_utc(record.created_at),
                "failure_code": failure_code,
                "record_id": record_id,
            }
        )
        label = read_label(root, record_id) if record_id else None
        table.append(
            [
                record.job_id[:12],
                record.state.value,
                reference,
                policy,
                label or "-",
                _local_minutes(record.created_at),
                failure_code or ("uncoded" if record.state in _FAILED_STATES else "-"),
            ]
        )
    lines = _table(
        ["JOB_ID", "STATE", "REFERENCE", "POLICY", "LABEL", "STARTED", "FAILURE"], table
    )
    lines.append("Labels are operator notes, not part of the signed record.")
    summary = (
        f"{len(rows)} job(s), newest first; unqualified, local, not for clinical use"
        if rows
        else "No jobs yet. Run traceback run BAM --reference ID --root ROOT"
    )
    payload = _result("jobs", "ok", summary, data={"jobs": rows})
    return ExitCode.OK, _with_human(payload, lines if rows else [], replace_data=True)


def _catalog_rows(root: Path) -> tuple[dict[str, Any], str | None]:
    """Map each imported record ID to its catalog result ID and import time.

    Returns ``({}, None)`` without a catalog; a catalog that does not open
    returns the refusal code instead of failing the listing.
    """

    from evidence_inspector.result_catalog import CatalogQuery

    from .local_catalog import explorer_paths, open_local_explorer

    imported: dict[str, Any] = {}
    try:
        with open_local_explorer(root) as explorer:
            if explorer is None:
                return {}, None
            cursor = None
            while True:
                page = explorer.catalog.query(CatalogQuery(limit=100, cursor=cursor))
                for ref in page.results:
                    artifact, _ = explorer_paths(root, ref.result_id)
                    try:
                        when = artifact.stat().st_mtime
                    except OSError:
                        when = None
                    imported[ref.bundle_record_id] = (ref.result_id, when)
                if page.next_cursor is None:
                    break
                cursor = page.next_cursor
    except ReferenceProblem as problem:
        return {}, problem.code
    return imported, None


def _verified_local_records(root: Path) -> list[tuple[str, Any | None]]:
    """(record ID, verified bundle or None) for every local record under ROOT."""

    from .bundles import verify_bundle
    from .signing import load_development_trust

    names = _local_record_names(root)
    try:
        trust = load_development_trust((root / _TRUST_RELATIVE).read_bytes())
    except Exception:
        trust = None
    records: list[tuple[str, Any | None]] = []
    for name in names:
        verified = None
        if trust is not None:
            try:
                verified = verify_bundle(root / "records" / name, trust)
            except Exception:
                verified = None
            if verified is not None and verified.manifest.record_id != name:
                verified = None
        records.append((name, verified))
    return records


def _store_or_none(root: Path) -> Any:
    from .store import JobStore

    database = root / "runner" / "runner.sqlite3"
    if not database.is_file() or database.is_symlink():
        return None
    return JobStore(database)


def _catalog_list(args: argparse.Namespace) -> tuple[ExitCode, dict[str, Any]]:
    """``traceback catalog list``: every record under ROOT and its catalog state."""

    import shlex

    from .labels import read_label

    root = args.root
    records = _verified_local_records(root)
    imported, catalog_problem = _catalog_rows(root)
    store = _store_or_none(root)
    try:
        twins = _measurement_twins(root, store) if records else {}
    except Exception:
        twins = {}
    finally:
        if store is not None:
            store.close()
    rows: list[dict[str, Any]] = []
    table: list[list[str]] = []
    hints: list[str] = []
    for record_id, verified in records:
        catalog = imported.get(record_id)
        measurement = verified.measurement if verified is not None else None
        reference = measurement.reference_id if measurement is not None else None
        when = catalog[1] if catalog is not None else None
        row = {
            "record_id": record_id,
            "reference_id": reference,
            "policy": "built-in",
            "eligible_alignments": (
                measurement.eligible_alignments if measurement is not None else None
            ),
            "imported": catalog is not None,
            "imported_at": _iso_utc(when) if when is not None else None,
            "result_id": catalog[0] if catalog is not None else None,
            "verification": "verified" if verified is not None else "not_verified",
            "same_measurement_as": twins.get(record_id),
        }
        rows.append(row)
        twin = twins.get(record_id)
        table.append(
            [
                _short_record(record_id),
                read_label(root, record_id) or "-",
                reference or "-",
                "built-in",
                f"{measurement.eligible_alignments:,}" if measurement is not None else "-",
                (
                    datetime.fromtimestamp(when).astimezone().strftime("%Y-%m-%d")
                    if when is not None
                    else "imported" if catalog is not None else "not imported"
                ),
                row["verification"].replace("_", " "),
                f"same measurement as {_short_record(twin)}" if twin else "-",
            ]
        )
        if catalog is None and verified is not None:
            hints.append(
                f"Import {_short_record(record_id)}: traceback catalog import "
                f"{_short_record(record_id)} --root {shlex.quote(str(root))}"
            )
    if not rows:
        payload = _result(
            "catalog list",
            "ok",
            "No records yet. Run traceback run BAM --reference ID --import --root ROOT",
            data={"records": [], "catalog_problem": catalog_problem},
        )
        return ExitCode.OK, payload
    lines = _table(
        ["RECORD", "LABEL", "REFERENCE", "POLICY", "ELIGIBLE", "IMPORTED", "VERIFICATION",
         "SAME MEASUREMENT"],
        table,
    )
    lines.append("Labels are operator notes, not part of the signed record.")
    if catalog_problem is not None:
        lines.append(f"The catalog under ROOT did not open ({catalog_problem}); see traceback doctor")
    lines.extend(hints)
    payload = _result(
        "catalog list",
        "ok",
        f"{len(rows)} record(s); unqualified, local, not for clinical use",
        data={"records": rows, "catalog_problem": catalog_problem},
    )
    return ExitCode.OK, _with_human(payload, lines, replace_data=True)


_CSV_COLUMNS = (
    "record_id", "reference_id", "policy_id", "min_mapq", "bin_lower", "bin_upper",
    "count", "eligible", "scanned",
)


def _catalog_export(args: argparse.Namespace) -> tuple[ExitCode, dict[str, Any]]:
    """``traceback catalog export --csv``: one row per (imported record, bin).

    Counts come from each record's verified signed measurement; nothing is
    derived.  No labels, paths or identifiers beyond record and reference IDs.
    """

    import csv
    import io

    from .local_authority import LOCAL_MIN_MAPPING_QUALITY, LOCAL_POLICY_ID

    root = args.root
    destination = args.csv_path
    if destination.exists() or destination.is_symlink():
        raise RunProblem(
            "TBX-CAT-003",
            "The CSV file already exists; nothing was written",
            cause="catalog export never overwrites a file",
            fix="Choose a new --csv file name, or move the existing file",
        )
    imported, catalog_problem = _catalog_rows(root)
    if catalog_problem is not None:
        raise RunProblem(
            catalog_problem,
            "The catalog under ROOT did not open; nothing was written",
            cause="the catalog or its trust and authority stores failed their checks",
            fix="Run traceback doctor --root ROOT",
        )
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(_CSV_COLUMNS)
    exported = 0
    for record_id, verified in _verified_local_records(root):
        if record_id not in imported or verified is None:
            continue
        measurement = verified.measurement
        built_in = measurement.definition_id == f"{LOCAL_POLICY_ID}.{measurement.reference_id}"
        for item in measurement.histogram:
            writer.writerow(
                (
                    record_id,
                    measurement.reference_id,
                    "built-in" if built_in else measurement.definition_id,
                    LOCAL_MIN_MAPPING_QUALITY if built_in else "",
                    item.bin.lower_inclusive,
                    "" if item.bin.upper_exclusive is None else item.bin.upper_exclusive,
                    item.count,
                    measurement.eligible_alignments,
                    measurement.records_scanned,
                )
            )
        exported += 1
    try:
        with destination.open("x", encoding="utf-8", newline="") as stream:
            stream.write(buffer.getvalue())
    except FileExistsError:
        raise RunProblem(
            "TBX-CAT-003",
            "The CSV file already exists; nothing was written",
            cause="catalog export never overwrites a file",
            fix="Choose a new --csv file name, or move the existing file",
        ) from None
    return ExitCode.OK, _result(
        "catalog export",
        "ok",
        f"Wrote {exported} imported record(s) as CSV rows (unqualified, local, not for "
        "clinical use)",
        data={"records": exported, "columns": list(_CSV_COLUMNS)},
    )


def _label(args: argparse.Namespace) -> tuple[ExitCode, dict[str, Any]]:
    """``traceback label RECORD_ID TEXT``: set or replace an operator note."""

    from .labels import LABEL_QUALIFIER, write_label

    root = args.root
    record_id = _resolve_record_id(root, args.record_id)
    if record_id is None:
        raise RunProblem(
            "TBX-CAT-001",
            "No local record under ROOT has this ID; no label was set",
            cause="the ID does not name exactly one record under ROOT/records",
            fix="Use a record ID from traceback catalog list --root ROOT",
            exit_code=ExitCode.NOT_FOUND,
        )
    previous = write_label(root, record_id, args.text)
    changed = previous is not None and previous != args.text
    lines = [f"LABEL  {args.text} ({LABEL_QUALIFIER})"]
    if changed:
        lines.append(f"Label changed from {previous} to {args.text}")
    payload = _result(
        "label",
        "ok",
        "Label set (an operator note, not part of the signed record)",
        data={"record_id": record_id, "label_set": True, "label_replaced": changed},
    )
    return ExitCode.OK, _with_human(payload, lines)


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
    from .web.records import LocalRecordSource
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
                    records=LocalRecordSource(root=root, catalog=explorer.catalog, store=store),
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
    except OSError as exc:
        # Fail closed: an unreadable registry must not fall back to synthetic.
        raise RunProblem(
            "TBX-REF-004",
            "preflight needs --reference: ROOT's references could not be listed",
            cause="ROOT/references exists but could not be read",
            fix="Add --reference ID, and check ROOT/references with traceback doctor",
            exit_code=ExitCode.USAGE,
        ) from exc
    if not identifiers:
        return
    path = Path(os.path.abspath(args.input))
    unaligned = False
    if not path.is_symlink() and path.is_file():  # htslib would log a missing path
        try:
            unaligned = header_is_unaligned(read_bam_header(path))
        except (OSError, ValueError):
            pass  # the registered preflight reports the unreadable BAM
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
    failure = _job_failure(runner, record)
    headline = view.headline
    if failure is not None:
        word = "FAILED" if record.state == JobState.TERMINAL_FAILURE else "RETRYABLE"
        headline = " ".join(
            part for part in (f"{word}:", failure["code"], failure["summary"]) if part
        )
    return ExitCode.OK, _result(
        "status",
        "ok",
        headline,
        data={
            "operator_state": view.model_dump(mode="json"),
            "trust_state": trust_state.value,
            "trust_source": trust_source.value,
            "trust_hint": _STATUS_TRUST_HINTS[trust_source],
            **({"failure": failure} if failure is not None else {}),
        },
    )


_FAILED_STATES = frozenset({JobState.TERMINAL_FAILURE, JobState.RETRYABLE_FAILURE})


def _job_failure(runner: Any, record: Any) -> dict[str, Any] | None:
    """The failed job's ``{code, summary, cause, fix}`` from its stored reason.

    Only a coded reason is shown; an uncoded one (it may name a file) never is.
    """

    from .problems import UNCODED_SUMMARY, failure_block

    if record.state not in _FAILED_STATES:
        return None
    return failure_block(runner.store.get(record.job_id).last_error) or {
        "code": None,
        "summary": UNCODED_SUMMARY,
        "cause": None,
        "fix": None,
    }


def _iso_utc(epoch: Any) -> str:
    return datetime.fromtimestamp(float(epoch), UTC).isoformat(timespec="seconds")


def _logs(args: argparse.Namespace) -> tuple[ExitCode, dict[str, Any]]:
    runner = _existing_runner(args.root)
    record = runner.status(args.job_id)
    failure = _job_failure(runner, record)
    events = [
        {
            "sequence": event["sequence"],
            "occurred_at": _iso_utc(event["occurred_at"]),
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
        data={
            "job_id": args.job_id,
            "events": events,
            **({"failure": failure} if failure is not None else {}),
        },
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
        request.workflow_release_sha256 != _synthetic_workflow_sha256()
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
        if args.catalog_command == "list":
            return _catalog_list(args)
        if args.catalog_command == "export":
            return _catalog_export(args)
        return _catalog_import(args)
    if args.command == "jobs":
        return _jobs(args)
    if args.command == "label":
        return _label(args)
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
    if command in {"run", "reference", "catalog", "jobs", "label"}:
        return True
    if command == "preflight":
        if args.reference_id is not None:
            return True
        # Without --reference on a ROOT with registrations (TBX-REF-004, or an
        # operator's unaligned BAM) the result is about local data too.
        from .references import list_reference_ids

        try:
            return bool(list_reference_ids(args.root))
        except OSError:
            return True
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

    failed_job_id: str | None = None
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
    except OperatorBusy as exc:
        failed_job_id = getattr(exc, "job_id", None)
        code, payload = ExitCode.BLOCKED, _result(
            args.command, "blocked",
            "A local action or unexpired worker lease is active; wait before resuming",
            data={"retryable": True},
        )
    except (FileNotFoundError, KeyError) as exc:
        failed_job_id = getattr(exc, "job_id", None)
        code, payload = ExitCode.NOT_FOUND, _result(
            args.command,
            "not_found",
            "Requested local job, bundle, or trust material was not found",
        )
    except Exception as exc:
        failed_job_id = getattr(exc, "job_id", None)
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
    if failed_job_id is not None and "job_id" not in payload["data"]:
        # The job a failed `run` created is named, so status and logs find it.
        payload["data"]["job_id"] = failed_job_id
    if _concerns_local_data(args):
        payload = _local_envelope(payload)
    _emit(payload, as_json=as_json)
    return int(code)


__all__ = ["ExitCode", "main"]
