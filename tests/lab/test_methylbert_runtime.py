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
        "installed_commit_sha": runtime.PINNED_UPSTREAM_COMMIT,
    }
    values.update(changes)
    return runtime.require_ready(_bundle(), **values)


def test_install_identity_requires_pinned_upstream_git_url_and_sha(monkeypatch):
    class Distribution:
        def __init__(self, direct):
            self.direct = direct

        def read_text(self, _name):
            return json.dumps(self.direct)

    valid = {
        "url": "https://github.com/CompEpigen/methylbert.git",
        "vcs_info": {"vcs": "git", "commit_id": runtime.PINNED_UPSTREAM_COMMIT},
    }
    monkeypatch.setattr(runtime.metadata, "distribution", lambda _name: Distribution(valid))
    assert runtime.installed_commit() == runtime.PINNED_UPSTREAM_COMMIT
    monkeypatch.setattr(runtime.metadata, "distribution", lambda _name: Distribution({**valid, "url": "https://example.org/model.git"}))
    with pytest.raises(runtime.RuntimeRefusal, match="pinned upstream repository"):
        runtime.installed_commit()


def test_readiness_requires_pinned_model_reference_depth_ont_and_gpu():
    assert _ready() is None
    for change, message in (
        ({"installed_commit_sha": "b" * 40}, "pinned commit"),
        ({"reference_sha256": "b" * 64}, "reference differs"),
        ({"eligible_reads": 2_499_999}, "2,500,000"),
        ({"eligible_reads": True}, "2,500,000"),
        ({"platform": "illumina"}, "declared ONT"),
        ({"gpu_available": False}, "CUDA GPU"),
    ):
        with pytest.raises(runtime.RuntimeRefusal, match=message):
            _ready(**change)


def _aggregate(path, **changes):
    values = {
        "schema": runtime.AGGREGATE_SCHEMA, "model_id": "tumour-normal",
        "tumour_fraction": 0.2, "normal_fraction": 0.8, "classified_reads": 50,
    }
    values.update(changes)
    path.write_text(json.dumps(values))


def test_only_small_finite_aggregate_without_per_read_fields_is_publishable(tmp_path):
    path = tmp_path / "aggregate.json"
    _aggregate(path)
    result = runtime.read_aggregate(path, model_id="tumour-normal")
    assert (result.tumour_fraction, result.classified_reads) == (0.2, 50)
    for change, message in (
        ({"read_name": "private"}, "unexpected fields"),
        ({"normal_fraction": 0.7}, "fractions"),
        ({"tumour_fraction": float("nan")}, "fractions"),
        ({"classified_reads": 0}, "no classified reads"),
    ):
        _aggregate(path, **change)
        with pytest.raises(runtime.RuntimeRefusal, match=message):
            runtime.read_aggregate(path, model_id="tumour-normal")
    path.write_bytes(b"x" * (runtime.MAX_AGGREGATE_BYTES + 1))
    with pytest.raises(runtime.RuntimeRefusal, match="oversized"):
        runtime.read_aggregate(path, model_id="tumour-normal")


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
        model_id="tumour-normal",
    )
    assert result.tumour_fraction == 0.2
    assert seen["timeout_seconds"] == runtime.MAX_WALL_SECONDS
    assert seen["log_limit_bytes"] == runtime.MAX_LOG_BYTES
    with pytest.raises(runtime.RuntimeRefusal, match="new file"):
        runtime.run_worker(
            [sys.executable, "worker.py"], scratch=scratch, env={}, aggregate_path=aggregate,
            model_id="tumour-normal",
        )
