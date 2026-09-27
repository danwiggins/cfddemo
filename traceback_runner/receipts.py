"""Immutable stage receipt envelope creation and verification."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Mapping

from .contracts import ArtifactDigest, ReceiptStatus, StageName, StageReceipt
from .serialization import canonical_json_bytes, canonical_model_from_bytes


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
class ReceiptEnvelope:
    """Local durability data around the shared path-free receipt contract."""

    contract: StageReceipt
    stage_definition_sha256: str
    input_manifest_sha256: str
    output_locators: Mapping[str, str]
    metadata: Mapping[str, bool | int | str]
    postconditions: Mapping[str, bool]

    def payload(self) -> dict[str, object]:
        return {
            "schema_version": "traceback.stage-receipt-envelope.v1",
            "receipt": self.contract.model_dump(mode="json"),
            "stage_definition_sha256": self.stage_definition_sha256,
            "input_manifest_sha256": self.input_manifest_sha256,
            "output_locators": dict(self.output_locators),
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


def _regular_file_under(root: Path, locator: str) -> Path:
    relative = _safe_output(locator)
    path = root.joinpath(*relative.parts)
    try:
        resolved_root = root.resolve(strict=True)
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise OutputCorrupt(f"output is missing: {locator}") from exc
    if not resolved.is_relative_to(resolved_root):
        raise OutputCorrupt(f"output escapes attempt directory: {locator}")
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise OutputCorrupt(f"output path contains a symlink: {locator}")
    if not path.is_file():
        raise OutputCorrupt(f"output is not a regular file: {locator}")
    return path


def hash_outputs(attempt_dir: Path, outputs: Mapping[str, str]) -> tuple[OutputRecord, ...]:
    records: list[OutputRecord] = []
    seen_paths: set[str] = set()
    for role, relative in sorted(outputs.items()):
        locator = _safe_output(relative).as_posix()
        if locator in seen_paths:
            raise OutputCorrupt(f"duplicate output locator: {locator}")
        seen_paths.add(locator)
        path = _regular_file_under(attempt_dir, locator)
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


def build_receipt(
    *,
    job_id: str,
    stage: StageName,
    attempt: int,
    lease_token: int,
    workflow_release_sha256: str,
    stage_definition_sha256: str,
    input_manifest_sha256: str,
    ordered_inputs: tuple[ArtifactDigest, ...],
    outputs: tuple[OutputRecord, ...],
    metadata: Mapping[str, bool | int | str],
    postconditions: Mapping[str, bool],
) -> ReceiptEnvelope:
    contract = StageReceipt(
        job_id=job_id,
        stage=stage,
        attempt=attempt,
        fencing_token=lease_token,
        workflow_release_sha256=workflow_release_sha256,
        ordered_inputs=ordered_inputs,
        outputs=tuple(
            ArtifactDigest(role=item.role, sha256=item.sha256, size_bytes=item.size_bytes)
            for item in outputs
        ),
        status=ReceiptStatus.SUCCEEDED,
    )
    return ReceiptEnvelope(
        contract=contract,
        stage_definition_sha256=stage_definition_sha256,
        input_manifest_sha256=input_manifest_sha256,
        output_locators=MappingProxyType(
            {item.role: item.relative_path for item in outputs}
        ),
        metadata=MappingProxyType(dict(metadata)),
        postconditions=MappingProxyType(dict(postconditions)),
    )


def write_receipt(directory: Path, receipt: ReceiptEnvelope) -> Path:
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


def load_receipt(directory: Path) -> ReceiptEnvelope:
    path = directory / "receipt.json"
    try:
        raw = path.read_bytes()
        payload = json.loads(raw)
        if raw != canonical_json_bytes(payload):
            raise ReceiptCorrupt("receipt is not canonical")
        if payload.pop("schema_version") != "traceback.stage-receipt-envelope.v1":
            raise ReceiptCorrupt("unsupported receipt schema")
        contract = canonical_model_from_bytes(
            StageReceipt, canonical_json_bytes(payload.pop("receipt"))
        )
        receipt = ReceiptEnvelope(
            contract=contract,
            stage_definition_sha256=payload.pop("stage_definition_sha256"),
            input_manifest_sha256=payload.pop("input_manifest_sha256"),
            output_locators=MappingProxyType(dict(payload.pop("output_locators"))),
            metadata=MappingProxyType(dict(payload.pop("metadata"))),
            postconditions=MappingProxyType(dict(payload.pop("postconditions"))),
        )
        if payload:
            raise ReceiptCorrupt("receipt envelope contains unknown fields")
        if not receipt.postconditions or not all(receipt.postconditions.values()):
            raise ReceiptCorrupt("receipt postconditions are incomplete or failed")
        if set(receipt.output_locators) != {item.role for item in contract.outputs}:
            raise ReceiptCorrupt("receipt output locators do not match output roles")
    except (OSError, ValueError, TypeError, KeyError) as exc:
        if isinstance(exc, ReceiptCorrupt):
            raise
        raise ReceiptCorrupt("receipt is malformed") from exc
    return receipt


def verify_receipt(directory: Path) -> ReceiptEnvelope:
    receipt = load_receipt(directory)
    for output in receipt.contract.outputs:
        locator = receipt.output_locators[output.role]
        path = _regular_file_under(directory, locator)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if path.stat().st_size != output.size_bytes or digest != output.sha256:
            raise OutputCorrupt(f"receipt output digest mismatch: {locator}")
    return receipt
