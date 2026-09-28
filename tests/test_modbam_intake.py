"""Offline synthetic tests for private multi-chunk modBAM intake."""

from __future__ import annotations

import os
import shutil
from array import array
from pathlib import Path

import pytest

from evidence_inspector.modbam_intake import (
    AlignmentDiskBudget,
    BasecallerIdentity,
    ExactToolIdentity,
    ModbamIntakeBounds,
    ModbamIntakeError,
    RegisteredGrch38Asset,
    build_grch38_alignment_plan,
    inspect_modbam_chunks,
)

ZERO_SHA = "0" * 64
ONE_SHA = "1" * 64
TWO_SHA = "2" * 64
THREE_SHA = "3" * 64


def _tool(name: str, digest: str = ZERO_SHA) -> ExactToolIdentity:
    return ExactToolIdentity(name=name, version="test-version", executable_sha256=digest)


def _basecaller() -> BasecallerIdentity:
    return BasecallerIdentity(
        tool=_tool("synthetic-basecaller"),
        model_id="synthetic-model",
        model_version="test-version",
    )


def _bounds(**updates: int) -> ModbamIntakeBounds:
    values = {
        "max_chunks": 4,
        "max_chunk_bytes": 1_000_000,
        "max_total_bytes": 2_000_000,
        "max_records": 20,
    }
    values.update(updates)
    return ModbamIntakeBounds(**values)


def _write_bam(
    path: Path,
    *,
    sequences: tuple[str, ...] = ("CCCC",),
    sort_order: str = "unknown",
    include_tags: tuple[str, ...] = ("MM", "ML", "MN"),
    mm: str = "C+m?,0,0;",
    ml: tuple[int, ...] = (200, 190),
    mn_adjustment: int = 0,
    aligned: bool = False,
) -> None:
    import pysam

    header: dict[str, object] = {"HD": {"VN": "1.6", "SO": sort_order}}
    if aligned:
        header["SQ"] = [{"SN": "synthetic-contig", "LN": 100}]
    with pysam.AlignmentFile(path, "wb", header=header) as bam:
        for sequence in sequences:
            record = pysam.AlignedSegment()
            # BAM requires a query name; this fixed placeholder carries no identity.
            record.query_name = "q"
            record.query_sequence = sequence
            if aligned:
                record.flag = 0
                record.reference_id = 0
                record.reference_start = 1
                record.cigarstring = f"{len(sequence)}M"
            else:
                record.flag = 4
            if "MM" in include_tags:
                record.set_tag("MM", mm)
            if "ML" in include_tags:
                record.set_tag("ML", array("B", ml))
            if "MN" in include_tags:
                record.set_tag("MN", len(sequence) + mn_adjustment)
            bam.write(record)


def _inspect(paths: list[Path], **bound_updates: int):
    return inspect_modbam_chunks(
        paths,
        bounds=_bounds(**bound_updates),
        scanner=_tool("pysam-scanner", ONE_SHA),
        basecaller=_basecaller(),
    )


def test_full_scan_preserves_explicit_order_and_separates_public_status(
    tmp_path: Path,
) -> None:
    first = tmp_path / "private-a.bam"
    second = tmp_path / "private-b.bam"
    _write_bam(first, sequences=("CCCC",))
    _write_bam(second, sequences=("CCCCC",), mm="C+m?,0;", ml=(180,))

    result = _inspect([second, first])

    manifest = result.private_manifest
    assert [chunk.order for chunk in manifest.ordered_chunks] == [0, 1]
    assert manifest.ordered_chunks[0].content_sha256 != manifest.ordered_chunks[1].content_sha256
    assert manifest.manifest_sha256 == manifest.manifest_sha256
    assert _inspect([second, first]).private_manifest.manifest_sha256 == (
        manifest.manifest_sha256
    )
    assert _inspect([first, second]).private_manifest.manifest_sha256 != (
        manifest.manifest_sha256
    )
    assert result.public_summary.chunk_count == 2
    assert result.public_summary.total_records == 2
    assert result.public_summary.valid_tag_records == 2
    public_json = result.public_summary.model_dump_json()
    private_json = manifest.model_dump_json()
    assert str(tmp_path) not in public_json + private_json
    assert first.name not in public_json + private_json
    assert second.name not in public_json + private_json
    assert "content_sha256" not in public_json
    assert "model_id" not in public_json


def test_accepts_explicit_empty_coordinate_list_and_n_fundamental_base(
    tmp_path: Path,
) -> None:
    empty = tmp_path / "empty-mod-list.bam"
    any_base = tmp_path / "n-fundamental.bam"
    _write_bam(empty, sequences=("AAAA",), mm="C+m?;", ml=())
    _write_bam(any_base, sequences=("ACTG",), mm="N+m?,3;", ml=(200,))

    assert _inspect([empty]).public_summary.valid_tag_records == 1
    assert _inspect([any_base]).public_summary.valid_tag_records == 1


@pytest.mark.parametrize(
    ("include_tags", "mn_adjustment", "mm", "ml"),
    [
        (("ML", "MN"), 0, "C+m?,0,0;", (200, 190)),
        (("MM", "MN"), 0, "C+m?,0,0;", (200, 190)),
        (("MM", "ML"), 0, "C+m?,0,0;", (200, 190)),
        (("MM", "ML", "MN"), 1, "C+m?,0,0;", (200, 190)),
        (("MM", "ML", "MN"), 0, "C+m?,0,0", (200, 190)),
        (("MM", "ML", "MN"), 0, "C+m?,0,0;", (200,)),
    ],
)
def test_missing_or_invalid_mm_ml_mn_fails_with_only_aggregate_status(
    tmp_path: Path,
    include_tags: tuple[str, ...],
    mn_adjustment: int,
    mm: str,
    ml: tuple[int, ...],
) -> None:
    path = tmp_path / "do-not-disclose.bam"
    _write_bam(
        path,
        include_tags=include_tags,
        mn_adjustment=mn_adjustment,
        mm=mm,
        ml=ml,
    )

    with pytest.raises(ModbamIntakeError) as caught:
        _inspect([path])

    message = str(caught.value)
    assert "invalid_or_missing_tag_records=1" in message
    assert str(tmp_path) not in message
    assert path.name not in message
    assert "q" not in message


def test_rejects_aligned_input_instead_of_reinterpreting_it(tmp_path: Path) -> None:
    path = tmp_path / "aligned-private.bam"
    _write_bam(path, aligned=True)

    with pytest.raises(ModbamIntakeError, match="not uniformly unaligned") as caught:
        _inspect([path])

    assert path.name not in str(caught.value)


def test_rejects_duplicate_file_and_content_identities(tmp_path: Path) -> None:
    original = tmp_path / "one.bam"
    copied = tmp_path / "two.bam"
    _write_bam(original)

    with pytest.raises(ModbamIntakeError, match="duplicate file identity"):
        _inspect([original, original])

    shutil.copyfile(original, copied)
    with pytest.raises(ModbamIntakeError, match="duplicate content identity"):
        _inspect([original, copied])


def test_rejects_symlinks_and_all_declared_bounds(tmp_path: Path) -> None:
    path = tmp_path / "source.bam"
    link = tmp_path / "alias.bam"
    _write_bam(path, sequences=("CCCC", "CCCC"))
    os.symlink(path, link)

    with pytest.raises(ModbamIntakeError, match="readable regular file"):
        _inspect([link])
    with pytest.raises(ModbamIntakeError, match="chunk count"):
        _inspect([path, path], max_chunks=1)
    with pytest.raises(ModbamIntakeError, match="chunk violates"):
        _inspect([path], max_chunk_bytes=1)
    with pytest.raises(ModbamIntakeError, match="aggregate bytes"):
        _inspect([path], max_total_bytes=1)
    with pytest.raises(ModbamIntakeError, match="record count"):
        _inspect([path], max_records=1)


def test_incompatible_safe_headers_fail_without_copying_headers(tmp_path: Path) -> None:
    first = tmp_path / "one.bam"
    second = tmp_path / "two.bam"
    _write_bam(first, sort_order="unknown")
    _write_bam(second, sort_order="unsorted", mm="C+m?,0;", ml=(190,))

    with pytest.raises(ModbamIntakeError, match="incompatible_safe_headers=2"):
        _inspect([first, second])


def test_alignment_plan_binds_tools_reference_disk_and_post_run_checks(
    tmp_path: Path,
) -> None:
    path = tmp_path / "private.bam"
    _write_bam(path)
    manifest = _inspect([path]).private_manifest
    reference = RegisteredGrch38Asset(
        asset_id="reference.grch38.test",
        fasta_sha256=ZERO_SHA,
        fai_sha256=ONE_SHA,
        minimap2_index_sha256=TWO_SHA,
        sequence_dictionary_sha256=THREE_SHA,
    )
    total_bytes = manifest.ordered_chunks[0].size_bytes
    budget = AlignmentDiskBudget(
        combined_bam_bytes=total_bytes,
        sort_temporary_bytes=total_bytes * 2,
        aligned_bam_bytes=total_bytes * 2,
        index_bytes=1,
        available_workspace_bytes=total_bytes * 3,
        available_output_bytes=total_bytes * 2 + 1,
    )

    plan = build_grch38_alignment_plan(
        manifest,
        reference=reference,
        samtools=_tool("samtools", TWO_SHA),
        minimap2=_tool("minimap2", THREE_SHA),
        disk_budget=budget,
    )

    assert plan.input_manifest_sha256 == manifest.manifest_sha256
    assert [stage.id for stage in plan.stages] == [
        "combine_unaligned_chunks",
        "emit_tagged_fastq",
        "align_grch38",
        "coordinate_sort",
        "build_index",
    ]
    assert plan.stages[1].argv[2:4] == ("-T", "MM,ML,MN")
    assert "-y" in plan.stages[2].argv
    assert plan.estimated_peak_workspace_bytes == total_bytes * 3
    assert plan.estimated_output_bytes == total_bytes * 2 + 1
    assert {check.id for check in plan.pre_run_checks} == {
        "input_identity",
        "tool_identity",
        "reference_assets",
        "disk_capacity",
    }
    assert {check.id for check in plan.post_run_checks} == {
        "input_digest_recheck",
        "record_count",
        "tag_count",
        "reference_identity",
        "sort_order",
        "index_integrity",
        "record_partition",
    }
    serialized = plan.model_dump_json()
    assert str(tmp_path) not in serialized
    assert path.name not in serialized


def test_alignment_plan_rejects_understated_disk_ceiling(tmp_path: Path) -> None:
    path = tmp_path / "private.bam"
    _write_bam(path)
    manifest = _inspect([path]).private_manifest
    with pytest.raises(ModbamIntakeError, match="below input bytes"):
        build_grch38_alignment_plan(
            manifest,
            reference=RegisteredGrch38Asset(
                asset_id="reference.grch38.test",
                fasta_sha256=ZERO_SHA,
                fai_sha256=ONE_SHA,
                minimap2_index_sha256=TWO_SHA,
                sequence_dictionary_sha256=THREE_SHA,
            ),
            samtools=_tool("samtools", TWO_SHA),
            minimap2=_tool("minimap2", THREE_SHA),
            disk_budget=AlignmentDiskBudget(
                combined_bam_bytes=1,
                sort_temporary_bytes=1,
                aligned_bam_bytes=1,
                index_bytes=1,
                available_workspace_bytes=2,
                available_output_bytes=2,
            ),
        )
