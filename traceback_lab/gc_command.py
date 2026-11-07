"""``traceback lab gc [--apply] [--keep-attempts N] [--staging-only]``: reclaim stale Lab store bytes.

Dry-run by default: the command lists what it would delete, grouped by
category with per-item and total byte counts, and exits 0 without touching
the store.  ``--apply`` takes the writer lock and deletes exactly that list.

Categories:

- ``attempts``: failed attempts beyond the newest ``--keep-attempts`` (default
  3, the store's own retention) per result directory.  The attempt named by
  ``selected.json`` (and its recorded last success) and every ``ok`` attempt
  are never candidates.  A result directory whose selector or any attempt is
  unreadable is skipped whole and reported: unknown never triggers deletion
  (D35).
- ``staging``: orphaned ``.staging-*`` directories under ``ROOT/lab`` (the set
  ``lab doctor`` reports).  A dry run leaves them unlisted while a live writer
  holds the lock, because that writer may still own them.
- ``interrupted``: ``.staging-gc-<attempt>`` directories, an attempt removal
  that a previous ``--apply`` renamed aside and then could not finish.  The
  store ignores every ``.staging-*`` name, so a half-removed attempt is never
  mistaken for a published one and a rerun always sees it.
- Panels of normals are not collected: the store keeps no index of which
  cohort generation, retained copy-number attempt or pending cohort
  transaction references a panel, so there is no unambiguous "unused" answer.

While a cohort selection transaction is pending every category is held.  A
writer replays that transaction before it may change the store, so
``--apply`` plans only after the replay; a dry run reports the hold.

An attempt is removed in two steps so the store never sees a partial one:
rename to a ``.staging-gc-`` sibling, then delete the renamed tree.  Bytes
are measured after the deletion attempt, so reported counts are what the
volume actually freed.  Aligned inputs, run logs and selected results are
never touched.  Every item is named by one store-relative label form
(``<dir>/<id12>/…``), never a host path (D34).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from traceback_runner.cli import _local_envelope, _result, _with_human

from .identity import ALGORITHM_ID_PATTERN, SHA256_PATTERN
from .problems import EXIT_OK, LabProblem, problem_lines, problem_payload
from .scrub import Scrubber
from .store import _ATTEMPT_ID, KEEP_FAILED_ATTEMPTS, STAGING_PREFIX, LabStore, new_run_id

GC_PREFIX = f"{STAGING_PREFIX}gc-"
PON_NOT_COLLECTED = (
    "panel references (cohort generations, retained copy-number attempts, a pending "
    "cohort transaction) are not indexed by the store; no unambiguous unused answer"
)
HELD_PENDING_COHORT = "a cohort selection transaction is pending; nothing is deleted until a writer replays it"
HELD_ACTIVE_WRITER = "a live writer may own them"
SKIPPED_STAGING_ONLY = "--staging-only"
CATEGORIES = ("attempts", "staging", "interrupted")
_UNKNOWN = object()


@dataclass(frozen=True)
class GcItem:
    """One directory the collector would remove."""

    category: str
    label: str
    path: Path
    bytes: int
    detail: dict[str, Any] = field(default_factory=dict)

    def public(self) -> dict[str, Any]:
        return {**self.detail, "label": self.label, "bytes": self.bytes}


@dataclass(frozen=True)
class GcOutcome:
    """What one ``--apply`` deletion actually did, measured afterwards."""

    item: GcItem
    removed: bool
    freed_bytes: int
    remaining_bytes: int
    error: OSError | None = None
    leftover: str | None = None

    def public(self) -> dict[str, Any]:
        return {
            "label": self.item.label,
            "category": self.item.category,
            "removed": self.removed,
            "freed_bytes": self.freed_bytes,
            "remaining_bytes": self.remaining_bytes,
            "leftover": self.leftover,
            "error": None if self.error is None else type(self.error).__name__,
        }


@dataclass
class GcPlan:
    keep_attempts: int
    attempts: list[GcItem] = field(default_factory=list)
    staging: list[GcItem] = field(default_factory=list)
    interrupted: list[GcItem] = field(default_factory=list)
    skipped_results: list[dict[str, str]] = field(default_factory=list)
    attempts_skipped: str | None = None
    held_reason: str | None = None
    held_staging: int = 0
    held_interrupted: int = 0

    @property
    def items(self) -> list[GcItem]:
        return [*self.attempts, *self.staging, *self.interrupted]

    @property
    def total_bytes(self) -> int:
        return sum(item.bytes for item in self.items)


def _keep_attempts(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        number = -1
    if number < 0:
        raise argparse.ArgumentTypeError("--keep-attempts takes a non-negative integer")
    return number


def add_gc_parser(commands: Any, common: Callable[[argparse.ArgumentParser], None]) -> None:
    gc = commands.add_parser(
        "gc",
        help="list (default) or delete (--apply) stale failed attempts and orphaned staging",
        description=(
            "Dry-run by default: lists what would be deleted, grouped by category, with byte "
            "counts, and deletes nothing. --apply takes the Lab writer lock and deletes exactly "
            "that list. Selected and ok attempts, aligned inputs, run logs and panels of normals "
            "are never deleted."
        ),
    )
    gc.add_argument("--apply", action="store_true", help="delete the listed items (default: dry-run)")
    gc.add_argument(
        "--keep-attempts", type=_keep_attempts, default=KEEP_FAILED_ATTEMPTS, metavar="N",
        help=f"keep the newest N failed attempts per result (default {KEEP_FAILED_ATTEMPTS})",
    )
    gc.add_argument(
        "--staging-only", action="store_true",
        help="only orphaned .staging-* directories and interrupted removals; leave every attempt",
    )
    common(gc)
    gc.set_defaults(handler=gc_command)


def rerun_command(*, keep: int, staging_only: bool) -> str:
    """The exact ``--apply`` invocation to repeat (same scope, ROOT as a placeholder)."""

    parts = ["traceback lab gc --apply"]
    if staging_only:
        parts.append("--staging-only")
    if keep != KEEP_FAILED_ATTEMPTS:
        parts.append(f"--keep-attempts {keep}")
    parts.append("--root <ROOT>")
    return " ".join(parts)


def store_label(store: LabStore, path: Path) -> str:
    """One label form for every item: ``ROOT/lab``-relative, 64-hex ids shortened to ID12."""

    parts = path.relative_to(store.lab).parts
    return "/".join(part[:12] if SHA256_PATTERN.fullmatch(part) else part for part in parts)


def _subdirs(path: Path) -> list[Path]:
    try:
        with os.scandir(path) as entries:
            return sorted(
                (Path(entry.path) for entry in entries if entry.is_dir(follow_symlinks=False)),
                key=lambda item: item.name,
            )
    except OSError:
        return []


def tree_bytes(path: Path) -> int:
    """Bytes held by every non-directory entry under ``path`` (symlinks not followed)."""

    total = 0
    for base, _dirs, files in os.walk(path, followlinks=False):
        for name in files:
            try:
                total += os.lstat(os.path.join(base, name)).st_size
            except OSError:
                continue
    return total


def _protected_attempts(selector: Path) -> set[str] | None:
    """Attempt ids ``selected.json`` names; ``None`` when the selector cannot be trusted.

    A missing selector is "no selection": ``LabStore._selector_bytes`` returns
    ``None`` for it and ``_selected_result`` then reports no selected attempt,
    never a fallback to the newest attempt (store.py, ``_selector_bytes``'s
    ``except OSError: pass``).  So nothing in that result directory is
    selected and the empty set is exact.
    """

    try:
        if selector.is_symlink():
            return None
        data = json.loads(selector.read_bytes())
    except FileNotFoundError:
        return set()
    except (OSError, ValueError, RecursionError):
        return None
    if not isinstance(data, dict):
        return None
    attempt_id = data.get("attempt_id")
    if not isinstance(attempt_id, str) or not re.fullmatch(_ATTEMPT_ID, attempt_id):
        return None  # a selector this code does not understand protects the whole result
    names = {attempt_id, data.get("last_success_attempt_id")}
    return {name for name in names if isinstance(name, str)}


def _attempt_status(path: Path) -> Any:
    """``result.json``'s status, or ``_UNKNOWN`` when it cannot be read."""

    try:
        status = LabStore._attempt_status(path)
    except Exception:  # noqa: BLE001 - unknown never triggers deletion
        return _UNKNOWN
    return _UNKNOWN if status is None else status


def _attempt_order(path: Path) -> Any:
    """The attempt's publication ordinal, or ``_UNKNOWN`` when its index cannot be read."""

    try:
        return LabStore._attempt_order(path)
    except Exception:  # noqa: BLE001 - unknown order never triggers deletion (D35)
        return _UNKNOWN


def scan_attempts(store: LabStore, *, keep: int) -> tuple[list[GcItem], list[dict[str, str]]]:
    """``(stale failed attempts, skipped result directories)``; never selected, never ok."""

    items: list[GcItem] = []
    skipped: list[dict[str, str]] = []
    if not store.results_dir.is_dir():
        return items, skipped
    for input_dir in _subdirs(store.results_dir):
        if not SHA256_PATTERN.fullmatch(input_dir.name):
            continue
        for algorithm_dir in _subdirs(input_dir):
            if not ALGORITHM_ID_PATTERN.fullmatch(algorithm_dir.name):
                continue
            for identity_dir in _subdirs(algorithm_dir):
                if not SHA256_PATTERN.fullmatch(identity_dir.name):
                    continue
                attempts = identity_dir / "attempts"
                if attempts.is_symlink() or not attempts.is_dir():
                    continue
                label = store_label(store, identity_dir)
                protected = _protected_attempts(identity_dir / "selected.json")
                if protected is None:
                    skipped.append({"label": label, "reason": "unreadable selector"})
                    continue
                published = [
                    path for path in _subdirs(attempts)
                    if not path.name.startswith(STAGING_PREFIX) and re.fullmatch(_ATTEMPT_ID, path.name)
                ]
                orders = {path.name: _attempt_order(path) for path in published}
                if any(order is _UNKNOWN for order in orders.values()):
                    skipped.append({"label": label, "reason": "unreadable order"})
                    continue
                statuses = {path.name: _attempt_status(path) for path in published}
                if any(status is _UNKNOWN for status in statuses.values()):
                    skipped.append({"label": label, "reason": "unreadable status"})
                    continue
                ordered = sorted(published, key=lambda path: (orders[path.name], path.name))
                failed = [path for path in ordered if statuses[path.name] == "failed"]
                retained = set(protected)
                if keep:
                    retained.update(path.name for path in failed[-keep:])
                for path in failed:
                    if path.name in retained:
                        continue
                    items.append(GcItem(
                        "attempts", store_label(store, path), path, tree_bytes(path),
                        {
                            "input_id": input_dir.name,
                            "algorithm_id": algorithm_dir.name,
                            "parameters_sha256": identity_dir.name,
                            "attempt_id": path.name,
                            "status": "failed",
                        },
                    ))
    return items, skipped


def scan_staging(store: LabStore) -> tuple[list[GcItem], list[GcItem]]:
    """``(orphaned staging, interrupted removals)`` under ``ROOT/lab``."""

    staging: list[GcItem] = []
    interrupted: list[GcItem] = []
    for path in store.orphaned_staging():
        detail = {"path": path.relative_to(store.lab).as_posix()}
        if path.name.startswith(GC_PREFIX):
            interrupted.append(GcItem("interrupted", store_label(store, path), path, tree_bytes(path), detail))
        else:
            staging.append(GcItem("staging", store_label(store, path), path, tree_bytes(path), detail))
    return staging, interrupted


def plan_gc(store: LabStore, *, keep: int, locked: bool, staging_only: bool = False) -> GcPlan:
    plan = GcPlan(keep_attempts=keep)
    journal = store.cohort_selection_journal_file
    if journal.exists() or journal.is_symlink():
        # The journal marks an in-flight cohort transaction; hold everything
        # until a writer has replayed it (store.writer does that before yield).
        plan.held_reason = HELD_PENDING_COHORT
        plan.attempts_skipped = HELD_PENDING_COHORT
        staging, interrupted = scan_staging(store)
        plan.held_staging, plan.held_interrupted = len(staging), len(interrupted)
        return plan
    if staging_only:
        plan.attempts_skipped = SKIPPED_STAGING_ONLY
    else:
        plan.attempts, plan.skipped_results = scan_attempts(store, keep=keep)
    staging, interrupted = scan_staging(store)
    if not locked and (staging or interrupted):
        active = store.read_active()
        if active is not None and active.active:
            plan.held_reason = HELD_ACTIVE_WRITER
            plan.held_staging, plan.held_interrupted = len(staging), len(interrupted)
            return plan
    plan.staging, plan.interrupted = staging, interrupted
    return plan


def remove_item(store: LabStore, item: GcItem) -> GcOutcome:
    """Delete one planned item; an attempt is renamed aside first (never half-published)."""

    target = item.path
    if item.category == "attempts":
        aside = target.parent / f"{GC_PREFIX}{target.name}"
        try:
            os.rename(target, aside)
        except OSError as exc:
            return GcOutcome(item, False, 0, item.bytes, error=exc)
        target = aside
    error: OSError | None = None
    try:
        shutil.rmtree(target)
    except OSError as exc:
        error = exc
    if target.exists() or target.is_symlink():
        remaining = tree_bytes(target)
        leftover = store_label(store, target) if item.category == "attempts" else None
        return GcOutcome(item, False, max(item.bytes - remaining, 0), remaining, error=error, leftover=leftover)
    return GcOutcome(item, True, item.bytes, 0)


def _human_bytes(count: int) -> str:
    value = float(count)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{count:,} B" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{count:,} B"


def _plan_data(store: LabStore, plan: GcPlan, *, applied: bool) -> dict[str, Any]:
    sums = {name: sum(item.bytes for item in getattr(plan, name)) for name in CATEGORIES}
    return {
        "root": str(store.root),
        "mode": "apply" if applied else "dry-run",
        "applied": applied,
        "keep_attempts": plan.keep_attempts,
        "held_reason": plan.held_reason,
        "attempts": {
            "scanned": plan.attempts_skipped is None,
            "skipped_reason": plan.attempts_skipped,
            "count": len(plan.attempts),
            "bytes": sums["attempts"],
            "items": [item.public() for item in plan.attempts],
        },
        "skipped_results": list(plan.skipped_results),
        "staging": {
            "count": len(plan.staging),
            "bytes": sums["staging"],
            "held": plan.held_staging,
            "items": [item.public() for item in plan.staging],
        },
        "interrupted": {
            "count": len(plan.interrupted),
            "bytes": sums["interrupted"],
            "held": plan.held_interrupted,
            "items": [item.public() for item in plan.interrupted],
        },
        "pon": {"collected": False, "reason": PON_NOT_COLLECTED},
        "total": {"count": len(plan.items), "bytes": plan.total_bytes},
    }


def _outcome_data(outcomes: list[GcOutcome]) -> dict[str, Any]:
    done = [outcome for outcome in outcomes if outcome.removed]
    failed = [outcome for outcome in outcomes if not outcome.removed]
    return {
        "removed": {
            "count": len(done),
            "bytes": sum(outcome.freed_bytes for outcome in done),
            "items": [{"label": outcome.item.label, "bytes": outcome.freed_bytes} for outcome in done],
        },
        "freed_bytes": sum(outcome.freed_bytes for outcome in outcomes),
        "remaining_bytes": sum(outcome.remaining_bytes for outcome in failed),
        "failed": [outcome.public() for outcome in failed],
    }


def _plan_lines(store: LabStore, plan: GcPlan, *, applied: bool, outcomes: list[GcOutcome] | None) -> list[str]:
    """Human report lines after ``ROOT``; every label passes through the scrubber."""

    scrub = Scrubber(root=store.root)
    sums = {name: sum(item.bytes for item in getattr(plan, name)) for name in CATEGORIES}
    by_label = {outcome.item.label: outcome for outcome in outcomes or []}
    incomplete = any(not outcome.removed for outcome in outcomes or [])
    verb = ("attempted" if incomplete else "removed") if applied else "would remove"

    def item_line(item: GcItem) -> str:
        text = f"  {item.label}  {item.bytes:,} B"
        outcome = by_label.get(item.label)
        if outcome is not None and not outcome.removed:
            text += f"  NOT removed ({outcome.remaining_bytes:,} B still held)"
        return scrub.line(text)

    lines = ["MODE  applied" if applied else "MODE  dry-run (nothing deleted; add --apply to delete)"]
    if plan.held_reason is not None:
        lines.append(f"HELD  {plan.held_reason}")
    if plan.attempts_skipped is not None:
        lines.append(f"ATTEMPTS     not scanned: {plan.attempts_skipped}")
    else:
        lines.append(
            f"ATTEMPTS     {len(plan.attempts)} failed attempt(s) beyond the newest "
            f"{plan.keep_attempts} per result, {_human_bytes(sums['attempts'])} {verb}"
        )
    lines.extend(item_line(item) for item in plan.attempts)
    if plan.skipped_results:
        n = len(plan.skipped_results)
        lines.append(f"SKIPPED      {n} result director{'y' if n == 1 else 'ies'} (unreadable selector/order/status)")
        lines.extend(scrub.line(f"  {row['label']}  {row['reason']}") for row in plan.skipped_results)
    held = f"; {plan.held_staging} held: {plan.held_reason}" if plan.held_staging else ""
    lines.append(
        f"STAGING      {len(plan.staging)} orphaned {STAGING_PREFIX}* director"
        f"{'y' if len(plan.staging) == 1 else 'ies'}, {_human_bytes(sums['staging'])} {verb}{held}"
    )
    lines.extend(item_line(item) for item in plan.staging)
    held = f"; {plan.held_interrupted} held: {plan.held_reason}" if plan.held_interrupted else ""
    lines.append(
        f"INTERRUPTED  {len(plan.interrupted)} unfinished removal(s) ({GC_PREFIX}*), "
        f"{_human_bytes(sums['interrupted'])} {verb}{held}"
    )
    lines.extend(item_line(item) for item in plan.interrupted)
    lines.append(f"PONS         not collected: {PON_NOT_COLLECTED}")
    if applied:
        results = _outcome_data(outcomes or [])
        lines.append(
            f"TOTAL        {results['removed']['count']} of {len(plan.items)} item(s) removed, "
            f"{results['freed_bytes']:,} B freed"
        )
    else:
        total = plan.total_bytes
        approx = f" ({_human_bytes(total)})" if total >= 1024 else ""
        lines.append(f"TOTAL        {len(plan.items)} item(s), {total:,} B{approx} {verb}")
    return lines


def _render(store: LabStore, plan: GcPlan, *, applied: bool, outcomes: list[GcOutcome] | None) -> dict[str, Any]:
    data = _plan_data(store, plan, applied=applied)
    if applied:
        results = _outcome_data(outcomes or [])
        data["removed"] = results["removed"]
        data["freed_bytes"] = results["freed_bytes"]
        summary = f"removed {results['removed']['count']} item(s), {_human_bytes(results['removed']['bytes'])}"
    else:
        summary = f"dry-run: {len(plan.items)} item(s), {_human_bytes(plan.total_bytes)} reclaimable; nothing deleted"
    payload = _local_envelope(_result("lab gc", "ok", summary, data=data))
    human = _with_human(payload, [f"ROOT  {store.root}", *_plan_lines(store, plan, applied=applied, outcomes=outcomes)], replace_data=True)
    human["_human"]["bare"] = True
    return human


def _render_failure(
    store: LabStore, plan: GcPlan, outcomes: list[GcOutcome], *, keep: int, staging_only: bool,
) -> tuple[int, dict[str, Any]]:
    """TBX-LAB-027 with the apply report (what was and was not removed) before the problem block."""

    results = _outcome_data(outcomes)
    failed = [outcome for outcome in outcomes if not outcome.removed]
    first = next((outcome.error for outcome in failed if outcome.error is not None), None)
    error_text = f"{type(first).__name__}: {first.strerror or 'unknown'}" if first is not None else "left behind"
    rerun = rerun_command(keep=keep, staging_only=staging_only)
    problem = LabProblem(
        "TBX-LAB-027",
        cause=(
            f"{len(failed)} of {len(outcomes)} item(s) could not be removed ({error_text}); "
            f"{results['removed']['count']} removed, {results['freed_bytes']:,} B freed, "
            f"{results['remaining_bytes']:,} B still held"
        ),
        fix=f"Check ownership and permissions under ROOT/lab, then rerun `{rerun}`; a rerun lists what is left",
        data={
            **_plan_data(store, plan, applied=True),
            **results,
            "rerun": rerun,
            "error": type(first).__name__ if first is not None else None,
            "errno": first.errno if first is not None else None,
        },
    )
    payload = problem_payload("lab gc", problem, root=str(store.root))
    lines = [
        f"ROOT  {store.root}",
        *_plan_lines(store, plan, applied=True, outcomes=outcomes),
        *problem_lines(problem),
    ]
    return problem.exit_code, _with_human(payload, lines, replace_data=True)


def gc_command(args: argparse.Namespace, store: LabStore) -> tuple[int, dict[str, Any]]:
    store.check_version()
    if store.lab.is_symlink() or not store.lab.is_dir():
        raise LabProblem(
            "TBX-LAB-017",
            cause="no Lab store under this ROOT (ROOT/lab is missing)",
            fix="Pass the same --root (or TRACEBACK_ROOT) you ran with; `traceback lab gc` never creates a store",
            data={"store": None},
        )
    keep = int(args.keep_attempts)
    staging_only = bool(args.staging_only)
    if not args.apply:
        plan = plan_gc(store, keep=keep, locked=False, staging_only=staging_only)
        return EXIT_OK, _render(store, plan, applied=False, outcomes=None)

    run_id = new_run_id()
    with store.writer("gc", run_id, step="gc"):
        log = store.run_log(run_id)
        log.append("run_start", command="gc", pid=os.getpid(), keep_attempts=keep, staging_only=staging_only)
        # Plan under the lock: the dry-run view may predate a publish, and the
        # writer has already replayed any pending cohort transaction.
        plan = plan_gc(store, keep=keep, locked=True, staging_only=staging_only)
        outcomes = [remove_item(store, item) for item in plan.items]
        failed = [outcome for outcome in outcomes if not outcome.removed]
        freed = sum(outcome.freed_bytes for outcome in outcomes)
        if failed:
            log.append(
                "run_end", status="blocked", exit_code=3, code="TBX-LAB-027",
                removed=len(outcomes) - len(failed), failed=len(failed), freed_bytes=freed, step="gc",
            )
            return _render_failure(store, plan, outcomes, keep=keep, staging_only=staging_only)
        log.append("run_end", status="ok", exit_code=0, removed=len(outcomes), bytes=freed, step="gc")
    return EXIT_OK, _render(store, plan, applied=True, outcomes=outcomes)


__all__ = [
    "GC_PREFIX", "GcItem", "GcOutcome", "GcPlan", "add_gc_parser", "gc_command", "plan_gc",
    "remove_item", "rerun_command", "scan_attempts", "scan_staging", "store_label", "tree_bytes",
]
