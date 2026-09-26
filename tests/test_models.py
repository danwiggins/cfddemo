"""Contract and immutable case-bundle tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from evidence_inspector.case_bundle import (
    CaseBundleLoadError,
    SYNTHETIC_LENGTHS,
    SYNTHETIC_LENGTH_ARTIFACT_ID,
    build_synthetic_case,
    load_case_bundle,
    load_default_case,
)
from evidence_inspector.models import (
    AuditResult,
    AuditStatus,
    CheckSelection,
    ExecutionMode,
    NumericAssertion,
    ReadLengthSummaryValues,
    ReferenceRangeComparisonValues,
    Source,
    ToolName,
    VerificationLevel,
    audit_is_stale,
    bind_claim,
    build_case,
    canonical_dataset_revision,
    resolve_sources,
    revise_claim,
    sha256_text,
    validate_audit_result,
    validate_check_selection,
    validate_numeric_assertions,
)

FIXTURES = Path(__file__).parent / "fixtures"


def test_synthetic_case_is_complete_private_data_free_and_hand_countable() -> None:
    case = build_synthetic_case()

    assert len(case.claims) == 3
    assert len(case.sources) <= 12
    assert SYNTHETIC_LENGTHS == (100, 150, 151, 151, 167, 167, 167, 1001)
    assert json.loads((FIXTURES / "synthetic_lengths.json").read_text()) == list(
        SYNTHETIC_LENGTHS
    )
    serialized = case.model_dump_json()
    assert "/Users/" not in serialized
    assert "Downloads" not in serialized
    assert "cfDNA_baseline" not in serialized
    assert all("synthetic" in source.document_id for source in case.sources)


def test_models_forbid_unexpected_fields_and_invalid_source_digest() -> None:
    case = build_synthetic_case()
    payload = case.claims[0].model_dump()
    payload["path"] = "/not/allowed"
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        type(case.claims[0]).model_validate(payload)

    source_payload = case.sources[0].model_dump()
    source_payload["quote"] = "tampered"
    with pytest.raises(ValidationError, match="content_sha256"):
        Source.model_validate(source_payload)


def test_dataset_revision_is_canonical_and_changes_with_evidence_or_config() -> None:
    case = build_synthetic_case()
    reversed_revision = canonical_dataset_revision(
        case.manifest, tuple(reversed(case.sources))
    )
    assert reversed_revision == case.dataset_revision

    changed_manifest = case.manifest.model_copy(
        update={
            "partial_collection": False,
        }
    )
    assert (
        canonical_dataset_revision(changed_manifest, case.sources)
        != case.dataset_revision
    )

    original_source = case.sources[-1]
    changed_source = Source.from_content(
        id=original_source.id,
        document_id=original_source.document_id,
        locator=original_source.locator,
        quote="changed content with a matching source digest",
    )
    with pytest.raises(ValidationError, match="dataset_revision"):
        type(case).model_validate(
            case.model_dump()
            | {"sources": case.sources[:-1] + (changed_source,)}
        )


def test_build_case_rejects_unknown_claim_sources_and_nonexact_quote() -> None:
    case = build_synthetic_case()
    bad_claim = case.claims[0].model_copy(update={"source_ids": ("missing",)})
    with pytest.raises(ValidationError, match="unknown sources"):
        build_case(
            case_id=case.case_id,
            dataset_id=case.dataset_id,
            manifest=case.manifest,
            sources=case.sources,
            claims=(bad_claim,) + case.claims[1:],
            tool_results=case.tool_results,
        )

    bad_quote = case.claims[0].model_copy(update={"original_quote": "paraphrase"})
    with pytest.raises(ValidationError, match="exactly match"):
        build_case(
            case_id=case.case_id,
            dataset_id=case.dataset_id,
            manifest=case.manifest,
            sources=case.sources,
            claims=(bad_quote,) + case.claims[1:],
            tool_results=case.tool_results,
        )


def test_source_resolution_is_exact_id_only() -> None:
    case = build_synthetic_case()
    source = resolve_sources(case, ("source.synthetic-method-current",))[0]
    assert source.id == "source.synthetic-method-current"

    with pytest.raises(KeyError, match="unknown source ID"):
        resolve_sources(case, ("current method",))
    with pytest.raises(ValueError, match="unique"):
        resolve_sources(case, (source.id, source.id))


def test_claim_revision_hash_binding_and_staleness() -> None:
    case = build_synthetic_case()
    original = case.claims[0]
    binding = bind_claim(original, case.dataset_revision)

    assert binding.claim_text_hash == sha256_text(original.text)
    assert revise_claim(original, original.text) is original

    edited = revise_claim(original, original.text + " Edited.")
    assert edited.revision == original.revision + 1
    assert edited.original_quote == original.original_quote

    audit = _audit(case)
    assert not audit_is_stale(
        audit, claim=original, dataset_revision=case.dataset_revision
    )
    assert audit_is_stale(
        audit, claim=edited, dataset_revision=case.dataset_revision
    )
    assert audit_is_stale(audit, claim=original, dataset_revision="0" * 64)


def test_check_selection_shape_and_case_scope() -> None:
    case = build_synthetic_case()
    selection = CheckSelection(
        tool=ToolName.READ_LENGTH_SUMMARY,
        reason="The assertion is an exact query-length measurement.",
        artifact_ids=(SYNTHETIC_LENGTH_ARTIFACT_ID,),
    )
    assert validate_check_selection(selection, case) is selection

    source_selection = CheckSelection(
        tool=ToolName.SOURCE_REVIEW,
        reason="Compare two registered method passages.",
        source_ids=(
            "source.synthetic-method-current",
            "source.synthetic-method-instructions",
        ),
    )
    assert validate_check_selection(source_selection, case) is source_selection

    foreign = selection.model_copy(update={"artifact_ids": ("foreign-artifact",)})
    with pytest.raises(ValueError, match="outside"):
        validate_check_selection(foreign, case)

    with pytest.raises(ValidationError, match="requires source_ids"):
        CheckSelection(
            tool=ToolName.SOURCE_REVIEW,
            reason="Missing explicit source IDs.",
        )

    insufficient = CheckSelection(
        tool=ToolName.INSUFFICIENT_INPUTS,
        reason="No applicable evidence exists.",
    )
    assert validate_check_selection(insufficient, case) is insufficient


def test_typed_tool_values_reject_broken_arithmetic_and_range_labels() -> None:
    case = build_synthetic_case()
    values = case.tool_results[0].values
    assert ReadLengthSummaryValues.model_validate(values).median_bp == 159

    bad_lengths = dict(values)
    bad_lengths["fraction_gt_1000"] = 0.5
    with pytest.raises(ValidationError, match="fraction_gt_1000"):
        ReadLengthSummaryValues.model_validate(bad_lengths)

    valid_ranges = {
        "rows": (
            {
                "cell_type_id": "boundary",
                "fraction": 0.2,
                "min_fraction": 0.2,
                "max_fraction": 0.3,
                "classification": "within",
                "source_ids": ("source.synthetic-table",),
            },
        ),
        "partial_table": False,
    }
    assert (
        ReferenceRangeComparisonValues.model_validate(valid_ranges)
        .rows[0]
        .classification
        == "within"
    )
    valid_ranges["rows"][0]["classification"] = "below"
    with pytest.raises(ValidationError, match="inclusive range"):
        ReferenceRangeComparisonValues.model_validate(valid_ranges)


def test_synthetic_case_has_executable_results_for_numerical_capabilities() -> None:
    case = build_synthetic_case()
    results = {result.tool: result for result in case.tool_results}

    assert set(results) == {
        ToolName.READ_LENGTH_SUMMARY,
        ToolName.COMPARE_REFERENCE_RANGES,
    }
    comparison = results[ToolName.COMPARE_REFERENCE_RANGES]
    assert comparison.id == "result.synthetic-reference-comparison.v1"
    assert set(comparison.provenance.artifact_ids) == {
        "artifact.synthetic-sample-table.v1",
        "artifact.synthetic-reference-table.v1",
    }
    rows = {
        row["cell_type_id"]: row["classification"]
        for row in comparison.values["rows"]
    }
    assert rows == {"immune": "within", "vascular": "below"}
    assert comparison.verification_level == VerificationLevel.RECOMPUTED
    assert "/private/" not in comparison.model_dump_json()


def test_load_case_bundle_round_trips_without_embedding_loader_path(
    tmp_path: Path,
) -> None:
    case = build_synthetic_case()
    bundle_path = tmp_path / "configured-private-location.json"
    bundle_path.write_text(case.model_dump_json())

    loaded = load_case_bundle(bundle_path)

    assert loaded == case
    assert str(bundle_path) not in loaded.model_dump_json()


def test_load_default_case_precedence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    explicit_case = build_synthetic_case().model_copy(
        update={"case_id": "case.explicit.v1"}
    )
    environment_case = build_synthetic_case().model_copy(
        update={"case_id": "case.environment.v1"}
    )
    local_case = build_synthetic_case().model_copy(
        update={"case_id": "case.local.v1"}
    )
    explicit_path = tmp_path / "explicit.json"
    environment_path = tmp_path / "environment.json"
    explicit_path.write_text(explicit_case.model_dump_json())
    environment_path.write_text(environment_case.model_dump_json())
    local_path = tmp_path / "data" / "local" / "case.json"
    local_path.parent.mkdir(parents=True)
    local_path.write_text(local_case.model_dump_json())
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TRACEBACK_CASE_PATH", str(environment_path))

    assert load_default_case(explicit_path).case_id == "case.explicit.v1"
    assert load_default_case().case_id == "case.environment.v1"

    monkeypatch.delenv("TRACEBACK_CASE_PATH")
    assert load_default_case().case_id == "case.local.v1"

    local_path.unlink()
    assert load_default_case().case_id == "case.synthetic.v1"


@pytest.mark.parametrize("configured_input", ["missing", "malformed"])
def test_configured_case_failure_is_sanitized_and_never_masks_with_synthetic(
    configured_input: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured_path = tmp_path / "sensitive" / "case.json"
    if configured_input == "malformed":
        configured_path.parent.mkdir()
        configured_path.write_text('{"case_id":"not-a-valid-case"}')
    monkeypatch.setenv("TRACEBACK_CASE_PATH", str(configured_path))

    with pytest.raises(CaseBundleLoadError) as captured:
        load_default_case()

    assert str(configured_path) not in str(captured.value)
    assert "synthetic" not in str(captured.value).lower()


def _audit(case=None, *, value: float | int = 167, unit: str = "bp") -> AuditResult:
    case = case or build_synthetic_case()
    claim = case.claims[0]
    result = case.tool_results[0]
    return AuditResult(
        id="audit.synthetic.v1",
        claim_id=claim.id,
        claim_revision=claim.revision,
        claim_text_hash=sha256_text(claim.text),
        dataset_revision=case.dataset_revision,
        status=AuditStatus.SUPPORTED,
        verification_level=VerificationLevel.SAMPLED_RECOMPUTED,
        summary="The bounded synthetic evidence supports the scoped assertion.",
        evidence_ids=(result.id,),
        numeric_assertions=(
            NumericAssertion(
                evidence_id=result.id,
                field="mode_bp",
                value=value,
                unit=unit,
                definition=result.definitions["mode_bp"],
                denominator=result.denominator,
                filters=result.filters,
            ),
        ),
        tool_results=(result,),
        assumptions=("The fixture is used only for offline contract testing.",),
        missing_validation=("No biological or clinical validation is supplied.",),
        revised_text=claim.text,
        next_checks=("Prepare an independently identified real-data artifact.",),
        execution_mode=ExecutionMode.FIXTURE,
        model_id="fixture-model",
        prompt_version="fixture-prompt.v1",
    )


def test_audit_validation_checks_binding_citations_numbers_and_verification() -> None:
    case = build_synthetic_case()
    audit = _audit(case)
    assert validate_audit_result(audit, case) is audit

    with pytest.raises(ValueError, match="value mismatch"):
        validate_numeric_assertions(_audit(case, value=168))
    with pytest.raises(ValueError, match="unit mismatch"):
        validate_numeric_assertions(_audit(case, unit="percent"))

    unknown = audit.model_copy(update={"evidence_ids": ("unknown",)})
    with pytest.raises(ValueError, match="unknown evidence"):
        validate_audit_result(unknown, case)

    wrong_level = audit.model_copy(
        update={"verification_level": VerificationLevel.RECOMPUTED}
    )
    with pytest.raises(ValueError, match="verification_level"):
        validate_audit_result(wrong_level, case)

    stale = audit.model_copy(update={"claim_revision": 2})
    with pytest.raises(ValueError, match="binding"):
        validate_audit_result(stale, case)


def test_supported_audit_requires_nonempty_evidence() -> None:
    case = build_synthetic_case()
    audit_payload = _audit(case).model_dump()
    audit_payload["evidence_ids"] = ()
    audit_payload["numeric_assertions"] = ()
    audit_payload["tool_results"] = ()
    with pytest.raises(ValidationError, match="require applicable evidence"):
        AuditResult.model_validate(audit_payload)
