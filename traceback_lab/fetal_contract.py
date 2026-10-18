"""Immutable, input-scoped prenatal assertion and external NIPTeR control pin.

The control RDS can contain individual sample data.  It stays at its
operator-owned path; the Lab stores only a private path, exact digest, and
minimal protocol assertions.  Registration cannot establish that the control
group is biologically matched or euploid; that remains operator attestation.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from traceback_runner.serialization import canonical_json_bytes

from .assets import AssetError, _check_contained, _regular_source
from .file_access import open_regular_no_follow
from .registration_json import load_registration_json

if TYPE_CHECKING:
    from .store import LabStore

SCHEMA = "traceback.lab.fetal-contract.v1"
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_FIELDS = {"schema", "source_id", "input_id", "reference_sha256", "pregnancy_confirmed",
           "fetal_sex", "control_path", "control_sha256", "control_n", "protocol_label",
           "control_euploid_attested", "control_protocol_matched_attested", "nipter_version"}


class FetalContractError(ValueError):
    """A prenatal assertion or control group is absent, altered, or invalid."""


@dataclass(frozen=True)
class FetalContract:
    source_id: str
    input_id: str
    reference_sha256: str
    fetal_sex: str
    control_path: Path
    control_sha256: str
    control_n: int
    protocol_label: str
    nipter_version: str
    manifest_sha256: str


def _read_small(path: Path) -> bytes:
    try:
        descriptor = open_regular_no_follow(path)
        try:
            size = os.fstat(descriptor).st_size
            if not 1 <= size <= 8192:
                raise FetalContractError("prenatal contract must be 1 to 8192 bytes")
            raw = os.read(descriptor, 8193)
            if len(raw) != size or os.fstat(descriptor).st_size != size:
                raise FetalContractError("prenatal contract changed while reading")
            return raw
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise FetalContractError("prenatal contract is missing or unsafe") from exc


def _hash_control(path: Path) -> str:
    try:
        safe = _regular_source(path)
        descriptor = open_regular_no_follow(safe)
        try:
            before = os.fstat(descriptor)
            if before.st_size <= 0 or before.st_size > (2 << 30):
                raise FetalContractError("NIPTeR control RDS must be 1 byte to 2 GiB")
            digest = hashlib.sha256()
            while block := os.read(descriptor, 1 << 20):
                digest.update(block)
            after = os.fstat(descriptor)
            if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
                after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns
            ):
                raise FetalContractError("NIPTeR control RDS changed while hashing")
            return digest.hexdigest()
        finally:
            os.close(descriptor)
    except (OSError, AssetError) as exc:
        raise FetalContractError("NIPTeR control RDS is missing or unsafe") from exc


def _parse(raw: bytes) -> FetalContract:
    try:
        data = load_registration_json(raw)
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise FetalContractError("prenatal contract is invalid JSON") from exc
    if not isinstance(data, dict) or set(data) != _FIELDS or data.get("schema") != SCHEMA:
        raise FetalContractError("prenatal contract has invalid fields")
    if any(not isinstance(data[key], str) or not _SHA.fullmatch(data[key])
           for key in ("source_id", "input_id", "reference_sha256", "control_sha256")):
        raise FetalContractError("prenatal contract requires full SHA-256 identifiers")
    if (data["pregnancy_confirmed"] is not True or data["control_euploid_attested"] is not True
            or data["control_protocol_matched_attested"] is not True):
        raise FetalContractError("pregnancy, euploid control, and protocol match require affirmative attestations")
    if data["fetal_sex"] not in ("male", "female", "unknown"):
        raise FetalContractError("fetal sex must be male, female, or unknown")
    if (not isinstance(data["control_n"], int) or isinstance(data["control_n"], bool)
            or data["control_n"] < 10 or data["control_n"] > 10000):
        raise FetalContractError("NIPTeR control group needs at least ten attested samples")
    label = data["protocol_label"]
    if (not isinstance(label, str) or not 1 <= len(label) <= 160
            or any(ord(char) < 32 or char in "/\\" for char in label)):
        raise FetalContractError("protocol label is invalid")
    if data["nipter_version"] != "1.0.2":
        raise FetalContractError("this adapter is pinned to NIPTeR 1.0.2")
    path = data["control_path"]
    if not isinstance(path, str) or not Path(path).is_absolute():
        raise FetalContractError("control_path must be an absolute operator-owned path")
    return FetalContract(data["source_id"], data["input_id"], data["reference_sha256"],
                         data["fetal_sex"], Path(path), data["control_sha256"],
                         data["control_n"], label, data["nipter_version"],
                         hashlib.sha256(raw).hexdigest())


def _directory(store: LabStore, input_id: str) -> Path:
    if not _SHA.fullmatch(input_id):
        raise FetalContractError("input_id must be a full SHA-256")
    return store.lab / "assets" / "fetal-fraction" / input_id


def inspect_contract(store: LabStore, input_id: str) -> FetalContract:
    directory = _directory(store, input_id)
    try:
        _check_contained(store, directory)
        contract = _parse(_read_small(directory / "contract.json"))
        receipt = json.loads(_read_small(directory / "receipt.json"))
    except (AssetError, ValueError, OSError) as exc:
        raise FetalContractError("prenatal registration is missing or unsafe") from exc
    if (contract.input_id != input_id or not isinstance(receipt, dict)
            or set(receipt) != {"schema", "sha256"} or receipt.get("schema") != SCHEMA
            or receipt.get("sha256") != contract.manifest_sha256):
        raise FetalContractError("prenatal registration receipt or input binding changed")
    if _hash_control(contract.control_path) != contract.control_sha256:
        raise FetalContractError("NIPTeR control RDS differs from registered SHA-256")
    return contract


def register_contract(store: LabStore, source: Path, expected_sha256: str) -> FetalContract:
    """Publish under the Lab writer lock; an input's assertion is immutable."""

    if not _SHA.fullmatch(expected_sha256):
        raise FetalContractError("expected contract SHA-256 must be lowercase hex")
    try:
        raw = _read_small(_regular_source(source))
    except AssetError as exc:
        raise FetalContractError("prenatal contract source is unsafe") from exc
    contract = _parse(raw)
    if contract.manifest_sha256 != expected_sha256:
        raise FetalContractError("prenatal contract bytes differ from expected SHA-256")
    if _hash_control(contract.control_path) != contract.control_sha256:
        raise FetalContractError("NIPTeR control RDS differs from registered SHA-256")
    directory = _directory(store, contract.input_id)
    try:
        _check_contained(store, directory)
    except AssetError as exc:
        raise FetalContractError("prenatal destination is unsafe") from exc
    if directory.exists() or directory.is_symlink():
        current = inspect_contract(store, contract.input_id)
        if current.manifest_sha256 != contract.manifest_sha256:
            raise FetalContractError("a different prenatal contract is already registered for this input")
        return current

    # Validate before publishing: a wrong source or reference must not reserve
    # this input's immutable prenatal slot.
    from .ingest import input_manifest

    try:
        manifest = input_manifest(store, contract.input_id)
    except OSError as exc:
        raise FetalContractError("stored input manifest is unreadable") from exc
    if not isinstance(manifest, dict) or manifest.get("input_id") != contract.input_id:
        raise FetalContractError("stored input manifest is missing or mismatched")
    if (manifest.get("source_id") != contract.source_id
            or manifest.get("reference_sha256") != contract.reference_sha256):
        raise FetalContractError("prenatal contract does not match stored input provenance")

    def build(staging: Path) -> None:
        (staging / "contract.json").write_bytes(raw)
        (staging / "receipt.json").write_bytes(canonical_json_bytes(
            {"schema": SCHEMA, "sha256": contract.manifest_sha256}))

    store.publish_directory(directory.parent, directory.name, build)
    return inspect_contract(store, contract.input_id)
