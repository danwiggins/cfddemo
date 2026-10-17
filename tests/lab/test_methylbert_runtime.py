"""Model readiness and aggregate publishing are bounded before activation."""

from __future__ import annotations

import json
import sys

import pytest

import traceback_lab.methylbert_runtime as runtime
from traceback_lab.methylbert_bundle import Bundle
from traceback_runner.contained_process import ContainedResult


def _bundle() -> Bundle:
    return Bundle(
        model_id="tumour-normal", upstream_commit=runtime.PINNED_UPSTREAM_COMMIT,
        source="synthetic contract", terms="test-only", reference_sha256="a" * 64,
        n_mers=3, seq_len=150, files={},
    )


def _ready(**changes):
    values = {
        "reference_sha256": "a" * 64, "eligible_reads": 2_500_000,
        "platform": "ont", "gpu_available": True,
        "expected_code_sha256": "a" * 64,
    }
    values.update(changes)
    return runtime.require_ready(_bundle(), **values)


def test_install_identity_requires_pinned_git_source_and_hashes_record_listed_code(tmp_path, monkeypatch):
    code = tmp_path / "methylbert" / "data" / "bam.py"
    code.parent.mkdir(parents=True)
    code.write_text("original source\n")

    class Distribution:
        def __init__(self, direct):
            self.direct = direct

        def read_text(self, _name):
            return json.dumps(self.direct)

        files = ["methylbert/data/bam.py"]

        def locate_file(self, name):
            return tmp_path / name

    valid = {
        "url": "https://github.com/CompEpigen/methylbert.git",
        "vcs_info": {"vcs": "git", "commit_id": runtime.PINNED_UPSTREAM_COMMIT},
    }
    monkeypatch.setattr(runtime.metadata, "distribution", lambda _name: Distribution(valid))
    original = runtime.installed_identity()
    assert original.commit == runtime.PINNED_UPSTREAM_COMMIT
    assert len(original.sha256) == 64
    assert runtime.require_ready(
        _bundle(), reference_sha256="a" * 64, eligible_reads=2_500_000,
        platform="ont", gpu_available=True, expected_code_sha256=original.sha256,
    ) == original
    code.write_text("changed source\n")
    assert runtime.installed_identity().sha256 != original.sha256
    with pytest.raises(runtime.RuntimeRefusal, match="trusted byte digest"):
        runtime.require_ready(
            _bundle(), reference_sha256="a" * 64, eligible_reads=2_500_000,
            platform="ont", gpu_available=True, expected_code_sha256=original.sha256,
        )
    monkeypatch.setattr(runtime.metadata, "distribution", lambda _name: Distribution({**valid, "url": "https://example.org/model.git"}))
    with pytest.raises(runtime.RuntimeRefusal, match="pinned upstream repository"):
        runtime.installed_identity()
    for malformed in (None, 42, ["https://github.com/CompEpigen/methylbert.git"]):
        monkeypatch.setattr(runtime.metadata, "distribution", lambda _name: Distribution({**valid, "url": malformed}))
        with pytest.raises(runtime.RuntimeRefusal, match="invalid source URL"):
            runtime.installed_identity()


def test_readiness_requires_pinned_model_reference_depth_ont_and_gpu(monkeypatch):
    monkeypatch.setattr(runtime, "installed_identity", lambda: runtime.InstalledCodeIdentity(
        runtime.PINNED_UPSTREAM_COMMIT, "a" * 64,
    ))
    assert _ready().sha256 == "a" * 64
    for change, message in (
        ({"expected_code_sha256": "b" * 64}, "trusted byte digest"),
        ({"reference_sha256": "b" * 64}, "reference differs"),
        ({"eligible_reads": 2_499_999}, "2,500,000"),
        ({"eligible_reads": True}, "2,500,000"),
        ({"platform": "illumina"}, "declared ONT"),
        ({"gpu_available": False}, "CUDA GPU"),
        ({"gpu_available": "false"}, "CUDA GPU"),
    ):
        with pytest.raises(runtime.RuntimeRefusal, match=message):
            _ready(**change)
    monkeypatch.setattr(runtime, "installed_identity", lambda: runtime.InstalledCodeIdentity(
        "b" * 40, "a" * 64,
    ))
    with pytest.raises(runtime.RuntimeRefusal, match="pinned commit"):
        _ready()


def _aggregate(path, **changes):
    values = {
        "schema": runtime.AGGREGATE_SCHEMA, "model_id": "tumour-normal",
        "tumour_fraction": 0.2, "normal_fraction": 0.8, "classified_reads": 50,
    }
    values.update(changes)
    path.write_text(json.dumps(values))


def test_only_small_finite_aggregate_without_per_read_fields_is_well_formed(tmp_path):
    path = tmp_path / "aggregate.json"
    _aggregate(path)
    result = runtime.read_aggregate(path, model_id="tumour-normal", eligible_reads=runtime.MIN_ELIGIBLE_READS)
    assert (result.tumour_fraction, result.classified_reads) == (0.2, 50)
    _aggregate(path, classified_reads=runtime.MIN_ELIGIBLE_READS)
    assert runtime.read_aggregate(
        path, model_id="tumour-normal", eligible_reads=runtime.MIN_ELIGIBLE_READS,
    ).classified_reads == runtime.MIN_ELIGIBLE_READS
    for change, message in (
        ({"read_name": "private"}, "unexpected fields"),
        ({"normal_fraction": 0.7}, "fractions"),
        ({"tumour_fraction": float("nan")}, "fractions"),
        ({"tumour_fraction": 10**1000}, "fractions"),
        ({"classified_reads": 0}, "no classified reads"),
        ({"classified_reads": runtime.MIN_ELIGIBLE_READS + 1}, "more classified than eligible"),
    ):
        _aggregate(path, **change)
        with pytest.raises(runtime.RuntimeRefusal, match=message):
            runtime.read_aggregate(path, model_id="tumour-normal", eligible_reads=runtime.MIN_ELIGIBLE_READS)
    path.write_bytes(b"x" * (runtime.MAX_AGGREGATE_BYTES + 1))
    with pytest.raises(runtime.RuntimeRefusal, match="oversized"):
        runtime.read_aggregate(path, model_id="tumour-normal", eligible_reads=runtime.MIN_ELIGIBLE_READS)
    path.write_text('{"classified_reads":' + "9" * 5000 + "}")
    with pytest.raises(runtime.RuntimeRefusal):
        runtime.read_aggregate(path, model_id="tumour-normal", eligible_reads=runtime.MIN_ELIGIBLE_READS)
    path.write_text("[" * 3000 + "0" + "]" * 3000)
    with pytest.raises(runtime.RuntimeRefusal, match="invalid JSON"):
        runtime.read_aggregate(path, model_id="tumour-normal", eligible_reads=runtime.MIN_ELIGIBLE_READS)


def test_worker_launch_uses_contained_deadline_and_private_aggregate(tmp_path, monkeypatch):
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    aggregate = scratch / "aggregate.json"
    seen = {}

    def contained(argv, **kwargs):
        seen.update({"argv": argv, **kwargs})
        _aggregate(aggregate)
        return ContainedResult(tuple(argv), "exited", 0, 1.0, b"", b"", 0, 0, False, False)

    monkeypatch.setattr(runtime, "run_contained", contained)
    result = runtime.run_worker(
        [sys.executable, "worker.py"], scratch=scratch, env={}, aggregate_path=aggregate,
        model_id="tumour-normal", eligible_reads=runtime.MIN_ELIGIBLE_READS,
    )
    assert result.tumour_fraction == 0.2
    assert seen["timeout_seconds"] == runtime.MAX_WALL_SECONDS
    assert seen["log_limit_bytes"] == runtime.MAX_LOG_BYTES
    with pytest.raises(runtime.RuntimeRefusal, match="new file"):
        runtime.run_worker(
            [sys.executable, "worker.py"], scratch=scratch, env={}, aggregate_path=aggregate,
            model_id="tumour-normal", eligible_reads=runtime.MIN_ELIGIBLE_READS,
        )
    with pytest.raises(runtime.RuntimeRefusal, match="parent is missing"):
        runtime.run_worker(
            [sys.executable, "worker.py"], scratch=scratch, env={},
            aggregate_path=scratch / "missing" / "aggregate.json", model_id="tumour-normal",
            eligible_reads=runtime.MIN_ELIGIBLE_READS,
        )
    aggregate.unlink()
    monkeypatch.setattr(runtime, "run_contained", lambda *args, **kwargs: ContainedResult(
        tuple(args[0]), "exited", 1, 1.0, b"", b"", 0, 0, False, False,
    ))
    with pytest.raises(runtime.RuntimeRefusal, match="failed"):
        runtime.run_worker(
            [sys.executable, "worker.py"], scratch=scratch, env={}, aggregate_path=aggregate,
            model_id="tumour-normal", eligible_reads=runtime.MIN_ELIGIBLE_READS,
        )
