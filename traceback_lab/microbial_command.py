"""Operator registration for an exact external Kraken2/Bracken database."""

from __future__ import annotations

import argparse
from collections.abc import Callable
from pathlib import Path
from typing import Any

from traceback_runner.cli import _local_envelope, _result, _with_human

from .microbial_db import MicrobialDbError, inspect_database, register_database
from .problems import EXIT_BLOCKED, EXIT_OK
from .store import LabStore, new_run_id


def add_microbial_parser(commands: Any, common: Callable[[argparse.ArgumentParser], None]) -> None:
    microbial = commands.add_parser("microbial", help="register a pinned Kraken2/Bracken database")
    actions = microbial.add_subparsers(dest="microbial_action", required=True)
    database = actions.add_parser("db", help="register or inspect the microbial database")
    db_actions = database.add_subparsers(dest="db_action", required=True)
    add = db_actions.add_parser("add", help="pin exact local Kraken2 and Bracken files")
    add.add_argument("--directory", type=Path, required=True)
    add.add_argument("--read-length", type=int, required=True, help="Bracken distribution read length, 75–500")
    add.add_argument("--source-label", required=True, help="public provenance label, never a path")
    common(add)
    add.set_defaults(handler=_add)
    inspect = db_actions.add_parser("inspect", help="verify all registered database bytes")
    common(inspect)
    inspect.set_defaults(handler=_inspect)


def _payload(command: str, status: str, summary: str, store: LabStore, **data: Any) -> dict[str, Any]:
    return _local_envelope(_result(command, status, summary, data={"root": str(store.root), **data}))


def _add(args: argparse.Namespace, store: LabStore) -> tuple[int, dict[str, Any]]:
    try:
        with store.writer("microbial db add", new_run_id(), step="database"):
            database = register_database(store, args.directory, args.read_length, args.source_label)
            store.bump_generation()
    except MicrobialDbError as exc:
        payload = _payload("lab microbial db add", "blocked", "Microbial database not registered", store,
                           cause=str(exc))
        return EXIT_BLOCKED, _with_human(payload, [f"ROOT  {store.root}", f"BLOCKED  {exc}"])
    payload = _payload("lab microbial db add", "ok", "Kraken2/Bracken database pinned", store,
                       sha256=database.sha256, read_length=database.read_length,
                       source_label=database.source_label)
    return EXIT_OK, _with_human(payload, [f"ROOT  {store.root}", f"DATABASE  {database.sha256}"])


def _inspect(args: argparse.Namespace, store: LabStore) -> tuple[int, dict[str, Any]]:
    try:
        database = inspect_database(store)
    except MicrobialDbError as exc:
        payload = _payload("lab microbial db inspect", "blocked", "Microbial database unavailable", store,
                           cause=str(exc))
        return EXIT_BLOCKED, _with_human(payload, [f"ROOT  {store.root}", f"BLOCKED  {exc}"])
    payload = _payload("lab microbial db inspect", "ok", "Kraken2/Bracken database verified", store,
                       sha256=database.sha256, read_length=database.read_length,
                       source_label=database.source_label)
    return EXIT_OK, _with_human(payload, [f"ROOT  {store.root}", f"DATABASE  {database.sha256}"])
