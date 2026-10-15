"""``traceback lab doctor [--json]``: setup and store checks, each with a fix (D36).

It calls the signed doctor's check functions read-only and adds the Lab's
own.  Every non-pass check carries ``fix`` (one command or action).  Exit 3
only when the example journey itself is blocked (Python, samtools, minimap2
or ROOT unusable); missing scientific assets are warnings, because they only
skip algorithms.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

from traceback_runner.cli import (
    _doctor_check,
    _doctor_copy_number_toolchain,
    _doctor_disk,
    _doctor_references,
    _doctor_root,
    _local_envelope,
    _result,
    _with_human,
)

from . import STORE_VERSION
from .ingest import BUDGET_FACTOR, GIB, MARGIN_BYTES, MMI_FACTOR, mmi_path, resolve_reference
from .problems import EXIT_BLOCKED, EXIT_OK, LabProblem
from .store import LabStore
from .tools import MIN_MINIMAP2, MIN_SAMTOOLS, find_minimap2, find_samtools

FULL_RUN_BYTES = 44 * 10**9  # a ~400-chunk FBB10481-sized run (D30)
MIN_RAM = 16 * GIB


def _check(name: str, status: str, detail: str, fix: str | None = None, *, blocks_example: bool = False) -> dict[str, Any]:
    check = _doctor_check(name, status, detail)
    if status != "pass":
        check["fix"] = fix or "see docs/LAB-GUIDE.md"
    check["blocks_example"] = blocks_example and status == "blocked"
    return check


def _tool(tool: Any, minimum: tuple[int, int]) -> dict[str, Any]:
    need = f"{minimum[0]}.{minimum[1]}"
    if tool.path is None:
        return _check(f"lab_{tool.name}", "blocked", f"{tool.name} not on PATH (needs >= {need} to align)", f"brew install {tool.name}", blocks_example=True)
    if tool.numeric is None or tool.numeric < minimum:
        return _check(f"lab_{tool.name}", "blocked", f"{tool.name} {tool.version or '?'} is below {need}", f"brew upgrade {tool.name}", blocks_example=True)
    return _check(f"lab_{tool.name}", "pass", f"{tool.name} {tool.version}")


def _ram() -> int | None:
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (ValueError, OSError, AttributeError):
        return None


EXAMPLE_BYTES = 3 * GIB  # the example's chunks, index, alignment and margin


def _writable(path: Path) -> bool:
    candidate = path
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    return candidate.is_dir() and os.access(candidate, os.W_OK | os.X_OK)


def lab_checks(store: LabStore) -> list[dict[str, Any]]:
    """Checks with ``<ROOT>`` as a token: JSON never carries a host path beyond
    ``data.root``; the human output substitutes the real ROOT."""

    root = store.root
    checks: list[dict[str, Any]] = [
        _check("python", "pass" if sys.version_info[:2] == (3, 11) else "blocked", f"Python {sys.version_info[0]}.{sys.version_info[1]}", "uv sync (Python 3.11)", blocks_example=True),
        _tool(find_samtools(), MIN_SAMTOOLS),
        _tool(find_minimap2(), MIN_MINIMAP2),
    ]
    signed_root = _doctor_root(root)
    if not _writable(root):
        checks.append(_check("root", "blocked", "ROOT is not writable here", "pass --root on a writable volume", blocks_example=True))
    else:
        checks.append(_check("root", signed_root["status"], signed_root["detail"], "chmod go-w <ROOT> (or use another --root)"))
    disk = _doctor_disk(root)
    free = disk.get("free_bytes")
    need = int(BUDGET_FACTOR * FULL_RUN_BYTES + MMI_FACTOR * 3.1e9 + MARGIN_BYTES)
    if isinstance(free, int) and free < EXAMPLE_BYTES:
        checks.append(_check("disk", "blocked", f"{free / GIB:.1f} GB free; the example needs about {EXAMPLE_BYTES / GIB:.0f} GB", "free space or use --root on a larger volume", blocks_example=True))
    elif isinstance(free, int) and free < need:
        checks.append(_check("disk", "warn", f"{free / GIB:.0f} GB free; a full 44 GB kit run needs about {need / GIB:.0f} GB", "free space or use --root on a larger volume"))
    else:
        checks.append(_check("disk", "pass" if isinstance(free, int) else "warn", disk["detail"], "check the ROOT volume (df -h <ROOT>)"))
    ram = _ram()
    checks.append(
        _check("ram", "pass" if ram and ram >= MIN_RAM else "warn",
               f"{ram / GIB:.0f} GB RAM" if ram else "RAM size unknown",
               "align on a machine with >= 16 GB RAM (minimap2 with hg38 needs about 12 GB)")
    )
    try:
        stored = store.stored_version()
    except LabProblem:
        stored = -1
    if stored is not None and (stored == -1 or stored > STORE_VERSION):
        checks.append(_check("store_version", "blocked", f"ROOT/lab/STORE_VERSION {stored} is not readable by this checkout ({STORE_VERSION})", "git pull && uv sync", blocks_example=True))
    else:
        checks.append(_check("store_version", "pass", f"store version {stored or STORE_VERSION}"))
    try:
        reference = resolve_reference(root, None)
        checks.append(_check("reference", "pass", f"reference {reference.reference_id} registered"))
        if mmi_path(store, reference).is_file():
            checks.append(_check("mmi", "pass", "minimap2 index present"))
        else:
            checks.append(_check("mmi", "warn", "minimap2 index not built yet (hg38: ~20-40 min, ~8 GB)", "traceback lab run <chunk dir> --root <ROOT> builds it once (or pass --mmi PATH)"))
    except LabProblem as problem:
        checks.append(_check("reference", "warn", problem.cause, "traceback reference register --fasta hg38.fa --id hg38 --root <ROOT>"))
    for check in _doctor_references(root, deep=False):
        if check["status"] != "pass":
            checks.append(_check(f"signed_{check['name']}", "warn", check["detail"], "traceback doctor --deep --root <ROOT>"))
    toolchain = _doctor_copy_number_toolchain(deep=False)
    checks.append(_check("ichor_toolchain", "pass" if toolchain["status"] == "pass" else "warn", f"{toolchain['detail']} (else copy-number skips)", "traceback toolchain install ichor"))
    try:
        from .adapters import ichor
        from .algorithms.copy_number import toolchain_or_none

        resolved = toolchain_or_none()
        if resolved is not None:
            ichor.resolve_assets(root, resolved)
            checks.append(_check("ichor_assets", "pass", "ichorCNA method assets registered"))
        else:
            checks.append(_check("ichor_assets", "warn", "ichorCNA assets need the toolchain first", "traceback toolchain install ichor"))
    except Exception:  # noqa: BLE001 - any asset problem means "not registered"
        checks.append(_check("ichor_assets", "warn", "ichorCNA method assets not registered under ROOT (else copy-number skips)", "traceback method-asset register --from-toolchain copy-number --root <ROOT>"))
    try:
        from traceback_runner.toolchain import resolve_modkit

        resolve_modkit()
        checks.append(_check("modkit", "pass", "modkit installed (cell-origin, Phase 0b)"))
    except Exception:  # noqa: BLE001
        checks.append(_check("modkit", "warn", "modkit not installed (cell-origin will skip; Phase 0b)", "traceback toolchain install modkit"))
    from traceback_runner.references import list_assets

    kinds = {kind.value for kind, _ in list_assets(root)}
    if {"loyfer-atlas", "loyfer-markers", "loyfer-regions"} <= kinds:
        checks.append(_check("loyfer_assets", "pass", "Loyfer atlas assets registered"))
    else:
        checks.append(_check("loyfer_assets", "warn", "Loyfer atlas assets not registered (cell-origin will skip; Phase 0b)", "traceback method-asset register --from-dir LOYFER_DIR --root <ROOT>"))
    active = store.read_active()
    if active is not None and not active.active:
        checks.append(_check("active", "warn", f"stale heartbeat from {active.run_id} (pid {active.pid} gone); the run was interrupted", "rerun the same lab command; only missing results recompute"))
    orphans = store.orphaned_staging()
    if orphans:
        checks.append(_check("staging", "warn", f"{len(orphans)} orphaned .staging-* director{'y' if len(orphans) == 1 else 'ies'} under ROOT/lab", "after confirming no lab command runs: find <ROOT>/lab -name '.staging-*' -prune -exec rm -rf {} +"))
    return checks


def doctor_command(args: Any, store: LabStore) -> tuple[int, dict[str, Any]]:
    checks = lab_checks(store)
    blocked = [check for check in checks if check.get("blocks_example")]
    warnings = [check for check in checks if check["status"] == "warn"]
    steps = [check["fix"] for check in checks if check["status"] != "pass" and check.get("fix")]
    summary = (
        "The example journey is blocked" if blocked
        else "Lab ready for `traceback lab run --example`" + (f" ({len(warnings)} warning{'s' if len(warnings) != 1 else ''})" if warnings else "")
    )
    payload = _local_envelope(
        _result("lab doctor", "blocked" if blocked else "ok", summary, data={"root": str(store.root), "checks": checks, "setup_steps": steps})
    )
    real = str(store.root)
    lines = [f"ROOT  {real}"]
    for check in checks:
        label = {"pass": "PASS", "warn": "WARN", "blocked": "BLOCKED"}.get(check["status"], check["status"].upper())
        lines.append(f"{label:<8} {check['name']:<16} {check['detail']}")
        if check["status"] != "pass":
            lines.append(f"{'':<8} {'':<16} fix: {check['fix'].replace('<ROOT>', real)}")
    if steps:
        lines.append("Missing setup steps, in order:")
        lines.extend(f"  {number}. {step.replace('<ROOT>', real)}" for number, step in enumerate(steps, start=1))
    return (EXIT_BLOCKED if blocked else EXIT_OK), _with_human(payload, lines, replace_data=True)


def add_doctor_parser(commands: Any, common: Any) -> None:
    doctor = commands.add_parser("doctor", help="check setup for the Lab, each problem with a fix")
    common(doctor)
    doctor.set_defaults(handler=doctor_command)


__all__ = ["add_doctor_parser", "doctor_command", "lab_checks"]
