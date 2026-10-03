"""H5 validator: identifier patterns, the shared key grammar, and the R2 guard.

The R2 regression guard runs the extended validator over every E07-E14 public
projection the existing tests build, including the E13 portable ``source_id``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evidence_inspector.portable_view import build_portable_view
from evidence_inspector.sensitivity_comparison import (
    build_sensitivity_comparison_artifact,
)
from tests.test_portable_view import (  # noqa: F401 - pytest fixtures
    integrated_request,
    trust_context,
)
from tests.test_sensitivity_comparison import _study_bundle
from traceback_runner.web import contracts, longitudinal
from traceback_runner.web.contracts import (
    PROTECTED_PUBLIC_KEYS,
    validate_public_key,
    validate_public_projection,
    validate_public_text,
)

# --- values ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "subject id 42",
        "Specimen ID S-17",
        "collection_id 9",
        "run-id 3",
        "provider identifier 12",
        "flowcell id FAB12345",
        "MRN 1234567",
        "mrn:#00042",
        "dob 1970-01-01",
        "Date of birth: 1970-01-01",
        "born 1970-01-01",
        "birthdate 1970-01-01",
        # The existing rules still hold.
        "source id 1",
        "patient_id 7",
    ],
)
def test_identifier_shaped_text_is_rejected(text: str) -> None:
    with pytest.raises(ValueError, match="private identifier"):
        validate_public_text(text)


@pytest.mark.parametrize(
    "text",
    [
        "Run state complete",
        "Collection window 2026-01-01",
        "Subject to exact compatibility",
        "Provider pilot setup",
        "MRN pending",
        "mrn 1234",
        "Born digital record",
        "Generated 1970-01-01",
    ],
)
def test_ordinary_public_text_still_passes(text: str) -> None:
    assert validate_public_text(text) == text


# --- keys --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "key",
    [
        "specimen_id",
        "subject_id",
        "collection_id",
        "run_id",
        "provider_id",
        "flowcell_id",
        "donor_identifier",
        "sample_ids",
        "left_patient_id",
        "subject_token",
        "mrn",
        "dob",
        "date_of_birth",
        "birthdate",
    ],
)
def test_identifier_keys_are_rejected(key: str) -> None:
    with pytest.raises(ValueError):
        validate_public_key(key)
    with pytest.raises(ValueError):
        validate_public_projection({"outer": [{key: "ok"}]})


@pytest.mark.parametrize("key", ["Source", "a-b", "x" * 65, "", "a.b", 1, None])
def test_keys_outside_the_grammar_are_rejected(key: object) -> None:
    with pytest.raises(ValueError, match="controlled name"):
        validate_public_projection({key: "ok"})


@pytest.mark.parametrize("key", sorted(PROTECTED_PUBLIC_KEYS))
def test_protected_keys_are_rejected(key: str) -> None:
    with pytest.raises(ValueError, match="protected field"):
        validate_public_projection({"nested": {key: 1}})


def test_public_keys_that_name_opaque_tokens_pass() -> None:
    # ``source_id`` is the E13 portable view's controlled opaque token; key
    # validation must not run the value regex over it (review finding S11).
    validate_public_projection(
        {
            "source_id": "source_alpha",
            "result_id": "result_" + "a" * 40,
            "left_result_id": "result_" + "b" * 40,
            "query": {"limit": 1},
            "run_state": "complete",
        }
    )
    # The value under ``source_id`` is still checked as public text.
    with pytest.raises(ValueError):
        validate_public_projection({"source_id": "source id 42"})


def test_longitudinal_reuses_the_shared_grammar_with_its_extra_names() -> None:
    # One grammar for both boundaries; E12 adds names that are public in E14.
    assert longitudinal.validate_public_key is validate_public_key
    assert PROTECTED_PUBLIC_KEYS < longitudinal._PROTECTED_KEYS
    # Pinned: the E12 protected set is exactly what #93 shipped.
    assert longitudinal._PROTECTED_KEYS == frozenset(
        {
            "protected_rows", "protected_only", "reader_authorization",
            "dependency_heads", "live_dependency_heads", "member", "member_sha256",
            "member_result_id", "linkage_id", "subject_token", "collection_token",
            "specimen_token", "analysis_record_id", "run_token", "provider_namespace",
            "time_coordinate", "time_coordinate_sha256", "biological_timepoint_id",
            "committed_receipt_sha256", "grant_sha256", "reader_grant_sha256",
            "state_head_sha256_binding", "saved_object_json", "credential",
            "session_token", "csrf_token", "result_id", "bundle_id",
        }
    )
    with pytest.raises(ValueError, match="protected field"):
        longitudinal.validate_longitudinal_public({"result_id": "x"})
    with pytest.raises(ValueError):
        longitudinal.validate_longitudinal_public({"specimen_id": "x"})
    validate_public_projection({"result_id": "result_" + "c" * 40})


# --- R2: every existing E07-E14 public projection still validates -------------------


def _keys(value: object) -> set[str]:
    if isinstance(value, dict):
        return set(value).union(*(_keys(item) for item in value.values()))
    if isinstance(value, list):
        return set().union(*(_keys(item) for item in value))
    return set()


def _strings(value: object) -> list[str]:
    if isinstance(value, dict):
        return [text for item in value.values() for text in _strings(item)]
    if isinstance(value, list):
        return [text for item in value for text in _strings(item)]
    return [value] if isinstance(value, str) else []


def _assert_h5_rules_accept(payload: object) -> None:
    """The H5 additions alone (keys and the new value patterns) accept ``payload``.

    Used for build artifacts that carry fields (bundle-relative paths, base64
    signatures) the pre-H5 value rules already reject; H5 must not add to that.
    """

    for key in _keys(payload):
        validate_public_key(key)
    for text in _strings(payload):
        decoded = contracts._decoded_public_text(text)
        assert not contracts._IDENTIFIER_TEXT.search(decoded), text
        assert not contracts._MRN_TEXT.search(decoded), text
        assert not contracts._DATE_OF_BIRTH_TEXT.search(decoded), text


def test_e07_to_e13_projections_built_by_existing_tests_still_validate(
    integrated_request, trust_context  # noqa: F811 - pytest fixtures
) -> None:
    # E13 portable view: the full public projection, including ``source_id``.
    view, _ = build_portable_view(integrated_request, trust_context=trust_context)
    portable = view.model_dump(mode="json")
    assert "source_id" in _keys(portable)
    validate_public_projection(portable)
    # E09 sensitivity comparison artifact.
    sensitivity = build_sensitivity_comparison_artifact(_study_bundle())
    validate_public_projection(sensitivity.model_dump(mode="json"))
    # E06-E11 build artifacts the portable view is projected from.
    for model in (
        integrated_request.surface_fixture.view,  # E06 result view
        integrated_request.fragment_view,  # E07 fragment explorer
        integrated_request.provenance_drawer,  # E08 provenance drawer
        integrated_request.cell_origin_artifact,  # E10 cell origin
        integrated_request.cna_snapshot,  # E11 copy number
    ):
        _assert_h5_rules_accept(model.model_dump(mode="json"))


def test_e14_explorer_document_and_catalog_still_validate(tmp_path: Path) -> None:
    from evidence_inspector.result_catalog import CatalogQuery
    from tests.web.test_integrated_explorer import _installed

    catalog, ref, _, _, explorer = _installed(tmp_path)
    try:
        document = explorer.get(ref.result_id).model_dump(mode="json")
        validate_public_projection(document)
        page = explorer.query(CatalogQuery(limit=1)).model_dump(mode="json")
        validate_public_projection(page)
    finally:
        catalog.close()


def test_e14_public_ui_fixture_still_validates() -> None:
    fixture = Path(__file__).parents[1] / "fixtures/product_gates/screenshot_manifest.json"
    validate_public_projection(json.loads(fixture.read_text()))
