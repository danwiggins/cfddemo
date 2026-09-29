"""Offline synthetic tests for private multi-chunk modBAM intake."""

from __future__ import annotations

import os
import random
import shutil
import subprocess
import hashlib
from array import array
from pathlib import Path

import pytest

import evidence_inspector.modbam_intake as modbam_intake
from evidence_inspector.models import canonical_json_bytes
from evidence_inspector.modbam_intake import (
    AlignmentDiskBudget,
    AlignmentRegistryIdentity,
    BasecallerIdentity,
    ExactToolIdentity,
    ModbamAlignmentPlan,
    ModbamIntakeBounds,
    ModbamIntakeError,
    RegisteredGrch38Asset,
    RegisteredToolIdentity,
    build_grch38_alignment_plan,
    inspect_modbam_chunks,
    measure_alignment_disk_budget,
    validate_registered_alignment_plan,
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


class SyntheticAlignmentRegistry:
    def __init__(self) -> None:
        self.identity = AlignmentRegistryIdentity(
            registry_id="synthetic-alignment-registry",
            version="v1",
            immutable_snapshot_sha256=ZERO_SHA,
        )
        self.reference = RegisteredGrch38Asset(
            asset_id="reference.grch38.test",
            version="v1",
            registry_record_sha256=ONE_SHA,
            fasta_sha256=ZERO_SHA,
            fai_sha256=ONE_SHA,
            minimap2_index_sha256=TWO_SHA,
            sequence_dictionary_sha256=THREE_SHA,
        )

    def resolve_grch38(
        self, asset_id: str, version: str
    ) -> RegisteredGrch38Asset:
        assert (asset_id, version) == (
            self.reference.asset_id,
            self.reference.version,
        )
        return self.reference

    def resolve_tool(self, name: str, version: str) -> RegisteredToolIdentity:
        return RegisteredToolIdentity(
            name=name,
            version=version,
            executable_sha256=TWO_SHA if name == "samtools" else THREE_SHA,
            registry_record_sha256=ONE_SHA,
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
    reverse: bool = False,
    paired: bool = False,
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
                record.flag = 4 | (16 if reverse else 0)
                if paired:
                    record.flag |= 1 | 8
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


def _disk_budget(tmp_path: Path, *, minimum: int = 10_000_000) -> AlignmentDiskBudget:
    return measure_alignment_disk_budget(
        tmp_path,
        tmp_path,
        combined_bam_bytes=minimum,
        sort_temporary_bytes=minimum,
        aligned_bam_bytes=minimum,
        index_bytes=minimum,
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


@pytest.mark.parametrize("layout", ["reverse", "paired"])
def test_rejects_unaligned_layouts_that_cannot_preserve_modification_tags(
    tmp_path: Path,
    layout: str,
) -> None:
    path = tmp_path / "unsupported-layout.bam"
    _write_bam(
        path,
        reverse=layout == "reverse",
        paired=layout == "paired",
    )

    with pytest.raises(ModbamIntakeError, match="non_primary_unmapped_records=1"):
        _inspect([path])


def test_pathname_swap_cannot_cross_bind_digest_and_scan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "source.bam"
    replacement = tmp_path / "replacement.bam"
    _write_bam(path)
    _write_bam(replacement, sequences=("CCCCC",), mm="C+m?,0;", ml=(190,))
    original_snapshot = modbam_intake._snapshot_source

    def swap_after_snapshot(source, snapshot, *, max_bytes):
        result = original_snapshot(source, snapshot, max_bytes=max_bytes)
        os.replace(replacement, path)
        return result

    monkeypatch.setattr(modbam_intake, "_snapshot_source", swap_after_snapshot)
    with pytest.raises(ModbamIntakeError, match="changed during validation") as caught:
        _inspect([path])
    assert path.name not in str(caught.value)


def test_same_size_rewrite_with_restored_mtime_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "source.bam"
    _write_bam(path)
    original_snapshot = modbam_intake._snapshot_source

    def rewrite_after_snapshot(source, snapshot, *, max_bytes):
        result = original_snapshot(source, snapshot, max_bytes=max_bytes)
        before = path.stat()
        with path.open("r+b") as handle:
            handle.seek(-1, os.SEEK_END)
            final_byte = handle.read(1)
            handle.seek(-1, os.SEEK_END)
            handle.write(bytes([final_byte[0] ^ 1]))
            handle.flush()
            os.fsync(handle.fileno())
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        assert path.stat().st_size == before.st_size
        assert path.stat().st_mtime_ns == before.st_mtime_ns
        return result

    monkeypatch.setattr(modbam_intake, "_snapshot_source", rewrite_after_snapshot)
    with pytest.raises(ModbamIntakeError, match="changed during validation") as caught:
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
    total_bytes = manifest.ordered_chunks[0].size_bytes
    budget = _disk_budget(tmp_path)

    plan = build_grch38_alignment_plan(
        manifest,
        registry=SyntheticAlignmentRegistry(),
        reference_id="reference.grch38.test",
        reference_version="v1",
        samtools_version="test-version",
        minimap2_version="test-version",
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
    assert "--secondary=no" in plan.stages[2].argv
    assert plan.expected_input_bytes == total_bytes
    assert plan.estimated_peak_workspace_bytes == 20_000_000
    assert plan.estimated_output_bytes == 20_000_000
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
        "secondary_count",
        "supplementary_accounting",
        "reference_identity",
        "sort_order",
        "index_integrity",
        "record_partition",
    }
    serialized = plan.model_dump_json()
    assert str(tmp_path) not in serialized
    assert path.name not in serialized
    assert validate_registered_alignment_plan(
        ModbamAlignmentPlan.model_validate_json(serialized),
        SyntheticAlignmentRegistry(),
    ) == plan

    for stage_index, argv_index, replacement in (
        (1, 3, "MM,ML"),
        (2, 4, "--secondary=yes"),
        (2, 6, "/private/reference/unregistered.mmi"),
    ):
        payload = plan.model_dump(mode="json")
        payload["stages"][stage_index]["argv"][argv_index] = replacement
        with pytest.raises(ValueError, match="plan digest"):
            ModbamAlignmentPlan.model_validate(payload)

    payload = plan.model_dump(mode="json")
    payload["stages"][3]["stdin_from_stage"] = "emit_tagged_fastq"
    payload["plan_sha256"] = hashlib.sha256(
        canonical_json_bytes(
            {key: value for key, value in payload.items() if key != "plan_sha256"}
        )
    ).hexdigest()
    with pytest.raises(ValueError, match="pipe topology"):
        ModbamAlignmentPlan.model_validate(payload)

    payload = plan.model_dump(mode="json")
    payload["reference"]["fasta_sha256"] = THREE_SHA
    payload["plan_sha256"] = hashlib.sha256(
        canonical_json_bytes(
            {key: value for key, value in payload.items() if key != "plan_sha256"}
        )
    ).hexdigest()
    rebound = ModbamAlignmentPlan.model_validate(payload)
    with pytest.raises(ModbamIntakeError, match="reference registry binding"):
        validate_registered_alignment_plan(rebound, SyntheticAlignmentRegistry())


def test_alignment_plan_rejects_understated_disk_ceiling(tmp_path: Path) -> None:
    path = tmp_path / "private.bam"
    _write_bam(path)
    manifest = _inspect([path]).private_manifest
    with pytest.raises(ModbamIntakeError, match="derived minimum"):
        build_grch38_alignment_plan(
            manifest,
            registry=SyntheticAlignmentRegistry(),
            reference_id="reference.grch38.test",
            reference_version="v1",
            samtools_version="test-version",
            minimap2_version="test-version",
            disk_budget=measure_alignment_disk_budget(
                tmp_path,
                tmp_path,
                combined_bam_bytes=1,
                sort_temporary_bytes=1,
                aligned_bam_bytes=1,
                index_bytes=1,
            ),
        )


@pytest.mark.skipif(
    shutil.which("samtools") is None or shutil.which("minimap2") is None,
    reason="local golden pipeline requires samtools and minimap2",
)
def test_real_alignment_pipeline_preserves_forward_mm_ml_mn_tags(
    tmp_path: Path,
) -> None:
    import pysam

    generator = random.Random(7)
    reference_sequence = "".join(
        generator.choice("ACGT") for _ in range(6_000)
    )
    read_sequence = reference_sequence[2_000:3_000]
    reference = tmp_path / "reference.fa"
    index = tmp_path / "reference.mmi"
    unaligned = tmp_path / "unaligned.bam"
    fastq = tmp_path / "tagged.fastq"
    sam = tmp_path / "aligned.sam"
    aligned = tmp_path / "aligned.sorted.bam"
    reference.write_text(f">synthetic-contig\n{reference_sequence}\n")
    _write_bam(
        unaligned,
        sequences=(read_sequence,),
        mm="C+m?,0;",
        ml=(211,),
    )

    subprocess.run(
        ["minimap2", "-d", os.fspath(index), os.fspath(reference)],
        check=True,
        capture_output=True,
    )
    with fastq.open("wb") as output:
        subprocess.run(
            ["samtools", "fastq", "-T", "MM,ML,MN", os.fspath(unaligned)],
            check=True,
            stdout=output,
            stderr=subprocess.PIPE,
        )
    with sam.open("wb") as output:
        subprocess.run(
            [
                "minimap2",
                "-a",
                "-x",
                "map-ont",
                "--secondary=no",
                "-y",
                os.fspath(index),
                os.fspath(fastq),
            ],
            check=True,
            stdout=output,
            stderr=subprocess.PIPE,
        )
    subprocess.run(
        [
            "samtools",
            "sort",
            "-@",
            "1",
            "-m",
            "256M",
            "-o",
            os.fspath(aligned),
            os.fspath(sam),
        ],
        check=True,
        capture_output=True,
    )

    with pysam.AlignmentFile(aligned, "rb") as bam:
        records = list(bam.fetch(until_eof=True))
    assert len(records) == 1
    record = records[0]
    assert not record.is_reverse
    assert not record.is_secondary
    assert record.get_tag("MM") == "C+m?,0;"
    assert tuple(record.get_tag("ML")) == (211,)
    assert record.get_tag("MN") == len(read_sequence)
