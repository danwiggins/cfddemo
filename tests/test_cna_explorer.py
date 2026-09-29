"""Synthetic deterministic tests for the E09 CNA explorer contract."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from evidence_inspector.cna_explorer import (
    CnaExplorerError,
    CnaExplorerSnapshot,
    CnaSource,
    ExecutionState,
    ExplorerAvailability,
    ExplorerInputAuthority,
    QualificationState,
    SegmentLayer,
    TrustState,
    build_cna_explorer_snapshot,
    cna_explorer_snapshot_bytes,
    cna_explorer_snapshot_from_bytes,
    cna_explorer_snapshot_sha256,
    replay_cna_explorer_snapshot,
)
from evidence_inspector.copy_number_qc import (
    BamDosageQcScan,
    DosageQcInsufficientResultBundle,
    DosageQcResultBundle,
    compute_dosage_qc,
    scan_bam,
)
from evidence_inspector.ichor_adapter import (
    CnvDevelopmentResult,
    prepare_ichor_run,
    validate_ichor_outputs,
)
from tests.test_copy_number_qc import _reference_and_bins, _write_fixture_bam
from tests.test_ichor_adapter import _fixture, _request


def _inputs(
    tmp_path: Path, *, segmented_fixture: str = "arm_loss"
) -> tuple[DosageQcResultBundle, CnvDevelopmentResult]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    reference, bins = _reference_and_bins()
    scan = scan_bam(
        _write_fixture_bam(tmp_path / "synthetic.bam"),
        reference=reference,
        bins=bins,
        window_size_bp=10,
    )
    dosage = compute_dosage_qc(scan)
    assert isinstance(dosage, DosageQcResultBundle)
    fixture_name = (
        "arm_loss"
        if segmented_fixture in {"arm_loss_na", "arm_loss_masked"}
        else segmented_fixture
    )
    segmented_output = _fixture(tmp_path, fixture_name)
    if segmented_fixture == "arm_loss_na":
        corrected = segmented_output / "sample.correctedDepth.txt"
        corrected.write_text(corrected.read_text().replace("-0.20", "NA", 1))
    elif segmented_fixture == "arm_loss_masked":
        corrected = segmented_output / "sample.correctedDepth.txt"
        corrected.write_text(
            corrected.read_text().replace("chr1\t1000001\t2000000\t-0.21\n", "")
        )
        events = segmented_output / "sample.cna.seg"
        events.write_text(
            events.read_text().replace("chr1\t1000001\t2000000\tHETD\t-0.21\n", "")
        )
    segmented = validate_ichor_outputs(
        prepare_ichor_run(
            _request(
                mask_index=1 if segmented_fixture == "arm_loss_masked" else None,
                normal_fraction_starts=(1.0,)
                if segmented_fixture == "native_na"
                else (0.95, 0.99, 0.995, 0.999),
            )
        ),
        segmented_output,
    )
    return dosage, segmented


def _authority(source: CnaSource) -> ExplorerInputAuthority:
    return ExplorerInputAuthority(
        source=source,
        execution_state=ExecutionState.COMPLETE,
        trust_state=TrustState.VERIFIED,
        qualification_state=QualificationState.QUALIFIED,
        research_inspectable=True,
    )


def _snapshot(
    tmp_path: Path, *, segmented_fixture: str = "arm_loss"
) -> tuple[DosageQcResultBundle, CnvDevelopmentResult, CnaExplorerSnapshot]:
    dosage, segmented = _inputs(tmp_path, segmented_fixture=segmented_fixture)
    snapshot = build_cna_explorer_snapshot(
        dosage,
        segmented,
        dosage_authority=_authority(CnaSource.DOSAGE_QC),
        segmented_authority=_authority(CnaSource.SEGMENTED_CNA),
    )
    return dosage, segmented, snapshot


def test_available_snapshot_preserves_distinct_exact_layers(tmp_path: Path) -> None:
    _, _, snapshot = _snapshot(tmp_path)

    assert snapshot.availability == ExplorerAvailability.AVAILABLE
    assert snapshot.layers is not None
    assert [item.source for item in snapshot.layers.coordinate_grids] == [
        CnaSource.DOSAGE_QC,
        CnaSource.SEGMENTED_CNA,
    ]
    assert [item.method_id for item in snapshot.methods] == [
        "sample-internal-whole-chromosome-dosage-qc",
        "ichor-development-adapter",
    ]
    assert all(
        item.embedded_qualification_status == "development_unqualified"
        for item in snapshot.methods
    )
    assert len(snapshot.layers.dosage_chromosomes) == 22
    assert len(snapshot.layers.corrected_depth) == 4
    assert len(snapshot.layers.segments) == 2
    assert len(snapshot.layers.candidates) == 4
    assert snapshot.chart.dosage_chromosomes == snapshot.layers.dosage_chromosomes
    assert snapshot.chart.corrected_depth == snapshot.layers.corrected_depth
    assert snapshot.tables.bins == snapshot.layers.bins
    assert snapshot.tables.candidates == snapshot.layers.candidates
    assert snapshot.provenance.product_release_authorized is False
    assert snapshot.provenance.diagnostic_interpretation_allowed is False
    assert snapshot.chart.clinical_thresholds_present is False


def test_canonical_round_trip_digest_and_semantic_replay(tmp_path: Path) -> None:
    dosage, segmented, snapshot = _snapshot(tmp_path)
    encoded = cna_explorer_snapshot_bytes(snapshot)

    assert cna_explorer_snapshot_from_bytes(encoded) == snapshot
    assert cna_explorer_snapshot_sha256(snapshot) == cna_explorer_snapshot_sha256(
        snapshot
    )
    assert (
        replay_cna_explorer_snapshot(
            dosage,
            segmented,
            snapshot,
            dosage_authority=_authority(CnaSource.DOSAGE_QC),
            segmented_authority=_authority(CnaSource.SEGMENTED_CNA),
        )
        == snapshot
    )
    with pytest.raises(CnaExplorerError, match="noncanonical"):
        cna_explorer_snapshot_from_bytes(
            json.dumps(snapshot.model_dump(mode="json"), indent=2).encode()
        )


@pytest.mark.parametrize(
    ("source", "field", "value"),
    (
        (CnaSource.DOSAGE_QC, "execution_state", ExecutionState.INCOMPLETE),
        (CnaSource.SEGMENTED_CNA, "execution_state", ExecutionState.UNKNOWN),
        (CnaSource.DOSAGE_QC, "trust_state", TrustState.REVOKED),
        (CnaSource.SEGMENTED_CNA, "trust_state", TrustState.UNKNOWN),
        (
            CnaSource.DOSAGE_QC,
            "qualification_state",
            QualificationState.DEVELOPMENT_UNQUALIFIED,
        ),
        (
            CnaSource.SEGMENTED_CNA,
            "qualification_state",
            QualificationState.UNKNOWN,
        ),
        (CnaSource.SEGMENTED_CNA, "research_inspectable", False),
    ),
)
def test_ineligible_inputs_fail_closed_without_values(
    tmp_path: Path,
    source: CnaSource,
    field: str,
    value: object,
) -> None:
    dosage, segmented = _inputs(tmp_path)
    dosage_authority = _authority(CnaSource.DOSAGE_QC)
    segmented_authority = _authority(CnaSource.SEGMENTED_CNA)
    if source == CnaSource.DOSAGE_QC:
        dosage_authority = dosage_authority.model_copy(update={field: value})
    else:
        segmented_authority = segmented_authority.model_copy(update={field: value})

    snapshot = build_cna_explorer_snapshot(
        dosage,
        segmented,
        dosage_authority=dosage_authority,
        segmented_authority=segmented_authority,
    )

    assert snapshot.availability == ExplorerAvailability.UNAVAILABLE
    assert snapshot.layers is None
    assert snapshot.chart.dosage_chromosomes == ()
    assert snapshot.chart.corrected_depth == ()
    assert snapshot.chart.segments == ()
    assert snapshot.tables.bins == ()
    assert snapshot.tables.candidates == ()
    assert snapshot.unavailable_reasons


def test_upstream_insufficiency_fails_closed(tmp_path: Path) -> None:
    dosage, segmented = _inputs(tmp_path, segmented_fixture="neutral")
    assert segmented.status == "insufficient_information"

    snapshot = build_cna_explorer_snapshot(
        dosage,
        segmented,
        dosage_authority=_authority(CnaSource.DOSAGE_QC),
        segmented_authority=_authority(CnaSource.SEGMENTED_CNA),
    )

    assert snapshot.availability == ExplorerAvailability.UNAVAILABLE
    assert snapshot.layers is None
    assert snapshot.unavailable_reasons == (
        "segmented_cna:upstream_insufficient_information",
    )


def test_dosage_upstream_insufficiency_fails_closed(tmp_path: Path) -> None:
    dosage, segmented = _inputs(tmp_path)
    payload = dosage.model_dump(mode="python")
    removed = 0
    for row in payload["bins"]:
        if row["contig"] == "chr22":
            removed += row["accepted_read_start_count"]
            row["accepted_read_start_count"] = 0
    accounting = payload["provenance"]["read_accounting"]
    accounting["accepted_autosomal_read_count"] -= removed
    accounting["inspected_alignment_count"] -= removed
    sparse_scan = BamDosageQcScan.model_validate(
        {
            "input_artifact_sha256": payload["provenance"]["input_artifact_sha256"],
            "reference": payload["provenance"]["reference"],
            "scan_policy": payload["provenance"]["scan_policy"],
            "bins": payload["bins"],
            "accounting": accounting,
            "bam_header_compatibility": payload["provenance"][
                "bam_header_compatibility"
            ],
        }
    )
    insufficient = compute_dosage_qc(sparse_scan)
    assert isinstance(insufficient, DosageQcInsufficientResultBundle)

    snapshot = build_cna_explorer_snapshot(
        insufficient,
        segmented,
        dosage_authority=_authority(CnaSource.DOSAGE_QC),
        segmented_authority=_authority(CnaSource.SEGMENTED_CNA),
    )

    assert snapshot.availability == ExplorerAvailability.UNAVAILABLE
    assert snapshot.unavailable_reasons == (
        "dosage_qc:upstream_insufficient_information",
    )


def test_mask_layer_replays_exact_prespecified_mask(tmp_path: Path) -> None:
    _, _, snapshot = _snapshot(tmp_path, segmented_fixture="arm_loss_masked")
    assert snapshot.layers is not None
    assert len(snapshot.layers.masks) == 1
    mask = snapshot.layers.masks[0]
    assert mask.bin_index == 1
    assert mask.reason == "centromere_or_flank"
    assert mask.source_value is None
    assert snapshot.tables.masks == snapshot.layers.masks
    segmented_bin = next(
        item
        for item in snapshot.layers.bins
        if item.source == CnaSource.SEGMENTED_CNA and item.bin_index == 1
    )
    assert segmented_bin.status == "masked_prespecified"
    assert segmented_bin.corrected_log2 is None


def test_native_missing_corrected_depth_never_becomes_zero(tmp_path: Path) -> None:
    _, _, snapshot = _snapshot(tmp_path, segmented_fixture="arm_loss_na")
    assert snapshot.layers is not None

    missing = snapshot.layers.corrected_depth[0]
    assert missing.corrected_log2 is None
    assert missing.value_state == "native_missing"
    assert snapshot.chart.corrected_depth[0].corrected_log2 is None
    segmented_bin = next(
        item
        for item in snapshot.tables.bins
        if item.source == CnaSource.SEGMENTED_CNA and item.bin_index == 0
    )
    assert segmented_bin.corrected_log2 is None


@pytest.mark.parametrize(
    "call",
    ("HOMD", "HETD", "NEUT", "GAIN", "AMP", "HLAMP", "HLAMP2", "HLAMP25"),
)
def test_segment_layer_accepts_only_pinned_ichor_call_vocabulary(call: str) -> None:
    layer = SegmentLayer(
        segment_index=0,
        contig="chr1",
        start=0,
        end=1_000_000,
        native_span_bin_count=1,
        retained_bin_count=1,
        median_log2=0.0,
        upstream_copy_number=2,
        upstream_call=call,
        subclone_status=False,
    )
    assert layer.upstream_call == call


@pytest.mark.parametrize(
    "embedded_path",
    (
        "artifact=/private/tmp/confidential.bam",
        r"artifact=C:\private\confidential.bam",
    ),
    ids=("embedded-posix", "embedded-windows"),
)
def test_embedded_paths_in_upstream_segment_call_fail_closed(
    tmp_path: Path, embedded_path: str
) -> None:
    dosage, segmented = _inputs(tmp_path)
    payload = segmented.model_dump(mode="python")
    payload["segments"][0]["call"] = embedded_path
    changed = CnvDevelopmentResult.model_validate(payload)

    with pytest.raises(ValidationError, match="upstream_call"):
        build_cna_explorer_snapshot(
            dosage,
            changed,
            dosage_authority=_authority(CnaSource.DOSAGE_QC),
            segmented_authority=_authority(CnaSource.SEGMENTED_CNA),
        )


@pytest.mark.parametrize(
    "mutation",
    ("dosage_chart", "corrected_chart", "bin_table", "candidate_table"),
)
def test_chart_and_table_mutations_fail_reconciliation(
    tmp_path: Path, mutation: str
) -> None:
    _, _, snapshot = _snapshot(tmp_path)
    payload = snapshot.model_dump(mode="python")
    if mutation == "dosage_chart":
        payload["chart"]["dosage_chromosomes"][0]["relative_diploid_dosage"] += 1
    elif mutation == "corrected_chart":
        payload["chart"]["corrected_depth"][0]["corrected_log2"] += 1
    elif mutation == "bin_table":
        payload["tables"]["bins"][0]["accepted_read_start_count"] += 1
    else:
        payload["tables"]["candidates"][0]["log_likelihood"] += 1

    with pytest.raises(ValidationError, match="does not replay"):
        CnaExplorerSnapshot.model_validate(payload)


@pytest.mark.parametrize(
    "mutation",
    ("grid", "asset", "segment", "candidate", "insufficiency"),
)
def test_adversarial_layer_mutations_fail_semantic_replay(
    tmp_path: Path, mutation: str
) -> None:
    dosage, segmented, snapshot = _snapshot(tmp_path)
    assert snapshot.layers is not None
    payload = snapshot.model_dump(mode="python")
    if mutation == "grid":
        payload["layers"]["coordinate_grids"][0]["bin_definition_sha256"] = "f" * 64
    elif mutation == "asset":
        payload["layers"]["assets"][0]["content_sha256"] = "f" * 64
    elif mutation == "segment":
        payload["layers"]["segments"][0]["median_log2"] += 0.1
        payload["chart"]["segments"][0]["median_log2"] += 0.1
        payload["tables"]["segments"][0]["median_log2"] += 0.1
    elif mutation == "candidate":
        payload["layers"]["candidates"][0]["log_likelihood"] += 1
        payload["tables"]["candidates"][0]["log_likelihood"] += 1
    else:
        payload["layers"]["insufficiency"][0]["upstream_status"] = (
            "insufficient_information"
        )
        payload["layers"]["insufficiency"][0]["reasons"] = ("synthetic reason",)
        payload["tables"]["insufficiency"][0]["upstream_status"] = (
            "insufficient_information"
        )
        payload["tables"]["insufficiency"][0]["reasons"] = ("synthetic reason",)
    changed = CnaExplorerSnapshot.model_validate(payload)

    with pytest.raises(CnaExplorerError, match="semantic replay"):
        replay_cna_explorer_snapshot(
            dosage,
            segmented,
            changed,
            dosage_authority=_authority(CnaSource.DOSAGE_QC),
            segmented_authority=_authority(CnaSource.SEGMENTED_CNA),
        )


@pytest.mark.parametrize(
    "mutation",
    ("method_authority", "source_status", "limitations", "candidate_order"),
)
def test_direct_contract_invariants_reject_invalid_layer_states(
    tmp_path: Path, mutation: str
) -> None:
    _, _, snapshot = _snapshot(tmp_path)
    payload = snapshot.model_dump(mode="python")
    if mutation == "method_authority":
        payload["methods"][0]["authority_qualification_state"] = (
            QualificationState.UNKNOWN
        )
        payload["layers"]["methods"][0]["authority_qualification_state"] = (
            QualificationState.UNKNOWN
        )
    elif mutation == "source_status":
        segmented_index = next(
            index
            for index, item in enumerate(payload["layers"]["bins"])
            if item["source"] == CnaSource.SEGMENTED_CNA
        )
        payload["layers"]["bins"][segmented_index]["status"] = "included"
        payload["tables"]["bins"][segmented_index]["status"] = "included"
    elif mutation == "limitations":
        payload["limitations"] = tuple(reversed(payload["limitations"]))
    else:
        payload["layers"]["candidates"][0]["candidate_index"] = 1
        payload["layers"]["candidates"][1]["candidate_index"] = 0

    with pytest.raises(ValidationError):
        CnaExplorerSnapshot.model_validate(payload)


@pytest.mark.parametrize(
    "private_value",
    (
        str(Path("/").joinpath("synthetic-private", "input.bam")),
        "sample_id=private-123",
        "AWS_SECRET_ACCESS_KEY=private",
        "".join(("ACGT",) * 6),
    ),
)
def test_privacy_sensitive_text_is_rejected(tmp_path: Path, private_value: str) -> None:
    dosage, segmented = _inputs(tmp_path)
    authority = _authority(CnaSource.DOSAGE_QC).model_copy(
        update={"trust_state": TrustState.REVOKED}
    )
    snapshot = build_cna_explorer_snapshot(
        dosage,
        segmented,
        dosage_authority=authority,
        segmented_authority=_authority(CnaSource.SEGMENTED_CNA),
    )
    payload = snapshot.model_dump(mode="python")
    payload["unavailable_reasons"] = (private_value,)

    with pytest.raises(ValidationError):
        CnaExplorerSnapshot.model_validate(payload)


def test_unknown_fields_and_wrong_authority_sources_are_rejected(
    tmp_path: Path,
) -> None:
    _, _, snapshot = _snapshot(tmp_path)
    payload = snapshot.model_dump(mode="python")
    payload["sample_id"] = "synthetic-private"
    with pytest.raises(ValidationError, match="Extra inputs"):
        CnaExplorerSnapshot.model_validate(payload)

    dosage, segmented = _inputs(tmp_path / "wrong-source")
    with pytest.raises(CnaExplorerError, match="wrong source"):
        build_cna_explorer_snapshot(
            dosage,
            segmented,
            dosage_authority=_authority(CnaSource.SEGMENTED_CNA),
            segmented_authority=_authority(CnaSource.DOSAGE_QC),
        )
