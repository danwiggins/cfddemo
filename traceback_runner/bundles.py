"""Canonical synthetic result-bundle construction and offline verification."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Annotated, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from evidence_inspector.models import canonical_json_bytes
from traceback_runner.export import (
    ExportLimitations,
    ExportProvenance,
    FragmentLengthChart,
    FragmentLengthMeasurement,
    chart_for_measurement,
    render_report,
    validate_measurement,
    validate_provenance,
)
from traceback_runner.signing import (
    DevelopmentSigningKey,
    KeyPurpose,
    SignatureEnvelope,
    TrustNamespace,
    TrustStore,
    sign_bytes,
    verify_signature,
)


class BundleError(ValueError):
    """Base class for malformed or unsafe bundle failures."""


class BundleFilesystemError(BundleError):
    """Bundle filesystem shape is unsafe or unsupported."""


class BundleFormatError(BundleError):
    """Bundle content or version is invalid."""


class BundleIntegrityError(BundleError):
    """Bundle bytes do not match their signed inventory."""


class _ClosedModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, str_strip_whitespace=True, allow_inf_nan=False
    )


Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
BundlePath = Annotated[
    str,
    StringConstraints(
        pattern=r"^(?:bundle-manifest\.json|checksums\.sha256|"
        r"measurements/fragment-length\.v1\.json|"
        r"charts/fragment-length\.v1\.json|provenance\.json|"
        r"limitations\.json|report\.html|bundle\.sig)$"
    ),
]

MEASUREMENT_PATH = "measurements/fragment-length.v1.json"
CHART_PATH = "charts/fragment-length.v1.json"
PROVENANCE_PATH = "provenance.json"
LIMITATIONS_PATH = "limitations.json"
REPORT_PATH = "report.html"
MANIFEST_PATH = "bundle-manifest.json"
CHECKSUMS_PATH = "checksums.sha256"
SIGNATURE_PATH = "bundle.sig"

_CONTENT_PATHS = (
    MEASUREMENT_PATH,
    CHART_PATH,
    PROVENANCE_PATH,
    LIMITATIONS_PATH,
    REPORT_PATH,
)
_CHECKSUM_PATHS = (MANIFEST_PATH, *_CONTENT_PATHS)
_ALL_PATHS = frozenset((*_CHECKSUM_PATHS, CHECKSUMS_PATH, SIGNATURE_PATH))


class BundleFile(_ClosedModel):
    path: BundlePath
    size_bytes: int = Field(ge=0)
    sha256: Sha256


class BundleManifest(_ClosedModel):
    schema_version: Literal["traceback.result-bundle.v1"] = (
        "traceback.result-bundle.v1"
    )
    synthetic_only: Literal[True] = True
    trust_namespace: Literal[TrustNamespace.DEVELOPMENT_SYNTHETIC] = (
        TrustNamespace.DEVELOPMENT_SYNTHETIC
    )
    signing_purpose: Literal[KeyPurpose.RESULT] = KeyPurpose.RESULT
    signing_key_id: Annotated[
        str,
        StringConstraints(
            min_length=8,
            max_length=128,
            pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$",
        ),
    ]
    files: tuple[BundleFile, ...] = Field(min_length=5, max_length=5)


class BundleSigningPayload(_ClosedModel):
    schema_version: Literal["traceback.bundle-signing-payload.v1"] = (
        "traceback.bundle-signing-payload.v1"
    )
    bundle_schema_version: Literal["traceback.result-bundle.v1"] = (
        "traceback.result-bundle.v1"
    )
    trust_namespace: Literal[TrustNamespace.DEVELOPMENT_SYNTHETIC] = (
        TrustNamespace.DEVELOPMENT_SYNTHETIC
    )
    signing_purpose: Literal[KeyPurpose.RESULT] = KeyPurpose.RESULT
    bundle_files: tuple[BundlePath, ...]
    checksums_sha256: Sha256


class VerifiedBundle(_ClosedModel):
    path: str
    manifest: BundleManifest
    measurement: FragmentLengthMeasurement
    chart: FragmentLengthChart
    provenance: ExportProvenance
    limitations: ExportLimitations
    signature: SignatureEnvelope


def _digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _canonical_line_json(model: BaseModel) -> bytes:
    return canonical_json_bytes(model) + b"\n"


def _checksums_bytes(files: Mapping[str, bytes]) -> bytes:
    return "".join(f"{_digest(files[path])}  {path}\n" for path in _CHECKSUM_PATHS).encode(
        "ascii"
    )


def _signing_payload(checksums: bytes) -> BundleSigningPayload:
    return BundleSigningPayload(
        bundle_files=tuple(sorted(_ALL_PATHS)),
        checksums_sha256=_digest(checksums),
    )


def build_result_bundle(
    output_dir: str | Path,
    *,
    measurement: FragmentLengthMeasurement | Mapping[str, object],
    provenance: ExportProvenance | Mapping[str, object],
    signing_key: DevelopmentSigningKey,
) -> Path:
    """Build one atomic, synthetic-only bundle from strict aggregate inputs."""

    if signing_key.purpose != KeyPurpose.RESULT:
        raise BundleFormatError("result bundles require a result-purpose signing key")
    parsed_measurement = validate_measurement(measurement)
    parsed_provenance = validate_provenance(provenance)
    if (
        parsed_provenance.workflow_release_id
        != parsed_measurement.workflow_release_id
        or parsed_provenance.measurement_definition_id
        != parsed_measurement.measurement_definition_id
        or parsed_provenance.reference_id != parsed_measurement.reference_id
    ):
        raise BundleFormatError("measurement and provenance identities do not match")

    measurement_content = _canonical_line_json(parsed_measurement)
    chart = chart_for_measurement(parsed_measurement, _digest(measurement_content))
    limitations = ExportLimitations()
    content: dict[str, bytes] = {
        MEASUREMENT_PATH: measurement_content,
        CHART_PATH: _canonical_line_json(chart),
        PROVENANCE_PATH: _canonical_line_json(parsed_provenance),
        LIMITATIONS_PATH: _canonical_line_json(limitations),
        REPORT_PATH: render_report(parsed_measurement),
    }
    manifest = BundleManifest(
        signing_key_id=signing_key.key_id,
        files=tuple(
            BundleFile(path=path, size_bytes=len(content[path]), sha256=_digest(content[path]))
            for path in _CONTENT_PATHS
        ),
    )
    content[MANIFEST_PATH] = _canonical_line_json(manifest)
    checksums = _checksums_bytes(content)
    signature = sign_bytes(
        canonical_json_bytes(_signing_payload(checksums)),
        signing_key,
        purpose=KeyPurpose.RESULT,
    )
    content[CHECKSUMS_PATH] = checksums
    content[SIGNATURE_PATH] = _canonical_line_json(signature)

    destination = Path(output_dir)
    if destination.exists():
        raise BundleFilesystemError("bundle destination already exists")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(
        tempfile.mkdtemp(prefix=f".{destination.name}.", dir=destination.parent)
    )
    try:
        for relative_path in sorted(content):
            target = temporary / relative_path
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("xb") as stream:
                stream.write(content[relative_path])
                stream.flush()
                os.fsync(stream.fileno())
        os.replace(temporary, destination)
    except Exception:
        # A failed private staging directory contains aggregate synthetic bytes only.
        for path in sorted(temporary.rglob("*"), reverse=True):
            if path.is_file():
                path.unlink()
            elif path.is_dir():
                path.rmdir()
        if temporary.exists():
            temporary.rmdir()
        raise
    return destination


def _read_exact_files(bundle_dir: Path) -> dict[str, bytes]:
    if bundle_dir.is_symlink() or not bundle_dir.is_dir():
        raise BundleFilesystemError("bundle path must be a real directory, not a symlink")
    found: set[str] = set()
    content: dict[str, bytes] = {}
    for entry in bundle_dir.rglob("*"):
        relative = entry.relative_to(bundle_dir).as_posix()
        if entry.is_symlink():
            raise BundleFilesystemError(f"symlink forbidden in bundle: {relative}")
        if entry.is_dir():
            if relative not in {"measurements", "charts"}:
                raise BundleFilesystemError(f"unexpected directory in bundle: {relative}")
            continue
        if not entry.is_file():
            raise BundleFilesystemError(f"non-regular bundle entry: {relative}")
        found.add(relative)
        if relative not in _ALL_PATHS:
            raise BundleFilesystemError(f"unexpected file in bundle: {relative}")
        content[relative] = entry.read_bytes()
    if found != _ALL_PATHS:
        missing = sorted(_ALL_PATHS - found)
        raise BundleFilesystemError(f"bundle file set mismatch; missing={missing}")
    return content


def _parse_json(content: bytes, model: type[BaseModel], label: str) -> BaseModel:
    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        value: dict[str, object] = {}
        for key, nested in pairs:
            if key in value:
                raise ValueError(f"duplicate JSON key {key!r}")
            value[key] = nested
        return value

    try:
        value = json.loads(content, object_pairs_hook=reject_duplicates)
        parsed = model.model_validate(value)
    except Exception as exc:
        raise BundleFormatError(f"invalid {label}") from exc
    if content != _canonical_line_json(parsed):
        raise BundleFormatError(f"{label} is not canonical JSON")
    return parsed


def _parse_checksums(content: bytes) -> dict[str, str]:
    try:
        text = content.decode("ascii")
    except UnicodeDecodeError as exc:
        raise BundleFormatError("checksums file must be ASCII") from exc
    parsed: dict[str, str] = {}
    for line in text.splitlines(keepends=True):
        match = re.fullmatch(r"([0-9a-f]{64})  ([^\r\n]+)\n", line)
        if match is None:
            raise BundleFormatError("malformed checksums line")
        digest, path = match.groups()
        if path not in _CHECKSUM_PATHS or path in parsed:
            raise BundleFormatError("checksums contain unknown or duplicate path")
        parsed[path] = digest
    if tuple(parsed) != _CHECKSUM_PATHS:
        raise BundleFormatError("checksums inventory or ordering is invalid")
    return parsed


def verify_bundle(bundle_dir: str | Path, trust_store: TrustStore) -> VerifiedBundle:
    """Verify actual directory bytes against strict schemas and independent trust."""

    root = Path(bundle_dir)
    content = _read_exact_files(root)
    manifest = _parse_json(content[MANIFEST_PATH], BundleManifest, "bundle manifest")
    signature = _parse_json(content[SIGNATURE_PATH], SignatureEnvelope, "signature")
    assert isinstance(manifest, BundleManifest)
    assert isinstance(signature, SignatureEnvelope)
    if signature.key_id != manifest.signing_key_id:
        raise BundleIntegrityError("manifest and signature key identifiers differ")

    checksums = _parse_checksums(content[CHECKSUMS_PATH])
    for path, expected in checksums.items():
        if _digest(content[path]) != expected:
            raise BundleIntegrityError(f"checksum mismatch for {path}")
    manifest_files = tuple(item.path for item in manifest.files)
    if manifest_files != _CONTENT_PATHS:
        raise BundleFormatError("manifest content inventory or ordering is invalid")
    for item in manifest.files:
        actual = content[item.path]
        if len(actual) != item.size_bytes or _digest(actual) != item.sha256:
            raise BundleIntegrityError(f"manifest mismatch for {item.path}")

    verify_signature(
        canonical_json_bytes(_signing_payload(content[CHECKSUMS_PATH])),
        signature,
        trust_store,
        purpose=KeyPurpose.RESULT,
    )

    measurement = _parse_json(
        content[MEASUREMENT_PATH], FragmentLengthMeasurement, "measurement"
    )
    chart = _parse_json(content[CHART_PATH], FragmentLengthChart, "chart")
    provenance = _parse_json(content[PROVENANCE_PATH], ExportProvenance, "provenance")
    limitations = _parse_json(
        content[LIMITATIONS_PATH], ExportLimitations, "limitations"
    )
    assert isinstance(measurement, FragmentLengthMeasurement)
    assert isinstance(chart, FragmentLengthChart)
    assert isinstance(provenance, ExportProvenance)
    assert isinstance(limitations, ExportLimitations)

    expected_chart = chart_for_measurement(measurement, _digest(content[MEASUREMENT_PATH]))
    if chart != expected_chart:
        raise BundleIntegrityError("chart does not derive from the canonical measurement")
    if content[REPORT_PATH] != render_report(measurement):
        raise BundleIntegrityError("report is not the approved rendering of measurement")
    if (
        provenance.workflow_release_id != measurement.workflow_release_id
        or provenance.measurement_definition_id != measurement.measurement_definition_id
        or provenance.reference_id != measurement.reference_id
    ):
        raise BundleIntegrityError("provenance does not identify the measurement")
    return VerifiedBundle(
        path=str(root.resolve()),
        manifest=manifest,
        measurement=measurement,
        chart=chart,
        provenance=provenance,
        limitations=limitations,
        signature=signature,
    )


def inspect_bundle(bundle_dir: str | Path) -> BundleManifest:
    """Inspect an untrusted manifest without implying verification."""

    content = _read_exact_files(Path(bundle_dir))
    manifest = _parse_json(content[MANIFEST_PATH], BundleManifest, "bundle manifest")
    assert isinstance(manifest, BundleManifest)
    return manifest


__all__ = [
    "BundleError",
    "BundleFilesystemError",
    "BundleFormatError",
    "BundleIntegrityError",
    "BundleManifest",
    "VerifiedBundle",
    "build_result_bundle",
    "inspect_bundle",
    "verify_bundle",
]
