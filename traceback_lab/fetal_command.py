"""Register and inspect a private, input-scoped NIPTeR control contract."""

from __future__ import annotations

import argparse
from collections.abc import Callable
from pathlib import Path
from typing import Any

from traceback_runner.cli import _local_envelope, _result, _with_human

from .fetal_contract import FetalContractError, inspect_contract, register_contract
from .problems import EXIT_BLOCKED, EXIT_OK
from .store import LabStore, new_run_id


def add_fetal_parser(commands: Any, common: Callable[[argparse.ArgumentParser], None]) -> None:
    fetal = commands.add_parser("fetal", help="register an input-scoped prenatal NIPTeR control contract")
    actions = fetal.add_subparsers(dest="fetal_action", required=True)
    add = actions.add_parser("register", help="pin an attested prenatal control contract")
    add.add_argument("--file", type=Path, required=True)
    add.add_argument("--sha256", required=True, help="expected exact-byte contract SHA-256")
    common(add)
    add.set_defaults(handler=_register)
    inspect = actions.add_parser("inspect", help="rehash a prenatal contract and control RDS")
    inspect.add_argument("input_id", help="full aligned input SHA-256")
    common(inspect)
    inspect.set_defaults(handler=_inspect)


def _payload(command: str, status: str, summary: str, store: LabStore, data: dict[str, Any]) -> dict[str, Any]:
    return _local_envelope(_result(command, status, summary, data={"root": str(store.root), **data}))


def _public(contract: Any) -> dict[str, Any]:
    # Neither the control file path nor sample names belong in CLI output.
    return {"input_id": contract.input_id, "source_id": contract.source_id,
            "reference_sha256": contract.reference_sha256,
            "control_sha256": contract.control_sha256, "control_n": contract.control_n,
            "nipter_version": contract.nipter_version,
            "manifest_sha256": contract.manifest_sha256}


def _register(args: argparse.Namespace, store: LabStore) -> tuple[int, dict[str, Any]]:
    store.check_version()
    try:
        with store.writer("fetal register", new_run_id(), step="fetal-fraction"):
            contract = register_contract(store, args.file, args.sha256)
    except FetalContractError as exc:
        payload = _payload("lab fetal register", "blocked", "Prenatal contract not registered", store,
                           {"cause": str(exc)})
        return EXIT_BLOCKED, _with_human(payload, [f"BLOCKED {exc}"])
    payload = _payload("lab fetal register", "ok", "Prenatal contract pinned", store, _public(contract))
    return EXIT_OK, _with_human(payload, [f"INPUT   {contract.input_id[:12]}",
                                          f"SHA256  {contract.manifest_sha256}"])


def _inspect(args: argparse.Namespace, store: LabStore) -> tuple[int, dict[str, Any]]:
    store.check_version()
    try:
        contract = inspect_contract(store, args.input_id)
    except FetalContractError as exc:
        payload = _payload("lab fetal inspect", "blocked", "Prenatal contract unavailable", store,
                           {"cause": str(exc)})
        return EXIT_BLOCKED, _with_human(payload, [f"BLOCKED {exc}"])
    payload = _payload("lab fetal inspect", "ok", "Prenatal contract verified", store, _public(contract))
    return EXIT_OK, _with_human(payload, [f"INPUT   {contract.input_id[:12]}",
                                          f"SHA256  {contract.manifest_sha256}"])
