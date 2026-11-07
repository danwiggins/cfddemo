"""``traceback lab support-bundle RUN_ID [--out PATH] [--force]``: a scrubbed archive of one run.

The bundle is for asking someone else for help.  It is one ``.tar.gz`` holding:

- ``<run_id>/run.jsonl``: the run log (``ROOT/lab/runs/<run_id>.jsonl``);
- ``<run_id>/doctor.json``: ``lab doctor --json`` captured now;
- ``<run_id>/versions.json``: package, Python, platform, samtools and minimap2
  version lines (or ``"missing"``), and the store version;
- ``<run_id>/failed/<step>/<attempt_id>/…``: every retained text file of a
  failed step's attempt directory (``result.json`` carries the scrubbed
  ``failure.stderr_tail``; ``artifacts.json`` the inventory; any text
  artifact the algorithm left);
- ``<run_id>/MANIFEST.json``: every member with its size and sha256, the
  failed steps (present or pruned), and anything skipped.

Every member passes through the D34 scrubber (``scrub.py``) with the host
name added, so no host path, user name or host name survives; the archive
headers carry no owner name either.  Binary files and files over
``MAX_MEMBER_BYTES`` are skipped and listed in the manifest.  Read names never
appear: the run log and result envelopes hold hashes only (D34).  Nothing is
uploaded; the operator sends the file.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import tarfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from traceback_runner.cli import _HUMAN, _local_envelope, _result, _with_human

from . import STORE_VERSION, __version__
from .problems import EXIT_OK, LabProblem
from .scrub import Scrubber
from .store import LabStore, RunLog, summarize_run

BUNDLE_SCHEMA = "traceback.lab.support-bundle.v1"
MAX_MEMBER_BYTES = 8 * 1024 * 1024  # per attempt file; the run log is never capped
# Members are store files, not stderr: never truncate a JSON line.
MAX_MEMBER_LINE = 1_000_000
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._@-]")
_ATTEMPT_ID = re.compile(r"^a-\d{8}T\d{12}-[0-9a-f]{8}$")


def _hostname() -> str | None:
    try:
        return socket.gethostname() or None
    except OSError:
        return None


def _is_text(content: bytes) -> bool:
    if b"\0" in content:
        return False
    try:
        content.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


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


def _attempt_dir(store: LabStore, event: dict[str, Any]) -> Path | None:
    attempt_id = event.get("attempt_id")
    input_id = event.get("input_id")
    identity = event.get("parameters_sha256")
    step = str(event.get("step", ""))
    algorithm_id = step.split("@", 1)[0]
    if not (isinstance(attempt_id, str) and _ATTEMPT_ID.fullmatch(attempt_id)):
        return None
    if not (isinstance(input_id, str) and isinstance(identity, str)):
        return None
    try:
        directory = store.result_dir(input_id, algorithm_id, identity) / "attempts" / attempt_id
    except ValueError:
        return None
    return directory if directory.is_dir() and not directory.is_symlink() else None


class _Bundle:
    def __init__(self, run_id: str, scrubber: Scrubber) -> None:
        self.run_id = run_id
        self.scrubber = scrubber
        self.members: list[tuple[str, bytes]] = []
        self.skipped: list[dict[str, Any]] = []

    def add_text(self, name: str, text: str) -> None:
        scrubbed = self.scrubber.text(text)
        if text.endswith("\n"):
            scrubbed += "\n"
        self.members.append((f"{self.run_id}/{name}", scrubbed.encode("utf-8")))

    def add_json(self, name: str, value: Any) -> None:
        self.add_text(name, json.dumps(value, indent=2, sort_keys=True) + "\n")

    def add_file(self, name: str, path: Path) -> bool:
        """Add one attempt file; skip (and record) binaries, oversize files and links."""

        member = f"{self.run_id}/{name}"
        if path.is_symlink() or not path.is_file():
            self.skipped.append({"name": member, "reason": "not_a_regular_file"})
            return False
        size = path.stat().st_size
        if size > MAX_MEMBER_BYTES:
            self.skipped.append({"name": member, "reason": "too_large", "bytes": size})
            return False
        content = path.read_bytes()
        if not _is_text(content):
            self.skipped.append({"name": member, "reason": "binary", "bytes": size})
            return False
        self.add_text(name, content.decode("utf-8"))
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
            "skipped": self.skipped,
        }


def _write_tar(destination: Path, members: list[tuple[str, bytes]], mtime: int) -> int:
    """Write the archive next to its destination, then rename it into place."""

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
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


def build_bundle(store: LabStore, run_id: str, destination: Path, *, force: bool) -> dict[str, Any]:
    """Collect, scrub and write one run's bundle; the caller resolved the paths."""

    from .doctor import doctor_command

    try:
        log = RunLog(store, run_id)
    except ValueError:
        log = None
    events = log.events() if log is not None else []
    if not events:
        raise LabProblem(
            "TBX-LAB-028",
            cause=f"no Lab run {run_id!r} under this ROOT",
            fix="`traceback lab runs` lists runs; pass the same --root you ran with",
            data={"run_id": run_id, "candidates": store.run_ids()[:10]},
        )
    if destination.exists() and not force:
        raise LabProblem(
            "TBX-LAB-029",
            cause=f"a support bundle for {run_id} already exists at the output path",
            fix="pass --force to replace it, or --out PATH for a new file",
            data={"run_id": run_id, "bundle": destination.name},
        )
    scrubber = Scrubber(root=store.root, hostname=_hostname(), max_line=MAX_MEMBER_LINE)
    bundle = _Bundle(run_id, scrubber)
    bundle.add_text("run.jsonl", log.path.read_bytes().decode("utf-8", errors="replace"))
    _, doctor = doctor_command(None, store)
    bundle.add_json("doctor.json", {key: value for key, value in doctor.items() if key != _HUMAN})
    bundle.add_json("versions.json", versions())
    failed: list[dict[str, Any]] = []
    for event in _failed_steps(events):
        step = str(event.get("step", ""))
        attempt = _attempt_dir(store, event)
        attempt_id = event.get("attempt_id") if isinstance(event.get("attempt_id"), str) else None
        entry: dict[str, Any] = {
            "step": step,
            "attempt_id": attempt_id,
            "attempt_present": attempt is not None,
            "first_line": event.get("first_line"),
            "files": 0,
        }
        if attempt is not None:
            prefix = f"failed/{_SAFE_NAME.sub('_', step)}/{attempt.name}"
            for path in sorted(attempt.rglob("*")):
                if path.is_dir() and not path.is_symlink():
                    continue
                relative = path.relative_to(attempt).as_posix()
                if bundle.add_file(f"{prefix}/{relative}", path):
                    entry["files"] += 1
        failed.append(entry)
    created = time.time()
    created_utc = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(created))
    bundle.add_json("MANIFEST.json", bundle.manifest(failed, created_utc))
    size = _write_tar(destination, bundle.members, int(created))
    return {
        "run_id": run_id,
        "status": summarize_run(events, store)["status"],
        "bundle": str(destination),
        "bytes": size,
        "members": [name for name, _ in bundle.members],
        "failed_steps": failed,
        "skipped": bundle.skipped,
    }


def support_bundle_command(args: argparse.Namespace, store: LabStore) -> tuple[int, dict[str, Any]]:
    store.check_version()
    run_id = str(args.run_id)
    destination = (
        Path(os.path.abspath(Path(args.out).expanduser())) if args.out is not None
        else store.lab / "support" / f"{run_id}.tar.gz"
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
        f"FAILED   {len(data['failed_steps'])} step(s)" + (f", {missing} attempt director{'y' if missing == 1 else 'ies'} already pruned" if missing else ""),
    ]
    if data["skipped"]:
        lines.append(f"SKIPPED  {len(data['skipped'])} file(s) (binary or too large; listed in MANIFEST.json)")
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
    "BUNDLE_SCHEMA",
    "MAX_MEMBER_BYTES",
    "add_support_bundle_parser",
    "build_bundle",
    "support_bundle_command",
    "versions",
]
