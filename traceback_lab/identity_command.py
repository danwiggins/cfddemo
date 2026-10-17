"""Explicit local Somalier site registration and two-sample comparison CLI."""

from __future__ import annotations

import argparse
from collections.abc import Callable
from pathlib import Path
from typing import Any

from traceback_runner.cli import _local_envelope, _result, _with_human

from .identity_pair import IdentityPairError, compare_pair
from .identity_sites import IdentitySitesError, inspect_sites, register_sites
from .problems import EXIT_BLOCKED, EXIT_OK
from .store import LabStore, new_run_id


def add_identity_parser(commands: Any, common: Callable[[argparse.ArgumentParser], None]) -> None:
    identity = commands.add_parser("identity", help="prepare fingerprints and compare two stored samples")
    actions = identity.add_subparsers(dest="identity_action", required=True)
    sites = actions.add_parser("sites", help="register or inspect Somalier hg38 sites")
    site_actions = sites.add_subparsers(dest="sites_action", required=True)
    add = site_actions.add_parser("add", help="register exact local gzip VCF bytes")
    add.add_argument("--file", type=Path, required=True)
    add.add_argument("--sha256", required=True, help="expected SHA-256 of the local sites file")
    add.add_argument("--source-label", required=True, help="public provenance label, never a path")
    common(add)
    add.set_defaults(handler=_sites_add)
    inspect = site_actions.add_parser("inspect", help="verify registered sites bytes")
    common(inspect)
    inspect.set_defaults(handler=_sites_inspect)
    compare = actions.add_parser("compare", help="compare two explicitly chosen stored samples")
    compare.add_argument("sample_a", metavar="SAMPLE_A")
    compare.add_argument("sample_b", metavar="SAMPLE_B")
    compare.add_argument("--force", action="store_true", help="publish another immutable pair attempt")
    common(compare)
    compare.set_defaults(handler=_compare)


def _payload(command: str, status: str, summary: str, store: LabStore, **data: Any) -> dict[str, Any]:
    return _local_envelope(_result(command, status, summary, data={"root": str(store.root), **data}))


def _sites_add(args: argparse.Namespace, store: LabStore) -> tuple[int, dict[str, Any]]:
    try:
        with store.writer("identity sites add", new_run_id(), step="sites"):
            sites = register_sites(store, args.file, args.sha256, args.source_label)
            store.bump_generation()
    except IdentitySitesError as exc:
        payload = _payload("lab identity sites add", "blocked", "Identity sites not registered", store, cause=str(exc))
        return EXIT_BLOCKED, _with_human(payload, [f"ROOT  {store.root}", f"BLOCKED  {exc}"])
    payload = _payload("lab identity sites add", "ok", "Pinned Somalier sites registered", store,
                       sha256=sites.sha256, source_label=sites.source_label)
    return EXIT_OK, _with_human(payload, [f"ROOT  {store.root}", f"SITES  {sites.sha256}"])


def _sites_inspect(args: argparse.Namespace, store: LabStore) -> tuple[int, dict[str, Any]]:
    try:
        sites = inspect_sites(store)
    except IdentitySitesError as exc:
        payload = _payload("lab identity sites inspect", "blocked", "Identity sites unavailable", store, cause=str(exc))
        return EXIT_BLOCKED, _with_human(payload, [f"ROOT  {store.root}", f"BLOCKED  {exc}"])
    payload = _payload("lab identity sites inspect", "ok", "Pinned Somalier sites verified", store,
                       sha256=sites.sha256, source_label=sites.source_label)
    return EXIT_OK, _with_human(payload, [f"ROOT  {store.root}", f"SITES  {sites.sha256}"])


def _compare(args: argparse.Namespace, store: LabStore) -> tuple[int, dict[str, Any]]:
    run_id = new_run_id()
    with store.writer("identity compare", run_id, step="relate"):
        log = store.run_log(run_id)
        log.append("run_start", command="identity compare")
        log.append("step_start", step="relate")
        try:
            pair = compare_pair(store, args.sample_a, args.sample_b, force=args.force)
        except IdentityPairError as exc:
            log.append("step_end", step="relate", status="failed")
            log.append("run_end", status="failed", exit_code=EXIT_BLOCKED)
            payload = _payload("lab identity compare", "blocked", "Samples were not compared", store,
                               run_id=run_id, cause=str(exc))
            return EXIT_BLOCKED, _with_human(payload, [f"ROOT  {store.root}", f"BLOCKED  {exc}"])
        for participant in pair["participants"]:
            log.append("sample", source_id=participant["source_id"], input_id=participant["input_id"])
        log.append("step_end", step="relate", status="ok", cached=pair["cached"],
                   pair_id=pair["pair_id"], execution_id=pair["execution_id"])
        log.append("run_end", status="ok", exit_code=EXIT_OK)
    payload = _payload("lab identity compare", "ok", "Exploratory Somalier pair measured", store,
                       run_id=run_id, pair=pair)
    metrics = pair["metrics"]
    lines = [f"ROOT  {store.root}", f"PAIR  {pair['pair_id'][:12]}",
             f"RELATEDNESS  {metrics['relatedness']:.3f}",
             "FLAG: PRIVACY — this comparison can reveal family relationships.",
             "Research use only; no ONT duplicate or kinship cutoff is claimed."]
    return EXIT_OK, _with_human(payload, lines, replace_data=True)
