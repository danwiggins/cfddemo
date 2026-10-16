"""CLI hooks for ``traceback lab cohort add/build`` (registered by S5)."""

from __future__ import annotations

import argparse
import re
from collections.abc import Callable
from typing import Any

from traceback_runner.cli import _local_envelope, _result, _with_human

from .cohorts import add_sample, build_cohort
from .problems import EXIT_OK
from .store import LabStore


def _cohort_id(value: str) -> str:
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,39}", value):
        raise argparse.ArgumentTypeError("cohort id must be 1–40 lowercase letters, digits or hyphens, beginning with a letter")
    return value


def add_cohort_parser(commands: Any, common: Callable[[argparse.ArgumentParser], None]) -> None:
    """S5 calls this once from ``cli.build_parser``; no shared-file edit here."""

    cohort = commands.add_parser("cohort", help="manage Lab cohort members and generations")
    actions = cohort.add_subparsers(dest="cohort_action", required=True)
    add = actions.add_parser("add", help="add one stored full-input sample")
    add.add_argument("sample", metavar="ID12|latest")
    add.add_argument("--cohort", required=True, type=_cohort_id)
    common(add)
    add.set_defaults(handler=_add)
    build = actions.add_parser("build", help="publish an immutable cohort generation")
    build.add_argument("--cohort", required=True, type=_cohort_id)
    build.add_argument("--kind", choices=("internal_cohort", "provisional_external"), default="internal_cohort")
    common(build)
    build.set_defaults(handler=_build)


def _add(args: argparse.Namespace, store: LabStore) -> tuple[int, dict[str, Any]]:
    added = add_sample(store, args.cohort, args.sample)
    summary = "sample added" if added else "sample already a member"
    payload = _local_envelope(_result("lab cohort add", "ok", summary, data={
        "root": str(store.root), "cohort": args.cohort, "added": added,
    }))
    return EXIT_OK, _with_human(payload, [f"ROOT  {store.root}", f"COHORT  {args.cohort}", summary], replace_data=True)


def _build(args: argparse.Namespace, store: LabStore) -> tuple[int, dict[str, Any]]:
    generation = build_cohort(store, args.cohort, kind=args.kind)
    counts: dict[str, int] = {}
    for member in generation.members:
        counts[member.aligner_identity] = counts.get(member.aligner_identity, 0) + 1
    comparability_note = (
        "Pre-aligned provenance is unknown; each input is isolated for cohort comparison."
        if any(name.startswith("pre-aligned; provenance unknown") for name in counts) else None
    )
    payload = _local_envelope(_result("lab cohort build", "ok", "cohort generation published", data={
        "root": str(store.root), "cohort": args.cohort, "cohort_version": generation.version_id,
        "kind": generation.kind, "member_n": generation.percentile_n,
        "pon_n": generation.pon_n, "aligner_counts": counts,
        "comparability_note": comparability_note,
    }))
    lines = [f"ROOT  {store.root}", f"COHORT  {generation.version_id}",
             f"KIND    {generation.kind}", f"MEMBERS {generation.percentile_n} (PoN n={generation.pon_n})"]
    lines.extend(f"ALIGNER {name}: {n}" for name, n in sorted(counts.items()))
    if comparability_note is not None:
        lines.append(f"NOTE    {comparability_note}")
    return EXIT_OK, _with_human(payload, lines, replace_data=True)


__all__ = ["add_cohort_parser"]
