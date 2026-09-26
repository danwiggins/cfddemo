"""Bounded, privacy-safe preparation of query lengths from ordered BAM inputs."""

from __future__ import annotations

import os
import hashlib
import shutil
import tempfile
import time
from collections import Counter
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import contextmanager, nullcontext
from pathlib import Path
from typing import Any, ContextManager, Literal

from pydantic import Field, model_validator

from .models import (
    Identifier,
    SampleLinkage,
    SampleLinkageStatus,
    SelectionParameters,
    Sha256,
    StrictModel,
    Trimming,
    TrimmingStatus,
    canonical_json_bytes,
    sha256_bytes,
)

PREPARATION_SCHEMA_VERSION = "prepared-lengths.v1"
PREPARATION_TOOL_VERSION = "1"
LENGTHS_FILE_NAME = "lengths.json"
MANIFEST_FILE_NAME = "manifest.json"
DEFAULT_ARTIFACT_ID = "artifact.prepared-lengths.v1"
MAX_SERIALIZED_ARTIFACT_BYTES = 2_097_152

StopReason = Literal[
    "complete_input_scan",
    "accepted_read_cap",
    "inspected_record_cap",
    "elapsed_time_cap",
    "serialized_artifact_cap",
]
RecordSource = Callable[
    [Path], ContextManager[Iterable[Any]] | Iterable[Any]
]


class PreparationError(ValueError):
    """Raised when preparation cannot publish a complete validated bundle."""


class ParentInputIdentity(StrictModel):
    """Privacy-safe parent identity; intentionally excludes paths and filenames."""

    id: Identifier
    order: int = Field(ge=0)
    size_bytes: int | None = Field(default=None, ge=0)
    mtime_ns: int | None = Field(default=None, ge=0)
    identity_verification: Literal["unverified"] = "unverified"
    hash_scope: Literal["parent_metadata"] = "parent_metadata"


class PreparedLengthArtifact(StrictModel):
    id: Identifier
    file_name: Literal["lengths.json"] = LENGTHS_FILE_NAME
    sha256: Sha256
    hash_scope: Literal["derived_lengths"] = "derived_lengths"
    size_bytes: int = Field(ge=2)
    value_count: int = Field(ge=1)


class PreparationManifest(StrictModel):
    """Model-safe provenance for one immutable derived-length artifact."""

    schema_version: Literal["prepared-lengths.v1"] = PREPARATION_SCHEMA_VERSION
    artifact: PreparedLengthArtifact
    ordered_inputs: tuple[ParentInputIdentity, ...] = Field(min_length=1)
    selection_parameters: SelectionParameters
    sample_linkage: SampleLinkage
    trimming: Trimming
    inspected_count: int = Field(ge=0)
    accepted_count: int = Field(ge=1)
    exclusions: dict[Identifier, int]
    scanned_complete_input: bool
    stop_reason: StopReason
    elapsed_ms: int = Field(ge=0)
    elapsed_overrun_ms: int = Field(ge=0)
    preparation_tool_version: Literal["1"] = PREPARATION_TOOL_VERSION
    measurement_definition: Literal[
        "length of the basecalled BAM query sequence in base pairs"
    ] = "length of the basecalled BAM query sequence in base pairs"
    sample_rule: Literal[
        "deterministic ordered prefix; first eligible primary record per read ID"
    ] = "deterministic ordered prefix; first eligible primary record per read ID"
    subset_nonrepresentative: Literal[True] = True
    partial_collection: bool

    @model_validator(mode="after")
    def validate_manifest(self) -> PreparationManifest:
        if self.accepted_count > self.inspected_count:
            raise ValueError("accepted_count cannot exceed inspected_count")
        if self.artifact.value_count != self.accepted_count:
            raise ValueError("artifact value_count must equal accepted_count")
        if (
            self.artifact.size_bytes
            > self.selection_parameters.max_serialized_artifact_bytes
        ):
            raise ValueError("artifact exceeds max_serialized_artifact_bytes")
        if any(value < 0 for value in self.exclusions.values()):
            raise ValueError("exclusion counts cannot be negative")
        orders = [item.order for item in self.ordered_inputs]
        if orders != list(range(len(orders))):
            raise ValueError("ordered input positions must be contiguous")
        if self.scanned_complete_input != (
            self.stop_reason == "complete_input_scan"
        ):
            raise ValueError("scan completion and stop_reason disagree")
        return self


def _default_sample_linkage() -> SampleLinkage:
    return SampleLinkage(
        status=SampleLinkageStatus.UNVERIFIED,
        evidence_ids=(),
        operator_rationale=(
            "Preparation alone does not establish linkage to a report or specimen."
        ),
    )


def _default_trimming() -> Trimming:
    return Trimming(
        status=TrimmingStatus.UNKNOWN,
        processing_detail=(
            "No trimming state was inferred and no fixed length correction was applied."
        ),
    )


@contextmanager
def _pysam_record_source(path: Path) -> Iterator[Iterable[Any]]:
    """Open one BAM lazily and stream records in file order."""

    try:
        import pysam

        with pysam.AlignmentFile(str(path), "rb", check_sq=False) as alignment:
            yield alignment.fetch(until_eof=True)
    except Exception as exc:
        raise PreparationError(
            f"failed to read registered BAM input ({type(exc).__name__})"
        ) from None


def _as_context_manager(
    source: ContextManager[Iterable[Any]] | Iterable[Any],
) -> ContextManager[Iterable[Any]]:
    if hasattr(source, "__enter__") and hasattr(source, "__exit__"):
        return source  # type: ignore[return-value]
    return nullcontext(source)  # type: ignore[arg-type]


def _ordered_paths(input_paths: Sequence[str | Path]) -> tuple[Path, ...]:
    if not input_paths:
        raise PreparationError("at least one BAM input is required")
    paths = tuple(sorted((Path(item) for item in input_paths), key=lambda p: str(p)))
    normalized = [os.path.abspath(os.fspath(path)) for path in paths]
    if len(set(normalized)) != len(normalized):
        raise PreparationError("BAM inputs must be unique")
    return paths


def _parent_identities(paths: Sequence[Path]) -> tuple[ParentInputIdentity, ...]:
    identities: list[ParentInputIdentity] = []
    for order, path in enumerate(paths):
        try:
            stat = path.stat()
        except OSError:
            stat = None
        identities.append(
            ParentInputIdentity(
                id=f"artifact.parent-bam.{order + 1:04d}",
                order=order,
                size_bytes=None if stat is None else stat.st_size,
                mtime_ns=None if stat is None else stat.st_mtime_ns,
            )
        )
    return tuple(identities)


def _record_length(record: Any) -> int | None:
    """Return sequence length without retaining sequence content."""

    sequence = getattr(record, "query_sequence", None)
    if sequence is None:
        return None
    return len(sequence)


def _serialized_size_after_append(
    current_size: int, accepted_count: int, length: int
) -> int:
    separator_bytes = 0 if accepted_count == 0 else 1
    return current_size + separator_bytes + len(str(length))


def _write_file(path: Path, content: bytes) -> None:
    with path.open("xb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())


def _validate_staged_bundle(bundle_dir: Path) -> PreparationManifest:
    artifact_bytes = (bundle_dir / LENGTHS_FILE_NAME).read_bytes()
    manifest = PreparationManifest.model_validate_json(
        (bundle_dir / MANIFEST_FILE_NAME).read_bytes()
    )
    if len(artifact_bytes) != manifest.artifact.size_bytes:
        raise PreparationError("staged artifact size does not match its manifest")
    if sha256_bytes(artifact_bytes) != manifest.artifact.sha256:
        raise PreparationError("staged artifact digest does not match its manifest")
    try:
        import json

        lengths = json.loads(artifact_bytes)
    except (UnicodeDecodeError, ValueError):
        raise PreparationError("staged artifact is not valid JSON") from None
    if (
        not isinstance(lengths, list)
        or len(lengths) != manifest.accepted_count
        or any(
            isinstance(item, bool) or not isinstance(item, int) or item <= 0
            for item in lengths
        )
    ):
        raise PreparationError("staged artifact has invalid query lengths")
    if artifact_bytes != canonical_json_bytes(lengths):
        raise PreparationError("staged artifact is not canonical compact JSON")
    return manifest


def _publish_bundle(
    output_dir: Path,
    artifact_bytes: bytes,
    manifest_bytes: bytes,
) -> PreparationManifest:
    output_dir = Path(output_dir)
    if output_dir.exists():
        raise PreparationError("output bundle already exists and is immutable")
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    staged = Path(
        tempfile.mkdtemp(prefix=f".{output_dir.name}.", dir=output_dir.parent)
    )
    try:
        _write_file(staged / LENGTHS_FILE_NAME, artifact_bytes)
        _write_file(staged / MANIFEST_FILE_NAME, manifest_bytes)
        manifest = _validate_staged_bundle(staged)
        os.replace(staged, output_dir)
        try:
            parent_fd = os.open(output_dir.parent, os.O_RDONLY)
            try:
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)
        except OSError:
            pass
        return manifest
    except Exception:
        if staged.exists():
            shutil.rmtree(staged)
        raise


def prepare_length_artifact(
    input_paths: Sequence[str | Path],
    output_dir: str | Path,
    *,
    artifact_id: str = DEFAULT_ARTIFACT_ID,
    selection_parameters: SelectionParameters | None = None,
    record_source: RecordSource | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    sample_linkage: SampleLinkage | None = None,
    trimming: Trimming | None = None,
    partial_collection: bool = True,
) -> PreparationManifest:
    """Prepare and atomically publish a bounded immutable query-length bundle.

    The injected ``record_source`` makes cap and exclusion behavior testable
    without reading private BAMs. The default source streams BAM records through
    pysam. Neither read identifiers nor local paths enter the returned manifest.
    """

    parameters = selection_parameters or SelectionParameters(
        ordering_rule="lexicographic ordering of registered local BAM paths"
    )
    if (
        parameters.max_serialized_artifact_bytes
        > MAX_SERIALIZED_ARTIFACT_BYTES
    ):
        raise PreparationError(
            "max_serialized_artifact_bytes cannot exceed the 2 MiB contract cap"
        )
    paths = _ordered_paths(input_paths)
    source_factory = record_source or _pysam_record_source
    lengths: list[int] = []
    accepted_read_hashes: set[bytes] = set()
    exclusions: Counter[str] = Counter()
    inspected_count = 0
    serialized_size = 2  # opening and closing brackets
    start = monotonic()
    stop_reason: StopReason | None = None

    for path in paths:
        try:
            supplied_source = source_factory(path)
            with _as_context_manager(supplied_source) as records:
                iterator = iter(records)
                while True:
                    if len(lengths) >= parameters.max_accepted_reads:
                        stop_reason = "accepted_read_cap"
                        break
                    if inspected_count >= parameters.max_inspected_records:
                        stop_reason = "inspected_record_cap"
                        break
                    if monotonic() - start >= parameters.max_elapsed_seconds:
                        stop_reason = "elapsed_time_cap"
                        break
                    try:
                        record = next(iterator)
                    except StopIteration:
                        break
                    inspected_count += 1

                    if bool(getattr(record, "is_secondary", False)):
                        exclusions["secondary"] += 1
                        continue
                    if bool(getattr(record, "is_supplementary", False)):
                        exclusions["supplementary"] += 1
                        continue

                    read_id = getattr(record, "query_name", None)
                    if not isinstance(read_id, str) or not read_id:
                        exclusions["missing_read_id"] += 1
                        continue
                    read_hash = hashlib.sha256(read_id.encode("utf-8")).digest()
                    if read_hash in accepted_read_hashes:
                        exclusions["duplicate_read_id"] += 1
                        continue

                    length = _record_length(record)
                    if length is None:
                        exclusions["missing_sequence"] += 1
                        continue
                    if length == 0:
                        exclusions["zero_length"] += 1
                        continue
                    if length > parameters.max_read_length_bp:
                        exclusions["oversized_read"] += 1
                        continue

                    prospective_size = _serialized_size_after_append(
                        serialized_size, len(lengths), length
                    )
                    if (
                        prospective_size
                        > parameters.max_serialized_artifact_bytes
                    ):
                        stop_reason = "serialized_artifact_cap"
                        break
                    lengths.append(length)
                    accepted_read_hashes.add(read_hash)
                    serialized_size = prospective_size
                if stop_reason is not None:
                    break
        except PreparationError:
            raise
        except Exception as exc:
            raise PreparationError(
                f"failed to inspect registered BAM input ({type(exc).__name__})"
            ) from None

    elapsed_seconds = max(0.0, monotonic() - start)
    if stop_reason is None:
        stop_reason = "complete_input_scan"
    if not lengths:
        raise PreparationError("preparation produced no eligible query lengths")

    artifact_bytes = canonical_json_bytes(lengths)
    if len(artifact_bytes) > parameters.max_serialized_artifact_bytes:
        raise PreparationError("prepared artifact exceeds its serialized byte cap")
    artifact = PreparedLengthArtifact(
        id=artifact_id,
        sha256=sha256_bytes(artifact_bytes),
        size_bytes=len(artifact_bytes),
        value_count=len(lengths),
    )
    manifest = PreparationManifest(
        artifact=artifact,
        ordered_inputs=_parent_identities(paths),
        selection_parameters=parameters,
        sample_linkage=sample_linkage or _default_sample_linkage(),
        trimming=trimming or _default_trimming(),
        inspected_count=inspected_count,
        accepted_count=len(lengths),
        exclusions=dict(sorted(exclusions.items())),
        scanned_complete_input=stop_reason == "complete_input_scan",
        stop_reason=stop_reason,
        elapsed_ms=round(elapsed_seconds * 1000),
        elapsed_overrun_ms=round(
            max(0.0, elapsed_seconds - parameters.max_elapsed_seconds) * 1000
        ),
        partial_collection=partial_collection,
    )
    return _publish_bundle(
        Path(output_dir),
        artifact_bytes,
        canonical_json_bytes(manifest),
    )


__all__ = [
    "DEFAULT_ARTIFACT_ID",
    "LENGTHS_FILE_NAME",
    "MANIFEST_FILE_NAME",
    "MAX_SERIALIZED_ARTIFACT_BYTES",
    "PREPARATION_SCHEMA_VERSION",
    "PREPARATION_TOOL_VERSION",
    "ParentInputIdentity",
    "PreparationError",
    "PreparationManifest",
    "PreparedLengthArtifact",
    "prepare_length_artifact",
]
