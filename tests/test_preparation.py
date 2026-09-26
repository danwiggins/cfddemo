"""Offline tests for bounded BAM-derived length preparation."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest

import evidence_inspector.preparation as preparation
from evidence_inspector.models import SelectionParameters
from evidence_inspector.preparation import (
    LENGTHS_FILE_NAME,
    MANIFEST_FILE_NAME,
    PreparationError,
    PreparationManifest,
    prepare_length_artifact,
)


@dataclass
class FakeRecord:
    query_name: str | None
    query_sequence: object | None
    is_secondary: bool = False
    is_supplementary: bool = False


class SizedSequence:
    def __init__(self, length: int) -> None:
        self.length = length

    def __len__(self) -> int:
        return self.length


def _inputs(tmp_path: Path, count: int = 1) -> list[Path]:
    paths = [tmp_path / f"private-name-{index}.bam" for index in range(count)]
    for path in paths:
        path.write_bytes(b"not a real BAM; record source is injected")
    return paths


def _source(records_by_name: dict[str, list[FakeRecord]]):
    return lambda path: iter(records_by_name[path.name])


def _parameters(**updates: object) -> SelectionParameters:
    values: dict[str, object] = {
        "ordering_rule": "synthetic deterministic test order",
        "max_accepted_reads": 100,
        "max_inspected_records": 100,
        "max_elapsed_seconds": 60.0,
        "max_serialized_artifact_bytes": 2_097_152,
        "max_read_length_bp": 1_000_000,
    }
    values.update(updates)
    return SelectionParameters(**values)


def test_preparation_filters_deduplicates_and_publishes_privacy_safe_bundle(
    tmp_path: Path,
) -> None:
    paths = _inputs(tmp_path, 2)
    records = {
        paths[0].name: [
            FakeRecord("secondary-secret", "A", is_secondary=True),
            FakeRecord("supplementary-secret", "AA", is_supplementary=True),
            FakeRecord(None, "AAA"),
            FakeRecord("missing-secret", None),
            FakeRecord("zero-secret", ""),
            FakeRecord("oversized-secret", "A" * 21),
            FakeRecord("duplicate-secret", "A" * 8),
            FakeRecord("duplicate-secret", "A" * 9),
            FakeRecord("eligible-after-invalid", None),
            FakeRecord("eligible-after-invalid", "A" * 10),
        ],
        paths[1].name: [
            FakeRecord("cross-file-secret", "A" * 12),
            FakeRecord("duplicate-secret", "A" * 13),
        ],
    }
    output = tmp_path / "published"
    manifest = prepare_length_artifact(
        reversed(paths),
        output,
        record_source=_source(records),
        selection_parameters=_parameters(max_read_length_bp=20),
    )

    assert json.loads((output / LENGTHS_FILE_NAME).read_text()) == [8, 10, 12]
    assert PreparationManifest.model_validate_json(
        (output / MANIFEST_FILE_NAME).read_bytes()
    ) == manifest
    assert manifest.inspected_count == 12
    assert manifest.accepted_count == 3
    assert manifest.stop_reason == "complete_input_scan"
    assert manifest.scanned_complete_input is True
    assert manifest.exclusions == {
        "duplicate_read_id": 2,
        "missing_read_id": 1,
        "missing_sequence": 2,
        "oversized_read": 1,
        "secondary": 1,
        "supplementary": 1,
        "zero_length": 1,
    }
    assert [item.order for item in manifest.ordered_inputs] == [0, 1]
    serialized = manifest.model_dump_json()
    assert str(tmp_path) not in serialized
    assert all(path.name not in serialized for path in paths)
    assert "duplicate-secret" not in serialized
    assert "derived_lengths" == manifest.artifact.hash_scope
    assert manifest.artifact.sha256


@pytest.mark.parametrize(
    ("parameter_updates", "records", "expected_reason", "expected_accepted"),
    [
        (
            {"max_accepted_reads": 1},
            [FakeRecord("one", "A"), FakeRecord("two", "AA")],
            "accepted_read_cap",
            1,
        ),
        (
            {"max_inspected_records": 1},
            [FakeRecord("one", "A"), FakeRecord("two", "AA")],
            "inspected_record_cap",
            1,
        ),
        (
            {
                "max_accepted_reads": 1000,
                "max_inspected_records": 1000,
                "max_serialized_artifact_bytes": 1024,
            },
            [
                FakeRecord(f"read-{index}", SizedSequence(1_000_000))
                for index in range(200)
            ],
            "serialized_artifact_cap",
            127,
        ),
    ],
)
def test_preparation_stops_at_injected_caps(
    tmp_path: Path,
    parameter_updates: dict[str, object],
    records: list[FakeRecord],
    expected_reason: str,
    expected_accepted: int,
) -> None:
    path = _inputs(tmp_path)[0]
    output = tmp_path / "published"
    manifest = prepare_length_artifact(
        [path],
        output,
        record_source=_source({path.name: records}),
        selection_parameters=_parameters(**parameter_updates),
    )

    assert manifest.stop_reason == expected_reason
    assert manifest.accepted_count == expected_accepted
    assert manifest.scanned_complete_input is False
    assert manifest.artifact.size_bytes <= (
        manifest.selection_parameters.max_serialized_artifact_bytes
    )


class AdvancingClock:
    def __init__(self, step: float) -> None:
        self.value = -step
        self.step = step

    def __call__(self) -> float:
        self.value += self.step
        return self.value


def test_preparation_time_cap_is_checked_between_records_and_records_overrun(
    tmp_path: Path,
) -> None:
    path = _inputs(tmp_path)[0]
    manifest = prepare_length_artifact(
        [path],
        tmp_path / "published",
        record_source=_source(
            {path.name: [FakeRecord("one", "A"), FakeRecord("two", "AA")]}
        ),
        selection_parameters=_parameters(max_elapsed_seconds=1.0),
        monotonic=AdvancingClock(0.6),
    )

    assert manifest.stop_reason == "elapsed_time_cap"
    assert manifest.accepted_count == 1
    assert manifest.elapsed_ms == 1800
    assert manifest.elapsed_overrun_ms == 800


def test_preparation_failure_publishes_neither_artifact_nor_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _inputs(tmp_path)[0]
    output = tmp_path / "published"
    real_write = preparation._write_file

    def fail_manifest(target: Path, content: bytes) -> None:
        if target.name == MANIFEST_FILE_NAME:
            raise OSError("synthetic write failure")
        real_write(target, content)

    monkeypatch.setattr(preparation, "_write_file", fail_manifest)
    with pytest.raises(OSError, match="synthetic write failure"):
        prepare_length_artifact(
            [path],
            output,
            record_source=_source({path.name: [FakeRecord("one", "A")]}),
            selection_parameters=_parameters(),
        )

    assert not output.exists()
    assert not list(tmp_path.glob(".published.*"))


def test_preparation_rejects_empty_results_reader_failure_and_overwrite(
    tmp_path: Path,
) -> None:
    path = _inputs(tmp_path)[0]
    output = tmp_path / "published"
    with pytest.raises(PreparationError, match="no eligible"):
        prepare_length_artifact(
            [path],
            output,
            record_source=_source({path.name: [FakeRecord("missing", None)]}),
            selection_parameters=_parameters(),
        )
    assert not output.exists()

    def broken_source(_: Path):
        raise RuntimeError("private path or record ID must not escape")

    with pytest.raises(PreparationError) as error:
        prepare_length_artifact(
            [path],
            output,
            record_source=broken_source,
            selection_parameters=_parameters(),
        )
    assert "private path" not in str(error.value)
    assert not output.exists()

    prepare_length_artifact(
        [path],
        output,
        record_source=_source({path.name: [FakeRecord("one", "A")]}),
        selection_parameters=_parameters(),
    )
    with pytest.raises(PreparationError, match="immutable"):
        prepare_length_artifact(
            [path],
            output,
            record_source=_source({path.name: [FakeRecord("two", "AA")]}),
            selection_parameters=_parameters(),
        )


def test_preparation_enforces_two_mib_absolute_artifact_cap(
    tmp_path: Path,
) -> None:
    path = _inputs(tmp_path)[0]
    with pytest.raises(PreparationError, match="2 MiB"):
        prepare_length_artifact(
            [path],
            tmp_path / "published",
            record_source=_source({path.name: [FakeRecord("one", "A")]}),
            selection_parameters=_parameters(
                max_serialized_artifact_bytes=2_097_153
            ),
        )


def test_default_pysam_source_streams_runtime_generated_bam(tmp_path: Path) -> None:
    import pysam

    bam_path = tmp_path / "runtime-private.bam"
    header = {"HD": {"VN": "1.6", "SO": "unknown"}}
    with pysam.AlignmentFile(bam_path, "wb", header=header) as bam:
        for name, sequence, flag in (
            ("accepted", "A" * 17, 4),
            ("secondary", "A" * 19, 4 | 256),
            ("duplicate", "A" * 23, 4),
            ("duplicate", "A" * 29, 4),
        ):
            record = pysam.AlignedSegment()
            record.query_name = name
            record.query_sequence = sequence
            record.flag = flag
            bam.write(record)

    output = tmp_path / "published"
    manifest = prepare_length_artifact(
        [bam_path],
        output,
        selection_parameters=_parameters(),
    )

    assert json.loads((output / LENGTHS_FILE_NAME).read_text()) == [17, 23]
    assert manifest.inspected_count == 4
    assert manifest.exclusions == {
        "duplicate_read_id": 1,
        "secondary": 1,
    }
