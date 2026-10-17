"""Private, input-scoped chrY fetal-fraction calibration.

The numbers are supplied by an operator from protocol-matched controls and
mixtures. Registration pins assertions and bytes; it does not validate biology.
"""

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

SCHEMA = "traceback.lab.fetal-chry-calibration.v1"
COUNTING_PROTOCOL = "F1-canonical-chrY-nonPAR-starts-per-million-all-eligible.v1"
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_FIELDS = {"schema", "source_id", "input_id", "reference_sha256",
           "pregnancy_confirmed", "maternal_female_attested", "male_fetus_attested",
           "transplant_excluded_attested",
           "calibration_status", "calibration_source", "protocol_label", "counting_protocol",
           "female_control_n", "male_mixture_n", "min_depth_x",
           "female_background_y_per_million", "full_male_y_per_million",
           "validated_min_fraction", "validated_max_fraction"}


class FetalChrYContractError(ValueError):
    """The chrY calibration is absent, changed, or invalid."""


@dataclass(frozen=True)
class FetalChrYContract:
    source_id: str
    input_id: str
    reference_sha256: str
    calibration_status: str
    calibration_source: str
    protocol_label: str
    female_control_n: int
    male_mixture_n: int
    min_depth_x: float
    female_background_y_per_million: float
    full_male_y_per_million: float
    validated_min_fraction: float
    validated_max_fraction: float
    manifest_sha256: str


def _read(path: Path) -> bytes:
    try:
        fd = open_regular_no_follow(path)
        try:
            size = os.fstat(fd).st_size
            if not 1 <= size <= 8192:
                raise FetalChrYContractError("fetal calibration must be 1 to 8192 bytes")
            raw = os.read(fd, 8193)
            if len(raw) != size or os.fstat(fd).st_size != size:
                raise FetalChrYContractError("fetal calibration changed while reading")
            return raw
        finally:
            os.close(fd)
    except OSError as exc:
        raise FetalChrYContractError("fetal calibration is missing or unsafe") from exc


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) else None


def _label(value: object) -> bool:
    return (isinstance(value, str) and 1 <= len(value) <= 160
            and all(ord(char) >= 32 and char not in "/\\" for char in value))


def _unique_fields(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate calibration field")
        result[key] = value
    return result


def _reject_constant(_value: str) -> None:
    raise ValueError("non-standard calibration number")


def _parse(raw: bytes) -> FetalChrYContract:
    try:
        data = json.loads(raw, object_pairs_hook=_unique_fields, parse_constant=_reject_constant)
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise FetalChrYContractError("fetal calibration is invalid JSON") from exc
    if not isinstance(data, dict) or set(data) != _FIELDS or data.get("schema") != SCHEMA:
        raise FetalChrYContractError("fetal calibration has invalid fields")
    if any(not isinstance(data[key], str) or not _SHA.fullmatch(data[key])
           for key in ("source_id", "input_id", "reference_sha256")):
        raise FetalChrYContractError("source, input, and reference need full SHA-256 identifiers")
    if any(data[key] is not True for key in ("pregnancy_confirmed", "maternal_female_attested",
                                           "male_fetus_attested", "transplant_excluded_attested")):
        raise FetalChrYContractError("pregnancy, maternal sex, male fetus, and transplant exclusion require affirmative assertions")
    if data["calibration_status"] not in ("synthetic", "operator_verified"):
        raise FetalChrYContractError("calibration status must be synthetic or operator_verified")
    if not _label(data["calibration_source"]) or not _label(data["protocol_label"]):
        raise FetalChrYContractError("calibration source and protocol labels are required")
    if data["counting_protocol"] != COUNTING_PROTOCOL:
        raise FetalChrYContractError("calibration counting protocol is incompatible")
    if any(not isinstance(data[key], int) or isinstance(data[key], bool) or data[key] < 1
           for key in ("female_control_n", "male_mixture_n")):
        raise FetalChrYContractError("calibration cohort counts must be positive")
    depth = _number(data["min_depth_x"])
    background = _number(data["female_background_y_per_million"])
    full = _number(data["full_male_y_per_million"])
    low = _number(data["validated_min_fraction"])
    high = _number(data["validated_max_fraction"])
    if depth is None or depth <= 0:
        raise FetalChrYContractError("calibrated minimum depth must be positive and finite")
    if background is None or full is None or not 0 <= background < full <= 1_000_000:
        raise FetalChrYContractError("female background and full male response must be ordered rates")
    if low is None or high is None or not 0 <= low < high <= 1:
        raise FetalChrYContractError("validated fetal-fraction bounds must lie within zero and one")
    return FetalChrYContract(data["source_id"], data["input_id"], data["reference_sha256"],
                             data["calibration_status"], data["calibration_source"],
                             data["protocol_label"], data["female_control_n"],
                             data["male_mixture_n"], depth, background, full, low, high,
                             hashlib.sha256(raw).hexdigest())


def _directory(store: LabStore, input_id: str, *, synthetic: bool = False) -> Path:
    if not _SHA.fullmatch(input_id):
        raise FetalChrYContractError("input_id must be a full SHA-256")
    kind = "fetal-fraction-chry-synthetic" if synthetic else "fetal-fraction-chry"
    return store.lab / "assets" / kind / input_id


def inspect_contract(store: LabStore, input_id: str, *, synthetic: bool = False) -> FetalChrYContract:
    directory = _directory(store, input_id, synthetic=synthetic)
    try:
        _check_contained(store, directory)
        contract = _parse(_read(directory / "contract.json"))
        receipt = json.loads(_read(directory / "receipt.json"))
    except (AssetError, ValueError, OSError, RecursionError) as exc:
        raise FetalChrYContractError("fetal calibration registration is missing or unsafe") from exc
    if (contract.input_id != input_id or not isinstance(receipt, dict)
            or set(receipt) != {"schema", "sha256"} or receipt.get("schema") != SCHEMA
            or receipt.get("sha256") != contract.manifest_sha256):
        raise FetalChrYContractError("fetal calibration receipt or input binding changed")
    return contract


def register_contract(store: LabStore, source: Path, expected_sha256: str, *,
                      allow_synthetic: bool = False) -> FetalChrYContract:
    if not _SHA.fullmatch(expected_sha256):
        raise FetalChrYContractError("expected calibration SHA-256 must be lowercase hex")
    try:
        raw = _read(_regular_source(source))
    except (AssetError, OSError) as exc:
        raise FetalChrYContractError("fetal calibration source is unsafe") from exc
    contract = _parse(raw)
    if contract.manifest_sha256 != expected_sha256:
        raise FetalChrYContractError("fetal calibration differs from expected SHA-256")
    if contract.calibration_status == "synthetic" and not allow_synthetic:
        raise FetalChrYContractError("synthetic calibration requires explicit --allow-synthetic opt-in")
    synthetic = contract.calibration_status == "synthetic"
    directory = _directory(store, contract.input_id, synthetic=synthetic)
    try:
        _check_contained(store, directory)
        exists = directory.exists() or directory.is_symlink()
    except (AssetError, OSError) as exc:
        raise FetalChrYContractError("fetal calibration destination is unsafe") from exc
    if exists:
        try:
            current = inspect_contract(store, contract.input_id, synthetic=synthetic)
        except FetalChrYContractError as exc:
            raise FetalChrYContractError(
                "existing immutable calibration is damaged; restore its exact original bytes from backup"
            ) from exc
        if current.manifest_sha256 != contract.manifest_sha256:
            raise FetalChrYContractError(
                "a different calibration is registered and immutable; use a new input"
            )
        return current

    def build(staging: Path) -> None:
        (staging / "contract.json").write_bytes(raw)
        (staging / "receipt.json").write_bytes(canonical_json_bytes(
            {"schema": SCHEMA, "sha256": contract.manifest_sha256}))

    try:
        store.publish_directory(directory.parent, directory.name, build)
    except OSError as exc:
        raise FetalChrYContractError("fetal calibration could not be published") from exc
    return inspect_contract(store, contract.input_id, synthetic=synthetic)
