"""Offline semantic and filesystem tests for development report bundles."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from evidence_inspector.copy_number_qc import (
    AUTOSOMES,
    BamDosageQcScan,
    BamHeaderContig,
    BamHeaderReferenceCompatibility,
    BinCount,
    DosageQcScanPolicy,
    ReadAccounting,
    ReferenceBinding,
    ReferenceContigBinding,
    bin_definition_sha256,
    compute_dosage_qc,
    fixed_width_bins,
)
from traceback_runner import report_bundles as report_module
from traceback_runner.report_bundles import (
    ACCESSIBLE_TABLE_PATH,
    MANIFEST_PATH,
    METHOD_ID,
    PLOT_DATA_PATH,
    PLOT_SPEC_PATH,
    PROVENANCE_PATH,
    QUALIFICATION_STATUS,
    RESULT_PATH,
    DevelopmentPlotData,
    DevelopmentPlotSpec,
    DevelopmentReportProvenance,
    DosagePlotRow,
    ReportBundleFilesystemError,
    ReportBundleFormatError,
    ReportBundleIntegrityError,
    ResultBindings,
    accessible_table_bytes,
    build_development_report_bundle,
    replay_development_report_bundle,
    verify_development_report_bundle,
)
from traceback_runner.serialization import canonical_json_bytes, sha256_bytes


def _semantic_result(*, complete: bool = True):
    contigs = tuple(
        ReferenceContigBinding(name=name, length=10, md5="c" * 32)
        for name in AUTOSOMES
    )
    definitions = fixed_width_bins(contigs, window_size_bp=10)
    reference = ReferenceBinding(
        reference_id="synthetic-reference.v1",
        assembly="synthetic-assembly.v1",
        fasta_sha256="a" * 64,
        contigs=contigs,
        bin_definition_sha256=bin_definition_sha256(definitions),
    )
    bins = tuple(
        BinCount(
            **definition.model_dump(),
            accepted_read_start_count=index + 10 if complete else 0,
            included_in_screen=True,
        )
        for index, definition in enumerate(definitions)
    )
    accepted = sum(row.accepted_read_start_count for row in bins)
    scan = BamDosageQcScan(
        input_artifact_sha256="b" * 64,
        reference=reference,
        scan_policy=DosageQcScanPolicy(
            window_size_bp=10,
            minimum_mapping_quality=20,
        ),
        bins=bins,
        accounting=ReadAccounting(
            inspected_alignment_count=accepted,
            accepted_autosomal_read_count=accepted,
            excluded_unmapped=0,
            excluded_secondary=0,
            excluded_supplementary=0,
            excluded_qc_failure=0,
            excluded_duplicate=0,
            excluded_below_mapq=0,
            excluded_non_autosomal=0,
            excluded_outside_analyzed_bins=0,
        ),
        bam_header_compatibility=BamHeaderReferenceCompatibility(
            reference_id=reference.reference_id,
            scope="header_m5_names_lengths_match",
            autosomes=tuple(
                BamHeaderContig(name=item.name, length=item.length, declared_md5=item.md5)
                for item in contigs
            ),
        ),
    )
    return compute_dosage_qc(scan)


def _inputs_for_result(result) -> dict[str, bytes]:
    result_bytes = canonical_json_bytes(result)
    result_digest = sha256_bytes(result_bytes)
    rows = (
        tuple(
            DosagePlotRow(
                chromosome=item.chromosome,
                relative_diploid_dosage=item.relative_diploid_dosage,
            )
            for item in result.chromosomes
        )
        if result.analysis_status == "complete"
        else ()
    )
    plot_data = DevelopmentPlotData(
        result_sha256=result_digest,
        analysis_status=result.analysis_status,
        rows=rows,
    )
    plot_data_bytes = canonical_json_bytes(plot_data)
    plot_spec = DevelopmentPlotSpec(
        result_sha256=result_digest,
        analysis_status=result.analysis_status,
        plot_data_sha256=sha256_bytes(plot_data_bytes),
    )
    provenance = DevelopmentReportProvenance(
        result_sha256=result_digest,
        analysis_status=result.analysis_status,
    )
    return {
        "result_bytes": result_bytes,
        "plot_data_bytes": plot_data_bytes,
        "plot_spec_bytes": canonical_json_bytes(plot_spec),
        "provenance_bytes": canonical_json_bytes(provenance),
        "accessible_table_bytes": accessible_table_bytes(
            result_sha256=result_digest,
            analysis_status=result.analysis_status,
            rows=rows,
        ),
    }


def _inputs(*, complete: bool = True) -> dict[str, bytes]:
    return _inputs_for_result(_semantic_result(complete=complete))


def _build(tmp_path: Path, name: str = "report") -> Path:
    return build_development_report_bundle(tmp_path / name, **_inputs())


def test_semantic_bundle_is_deterministic_and_binds_every_artifact(tmp_path: Path) -> None:
    first = _build(tmp_path, "first")
    second = _build(tmp_path, "second")
    assert {item.name: item.read_bytes() for item in first.iterdir()} == {
        item.name: item.read_bytes() for item in second.iterdir()
    }

    verified = verify_development_report_bundle(first)
    assert replay_development_report_bundle(first) == verified
    assert not hasattr(verified, "path")
    assert verified.manifest.method_id == METHOD_ID
    assert verified.manifest.qualification_status == QUALIFICATION_STATUS
    assert verified.manifest.product_release_authorized is False
    assert verified.plot_data.rows == tuple(
        DosagePlotRow(
            chromosome=item.chromosome,
            relative_diploid_dosage=item.relative_diploid_dosage,
        )
        for item in verified.result.chromosomes
    )
    for artifact in verified.manifest.artifacts:
        content = (first / artifact.relative_path).read_bytes()
        assert artifact.size_bytes == len(content)
        assert artifact.sha256 == sha256_bytes(content)
        assert artifact.schema_identity
        assert artifact.media_type


def test_insufficient_result_union_member_has_bound_empty_presentations(
    tmp_path: Path,
) -> None:
    bundle = build_development_report_bundle(
        tmp_path / "insufficient", **_inputs(complete=False)
    )
    verified = verify_development_report_bundle(bundle)
    assert verified.result.analysis_status == "insufficient_information"
    assert verified.plot_data.rows == ()
    assert verified.accessible_table_bytes.endswith(
        b"insufficient_information\tNA\tNA\n"
    )


@pytest.mark.parametrize(
    "argument",
    ("result_bytes", "plot_data_bytes", "plot_spec_bytes", "provenance_bytes"),
)
def test_every_json_artifact_requires_closed_canonical_schema(
    tmp_path: Path, argument: str
) -> None:
    inputs = _inputs()
    value = json.loads(inputs[argument])
    value["schema_version"] = "unknown.v9"
    inputs[argument] = canonical_json_bytes(value)
    with pytest.raises(ReportBundleFormatError):
        build_development_report_bundle(tmp_path / "unknown", **inputs)

    inputs = _inputs()
    inputs[argument] = json.dumps(json.loads(inputs[argument]), indent=2).encode()
    with pytest.raises(ReportBundleFormatError, match="canonical|union"):
        build_development_report_bundle(tmp_path / "pretty", **inputs)


@pytest.mark.parametrize(
    ("argument", "field", "value"),
    (
        ("plot_data_bytes", "method_id", "different-method"),
        ("plot_spec_bytes", "qualification_status", "qualified"),
        ("provenance_bytes", "product_release_authorized", True),
    ),
)
def test_development_identity_cannot_drift(
    tmp_path: Path, argument: str, field: str, value: object
) -> None:
    inputs = _inputs()
    artifact = json.loads(inputs[argument])
    artifact[field] = value
    inputs[argument] = canonical_json_bytes(artifact)
    with pytest.raises(ReportBundleFormatError):
        build_development_report_bundle(tmp_path / "drift", **inputs)


def test_result_plot_spec_table_and_status_bindings_are_semantically_replayed(
    tmp_path: Path,
) -> None:
    inputs = _inputs()
    plot_data = json.loads(inputs["plot_data_bytes"])
    plot_data["result_sha256"] = "d" * 64
    inputs["plot_data_bytes"] = canonical_json_bytes(plot_data)
    with pytest.raises(ReportBundleIntegrityError, match="exact result"):
        build_development_report_bundle(tmp_path / "wrong-result", **inputs)

    inputs = _inputs()
    plot_spec = json.loads(inputs["plot_spec_bytes"])
    plot_spec["plot_data_sha256"] = "e" * 64
    inputs["plot_spec_bytes"] = canonical_json_bytes(plot_spec)
    with pytest.raises(ReportBundleIntegrityError, match="exact plot data"):
        build_development_report_bundle(tmp_path / "wrong-plot", **inputs)

    inputs = _inputs()
    inputs["accessible_table_bytes"] += b"unexpected\n"
    with pytest.raises(ReportBundleFormatError, match="canonical typed UTF-8 TSV"):
        build_development_report_bundle(tmp_path / "wrong-table", **inputs)


@pytest.mark.parametrize(
    "private_text",
    (
        "/private/provider/sample.bam",
        "read_id=raw-read-123",
        "AWS_SECRET_ACCESS_KEY=example",
        "ACGTACGTACGTACGTACGTACGT",
    ),
)
def test_privacy_allowlist_rejects_paths_raw_ids_secrets_and_sequence(
    tmp_path: Path, private_text: str
) -> None:
    inputs = _inputs()
    result = json.loads(inputs["result_bytes"])
    result["limitations"][0] = private_text
    inputs["result_bytes"] = canonical_json_bytes(result)
    with pytest.raises(ReportBundleFormatError, match="forbidden|allowlist"):
        build_development_report_bundle(tmp_path / "private", **inputs)


def test_replay_reapplies_privacy_allowlist(tmp_path: Path) -> None:
    bundle = _build(tmp_path)
    result_path = bundle / RESULT_PATH
    result = json.loads(result_path.read_bytes())
    result["limitations"][0] = "/private/provider/sample.bam"
    result_path.write_bytes(canonical_json_bytes(result))
    with pytest.raises(ReportBundleFormatError, match="absolute local path|allowlist"):
        replay_development_report_bundle(bundle)


@pytest.mark.parametrize(
    "bypass_text",
    (
        "file:///private/provider/sample.bam",
        "path=/private/provider/sample.bam",
        "private read raw-read-123",
    ),
)
def test_closed_limitation_allowlist_rejects_fully_rebound_private_content(
    tmp_path: Path, bypass_text: str
) -> None:
    result = _semantic_result()
    payload = result.model_dump(mode="python")
    payload["limitations"] = (bypass_text, *payload["limitations"][1:])
    rebound = _inputs_for_result(result.__class__.model_validate(payload))

    with pytest.raises(ReportBundleFormatError, match="closed publication allowlist"):
        build_development_report_bundle(tmp_path / "build-rejected", **rebound)

    stored = {
        RESULT_PATH: rebound["result_bytes"],
        PLOT_DATA_PATH: rebound["plot_data_bytes"],
        PLOT_SPEC_PATH: rebound["plot_spec_bytes"],
        PROVENANCE_PATH: rebound["provenance_bytes"],
        ACCESSIBLE_TABLE_PATH: rebound["accessible_table_bytes"],
    }
    stored[MANIFEST_PATH] = canonical_json_bytes(report_module._manifest_for(stored))
    bundle = tmp_path / "replay-rejected"
    bundle.mkdir()
    for relative_path, content in stored.items():
        (bundle / relative_path).write_bytes(content)
    with pytest.raises(ReportBundleFormatError, match="closed publication allowlist"):
        replay_development_report_bundle(bundle)


def test_binary_empty_and_malformed_tables_fail_closed(tmp_path: Path) -> None:
    for content in (b"", b"\xff\x00", b"wrong\theader\n"):
        inputs = _inputs()
        inputs["accessible_table_bytes"] = content
        with pytest.raises(ReportBundleFormatError, match="canonical typed UTF-8 TSV"):
            build_development_report_bundle(tmp_path / f"bad-{len(content)}", **inputs)


def test_late_empty_destination_race_cannot_overwrite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "late"
    exclusive = report_module.rename_directory_exclusive_at

    def race(parent_fd: int, source_name: str, destination_name: str) -> None:
        os.mkdir(destination_name, dir_fd=parent_fd)
        exclusive(parent_fd, source_name, destination_name)

    monkeypatch.setattr(report_module, "rename_directory_exclusive_at", race)
    with pytest.raises(ReportBundleFilesystemError, match="already exists"):
        build_development_report_bundle(destination, **_inputs())
    assert destination.is_dir() and list(destination.iterdir()) == []
    assert not list(tmp_path.glob(".late.*"))


def test_stage_allocation_failure_cleans_publication_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_stage(parent_fd: int, destination_name: str) -> str:
        raise OSError("injected staging failure")

    monkeypatch.setattr(report_module, "_stage_name", fail_stage)
    with pytest.raises(OSError, match="staging failure"):
        build_development_report_bundle(tmp_path / "failed", **_inputs())
    assert not (tmp_path / ".failed.publish.lock").exists()


def test_staging_directory_and_parent_are_fsynced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[bool] = []
    real_fsync = report_module.os.fsync

    def observe(descriptor: int) -> None:
        calls.append(stat.S_ISDIR(os.fstat(descriptor).st_mode))
        real_fsync(descriptor)

    monkeypatch.setattr(report_module.os, "fsync", observe)
    _build(tmp_path)
    assert calls.count(True) >= 2
    assert calls.count(False) == 6


def test_parent_symlink_and_root_symlink_are_rejected(tmp_path: Path) -> None:
    real_parent = tmp_path / "real-parent"
    real_parent.mkdir()
    alias = tmp_path / "parent-alias"
    alias.symlink_to(real_parent, target_is_directory=True)
    with pytest.raises(ReportBundleFilesystemError, match="safely open"):
        build_development_report_bundle(alias / "report", **_inputs())

    bundle = _build(tmp_path, "real-report")
    link = tmp_path / "report-link"
    link.symlink_to(bundle, target_is_directory=True)
    with pytest.raises(ReportBundleFilesystemError, match="safely open"):
        verify_development_report_bundle(link)


def test_parent_symlink_substitution_during_publication_fails_without_escape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = tmp_path / "parent"
    parent.mkdir()
    moved = tmp_path / "moved-parent"
    hostile = tmp_path / "hostile-parent"
    hostile.mkdir()
    real_stage_name = report_module._stage_name

    def swap_parent(parent_fd: int, destination_name: str) -> str:
        parent.rename(moved)
        parent.symlink_to(hostile, target_is_directory=True)
        return real_stage_name(parent_fd, destination_name)

    monkeypatch.setattr(report_module, "_stage_name", swap_parent)
    with pytest.raises(ReportBundleFilesystemError, match="parent changed"):
        build_development_report_bundle(parent / "report", **_inputs())
    assert not (hostile / "report").exists()
    assert not (moved / "report").exists()
    assert list(moved.iterdir()) == []


def test_verifier_uses_one_pinned_root_during_path_substitution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = _build(tmp_path)
    moved = tmp_path / "moved-valid"
    replacement = tmp_path / "replacement"
    replacement.mkdir()
    (replacement / "hostile").write_text("not a bundle")
    real_listdir = report_module.os.listdir
    swapped = False

    def swap_then_list(descriptor: int) -> list[str]:
        nonlocal swapped
        if not swapped:
            bundle.rename(moved)
            replacement.rename(bundle)
            swapped = True
        return real_listdir(descriptor)

    monkeypatch.setattr(report_module.os, "listdir", swap_then_list)
    verified = verify_development_report_bundle(bundle)
    assert verified.result.analysis_status == "complete"
    assert (bundle / "hostile").is_file()


def test_extra_missing_symlink_digest_size_and_manifest_format_fail_closed(
    tmp_path: Path,
) -> None:
    extra = _build(tmp_path, "extra")
    (extra / "extra.json").write_text("{}")
    with pytest.raises(ReportBundleFilesystemError, match="extra"):
        verify_development_report_bundle(extra)

    missing = _build(tmp_path, "missing")
    (missing / PLOT_SPEC_PATH).unlink()
    with pytest.raises(ReportBundleFilesystemError, match="missing"):
        verify_development_report_bundle(missing)

    linked = _build(tmp_path, "linked")
    (linked / PLOT_DATA_PATH).unlink()
    (linked / PLOT_DATA_PATH).symlink_to(linked / RESULT_PATH)
    with pytest.raises(ReportBundleFilesystemError, match="safely open"):
        verify_development_report_bundle(linked)

    digest = _build(tmp_path, "digest")
    manifest_path = digest / MANIFEST_PATH
    manifest = json.loads(manifest_path.read_bytes())
    replacement_digest = "d" * 64
    for artifact in manifest["artifacts"]:
        if artifact["relative_path"] == ACCESSIBLE_TABLE_PATH:
            artifact["sha256"] = replacement_digest
    manifest["bindings"]["accessible_table_sha256"] = replacement_digest
    bindings = ResultBindings.model_validate(manifest["bindings"])
    manifest["report_id"] = (
        f"development-report-{sha256_bytes(canonical_json_bytes(bindings))[:24]}"
    )
    manifest_path.write_bytes(canonical_json_bytes(manifest))
    with pytest.raises(ReportBundleIntegrityError, match="digest mismatch"):
        verify_development_report_bundle(digest)

    size = _build(tmp_path, "size")
    manifest_path = size / MANIFEST_PATH
    manifest = json.loads(manifest_path.read_bytes())
    manifest["artifacts"][0]["size_bytes"] += 1
    manifest_path.write_bytes(canonical_json_bytes(manifest))
    with pytest.raises(ReportBundleIntegrityError, match="size mismatch"):
        verify_development_report_bundle(size)

    noncanonical = _build(tmp_path, "noncanonical")
    manifest_path = noncanonical / MANIFEST_PATH
    manifest_path.write_bytes(
        json.dumps(json.loads(manifest_path.read_bytes()), indent=2).encode()
    )
    with pytest.raises(ReportBundleFormatError, match="noncanonical"):
        verify_development_report_bundle(noncanonical)

    unknown = _build(tmp_path, "unknown-manifest")
    manifest_path = unknown / MANIFEST_PATH
    manifest = json.loads(manifest_path.read_bytes())
    manifest["schema_version"] = "traceback.development-report-bundle.v2"
    manifest_path.write_bytes(canonical_json_bytes(manifest))
    with pytest.raises(ReportBundleFormatError, match="manifest"):
        verify_development_report_bundle(unknown)
