"""Deterministic development-only report bundles.

This module packages already validated aggregate artifacts.  It does not plot,
interpret biology, probe a runtime, authorize release, or accept real-data
locators.  The v1 contract is deliberately pinned to the exploratory
whole-chromosome dosage QC method.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import tempfile
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .serialization import canonical_json_bytes, canonical_model_from_bytes, sha256_bytes

REPORT_BUNDLE_SCHEMA = "traceback.development-report-bundle.v1"
RESULT_SCHEMA = "copy-number-dosage-qc.v2"
PLOT_DATA_SCHEMA = "traceback.copy-number-dosage-plot-data.v1"
PLOT_SPEC_SCHEMA = "traceback.copy-number-dosage-plot-spec.v1"
PROVENANCE_SCHEMA = "traceback.copy-number-dosage-report-provenance.v1"
METHOD_ID = "sample-internal-whole-chromosome-dosage-qc"
QUALIFICATION_STATUS = "development_unqualified"

RESULT_PATH = "result.json"
PLOT_DATA_PATH = "plot-data.json"
PLOT_SPEC_PATH = "plot-spec.json"
PROVENANCE_PATH = "provenance.json"
ACCESSIBLE_TABLE_PATH = "accessible-table.tsv"
MANIFEST_PATH = "manifest.json"

_ARTIFACT_PATHS = tuple(
    sorted(
        (
            RESULT_PATH,
            PLOT_DATA_PATH,
            PLOT_SPEC_PATH,
            PROVENANCE_PATH,
            ACCESSIBLE_TABLE_PATH,
        )
    )
)
_ALL_PATHS = frozenset((*_ARTIFACT_PATHS, MANIFEST_PATH))
_JSON_SCHEMAS = {
    RESULT_PATH: RESULT_SCHEMA,
    PLOT_DATA_PATH: PLOT_DATA_SCHEMA,
    PLOT_SPEC_PATH: PLOT_SPEC_SCHEMA,
    PROVENANCE_PATH: PROVENANCE_SCHEMA,
}
_MAX_FILE_BYTES = {
    RESULT_PATH: 32 * 1024 * 1024,
    PLOT_DATA_PATH: 16 * 1024 * 1024,
    PLOT_SPEC_PATH: 1024 * 1024,
    PROVENANCE_PATH: 4 * 1024 * 1024,
    ACCESSIBLE_TABLE_PATH: 32 * 1024 * 1024,
    MANIFEST_PATH: 128 * 1024,
}
_MAX_TOTAL_BYTES = 86 * 1024 * 1024


class ReportBundleError(ValueError):
    """Base class for deterministic report-bundle failures."""


class ReportBundleFormatError(ReportBundleError):
    """An artifact violates the closed v1 format or identity."""


class ReportBundleFilesystemError(ReportBundleError):
    """The bundle filesystem shape is unsafe or incomplete."""


class ReportBundleIntegrityError(ReportBundleError):
    """Artifact bytes disagree with the manifest commitments."""


class _ClosedModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
        validate_default=True,
        allow_inf_nan=False,
    )


class ReportArtifact(_ClosedModel):
    relative_path: Literal[
        "accessible-table.tsv",
        "plot-data.json",
        "plot-spec.json",
        "provenance.json",
        "result.json",
    ]
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0)


class ResultBindings(_ClosedModel):
    result_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    plot_data_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    plot_spec_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    provenance_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    accessible_table_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class DevelopmentReportManifest(_ClosedModel):
    schema_version: Literal["traceback.development-report-bundle.v1"] = (
        REPORT_BUNDLE_SCHEMA
    )
    method_id: Literal["sample-internal-whole-chromosome-dosage-qc"] = METHOD_ID
    qualification_status: Literal["development_unqualified"] = (
        QUALIFICATION_STATUS
    )
    product_release_authorized: Literal[False] = False
    report_id: str = Field(pattern=r"^development-report-[0-9a-f]{24}$")
    artifacts: tuple[ReportArtifact, ...] = Field(min_length=5, max_length=5)
    bindings: ResultBindings

    @model_validator(mode="after")
    def exact_inventory_and_bindings(self) -> DevelopmentReportManifest:
        paths = tuple(item.relative_path for item in self.artifacts)
        if paths != _ARTIFACT_PATHS:
            raise ValueError("report artifacts must be the exact sorted v1 inventory")
        digests = {item.relative_path: item.sha256 for item in self.artifacts}
        expected_bindings = ResultBindings(
            result_sha256=digests[RESULT_PATH],
            plot_data_sha256=digests[PLOT_DATA_PATH],
            plot_spec_sha256=digests[PLOT_SPEC_PATH],
            provenance_sha256=digests[PROVENANCE_PATH],
            accessible_table_sha256=digests[ACCESSIBLE_TABLE_PATH],
        )
        if self.bindings != expected_bindings:
            raise ValueError("result bindings do not match the artifact inventory")
        expected_id = f"development-report-{sha256_bytes(canonical_json_bytes(expected_bindings))[:24]}"
        if self.report_id != expected_id:
            raise ValueError("report_id does not match the exact artifact bindings")
        return self


class VerifiedDevelopmentReportBundle(_ClosedModel):
    path: str
    manifest: DevelopmentReportManifest
    result_bytes: bytes
    plot_data_bytes: bytes
    plot_spec_bytes: bytes
    provenance_bytes: bytes
    accessible_table_bytes: bytes


def _load_canonical_json(content: bytes, *, path: str) -> dict[str, Any]:
    def reject_constant(token: str) -> None:
        raise ValueError(f"non-finite JSON token: {token}")

    try:
        value = json.loads(content, parse_constant=reject_constant)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ReportBundleFormatError(f"{path} is not valid canonical JSON") from exc
    if not isinstance(value, dict) or canonical_json_bytes(value) != content:
        raise ReportBundleFormatError(f"{path} is not canonical JSON")
    return value


def _require_identity(content: bytes, *, path: str) -> None:
    value = _load_canonical_json(content, path=path)
    if value.get("schema_version") != _JSON_SCHEMAS[path]:
        raise ReportBundleFormatError(f"{path} has an unknown schema identity")
    identity = value.get("identity") if path == RESULT_PATH else value
    if not isinstance(identity, dict) or identity.get("method_id") != METHOD_ID:
        raise ReportBundleFormatError(f"{path} has the wrong method identity")
    if identity.get("qualification_status") != QUALIFICATION_STATUS:
        raise ReportBundleFormatError(f"{path} is not development-unqualified")


def _manifest_for(content: dict[str, bytes]) -> DevelopmentReportManifest:
    artifacts = tuple(
        ReportArtifact(
            relative_path=path,
            sha256=sha256_bytes(content[path]),
            size_bytes=len(content[path]),
        )
        for path in _ARTIFACT_PATHS
    )
    digests = {item.relative_path: item.sha256 for item in artifacts}
    bindings = ResultBindings(
        result_sha256=digests[RESULT_PATH],
        plot_data_sha256=digests[PLOT_DATA_PATH],
        plot_spec_sha256=digests[PLOT_SPEC_PATH],
        provenance_sha256=digests[PROVENANCE_PATH],
        accessible_table_sha256=digests[ACCESSIBLE_TABLE_PATH],
    )
    return DevelopmentReportManifest(
        report_id=f"development-report-{sha256_bytes(canonical_json_bytes(bindings))[:24]}",
        artifacts=artifacts,
        bindings=bindings,
    )


def _validate_inputs(content: dict[str, bytes]) -> None:
    for path in _JSON_SCHEMAS:
        _require_identity(content[path], path=path)
    for path, value in content.items():
        if len(value) > _MAX_FILE_BYTES[path]:
            raise ReportBundleFormatError(f"{path} exceeds its byte limit")
    if sum(map(len, content.values())) > _MAX_TOTAL_BYTES:
        raise ReportBundleFormatError("report bundle exceeds its total byte limit")


def build_development_report_bundle(
    output_dir: str | Path,
    *,
    result_bytes: bytes,
    plot_data_bytes: bytes,
    plot_spec_bytes: bytes,
    provenance_bytes: bytes,
    accessible_table_bytes: bytes,
) -> Path:
    """Atomically publish one immutable bundle from validated development bytes."""

    content = {
        RESULT_PATH: result_bytes,
        PLOT_DATA_PATH: plot_data_bytes,
        PLOT_SPEC_PATH: plot_spec_bytes,
        PROVENANCE_PATH: provenance_bytes,
        ACCESSIBLE_TABLE_PATH: accessible_table_bytes,
    }
    if any(type(value) is not bytes for value in content.values()):
        raise TypeError("all report artifacts must be exact bytes")
    _validate_inputs(content)
    manifest = _manifest_for(content)
    content[MANIFEST_PATH] = canonical_json_bytes(manifest)

    destination = Path(output_dir)
    destination.parent.mkdir(parents=True, exist_ok=True)
    lock_path = destination.parent / f".{destination.name}.publish.lock"
    try:
        lock_descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise ReportBundleFilesystemError("report bundle publication is already in progress") from exc
    os.close(lock_descriptor)
    temporary = Path(tempfile.mkdtemp(prefix=f".{destination.name}.", dir=destination.parent))
    try:
        if destination.exists() or destination.is_symlink():
            raise ReportBundleFilesystemError("report bundle destination already exists")
        for relative_path in sorted(content):
            target = temporary / relative_path
            with target.open("xb") as stream:
                stream.write(content[relative_path])
                stream.flush()
                os.fsync(stream.fileno())
        os.rename(temporary, destination)
        parent_descriptor = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(parent_descriptor)
        finally:
            os.close(parent_descriptor)
    except Exception:
        if temporary.exists():
            shutil.rmtree(temporary)
        raise
    finally:
        lock_path.unlink(missing_ok=True)
    return destination


def _read_exact_bundle(root: Path) -> dict[str, bytes]:
    if root.is_symlink() or not root.is_dir():
        raise ReportBundleFilesystemError("report bundle must be a real directory")
    found: set[str] = set()
    content: dict[str, bytes] = {}
    total_bytes = 0
    for entry in root.iterdir():
        relative = entry.name
        if entry.is_symlink():
            raise ReportBundleFilesystemError(f"symlink forbidden in report bundle: {relative}")
        if not entry.is_file():
            raise ReportBundleFilesystemError(f"non-file entry in report bundle: {relative}")
        if relative not in _ALL_PATHS:
            raise ReportBundleFilesystemError(f"unexpected report bundle file: {relative}")
        found.add(relative)
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(entry, flags)
        except OSError as exc:
            raise ReportBundleFilesystemError(f"cannot safely open report file: {relative}") from exc
        with os.fdopen(descriptor, "rb") as stream:
            metadata = os.fstat(stream.fileno())
            if not stat.S_ISREG(metadata.st_mode):
                raise ReportBundleFilesystemError(f"non-regular report file: {relative}")
            if metadata.st_size > _MAX_FILE_BYTES[relative]:
                raise ReportBundleFilesystemError(f"report file exceeds byte limit: {relative}")
            value = stream.read(_MAX_FILE_BYTES[relative] + 1)
        total_bytes += len(value)
        if total_bytes > _MAX_TOTAL_BYTES:
            raise ReportBundleFilesystemError("report bundle exceeds its total byte limit")
        content[relative] = value
    if found != _ALL_PATHS:
        raise ReportBundleFilesystemError(
            f"report bundle file set mismatch; missing={sorted(_ALL_PATHS - found)}"
        )
    return content


def replay_development_report_bundle(
    bundle_dir: str | Path,
) -> VerifiedDevelopmentReportBundle:
    """Replay all v1 identities and commitments from the exact stored bytes."""

    root = Path(bundle_dir)
    content = _read_exact_bundle(root)
    try:
        manifest = canonical_model_from_bytes(
            DevelopmentReportManifest, content[MANIFEST_PATH]
        )
    except Exception as exc:
        raise ReportBundleFormatError("manifest is invalid or noncanonical") from exc
    artifact_content = {path: content[path] for path in _ARTIFACT_PATHS}
    _validate_inputs(artifact_content)
    expected = _manifest_for(artifact_content)
    for item in manifest.artifacts:
        actual = artifact_content[item.relative_path]
        if len(actual) != item.size_bytes:
            raise ReportBundleIntegrityError(f"size mismatch for {item.relative_path}")
        if sha256_bytes(actual) != item.sha256:
            raise ReportBundleIntegrityError(f"digest mismatch for {item.relative_path}")
    if manifest != expected:
        raise ReportBundleIntegrityError("manifest does not replay from report artifacts")
    return VerifiedDevelopmentReportBundle(
        path=str(root.resolve()),
        manifest=manifest,
        result_bytes=content[RESULT_PATH],
        plot_data_bytes=content[PLOT_DATA_PATH],
        plot_spec_bytes=content[PLOT_SPEC_PATH],
        provenance_bytes=content[PROVENANCE_PATH],
        accessible_table_bytes=content[ACCESSIBLE_TABLE_PATH],
    )


def verify_development_report_bundle(
    bundle_dir: str | Path,
) -> VerifiedDevelopmentReportBundle:
    """Fail closed unless the report bundle replays exactly."""

    return replay_development_report_bundle(bundle_dir)


__all__ = [
    "ACCESSIBLE_TABLE_PATH",
    "DevelopmentReportManifest",
    "MANIFEST_PATH",
    "METHOD_ID",
    "PLOT_DATA_PATH",
    "PLOT_DATA_SCHEMA",
    "PLOT_SPEC_PATH",
    "PLOT_SPEC_SCHEMA",
    "PROVENANCE_PATH",
    "PROVENANCE_SCHEMA",
    "QUALIFICATION_STATUS",
    "REPORT_BUNDLE_SCHEMA",
    "RESULT_PATH",
    "RESULT_SCHEMA",
    "ReportArtifact",
    "ReportBundleError",
    "ReportBundleFilesystemError",
    "ReportBundleFormatError",
    "ReportBundleIntegrityError",
    "ResultBindings",
    "VerifiedDevelopmentReportBundle",
    "build_development_report_bundle",
    "replay_development_report_bundle",
    "verify_development_report_bundle",
]
