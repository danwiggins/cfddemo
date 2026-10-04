"""Result-bundle v4 (signal methods SH3): paths and readers chosen by measurement schema.

Every schema here is a synthetic, test-only probe (``traceback.sh3-probe-*``);
the real cell-origin and copy-number contracts register themselves later
(CO3/CN3).  Every record is generated, unqualified, local and not for clinical
use.  The frozen v1/v2/v3 fixtures were built by the pre-v4 code on main.
"""

from __future__ import annotations

import hashlib
import html
import io
import json
import shutil
from contextlib import redirect_stdout
from pathlib import Path
from typing import Annotated, Any, Literal

import pytest
from pydantic import Field, StringConstraints

import traceback_runner.measurement_schemas as schemas_module
from evidence_inspector.result_catalog import (
    LOCAL_RESULT_BUNDLE_READER_REGISTRY,
    CatalogUnsupportedSchema,
    ResultBundleReader,
    ResultBundleReaderRegistry,
    ResultCatalog,
    _FIXED_FILES,
    _inventory,
)
from tests.test_bundles import _provenance
from tests.test_result_catalog import ALIASES, _authority, _bundle_method
from traceback_runner import cli
from traceback_runner.bundles import (
    CHECKSUMS_PATH,
    LIMITATIONS_PATH,
    MANIFEST_PATH,
    MEASUREMENT_PATH,
    PROVENANCE_PATH,
    REPORT_PATH,
    SIGNATURE_PATH,
    BundleFilesystemError,
    BundleFormatError,
    BundleIntegrityError,
    _FRAGMENT_LAYOUT,
    _checksums_bytes,
    _layout_for,
    _signing_payload,
    _v4_layout,
    build_result_bundle,
    bundle_file_byte_limit,
    inspect_bundle,
    peek_measurement_path,
    verify_bundle,
)
from traceback_runner.contracts import (
    ApprovalState,
    FragmentMeasurementV2,
    ResultBundleManifestV4,
    RunnerContract,
    canonical_json_bytes,
)
from traceback_runner.export import (
    LOCAL_REPORT_BANNER,
    ExportBoundaryError,
    ReferenceMatch,
    chart_for_measurement,
    render_bundle_report,
)
from traceback_runner.fixtures import create_local_golden_path_inputs
from traceback_runner.local_authority import ensure_local_method_authority
from traceback_runner.local_catalog import (
    _peek_local_record,
    _read_peek,
    load_explorer_artifacts,
    local_result_bundle_reader_registry,
    open_local_explorer,
)
from traceback_runner.measurement_schemas import (
    BundleMeasurementSchema,
    LocalCatalogBinding,
    MeasurementSchemaError,
    register_measurement_schema,
)
from traceback_runner.references import load_reference
from traceback_runner.signing import (
    DevelopmentSigningKey,
    KeyPurpose,
    TrustNamespace,
    TrustNamespaceError,
    TrustStore,
    generate_development_keypair,
    load_development_trust,
    sign_bytes,
)

FIXTURES = Path(__file__).parent / "fixtures" / "bundles"
FROZEN = ("v1-synthetic", "v2-synthetic", "v3-local")
LOCAL = TrustNamespace.DEVELOPMENT_LOCAL
_LOCAL_REGISTRY_SHA256 = "fa823c33d4a95c665404099d1817ddd75cff1716cf1ef3c7ad3c434d2209c0e2"

Id = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9.-]*$", max_length=64)]


# --------------------------------------------------------------------------
# A synthetic, test-only v4 measurement schema (and a second one, for crossing)
# --------------------------------------------------------------------------


class ProbeMeasurement(RunnerContract):
    schema_version: Literal["traceback.sh3-probe-measurement.v1"] = (
        "traceback.sh3-probe-measurement.v1"
    )
    approval_state: Literal[ApprovalState.UNAPPROVED_LOCAL]
    reference_id: Id
    reads_counted: int = Field(ge=1)
    values: tuple[int, ...] = Field(min_length=1)


class OtherProbeMeasurement(RunnerContract):
    schema_version: Literal["traceback.sh3-other-measurement.v1"] = (
        "traceback.sh3-other-measurement.v1"
    )
    approval_state: Literal[ApprovalState.UNAPPROVED_LOCAL]
    reference_id: Id
    reads_counted: int = Field(ge=1)
    values: tuple[int, ...] = Field(min_length=1)


class ProbeChart(RunnerContract):
    schema_version: Literal["traceback.sh3-probe-chart.v1"] = "traceback.sh3-probe-chart.v1"
    measurement_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    values: tuple[int, ...]


class ProbeLimitations(RunnerContract):
    schema_version: Literal["traceback.sh3-probe-limitations.v1"] = (
        "traceback.sh3-probe-limitations.v1"
    )
    template_id: Literal["local-sh3-probe-research-use.v1"] = "local-sh3-probe-research-use.v1"
    reference_match: ReferenceMatch


def _probe_chart(measurement: Any, sha256: str) -> ProbeChart:
    return ProbeChart(measurement_sha256=sha256, values=measurement.values)


def _probe_limitations(measurement: Any, reference_match: Any) -> ProbeLimitations:
    return ProbeLimitations(reference_match=reference_match)


def _probe_report(measurement: Any, limitations: Any) -> bytes:
    reference = html.escape(measurement.reference_id, quote=True)
    return (
        "<!doctype html><html lang=\"en\"><meta charset=\"utf-8\">"
        "<title>Traceback SH3 probe record</title>"
        f"<p role=\"note\"><strong>{LOCAL_REPORT_BANNER}</strong></p>"
        f"<h1>SH3 probe research record</h1><p>Reference: <code>{reference}</code>; "
        f"reads counted: {measurement.reads_counted}.</p></html>"
    ).encode("utf-8")


def _probe_denominator(verified: Any) -> Any:
    from evidence_inspector.result_view import (
        AttritionReason,
        AttritionStage,
        CountState,
        CountValue,
        DenominatorLedger,
    )

    reads = verified.measurement.reads_counted

    def observed(value: int, label: str) -> CountValue:
        return CountValue(state=CountState.OBSERVED, value=value, accessible_label=label)

    attrition = tuple(
        AttritionReason(
            stage=stage, reason_code="reason_none", accessible_label="None", count=observed(0, "None")
        )
        for stage in (AttritionStage.ACCEPTANCE, AttritionStage.DISPLAY, AttritionStage.ELIGIBILITY)
    )
    return DenominatorLedger(
        input_records=observed(reads, "Values counted"),
        accepted_records=observed(reads, "Values counted"),
        eligible_records=observed(reads, "Values counted"),
        displayed_records=observed(reads, "Values counted"),
        attrition=attrition,
    )


def _spec(
    *,
    schema_version: str = "traceback.sh3-probe-measurement.v1",
    path_stem: str = "sh3-probe.v1",
    model: type = ProbeMeasurement,
    result_schema_id: str = "schema_sh3_probe_measurement",
    **overrides: Any,
) -> BundleMeasurementSchema:
    values: dict[str, Any] = {
        "schema_version": schema_version,
        "path_stem": path_stem,
        "measurement_model": model,
        "chart_model": ProbeChart,
        "limitations_model": ProbeLimitations,
        "build_chart": _probe_chart,
        "build_limitations": _probe_limitations,
        "render_report": _probe_report,
        "catalog": LocalCatalogBinding(
            result_schema_id=result_schema_id,
            result_schema_version="1.0.0",
            accessible_label="SH3 probe, unqualified local record",
            normalization_semantics_id="sem_sh3_probe_values",
            coordinate_semantics_id="sem_sh3_probe_reference",
            denominator_semantics_id="sem_sh3_probe_count",
            # Test-only: the probe binds the fragment authority of its reference.
            authority=ensure_local_method_authority,
            denominator=_probe_denominator,
        ),
    }
    values.update(overrides)
    return BundleMeasurementSchema(**values)


def _other_spec() -> BundleMeasurementSchema:
    return _spec(
        schema_version="traceback.sh3-other-measurement.v1",
        path_stem="sh3-other.v1",
        model=OtherProbeMeasurement,
        result_schema_id="schema_sh3_other_measurement",
    )


@pytest.fixture
def probe(monkeypatch: pytest.MonkeyPatch) -> BundleMeasurementSchema:
    """Register the probe (and its sibling) in a registry private to this test."""

    monkeypatch.setattr(schemas_module, "_REGISTRY", {})
    register_measurement_schema(_other_spec())
    return register_measurement_schema(_spec())


def _probe_measurement(**updates: object) -> dict[str, object]:
    value: dict[str, object] = {
        "schema_version": "traceback.sh3-probe-measurement.v1",
        "approval_state": "unapproved_local",
        "reference_id": "ref-local",
        "reads_counted": 7,
        "values": [3, 4],
    }
    value.update(updates)
    return value


def _local_key() -> DevelopmentSigningKey:
    return generate_development_keypair(KeyPurpose.RESULT, namespace=LOCAL)


def _v4_bundle(
    path: Path,
    *,
    key: DevelopmentSigningKey | None = None,
    method: object = None,
    measurement: dict[str, object] | None = None,
    provenance: dict[str, object] | None = None,
) -> tuple[Path, DevelopmentSigningKey, TrustStore]:
    key = key or _local_key()
    store = TrustStore()
    store.add_signing_key(key)
    built = build_result_bundle(
        path,
        measurement=measurement or _probe_measurement(),
        provenance=provenance or _provenance(),
        method=method
        or {
            "method_id": "mth_fragment_aligned_reference_span",
            "version": "1.0.0",
            "method_definition_sha256": "c" * 64,
        },
        signing_key=key,
        reference_match="name_and_length_only",
    )
    return built, key, store


def _resign_v4(
    path: Path,
    key: DevelopmentSigningKey,
    spec: BundleMeasurementSchema,
    *,
    manifest_updates: dict[str, object] | None = None,
) -> None:
    """Rewrite the manifest inventory and re-sign over ``spec``'s v4 layout."""

    layout = _v4_layout(spec)
    manifest = json.loads((path / MANIFEST_PATH).read_bytes())
    manifest.update(manifest_updates or {})
    manifest["contents"] = [
        {
            "relative_path": relative,
            "sha256": hashlib.sha256((path / relative).read_bytes()).hexdigest(),
            "size_bytes": len((path / relative).read_bytes()),
        }
        for relative in sorted(layout.content_paths)
    ]
    (path / MANIFEST_PATH).write_bytes(canonical_json_bytes(manifest))
    content = {relative: (path / relative).read_bytes() for relative in layout.checksum_paths}
    checksums = _checksums_bytes(content, layout)
    (path / CHECKSUMS_PATH).write_bytes(checksums)
    signature = sign_bytes(
        canonical_json_bytes(_signing_payload(checksums, manifest["schema_version"], layout)),
        key,
        purpose=KeyPurpose.RESULT,
    )
    (path / SIGNATURE_PATH).write_bytes(canonical_json_bytes(signature))


def _tree_digests(path: Path) -> dict[str, str]:
    return {
        item.relative_to(path).as_posix(): hashlib.sha256(item.read_bytes()).hexdigest()
        for item in sorted(path.rglob("*"))
        if item.is_file()
    }


# --------------------------------------------------------------------------
# Golden: every frozen v1-v3 bundle verifies byte for byte, with or without v4
# --------------------------------------------------------------------------


def _frozen_digest_lines(name: str) -> dict[str, str]:
    lines = (FIXTURES / f"{name}.sha256").read_text().splitlines()
    return {path: digest for digest, path in (line.split("  ", 1) for line in lines)}


@pytest.mark.parametrize("name", FROZEN)
@pytest.mark.parametrize("registered", [False, True])
def test_frozen_v1_v3_bundles_verify_byte_for_byte(
    name: str, registered: bool, request: pytest.FixtureRequest
) -> None:
    if registered:
        request.getfixturevalue("probe")
    expected = _frozen_digest_lines(name)
    actual = {
        path.relative_to(FIXTURES).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in [FIXTURES / f"{name}.trust.json", *(FIXTURES / name).rglob("*")]
        if path.is_file()
    }
    assert actual == expected
    verified = verify_bundle(FIXTURES / name, load_development_trust(
        (FIXTURES / f"{name}.trust.json").read_bytes()
    ))
    version = {"v1-synthetic": "v1", "v2-synthetic": "v2", "v3-local": "v3"}[name]
    assert verified.manifest.schema_version == f"traceback.result-bundle.{version}"
    assert inspect_bundle(FIXTURES / name) == verified.manifest
    assert _layout_for(verified.manifest) == _FRAGMENT_LAYOUT
    # The current derivations reproduce the frozen bytes exactly.
    measurement_bytes = (FIXTURES / name / MEASUREMENT_PATH).read_bytes()
    assert canonical_json_bytes(verified.measurement) == measurement_bytes
    assert canonical_json_bytes(
        chart_for_measurement(verified.measurement, hashlib.sha256(measurement_bytes).hexdigest())
    ) == (FIXTURES / name / "charts/fragment-length.v1.json").read_bytes()
    assert render_bundle_report(verified.measurement, verified.limitations) == (
        FIXTURES / name / REPORT_PATH
    ).read_bytes()
    assert peek_measurement_path(json.loads((FIXTURES / name / MANIFEST_PATH).read_bytes())) == (
        MEASUREMENT_PATH
    )


def test_frozen_v3_content_rebuilds_byte_for_byte(probe, tmp_path: Path) -> None:
    """The v3 builder (with a v4 schema registered) writes the frozen content bytes."""

    frozen = FIXTURES / "v3-local"
    manifest = json.loads((frozen / MANIFEST_PATH).read_bytes())
    measurement = json.loads((frozen / MEASUREMENT_PATH).read_bytes())
    key = _local_key()
    built = build_result_bundle(
        tmp_path / "rebuilt",
        measurement=measurement,
        provenance=json.loads((frozen / PROVENANCE_PATH).read_bytes()),
        method=manifest["method"],
        signing_key=key,
        reference_match="name_and_length_only",
    )
    rebuilt = json.loads((built / MANIFEST_PATH).read_bytes())
    # Only the key differs: same record ID, same content inventory.
    assert rebuilt["record_id"] == manifest["record_id"]
    assert rebuilt["contents"] == manifest["contents"]
    for relative in (MEASUREMENT_PATH, "charts/fragment-length.v1.json", PROVENANCE_PATH,
                     LIMITATIONS_PATH, REPORT_PATH):
        assert (built / relative).read_bytes() == (frozen / relative).read_bytes()
    assert sorted(_tree_digests(built)) == sorted(_tree_digests(frozen))


@pytest.mark.parametrize("registered", [False, True])
def test_frozen_v3_bundle_imports_through_the_v3_reader(
    registered: bool, request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    if registered:
        request.getfixturevalue("probe")
    import_root = tmp_path / "imports"
    shutil.copytree(FIXTURES / "v3-local", import_root / "frozen")
    registry, _, _, _, head, head_sha256, capability = _authority()
    catalog = ResultCatalog(
        tmp_path / "catalog",
        import_roots={"root_primary": import_root},
        trust_store=load_development_trust((FIXTURES / "v3-local.trust.json").read_bytes()),
        reader_registry=local_result_bundle_reader_registry(),
    )
    reference = catalog.import_bundle(
        root_id="root_primary",
        relative_path="frozen",
        registry=registry,
        authority_head=head,
        expected_authority_head_sha256=head_sha256,
        capability=capability,
        aliases=ALIASES,
    )
    verified, reader = catalog.verify_reference(reference)
    assert reader.reader_id == "reader_result_bundle_v3"
    assert type(verified.measurement) is FragmentMeasurementV2


def test_catalog_inventory_of_a_v3_bundle_is_the_fixed_file_list() -> None:
    import os

    descriptor = os.open(FIXTURES / "v3-local", os.O_RDONLY)
    try:
        assert _inventory(descriptor) == _FIXED_FILES
    finally:
        os.close(descriptor)


def test_local_reader_registry_is_unchanged_without_a_v4_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(schemas_module, "_REGISTRY", {})
    registry = local_result_bundle_reader_registry()
    assert registry is LOCAL_RESULT_BUNDLE_READER_REGISTRY
    assert hashlib.sha256(canonical_json_bytes(registry)).hexdigest() == _LOCAL_REGISTRY_SHA256


# --------------------------------------------------------------------------
# v4: build, verify, layout chosen by schema
# --------------------------------------------------------------------------


def test_v4_bundle_builds_at_the_schema_paths_and_verifies(probe, tmp_path: Path) -> None:
    path, key, store = _v4_bundle(tmp_path / "record")
    files = set(_tree_digests(path))
    assert files == {
        "bundle-manifest.json", "bundle.sig", "checksums.sha256", "limitations.json",
        "provenance.json", "report.html",
        "measurements/sh3-probe.v1.json", "charts/sh3-probe.v1.json",
    }
    verified = verify_bundle(path, store)
    assert type(verified.manifest) is ResultBundleManifestV4
    assert verified.manifest.measurement_schema_versions == (probe.schema_version,)
    assert type(verified.measurement) is ProbeMeasurement
    assert type(verified.chart) is ProbeChart
    assert verified.limitations == ProbeLimitations(reference_match="name_and_length_only")
    assert verified.signature.namespace == LOCAL
    assert LOCAL_REPORT_BANNER.encode() in (path / REPORT_PATH).read_bytes()
    assert json.loads((path / SIGNATURE_PATH).read_bytes())["key_id"] == key.key_id
    assert inspect_bundle(path) == verified.manifest
    assert peek_measurement_path(json.loads((path / MANIFEST_PATH).read_bytes())) == (
        "measurements/sh3-probe.v1.json"
    )
    # The v4 reader is selected by (4, (schema,)), never by version alone.
    reader = local_result_bundle_reader_registry().select(verified)
    assert reader.reader_id == "reader_result_bundle_v4_sh3_probe_measurement"


def test_v4_bundle_requires_a_local_key_and_a_reference_match(probe, tmp_path: Path) -> None:
    synthetic = generate_development_keypair(KeyPurpose.RESULT)
    with pytest.raises(BundleFormatError, match="development-local"):
        _v4_bundle(tmp_path / "a", key=synthetic)
    with pytest.raises(BundleFormatError, match="reference match"):
        build_result_bundle(
            tmp_path / "b",
            measurement=_probe_measurement(),
            provenance=_provenance(),
            method={"method_id": "m", "version": "1", "method_definition_sha256": "c" * 64},
            signing_key=_local_key(),
        )


def test_v4_bundle_re_signed_by_a_synthetic_key_fails_on_namespace(
    probe, tmp_path: Path
) -> None:
    path, _, _ = _v4_bundle(tmp_path / "record")
    synthetic = generate_development_keypair(KeyPurpose.RESULT)
    store = TrustStore()
    store.add_signing_key(synthetic)
    _resign_v4(path, synthetic, probe, manifest_updates={"signing_key_id": synthetic.key_id})
    with pytest.raises(TrustNamespaceError):
        verify_bundle(path, store)


def test_v4_record_ids_bind_the_measurement(probe, tmp_path: Path) -> None:
    first, _, _ = _v4_bundle(tmp_path / "a")
    second, _, _ = _v4_bundle(tmp_path / "b", measurement=_probe_measurement(values=[5]))
    ids = {json.loads((p / MANIFEST_PATH).read_bytes())["record_id"] for p in (first, second)}
    assert len(ids) == 2


def test_v4_bundle_with_a_mismatched_measurement_path_is_refused(
    probe, tmp_path: Path
) -> None:
    """The probe measurement moved to the sibling schema's paths, consistently re-signed."""

    path, key, store = _v4_bundle(tmp_path / "record")
    other = _other_spec()
    (path / "measurements/sh3-probe.v1.json").rename(path / other.measurement_path)
    (path / "charts/sh3-probe.v1.json").rename(path / other.chart_path)
    # Re-signed over the sibling layout while the manifest still names the probe.
    _resign_v4(path, key, other)
    with pytest.raises(BundleFilesystemError, match="does not match its manifest"):
        verify_bundle(path, store)


def test_v4_bundle_claiming_the_sibling_schema_at_its_paths_is_refused(
    probe, tmp_path: Path
) -> None:
    """Paths and manifest name the sibling; the bytes are a probe measurement."""

    path, key, store = _v4_bundle(tmp_path / "record")
    other = _other_spec()
    (path / "measurements/sh3-probe.v1.json").rename(path / other.measurement_path)
    (path / "charts/sh3-probe.v1.json").rename(path / other.chart_path)
    _resign_v4(
        path, key, other,
        manifest_updates={"measurement_schema_versions": [other.schema_version]},
    )
    with pytest.raises(BundleFormatError, match="invalid measurement"):
        verify_bundle(path, store)


def test_v4_bundle_with_the_fragment_paths_is_refused(probe, tmp_path: Path) -> None:
    path, key, store = _v4_bundle(tmp_path / "record")
    (path / "measurements/sh3-probe.v1.json").rename(path / MEASUREMENT_PATH)
    (path / "charts/sh3-probe.v1.json").rename(path / "charts/fragment-length.v1.json")
    with pytest.raises(BundleFilesystemError, match="does not match its manifest"):
        verify_bundle(path, store)
    with pytest.raises(BundleFormatError, match="never travel in v4"):
        _signing_payload(b"x", "traceback.result-bundle.v4", _FRAGMENT_LAYOUT)


def test_v4_bundle_with_two_measurements_is_refused(probe, tmp_path: Path) -> None:
    path, key, store = _v4_bundle(tmp_path / "record")
    other = _other_spec()
    sibling, _, _ = _v4_bundle(
        tmp_path / "sibling",
        measurement=_probe_measurement(schema_version=other.schema_version),
    )
    for relative in (other.measurement_path, other.chart_path):
        shutil.copyfile(sibling / relative, path / relative)
    _resign_v4(
        path, key, probe,
        manifest_updates={
            "measurement_schema_versions": sorted([probe.schema_version, other.schema_version])
        },
    )
    with pytest.raises(BundleFormatError, match="exactly one measurement"):
        verify_bundle(path, store)
    # The layout guard on its own (the manifest model admits the 2-tuple).
    manifest = ResultBundleManifestV4.model_validate_json((path / MANIFEST_PATH).read_bytes())
    with pytest.raises(BundleFormatError, match="exactly one measurement"):
        _layout_for(manifest)
    assert peek_measurement_path(json.loads((path / MANIFEST_PATH).read_bytes())) is None


def test_v4_bundle_without_a_registered_schema_is_refused(
    probe, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, _, store = _v4_bundle(tmp_path / "record")
    monkeypatch.setattr(schemas_module, "_REGISTRY", {})
    with pytest.raises(BundleFilesystemError, match="unexpected file"):
        verify_bundle(path, store)


def test_v4_manifest_naming_a_fragment_schema_is_refused(probe, tmp_path: Path) -> None:
    path, key, store = _v4_bundle(tmp_path / "record")
    manifest = json.loads((path / MANIFEST_PATH).read_bytes())
    manifest["measurement_schema_versions"] = ["traceback.fragment-measurement.v2"]
    (path / MANIFEST_PATH).write_bytes(canonical_json_bytes(manifest))
    with pytest.raises(BundleFormatError, match="not registered"):
        verify_bundle(path, store)


def test_v3_bundle_with_v4_paths_is_refused(probe, tmp_path: Path) -> None:
    v3 = tmp_path / "v3"
    shutil.copytree(FIXTURES / "v3-local", v3)
    shutil.copyfile(
        FIXTURES / "v3-local" / MEASUREMENT_PATH, v3 / "measurements/sh3-probe.v1.json"
    )
    trust = load_development_trust((FIXTURES / "v3-local.trust.json").read_bytes())
    with pytest.raises(BundleFilesystemError, match="does not match its manifest"):
        verify_bundle(v3, trust)
    with pytest.raises(BundleFormatError, match="fragment-length paths"):
        _signing_payload(b"x", "traceback.result-bundle.v3", _v4_layout(probe))


def test_v4_tampered_chart_or_report_is_refused(probe, tmp_path: Path) -> None:
    for relative, error in (
        ("charts/sh3-probe.v1.json", BundleIntegrityError),
        (REPORT_PATH, BundleIntegrityError),
    ):
        path, key, store = _v4_bundle(tmp_path / relative.replace("/", "-"))
        content = (path / relative).read_bytes()
        (path / relative).write_bytes(content.replace(b"3", b"9", 1))
        _resign_v4(path, key, probe)
        with pytest.raises(error):
            verify_bundle(path, store)


def test_v4_byte_bounds_are_the_schema_bounds(
    probe, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, _, store = _v4_bundle(tmp_path / "record")
    size = (path / probe.measurement_path).stat().st_size
    small = _spec(max_measurement_bytes=size - 1)
    monkeypatch.setattr(schemas_module, "_REGISTRY", {})
    register_measurement_schema(small)
    assert bundle_file_byte_limit(small.measurement_path) == size - 1
    with pytest.raises(BundleFilesystemError, match="byte limit"):
        verify_bundle(path, store)


def test_v4_report_must_carry_the_banner_and_no_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(schemas_module, "_REGISTRY", {})
    register_measurement_schema(_spec(render_report=lambda m, lim: b"<p>no banner</p>"))
    measurement = ProbeMeasurement.model_validate(_probe_measurement())
    limitations = ProbeLimitations(reference_match="registered_digests")
    with pytest.raises(ExportBoundaryError, match="banner"):
        render_bundle_report(measurement, limitations)
    monkeypatch.setattr(schemas_module, "_REGISTRY", {})
    register_measurement_schema(
        _spec(render_report=lambda m, lim: _probe_report(m, lim) + b"<p>healthy</p>")
    )
    with pytest.raises(ExportBoundaryError, match="claim"):
        render_bundle_report(measurement, limitations)


def test_registration_refuses_fragment_reserved_and_duplicate_schemas(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(schemas_module, "_REGISTRY", {})
    with pytest.raises(MeasurementSchemaError, match="fragment-length"):
        register_measurement_schema(_spec(schema_version="traceback.fragment-measurement.v2"))
    with pytest.raises(MeasurementSchemaError, match="reserved"):
        register_measurement_schema(_spec(path_stem="fragment-length.v1"))
    with pytest.raises(MeasurementSchemaError, match="malformed"):
        register_measurement_schema(_spec(path_stem="../escape.v1"))
    with pytest.raises(MeasurementSchemaError, match="default"):
        register_measurement_schema(_spec(schema_version="traceback.sh3-unmatched.v1"))
    spec = register_measurement_schema(_spec())
    assert register_measurement_schema(spec) is spec
    with pytest.raises(MeasurementSchemaError, match="already registered"):
        register_measurement_schema(_spec())
    with pytest.raises(MeasurementSchemaError, match="stem is already"):
        register_measurement_schema(
            _spec(
                schema_version="traceback.sh3-other-measurement.v1",
                model=OtherProbeMeasurement,
                result_schema_id="schema_sh3_other_measurement",
            )
        )
    with pytest.raises(MeasurementSchemaError, match="byte bounds"):
        register_measurement_schema(_spec(max_chart_bytes=17 * 1024 * 1024))


# --------------------------------------------------------------------------
# Reader selection by (bundle_version, measurement_schema)
# --------------------------------------------------------------------------


def test_reader_registry_selects_by_version_and_schema(probe, tmp_path: Path) -> None:
    registry = local_result_bundle_reader_registry()
    assert [(r.reader_id, r.minimum_version) for r in registry.readers] == [
        ("reader_result_bundle_v2", 2),
        ("reader_result_bundle_v3", 3),
        ("reader_result_bundle_v4_sh3_other_measurement", 4),
        ("reader_result_bundle_v4_sh3_probe_measurement", 4),
    ]
    path, _, store = _v4_bundle(tmp_path / "probe")
    other, _, other_store = _v4_bundle(
        tmp_path / "other",
        measurement=_probe_measurement(schema_version="traceback.sh3-other-measurement.v1"),
    )
    assert registry.select(verify_bundle(path, store)).reader_id.endswith("sh3_probe_measurement")
    assert registry.select(verify_bundle(other, other_store)).reader_id.endswith(
        "sh3_other_measurement"
    )
    only_other = ResultBundleReaderRegistry(
        readers=(*LOCAL_RESULT_BUNDLE_READER_REGISTRY.readers, registry.readers[2])
    )
    with pytest.raises(CatalogUnsupportedSchema, match="measurement schema"):
        only_other.select(verify_bundle(path, store))


def test_reader_registry_refuses_two_readers_for_one_schema_and_version() -> None:
    duplicate = ResultBundleReader(
        reader_id="reader_result_bundle_v3_again",
        minimum_version=3,
        maximum_version=3,
        measurement_schema_versions=("traceback.fragment-measurement.v2",),
    )
    with pytest.raises(ValueError, match="cannot overlap"):
        ResultBundleReaderRegistry(
            readers=(*LOCAL_RESULT_BUNDLE_READER_REGISTRY.readers, duplicate)
        )


def test_v4_bundle_imports_through_its_schema_reader(probe, tmp_path: Path) -> None:
    import_root = tmp_path / "imports"
    registry, _, _, _, head, head_sha256, capability = _authority()
    _, _, store = _v4_bundle(import_root / "incoming", method=_bundle_method(capability))
    catalog = ResultCatalog(
        tmp_path / "catalog",
        import_roots={"root_primary": import_root},
        trust_store=store,
        reader_registry=local_result_bundle_reader_registry(),
    )
    reference = catalog.import_bundle(
        root_id="root_primary",
        relative_path="incoming",
        registry=registry,
        authority_head=head,
        expected_authority_head_sha256=head_sha256,
        capability=capability,
        aliases=ALIASES,
    )
    verified, reader = catalog.verify_reference(reference)
    assert reader.reader_id == "reader_result_bundle_v4_sh3_probe_measurement"
    assert type(verified.measurement) is ProbeMeasurement


def test_catalog_refuses_a_v4_bundle_whose_names_differ(probe, tmp_path: Path) -> None:
    import os

    path, _, _ = _v4_bundle(tmp_path / "record")
    (path / "charts/sh3-probe.v1.json").rename(path / "charts/sh3-other.v1.json")
    descriptor = os.open(path, os.O_RDONLY)
    try:
        with pytest.raises(Exception, match="inventory is not exact"):
            _inventory(descriptor)
    finally:
        os.close(descriptor)


# --------------------------------------------------------------------------
# Peeks: chosen per schema, bounded exactly as verification is
# --------------------------------------------------------------------------


def test_peek_bound_equals_the_verifier_bound(tmp_path: Path) -> None:
    bundle = tmp_path / "record"
    (bundle / "measurements").mkdir(parents=True)
    target = bundle / MEASUREMENT_PATH
    limit = bundle_file_byte_limit(MEASUREMENT_PATH)
    assert limit == 16 * 1024 * 1024
    # A measurement verification accepts (over the old 4 MiB peek bound) peeks.
    target.write_bytes(b"x" * (4 * 1024 * 1024 + 1))
    assert len(_read_peek(target)) == 4 * 1024 * 1024 + 1
    target.write_bytes(b"x" * (limit + 1))
    with pytest.raises(ValueError, match="byte bound"):
        _read_peek(target)
    manifest = bundle / MANIFEST_PATH
    manifest.write_bytes(b"x" * (bundle_file_byte_limit(MANIFEST_PATH) + 1))
    with pytest.raises(ValueError, match="byte bound"):
        _read_peek(manifest)


def test_peek_local_record_reads_v3_and_v4(probe, tmp_path: Path) -> None:
    assert _peek_local_record(FIXTURES / "v3-local")[1] == "ref-local"
    path, key, _ = _v4_bundle(tmp_path / "record")
    record_id, reference_id, key_id = _peek_local_record(path)
    assert reference_id == "ref-local"
    assert key_id == key.key_id
    assert record_id == json.loads((path / MANIFEST_PATH).read_bytes())["record_id"]


# --------------------------------------------------------------------------
# A mixed ROOT: a v3 fragment record and v4 probe records side by side
# --------------------------------------------------------------------------


def _main(*argv: object) -> tuple[int, dict]:
    stream = io.StringIO()
    with redirect_stdout(stream):
        code = cli.main([*map(str, argv), "--json"])
    return code, json.loads(stream.getvalue())


@pytest.fixture(scope="module")
def fragment_root(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, str]:
    work = tmp_path_factory.mktemp("mixed-root")
    inputs = create_local_golden_path_inputs(work / "inputs")
    root = work / "root"
    assert _main("reference", "register", "--fasta", inputs.fasta_path, "--id", "ref",
                 "--root", root)[0] == 0
    code, payload = _main("run", inputs.bam_path, "--reference", "ref", "--root", root)
    assert code == 0, payload
    return root, payload["data"]["record_id"]


def _publish_probe(root: Path, name: str, **updates: object) -> str:
    """Sign a probe record with ROOT's local key under the fragment authority."""

    registered = load_reference(root, "ref").registered
    capability = ensure_local_method_authority(root, registered).capability
    staging = root / "staging-probe" / name
    _v4_bundle(
        staging,
        key=cli._local_signing_key(root),
        method=_bundle_method(capability),
        measurement=_probe_measurement(reference_id="ref", **updates),
        provenance=_provenance(run_token=f"synthetic.{name}.v1"),
    )
    record_id = json.loads((staging / MANIFEST_PATH).read_bytes())["record_id"]
    shutil.move(str(staging), root / "records" / record_id)
    return record_id


def test_a_mixed_root_imports_every_record(
    probe, fragment_root: tuple[Path, str], tmp_path: Path
) -> None:
    source, fragment_record = fragment_root
    root = tmp_path / "root"
    shutil.copytree(source, root, symlinks=True)
    probe_record = _publish_probe(root, "a")

    for record in (fragment_record, probe_record):
        code, payload = _main("catalog", "import", root / "records" / record, "--root", root)
        assert code == cli.ExitCode.OK, json.dumps(payload)
    # Re-importing the v3 record under the v4-extended reader registry is a no-op.
    code, payload = _main("catalog", "import", root / "records" / fragment_record, "--root", root)
    assert code == cli.ExitCode.OK, payload
    assert payload["data"]["explorer_artifact"] == "unchanged"

    with open_local_explorer(root) as explorer:
        assert explorer is not None
        assert explorer.views == 2
        assert explorer.skipped == 0
        rows = {row.result_id for row in explorer.catalog.query(_query()).results}
    loaded = load_explorer_artifacts(root)
    assert {record.result_id for record in loaded.records} == rows
    schemas = {
        record.result_view_request.sources[0].record.compatibility_key.result_schema.schema_id
        for record in loaded.records
    }
    assert schemas == {"schema_fragment_measurement", "schema_sh3_probe_measurement"}

    code, payload = _main("verify", probe_record, "--root", root)
    assert code == cli.ExitCode.OK, payload


def test_a_v3_record_imported_before_registration_still_serves_after(
    fragment_root: tuple[Path, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, fragment_record = fragment_root
    root = tmp_path / "root"
    shutil.copytree(source, root, symlinks=True)
    monkeypatch.setattr(schemas_module, "_REGISTRY", {})
    assert _main("catalog", "import", root / "records" / fragment_record, "--root", root)[0] == 0
    register_measurement_schema(_spec())
    probe_record = _publish_probe(root, "late")
    assert _main("catalog", "import", root / "records" / probe_record, "--root", root)[0] == 0
    with open_local_explorer(root) as explorer:
        assert explorer is not None and explorer.views == 2 and explorer.skipped == 0


def test_v4_twins_group_by_their_own_measurement(
    probe, fragment_root: tuple[Path, str], tmp_path: Path
) -> None:
    source, fragment_record = fragment_root
    root = tmp_path / "root"
    shutil.copytree(source, root, symlinks=True)
    first = _publish_probe(root, "first")
    second = _publish_probe(root, "second")
    third = _publish_probe(root, "third", values=[9])
    assert len({first, second, third}) == 3
    digest = cli._peek_measurement_sha256(root / "records" / first)
    assert digest == hashlib.sha256(
        (root / "records" / first / probe.measurement_path).read_bytes()
    ).hexdigest()
    twins = cli._measurement_twins(root, None)
    # Same measurement, different provenance: one maps to the other (by record ID
    # without a job store); the fragment record and the distinct probe do not.
    assert set(twins) | set(twins.values()) == {first, second}
    assert fragment_record not in twins and third not in twins


def _query() -> Any:
    from evidence_inspector.result_catalog import CatalogQuery

    return CatalogQuery()
