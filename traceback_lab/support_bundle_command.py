"""``traceback lab support-bundle RUN_ID [--out PATH] [--force]``: a scrubbed archive of one run.

The bundle is for asking someone else for help.  It is one ``.tar.gz`` holding:

- ``<run_id>/run.jsonl``: the run log (``ROOT/lab/runs/<run_id>.jsonl``), one
  event per line;
- ``<run_id>/doctor.json``: ``lab doctor --json`` captured now;
- ``<run_id>/versions.json``: package, Python, platform, samtools and minimap2
  version lines (or ``"missing"``), and the store version;
- ``<run_id>/failed/<step>/<attempt_id>/result.json`` and ``artifacts.json``:
  for every failed step whose attempt directory is still retained, the
  envelope (with the scrubbed ``failure.stderr_tail``) and the artifact
  inventory.  Nothing else from an attempt is included: a method's own
  artifacts (tables, calls, logs) can carry read names or sample labels the
  scrubber cannot know, so they are omitted by name;
- ``<run_id>/MANIFEST.json``: every member with its size and sha256, the
  failed steps (present, pruned or outside ROOT), and every omitted file.

Every member is JSON (or JSON lines).  Its string values pass through the D34
scrubber (``scrub.py``: ROOT, paths, home, login); free-text values
(``HOST_TEXT_KEYS``: stderr, messages, version strings) also get host-name
redaction (the host name and its first label, as case-insensitive words of
three or more characters).  Keys, identifiers and member names are never
rewritten, so the JSON stays readable.  Member names are built from validated ids and fixed file
names only.  The archive's entries carry no owner name.  Nothing is uploaded;
the operator sends the file.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import platform
import re
import secrets
import shutil
import socket
import subprocess
import tarfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from traceback_runner.cli import _HUMAN, _local_envelope, _result, _with_human
from traceback_runner.serialization import canonical_json_bytes

from . import STORE_VERSION, __version__
from .problems import EXIT_OK, LabProblem
from .scrub import REDACTED, Scrubber
from .store import _ATTEMPT_ID, _RUN_ID, LabStore, RunLog, summarize_run

BUNDLE_SCHEMA = "traceback.lab.support-bundle.v1"
# The only attempt files that enter the bundle (their values are hashes,
# identifiers, counts and already-scrubbed stderr).
ATTEMPT_ALLOWLIST = ("result.json", "artifacts.json")
MAX_MEMBER_BYTES = 8 * 1024 * 1024  # per attempt file; the run log is never capped
# Members are store files, not stderr: never truncate a JSON line.
MAX_MEMBER_LINE = 1_000_000
MIN_HOST_WORD = 3
# Host-name redaction applies to free text only: these keys (or the list under
# them) hold stderr, messages and version strings.  Identifiers, enumerations,
# keys and member names are never rewritten, so a host called ``lab`` or
# ``run`` cannot corrupt the JSON.
HOST_TEXT_KEYS = frozenset({
    "first_line", "stderr_tail", "skip_message", "detail", "fix", "summary", "cause",
    "setup_steps", "platform", "samtools", "minimap2",
})
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._@-]")


def _hostname() -> str | None:
    try:
        return socket.gethostname() or None
    except OSError:
        return None


def _host_pattern(hostname: str | None) -> re.Pattern[str] | None:
    """The host name and its first label as case-insensitive whole words."""

    if not hostname:
        return None
    words = {word for word in (hostname, hostname.split(".", 1)[0]) if len(word) >= MIN_HOST_WORD}
    if not words:
        return None
    alternatives = "|".join(re.escape(word) for word in sorted(words, key=len, reverse=True))
    return re.compile(rf"(?<![A-Za-z0-9])(?:{alternatives})(?![A-Za-z0-9])", re.IGNORECASE)


class _ValueScrubber:
    """Scrub string values of a JSON structure; keys are never rewritten."""

    def __init__(self, scrubber: Scrubber, host: re.Pattern[str] | None) -> None:
        self.scrubber = scrubber
        self.host = host

    def text(self, value: str, *, free_text: bool = True) -> str:
        out = "\n".join(self.scrubber.line(line) for line in value.split("\n"))
        if free_text and self.host is not None:
            out = self.host.sub(REDACTED, out)
        return out

    def walk(self, value: Any, *, free_text: bool = False) -> Any:
        if isinstance(value, str):
            return self.text(value, free_text=free_text)
        if isinstance(value, dict):
            return {key: self.walk(item, free_text=key in HOST_TEXT_KEYS) for key, item in value.items()}
        if isinstance(value, list):
            return [self.walk(item, free_text=free_text) for item in value]
        return value


def _version_line(name: str) -> str:
    """The first line of ``<tool> --version``, ``missing`` when not on PATH."""

    executable = shutil.which(name)
    if executable is None:
        return "missing"
    try:
        completed = subprocess.run(  # noqa: S603 - absolute executable, no shell
            [executable, "--version"], capture_output=True, text=True, errors="replace",
            timeout=20, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return "unreadable"
    lines = [line for line in (completed.stdout + completed.stderr).splitlines() if line.strip()]
    return lines[0].strip() if lines and completed.returncode == 0 else "unreadable"


def versions() -> dict[str, Any]:
    return {
        "traceback": __version__,
        "python": platform.python_version(),
        "platform": f"{platform.system()} {platform.release()} {platform.machine()}".strip(),
        "samtools": _version_line("samtools"),
        "minimap2": _version_line("minimap2"),
        "store_version": STORE_VERSION,
    }


def _failed_steps(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The failed ``step_end`` events, last occurrence per step, in log order."""

    by_step: dict[str, dict[str, Any]] = {}
    for event in events:
        if event.get("event") == "step_end" and event.get("status") == "failed":
            by_step[str(event.get("step", ""))] = event
    return list(by_step.values())


def _contained(path: Path, root: Path) -> bool:
    """``path`` resolves under ``root`` and no component below ``root`` is a symlink."""

    try:
        resolved_root = root.resolve(strict=True)
        if not path.resolve(strict=True).is_relative_to(resolved_root):
            return False
        relative = path.relative_to(root)
    except (OSError, ValueError):
        return False
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            return False
    return True


def _attempt_dir(store: LabStore, event: dict[str, Any]) -> tuple[Path | None, str | None]:
    """The retained attempt directory of a failed step, or why there is none."""

    attempt_id = event.get("attempt_id")
    input_id = event.get("input_id")
    identity = event.get("parameters_sha256")
    algorithm_id = str(event.get("step", "")).split("@", 1)[0]
    if not (isinstance(attempt_id, str) and re.fullmatch(_ATTEMPT_ID, attempt_id)):
        return None, "no_attempt"
    if not (isinstance(input_id, str) and isinstance(identity, str)):
        return None, "no_attempt"
    try:
        directory = store.result_dir(input_id, algorithm_id, identity) / "attempts" / attempt_id
    except ValueError:
        return None, "no_attempt"
    if not directory.is_dir():
        return None, "pruned"
    if not _contained(directory, store.lab):
        return None, "outside_root"
    return directory, None


class _Bundle:
    """Members by their final name; names are built once and reused by the manifest."""

    def __init__(self, run_id: str, values: _ValueScrubber) -> None:
        self.run_id = run_id
        self.values = values
        self.members: list[tuple[str, bytes]] = []
        self.omitted: list[dict[str, Any]] = []

    def name(self, *parts: str) -> str:
        return "/".join([self.run_id, *(_SAFE_NAME.sub("_", part) for part in parts)])

    def add_json(self, name: str, value: Any) -> None:
        scrubbed = self.values.walk(value)
        self.members.append((name, (json.dumps(scrubbed, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")))

    def add_jsonl(self, name: str, raw: bytes) -> None:
        lines: list[bytes] = []
        for text in raw.decode("utf-8", errors="replace").splitlines():
            try:
                value = json.loads(text)
            except ValueError:  # a torn last line after a crash: scrub it as text
                lines.append(self.values.text(text).encode("utf-8"))
                continue
            lines.append(canonical_json_bytes(self.values.walk(value)))
        self.members.append((name, b"".join(line + b"\n" for line in lines)))

    def omit(self, name: str, reason: str, **extra: Any) -> None:
        self.omitted.append({"name": name, "reason": reason, **extra})

    def add_attempt_file(self, name: str, path: Path) -> bool:
        """Add one allowlisted attempt file as JSON; omit (and record) anything else."""

        if path.is_symlink() or not path.is_file():
            self.omit(name, "not_a_regular_file")
            return False
        size = path.stat().st_size
        if size > MAX_MEMBER_BYTES:
            self.omit(name, "too_large", bytes=size)
            return False
        try:
            value = json.loads(path.read_bytes())
        except ValueError:
            self.omit(name, "unreadable", bytes=size)
            return False
        self.add_json(name, value)
        return True

    def manifest(self, failed: list[dict[str, Any]], created_utc: str) -> dict[str, Any]:
        return {
            "schema": BUNDLE_SCHEMA,
            "run_id": self.run_id,
            "created_utc": created_utc,
            "members": [
                {"name": name, "bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()}
                for name, content in self.members
            ],
            "failed_steps": failed,
            "omitted": self.omitted,
        }


def _write_tar(destination: Path, members: list[tuple[str, bytes]], mtime: int) -> int:
    """Write the archive next to its destination, then rename it into place."""

    destination.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(8):
        temporary = destination.with_name(f".{destination.name}.{os.getpid()}-{secrets.token_hex(4)}.tmp")
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:  # a stale temp from an interrupted write: pick another name
            continue
        break
    else:
        raise OSError("could not create a temporary archive next to the destination")
    try:
        with os.fdopen(descriptor, "wb") as raw, tarfile.open(fileobj=raw, mode="w:gz") as archive:
            for name, content in members:
                info = tarfile.TarInfo(name)
                info.size = len(content)
                info.mtime = mtime
                info.mode = 0o600
                info.uid = info.gid = 0
                info.uname = info.gname = ""  # never the operator's login
                archive.addfile(info, io.BytesIO(content))
        os.replace(temporary, destination)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return destination.stat().st_size


def _not_found(store: LabStore, run_id: str, *, reason: str) -> LabProblem:
    candidates = [candidate for candidate in store.run_ids() if candidate != run_id][:10]
    cause = (
        f"the run log for {run_id} has no events; nothing to bundle" if reason == "empty_log"
        else f"no Lab run {run_id!r} under this ROOT"
    )
    return LabProblem(
        "TBX-LAB-028",
        cause=cause,
        fix="`traceback lab runs` lists runs; pass the same --root you ran with",
        data={"run_id": run_id, "reason": reason, "candidates": candidates},
    )


def build_bundle(store: LabStore, run_id: str, destination: Path, *, force: bool) -> dict[str, Any]:
    """Collect, scrub and write one run's bundle; the caller resolved the paths."""

    from .doctor import doctor_command

    if not re.fullmatch(_RUN_ID, run_id):
        raise _not_found(store, run_id, reason="unknown_run")
    log = RunLog(store, run_id)
    if not log.path.is_file():
        raise _not_found(store, run_id, reason="unknown_run")
    events = log.events()
    if not events:
        raise _not_found(store, run_id, reason="empty_log")
    if destination.is_dir():
        raise LabProblem(
            "TBX-LAB-029",
            cause="the output path is a directory; the bundle is one .tar.gz file",
            fix="pass --out PATH naming a file (for example PATH/RUN_ID.tar.gz)",
            data={"run_id": run_id, "bundle": destination.name, "is_directory": True},
        )
    if destination.exists() and not force:
        raise LabProblem(
            "TBX-LAB-029",
            cause=f"a support bundle for {run_id} already exists at the output path",
            fix="pass --force to replace it, or --out PATH for a new file",
            data={"run_id": run_id, "bundle": destination.name, "is_directory": False},
        )
    values = _ValueScrubber(Scrubber(root=store.root, max_line=MAX_MEMBER_LINE), _host_pattern(_hostname()))
    bundle = _Bundle(run_id, values)
    bundle.add_jsonl(bundle.name("run.jsonl"), log.path.read_bytes())
    _, doctor = doctor_command(None, store)
    bundle.add_json(bundle.name("doctor.json"), {key: value for key, value in doctor.items() if key != _HUMAN})
    bundle.add_json(bundle.name("versions.json"), versions())
    failed: list[dict[str, Any]] = []
    for event in _failed_steps(events):
        step = str(event.get("step", ""))
        attempt, reason = _attempt_dir(store, event)
        attempt_id = event.get("attempt_id") if isinstance(event.get("attempt_id"), str) else None
        entry: dict[str, Any] = {
            "step": step,
            "attempt_id": attempt_id,
            "attempt_present": attempt is not None,
            "reason": reason,
            "first_line": values.walk(event.get("first_line"), free_text=True),
            "files": 0,
        }
        if attempt is not None:
            for path in sorted(attempt.rglob("*")):
                if path.is_dir() and not path.is_symlink():
                    continue
                relative = path.relative_to(attempt).as_posix()
                name = bundle.name("failed", step, attempt.name, *relative.split("/"))
                if relative not in ATTEMPT_ALLOWLIST:
                    bundle.omit(name, "not_allowlisted")
                elif bundle.add_attempt_file(name, path):
                    entry["files"] += 1
        elif reason == "outside_root" and attempt_id is not None:
            bundle.omit(bundle.name("failed", step, attempt_id), "outside_root")
        failed.append(entry)
    created = time.time()
    created_utc = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(created))
    bundle.add_json(bundle.name("MANIFEST.json"), bundle.manifest(failed, created_utc))
    size = _write_tar(destination, bundle.members, int(created))
    return {
        "run_id": run_id,
        "status": summarize_run(events, store)["status"],
        "bundle": str(destination),
        "bytes": size,
        "members": [name for name, _ in bundle.members],
        "failed_steps": failed,
        "omitted": bundle.omitted,
    }


def support_bundle_command(args: argparse.Namespace, store: LabStore) -> tuple[int, dict[str, Any]]:
    store.check_version()
    run_id = str(args.run_id)
    destination = (
        Path(os.path.abspath(Path(args.out).expanduser())) if args.out is not None
        else store.lab / "support" / f"{_SAFE_NAME.sub('_', run_id)}.tar.gz"
    )
    data = build_bundle(store, run_id, destination, force=bool(args.force))
    missing = sum(1 for entry in data["failed_steps"] if not entry["attempt_present"])
    summary = (
        f"support bundle written for {run_id} ({len(data['members'])} members, {data['bytes']} bytes); "
        "nothing was uploaded"
    )
    payload = _local_envelope(_result("lab support-bundle", "ok", summary, data={"root": str(store.root), **data}))
    lines = [
        f"ROOT  {store.root}",
        f"BUNDLE   {destination}",
        f"SIZE     {data['bytes']} bytes",
        f"MEMBERS  {len(data['members'])}",
        f"FAILED   {len(data['failed_steps'])} step(s)" + (f", {missing} without a retained attempt directory" if missing else ""),
    ]
    if data["omitted"]:
        lines.append(f"OMITTED  {len(data['omitted'])} file(s) (not allowlisted, too large or outside ROOT; listed in MANIFEST.json)")
    lines.append("Scrubbed: no host path, user name or host name; read names never appear. Send the file yourself.")
    return EXIT_OK, _with_human(payload, lines, replace_data=True)


def add_support_bundle_parser(commands: Any, common: Callable[[argparse.ArgumentParser], None]) -> None:
    bundle = commands.add_parser(
        "support-bundle", help="write a scrubbed .tar.gz of one run for asking for help"
    )
    bundle.add_argument("run_id", metavar="RUN_ID")
    bundle.add_argument("--out", type=Path, default=None, metavar="PATH",
                        help="archive path (default: ROOT/lab/support/RUN_ID.tar.gz)")
    bundle.add_argument("--force", action="store_true", help="replace an existing archive")
    common(bundle)
    bundle.set_defaults(handler=support_bundle_command)


__all__ = [
    "ATTEMPT_ALLOWLIST",
    "BUNDLE_SCHEMA",
    "MAX_MEMBER_BYTES",
    "add_support_bundle_parser",
    "build_bundle",
    "support_bundle_command",
    "versions",
]
