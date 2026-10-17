"""Register and inspect a private donor-derived cfDNA calibration."""

from __future__ import annotations

import argparse
from collections.abc import Callable
from pathlib import Path
from typing import Any

from traceback_runner.cli import _local_envelope, _result, _with_human

from .dd_contract import DonorContract, DonorContractError, inspect_contract, register_contract
from .problems import EXIT_BLOCKED, EXIT_OK
from .store import LabStore, new_run_id


def add_dd_parser(commands: Any, common: Callable[[argparse.ArgumentParser], None]) -> None:
    donor = commands.add_parser("donor", help="register input-scoped transplant chrY calibration")
    actions = donor.add_subparsers(dest="donor_action", required=True)
    add = actions.add_parser("register", help="pin transplant assertions and calibration bytes")
    add.add_argument("--file", type=Path, required=True)
    add.add_argument("--sha256", required=True)
    add.add_argument("--allow-synthetic", action="store_true",
                     help="explicitly reserve this immutable input slot for mechanics-only calibration")
    common(add)
    add.set_defaults(handler=_register)
    inspect = actions.add_parser("inspect", help="verify the input-scoped transplant calibration")
    inspect.add_argument("input_id")
    common(inspect)
    inspect.set_defaults(handler=_inspect)


def _payload(command: str, status: str, summary: str, store: LabStore, data: dict[str, Any]) -> dict[str, Any]:
    return _local_envelope(_result(command, status, summary, data={"root": str(store.root), **data}))


def _public(contract: DonorContract) -> dict[str, Any]:
    return {"input_id": contract.input_id, "source_id": contract.source_id,
            "reference_sha256": contract.reference_sha256,
            "calibration_status": contract.calibration_status,
            "calibration_source": contract.calibration_source,
            "protocol_label": contract.protocol_label,
            "counting_protocol": contract.counting_protocol,
            "female_control_n": contract.female_control_n,
            "male_calibration_n": contract.male_calibration_n,
            "min_depth_x": contract.min_depth_x,
            "female_background_y_per_million": contract.female_background_y_per_million,
            "male_full_donor_y_per_million": contract.male_full_donor_y_per_million,
            "manifest_sha256": contract.manifest_sha256}


def _register(args: argparse.Namespace, store: LabStore) -> tuple[int, dict[str, Any]]:
    store.check_version()
    try:
        with store.writer("donor register", new_run_id(), step="dd-cfdna-chry"):
            contract = register_contract(store, args.file, args.sha256,
                                         allow_synthetic=args.allow_synthetic)
    except DonorContractError as exc:
        payload = _payload("lab donor register", "blocked", "Transplant calibration not registered", store,
                           {"cause": str(exc)})
        return EXIT_BLOCKED, _with_human(payload, [f"BLOCKED {exc}"])
    payload = _payload("lab donor register", "ok", "Transplant calibration pinned", store, _public(contract))
    return EXIT_OK, _with_human(payload, [f"INPUT   {contract.input_id[:12]}",
                                          f"SHA256  {contract.manifest_sha256}"])


def _inspect(args: argparse.Namespace, store: LabStore) -> tuple[int, dict[str, Any]]:
    store.check_version()
    try:
        contract = inspect_contract(store, args.input_id)
    except DonorContractError as exc:
        payload = _payload("lab donor inspect", "blocked", "Transplant calibration unavailable", store,
                           {"cause": str(exc)})
        return EXIT_BLOCKED, _with_human(payload, [f"BLOCKED {exc}"])
    payload = _payload("lab donor inspect", "ok", "Transplant calibration verified", store,
                       _public(contract))
    return EXIT_OK, _with_human(payload, [f"INPUT   {contract.input_id[:12]}",
                                          f"SHA256  {contract.manifest_sha256}"])
