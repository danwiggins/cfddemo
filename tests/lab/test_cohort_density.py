"""Density bands use publication-time histograms, compatible sources, and self exclusion."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import threading
from types import SimpleNamespace

import pytest

from traceback_lab.cohort_contracts import CohortGeneration, CohortMember, CohortResult, SizeDensityVector
from traceback_lab import cohort_density
from traceback_lab.cohort_density import capture_density_vectors, project_density_band
from traceback_lab import cohorts
from traceback_lab.cohorts import add_sample, build_cohort, read_current
from traceback_lab.envelope import Metric, ResultEnvelope, provenance
from traceback_lab.identity import parameters_sha256
from traceback_lab.problems import LabProblem
from traceback_lab.store import LabStore


PARAMETERS = "b" * 64
COMPAT = "a" * 64
ATTEMPT = "a-20261016T123456789012-abcdef12"


def _counts(a: int, b: int = 0) -> tuple[int, ...]:
    values = [0] * 1000
    values[99] = a   # 100 bp
    values[100] = b  # 101 bp
    return tuple(values)


def _member(n: int, *, counts: tuple[int, ...] | None = None, compat: str = COMPAT) -> CohortMember:
    vector = SizeDensityVector(attempt_id=ATTEMPT, artifact_sha256="c" * 64, count=counts) if counts else None
    return CohortMember(
        source_id=f"{n:064x}", input_id=f"{100 + n:064x}", aligner_identity="minimap2 2.28",
        results=(CohortResult(
            algorithm_id="fragment-size", parameters_sha256=PARAMETERS,
            cohort_compat_key=compat, metrics={"mode_bp": 100}, size_density=vector,
        ),),
    )


def test_generation_band_is_normalized_min_max_and_excludes_target() -> None:
    target = _member(1, counts=_counts(100))
    members = (target, *(_member(n, counts=_counts(n, 10 - n % 5)) for n in range(2, 12)),
               _member(12, counts=_counts(1), compat="d" * 64))
    generation = CohortGeneration(cohort_id="healthy-v1", version=3, members=members)

    band = project_density_band(generation, target, target.source_id, PARAMETERS, "ready")
    assert band["state"] == "ready" and band["eligible_n"] == 10
    assert band["span_bp"] == list(range(50, 701))
    expected = [n / (n + 10 - n % 5) for n in range(2, 12)]
    assert band["low"][50] == pytest.approx(min(expected))   # 100 bp
    assert band["high"][50] == pytest.approx(max(expected))
    assert band["excluded"] == {"incompatible": 1, "self": 1}
    assert project_density_band(generation, target, target.source_id, "e" * 64, "ready")["state"] == "target_unavailable"


def test_old_generation_reports_missing_pinned_vectors() -> None:
    target = _member(1)
    generation = CohortGeneration(cohort_id="healthy-v1", version=1, members=(
        target, *(_member(n) for n in range(2, 12)),
    ))
    band = project_density_band(generation, target, target.source_id, PARAMETERS, "ready")
    assert band["state"] == "old_generation_no_vectors"
    assert band["eligible_n"] == 0 and band["excluded"] == {"missing_pinned_vector": 10, "self": 1}
    assert band["low"] == [] and band["high"] == []


def test_capture_copies_exact_selected_artifact_and_does_not_drift(tmp_path, monkeypatch) -> None:
    store = LabStore(tmp_path)
    member = _member(1)
    attempt = store.results_dir / member.input_id / "fragment-size" / PARAMETERS / "attempts" / ATTEMPT
    artifacts = attempt / "artifacts"
    artifacts.mkdir(parents=True)
    panel = {"span_bp": list(range(1, 1001)), "count": list(_counts(3, 1))}
    content = json.dumps(panel).encode()
    artifact = artifacts / "size_hist.json"
    artifact.write_bytes(content)
    monkeypatch.setattr(store, "selected_attempt", lambda *args: attempt)
    monkeypatch.setattr(store, "read_result", lambda *args: SimpleNamespace(
        status="ok", panels=[SimpleNamespace(panel_id="size-density", data_ref="artifacts/size_hist.json")],
    ))

    captured = capture_density_vectors(store, (member,))[0]
    vector = captured.results[0].size_density
    assert vector is not None and vector.count == _counts(3, 1)
    assert vector.artifact_sha256 == hashlib.sha256(content).hexdigest()
    assert vector.attempt_id == ATTEMPT and vector.denominator == 4
    artifact.write_bytes(json.dumps({**panel, "count": list(_counts(1, 3))}).encode())
    assert vector.count == _counts(3, 1)
    assert CohortGeneration(cohort_id="healthy-v1", version=1, members=(captured,)).model_dump(mode="json")["members"][0]["results"][0]["size_density"]["count"][99] == 3

    artifact.unlink()
    outside = tmp_path / "outside.json"
    outside.write_bytes(content)
    artifact.symlink_to(outside)
    with pytest.raises(LabProblem) as error:
        capture_density_vectors(store, (member,))
    assert "fragment-size density" in error.value.cause
    assert outside.read_bytes() == content


def test_size_density_contract_rejects_bad_denominator_and_dimension() -> None:
    with pytest.raises(ValueError):
        SizeDensityVector(attempt_id=ATTEMPT, artifact_sha256="c" * 64, count=(0,) * 1000)
    with pytest.raises(ValueError):
        SizeDensityVector(attempt_id=ATTEMPT, artifact_sha256="c" * 64, count=(1,) * 999)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO regression requires POSIX")
def test_artifact_fifo_is_rejected_without_blocking(tmp_path) -> None:
    fifo = tmp_path / "size_hist.json"
    os.mkfifo(fifo)
    outcomes: list[BaseException] = []

    def read() -> None:
        try:
            cohort_density._read_attempt_file(tmp_path, (fifo.name,), f"{1:064x}")
        except BaseException as exc:
            outcomes.append(exc)

    thread = threading.Thread(target=read, daemon=True)
    thread.start()
    thread.join(timeout=2)
    assert not thread.is_alive(), "a FIFO replacement blocked cohort publication"
    assert len(outcomes) == 1 and isinstance(outcomes[0], LabProblem)


def test_artifact_read_rejects_symlinked_ancestor(tmp_path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    (real / "size_hist.json").write_text("{}")
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    with pytest.raises(LabProblem) as error:
        cohort_density._read_attempt_file(alias, ("size_hist.json",), f"{1:064x}")
    assert "unreadable or unsafe" in error.value.cause


def test_artifact_read_checks_bytes_after_a_file_grows(tmp_path, monkeypatch) -> None:
    (tmp_path / "size_hist.json").write_bytes(b"x" * 65)
    monkeypatch.setattr(cohort_density, "_MAX_ARTIFACT_BYTES", 64)
    monkeypatch.setattr(cohort_density.os, "fstat", lambda fd: SimpleNamespace(
        st_mode=stat.S_IFREG, st_nlink=1, st_size=10,
    ))
    with pytest.raises(LabProblem) as error:
        cohort_density._read_attempt_file(tmp_path, ("size_hist.json",), f"{1:064x}")
    assert "larger than the artifact limit" in error.value.cause


def test_build_pins_vector_before_selected_attempt_changes(tmp_path, monkeypatch) -> None:
    from tests.lab.test_cohort_store import _sample

    store = LabStore(tmp_path)
    algorithm = SimpleNamespace(id="fragment-size", version="1", parameters={}, tools=[], upstream=())
    monkeypatch.setattr(cohorts, "load_registry", lambda: (algorithm,))
    source_id = _sample(store, 1)
    input_id = f"{101:064x}"
    params = parameters_sha256(
        algorithm_id="fragment-size", algorithm_version="1", parameters={},
        tools=[], reference_sha256=f"{900:064x}",
    )

    def publish(counts: tuple[int, ...]) -> None:
        envelope = ResultEnvelope(
            algorithm_id="fragment-size", algorithm_version="1", status="ok",
            provenance=provenance(tool="fixture", parameters_sha256=params,
                                  source_id=source_id, input_id=input_id, reference_id="synthetic"),
            metrics=[Metric(metric_id="mode_bp", value=100)],
            panels=[{"panel_id": "size-density", "kind": "density", "data_ref": "artifacts/size_hist.json"}],
        )
        artifact = json.dumps({"span_bp": list(range(1, 1001)), "count": counts}).encode()
        store.publish_result(envelope, input_id=input_id, artifacts={"size_hist.json": artifact})

    publish(_counts(3, 1))
    add_sample(store, "healthy", source_id)
    built = build_cohort(store, "healthy")
    assert built.members[0].results[0].size_density.count == _counts(3, 1)
    publish(_counts(1, 3))
    assert read_current(store, "healthy").members[0].results[0].size_density.count == _counts(3, 1)


def test_valid_zero_density_member_publishes_without_vector_but_malformed_still_refuses(tmp_path, monkeypatch) -> None:
    from tests.lab.test_cohort_store import _sample

    store = LabStore(tmp_path)
    algorithm = SimpleNamespace(id="fragment-size", version="1", parameters={}, tools=[], upstream=())
    monkeypatch.setattr(cohorts, "load_registry", lambda: (algorithm,))
    source_id = _sample(store, 1)
    input_id = f"{101:064x}"
    params = parameters_sha256(
        algorithm_id="fragment-size", algorithm_version="1", parameters={},
        tools=[], reference_sha256=f"{900:064x}",
    )

    def publish(count: list[int]) -> None:
        envelope = ResultEnvelope(
            algorithm_id="fragment-size", algorithm_version="1", status="ok",
            provenance=provenance(tool="fixture", parameters_sha256=params,
                                  source_id=source_id, input_id=input_id, reference_id="synthetic"),
            metrics=[Metric(metric_id="mode_bp", value=None)],
            panels=[{"panel_id": "size-density", "kind": "density", "data_ref": "artifacts/size_hist.json"}],
        )
        store.publish_result(envelope, input_id=input_id, artifacts={"size_hist.json": json.dumps({
            "span_bp": list(range(1, 1001)), "count": count,
        }).encode()})

    zero = [0] * 1000
    zero[9] = 3  # A valid histogram with only 10 bp spans.
    publish(zero)
    add_sample(store, "healthy", source_id)
    built = build_cohort(store, "healthy")
    assert built.members[0].results[0].size_density is None
    assert read_current(store, "healthy").version == 1
    previous = built.members[0].results[0].model_copy(update={
        "size_density": SizeDensityVector(attempt_id=ATTEMPT, artifact_sha256="c" * 64, count=_counts(1)),
    })
    stale = built.members[0].model_copy(update={"results": (previous,)})
    assert capture_density_vectors(store, (stale,))[0].results[0].size_density is None

    publish([0] * 999)
    with pytest.raises(LabProblem) as error:
        build_cohort(store, "healthy")
    assert "fragment-size density" in error.value.cause
    assert read_current(store, "healthy").version == 1
