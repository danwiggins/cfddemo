"""Immutable input-scoped transplant assertions and chrY calibration."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from traceback_runner.serialization import canonical_json_bytes

from .assets import AssetError, _check_contained, _regular_source
from .file_access import open_regular_no_follow

if TYPE_CHECKING:
    from .store import LabStore

SCHEMA = "traceback.lab.dd-cfdna-contract.v1"
COUNTING_PROTOCOL = "D21-canonical-chrY-nonPAR-starts-per-million-all-eligible.v1"
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_FIELDS = {"schema", "source_id", "input_id", "reference_sha256",
           "male_donor_attested", "female_recipient_attested",
           "transplant_attested", "pregnancy_excluded_attested",
           "calibration_status", "calibration_source", "protocol_label", "counting_protocol",
           "female_control_n", "male_calibration_n", "min_depth_x",
           "female_background_y_per_million", "male_full_donor_y_per_million"}


class DonorContractError(ValueError):
    """The registered assertion or calibration is missing or untrustworthy."""


@dataclass(frozen=True)
class DonorContract:
    source_id: str
    input_id: str
    reference_sha256: str
    calibration_status: str
    calibration_source: str
    protocol_label: str
    counting_protocol: str
    female_control_n: int
    male_calibration_n: int
    min_depth_x: float
    female_background_y_per_million: float
    male_full_donor_y_per_million: float
    manifest_sha256: str


def _read(path: Path) -> bytes:
    try:
        fd = open_regular_no_follow(path)
        try:
            size = os.fstat(fd).st_size
            if not 1 <= size <= 8192:
                raise DonorContractError("donor calibration must be 1 to 8192 bytes")
            raw = os.read(fd, 8193)
            if len(raw) != size or os.fstat(fd).st_size != size:
                raise DonorContractError("donor calibration changed while reading")
            return raw
        finally:
            os.close(fd)
    except OSError as exc:
        raise DonorContractError("donor calibration is missing or unsafe") from exc


def _label(value: object) -> bool:
    return (isinstance(value, str) and 1 <= len(value) <= 160
            and all(ord(char) >= 32 and char not in "/\\" for char in value))


def _finite_number(value: object) -> float | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) else None


def _parse(raw: bytes) -> DonorContract:
    try:
        data = json.loads(raw)
    except (UnicodeError, ValueError) as exc:
        raise DonorContractError("donor calibration is invalid JSON") from exc
    if not isinstance(data, dict) or set(data) != _FIELDS or data.get("schema") != SCHEMA:
        raise DonorContractError("donor calibration has invalid fields")
    if any(not isinstance(data[key], str) or not _SHA.fullmatch(data[key])
           for key in ("source_id", "input_id", "reference_sha256")):
        raise DonorContractError("source, input, and reference need full SHA-256 identifiers")
    if any(data[key] is not True for key in ("male_donor_attested", "female_recipient_attested",
                                           "transplant_attested", "pregnancy_excluded_attested")):
        raise DonorContractError("transplant, donor, recipient, and pregnancy assertions are required")
    if data["calibration_status"] not in ("synthetic", "operator_verified"):
        raise DonorContractError("calibration status must be synthetic or operator_verified")
    if not _label(data["calibration_source"]) or not _label(data["protocol_label"]):
        raise DonorContractError("calibration source and protocol labels are required")
    if data["counting_protocol"] != COUNTING_PROTOCOL:
        raise DonorContractError("calibration counting protocol is incompatible")
    if any(not isinstance(data[key], int) or isinstance(data[key], bool) or data[key] < 1
           for key in ("female_control_n", "male_calibration_n")):
        raise DonorContractError("calibration cohort counts must be positive")
    depth = _finite_number(data["min_depth_x"])
    if depth is None or depth <= 0:
        raise DonorContractError("calibrated depth gate must be positive and finite")
    background = _finite_number(data["female_background_y_per_million"])
    male = _finite_number(data["male_full_donor_y_per_million"])
    if (background is None or background < 0 or male is None or male <= background
            or male > 1_000_000):
        raise DonorContractError("female background and male response must be finite ordered rates")
    return DonorContract(data["source_id"], data["input_id"], data["reference_sha256"],
                         data["calibration_status"], data["calibration_source"],
                         data["protocol_label"], COUNTING_PROTOCOL, data["female_control_n"],
                         data["male_calibration_n"], depth, background, male,
                         hashlib.sha256(raw).hexdigest())


def _directory(store: LabStore, input_id: str) -> Path:
    if not _SHA.fullmatch(input_id):
        raise DonorContractError("input_id must be a full SHA-256")
    return store.lab / "assets" / "dd-cfdna-chry" / input_id


def inspect_contract(store: LabStore, input_id: str) -> DonorContract:
    directory = _directory(store, input_id)
    try:
        _check_contained(store, directory)
        contract = _parse(_read(directory / "contract.json"))
        receipt = json.loads(_read(directory / "receipt.json"))
    except (AssetError, ValueError, OSError) as exc:
        raise DonorContractError("donor calibration registration is missing or unsafe") from exc
    if (contract.input_id != input_id or not isinstance(receipt, dict)
            or set(receipt) != {"schema", "sha256"} or receipt.get("schema") != SCHEMA
            or receipt.get("sha256") != contract.manifest_sha256):
        raise DonorContractError("donor calibration receipt or input binding changed")
    return contract


def register_contract(store: LabStore, source: Path, expected_sha256: str) -> DonorContract:
    if not _SHA.fullmatch(expected_sha256):
        raise DonorContractError("expected calibration SHA-256 must be lowercase hex")
    try:
        raw = _read(_regular_source(source))
    except AssetError as exc:
        raise DonorContractError("donor calibration source is unsafe") from exc
    contract = _parse(raw)
    if contract.manifest_sha256 != expected_sha256:
        raise DonorContractError("donor calibration differs from expected SHA-256")
    directory = _directory(store, contract.input_id)
    try:
        _check_contained(store, directory)
    except AssetError as exc:
        raise DonorContractError("donor calibration destination is unsafe") from exc
    if directory.exists() or directory.is_symlink():
        current = inspect_contract(store, contract.input_id)
        if current.manifest_sha256 != contract.manifest_sha256:
            raise DonorContractError("a different calibration is already registered for this input")
        return current

    def build(staging: Path) -> None:
        (staging / "contract.json").write_bytes(raw)
        (staging / "receipt.json").write_bytes(canonical_json_bytes(
            {"schema": SCHEMA, "sha256": contract.manifest_sha256}))

    try:
        store.publish_directory(directory.parent, directory.name, build)
    except OSError as exc:
        raise DonorContractError("donor calibration could not be published") from exc
    return inspect_contract(store, contract.input_id)
