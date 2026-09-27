"""Immutable stage receipt creation and verification."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Mapping

from evidence_inspector.models import canonical_json_bytes


class ReceiptError(RuntimeError):
    """Base class for invalid durable stage evidence."""


class ReceiptCorrupt(ReceiptError):
    """A receipt is missing, malformed, or not canonical."""


class OutputCorrupt(ReceiptError):
    """A receipt output is missing or has a different digest."""


@dataclass(frozen=True)
class OutputRecord:
    role: str
    relative_path: str
    size_bytes: int
    sha256: str


@dataclass(frozen=True)
class StageReceipt:
    job_id: str
    stage: str
    attempt: int
    lease_token: int
    stage_definition_sha256: str
    workflow_release_sha256: str
    input_manifest_sha256: str
    ordered_input_sha256: tuple[str, ...]
    outputs: tuple[OutputRecord, ...]
    metadata: Mapping[str, bool | int | str]
    postconditions: Mapping[str, bool]

    def payload(self) -> dict[str, object]:
        return {
            "schema_version": "traceback.stage-receipt.v1",
            "job_id": self.job_id,
            "stage": self.stage,
            "attempt": self.attempt,
            "lease_token": self.lease_token,
            "stage_definition_sha256": self.stage_definition_sha256,
            "workflow_release_sha256": self.workflow_release_sha256,
            "input_manifest_sha256": self.input_manifest_sha256,
            "ordered_input_sha256": list(self.ordered_input_sha256),
            "outputs": [item.__dict__ for item in self.outputs],
            "metadata": dict(self.metadata),
            "postconditions": dict(self.postconditions),
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.payload())

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()


def _safe_output(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if not value or path.is_absolute() or "." in path.parts or ".." in path.parts:
        raise OutputCorrupt(f"unsafe output locator: {value!r}")
    return path


def hash_outputs(attempt_dir: Path, outputs: Mapping[str, str]) -> tuple[OutputRecord, ...]:
    records: list[OutputRecord] = []
    seen_paths: set[str] = set()
    for role, relative in sorted(outputs.items()):
        locator = _safe_output(relative).as_posix()
        if locator in seen_paths:
            raise OutputCorrupt(f"duplicate output locator: {locator}")
        seen_paths.add(locator)
        path = attempt_dir.joinpath(*PurePosixPath(locator).parts)
        if path.is_symlink() or not path.is_file():
            raise OutputCorrupt(f"output is not a regular file: {locator}")
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
                size += len(chunk)
        records.append(OutputRecord(role, locator, size, digest.hexdigest()))
    if not records:
        raise OutputCorrupt("a successful stage must publish at least one output")
    return tuple(records)


def write_receipt(directory: Path, receipt: StageReceipt) -> Path:
    path = directory / "receipt.json"
    try:
        with path.open("xb") as handle:
            handle.write(receipt.canonical_bytes())
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError as exc:
        raise ReceiptCorrupt("receipt is immutable and already exists") from exc
    path.chmod(0o444)
    return path


def load_receipt(directory: Path) -> StageReceipt:
    path = directory / "receipt.json"
    try:
        raw = path.read_bytes()
        payload = json.loads(raw)
        if raw != canonical_json_bytes(payload):
            raise ReceiptCorrupt("receipt is not canonical")
        if payload.pop("schema_version") != "traceback.stage-receipt.v1":
            raise ReceiptCorrupt("unsupported receipt schema")
        outputs = tuple(OutputRecord(**item) for item in payload.pop("outputs"))
        payload["ordered_input_sha256"] = tuple(payload["ordered_input_sha256"])
        receipt = StageReceipt(outputs=outputs, **payload)
        if not receipt.postconditions or not all(receipt.postconditions.values()):
            raise ReceiptCorrupt("receipt postconditions are incomplete or failed")
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        if isinstance(exc, ReceiptCorrupt):
            raise
        raise ReceiptCorrupt("receipt is malformed") from exc
    return receipt


def verify_receipt(directory: Path) -> StageReceipt:
    receipt = load_receipt(directory)
    for output in receipt.outputs:
        path = directory.joinpath(*_safe_output(output.relative_path).parts)
        if path.is_symlink() or not path.is_file():
            raise OutputCorrupt(f"receipt output is missing: {output.relative_path}")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if path.stat().st_size != output.size_bytes or digest != output.sha256:
            raise OutputCorrupt(f"receipt output digest mismatch: {output.relative_path}")
    return receipt
