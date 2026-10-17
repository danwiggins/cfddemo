"""D30 inventory separates literal AC4 input bytes from the full footprint."""

import json

import pytest

from scripts.validation.lab_storage_accounting import InventoryError, account


def _write(root, name, size):
    path = root / "lab" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)


def _fixture(root):
    source = "a" * 64
    full = "b" * 64
    derived = "c" * 64
    record = {"source_id": source, "inputs": [{"input_id": full, "kind": "aligned"}],
              "derived": [{"input_id": derived, "parent_input_id": full}]}
    path = root / "lab" / "sources" / f"{source}.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(record))
    _write(root, f"inputs/{full}/aligned.bam", 100)
    _write(root, f"inputs/{full}/aligned.bam.bai", 10)
    _write(root, f"inputs/{derived}/manifest.json", 2)
    _write(root, "inputs/index.json", 3)
    _write(root, f"results/{full}/read-summary/p/attempts/a/artifacts/summary", 80)
    _write(root, "refs/reference.mmi", 20)
    return source, full


def test_reports_literal_and_complete_bytes_separately(tmp_path):
    source, full = _fixture(tmp_path)
    report = account(tmp_path, source)
    assert report["full_input_id"] == full
    assert report["aligned_bam_plus_index_bytes"] == 110
    assert report["literal_lab_inputs_bytes"] == 115
    assert report["literal_ac4_at_most_1_2"] is True
    assert report["sample_result_bytes"] == 80
    assert report["shared_mmi_bytes"] == 20
    assert report["complete_lab_persistent_bytes"] > report["literal_lab_inputs_bytes"]
    assert report["complete_lab_persistent_bytes"] == sum(report[key] for key in (
        "literal_lab_inputs_bytes", "sample_result_bytes", "other_result_bytes",
        "shared_mmi_bytes", "other_lab_bytes",
    ))


def test_other_sample_prevents_single_sample_ac4_claim(tmp_path):
    source, _ = _fixture(tmp_path)
    _write(tmp_path, f"inputs/{'d' * 64}/aligned.bam", 10)
    report = account(tmp_path, source)
    assert report["other_input_bytes"] == 10
    assert report["literal_ac4_single_sample_applicable"] is False
    assert report["literal_ac4_at_most_1_2"] is None


@pytest.mark.parametrize("kind", ["empty_directory", "zero_byte_file"])
def test_other_input_presence_prevents_single_sample_claim(tmp_path, kind):
    source, _ = _fixture(tmp_path)
    if kind == "empty_directory":
        (tmp_path / "lab" / "inputs" / ("d" * 64)).mkdir()
    else:
        _write(tmp_path, "inputs/unrelated.tmp", 0)
    report = account(tmp_path, source)
    assert report["other_input_bytes"] == 0
    assert report["literal_ac4_single_sample_applicable"] is False
    assert report["literal_ac4_at_most_1_2"] is None


def test_ratio_over_limit_is_reported_false(tmp_path):
    source, full = _fixture(tmp_path)
    _write(tmp_path, f"inputs/{full}/extra", 30)
    report = account(tmp_path, source)
    assert report["literal_lab_inputs_bytes"] == 145
    assert report["literal_ac4_at_most_1_2"] is False


def test_refuses_incomplete_or_unsafe_inventory(tmp_path):
    source, full = _fixture(tmp_path)
    with pytest.raises(InventoryError, match="entry limit"):
        account(tmp_path, source, max_entries=2)
    (tmp_path / "lab" / "inputs" / full / "aligned.bam.bai").unlink()
    with pytest.raises(InventoryError, match="unavailable"):
        account(tmp_path, source)


@pytest.mark.parametrize("entry", [{"kind": "aligned"}, {"kind": "aligned", "input_id": None}])
def test_refuses_malformed_full_input_record(tmp_path, entry):
    source, _ = _fixture(tmp_path)
    path = tmp_path / "lab" / "sources" / f"{source}.json"
    data = json.loads(path.read_text())
    data["inputs"] = [entry]
    path.write_text(json.dumps(data))
    with pytest.raises(InventoryError, match="full input_id"):
        account(tmp_path, source)


def test_prealigned_source_is_accepted(tmp_path):
    source, full = _fixture(tmp_path)
    path = tmp_path / "lab" / "sources" / f"{source}.json"
    data = json.loads(path.read_text())
    data["inputs"][0]["kind"] = "prealigned"
    path.write_text(json.dumps(data))
    assert account(tmp_path, source)["full_input_id"] == full
