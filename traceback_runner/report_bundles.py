"""Typed deterministic bundles for local development dosage reports only."""

from __future__ import annotations

import json
import os
import re
import secrets
import stat
from pathlib import Path
from typing import Annotated, Any, Literal, TypeVar

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    TypeAdapter,
    model_validator,
)

from evidence_inspector.copy_number_qc import (
    DosageQcInsufficientResultBundle,
    DosageQcResultBundle,
)

from .filesystem import rename_directory_exclusive_at
from .serialization import canonical_json_bytes, canonical_model_from_bytes, sha256_bytes

REPORT_BUNDLE_SCHEMA = "traceback.development-report-bundle.v1"
RESULT_SCHEMA = "copy-number-dosage-qc.v2"
PLOT_DATA_SCHEMA = "traceback.copy-number-dosage-plot-data.v1"
PLOT_SPEC_SCHEMA = "traceback.copy-number-dosage-plot-spec.v1"
PROVENANCE_SCHEMA = "traceback.copy-number-dosage-report-provenance.v1"
TABLE_SCHEMA = "traceback.copy-number-dosage-accessible-table.v1"
TABLE_MEDIA_TYPE = "text/tab-separated-values; charset=utf-8"
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
_MAX_FILE_BYTES = {
    RESULT_PATH: 32 * 1024 * 1024,
    PLOT_DATA_PATH: 1024 * 1024,
    PLOT_SPEC_PATH: 64 * 1024,
    PROVENANCE_PATH: 64 * 1024,
    ACCESSIBLE_TABLE_PATH: 2 * 1024 * 1024,
    MANIFEST_PATH: 128 * 1024,
}
_MAX_TOTAL_BYTES = 36 * 1024 * 1024
_TABLE_HEADER = (
    "table_schema\tmedia_type\tresult_sha256\tmethod_id\tqualification_status\t"
    "analysis_status\tchromosome\trelative_diploid_dosage\n"
)
_ABSOLUTE_PATH = re.compile(r"(?:^|\s)(?:/[^\s]+|[A-Za-z]:\\[^\s]+)")
_SEQUENCE = re.compile(r"(?<![A-Za-z])[ACGTN]{20,}(?![A-Za-z])", re.IGNORECASE)
_SECRET = re.compile(
    r"(?:AWS_SECRET_ACCESS_KEY|PRIVATE_KEY|PASSWORD|SECRET|TOKEN)\s*=",
    re.IGNORECASE,
)
_RAW_IDENTIFIER = re.compile(
    r"\b(?:read|sample|query)[_-]?id\s*[:=]\s*\S+",
    re.IGNORECASE,
)
_FORBIDDEN_FIELDS = frozenset(
    {"path", "local_path", "read_id", "read_ids", "query_name", "sample_label", "secret"}
)


class ReportBundleError(ValueError):
    """Base class for malformed or unsafe development report bundles."""


class ReportBundleFormatError(ReportBundleError):
    """An artifact violates its closed contract or privacy allowlist."""


class ReportBundleFilesystemError(ReportBundleError):
    """The bundle filesystem shape or publication target is unsafe."""


class ReportBundleIntegrityError(ReportBundleError):
    """Stored bytes disagree with their manifest commitments."""


class _ClosedModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
        validate_default=True,
        allow_inf_nan=False,
    )


Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
Chromosome = Annotated[
    str, StringConstraints(pattern=r"^chr(?:[1-9]|1[0-9]|2[0-2])$")
]
AnalysisStatus = Literal["complete", "insufficient_information"]


class DosagePlotRow(_ClosedModel):
    chromosome: Chromosome
    relative_diploid_dosage: float = Field(ge=0, allow_inf_nan=False)


class DevelopmentPlotData(_ClosedModel):
    schema_version: Literal["traceback.copy-number-dosage-plot-data.v1"] = (
        PLOT_DATA_SCHEMA
    )
    method_id: Literal["sample-internal-whole-chromosome-dosage-qc"] = METHOD_ID
    qualification_status: Literal["development_unqualified"] = QUALIFICATION_STATUS
    result_sha256: Sha256
    analysis_status: AnalysisStatus
    rows: tuple[DosagePlotRow, ...]

    @model_validator(mode="after")
    def exact_rows(self) -> DevelopmentPlotData:
        chromosomes = tuple(row.chromosome for row in self.rows)
        expected = tuple(f"chr{index}" for index in range(1, 23))
        if self.analysis_status == "complete" and chromosomes != expected:
            raise ValueError("complete plot data must contain chr1 through chr22 in order")
        if self.analysis_status == "insufficient_information" and self.rows:
            raise ValueError("insufficient plot data must not contain dosage rows")
        return self


class DevelopmentPlotSpec(_ClosedModel):
    schema_version: Literal["traceback.copy-number-dosage-plot-spec.v1"] = (
        PLOT_SPEC_SCHEMA
    )
    method_id: Literal["sample-internal-whole-chromosome-dosage-qc"] = METHOD_ID
    qualification_status: Literal["development_unqualified"] = QUALIFICATION_STATUS
    result_sha256: Sha256
    analysis_status: AnalysisStatus
    plot_data_sha256: Sha256
    mark: Literal["point"] = "point"
    x_field: Literal["chromosome"] = "chromosome"
    y_field: Literal["relative_diploid_dosage"] = "relative_diploid_dosage"
    x_title: Literal["Chromosome"] = "Chromosome"
    y_title: Literal["Relative diploid dosage"] = "Relative diploid dosage"


class DevelopmentReportProvenance(_ClosedModel):
    schema_version: Literal["traceback.copy-number-dosage-report-provenance.v1"] = (
        PROVENANCE_SCHEMA
    )
    method_id: Literal["sample-internal-whole-chromosome-dosage-qc"] = METHOD_ID
    qualification_status: Literal["development_unqualified"] = QUALIFICATION_STATUS
    result_schema_version: Literal["copy-number-dosage-qc.v2"] = RESULT_SCHEMA
    result_sha256: Sha256
    analysis_status: AnalysisStatus
    generator_id: Literal["traceback.report-bundles.v1"] = (
        "traceback.report-bundles.v1"
    )
    product_release_authorized: Literal[False] = False


class ReportArtifact(_ClosedModel):
    relative_path: Literal[
        "accessible-table.tsv",
        "plot-data.json",
        "plot-spec.json",
        "provenance.json",
        "result.json",
    ]
    sha256: Sha256
    size_bytes: int = Field(ge=0)
    schema_identity: Literal[
        "copy-number-dosage-qc.v2",
        "traceback.copy-number-dosage-plot-data.v1",
        "traceback.copy-number-dosage-plot-spec.v1",
        "traceback.copy-number-dosage-report-provenance.v1",
        "traceback.copy-number-dosage-accessible-table.v1",
    ]
    media_type: Literal[
        "application/json",
        "text/tab-separated-values; charset=utf-8",
    ]


class ResultBindings(_ClosedModel):
    result_sha256: Sha256
    plot_data_sha256: Sha256
    plot_spec_sha256: Sha256
    provenance_sha256: Sha256
    accessible_table_sha256: Sha256


class DevelopmentReportManifest(_ClosedModel):
    schema_version: Literal["traceback.development-report-bundle.v1"] = (
        REPORT_BUNDLE_SCHEMA
    )
    method_id: Literal["sample-internal-whole-chromosome-dosage-qc"] = METHOD_ID
    qualification_status: Literal["development_unqualified"] = QUALIFICATION_STATUS
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
        expected = ResultBindings(
            result_sha256=digests[RESULT_PATH],
            plot_data_sha256=digests[PLOT_DATA_PATH],
            plot_spec_sha256=digests[PLOT_SPEC_PATH],
            provenance_sha256=digests[PROVENANCE_PATH],
            accessible_table_sha256=digests[ACCESSIBLE_TABLE_PATH],
        )
        if self.bindings != expected:
            raise ValueError("result bindings do not match the artifact inventory")
        report_id = f"development-report-{sha256_bytes(canonical_json_bytes(expected))[:24]}"
        if self.report_id != report_id:
            raise ValueError("report_id does not match the exact artifact bindings")
        return self


class VerifiedDevelopmentReportBundle(_ClosedModel):
    manifest: DevelopmentReportManifest
    result: DosageQcResultBundle | DosageQcInsufficientResultBundle
    plot_data: DevelopmentPlotData
    plot_spec: DevelopmentPlotSpec
    provenance: DevelopmentReportProvenance
    accessible_table_bytes: bytes


_RESULT_ADAPTER = TypeAdapter(DosageQcResultBundle | DosageQcInsufficientResultBundle)
ModelT = TypeVar("ModelT", bound=BaseModel)


def _privacy_check(value: Any, *, field_name: str = "") -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            if key.lower() in _FORBIDDEN_FIELDS:
                raise ReportBundleFormatError(f"privacy-forbidden field: {key}")
            _privacy_check(nested, field_name=str(key))
    elif isinstance(value, (tuple, list)):
        for nested in value:
            _privacy_check(nested, field_name=field_name)
    elif isinstance(value, str):
        digest_field = field_name.endswith(("sha256", "md5"))
        if _ABSOLUTE_PATH.search(value):
            raise ReportBundleFormatError(f"absolute local path forbidden in {field_name}")
        if _SECRET.search(value):
            raise ReportBundleFormatError(f"secret-like text forbidden in {field_name}")
        if _RAW_IDENTIFIER.search(value):
            raise ReportBundleFormatError(f"raw identifier forbidden in {field_name}")
        if not digest_field and _SEQUENCE.search(value):
            raise ReportBundleFormatError(f"sequence-like text forbidden in {field_name}")


def _reject_constant(token: str) -> None:
    raise ValueError(f"non-finite JSON token: {token}")


def _parse_result(content: bytes) -> DosageQcResultBundle | DosageQcInsufficientResultBundle:
    try:
        value = json.loads(content, parse_constant=_reject_constant)
        result = _RESULT_ADAPTER.validate_python(value)
    except Exception as exc:
        raise ReportBundleFormatError("result.json violates the dosage result union") from exc
    if canonical_json_bytes(result) != content:
        raise ReportBundleFormatError("result.json is not canonical JSON")
    _privacy_check(result.model_dump(mode="json"))
    return result


def _parse_model(model: type[ModelT], content: bytes, path: str) -> ModelT:
    try:
        parsed = canonical_model_from_bytes(model, content)
    except Exception as exc:
        raise ReportBundleFormatError(f"{path} violates its closed canonical schema") from exc
    _privacy_check(parsed.model_dump(mode="json"))
    return parsed


def _result_rows(
    result: DosageQcResultBundle | DosageQcInsufficientResultBundle,
) -> tuple[DosagePlotRow, ...]:
    if isinstance(result, DosageQcInsufficientResultBundle):
        return ()
    return tuple(
        DosagePlotRow(
            chromosome=row.chromosome,
            relative_diploid_dosage=row.relative_diploid_dosage,
        )
        for row in result.chromosomes
    )


def accessible_table_bytes(
    *, result_sha256: str, analysis_status: AnalysisStatus, rows: tuple[DosagePlotRow, ...]
) -> bytes:
    """Return the only canonical accessible-table representation accepted by v1."""

    prefix = (
        f"{TABLE_SCHEMA}\t{TABLE_MEDIA_TYPE}\t{result_sha256}\t{METHOD_ID}\t"
        f"{QUALIFICATION_STATUS}\t{analysis_status}\t"
    )
    if not rows:
        body = f"{prefix}NA\tNA\n"
    else:
        body = "".join(
            f"{prefix}{row.chromosome}\t"
            f"{json.dumps(row.relative_diploid_dosage, allow_nan=False)}\n"
            for row in rows
        )
    return (_TABLE_HEADER + body).encode("utf-8")


def _validate_semantics(content: dict[str, bytes]) -> tuple[
    DosageQcResultBundle | DosageQcInsufficientResultBundle,
    DevelopmentPlotData,
    DevelopmentPlotSpec,
    DevelopmentReportProvenance,
]:
    result = _parse_result(content[RESULT_PATH])
    plot_data = _parse_model(DevelopmentPlotData, content[PLOT_DATA_PATH], PLOT_DATA_PATH)
    plot_spec = _parse_model(DevelopmentPlotSpec, content[PLOT_SPEC_PATH], PLOT_SPEC_PATH)
    provenance = _parse_model(
        DevelopmentReportProvenance, content[PROVENANCE_PATH], PROVENANCE_PATH
    )
    result_digest = sha256_bytes(content[RESULT_PATH])
    status: AnalysisStatus = result.analysis_status
    if any(
        item.result_sha256 != result_digest
        for item in (plot_data, plot_spec, provenance)
    ):
        raise ReportBundleIntegrityError("an artifact is not bound to the exact result")
    if any(item.analysis_status != status for item in (plot_data, plot_spec, provenance)):
        raise ReportBundleIntegrityError("artifact analysis statuses disagree")
    expected_rows = _result_rows(result)
    if plot_data.rows != expected_rows:
        raise ReportBundleIntegrityError("plot data does not replay from the result")
    if plot_spec.plot_data_sha256 != sha256_bytes(content[PLOT_DATA_PATH]):
        raise ReportBundleIntegrityError("plot spec is not bound to exact plot data")
    expected_table = accessible_table_bytes(
        result_sha256=result_digest,
        analysis_status=status,
        rows=expected_rows,
    )
    if content[ACCESSIBLE_TABLE_PATH] != expected_table:
        raise ReportBundleFormatError("accessible table is not canonical typed UTF-8 TSV")
    return result, plot_data, plot_spec, provenance


def _artifact_schema(path: str) -> tuple[str, str]:
    return {
        RESULT_PATH: (RESULT_SCHEMA, "application/json"),
        PLOT_DATA_PATH: (PLOT_DATA_SCHEMA, "application/json"),
        PLOT_SPEC_PATH: (PLOT_SPEC_SCHEMA, "application/json"),
        PROVENANCE_PATH: (PROVENANCE_SCHEMA, "application/json"),
        ACCESSIBLE_TABLE_PATH: (TABLE_SCHEMA, TABLE_MEDIA_TYPE),
    }[path]


def _manifest_for(content: dict[str, bytes]) -> DevelopmentReportManifest:
    artifacts = tuple(
        ReportArtifact(
            relative_path=path,
            sha256=sha256_bytes(content[path]),
            size_bytes=len(content[path]),
            schema_identity=_artifact_schema(path)[0],
            media_type=_artifact_schema(path)[1],
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


def _check_sizes(content: dict[str, bytes]) -> None:
    for path, value in content.items():
        if type(value) is not bytes:
            raise TypeError("all report artifacts must be exact bytes")
        if len(value) > _MAX_FILE_BYTES[path]:
            raise ReportBundleFormatError(f"{path} exceeds its byte limit")
    if sum(map(len, content.values())) > _MAX_TOTAL_BYTES:
        raise ReportBundleFormatError("report bundle exceeds its total byte limit")


def _open_directory(path: Path) -> int:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ReportBundleFilesystemError(f"cannot safely open directory: {path.name}") from exc
    if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise ReportBundleFilesystemError("expected a real directory")
    return descriptor


def _stage_name(parent_fd: int, destination_name: str) -> str:
    for _ in range(32):
        candidate = f".{destination_name}.{secrets.token_hex(8)}"
        try:
            os.mkdir(candidate, mode=0o700, dir_fd=parent_fd)
            return candidate
        except FileExistsError:
            continue
    raise ReportBundleFilesystemError("could not allocate a private staging directory")


def _require_same_directory(path: Path, descriptor: int) -> None:
    """Fail if the named parent stopped referring to the pinned directory."""

    try:
        named = os.stat(path, follow_symlinks=False)
    except OSError as exc:
        raise ReportBundleFilesystemError("report parent changed during publication") from exc
    pinned = os.fstat(descriptor)
    if not stat.S_ISDIR(named.st_mode) or (named.st_dev, named.st_ino) != (
        pinned.st_dev,
        pinned.st_ino,
    ):
        raise ReportBundleFilesystemError("report parent changed during publication")


def _cleanup_stage(parent_fd: int, stage_name: str | None, stage_fd: int | None) -> None:
    if stage_name is None:
        return
    if stage_fd is not None:
        for name in _ALL_PATHS:
            try:
                os.unlink(name, dir_fd=stage_fd)
            except FileNotFoundError:
                pass
    try:
        os.rmdir(stage_name, dir_fd=parent_fd)
    except FileNotFoundError:
        pass


def build_development_report_bundle(
    output_dir: str | Path,
    *,
    result_bytes: bytes,
    plot_data_bytes: bytes,
    plot_spec_bytes: bytes,
    provenance_bytes: bytes,
    accessible_table_bytes: bytes,
) -> Path:
    """Durably and exclusively publish one typed development report bundle."""

    content = {
        RESULT_PATH: result_bytes,
        PLOT_DATA_PATH: plot_data_bytes,
        PLOT_SPEC_PATH: plot_spec_bytes,
        PROVENANCE_PATH: provenance_bytes,
        ACCESSIBLE_TABLE_PATH: accessible_table_bytes,
    }
    _check_sizes(content)
    _validate_semantics(content)
    content[MANIFEST_PATH] = canonical_json_bytes(_manifest_for(content))

    destination = Path(output_dir)
    if destination.name in {"", ".", ".."}:
        raise ReportBundleFilesystemError("report destination must have a simple name")
    destination.parent.mkdir(parents=True, exist_ok=True)
    parent_fd = _open_directory(destination.parent)
    lock_name = f".{destination.name}.publish.lock"
    stage_name: str | None = None
    stage_fd: int | None = None
    lock_created = False
    published = False
    try:
        try:
            lock_fd = os.open(
                lock_name,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o600,
                dir_fd=parent_fd,
            )
        except FileExistsError as exc:
            raise ReportBundleFilesystemError("report publication is already in progress") from exc
        lock_created = True
        os.close(lock_fd)
        stage_name = _stage_name(parent_fd, destination.name)
        stage_fd = os.open(
            stage_name,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent_fd,
        )
        for relative_path in sorted(content):
            descriptor = os.open(
                relative_path,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=stage_fd,
            )
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content[relative_path])
                stream.flush()
                os.fsync(stream.fileno())
        os.fsync(stage_fd)
        _require_same_directory(destination.parent, parent_fd)
        try:
            rename_directory_exclusive_at(parent_fd, stage_name, destination.name)
        except FileExistsError as exc:
            raise ReportBundleFilesystemError("report bundle destination already exists") from exc
        published = True
        os.fsync(parent_fd)
        return destination
    finally:
        if not published:
            _cleanup_stage(parent_fd, stage_name, stage_fd)
        if stage_fd is not None:
            os.close(stage_fd)
        if lock_created:
            try:
                os.unlink(lock_name, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
        os.close(parent_fd)


def _read_exact_bundle(root: Path) -> dict[str, bytes]:
    root_fd = _open_directory(root)
    try:
        names = set(os.listdir(root_fd))
        if names != _ALL_PATHS:
            raise ReportBundleFilesystemError(
                "report bundle file set mismatch; "
                f"missing={sorted(_ALL_PATHS - names)}, extra={sorted(names - _ALL_PATHS)}"
            )
        content: dict[str, bytes] = {}
        total_bytes = 0
        for relative in sorted(_ALL_PATHS):
            flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            try:
                descriptor = os.open(relative, flags, dir_fd=root_fd)
            except OSError as exc:
                raise ReportBundleFilesystemError(
                    f"cannot safely open report file: {relative}"
                ) from exc
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
        return content
    finally:
        os.close(root_fd)


def replay_development_report_bundle(
    bundle_dir: str | Path,
) -> VerifiedDevelopmentReportBundle:
    """Replay every contract and commitment from a pinned bundle directory."""

    content = _read_exact_bundle(Path(bundle_dir))
    try:
        manifest = canonical_model_from_bytes(
            DevelopmentReportManifest, content[MANIFEST_PATH]
        )
    except Exception as exc:
        raise ReportBundleFormatError("manifest is invalid or noncanonical") from exc
    artifact_content = {path: content[path] for path in _ARTIFACT_PATHS}
    _check_sizes(artifact_content)
    result, plot_data, plot_spec, provenance = _validate_semantics(artifact_content)
    for item in manifest.artifacts:
        actual = artifact_content[item.relative_path]
        if len(actual) != item.size_bytes:
            raise ReportBundleIntegrityError(f"size mismatch for {item.relative_path}")
        if sha256_bytes(actual) != item.sha256:
            raise ReportBundleIntegrityError(f"digest mismatch for {item.relative_path}")
        if (item.schema_identity, item.media_type) != _artifact_schema(item.relative_path):
            raise ReportBundleIntegrityError(f"identity mismatch for {item.relative_path}")
    if manifest != _manifest_for(artifact_content):
        raise ReportBundleIntegrityError("manifest does not replay from report artifacts")
    return VerifiedDevelopmentReportBundle(
        manifest=manifest,
        result=result,
        plot_data=plot_data,
        plot_spec=plot_spec,
        provenance=provenance,
        accessible_table_bytes=content[ACCESSIBLE_TABLE_PATH],
    )


def verify_development_report_bundle(
    bundle_dir: str | Path,
) -> VerifiedDevelopmentReportBundle:
    """Fail closed unless the development report replays exactly."""

    return replay_development_report_bundle(bundle_dir)


__all__ = [
    "ACCESSIBLE_TABLE_PATH",
    "DevelopmentPlotData",
    "DevelopmentPlotSpec",
    "DevelopmentReportManifest",
    "DevelopmentReportProvenance",
    "DosagePlotRow",
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
    "TABLE_MEDIA_TYPE",
    "TABLE_SCHEMA",
    "VerifiedDevelopmentReportBundle",
    "accessible_table_bytes",
    "build_development_report_bundle",
    "replay_development_report_bundle",
    "verify_development_report_bundle",
]
