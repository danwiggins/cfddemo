"""Canonical development result-bundle construction and offline verification.

Bundle versions and what each carries:

- ``traceback.result-bundle.v1`` / ``v2``: ``fragment-measurement.v1``
  (synthetic), ``limitations.v1``, the synthetic report, signed under
  ``development-synthetic``.  ``build_result_bundle`` writes v2.
- ``traceback.result-bundle.v3``: ``fragment-measurement.v2`` labelled
  ``unapproved_local``, ``limitations.v2`` (local template), the local report,
  signed under ``development-local``.
- ``traceback.result-bundle.v4``: exactly one ``unapproved_local`` measurement
  of a schema registered in :mod:`traceback_runner.measurement_schemas`, signed
  under ``development-local``.  The measurement and chart paths
  (``measurements/<stem>.json``, ``charts/<stem>.json``), the contracts, the
  chart, limitations and report derivation and the byte bounds are all chosen
  by that schema.  Fragment-length records never travel in v4.

Rules are selected by ``(bundle_version, measurement_schema)``: v1-v3 each
admit one fixed fragment schema at the fixed fragment paths, unchanged.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal, Mapping

from pydantic import BaseModel, ConfigDict, StringConstraints

from traceback_runner.contracts import (
    AnyFragmentMeasurement,
    ApprovalState,
    BundleContent,
    BundleMethodIdentity,
    ExportRunProvenance,
    FragmentMeasurement,
    FragmentMeasurementV2,
    ResultBundleManifest,
    ResultBundleManifestV2,
    ResultBundleManifestV3,
    ResultBundleManifestV4,
    canonical_json_bytes,
    canonical_model_from_bytes,
    parse_fragment_measurement,
)
from traceback_runner.export import (
    AnyExportLimitations,
    ExportLimitations,
    ExportLimitationsV2,
    FragmentLengthChart,
    ReferenceMatch,
    chart_for_measurement,
    limitations_for_measurement,
    render_bundle_report,
    validate_measurement,
    validate_provenance,
)
from traceback_runner.measurement_schemas import (
    PATH_STEM,
    BundleMeasurementSchema,
    measurement_schema,
    measurement_schema_for_path,
    schema_version_of,
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

# v4 paths: the fixed files plus one measurement and one chart whose stem the
# measurement schema chooses.  v1-v3 payloads keep ``BundlePath`` unchanged.
BundlePathV4 = Annotated[
    str,
    StringConstraints(
        pattern=r"^(?:bundle-manifest\.json|checksums\.sha256|"
        rf"measurements/{PATH_STEM}\.json|"
        rf"charts/{PATH_STEM}\.json|provenance\.json|"
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
_MAX_FILE_BYTES: dict[str, int] = {
    MANIFEST_PATH: 256 * 1024,
    MEASUREMENT_PATH: 16 * 1024 * 1024,
    CHART_PATH: 16 * 1024 * 1024,
    PROVENANCE_PATH: 1024 * 1024,
    LIMITATIONS_PATH: 64 * 1024,
    REPORT_PATH: 2 * 1024 * 1024,
    CHECKSUMS_PATH: 64 * 1024,
    SIGNATURE_PATH: 16 * 1024,
}
_MAX_TOTAL_BYTES = 36 * 1024 * 1024


BundleManifest = (
    ResultBundleManifest
    | ResultBundleManifestV2
    | ResultBundleManifestV3
    | ResultBundleManifestV4
)

RESULT_BUNDLE_V1 = "traceback.result-bundle.v1"
RESULT_BUNDLE_V2 = "traceback.result-bundle.v2"
RESULT_BUNDLE_V3 = "traceback.result-bundle.v3"
RESULT_BUNDLE_V4 = "traceback.result-bundle.v4"
# Bundle versions that carry local (``unapproved_local``) records.
LOCAL_RESULT_BUNDLE_VERSIONS = frozenset({RESULT_BUNDLE_V3, RESULT_BUNDLE_V4})


@dataclass(frozen=True)
class _BundleLayout:
    """The exact file set of one bundle: fixed files plus its measurement pair."""

    measurement_path: str
    chart_path: str
    measurement_limit: int
    chart_limit: int

    @property
    def content_paths(self) -> tuple[str, ...]:
        return (
            self.measurement_path,
            self.chart_path,
            PROVENANCE_PATH,
            LIMITATIONS_PATH,
            REPORT_PATH,
        )

    @property
    def checksum_paths(self) -> tuple[str, ...]:
        return (MANIFEST_PATH, *self.content_paths)

    @property
    def all_paths(self) -> frozenset[str]:
        return frozenset((*self.checksum_paths, CHECKSUMS_PATH, SIGNATURE_PATH))

    def limit(self, relative: str) -> int:
        if relative == self.measurement_path:
            return self.measurement_limit
        if relative == self.chart_path:
            return self.chart_limit
        return _MAX_FILE_BYTES[relative]


# The one v1-v3 layout; every existing record has exactly these files.
_FRAGMENT_LAYOUT = _BundleLayout(
    measurement_path=MEASUREMENT_PATH,
    chart_path=CHART_PATH,
    measurement_limit=_MAX_FILE_BYTES[MEASUREMENT_PATH],
    chart_limit=_MAX_FILE_BYTES[CHART_PATH],
)


def _v4_layout(spec: BundleMeasurementSchema) -> _BundleLayout:
    return _BundleLayout(
        measurement_path=spec.measurement_path,
        chart_path=spec.chart_path,
        measurement_limit=spec.max_measurement_bytes,
        chart_limit=spec.max_chart_bytes,
    )


class _BundleVersionRules(BaseModel):
    """What one bundle schema version must carry; the single dispatch table."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    measurement_model: type
    limitations_model: type
    trust_namespace: TrustNamespace


# Every result-bundle schema this module reads, and the exact measurement,
# limitations, and signing namespace each requires.
_BUNDLE_VERSION_RULES: Mapping[str, _BundleVersionRules] = {
    RESULT_BUNDLE_V1: _BundleVersionRules(
        measurement_model=FragmentMeasurement,
        limitations_model=ExportLimitations,
        trust_namespace=TrustNamespace.DEVELOPMENT_SYNTHETIC,
    ),
    RESULT_BUNDLE_V2: _BundleVersionRules(
        measurement_model=FragmentMeasurement,
        limitations_model=ExportLimitations,
        trust_namespace=TrustNamespace.DEVELOPMENT_SYNTHETIC,
    ),
    RESULT_BUNDLE_V3: _BundleVersionRules(
        measurement_model=FragmentMeasurementV2,
        limitations_model=ExportLimitationsV2,
        trust_namespace=TrustNamespace.DEVELOPMENT_LOCAL,
    ),
}
RESULT_BUNDLE_SCHEMA_VERSIONS = (*_BUNDLE_VERSION_RULES, RESULT_BUNDLE_V4)


def _v4_rules(spec: BundleMeasurementSchema) -> _BundleVersionRules:
    """v4 rules for one registered measurement schema: always development-local."""

    return _BundleVersionRules(
        measurement_model=spec.measurement_model,
        limitations_model=spec.limitations_model,
        trust_namespace=TrustNamespace.DEVELOPMENT_LOCAL,
    )


def _v4_schema_for(measurement_schema_versions: tuple[str, ...]) -> BundleMeasurementSchema:
    """The one registered schema a v4 manifest names.

    A v4 bundle carries exactly one measurement: its layout (paths, bounds) and
    rules come from that measurement's schema.
    """

    if len(measurement_schema_versions) != 1:
        raise BundleFormatError("a v4 bundle carries exactly one measurement")
    spec = measurement_schema(measurement_schema_versions[0])
    if spec is None:
        raise BundleFormatError("v4 measurement schema is not registered")
    return spec


def _layout_for(manifest: BundleManifest) -> _BundleLayout:
    """Select the file layout by ``(bundle_version, measurement_schema)``."""

    if manifest.schema_version != RESULT_BUNDLE_V4:
        return _FRAGMENT_LAYOUT
    return _v4_layout(_v4_schema_for(tuple(manifest.measurement_schema_versions)))


def _rules_for(manifest: BundleManifest) -> _BundleVersionRules:
    """Select the rules by ``(bundle_version, measurement_schema)``."""

    if manifest.schema_version != RESULT_BUNDLE_V4:
        return _bundle_rules(manifest.schema_version)
    return _v4_rules(_v4_schema_for(tuple(manifest.measurement_schema_versions)))


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


class BundleSigningPayloadV2(_ClosedModel):
    schema_version: Literal["traceback.bundle-signing-payload.v2"] = (
        "traceback.bundle-signing-payload.v2"
    )
    bundle_schema_version: Literal["traceback.result-bundle.v2"] = (
        "traceback.result-bundle.v2"
    )
    trust_namespace: Literal[TrustNamespace.DEVELOPMENT_SYNTHETIC] = (
        TrustNamespace.DEVELOPMENT_SYNTHETIC
    )
    signing_purpose: Literal[KeyPurpose.RESULT] = KeyPurpose.RESULT
    bundle_files: tuple[BundlePath, ...]
    checksums_sha256: Sha256


class BundleSigningPayloadV3(_ClosedModel):
    schema_version: Literal["traceback.bundle-signing-payload.v3"] = (
        "traceback.bundle-signing-payload.v3"
    )
    bundle_schema_version: Literal["traceback.result-bundle.v3"] = (
        "traceback.result-bundle.v3"
    )
    trust_namespace: Literal[TrustNamespace.DEVELOPMENT_LOCAL] = (
        TrustNamespace.DEVELOPMENT_LOCAL
    )
    signing_purpose: Literal[KeyPurpose.RESULT] = KeyPurpose.RESULT
    bundle_files: tuple[BundlePath, ...]
    checksums_sha256: Sha256


class BundleSigningPayloadV4(_ClosedModel):
    schema_version: Literal["traceback.bundle-signing-payload.v4"] = (
        "traceback.bundle-signing-payload.v4"
    )
    bundle_schema_version: Literal["traceback.result-bundle.v4"] = (
        "traceback.result-bundle.v4"
    )
    trust_namespace: Literal[TrustNamespace.DEVELOPMENT_LOCAL] = (
        TrustNamespace.DEVELOPMENT_LOCAL
    )
    signing_purpose: Literal[KeyPurpose.RESULT] = KeyPurpose.RESULT
    bundle_files: tuple[BundlePathV4, ...]
    checksums_sha256: Sha256


class VerifiedBundle(_ClosedModel):
    """A verified bundle.  v1-v3 carry fragment contracts; v4 its registered ones."""

    path: str
    manifest: BundleManifest
    measurement: AnyFragmentMeasurement | BaseModel
    chart: FragmentLengthChart | BaseModel
    provenance: ExportRunProvenance
    limitations: AnyExportLimitations | BaseModel
    signature: SignatureEnvelope


def _digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _canonical_line_json(model: BaseModel) -> bytes:
    return canonical_json_bytes(model)


def _checksums_bytes(
    files: Mapping[str, bytes], layout: _BundleLayout = _FRAGMENT_LAYOUT
) -> bytes:
    return "".join(
        f"{_digest(files[path])}  {path}\n" for path in layout.checksum_paths
    ).encode("ascii")


def _signing_payload(
    checksums: bytes,
    bundle_schema_version: str,
    layout: _BundleLayout = _FRAGMENT_LAYOUT,
) -> (
    BundleSigningPayload
    | BundleSigningPayloadV2
    | BundleSigningPayloadV3
    | BundleSigningPayloadV4
):
    values = {
        "bundle_files": tuple(sorted(layout.all_paths)),
        "checksums_sha256": _digest(checksums),
    }
    if bundle_schema_version == RESULT_BUNDLE_V4:
        if layout == _FRAGMENT_LAYOUT:
            raise BundleFormatError("fragment-length records never travel in v4")
        return BundleSigningPayloadV4(**values)
    if layout != _FRAGMENT_LAYOUT:
        raise BundleFormatError("v1-v3 bundles carry only the fragment-length paths")
    if bundle_schema_version == RESULT_BUNDLE_V1:
        return BundleSigningPayload(**values)
    if bundle_schema_version == RESULT_BUNDLE_V2:
        return BundleSigningPayloadV2(**values)
    if bundle_schema_version == RESULT_BUNDLE_V3:
        return BundleSigningPayloadV3(**values)
    raise BundleFormatError("unsupported result bundle schema")


def _bundle_rules(bundle_schema_version: str) -> _BundleVersionRules:
    try:
        return _BUNDLE_VERSION_RULES[bundle_schema_version]
    except KeyError:
        raise BundleFormatError("unsupported result bundle schema") from None


def _v4_spec_of(measurement: object) -> BundleMeasurementSchema | None:
    if isinstance(measurement, (FragmentMeasurement, FragmentMeasurementV2)):
        return None
    return measurement_schema(schema_version_of(measurement))


def _check_record_label(
    measurement: AnyFragmentMeasurement | BaseModel,
    limitations: AnyExportLimitations | BaseModel,
) -> None:
    """A v3/v4 record is local-labelled throughout; v1/v2 are synthetic throughout."""

    spec = _v4_spec_of(measurement)
    if spec is not None:
        if (
            type(measurement) is not spec.measurement_model
            or getattr(measurement, "approval_state", None)
            != ApprovalState.UNAPPROVED_LOCAL
            or type(limitations) is not spec.limitations_model
            or limitations
            != spec.build_limitations(
                measurement, getattr(limitations, "reference_match", None)
            )
        ):
            raise BundleFormatError(
                "a v4 bundle carries only unapproved_local records with their "
                "registered local limitations"
            )
        return
    if type(measurement) is FragmentMeasurementV2:
        if (
            measurement.approval_state != ApprovalState.UNAPPROVED_LOCAL
            or not isinstance(limitations, ExportLimitationsV2)
            or limitations
            != limitations_for_measurement(measurement, limitations.reference_match)
        ):
            raise BundleFormatError(
                "a v3 bundle carries only unapproved_local records with local limitations"
            )
    elif type(limitations) is not ExportLimitations:
        raise BundleFormatError("a synthetic bundle carries v1 limitations")


def build_result_bundle(
    output_dir: str | Path,
    *,
    measurement: FragmentMeasurement | BaseModel | Mapping[str, object],
    provenance: ExportRunProvenance | Mapping[str, object],
    method: BundleMethodIdentity | Mapping[str, object],
    signing_key: DevelopmentSigningKey,
    reference_match: ReferenceMatch | None = None,
) -> Path:
    """Build one atomic bundle from strict aggregate inputs.

    A ``fragment-measurement.v1`` (synthetic) measurement builds a v2 bundle
    signed by a ``development-synthetic`` key, exactly as before.  A
    ``fragment-measurement.v2`` measurement labelled ``unapproved_local`` builds
    a v3 bundle signed by a ``development-local`` key; it requires
    ``reference_match`` (how preflight matched the reference), which the local
    limitations and report state.  A measurement of a schema registered for v4
    builds a v4 bundle signed by a ``development-local`` key, at the paths and
    with the chart, limitations and report its schema registered; it also
    requires ``reference_match``.
    """

    if signing_key.purpose != KeyPurpose.RESULT:
        raise BundleFormatError("result bundles require a result-purpose signing key")
    parsed_measurement = validate_measurement(measurement)
    parsed_provenance = validate_provenance(provenance)
    parsed_method = BundleMethodIdentity.model_validate(method)
    limitations: AnyExportLimitations | BaseModel
    layout = _FRAGMENT_LAYOUT
    spec = _v4_spec_of(parsed_measurement)
    if spec is not None:
        bundle_schema = RESULT_BUNDLE_V4
        if reference_match is None:
            raise BundleFormatError("a v4 bundle requires the reference match outcome")
        limitations = spec.build_limitations(parsed_measurement, reference_match)
        layout = _v4_layout(spec)
    elif type(parsed_measurement) is FragmentMeasurementV2:
        bundle_schema = RESULT_BUNDLE_V3
        if reference_match is None:
            raise BundleFormatError("a v3 bundle requires the reference match outcome")
        limitations = limitations_for_measurement(parsed_measurement, reference_match)
    else:
        bundle_schema = RESULT_BUNDLE_V2
        if reference_match is not None:
            raise BundleFormatError("a synthetic bundle takes no reference match outcome")
        limitations = ExportLimitations()
    rules = _bundle_rules(bundle_schema) if spec is None else _v4_rules(spec)
    if signing_key.namespace != rules.trust_namespace:
        raise BundleFormatError(
            f"{bundle_schema} requires a {rules.trust_namespace.value} signing key"
        )
    _check_record_label(parsed_measurement, limitations)
    measurement_content = _canonical_line_json(parsed_measurement)
    chart = chart_for_measurement(parsed_measurement, _digest(measurement_content))
    content: dict[str, bytes] = {
        layout.measurement_path: measurement_content,
        layout.chart_path: _canonical_line_json(chart),
        PROVENANCE_PATH: _canonical_line_json(parsed_provenance),
        LIMITATIONS_PATH: _canonical_line_json(limitations),
        REPORT_PATH: render_bundle_report(parsed_measurement, limitations),
    }
    record_identity = canonical_json_bytes(
        {
            "measurement_sha256": _digest(measurement_content),
            "method": parsed_method.model_dump(mode="json"),
            "provenance_sha256": _digest(content[PROVENANCE_PATH]),
        }
    )
    record_id = f"record-{_digest(record_identity)[:24]}"
    manifest_model = _MANIFEST_MODELS[bundle_schema]
    manifest = manifest_model(
        record_id=record_id,
        workflow_release_id=parsed_provenance.workflow_release_id,
        measurement_schema_versions=(parsed_measurement.schema_version,),
        method=parsed_method,
        signing_key_id=signing_key.key_id,
        contents=tuple(
            BundleContent(relative_path=path, size_bytes=len(content[path]), sha256=_digest(content[path]))
            for path in sorted(layout.content_paths)
        ),
    )
    content[MANIFEST_PATH] = _canonical_line_json(manifest)
    checksums = _checksums_bytes(content, layout)
    signature = sign_bytes(
        canonical_json_bytes(
            _signing_payload(checksums, manifest.schema_version, layout)
        ),
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


def _candidate_limit(relative: str) -> int | None:
    """The byte bound of a path some layout admits, or ``None`` for any other path.

    The fixed v1-v3 files keep their bounds; a registered v4 schema's
    measurement and chart paths carry that schema's bounds.  Which of these a
    bundle must hold exactly is decided by its manifest (``_layout_for``).
    """

    if relative in _ALL_PATHS:
        return _MAX_FILE_BYTES[relative]
    spec = measurement_schema_for_path(relative)
    if spec is None:
        return None
    return spec.max_measurement_bytes if relative == spec.measurement_path else (
        spec.max_chart_bytes
    )


def bundle_file_byte_limit(relative: str) -> int | None:
    """Public: the verifier's byte bound for one bundle path (``None``: never admitted).

    Unverified peeks read with this bound so a peek never refuses a file the
    verifier would accept, nor reads more than it would.
    """

    return _candidate_limit(relative)


def _require_file_set(found: set[str] | frozenset[str], layout: _BundleLayout) -> None:
    if found != layout.all_paths:
        missing = sorted(layout.all_paths - found)
        if found - layout.all_paths:
            raise BundleFilesystemError(
                f"bundle file set does not match its manifest; missing={missing}"
            )
        raise BundleFilesystemError(f"bundle file set mismatch; missing={missing}")


def _read_exact_files(bundle_dir: Path) -> dict[str, bytes]:
    if bundle_dir.is_symlink() or not bundle_dir.is_dir():
        raise BundleFilesystemError("bundle path must be a real directory, not a symlink")
    found: set[str] = set()
    content: dict[str, bytes] = {}
    total_bytes = 0
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
        candidate = _candidate_limit(relative)
        if candidate is None:
            raise BundleFilesystemError(f"unexpected file in bundle: {relative}")
        limit = candidate
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(entry, flags)
        except OSError as exc:
            raise BundleFilesystemError(f"cannot safely open bundle file: {relative}") from exc
        with os.fdopen(descriptor, "rb") as stream:
            metadata = os.fstat(stream.fileno())
            if not stat.S_ISREG(metadata.st_mode):
                raise BundleFilesystemError(f"non-regular bundle entry: {relative}")
            if metadata.st_size > limit:
                raise BundleFilesystemError(
                    f"bundle file exceeds {limit} byte limit: {relative}"
                )
            if total_bytes + metadata.st_size > _MAX_TOTAL_BYTES:
                raise BundleFilesystemError("bundle exceeds total byte limit")
            value = stream.read(limit + 1)
        if len(value) > limit:
            raise BundleFilesystemError(
                f"bundle file exceeds {limit} byte limit: {relative}"
            )
        total_bytes += len(value)
        if total_bytes > _MAX_TOTAL_BYTES:
            raise BundleFilesystemError("bundle exceeds total byte limit")
        content[relative] = value
    if MANIFEST_PATH not in content:
        _require_file_set(found, _FRAGMENT_LAYOUT)
    return content


def _read_bundle(bundle_dir: Path) -> tuple[dict[str, bytes], BundleManifest, _BundleLayout]:
    """Read every admitted file, parse the manifest, and hold the exact layout."""

    content = _read_exact_files(bundle_dir)
    manifest = _parse_manifest(content[MANIFEST_PATH])
    layout = _layout_for(manifest)
    _require_file_set(frozenset(content), layout)
    return content, manifest, layout


def _parse_json(content: bytes, model: type[BaseModel], label: str) -> BaseModel:
    try:
        parsed = canonical_model_from_bytes(model, content)
    except Exception as exc:
        raise BundleFormatError(f"invalid {label}") from exc
    return parsed


_MANIFEST_MODELS: Mapping[str, type[BaseModel]] = {
    RESULT_BUNDLE_V1: ResultBundleManifest,
    RESULT_BUNDLE_V2: ResultBundleManifestV2,
    RESULT_BUNDLE_V3: ResultBundleManifestV3,
    RESULT_BUNDLE_V4: ResultBundleManifestV4,
}


def _parse_manifest(content: bytes) -> BundleManifest:
    try:
        raw = json.loads(content)
        model = _MANIFEST_MODELS[raw["schema_version"]]
        parsed = canonical_model_from_bytes(model, content)
    except Exception:
        raise BundleFormatError("invalid bundle manifest") from None
    assert isinstance(
        parsed,
        (
            ResultBundleManifest,
            ResultBundleManifestV2,
            ResultBundleManifestV3,
            ResultBundleManifestV4,
        ),
    )
    return parsed


def _parse_checksums(
    content: bytes, layout: _BundleLayout = _FRAGMENT_LAYOUT
) -> dict[str, str]:
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
        if path not in layout.checksum_paths or path in parsed:
            raise BundleFormatError("checksums contain unknown or duplicate path")
        parsed[path] = digest
    if tuple(parsed) != layout.checksum_paths:
        raise BundleFormatError("checksums inventory or ordering is invalid")
    return parsed


def verify_bundle(bundle_dir: str | Path, trust_store: TrustStore) -> VerifiedBundle:
    """Verify actual directory bytes against strict schemas and independent trust."""

    root = Path(bundle_dir)
    content, manifest, layout = _read_bundle(root)
    signature = _parse_json(content[SIGNATURE_PATH], SignatureEnvelope, "signature")
    assert isinstance(signature, SignatureEnvelope)
    if signature.key_id != manifest.signing_key_id:
        raise BundleIntegrityError("manifest and signature key identifiers differ")

    checksums = _parse_checksums(content[CHECKSUMS_PATH], layout)
    for path, expected in checksums.items():
        if _digest(content[path]) != expected:
            raise BundleIntegrityError(f"checksum mismatch for {path}")
    manifest_files = tuple(item.relative_path for item in manifest.contents)
    if manifest_files != tuple(sorted(layout.content_paths)):
        raise BundleFormatError("manifest content inventory or ordering is invalid")
    for item in manifest.contents:
        actual = content[item.relative_path]
        if len(actual) != item.size_bytes or _digest(actual) != item.sha256:
            raise BundleIntegrityError(f"manifest mismatch for {item.relative_path}")

    rules = _rules_for(manifest)
    # The namespace is fixed by the bundle version: a v3/v4 (local) bundle signed
    # by a development-synthetic key, or a v1/v2 bundle signed by a
    # development-local key, fails here.
    verify_signature(
        canonical_json_bytes(
            _signing_payload(content[CHECKSUMS_PATH], manifest.schema_version, layout)
        ),
        signature,
        trust_store,
        purpose=KeyPurpose.RESULT,
        namespace=rules.trust_namespace,
    )

    spec = (
        _v4_schema_for(tuple(manifest.measurement_schema_versions))
        if manifest.schema_version == RESULT_BUNDLE_V4
        else None
    )
    try:
        parsed_measurement = (
            parse_fragment_measurement(content[MEASUREMENT_PATH])
            if spec is None
            else spec.parse_measurement(content[layout.measurement_path])
        )
    except Exception:
        raise BundleFormatError("invalid measurement") from None
    if type(parsed_measurement) is not rules.measurement_model:
        raise BundleFormatError("bundle version does not carry this measurement schema")
    chart_model: type[BaseModel] = FragmentLengthChart if spec is None else spec.chart_model
    chart = _parse_json(content[layout.chart_path], chart_model, "chart")
    parsed_provenance = _parse_json(
        content[PROVENANCE_PATH], ExportRunProvenance, "provenance"
    )
    limitations = _parse_json(
        content[LIMITATIONS_PATH], rules.limitations_model, "limitations"
    )
    assert isinstance(chart, chart_model)
    assert isinstance(parsed_provenance, ExportRunProvenance)
    assert isinstance(limitations, rules.limitations_model)
    measurement = validate_measurement(parsed_measurement)
    provenance = validate_provenance(parsed_provenance)
    _check_record_label(measurement, limitations)

    expected_chart = chart_for_measurement(
        measurement, _digest(content[layout.measurement_path])
    )
    if chart != expected_chart:
        raise BundleIntegrityError("chart does not derive from the canonical measurement")
    if content[REPORT_PATH] != render_bundle_report(measurement, limitations):
        raise BundleIntegrityError("report is not the approved rendering of measurement")
    if provenance.workflow_release_id != manifest.workflow_release_id:
        raise BundleIntegrityError("provenance does not identify the workflow release")
    if manifest.measurement_schema_versions != (measurement.schema_version,):
        raise BundleIntegrityError("manifest does not identify the measurement schema")
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

    _, manifest, _ = _read_bundle(Path(bundle_dir))
    return manifest


def bundle_measurement_path(manifest: BundleManifest) -> str:
    """The measurement path a (verified) manifest's layout names."""

    return _layout_for(manifest).measurement_path


def peek_measurement_path(manifest: object) -> str | None:
    """Unverified: the measurement path an untrusted manifest mapping would use.

    Only a pre-filter for peeks; ``None`` when the manifest names no layout
    this module reads.  Verification decides everything.
    """

    if not isinstance(manifest, Mapping):
        return None
    version = manifest.get("schema_version")
    if type(version) is not str:
        return None
    if version in _BUNDLE_VERSION_RULES:
        return MEASUREMENT_PATH
    schemas = manifest.get("measurement_schema_versions")
    if version != RESULT_BUNDLE_V4 or not isinstance(schemas, list) or len(schemas) != 1:
        return None
    spec = measurement_schema(schemas[0])
    return None if spec is None else spec.measurement_path


__all__ = [
    "LOCAL_RESULT_BUNDLE_VERSIONS",
    "RESULT_BUNDLE_SCHEMA_VERSIONS",
    "RESULT_BUNDLE_V4",
    "bundle_file_byte_limit",
    "bundle_measurement_path",
    "peek_measurement_path",
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
