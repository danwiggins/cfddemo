"""The per-algorithm memory cap: derivation, flags, enforcement, run-log fields.

One algorithm must not be able to OOM the workstation.  The e2e cases run a
fake algorithm that holds more memory than its cap; the aligner is never
capped (it is not an algorithm).
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from traceback_lab import ingest, memory_cap, registry
from traceback_lab.memory_cap import (
    ABSOLUTE_CEILING_BYTES,
    DEFAULT_MEMORY_CAP_BYTES,
    GIB,
    MIB,
    MemoryCapError,
    MemoryCapPolicy,
    address_space_limit,
    apply_memory_limit,
    blas_threads,
    derived_ceiling,
    failure_reason,
    history_path,
    load_history,
    looks_like_allocation_failure,
    parse_memory_cap,
    pin_blas_threads,
    pipeline_options,
    rebuild_history,
    save_history,
    select_memory_limit,
    start_failure,
    update_history,
)
from traceback_lab.runner import StepResult
from traceback_lab.scrub import Scrubber
from traceback_lab.store import LabStore
from traceback_runner import cli as signed_cli
from traceback_runner.contained_process import Stage, run_contained_pipeline as run_pipeline

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
        ("256MiB", 256 * MIB),
        ("1.5GiB", 3 * GIB // 2),
        ("1048576KiB", GIB),
        ("3000000000", 3_000_000_000),
        (" 2 gib ", 2 * GIB),
    ],
)
def test_parse_memory_cap_accepts_bytes_and_binary_suffixes(text: str, expected: int) -> None:
    assert parse_memory_cap(text) == expected


@pytest.mark.parametrize("text", ["", "abc", "4GB", "2G", "-1GiB", "100", "255MiB", "2048KiB", "0MiB", "9999999TiB"])
def test_parse_memory_cap_rejects_other_spellings_and_sizes(text: str) -> None:
    with pytest.raises(ValueError):
        parse_memory_cap(text)


@pytest.mark.parametrize("value", ["2G", "4GB", "100", "128MiB", "lots"])
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


# -- history ---------------------------------------------------------------------------------------
def _store_with_logs(root: Path, runs: list[list[dict]]) -> LabStore:
    store = LabStore(root)
    for index, events in enumerate(runs):
        log = store.run_log(f"r-20261106-{index:04d}-abcd")
        for event in events:
            log.append(**event)
    return store


def _step_end(step: str, peak: int | None, tree: int | None = None, status: str = "ok", **extra) -> dict:
    return {"event": "step_end", "step": step, "status": status, "peak_rss_bytes": peak,
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
            [[_step_end("copy-number", 9 * GIB, status="failed", failure_reason="memory"),
              _step_end("copy-number", 8 * GIB, status="failed", timed_out=True),
              _step_end("copy-number", 7 * GIB, status="failed"),  # uncapped or crashed
              _step_end("copy-number", 6 * GIB, status="skipped"),
              {"event": "step_end", "step": "copy-number", "peak_rss_bytes": 5 * GIB},  # no status
              _step_end("copy-number", 1 * GIB)]],
            {"copy-number": 1 * GIB},
            id="only-ok-steps-count",
        ),
        pytest.param(
            [[_step_end("copy-number", None), _step_end("copy-number", "lots"), _step_end("copy-number", True),
              {"event": "step_start", "step": "copy-number", "peak_rss_bytes": 9 * GIB},
              {"event": "step_end", "step": 7, "status": "ok", "peak_rss_bytes": 9 * GIB}]],
            {},
            id="malformed-values-and-other-events-are-ignored",
        ),
    ],
)
def test_rebuild_history_is_the_highest_ok_peak_per_algorithm(tmp_path: Path, runs, expected) -> None:
    store = _store_with_logs(tmp_path / "root", runs)
    assert rebuild_history(store) == (expected, 0)


def test_unreadable_run_log_is_skipped_with_a_notice_never_fatal(tmp_path: Path) -> None:
    store = _store_with_logs(tmp_path / "root", [[_step_end("copy-number", 3 * GIB)]])
    (store.runs_dir / "r-20261106-0001-ffff.jsonl").mkdir()  # listed, unreadable
    assert rebuild_history(store) == ({"copy-number": 3 * GIB}, 1)
    history, notices = load_history(store)
    assert history == {"copy-number": 3 * GIB}
    assert any("1 unreadable run log(s) skipped" in notice for notice in notices)
    assert any("rebuilt from 2 run log(s)" in notice for notice in notices)
    assert history_path(store).is_file()
    assert load_history(store) == ({"copy-number": 3 * GIB}, [])  # stored now; no rescan


def test_history_file_is_rebuilt_once_then_maintained(tmp_path: Path) -> None:
    store = _store_with_logs(tmp_path / "root", [[_step_end("copy-number", 3 * GIB)]])
    assert not history_path(store).exists()
    assert load_history(store)[0] == {"copy-number": 3 * GIB}
    stored = json.loads(history_path(store).read_text())
    assert stored == {"schema": "traceback.lab.memory-history.v1", "max_ok_peak_bytes": {"copy-number": 3 * GIB}}
    # Logs added later do not change a stored history; run ends maintain it.
    store.run_log("r-20261106-0009-abcd").append(**_step_end("copy-number", 9 * GIB))
    assert load_history(store) == ({"copy-number": 3 * GIB}, [])

    def result(algorithm_id: str, status: str, peak: int | None, tree: int | None = None, cached: bool = False):
        return StepResult(algorithm_id, status, False, "x" * 64, peak_rss_bytes=peak,
                          peak_tree_rss_bytes=tree, cached=cached)

    history = update_history(store, [
        result("copy-number", "ok", 4 * GIB),
        result("copy-number", "ok", 5 * GIB, cached=True),  # a cached result measured nothing
        result("read-summary", "ok", 100 * MIB, 150 * MIB),
        result("fragment-size", "failed", 8 * GIB),
        result("mtdna", "ok", None),
    ])
    assert history == {"copy-number": 4 * GIB, "read-summary": 150 * MIB}
    assert load_history(store) == (history, [])


@pytest.mark.parametrize(
    "content",
    [b"", b"not json", b"[]", b'{"schema": "other", "max_ok_peak_bytes": {}}',
     b'{"schema": "traceback.lab.memory-history.v1", "max_ok_peak_bytes": []}'],
)
def test_damaged_history_file_is_rebuilt_from_the_logs(tmp_path: Path, content: bytes) -> None:
    store = _store_with_logs(tmp_path / "root", [[_step_end("copy-number", 3 * GIB)]])
    history_path(store).write_bytes(content)
    assert load_history(store)[0] == {"copy-number": 3 * GIB}


def test_history_file_values_are_validated(tmp_path: Path) -> None:
    store = LabStore(tmp_path / "root")
    store.lab.mkdir(parents=True)
    history_path(store).write_text(json.dumps({
        "schema": "traceback.lab.memory-history.v1",
        "max_ok_peak_bytes": {"copy-number": 3 * GIB, "bad": "x", "zero": 0, "flag": True, "neg": -5},
    }))
    assert load_history(store) == ({"copy-number": 3 * GIB}, [])


# -- derivation ---------------------------------------------------------------------------------------
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
        pytest.param({"copy-number": 40 * GIB}, {}, "copy-number", 64 * GIB, "ceiling", 40 * GIB,
                     id="absolute-ceiling-bounds-a-runaway-history"),
        pytest.param({}, {"copy-number": 100 * GIB}, "copy-number", 64 * GIB, "ceiling", None,
                     id="absolute-ceiling-bounds-a-floor"),
    ],
)
def test_default_cap_is_max_of_floor_and_twice_history_under_the_ceiling(
    history, floors, algorithm, cap, basis, observed,
) -> None:
    policy = MemoryCapPolicy(history=history, floors=floors)
    chosen = policy.for_algorithm(algorithm)
    assert (chosen.bytes, chosen.basis, chosen.observed_bytes) == (cap, basis, observed)
    assert chosen.log_fields() == {
        "memory_cap_bytes": cap, "memory_cap_basis": basis, "memory_cap_observed_bytes": observed,
    }


@pytest.mark.parametrize(
    ("ram", "ceiling"),
    [
        (None, ABSOLUTE_CEILING_BYTES),
        (0, ABSOLUTE_CEILING_BYTES),
        (8 * GIB, int(8 * GIB * 0.8)),
        (16 * GIB, int(16 * GIB * 0.8)),
        (80 * GIB, 64 * GIB),
        (512 * GIB, 64 * GIB),
        (100 * MIB, 256 * MIB),
    ],
)
def test_derived_ceiling_is_80_percent_of_ram_never_above_64gib(ram, ceiling) -> None:
    assert derived_ceiling(ram) == ceiling


def test_physical_ram_bounds_the_derived_cap_but_not_an_override(tmp_path: Path) -> None:
    store = _store_with_logs(tmp_path / "root", [[_step_end("copy-number", 4 * GIB)]])
    bounded = MemoryCapPolicy.for_store(store, ram_bytes=8 * GIB)
    assert bounded.ceiling_bytes == int(8 * GIB * 0.8)
    assert bounded.for_algorithm("copy-number") == memory_cap.MemoryCap(int(8 * GIB * 0.8), "ceiling", 4 * GIB)
    assert bounded.for_algorithm("read-summary") == memory_cap.MemoryCap(2 * GIB, "default")
    override = MemoryCapPolicy.for_store(store, ram_bytes=8 * GIB, override_bytes=12 * GIB)
    assert override.for_algorithm("copy-number") == memory_cap.MemoryCap(12 * GIB, "override")
    unbounded = MemoryCapPolicy.for_store(store, ram_bytes=64 * GIB)
    assert unbounded.for_algorithm("copy-number") == memory_cap.MemoryCap(8 * GIB, "history", 4 * GIB)


def test_override_and_disable_win_over_history_and_floors(tmp_path: Path) -> None:
    store = _store_with_logs(tmp_path / "root", [[_step_end("copy-number", 3 * GIB)]])
    derived = MemoryCapPolicy.for_store(store, floors={"copy-number": 5 * GIB}, ram_bytes=64 * GIB)
    assert derived.for_algorithm("copy-number").bytes == 6 * GIB
    assert any("rebuilt" in notice for notice in derived.notices)
    override = MemoryCapPolicy.for_store(store, override_bytes=GIB, floors={"copy-number": 5 * GIB})
    assert override.for_algorithm("copy-number") == memory_cap.MemoryCap(GIB, "override")
    assert override.history == {} and override.notices == ()  # not read: the override applies to every algorithm
    assert override.flags() == {"memory_cap_bytes": GIB, "no_memory_cap": False}
    disabled = MemoryCapPolicy.for_store(store, disabled=True)
    assert disabled.for_algorithm("copy-number") == memory_cap.MemoryCap(None, "disabled")
    assert disabled.flags() == {"memory_cap_bytes": None, "no_memory_cap": True}


def test_registry_floor_table_feeds_the_default_policy(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(registry, "MEMORY_FLOOR_BYTES", {"copy-number": 3 * GIB})
    policy = MemoryCapPolicy.for_store(LabStore(tmp_path / "root"), ram_bytes=64 * GIB)
    assert policy.for_algorithm("copy-number") == memory_cap.MemoryCap(3 * GIB, "floor")
    assert policy.for_algorithm("read-summary").bytes == DEFAULT_MEMORY_CAP_BYTES
    for algorithm_id, floor in registry.MEMORY_FLOOR_BYTES.items():
        assert floor >= DEFAULT_MEMORY_CAP_BYTES, algorithm_id


# -- BLAS threads and the Linux address-space headroom -------------------------------------------------
def test_blas_threads_default_to_min_4_cpus_unless_the_algorithm_declares() -> None:
    assert blas_threads(None, cpu_count=128) == 4
    assert blas_threads(None, cpu_count=2) == 2
    assert blas_threads(None, cpu_count=0) == 1
    assert blas_threads(SimpleNamespace(blas_threads=8), cpu_count=128) == 8
    assert blas_threads(SimpleNamespace(blas_threads=0), cpu_count=128) == 4
    assert blas_threads(SimpleNamespace(blas_threads=True), cpu_count=128) == 4
    assert blas_threads(SimpleNamespace(blas_threads="6"), cpu_count=128) == 4
    assert 1 <= blas_threads() <= 4
    for algorithm in registry.load_registry():
        assert getattr(algorithm, "blas_threads", None) is None, algorithm.id  # none rely on BLAS threading


def test_pin_blas_threads_keeps_an_operator_value_and_reports_the_effective_count() -> None:
    env: dict[str, str] = {}
    assert pin_blas_threads(env, 4) == 4
    assert env == {"OPENBLAS_NUM_THREADS": "4", "OMP_NUM_THREADS": "4", "MKL_NUM_THREADS": "4"}
    env = {"OPENBLAS_NUM_THREADS": "16", "OMP_NUM_THREADS": "0", "MKL_NUM_THREADS": "x"}
    assert pin_blas_threads(env, 4) == 16
    assert env == {"OPENBLAS_NUM_THREADS": "16", "OMP_NUM_THREADS": "4", "MKL_NUM_THREADS": "4"}


def test_address_space_limit_adds_import_headroom() -> None:
    assert address_space_limit(2 * GIB, 4) == 2 * GIB + 512 * MIB + 4 * 64 * MIB
    assert address_space_limit(2 * GIB, 0) == 2 * GIB + 512 * MIB + 64 * MIB
    assert address_space_limit(CAP_BYTES, 1) == CAP_BYTES + 576 * MIB


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


def _pythonpath_env() -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(entry for entry in sys.path if entry and os.path.isdir(entry))
    return env


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


# -- failure attribution --------------------------------------------------------------------------------
def test_allocation_failure_detection() -> None:
    assert looks_like_allocation_failure(memory_cap.WORKER_MEMORY_EXIT, [])
    assert looks_like_allocation_failure(1, ["Traceback", "MemoryError"])
    assert looks_like_allocation_failure(1, ["numpy._core._exceptions._ArrayMemoryError: Unable to allocate 3 GiB"])
    assert looks_like_allocation_failure(1, ["[E::bgzf_read] Cannot allocate memory"])
    assert looks_like_allocation_failure(1, ["OpenBLAS error: Memory allocation still failed after 10 retries, giving up."])
    assert not looks_like_allocation_failure(1, ["RuntimeError: synthetic failure"])
    assert not looks_like_allocation_failure(-9, [])


@pytest.mark.parametrize(
    ("capped", "exceeded", "timed_out", "exit_code", "tail", "expected"),
    [
        pytest.param(True, True, False, -15, [], "memory", id="our-kill"),
        pytest.param(True, False, False, 77, [], "memory", id="worker-memory-exit"),
        pytest.param(True, False, False, 1, ["MemoryError"], "memory", id="nonzero-and-allocation-text"),
        pytest.param(True, False, False, 0, ["MemoryError"], None, id="exit-0-is-never-memory"),
        pytest.param(True, True, False, 0, [], None, id="exit-0-even-after-an-over-cap-sample"),
        pytest.param(True, False, True, -9, ["MemoryError"], None, id="timeout-is-never-memory"),
        pytest.param(True, True, True, -9, [], None, id="timeout-wins-over-the-sampler"),
        pytest.param(False, False, False, 77, ["MemoryError"], None, id="uncapped-step"),
        pytest.param(True, False, False, None, ["MemoryError"], None, id="no-exit-code"),
        pytest.param(True, False, False, 1, ["RuntimeError"], None, id="ordinary-crash"),
        pytest.param(True, False, False, -9, [], None, id="foreign-sigkill"),
    ],
)
def test_failure_reason_attribution(capped, exceeded, timed_out, exit_code, tail, expected) -> None:
    assert failure_reason(capped=capped, memory_exceeded=exceeded, timed_out=timed_out,
                          exit_code=exit_code, stderr_tail=tail) == expected


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
    monkeypatch.setattr(memory_cap, "select_memory_limit", lambda: None)
    options = pipeline_options(64 * MIB)
    assert set(options) == {"on_tree_rss_sample"}
    result = run_pipeline([Stage("hog", [PY, "-c", HOG])], timeout_s=30, **options)
    assert result.aborted and not result.ok and not result.timed_out
    assert result.duration_s < 10
    assert result.sampled_tree_peak_rss_bytes is not None and result.sampled_tree_peak_rss_bytes > 64 * MIB
    assert result.stages[0].returncode in (-15, -9)


def test_the_cap_stops_a_hog_one_way_or_the_other() -> None:
    options = pipeline_options(64 * MIB)
    assert ("preexec_fn" in options) == (sys.platform != "darwin")
    result = run_pipeline([Stage("hog", [PY, "-c", HOG])], timeout_s=30, **options)
    assert not result.ok and not result.timed_out and result.duration_s < 10
    tail = result.stages[0].stderr.scrubbed(Scrubber())
    assert result.aborted or looks_like_allocation_failure(result.stages[0].returncode, tail)


def test_a_worker_under_its_cap_is_unaffected() -> None:
    result = run_pipeline([Stage("ok", [PY, "-c", "print('done')"])], timeout_s=30, **pipeline_options(4 * GIB))
    assert result.ok and not result.aborted
    assert pipeline_options(None) == {}


def _exited_unreaped(pid: int) -> bool:
    """True once ``pid`` has exited but is still an unreaped zombie of this process."""

    if hasattr(os, "waitid"):  # Linux: peek at the exit without reaping it
        return os.waitid(os.P_PID, pid, os.WEXITED | os.WNOWAIT | os.WNOHANG) is not None
    # macOS has no waitid; ps reports a zombie as state Z until it is reaped.
    state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout
    return state.strip().startswith("Z")


def _late_sample_run(monkeypatch, code: str):
    """Run one worker whose over-cap sample arrives after it exited, before it is reaped.

    Non-blocking reaps are held back until the over-cap sample has asked for a
    stop, and the sampler only reports once the worker is an unreaped zombie,
    so the stop decision always sees a group that has exited but whose exit
    the loop has not yet collected. Only a re-reap before that decision can
    tell the exit apart from a live group.
    """

    import traceback_runner.contained_process as contained

    monkeypatch.setattr(contained, "RSS_SAMPLE_S", 0.0)
    stop_requested: list[int] = []
    real_reap = contained._reap

    def reap(process, outcome, *, block: bool) -> bool:
        if not block and not stop_requested:
            return False
        return real_reap(process, outcome, block=block)

    monkeypatch.setattr(contained, "_reap", reap)

    def sampler(leader_pid: int) -> int | None:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if _exited_unreaped(leader_pid):
                return 100 * GIB
            time.sleep(0.005)
        return None

    options = pipeline_options(GIB)
    over_cap = options["on_tree_rss_sample"]

    def on_sample(rss: int) -> bool:
        stop = over_cap(rss)
        if stop:
            stop_requested.append(rss)
        return stop

    options["on_tree_rss_sample"] = on_sample
    result = run_pipeline([Stage("quick", [PY, "-c", code])], timeout_s=30, tree_rss_sampler=sampler, **options)
    return result, stop_requested


def test_an_over_cap_sample_after_the_worker_exited_is_not_a_memory_kill(tmp_path: Path, monkeypatch) -> None:
    """A late sample must never turn a finished group's own exit into our kill."""

    result, stopped = _late_sample_run(monkeypatch, "print('done')")
    assert stopped == [100 * GIB]
    assert result.ok and not result.aborted and result.stages[0].returncode == 0
    failing, stopped = _late_sample_run(monkeypatch, "raise SystemExit(3)")
    assert stopped == [100 * GIB]
    assert not failing.ok and not failing.aborted and failing.stages[0].returncode == 3
    monkeypatch.undo()
    # A group that is still alive when the over-cap sample lands is ours to kill.
    alive = run_pipeline([Stage("sleep", [PY, "-c", "import time; time.sleep(20)"])], timeout_s=30,
                         tree_rss_sampler=lambda pid: 100 * GIB, **pipeline_options(GIB))
    assert alive.aborted and alive.stages[0].returncode in (-15, -9) and alive.duration_s < 10


def test_rlimit_is_set_before_exec_only_where_the_kernel_enforces_it(tmp_path: Path) -> None:
    probe = tmp_path / "limits.json"
    script = (
        "import json, resource, sys\n"
        "json.dump({'as': resource.getrlimit(resource.RLIMIT_AS), 'data': resource.getrlimit(resource.RLIMIT_DATA),"
        f" 'inf': resource.RLIM_INFINITY}}, open({str(probe)!r}, 'w'))\n"
    )
    rlimit = CAP_BYTES + 576 * MIB
    options = pipeline_options(CAP_BYTES, rlimit)
    result = run_pipeline([Stage("probe", [PY, "-c", script])], timeout_s=30, **options)
    assert result.ok
    seen = json.loads(probe.read_text())
    if sys.platform == "darwin":
        assert "preexec_fn" not in options
        # RLIM_INFINITY: macOS refuses lower values and does not enforce them.
        assert seen["as"][0] == seen["inf"] and seen["data"][0] == seen["inf"]
    else:
        assert "preexec_fn" in options
        assert seen["as"] == [rlimit, rlimit]  # the headroom value, not the cap


@pytest.mark.skipif(sys.platform == "darwin", reason="macOS does not enforce address-space limits")
def test_rlimit_refuses_the_allocation_on_linux() -> None:
    script = "block = bytearray(1536 << 20)\nprint('allocated')\n"
    result = run_pipeline([Stage("hog", [PY, "-c", script])], timeout_s=30,
                          **pipeline_options(CAP_BYTES, CAP_BYTES + 576 * MIB))
    assert not result.ok
    tail = result.stages[0].stderr.scrubbed(Scrubber())
    assert looks_like_allocation_failure(result.stages[0].returncode, tail)


def test_memory_cap_arguments_are_validated() -> None:
    with pytest.raises(ValueError):
        pipeline_options(0)
    with pytest.raises(ValueError):
        pipeline_options(None, GIB)
    with pytest.raises(ValueError):
        pipeline_options(GIB, GIB - 1)


def _failing_preexec(monkeypatch) -> None:
    monkeypatch.setattr(memory_cap, "select_memory_limit", lambda: "RLIMIT_AS")

    def refuse(cap_bytes: int, limit_name: str | None) -> str | None:
        raise RuntimeError("synthetic preexec failure")

    monkeypatch.setattr(memory_cap, "apply_memory_limit", refuse)


def test_a_child_that_cannot_start_under_the_cap_is_a_memory_cap_error(monkeypatch) -> None:
    _failing_preexec(monkeypatch)
    options = pipeline_options(GIB)
    assert "preexec_fn" in options
    with pytest.raises((OSError, subprocess.SubprocessError)) as raised:
        run_pipeline([Stage("ok", [PY, "-c", "pass"])], timeout_s=30, **options)
    failure = start_failure(raised.value, options)
    assert isinstance(failure, MemoryCapError) and "could not start under the memory cap" in str(failure)
    assert start_failure(raised.value, {}) is None  # no cap: the start failure is the caller's
    assert start_failure(RuntimeError("x"), options) is None
    # Without a cap there is no preexec, so nothing changes.
    assert run_pipeline([Stage("ok", [PY, "-c", "pass"])], timeout_s=30).ok


# -- lab run end to end ---------------------------------------------------------------------------------------
def _never_cap_the_aligner(monkeypatch) -> list[dict]:
    calls: list[dict] = []
    real = ingest.run_pipeline

    def recording(*args, **kwargs):
        calls.append(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(ingest, "run_pipeline", recording)
    return calls


def _events(store: LabStore, run_id: str, event: str, step: str) -> dict:
    return next(e for e in store.run_log(run_id).events() if e["event"] == event and e["step"] == step)


def test_capped_algorithm_fails_with_reason_memory_and_the_run_continues(
    lab_world, capsys, monkeypatch, shared_mmi,
) -> None:
    _registry(monkeypatch, "SUMMARY", "HOGS", "COUNTER")
    monkeypatch.setenv("LAB_TEST_HOG_BYTES", str(HOG_BYTES))
    monkeypatch.setenv("LAB_TEST_HOG_HOLD_S", "3")
    for name in memory_cap.BLAS_THREAD_VARIABLES:
        monkeypatch.delenv(name, raising=False)  # the runner pins them itself
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
    assert fix.index("--memory-cap 1.0GiB") < fix.index("--no-memory-cap") and "last resort" in fix

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
    assert steps["fragment-size"]["cached"]  # the first (human) run computed it under the same cap
    assert data["counts"] == {"ok": 2, "skipped": {}, "failed": 1}

    store = LabStore(root)
    run_id = data["run_id"]
    start = next(event for event in store.run_log(run_id).events() if event["event"] == "run_start")
    assert start["flags"]["memory_cap_bytes"] == CAP_BYTES and start["flags"]["no_memory_cap"] is False
    step_start = _events(store, run_id, "step_start", "copy-number")
    assert step_start["memory_cap_bytes"] == CAP_BYTES and step_start["memory_cap_basis"] == "override"
    assert step_start["memory_cap_observed_bytes"] is None
    assert 1 <= step_start["blas_threads"] <= 4
    assert step_start["memory_rlimit_bytes"] == address_space_limit(CAP_BYTES, step_start["blas_threads"])
    step_end = _events(store, run_id, "step_end", "copy-number")
    assert step_end["status"] == "failed" and step_end["failure_reason"] == "memory"
    assert step_end["memory_cap_bytes"] == CAP_BYTES and step_end["timed_out"] is False
    ok_end = next(event for other in store.run_ids() for event in store.run_log(other).events()
                  if event["event"] == "step_end" and event["step"] == "fragment-size")
    assert ok_end["status"] == "ok" and "failure_reason" not in ok_end
    ok_start = next(event for other in store.run_ids() for event in store.run_log(other).events()
                    if event["event"] == "step_start" and event["step"] == "fragment-size")
    assert ok_start["memory_cap_bytes"] == CAP_BYTES and ok_start["memory_cap_basis"] == "override"
    input_id = data["sample"]["input_id"]
    envelope = store.read_result(input_id, "copy-number", step_end["parameters_sha256"])
    assert envelope.status == "failed" and envelope.failure is not None
    assert envelope.failure.reason == "memory" and envelope.failure.first_line == "memory cap exceeded (512.0 MiB)"

    # The memory-killed step must not seed the history; the default stays 2 GiB.
    history, _ = load_history(store)
    assert "copy-number" not in history and history["read-summary"] > 0 and history["fragment-size"] > 0
    assert MemoryCapPolicy.for_store(store, ram_bytes=64 * GIB).for_algorithm("copy-number").basis == "default"

    # --no-memory-cap: the same algorithm completes and only it recomputes.
    code, payload = _json(capsys, *base, "--no-memory-cap")
    assert code == 0, payload
    data = payload["data"]
    assert data["memory_cap"] == {"override_bytes": None, "disabled": True, "exceeded": []}
    steps = {step["algorithm_id"]: step for step in data["steps"]}
    assert steps["copy-number"]["status"] == "ok" and not steps["copy-number"]["cached"]
    assert steps["fragment-size"]["cached"] and steps["read-summary"]["cached"]
    run_id = data["run_id"]
    start = next(event for event in store.run_log(run_id).events() if event["event"] == "run_start")
    assert start["flags"] == {**start["flags"], "memory_cap_bytes": None, "no_memory_cap": True}
    step_start = _events(store, run_id, "step_start", "copy-number")
    assert step_start["memory_cap_bytes"] is None and step_start["memory_cap_basis"] == "disabled"
    assert "memory_rlimit_bytes" not in step_start and "blas_threads" not in step_start
    step_end = _events(store, run_id, "step_end", "copy-number")
    assert step_end["status"] == "ok" and step_end["peak_rss_bytes"] >= HOG_BYTES
    # An ok uncapped step is a real observation: it enters the maintained history.
    assert load_history(store)[0]["copy-number"] >= HOG_BYTES

    # Default cap on a later run: twice the recorded ok peak once that beats 2 GiB.
    save_history(store, {"copy-number": 3 * GIB // 2})
    monkeypatch.setenv("LAB_TEST_HOG_BYTES", str(16 * MIB))
    code, payload = _json(capsys, *base, "--force")
    assert code == 0, payload
    data = payload["data"]
    assert data["memory_cap"] == {"override_bytes": None, "disabled": False, "exceeded": []}
    run_id = data["run_id"]
    starts = {e["step"]: e for e in store.run_log(run_id).events() if e["event"] == "step_start"}
    assert starts["copy-number"]["memory_cap_bytes"] == 3 * GIB
    assert starts["copy-number"]["memory_cap_basis"] == "history"
    assert starts["copy-number"]["memory_cap_observed_bytes"] == 3 * GIB // 2
    assert starts["read-summary"]["memory_cap_bytes"] == 2 * GIB
    assert starts["read-summary"]["memory_cap_basis"] == "default"
    assert starts["read-summary"]["memory_rlimit_bytes"] == address_space_limit(2 * GIB, starts["read-summary"]["blas_threads"])
    # The history file was maintained at run end, not rebuilt from the logs.
    assert load_history(store)[0]["copy-number"] == 3 * GIB // 2


def test_numpy_worker_starts_under_the_default_cap_on_a_many_core_host(
    lab_world, capsys, monkeypatch, shared_mmi,
) -> None:
    _registry(monkeypatch, "SUMMARY", "USES_NUMPY")
    for name in memory_cap.BLAS_THREAD_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(memory_cap.os, "cpu_count", lambda: 128)
    root = lab_world["root"]
    code, payload = _json(capsys, "run", str(lab_world["chunks"]), "--root", str(root), "--mmi", str(shared_mmi))
    assert code == 0, payload
    steps = {step["algorithm_id"]: step for step in payload["data"]["steps"]}
    assert steps["bam-qc"]["status"] == "ok"
    store = LabStore(root)
    start = _events(store, payload["data"]["run_id"], "step_start", "bam-qc")
    assert start["memory_cap_bytes"] == 2 * GIB and start["blas_threads"] == 4
    assert start["memory_rlimit_bytes"] == 2 * GIB + 512 * MIB + 4 * 64 * MIB
    envelope = store.read_result(payload["data"]["sample"]["input_id"], "bam-qc", start["parameters_sha256"])
    values = {metric.metric_id: metric.value for metric in envelope.metrics}
    assert values == {"ones": 1000.0, "blas_threads": 4}


def test_operator_blas_thread_setting_is_kept_and_widens_the_headroom(
    lab_world, capsys, monkeypatch, shared_mmi,
) -> None:
    _registry(monkeypatch, "SUMMARY", "USES_NUMPY")
    monkeypatch.setenv("OPENBLAS_NUM_THREADS", "2")
    monkeypatch.setenv("OMP_NUM_THREADS", "6")
    monkeypatch.delenv("MKL_NUM_THREADS", raising=False)
    root = lab_world["root"]
    code, payload = _json(capsys, "run", str(lab_world["chunks"]), "--root", str(root), "--mmi", str(shared_mmi))
    assert code == 0, payload
    store = LabStore(root)
    start = _events(store, payload["data"]["run_id"], "step_start", "bam-qc")
    assert start["blas_threads"] == 6
    assert start["memory_rlimit_bytes"] == 2 * GIB + 512 * MIB + 6 * 64 * MIB
    envelope = store.read_result(payload["data"]["sample"]["input_id"], "bam-qc", start["parameters_sha256"])
    assert {m.metric_id: m.value for m in envelope.metrics}["blas_threads"] == 2


def test_a_child_that_cannot_start_under_the_cap_is_a_memory_failure_not_a_crash(
    lab_world, capsys, monkeypatch, shared_mmi,
) -> None:
    _registry(monkeypatch, "SUMMARY", "COUNTER")
    _failing_preexec(monkeypatch)
    root = lab_world["root"]
    code, payload = _json(capsys, "run", str(lab_world["chunks"]), "--root", str(root), "--mmi", str(shared_mmi))
    assert code == 8, payload
    data = payload["data"]
    assert data["counts"] == {"ok": 0, "skipped": {"upstream": 1}, "failed": 1}
    [failed] = data["failed"]
    assert failed["algorithm_id"] == "read-summary"
    assert failed["first_line"].startswith("memory cap could not be applied (2.0 GiB): worker could not start")
    assert [row["algorithm_id"] for row in data["memory_cap"]["exceeded"]] == ["read-summary"]
    assert data["memory_cap"]["exceeded"][0]["code"] == "TBX-LAB-030"
    store = LabStore(root)
    events = store.run_log(data["run_id"]).events()
    algorithm_events = [(e["event"], e.get("step")) for e in events
                        if e["event"] in {"step_start", "step_end", "run_end"} and e.get("step") not in {"ingest", "align"}]
    assert algorithm_events == [
        ("step_start", "read-summary"), ("step_end", "read-summary"), ("step_end", "fragment-size"), ("run_end", None),
    ]  # no dangling step_start; fragment-size is a skipped step_end
    end = _events(store, data["run_id"], "step_end", "read-summary")
    assert end["status"] == "failed" and end["failure_reason"] == "memory" and end["memory_cap_bytes"] == 2 * GIB
    assert not list((store.lab / "tmp").rglob("*")) if (store.lab / "tmp").exists() else True


def test_runs_step_shows_the_cap_fields(lab_world, capsys, monkeypatch, shared_mmi) -> None:
    _registry(monkeypatch, "SUMMARY", "HOGS")
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
    assert "memory_rlimit_bytes=" in out and "blas_threads=" in out
    assert "failure_reason=memory" in out


def test_history_rebuild_notice_goes_to_the_progress_stream(lab_world, capsys, monkeypatch, shared_mmi) -> None:
    _registry(monkeypatch, "SUMMARY")
    root = lab_world["root"]
    argv = ("run", str(lab_world["chunks"]), "--root", str(root), "--mmi", str(shared_mmi))
    code, out, _ = _lab(capsys, *argv)
    assert code == 0 and "NOTE  memory history rebuilt from 1 run log(s)" in out  # this run's own log
    history_path(LabStore(root)).unlink()
    (LabStore(root).runs_dir / "r-20261106-0001-ffff.jsonl").mkdir()
    code, out, err = _lab(capsys, *argv, "--json")
    assert code == 0
    assert json.loads(out)["status"] == "ok"  # stdout stays one object
    assert "NOTE  memory history: 1 unreadable run log(s) skipped" in err


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
