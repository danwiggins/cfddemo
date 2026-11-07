"""The per-algorithm memory cap: derivation, flags, enforcement, run-log fields.

One algorithm must not be able to OOM the workstation.  The e2e cases run a
fake algorithm that holds more memory than its cap; the aligner is never
capped (it is not an algorithm).
"""

from __future__ import annotations

import json
import os
import signal
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from traceback_lab import ingest, memory_cap, pipeline, registry
from traceback_lab.memory_cap import (
    DEFAULT_MEMORY_CAP_BYTES,
    GIB,
    MIB,
    MemoryCapPolicy,
    apply_memory_limit,
    looks_like_allocation_failure,
    memory_history,
    parse_memory_cap,
    select_memory_limit,
)
from traceback_lab.pipeline import Stage, run_pipeline
from traceback_lab.scrub import Scrubber
from traceback_lab.store import LabStore
from traceback_runner import cli as signed_cli

from .test_lab_run import _json, _lab, _registry

PY = sys.executable
HOG_BYTES = 768 * MIB
CAP = "512MiB"
CAP_BYTES = 512 * MIB


# -- flag values ----------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("4GiB", 4 * GIB),
        ("512MiB", 512 * MIB),
        ("1.5GiB", 3 * GIB // 2),
        ("2048KiB", 2 * MIB),
        ("3000000", 3_000_000),
        (" 2 gib ", 2 * GIB),
    ],
)
def test_parse_memory_cap_accepts_bytes_and_binary_suffixes(text: str, expected: int) -> None:
    assert parse_memory_cap(text) == expected


@pytest.mark.parametrize("text", ["", "abc", "4GB", "2G", "-1GiB", "100", "0MiB", "9999999TiB"])
def test_parse_memory_cap_rejects_other_spellings(text: str) -> None:
    with pytest.raises(ValueError):
        parse_memory_cap(text)


@pytest.mark.parametrize("value", ["2G", "4GB", "100", "0MiB", "lots"])
def test_bad_memory_cap_flag_is_a_usage_error(tmp_path: Path, capsys, value: str) -> None:
    code = signed_cli.main(["lab", "run", "latest", "--memory-cap", value, "--root", str(tmp_path / "root")])
    assert code == 2
    assert "argument --memory-cap:" in capsys.readouterr().err
    assert not (tmp_path / "root").exists()


def test_memory_cap_and_no_memory_cap_together_are_tbx_lab_002(tmp_path: Path, capsys) -> None:
    code, payload = _json(
        capsys, "run", "latest", "--memory-cap", "1GiB", "--no-memory-cap", "--root", str(tmp_path / "root"),
    )
    assert code == 2
    assert payload["data"]["code"] == "TBX-LAB-002"
    assert "--no-memory-cap" in payload["data"]["cause"]


# -- derivation ---------------------------------------------------------------------------------------
def _store_with_history(root: Path, runs: list[list[dict]]) -> LabStore:
    store = LabStore(root)
    for index, events in enumerate(runs):
        log = store.run_log(f"r-20261106-{index:04d}-abcd")
        for event in events:
            log.append(**event)
    return store


def _step_end(step: str, peak: int | None, tree: int | None = None, **extra) -> dict:
    return {"event": "step_end", "step": step, "status": "ok", "peak_rss_bytes": peak,
            "peak_tree_rss_bytes": tree, **extra}


@pytest.mark.parametrize(
    ("runs", "expected"),
    [
        pytest.param([], {}, id="no-history"),
        pytest.param([[_step_end("copy-number", 3 * GIB)]], {"copy-number": 3 * GIB}, id="one-run"),
        pytest.param(
            [[_step_end("copy-number", 1 * GIB)], [_step_end("copy-number", 3 * GIB, 5 * GIB)],
             [_step_end("copy-number@0.5", 7 * GIB), _step_end("read-summary", 100 * MIB)]],
            {"copy-number": 7 * GIB, "read-summary": 100 * MIB},
            id="many-runs-take-the-highest-including-derived-and-tree",
        ),
        pytest.param(
            [[_step_end("copy-number", 3 * GIB, status="failed", failure_reason="memory"),
              _step_end("copy-number", 1 * GIB)]],
            {"copy-number": 1 * GIB},
            id="steps-the-cap-killed-do-not-count",
        ),
        pytest.param(
            [[_step_end("copy-number", None), _step_end("copy-number", "lots"), _step_end("copy-number", True),
              {"event": "step_start", "step": "copy-number", "peak_rss_bytes": 9 * GIB},
              {"event": "step_end", "step": 7, "peak_rss_bytes": 9 * GIB}]],
            {},
            id="malformed-values-and-other-events-are-ignored",
        ),
    ],
)
def test_memory_history_is_the_highest_recorded_peak_per_algorithm(tmp_path: Path, runs, expected) -> None:
    store = _store_with_history(tmp_path / "root", runs)
    assert memory_history(store) == expected


@pytest.mark.parametrize(
    ("history", "floors", "algorithm", "cap", "basis", "observed"),
    [
        pytest.param({}, {}, "copy-number", 2 * GIB, "default", None, id="none"),
        pytest.param({"copy-number": 500 * MIB}, {}, "copy-number", 2 * GIB, "default", None,
                     id="one-small-prior-run-keeps-the-2GiB-floor"),
        pytest.param({"copy-number": 3 * GIB}, {}, "copy-number", 6 * GIB, "history", 3 * GIB,
                     id="prior-peak-doubles"),
        pytest.param({"copy-number": GIB + 1}, {}, "copy-number", 2 * GIB + 2, "history", GIB + 1,
                     id="just-above-half-the-floor"),
        pytest.param({"copy-number": GIB}, {}, "copy-number", 2 * GIB, "default", None,
                     id="exactly-half-the-floor-is-the-floor"),
        pytest.param({}, {"copy-number": 3 * GIB}, "copy-number", 3 * GIB, "floor", None, id="registry-floor"),
        pytest.param({"copy-number": GIB}, {"copy-number": 3 * GIB}, "copy-number", 3 * GIB, "floor", None,
                     id="history-below-the-floor"),
        pytest.param({"copy-number": 2 * GIB}, {"copy-number": 3 * GIB}, "copy-number", 4 * GIB, "history", 2 * GIB,
                     id="history-above-the-floor"),
        pytest.param({"copy-number": 3 * GIB}, {"copy-number": 3 * GIB}, "read-summary", 2 * GIB, "default", None,
                     id="other-algorithms-unaffected"),
    ],
)
def test_default_cap_is_max_of_floor_and_twice_history(history, floors, algorithm, cap, basis, observed) -> None:
    policy = MemoryCapPolicy(history=history, floors=floors)
    chosen = policy.for_algorithm(algorithm)
    assert (chosen.bytes, chosen.basis, chosen.observed_bytes) == (cap, basis, observed)
    assert chosen.log_fields() == {
        "memory_cap_bytes": cap, "memory_cap_basis": basis, "memory_cap_observed_bytes": observed,
    }


def test_override_and_disable_win_over_history_and_floors(tmp_path: Path) -> None:
    store = _store_with_history(tmp_path / "root", [[_step_end("copy-number", 3 * GIB)]])
    derived = MemoryCapPolicy.for_store(store, floors={"copy-number": 5 * GIB})
    assert derived.for_algorithm("copy-number").bytes == 6 * GIB
    override = MemoryCapPolicy.for_store(store, override_bytes=GIB, floors={"copy-number": 5 * GIB})
    assert override.for_algorithm("copy-number") == memory_cap.MemoryCap(GIB, "override")
    assert override.history == {}  # not read: the override applies to every algorithm
    assert override.flags() == {"memory_cap_bytes": GIB, "no_memory_cap": False}
    disabled = MemoryCapPolicy.for_store(store, disabled=True)
    assert disabled.for_algorithm("copy-number") == memory_cap.MemoryCap(None, "disabled")
    assert disabled.flags() == {"memory_cap_bytes": None, "no_memory_cap": True}


def test_registry_floor_table_feeds_the_default_policy(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(registry, "MEMORY_FLOOR_BYTES", {"copy-number": 3 * GIB})
    policy = MemoryCapPolicy.for_store(LabStore(tmp_path / "root"))
    assert policy.for_algorithm("copy-number") == memory_cap.MemoryCap(3 * GIB, "floor")
    assert policy.for_algorithm("read-summary").bytes == DEFAULT_MEMORY_CAP_BYTES
    for algorithm_id, floor in registry.MEMORY_FLOOR_BYTES.items():
        assert floor >= DEFAULT_MEMORY_CAP_BYTES, algorithm_id


# -- limit selection ------------------------------------------------------------------------------------
def test_limit_selection_by_platform(monkeypatch) -> None:
    full = SimpleNamespace(RLIMIT_AS=9, RLIMIT_DATA=2)
    data_only = SimpleNamespace(RLIMIT_DATA=2)
    assert select_memory_limit("Linux", resource_module=full) == "RLIMIT_AS"
    assert select_memory_limit("Linux", resource_module=data_only) == "RLIMIT_DATA"
    assert select_memory_limit("Linux", resource_module=SimpleNamespace()) is None
    assert select_memory_limit("Darwin", resource_module=full) is None
    monkeypatch.setattr(memory_cap.os, "uname", lambda: SimpleNamespace(sysname="Darwin"))
    assert select_memory_limit() is None
    monkeypatch.setattr(memory_cap.os, "uname", lambda: SimpleNamespace(sysname="Linux"))
    assert select_memory_limit() == "RLIMIT_AS"


def test_apply_memory_limit_without_a_limit_is_a_no_op() -> None:
    assert apply_memory_limit(GIB, None) is None


@pytest.mark.skipif(sys.platform == "darwin", reason="macOS refuses address-space limits")
def test_apply_memory_limit_lowers_soft_and_hard_in_a_child() -> None:
    script = (
        "import resource\nfrom traceback_lab.memory_cap import apply_memory_limit\n"
        f"assert apply_memory_limit({CAP_BYTES}, 'RLIMIT_AS') == 'RLIMIT_AS'\n"
        f"assert resource.getrlimit(resource.RLIMIT_AS) == ({CAP_BYTES}, {CAP_BYTES})\n"
        "assert apply_memory_limit(2 * 1024 ** 3, 'RLIMIT_AS') == 'RLIMIT_AS'  # cannot raise a hard limit\n"
        f"assert resource.getrlimit(resource.RLIMIT_AS) == ({CAP_BYTES}, {CAP_BYTES})\n"
    )
    result = run_pipeline([Stage("probe", [PY, "-c", script])], timeout_s=30, env=_pythonpath_env())
    assert result.ok, result.stages[0].stderr.scrubbed(Scrubber())


def _pythonpath_env() -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(entry for entry in sys.path if entry and os.path.isdir(entry))
    return env


def test_allocation_failure_detection() -> None:
    assert looks_like_allocation_failure(memory_cap.WORKER_MEMORY_EXIT, [])
    assert looks_like_allocation_failure(1, ["Traceback", "MemoryError"])
    assert looks_like_allocation_failure(1, ["numpy._core._exceptions._ArrayMemoryError: Unable to allocate 3 GiB"])
    assert looks_like_allocation_failure(1, ["[E::bgzf_read] Cannot allocate memory"])
    assert looks_like_allocation_failure(1, ["OpenBLAS error: Memory allocation still failed after 10 retries, giving up."])
    assert not looks_like_allocation_failure(1, ["RuntimeError: synthetic failure"])
    assert not looks_like_allocation_failure(-9, [])


# -- enforcement in the pipeline --------------------------------------------------------------------------
HOG = (
    "import time\n"
    "block = bytearray(256 << 20)\n"
    "for offset in range(0, len(block), 4096): block[offset] = 1\n"
    "time.sleep(20)\n"
    "print('survived')\n"
)


def test_rss_sampler_stops_a_worker_past_the_cap(monkeypatch) -> None:
    # Without the rlimit (macOS, or a platform without one) only the sampler stands.
    monkeypatch.setattr(pipeline, "select_memory_limit", lambda: None)
    result = run_pipeline([Stage("hog", [PY, "-c", HOG])], timeout_s=30, memory_cap_bytes=64 * MIB)
    assert result.memory_exceeded and not result.ok and not result.timed_out
    assert result.memory_limit is None and result.duration_s < 10
    assert result.sampled_tree_peak_rss_bytes is not None and result.sampled_tree_peak_rss_bytes > 64 * MIB
    assert result.stages[0].returncode in (-15, -9)
    assert result.first_line(Scrubber()) == "memory cap exceeded"


def test_the_cap_stops_a_hog_one_way_or_the_other() -> None:
    result = run_pipeline([Stage("hog", [PY, "-c", HOG])], timeout_s=30, memory_cap_bytes=64 * MIB)
    assert not result.ok and not result.timed_out and result.duration_s < 10
    tail = result.stages[0].stderr.scrubbed(Scrubber())
    assert result.memory_exceeded or looks_like_allocation_failure(result.stages[0].returncode, tail)
    assert (result.memory_limit is None) == (sys.platform == "darwin")


def test_a_worker_under_its_cap_is_unaffected() -> None:
    result = run_pipeline([Stage("ok", [PY, "-c", "print('done')"])], timeout_s=30, memory_cap_bytes=4 * GIB)
    assert result.ok and not result.memory_exceeded


def test_rlimit_is_set_before_exec_only_where_the_kernel_enforces_it(tmp_path: Path) -> None:
    probe = tmp_path / "limits.json"
    script = (
        "import json, resource, sys\n"
        "json.dump({'as': resource.getrlimit(resource.RLIMIT_AS), 'data': resource.getrlimit(resource.RLIMIT_DATA),"
        f" 'inf': resource.RLIM_INFINITY}}, open({str(probe)!r}, 'w'))\n"
    )
    result = run_pipeline([Stage("probe", [PY, "-c", script])], timeout_s=30, memory_cap_bytes=CAP_BYTES)
    assert result.ok
    seen = json.loads(probe.read_text())
    if sys.platform == "darwin":
        assert result.memory_limit is None
        # RLIM_INFINITY: macOS refuses lower values and does not enforce them.
        assert seen["as"][0] == seen["inf"] and seen["data"][0] == seen["inf"]
    else:
        assert result.memory_limit == "RLIMIT_AS"
        assert seen["as"] == [CAP_BYTES, CAP_BYTES]


@pytest.mark.skipif(sys.platform == "darwin", reason="macOS does not enforce address-space limits")
def test_rlimit_refuses_the_allocation_on_linux() -> None:
    script = "block = bytearray(768 << 20)\nprint('allocated')\n"
    result = run_pipeline([Stage("hog", [PY, "-c", script])], timeout_s=30, memory_cap_bytes=CAP_BYTES)
    assert not result.ok
    tail = result.stages[0].stderr.scrubbed(Scrubber())
    assert looks_like_allocation_failure(result.stages[0].returncode, tail)


def test_memory_cap_must_be_positive() -> None:
    with pytest.raises(ValueError):
        run_pipeline([Stage("ok", [PY, "-c", "pass"])], timeout_s=30, memory_cap_bytes=0)


# -- lab run end to end ---------------------------------------------------------------------------------------
def _single_threaded_blas(monkeypatch) -> None:
    """Keep the worker's Linux address space small: OpenBLAS reserves ~40 MB per CPU at import."""

    monkeypatch.setenv("OPENBLAS_NUM_THREADS", "1")
    monkeypatch.setenv("OMP_NUM_THREADS", "1")


def _never_cap_the_aligner(monkeypatch) -> list[dict]:
    calls: list[dict] = []
    real = ingest.run_pipeline

    def recording(*args, **kwargs):
        calls.append(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(ingest, "run_pipeline", recording)
    return calls


def test_capped_algorithm_fails_with_reason_memory_and_the_run_continues(
    lab_world, capsys, monkeypatch, shared_mmi,
) -> None:
    _registry(monkeypatch, "SUMMARY", "HOGS", "COUNTER")
    _single_threaded_blas(monkeypatch)
    monkeypatch.setenv("LAB_TEST_HOG_BYTES", str(HOG_BYTES))
    monkeypatch.setenv("LAB_TEST_HOG_HOLD_S", "3")
    aligner_calls = _never_cap_the_aligner(monkeypatch)
    root = lab_world["root"]
    base = ("run", str(lab_world["chunks"]), "--root", str(root), "--mmi", str(shared_mmi))

    code, out, _ = _lab(capsys, *base, "--memory-cap", CAP)
    assert code == 8
    assert aligner_calls and all("memory_cap_bytes" not in call for call in aligner_calls)
    assert "FAILED  copy-number: memory cap exceeded (512.0 MiB)" in out
    assert "MEMORY  copy-number: exceeded its cap of 512.0 MiB" in out
    assert "CODE       TBX-LAB-016" in out and "CODE       TBX-LAB-030" in out
    fix = next(line for line in out.splitlines() if line.startswith("FIX") and "--memory-cap" in line)
    assert "--memory-cap 1.0GiB" in fix and "--no-memory-cap" in fix

    code, payload = _json(capsys, *base, "--memory-cap", CAP)
    assert code == 8
    data = payload["data"]
    assert data["code"] == "TBX-LAB-016"
    assert data["memory_cap"]["override_bytes"] == CAP_BYTES and data["memory_cap"]["disabled"] is False
    [exceeded] = data["memory_cap"]["exceeded"]
    assert exceeded["algorithm_id"] == "copy-number" and exceeded["cap_bytes"] == CAP_BYTES
    assert exceeded["basis"] == "override" and exceeded["code"] == "TBX-LAB-030"
    assert exceeded["docs"].endswith("#tbx-lab-030") and "512.0 MiB" in exceeded["cause"]
    assert data["failed"] == [{"algorithm_id": "copy-number", "first_line": "memory cap exceeded (512.0 MiB)"}]
    steps = {step["algorithm_id"]: step for step in data["steps"]}
    assert steps["copy-number"]["status"] == "failed" and steps["copy-number"]["failure_reason"] == "memory"
    assert steps["fragment-size"]["status"] == "ok" and steps["fragment-size"]["failure_reason"] is None
    assert data["counts"] == {"ok": 2, "skipped": {}, "failed": 1}

    store = LabStore(root)
    events = store.run_log(data["run_id"]).events()
    start = next(event for event in events if event["event"] == "run_start")
    assert start["flags"]["memory_cap_bytes"] == CAP_BYTES and start["flags"]["no_memory_cap"] is False
    step_start = next(event for event in events if event["event"] == "step_start" and event["step"] == "copy-number")
    assert step_start["memory_cap_bytes"] == CAP_BYTES and step_start["memory_cap_basis"] == "override"
    assert step_start["memory_cap_observed_bytes"] is None
    step_end = next(event for event in events if event["event"] == "step_end" and event["step"] == "copy-number")
    assert step_end["status"] == "failed" and step_end["failure_reason"] == "memory"
    assert step_end["memory_cap_bytes"] == CAP_BYTES and step_end["timed_out"] is False
    assert steps["fragment-size"]["cached"]  # the first (human) run computed it under the same cap
    ok_end = next(event for run_id in store.run_ids() for event in store.run_log(run_id).events()
                  if event["event"] == "step_end" and event["step"] == "fragment-size")
    assert ok_end["status"] == "ok" and "failure_reason" not in ok_end
    ok_start = next(event for run_id in store.run_ids() for event in store.run_log(run_id).events()
                    if event["event"] == "step_start" and event["step"] == "fragment-size")
    assert ok_start["memory_cap_bytes"] == CAP_BYTES and ok_start["memory_cap_basis"] == "override"
    input_id = data["sample"]["input_id"]
    envelope = store.read_result(input_id, "copy-number", step_end["parameters_sha256"])
    assert envelope.status == "failed" and envelope.failure is not None
    assert envelope.failure.reason == "memory" and envelope.failure.first_line == "memory cap exceeded (512.0 MiB)"

    # The memory-killed step must not seed the history; the default stays 2 GiB.
    assert memory_history(store).get("copy-number", 0) < CAP_BYTES
    assert MemoryCapPolicy.for_store(store).for_algorithm("copy-number").basis == "default"

    # --no-memory-cap: the same algorithm completes and only it recomputes.
    code, payload = _json(capsys, *base, "--no-memory-cap")
    assert code == 0, payload
    data = payload["data"]
    assert data["memory_cap"] == {"override_bytes": None, "disabled": True, "exceeded": []}
    steps = {step["algorithm_id"]: step for step in data["steps"]}
    assert steps["copy-number"]["status"] == "ok" and not steps["copy-number"]["cached"]
    assert steps["fragment-size"]["cached"] and steps["read-summary"]["cached"]
    events = store.run_log(data["run_id"]).events()
    start = next(event for event in events if event["event"] == "run_start")
    assert start["flags"] == {**start["flags"], "memory_cap_bytes": None, "no_memory_cap": True}
    step_start = next(event for event in events if event["event"] == "step_start" and event["step"] == "copy-number")
    assert step_start["memory_cap_bytes"] is None and step_start["memory_cap_basis"] == "disabled"
    step_end = next(event for event in events if event["event"] == "step_end" and event["step"] == "copy-number")
    assert step_end["status"] == "ok" and step_end["peak_rss_bytes"] >= HOG_BYTES

    # Default cap on a later run: twice the recorded peak once that beats 2 GiB.
    assert memory_history(store)["copy-number"] >= HOG_BYTES
    store.run_log("r-20261106-0000-ffff").append(
        "step_end", step="copy-number@0.5", status="ok", peak_rss_bytes=3 * GIB,
    )
    monkeypatch.setenv("LAB_TEST_HOG_BYTES", str(16 * MIB))
    code, payload = _json(capsys, *base, "--force")
    assert code == 0, payload
    data = payload["data"]
    assert data["memory_cap"] == {"override_bytes": None, "disabled": False, "exceeded": []}
    events = store.run_log(data["run_id"]).events()
    starts = {event["step"]: event for event in events if event["event"] == "step_start"}
    assert starts["copy-number"]["memory_cap_bytes"] == 6 * GIB
    assert starts["copy-number"]["memory_cap_basis"] == "history"
    assert starts["copy-number"]["memory_cap_observed_bytes"] == 3 * GIB
    assert starts["read-summary"]["memory_cap_bytes"] == 2 * GIB
    assert starts["read-summary"]["memory_cap_basis"] == "default"


def test_runs_step_shows_the_cap_fields(lab_world, capsys, monkeypatch, shared_mmi) -> None:
    _registry(monkeypatch, "SUMMARY", "HOGS")
    _single_threaded_blas(monkeypatch)
    monkeypatch.setenv("LAB_TEST_HOG_BYTES", str(HOG_BYTES))
    monkeypatch.setenv("LAB_TEST_HOG_HOLD_S", "3")
    root = lab_world["root"]
    code, payload = _json(
        capsys, "run", str(lab_world["chunks"]), "--root", str(root), "--mmi", str(shared_mmi), "--memory-cap", CAP,
    )
    assert code == 8
    code, out, _ = _lab(capsys, "runs", payload["data"]["run_id"], "--step", "copy-number", "--root", str(root))
    assert code == 0
    assert f"memory_cap_bytes={CAP_BYTES}" in out and "memory_cap_basis=override" in out
    assert "failure_reason=memory" in out


def test_worker_exits_with_the_memory_code_on_memory_error(tmp_path: Path, monkeypatch, capsys) -> None:
    from traceback_lab import worker

    class Raises:
        id = "raises"

        def run(self, ctx, out):
            raise MemoryError

    monkeypatch.setattr(worker, "load_entry", lambda entry: Raises())
    monkeypatch.setattr(worker, "context_from_json", lambda data: data)
    request = tmp_path / "request.json"
    request.write_text(json.dumps({"entry": "x:Y", "context": {}, "out_dir": str(tmp_path / "out")}))
    previous = signal.getsignal(signal.SIGTERM)
    try:
        assert worker.main([str(request)]) == memory_cap.WORKER_MEMORY_EXIT
    finally:
        signal.signal(signal.SIGTERM, previous)
    assert "memory cap" in capsys.readouterr().err
    assert not (tmp_path / "out" / "output.json").exists()
    assert os.path.isdir(tmp_path / "out" / "artifacts")
