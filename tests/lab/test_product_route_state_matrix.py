"""P12 route/state matrix: 11 recorded states x 9 sample lenses.

Each parametrized case uses one Chrome process.  The per-cell observations are
attached to pytest's result and asserted before the case can pass.
"""

from __future__ import annotations

import copy
import json
import time
from pathlib import Path

import pytest

from tests.lab.test_acceptance_synthetic_routes import ID12, SAMPLE_ROUTES, _sample, _server
from tests.lab.test_completed_landing import _run, _source
from tests.lab.test_lab_server import _chrome, _dom_cdp
from tests.lab.test_landing import needs_chrome
from traceback_lab.completed_runs import record_completion
from traceback_lab.registry import InputContext
from traceback_lab.algorithms.short_read_fragment_size import SHORT_READ_FRAGMENT_SIZE
from traceback_lab.store import LabStore
from traceback_lab.web.api import LabApi
from traceback_lab.web.server import RunningLabServer


STATES = (
    "ready", "empty_root", "running_prior_input", "partial_results",
    "failed_algorithm", "missing_reference", "platform_skip",
    "disconnected", "expired_session", "missing_selected_input",
    "unreadable_completion_history",
)


def _algorithm(sample: dict, algorithm_id: str) -> dict:
    return next(row for row in sample["algorithms"] if row["algorithm_id"] == algorithm_id)


def _mark(sample: dict, algorithm_id: str, status: str, *, reason: str | None = None) -> None:
    row = _algorithm(sample, algorithm_id)
    row.update(status=status, skip_reason=reason, skip_message="recorded fixture reason",
               failure={"first_line": "synthetic failure", "stderr_tail": ["synthetic failure"]}
               if status == "failed" else None, metrics=[])
    catalog = next(row for row in sample["catalog"] if row["algorithm_id"] == algorithm_id)
    catalog.update(status=status, skip_reason=reason)


def _broken_store(tmp_path: Path, *, history: bool) -> tuple[LabStore, str]:
    store = LabStore(tmp_path / "root")
    store.ensure()
    source, selected, newer = "a" * 64, "8" * 64, "9" * 64
    run_id = "r-20261017-1000-abcd"
    _source(store, source, (selected, "2026-10-17T10:00:00Z"),
            (newer, "2026-10-18T10:00:00Z"))
    _run(store, run_id, source, selected, "2026-10-17T10:00:00Z")
    record_completion(store, run_id, source, selected)
    if history:
        (store.runs_dir / f"{run_id}.jsonl").unlink()
        assert LabApi(store).sample(source[:12])["state"] == "completion_history_unavailable"
    else:
        (store.inputs_dir / selected / "manifest.json").unlink()
        assert LabApi(store).sample(source[:12])["state"] == "completed_input_unavailable"
    return store, source[:12]


def _snapshot(evaluate) -> dict:
    return evaluate("""(() => {
      if (!window.TracebackLab) return {};
      const s = window.TracebackLab.state;
      const text = (id) => document.getElementById(id)?.textContent || '';
      const main = document.getElementById('main');
      return {
        hash: location.hash, rendered: s.renderedHash, route: s.route.lens || s.route.name,
        id12: s.sample?.id12 || null, input: s.sample?.full_input_id || null,
        found: s.sample?.found === true, sampleState: s.sample?.state || null,
        heading: main?.querySelector('h1')?.textContent || '', main: main?.textContent || '',
        context: text('sample-context'), result: text('result-state'),
        freshness: text('freshness-state'), run: text('run-state'),
        recovery: text('recovery-action'), status: text('status'),
        navCurrent: document.querySelector('nav a[aria-current="page"]')?.getAttribute('href') || null,
        stale: document.getElementById('freshness-state')?.dataset.stale || null,
        lost: s.connectionLost, missed: s.missed, expired: s.sessionExpired,
        activeInput: s.lastStatus?.active?.input_id || null,
        unreadable: s.sample?.unreadable_results || [],
        excluded: s.sample?.excluded_chunks || [],
        switchDisabled: document.getElementById('sample-switcher')?.disabled || false,
        serverControls: [...document.querySelectorAll('[data-requires-server]')]
          .filter(el => 'disabled' in el).map(el => el.disabled),
        obsoleteDone: window.__p12ObsoleteDone || false,
        obsoleteKind: window.__p12ObsoleteKind || null,
        algorithm: s.sample?.algorithms?.map(a => ({id:a.algorithm_id,status:a.status,
          reason:a.skip_reason,values:a.metrics?.map(m => m.value) || []})) || [],
        referenceMetric: s.sample?.algorithms?.find(a => a.algorithm_id === 'fragment-size')
          ?.metrics?.[0] || null,
      };
    })()""")


def _until(evaluate, predicate, *, seconds: float = 8) -> dict:
    deadline = time.monotonic() + seconds
    latest = {}
    while time.monotonic() < deadline:
        try:
            latest = _snapshot(evaluate)
            if predicate(latest):
                return latest
        except (AssertionError, KeyError):
            pass  # A full navigation briefly replaces the JavaScript context.
        time.sleep(0.05)
    raise AssertionError(f"route/state did not settle: {latest!r}")


def _visit(evaluate, route: str, id12: str, *, navigate: bool = True) -> dict:
    target = f"#/sample/{id12}/{route}"
    if navigate:
        evaluate(f"location.hash = {json.dumps(target)}")
    return _until(evaluate, lambda row: row["rendered"] == target and row["route"] == route)


def _observe(row: dict, state: str, route: str, id12: str) -> None:
    assert row["rendered"] == f"#/sample/{id12}/{route}", (state, route, row)
    assert row["id12"] == id12, (state, route, row)
    assert id12 in row["context"], (state, route, row)
    assert row["result"].startswith("Recorded result:"), (state, route, row)
    if state == "empty_root":
        assert not row["found"] and row["heading"] == "Unknown sample", row
        assert "No Lab sample matches" in row["main"] and "no selected result" in row["result"], row
    elif state in ("missing_selected_input", "unreadable_completion_history"):
        expected = ("Completed input unavailable" if state == "missing_selected_input"
                    else "Completion history unavailable")
        assert row["found"] and row["heading"] == expected, row
        assert "Back to Lab home" in row["main"], row
        if state == "missing_selected_input":
            assert "selected input unavailable" in row["result"] and "no result is asserted" in row["result"], row
        else:
            assert "completion history unavailable" in row["result"] and "latest completion cannot be verified" in row["result"], row
    else:
        assert row["found"] and row["input"] == "b" * 64, (state, route, row)
        assert row["heading"] and row["heading"] not in ("Unknown sample", "Completed input unavailable",
                                                     "Completion history unavailable"), row
        assert row["navCurrent"] == f"#/sample/{id12}/{route}", (state, route, row)
        assert "recorded" in row["result"].lower() and row["algorithm"], row


@needs_chrome
@pytest.mark.parametrize("state", STATES)
def test_product_route_state_matrix(state: str, tmp_path: Path, record_property) -> None:
    """A passing run emits all nine observed cells for its state, or fails at the cell."""
    sample = _sample()
    id12 = ID12
    variants: dict[str, dict] | None = None
    mode = {"name": "cohort_missing"}
    phase = {"down": False}
    if state == "partial_results":
        _mark(sample, "bam-qc", "failed")
        _mark(sample, "copy-number", "skipped", reason="missing_asset")
        sample["algorithms"] = [row for row in sample["algorithms"] if row["algorithm_id"] != "end-motifs"]
        sample["unreadable_results"] = ["end-motifs"]
        sample["excluded_chunks"] = [2, 5]
    elif state == "failed_algorithm":
        _mark(sample, "bam-qc", "failed")
    elif state == "platform_skip":
        input_dir = tmp_path / "ont-input"
        input_dir.mkdir()
        (input_dir / "manifest.json").write_text('{"kind":"unaligned"}')
        ctx = InputContext(root=tmp_path, source_id=sample["source_id"],
                           input_id=sample["full_input_id"], reference_id="hg38",
                           reference_sha256="a" * 64, input_dir=input_dir, platform="ont")
        skip = SHORT_READ_FRAGMENT_SIZE.preflight(ctx)
        assert skip.reason_code == "platform"
        _mark(sample, "short-read-fragment-size", "skipped", reason=skip.reason_code)
        _algorithm(sample, "short-read-fragment-size")["skip_message"] = skip.message
        sample["platform"] = "ont"
    elif state == "missing_reference":
        variants = {}
        for name, reason in (("cohort_missing", "cohort_too_small"),
                             ("library_missing", "no_library_entry")):
            variant = copy.deepcopy(sample)
            metric = _algorithm(variant, "fragment-size")["metrics"][0]
            metric.update(state="no_reference", reason=reason, interval=None,
                          percentile=None, exploratory_n=None,
                          reference_kind="internal_cohort" if name == "cohort_missing" else "none",
                          entry=None if name == "library_missing" else metric["entry"],
                          caption="No cohort reference" if name == "cohort_missing" else "No library entry")
            if name == "cohort_missing":
                variant.update(cohort_state="cohort_unavailable", cohort_n=0,
                               cohort_problem={"code": "TBX-LAB-022", "fix": "Build a compatible cohort"})
            variants[name] = variant
    if state == "empty_root":
        store = LabStore(tmp_path / "root")
        store.ensure()
        server = RunningLabServer(store)
    elif state in ("missing_selected_input", "unreadable_completion_history"):
        store, id12 = _broken_store(tmp_path, history=state == "unreadable_completion_history")
        server = RunningLabServer(store)
    else:
        server = _server(tmp_path, sample)
        if variants:
            server.app.api.sample = lambda requested: (copy.deepcopy(variants[mode["name"]])
                                                      if requested == ID12 else {"found": False, "candidates": []})
        if state == "running_prior_input":
            active = {"run_id": "r-20261019-1000-abcd", "command": "run", "step": "bam-qc",
                      "source_id": sample["source_id"], "input_id": "d" * 64,
                      "started_utc": "2026-10-19T10:00:00Z"}
            server.app.api.status = lambda: {"generation": 1, "active": active,
                                             "interrupted": None, "library": sample["library_health"]}
        if state == "disconnected":
            normal_status = server.app.api.status
            def status():
                if phase["down"]:
                    raise OSError("deliberate status poll failure")
                return normal_status()
            server.app.api.status = status
            kind, source = server.app.assets["/assets/lab.js"]
            assert b"const POLL_MS = 15000;" in source
            server.app.assets["/assets/lab.js"] = (
                kind, source.replace(b"const POLL_MS = 15000;", b"const POLL_MS = 500;"))

    observations = []
    with server:
        profile = tmp_path / "profile"
        url = server.issue_link() + f"&route=/sample/{id12}/{SAMPLE_ROUTES[0]}"

        def inspect(command, evaluate) -> None:
            for index, route in enumerate(SAMPLE_ROUTES):
                if state == "expired_session" and index:
                    # A fresh one-use link requires a new document.  Navigating
                    # between hashes on the expired page retains its JS state.
                    command("Page.navigate", {"url": "about:blank"})
                    for _ in range(80):
                        if evaluate("location.href === 'about:blank'"):
                            break
                        time.sleep(0.05)
                    else:
                        raise AssertionError("expired page did not leave the old document")
                    command("Page.navigate", {"url": server.issue_link() + f"&route=/sample/{id12}/{route}"})
                    _until(evaluate, lambda row: row["rendered"] == f"#/sample/{id12}/{route}" and not row["expired"])
                row = _visit(evaluate, route, id12, navigate=index > 0 and state != "expired_session")
                _observe(row, state, route, id12)
                if state == "ready":
                    assert "available for the selected input" in row["result"] and not row["stale"] == "true", row
                    if index == 0:
                        evaluate("""(() => { const previous = window.fetch;
                          window.__p12ObsoleteDone = false;
                          window.fetch = (url, ...args) => String(url).includes('/api/obsolete401')
                            ? Promise.resolve(new Response('{}', {status:401})) : previous(url, ...args);
                          window.TracebackLab.requestJson('/api/obsolete401', {isCurrent: () => false})
                            .catch(error => {window.__p12ObsoleteKind = error.kind})
                            .finally(() => {window.fetch = previous; window.__p12ObsoleteDone = true});
                          return true; })()""")
                        obsolete = _until(evaluate, lambda item: item["obsoleteDone"])
                        assert obsolete["obsoleteKind"] == "obsolete" and not obsolete["expired"], obsolete
                        assert obsolete["input"] == row["input"] and obsolete["rendered"] == row["rendered"], obsolete
                elif state == "running_prior_input":
                    assert row["activeInput"] == "d" * 64 and row["input"] == "b" * 64, row
                    assert "run is in progress" in row["run"] and "completed steps" in row["run"], row
                elif state in ("partial_results", "failed_algorithm", "platform_skip"):
                    affected = {item["id"]: item for item in row["algorithm"]}
                    assert "partially available" in row["result"], row
                    if state == "partial_results":
                        assert "1 failed, 1 skipped" in row["result"], row
                        assert affected["bam-qc"]["status"] == "failed" and not affected["bam-qc"]["values"], row
                        assert affected["copy-number"]["status"] == "skipped" and not affected["copy-number"]["values"], row
                        assert "end-motifs" not in affected and "end-motifs" in row["unreadable"], row
                        assert "fragment-size" in affected and row["excluded"] == [2, 5], row
                    elif state == "failed_algorithm":
                        assert affected["bam-qc"]["status"] == "failed" and not affected["bam-qc"]["values"], row
                        if route == "methods":
                            assert "Recorded step failed" in row["main"] and "negative biological finding" in row["main"], row
                    else:
                        assert affected["short-read-fragment-size"]["reason"] == "platform", row
                        assert not affected["short-read-fragment-size"]["values"], row
                        if route == "methods":
                            assert "Input platform mismatch" in row["main"], row
                elif state == "disconnected":
                    phase["down"] = True
                    stale = _until(evaluate, lambda item: item["lost"] and item["missed"] >= 3, seconds=6)
                    assert stale["input"] == row["input"] and stale["rendered"] == row["rendered"], stale
                    assert stale["stale"] == "true" and "last confirmed result" in stale["freshness"], stale
                    assert stale["switchDisabled"] and all(stale["serverControls"]), stale
                    assert "current status is unknown" in stale["run"], stale
                    row = stale
                    phase["down"] = False
                    _until(evaluate, lambda item: not item["lost"] and item["stale"] == "false", seconds=6)
                elif state == "missing_reference":
                    metric = row["referenceMetric"]
                    assert metric["value"] == 1.0 and metric["state"] == "no_reference", row
                    assert metric["reason"] == "cohort_too_small" and metric["interval"] is None, row
                    if route == "findings":
                        assert "No reference" in row["main"] and "1" in row["main"], row
                    if route == "methods":
                        assert "Cohort reference: unavailable" in row["main"], row
                elif state == "expired_session":
                    evaluate("""(() => { const previous = window.fetch;
                      window.fetch = (url, ...args) => String(url).includes('/api/samples/')
                        ? Promise.resolve(new Response('{}', {status:401})) : previous(url, ...args);
                      window.TracebackLab.refreshSelectedSample().finally(() => {window.fetch = previous});
                      return true; })()""")
                    expired = _until(evaluate, lambda item: item["expired"])
                    assert expired["input"] == row["input"] and expired["rendered"] == row["rendered"], expired
                    assert expired["switchDisabled"] and expired["stale"] == "true", expired
                    assert "fresh one-use link" in expired["recovery"], expired
                    row = expired
                observations.append({"state": state, "route": route, "fixture": mode["name"] if variants else state,
                                     "observed": {key: row[key] for key in ("heading", "input", "result", "stale", "lost", "expired")},
                                     "result": "pass"})
            if variants:
                mode["name"] = "library_missing"
                command("Page.reload", {"ignoreCache": True})
                _until(evaluate, lambda row: row["rendered"] == f"#/sample/{id12}/{SAMPLE_ROUTES[-1]}" and row["found"])
                for route in SAMPLE_ROUTES:
                    row = _visit(evaluate, route, id12)
                    _observe(row, state, route, id12)
                    assert row["input"] == "b" * 64 and "available for the selected input" in row["result"], row
                    metric = row["referenceMetric"]
                    assert metric["value"] == 1.0 and metric["state"] == "no_reference", row
                    assert metric["reason"] == "no_library_entry" and metric["interval"] is None, row
                    if route == "findings":
                        assert "No reference" in row["main"] and "1" in row["main"], row
                    observations.append({"state": state, "route": route, "fixture": mode["name"],
                                         "observed": {"heading": row["heading"], "input": row["input"],
                                                      "result": row["result"]}, "result": "pass"})

        _dom_cdp([_chrome(), "--headless=new", "--disable-gpu", "--no-first-run",
                  f"--user-data-dir={profile}", "--window-size=1280,800", url],
                 profile, ok=lambda dom: 'data-ready="1"' in dom and '<main' in dom,
                 interact=inspect, timeout=150)

    primary = [row for row in observations if row["fixture"] != "library_missing"]
    assert len(primary) == len(SAMPLE_ROUTES) == 9, observations
    assert {row["route"] for row in primary} == set(SAMPLE_ROUTES), observations
    assert len(observations) == (18 if variants else 9), observations
    assert all(row["result"] == "pass" for row in observations)
    record_property("p12_route_state_cells", json.dumps(observations, sort_keys=True))
