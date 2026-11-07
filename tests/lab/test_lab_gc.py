"""``traceback lab gc``: dry-run lists, --apply deletes exactly that, never the selected or ok attempts."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tests.lab.test_cli_error_triggers import _assert_both_modes
from tests.lab.test_store import _envelope, _failed, A, C
from traceback_lab import gc_command
from traceback_lab.gc_command import GC_PREFIX, rerun_command, scan_attempts, store_label, tree_bytes
from traceback_lab.scrub import Scrubber
from traceback_lab.store import ARTIFACT_INDEX_SCHEMA, LabStore, new_run_id, summarize_run
from traceback_runner import cli as signed_cli
from traceback_runner.serialization import canonical_json_bytes

JSON_KEYS = {
    "root", "mode", "applied", "keep_attempts", "held_reason", "attempts", "skipped_results", "staging",
    "interrupted", "pon", "total",
}
LABEL = f"results/{C[:12]}/fragment-size/{A[:12]}"


def _run(capsys, argv: list[str]) -> tuple[int, str]:
    code = signed_cli.main(["lab", *argv])
    return code, capsys.readouterr().out


def _json(capsys, argv: list[str]) -> tuple[int, dict]:
    code, out = _run(capsys, [*argv, "--json"])
    assert out.count("\n") == 1
    payload = json.loads(out)
    assert payload["schema_version"] == "traceback.cli-result.v2"
    return code, payload


def _raw_failed_attempt(store: LabStore, n: int, *, ordinal: int | None, size: int = 100) -> str:
    """A failed attempt published outside ``publish_result`` (so pruning never saw it).

    ``ordinal=None`` writes a legacy index (no ordinal): the store orders it first.
    """

    attempt = f"a-20261001T{n:012d}-{n:08x}"

    def build(staging: Path) -> None:
        (staging / "result.json").write_bytes(_failed(n).to_bytes())
        (staging / "artifacts").mkdir()
        (staging / "artifacts" / "log.txt").write_bytes(b"x" * size)
        index: dict = {"schema": ARTIFACT_INDEX_SCHEMA, "files": {"log.txt": size}}
        if ordinal is not None:
            index["ordinal"] = ordinal
        (staging / "artifacts.json").write_bytes(canonical_json_bytes(index))

    store.publish_directory(store.result_dir(C, "fragment-size", A) / "attempts", attempt, build)
    return attempt


def _attempts_dir(store: LabStore) -> Path:
    return store.result_dir(C, "fragment-size", A) / "attempts"


def _attempts(store: LabStore) -> set[str]:
    return {path.name for path in _attempts_dir(store).iterdir()}


@pytest.fixture
def stale_store(tmp_path: Path) -> dict:
    """Two stale (legacy-ordered) failed attempts, the newest three failures, one selected ok, one orphan staging.

    ``publish_result`` prunes as it goes, so the stale attempts are added last with a
    legacy index: the store orders them before every attempt with an ordinal.
    """

    root = tmp_path / "root"
    store = LabStore(root)
    store.ensure()
    recent = [store.publish_result(_failed(n), input_id=C) for n in range(10, 13)]
    selected = store.publish_result(_envelope(1), input_id=C, artifacts={"h/x.json": b"ok"})
    stale = [_raw_failed_attempt(store, n, ordinal=None, size=100 * (n + 1)) for n in range(2)]
    aligned = store.inputs_dir / ("e" * 64)
    aligned.mkdir(parents=True)
    (aligned / "aligned.bam").write_bytes(b"not really a bam")
    orphan = store.inputs_dir / ".staging-0123456789abcdef"
    (orphan / "nested").mkdir(parents=True)
    (orphan / "nested" / "part.bam").write_bytes(b"y" * 4096)
    (orphan / "manifest.json").write_bytes(b"{}")
    log = store.run_log("r-20261106-0900-abcd")
    log.append("run_start", command="run")
    stale_bytes = [tree_bytes(_attempts_dir(store) / name) for name in stale]
    assert stale_bytes[0] >= 100 and stale_bytes[1] >= 200 + 100  # artifact plus result/index files
    return {
        "root": root, "store": store, "stale": stale, "recent": recent, "selected": selected,
        "orphan": orphan, "aligned": aligned, "stale_bytes": stale_bytes, "orphan_bytes": 4096 + 2,
    }


def test_dry_run_lists_items_with_bytes_and_deletes_nothing(stale_store, capsys) -> None:
    root = stale_store["root"]
    store = stale_store["store"]
    before = _attempts(store)
    code, payload = _json(capsys, ["gc", "--root", str(root)])
    assert code == 0 and payload["status"] == "ok"
    data = payload["data"]
    assert data["root"] == str(root)
    assert data["mode"] == "dry-run" and data["applied"] is False and data["keep_attempts"] == 3
    assert data["held_reason"] is None
    attempts = data["attempts"]
    assert attempts["scanned"] is True and attempts["skipped_reason"] is None
    assert [item["attempt_id"] for item in attempts["items"]] == stale_store["stale"]
    assert [item["bytes"] for item in attempts["items"]] == stale_store["stale_bytes"]
    assert attempts["count"] == 2 and attempts["bytes"] == sum(stale_store["stale_bytes"])
    for item in attempts["items"]:
        assert item["input_id"] == C and item["algorithm_id"] == "fragment-size"
        assert item["parameters_sha256"] == A and item["status"] == "failed"
    staging = data["staging"]
    assert staging["count"] == 1 and staging["held"] == 0
    assert staging["items"] == [
        {"path": "inputs/.staging-0123456789abcdef", "label": "inputs/.staging-0123456789abcdef", "bytes": stale_store["orphan_bytes"]},
    ]
    assert data["interrupted"] == {"count": 0, "bytes": 0, "held": 0, "items": []}
    assert data["skipped_results"] == []
    assert data["pon"]["collected"] is False and data["pon"]["reason"]
    assert data["total"] == {"count": 3, "bytes": sum(stale_store["stale_bytes"]) + stale_store["orphan_bytes"]}
    assert "removed" not in data
    # Nothing moved.
    assert _attempts(store) == before
    assert stale_store["orphan"].is_dir()
    assert (stale_store["aligned"] / "aligned.bam").exists()

    code, out = _run(capsys, ["gc", "--root", str(root)])
    lines = out.splitlines()
    assert code == 0 and lines[0] == f"ROOT  {root}"
    assert lines[1].startswith("MODE  dry-run")
    assert not any(line.startswith("HELD") for line in lines)
    assert any(line.startswith("ATTEMPTS     2 failed attempt(s) beyond the newest 3") for line in lines)
    assert sum(1 for line in lines if line.startswith("  results/")) == 2
    for attempt, size in zip(stale_store["stale"], stale_store["stale_bytes"]):
        assert f"  {LABEL}/attempts/{attempt}  {size:,} B" in lines
    assert "<path>" not in out and "<redacted>" not in out
    assert not any(line.startswith("SKIPPED") for line in lines)
    # Every label survives the scrubber unchanged (one label form for every category).
    scrub = Scrubber(root=root)
    labels = [item["label"] for category in ("attempts", "staging", "interrupted") for item in data[category]["items"]]
    assert labels and all(scrub.line(label) == label for label in labels)
    assert any(line.startswith("STAGING      1 orphaned .staging-* directory") for line in lines)
    assert "  inputs/.staging-0123456789abcdef  4,098 B" in lines
    assert any(line.startswith("INTERRUPTED  0 unfinished removal(s)") for line in lines)
    assert any(line.startswith("PONS         not collected") for line in lines)
    assert any(line.startswith("TOTAL        3 item(s), ") and "would remove" in line for line in lines)
    assert str(root.parent) not in "\n".join(lines[1:])


def test_apply_deletes_exactly_the_listed_items_and_logs_a_run(stale_store, capsys) -> None:
    root = stale_store["root"]
    store = stale_store["store"]
    _, preview = _json(capsys, ["gc", "--root", str(root)])
    code, payload = _json(capsys, ["gc", "--apply", "--root", str(root)])
    assert code == 0 and payload["status"] == "ok"
    data = payload["data"]
    assert data["mode"] == "apply" and data["applied"] is True
    assert data["attempts"]["items"] == preview["data"]["attempts"]["items"]
    assert data["staging"]["items"] == preview["data"]["staging"]["items"]
    expected = {"count": 3, "bytes": sum(stale_store["stale_bytes"]) + stale_store["orphan_bytes"]}
    assert {key: data["removed"][key] for key in ("count", "bytes")} == preview["data"]["total"] == expected
    assert data["removed"]["items"] == [
        {"label": item["label"], "bytes": item["bytes"]}
        for category in ("attempts", "staging") for item in preview["data"][category]["items"]
    ]
    assert data["freed_bytes"] == expected["bytes"]
    assert _attempts(store) == {*stale_store["recent"], stale_store["selected"]}
    assert not stale_store["orphan"].exists()
    assert (stale_store["aligned"] / "aligned.bam").read_bytes() == b"not really a bam"
    assert store.selected_attempt(C, "fragment-size", A).name == stale_store["selected"]
    assert (store.runs_dir / "r-20261106-0900-abcd.jsonl").exists()
    rows = [summarize_run(store.run_log(run_id).events(), store) for run_id in store.run_ids()]
    gc_rows = [row for row in rows if row["command"] == "gc"]
    assert len(gc_rows) == 1 and gc_rows[0]["status"] == "ok" and gc_rows[0]["exit_code"] == 0

    # A second apply finds nothing.
    code, payload = _json(capsys, ["gc", "--apply", "--root", str(root)])
    assert code == 0 and payload["data"]["removed"] == {"count": 0, "bytes": 0, "items": []}
    code, out = _run(capsys, ["gc", "--apply", "--root", str(root)])
    lines = out.splitlines()
    assert code == 0 and lines[1] == "MODE  applied"
    assert any(line.startswith("TOTAL        0 of 0 item(s) removed, 0 B freed") for line in lines)


def test_keep_attempts_bounds_the_failed_set_but_never_the_selected_or_ok(tmp_path: Path, capsys) -> None:
    root = tmp_path / "root"
    store = LabStore(root)
    store.ensure()
    store.publish_result(_envelope(1), input_id=C)  # pruned at the first failure
    newer_ok = store.publish_result(_envelope(2), input_id=C)
    failures = [store.publish_result(_failed(n), input_id=C) for n in range(3)]
    # selected = failures[-1]; last_success = newer_ok; older_ok was pruned at the first failure
    assert _attempts(store) == {newer_ok, *failures}
    code, payload = _json(capsys, ["gc", "--keep-attempts", "0", "--root", str(root)])
    assert code == 0
    listed = [item["attempt_id"] for item in payload["data"]["attempts"]["items"]]
    assert listed == failures[:-1]  # the selected failed attempt is never a candidate
    code, payload = _json(capsys, ["gc", "--keep-attempts", "1", "--root", str(root)])
    assert [item["attempt_id"] for item in payload["data"]["attempts"]["items"]] == failures[:-1]
    code, payload = _json(capsys, ["gc", "--keep-attempts", "2", "--root", str(root)])
    assert [item["attempt_id"] for item in payload["data"]["attempts"]["items"]] == failures[:1]
    code, _ = _json(capsys, ["gc", "--apply", "--keep-attempts", "0", "--root", str(root)])
    assert code == 0
    assert _attempts(store) == {newer_ok, failures[-1]}
    assert store.selected_attempt(C, "fragment-size", A).name == failures[-1]


def test_unreadable_selector_order_or_result_protects_the_whole_result(tmp_path: Path, capsys) -> None:
    root = tmp_path / "root"
    store = LabStore(root)
    store.ensure()
    for n in range(5):
        _raw_failed_attempt(store, n, ordinal=n + 1)
    directory = store.result_dir(C, "fragment-size", A)
    selector = directory / "selected.json"
    selector.write_text("not json")
    code, payload = _json(capsys, ["gc", "--root", str(root)])
    assert code == 0 and payload["data"]["attempts"]["count"] == 0
    assert payload["data"]["skipped_results"] == [{"label": LABEL, "reason": "unreadable selector"}]
    code, out = _run(capsys, ["gc", "--root", str(root)])
    assert "SKIPPED      1 result directory (unreadable selector/order/status)" in out.splitlines()
    assert f"  {LABEL}  unreadable selector" in out.splitlines()
    # A selector this code does not understand (no attempt_id) protects the result too (P2-1).
    selector.write_text(json.dumps({"attempt": "a-20261001T000000000004-00000004", "publication_order": 1}))
    code, payload = _json(capsys, ["gc", "--keep-attempts", "0", "--root", str(root)])
    assert code == 0 and payload["data"]["attempts"]["count"] == 0
    selector.write_text(json.dumps({"attempt_id": "a-20261001T000000000004-00000004", "publication_order": 1}))
    code, payload = _json(capsys, ["gc", "--keep-attempts", "0", "--root", str(root)])
    assert [item["attempt_id"] for item in payload["data"]["attempts"]["items"]] == [
        f"a-20261001T{n:012d}-{n:08x}" for n in range(4)
    ]
    selector.unlink()
    code, payload = _json(capsys, ["gc", "--root", str(root)])
    assert code == 0 and payload["data"]["attempts"]["count"] == 2
    # An attempt whose result.json makes the store's reader raise is unknown, not a traceback (P2-3).
    (directory / "attempts" / "a-20261001T000000000000-00000000" / "result.json").write_text(
        '{"status":' + "[" * 2000 + "]" * 2000 + "}"
    )
    code, payload = _json(capsys, ["gc", "--root", str(root)])
    assert code == 0 and payload["data"]["attempts"]["count"] == 0
    assert payload["data"]["skipped_results"] == [{"label": LABEL, "reason": "unreadable status"}]
    (directory / "attempts" / "a-20261001T000000000000-00000000" / "result.json").write_bytes(_failed(0).to_bytes())
    code, payload = _json(capsys, ["gc", "--root", str(root)])
    assert code == 0 and payload["data"]["attempts"]["count"] == 2
    index = directory / "attempts" / _raw_failed_attempt(store, 9, ordinal=1) / "artifacts.json"
    index.write_text('{"ordinal": "x"}')
    code, payload = _json(capsys, ["gc", "--root", str(root)])
    assert code == 0 and payload["data"]["attempts"]["count"] == 0
    assert payload["data"]["skipped_results"] == [{"label": LABEL, "reason": "unreadable order"}]
    # A truncated, non-UTF-8 index is unknown order too, never a traceback (P2-1).
    index.write_bytes(b'{"schema": "\xff\xfe\x80')
    code, payload = _json(capsys, ["gc", "--root", str(root)])
    assert code == 0 and payload["data"]["attempts"]["count"] == 0
    assert payload["data"]["skipped_results"] == [{"label": LABEL, "reason": "unreadable order"}]
    code, payload = _json(capsys, ["gc", "--apply", "--root", str(root)])
    assert code == 0 and payload["data"]["skipped_results"] == [{"label": LABEL, "reason": "unreadable order"}]
    assert len(_attempts(store)) == 6


def test_pending_cohort_transaction_holds_every_category(stale_store, capsys) -> None:
    root = stale_store["root"]
    store = stale_store["store"]
    store.cohort_selection_journal_file.write_bytes(b"{}")
    code, payload = _json(capsys, ["gc", "--root", str(root)])
    assert code == 0
    data = payload["data"]
    assert "pending" in data["held_reason"]
    assert data["attempts"]["scanned"] is False and data["attempts"]["count"] == 0
    assert data["staging"]["count"] == 0 and data["staging"]["held"] == 1 and data["staging"]["items"] == []
    assert data["total"] == {"count": 0, "bytes": 0}
    code, out = _run(capsys, ["gc", "--root", str(root)])
    lines = out.splitlines()
    assert lines[2].startswith("HELD  a cohort selection transaction is pending")
    assert any(line.startswith("ATTEMPTS     not scanned: ") for line in lines)
    assert any(line.startswith("STAGING      0 orphaned") and "1 held: a cohort selection transaction" in line for line in lines)
    # --apply goes through the writer, which replays the journal first; an unreadable
    # journal is refused there (TBX-LAB-022) and nothing has been deleted.
    _assert_both_modes(capsys, ["gc", "--apply", "--root", str(root)], "TBX-LAB-022", 3)
    assert stale_store["orphan"].is_dir() and _attempts(store) >= set(stale_store["stale"])


def test_locked_store_refuses_apply_and_dry_run_holds_staging(stale_store, capsys) -> None:
    root = stale_store["root"]
    store = stale_store["store"]
    run_id = new_run_id()
    with store.writer("run", run_id, step="align"):
        human, data = _assert_both_modes(capsys, ["gc", "--apply", "--root", str(root)], "TBX-LAB-012", 3)
        assert data["holder"]["run_id"] == run_id and data["retryable"] is True
        assert stale_store["orphan"].is_dir() and _attempts(store) >= set(stale_store["stale"])
        code, payload = _json(capsys, ["gc", "--root", str(root)])
        assert code == 0
        staging = payload["data"]["staging"]
        assert staging["items"] == [] and staging["count"] == 0 and staging["held"] == 1
        assert payload["data"]["held_reason"] == "a live writer may own them"
        assert payload["data"]["attempts"]["count"] == 2
        code, out = _run(capsys, ["gc", "--root", str(root)])
        assert "1 held: a live writer may own them" in out
    code, payload = _json(capsys, ["gc", "--root", str(root)])
    assert payload["data"]["staging"]["count"] == 1 and payload["data"]["held_reason"] is None


def test_staging_only_leaves_every_attempt(stale_store, capsys) -> None:
    root = stale_store["root"]
    store = stale_store["store"]
    before = _attempts(store)
    code, payload = _json(capsys, ["gc", "--staging-only", "--root", str(root)])
    assert code == 0
    data = payload["data"]
    assert data["attempts"] == {"scanned": False, "skipped_reason": "--staging-only", "count": 0, "bytes": 0, "items": []}
    assert data["staging"]["count"] == 1 and data["total"]["count"] == 1
    code, payload = _json(capsys, ["gc", "--staging-only", "--apply", "--root", str(root)])
    assert code == 0 and payload["data"]["removed"] == {
        "count": 1, "bytes": stale_store["orphan_bytes"],
        "items": [{"label": "inputs/.staging-0123456789abcdef", "bytes": stale_store["orphan_bytes"]}],
    }
    assert not stale_store["orphan"].exists()
    assert _attempts(store) == before
    # The doctor's staging fix prescribes exactly this.
    from traceback_lab.doctor import lab_checks

    (store.inputs_dir / ".staging-fedcba9876543210").mkdir()
    staging = next(check for check in lab_checks(store) if check["name"] == "staging")
    assert staging["fix"] == (
        "traceback lab gc --staging-only --root <ROOT> (lists them), "
        "then traceback lab gc --staging-only --apply --root <ROOT>"
    )


def test_unknown_root_is_tbx_lab_017_and_never_creates_a_store(tmp_path: Path, capsys) -> None:
    root = tmp_path / "nowhere"
    _assert_both_modes(capsys, ["gc", "--root", str(root)], "TBX-LAB-017", 4)
    _assert_both_modes(capsys, ["gc", "--apply", "--root", str(root)], "TBX-LAB-017", 4)
    assert not root.exists()


def test_json_shape_and_no_host_path_beyond_root(stale_store, capsys) -> None:
    root = stale_store["root"]
    code, payload = _json(capsys, ["gc", "--root", str(root)])
    assert code == 0
    assert payload["command"] == "lab gc"
    data = dict(payload["data"])
    assert set(data) == JSON_KEYS
    assert set(data["attempts"]) == {"scanned", "skipped_reason", "count", "bytes", "items"}
    assert set(data["staging"]) == set(data["interrupted"]) == {"count", "bytes", "held", "items"}
    assert data.pop("root") == str(root)
    assert str(root.parent) not in json.dumps(data)
    assert str(root) not in json.dumps(data)
    code, payload = _json(capsys, ["gc", "--apply", "--root", str(root)])
    assert set(payload["data"]) == JSON_KEYS | {"removed", "freed_bytes"}


def test_bad_keep_attempts_is_a_usage_error(tmp_path: Path, capsys) -> None:
    code, payload = _json(capsys, ["gc", "--keep-attempts", "-1", "--root", str(tmp_path)])
    assert code == 2 and payload["status"] == "usage"
    code, _ = _run(capsys, ["gc", "--keep-attempts", "x", "--root", str(tmp_path)])
    assert code == 2


def test_unremovable_staging_is_tbx_lab_027_with_measured_bytes(stale_store, capsys) -> None:
    if os.geteuid() == 0:
        pytest.skip("root removes everything")
    root = stale_store["root"]
    store = stale_store["store"]
    store.inputs_dir.chmod(0o500)
    try:
        human, data = _assert_both_modes(capsys, ["gc", "--apply", "--root", str(root)], "TBX-LAB-027", 3)
        scoped = []
        for argv in (["--staging-only"], ["--keep-attempts", "10"], ["--staging-only", "--keep-attempts", "0"]):
            _, scoped_data = _assert_both_modes(capsys, ["gc", "--apply", *argv, "--root", str(root)], "TBX-LAB-027", 3)
            scoped.append(scoped_data)
    finally:
        store.inputs_dir.chmod(0o700)
    assert data["retryable"] is True
    # The fix repeats the operator's own scope, never a bare `gc --apply` (P1).
    assert "`traceback lab gc --apply --root <ROOT>`" in data["fix"] and data["rerun"] == "traceback lab gc --apply --root <ROOT>"
    assert "`traceback lab gc --apply --staging-only --root <ROOT>`" in scoped[0]["fix"]
    assert "`traceback lab gc --apply --keep-attempts 10 --root <ROOT>`" in scoped[1]["fix"]
    assert "`traceback lab gc --apply --staging-only --keep-attempts 0 --root <ROOT>`" in scoped[2]["fix"]
    assert all("gc --apply`" not in item["fix"] and "--staging-only" in item["fix"] for item in (scoped[0], scoped[2]))
    assert all(str(root) not in item["fix"] for item in (data, *scoped))
    assert rerun_command(keep=3, staging_only=False) == "traceback lab gc --apply --root <ROOT>"
    # The apply report (per-item outcome) precedes the problem block (P2-5).
    lines = human.splitlines()
    assert lines[1] == "BLOCKED  Garbage collection could not remove an item"
    assert lines.index("MODE  applied") < lines.index("CODE       TBX-LAB-027")
    assert any(line.startswith("  inputs/.staging-0123456789abcdef  ") and "NOT removed (0 B still held)" in line for line in lines)
    assert any(line.startswith("STAGING      1 orphaned .staging-* directory, ") and line.endswith(" attempted") for line in lines)
    freed = sum(stale_store["stale_bytes"]) + stale_store["orphan_bytes"]
    assert f"TOTAL        2 of 3 item(s) removed, {freed:,} B freed" in lines
    # rmtree emptied the orphan (its own entries are writable) but could not unlink it
    # from the read-only parent: the first (human) call freed every byte and reports the
    # directory as still held with 0 B; the second (JSON) call found 0 B to free.
    assert stale_store["orphan"].is_dir() and tree_bytes(stale_store["orphan"]) == 0
    assert f"{sum(stale_store['stale_bytes']) + stale_store['orphan_bytes']:,} B freed, 0 B still held" in human
    assert [item["label"] for item in data["failed"]] == ["inputs/.staging-0123456789abcdef"]
    assert data["failed"][0]["removed"] is False and data["failed"][0]["error"] == "PermissionError"
    assert data["failed"][0]["remaining_bytes"] == 0 and data["failed"][0]["freed_bytes"] == 0
    assert data["remaining_bytes"] == 0 and data["freed_bytes"] == 0
    assert data["removed"] == {"count": 0, "bytes": 0, "items": []}  # the stale attempts went on the first (human) call
    assert data["total"] == {"count": 1, "bytes": 0} and data["staging"]["count"] == 1
    assert str(root.parent) not in human.replace(str(root), "")
    code, payload = _json(capsys, ["gc", "--apply", "--root", str(root)])
    assert code == 0 and payload["data"]["removed"] == {
        "count": 1, "bytes": 0, "items": [{"label": "inputs/.staging-0123456789abcdef", "bytes": 0}],
    }
    assert not stale_store["orphan"].exists()


def test_interrupted_attempt_removal_is_atomic_for_the_store_and_listed_again(
    stale_store, capsys, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = stale_store["root"]
    store = stale_store["store"]
    attempts = _attempts_dir(store)
    victim = stale_store["stale"][0]
    real_rmtree = gc_command.shutil.rmtree

    def half_rmtree(path, *args, **kwargs):
        path = Path(path)
        if path.name == f"{GC_PREFIX}{victim}":
            (path / "result.json").unlink()  # one child gone, then the volume "fails"
            raise OSError(5, "synthetic I/O error")
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(gc_command.shutil, "rmtree", half_rmtree)
    code, payload = _json(capsys, ["gc", "--apply", "--root", str(root)])
    assert code == 3 and payload["data"]["code"] == "TBX-LAB-027"
    data = payload["data"]
    leftover = attempts / f"{GC_PREFIX}{victim}"
    assert leftover.is_dir() and not (attempts / victim).exists()
    assert not (leftover / "result.json").exists() and (leftover / "artifacts" / "log.txt").exists()
    # The store never sees a half-removed attempt: not under its id, ignored as .staging-*.
    assert _attempts(store) == {*stale_store["recent"], stale_store["selected"], leftover.name}
    assert store.selected_attempt(C, "fragment-size", A).name == stale_store["selected"]
    assert scan_attempts(store, keep=3) == ([], [])
    assert store.publish_result(_failed(99), input_id=C)  # publish-time pruning is not tripped
    # Counts match reality: the other stale attempt and the orphan were removed; the
    # victim freed only result.json's bytes and holds the rest.
    remaining = tree_bytes(leftover)
    assert data["removed"] == {
        "count": 2, "bytes": stale_store["stale_bytes"][1] + stale_store["orphan_bytes"],
        "items": [
            {"label": f"{LABEL}/attempts/{stale_store['stale'][1]}", "bytes": stale_store["stale_bytes"][1]},
            {"label": "inputs/.staging-0123456789abcdef", "bytes": stale_store["orphan_bytes"]},
        ],
    }
    assert data["failed"] == [{
        "label": f"{LABEL}/attempts/{victim}",
        "category": "attempts", "removed": False,
        "freed_bytes": stale_store["stale_bytes"][0] - remaining, "remaining_bytes": remaining,
        "leftover": f"{LABEL}/attempts/{leftover.name}",
        "error": "OSError",
    }]
    assert data["failed"][0]["leftover"] == store_label(store, leftover)
    assert data["freed_bytes"] == data["removed"]["bytes"] + data["failed"][0]["freed_bytes"]
    assert data["remaining_bytes"] == remaining
    # A rerun lists the leftover under its own category and the publish-time pruning did not.
    monkeypatch.setattr(gc_command.shutil, "rmtree", real_rmtree)
    code, payload = _json(capsys, ["gc", "--root", str(root)])
    assert code == 0
    interrupted = payload["data"]["interrupted"]
    assert interrupted["count"] == 1 and interrupted["bytes"] == remaining
    assert interrupted["items"] == [{
        "path": f"results/{C}/fragment-size/{A}/attempts/{leftover.name}",
        "label": data["failed"][0]["leftover"], "bytes": remaining,
    }]
    code, out = _run(capsys, ["gc", "--root", str(root)])
    assert any(line.startswith("INTERRUPTED  1 unfinished removal(s)") for line in out.splitlines())
    assert f"  {data['failed'][0]['leftover']}  {remaining:,} B" in out.splitlines()
    code, payload = _json(capsys, ["gc", "--apply", "--root", str(root)])
    assert code == 0 and payload["data"]["removed"] == {
        "count": 1, "bytes": remaining, "items": [{"label": data["failed"][0]["leftover"], "bytes": remaining}],
    }
    assert not leftover.exists()


def test_tree_bytes_does_not_follow_symlinks(tmp_path: Path) -> None:
    (tmp_path / "big").write_bytes(b"z" * 1000)
    inside = tmp_path / "dir"
    inside.mkdir()
    (inside / "small").write_bytes(b"q" * 10)
    (inside / "link").symlink_to(tmp_path / "big")
    assert tree_bytes(inside) == 10 + os.lstat(inside / "link").st_size
