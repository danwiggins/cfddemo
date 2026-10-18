"""Crash and read-boundary checks for cohort member selector publication."""

import os
import select
import signal
import time
from pathlib import Path

import pytest

from tests.lab.test_cohort_store import _sample
from traceback_lab import cohorts, pon, pon_consumer
from traceback_lab.cohorts import add_sample, build_cohort, read_current
from traceback_lab.envelope import ResultEnvelope, provenance
from traceback_lab.store import (MAX_COHORT_SELECTION_JOURNAL_BYTES, LabStore,
                                 ResultOrderUnavailable, SelectionSnapshotChanged)
from traceback_lab.views import selected_results


def _panel(sources: tuple[str, ...]) -> pon.PonBuild:
    return pon.PonBuild(
        source_ids=sources, full_asset_sha256="a" * 64,
        loo_asset_sha256s={source: f"{index:064x}" for index, source in enumerate(sources, 101)},
        reference_sha256=f"{900:064x}", aligner_identity="minimap2 2.28; samtools 1.20",
        grid_sha256="b" * 64, counting_parameters_sha256="c" * 64,
        toolchain_lock_sha256="d" * 64, builder_source_sha256="e" * 64,
        builder_sha256="3" * 64, validator_sha256="4" * 64,
        gc_sha256="f" * 64, map_sha256="1" * 64, centromere_sha256="2" * 64,
    )


def _fixture(tmp_path: Path, monkeypatch):
    store = LabStore(tmp_path)
    sources = tuple(_sample(store, index) for index in range(1, 11))
    for source in sources[:9]:
        add_sample(store, "healthy-v1", source)
    previous = build_cohort(store, "healthy-v1")
    add_sample(store, "healthy-v1", sources[9])
    monkeypatch.setattr(pon, "build_panels", lambda *_: _panel(sources))
    first = previous.members[0]

    def result(identity: str) -> ResultEnvelope:
        return ResultEnvelope(
            algorithm_id="copy-number", algorithm_version="2", status="ok",
            provenance=provenance(
                tool="fixture", parameters_sha256=identity,
                source_id=first.source_id, input_id=first.input_id, reference_id="hg38",
            ),
        )

    store.publish_result(result("8" * 64), input_id=first.input_id)
    return store, sources, previous, first.input_id, result


def _identity(store: LabStore, input_id: str) -> str:
    return selected_results(store, input_id)["copy-number"].provenance.parameters_sha256


def _wait_byte(fd: int) -> bytes:
    ready, _, _ = select.select([fd], [], [], 20)
    assert ready, "child did not reach its barrier"
    return os.read(fd, 1)


@pytest.mark.parametrize("phase", ["member", "pointer"])
def test_sigkill_before_and_after_cohort_pointer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str,
) -> None:
    store, sources, previous, input_id, result = _fixture(tmp_path, monkeypatch)
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(read_fd)
        try:
            first = True

            def recompute(child_store, pending, source_id, **_kwargs):
                nonlocal first
                if first:
                    first = False
                    child_store.publish_result(result("9" * 64), input_id=input_id)
                    if phase == "member":
                        os.write(write_fd, b"M")
                        time.sleep(120)
                return next(member for member in pending.members if member.source_id == source_id)

            pon_consumer.recompute_member = recompute
            if phase == "pointer":
                original_sync = cohorts._fsync_dir

                def pause_after_pointer(directory):
                    original_sync(directory)
                    if directory == store.lab / "cohorts" / "healthy-v1":
                        os.write(write_fd, b"P")
                        time.sleep(120)

                cohorts._fsync_dir = pause_after_pointer
            build_cohort(store, "healthy-v1")
        except BaseException:
            os.write(write_fd, b"E")
        os._exit(2)
    os.close(write_fd)
    try:
        assert _wait_byte(read_fd) == (b"M" if phase == "member" else b"P")
    finally:
        os.kill(pid, signal.SIGKILL)
        os.waitpid(pid, 0)
        os.close(read_fd)

    reopened = LabStore(tmp_path)
    assert reopened.cohort_selection_journal_file.is_file()
    current = read_current(reopened, "healthy-v1")
    assert current is not None
    assert current.version == (previous.version if phase == "member" else previous.version + 1)
    assert _identity(reopened, input_id) == ("8" if phase == "member" else "9") * 64
    # A new writer must repair/confirm the transaction before touching run state.
    with reopened.writer("test recovery", "r-20261017-0000-abcd"):
        assert not reopened.cohort_selection_journal_file.exists()
    assert _identity(reopened, input_id) == ("8" if phase == "member" else "9") * 64
    attempts = reopened.result_dir(input_id, "copy-number", "9" * 64) / "attempts"
    assert any((path / "result.json").is_file() for path in attempts.iterdir())
    with reopened.writer("test second recovery", "r-20261017-0001-abcd"):
        pass


def test_pointer_crossing_rejects_pinned_old_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, sources, previous, input_id, result = _fixture(tmp_path, monkeypatch)
    ready_read, ready_write = os.pipe()
    go_read, go_write = os.pipe()
    done_read, done_write = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(ready_read)
        os.close(go_write)
        os.close(done_read)
        try:
            first = True

            def recompute(child_store, pending, source_id, **_kwargs):
                nonlocal first
                if first:
                    first = False
                    child_store.publish_result(result("9" * 64), input_id=input_id)
                    os.write(ready_write, b"R")
                    os.read(go_read, 1)
                return next(member for member in pending.members if member.source_id == source_id)

            pon_consumer.recompute_member = recompute
            original_sync = cohorts._fsync_dir

            def pause_after_pointer(directory):
                original_sync(directory)
                if directory == store.lab / "cohorts" / "healthy-v1":
                    os.write(done_write, b"P")
                    time.sleep(120)

            cohorts._fsync_dir = pause_after_pointer
            build_cohort(store, "healthy-v1")
        except BaseException:
            os.write(done_write, b"E")
        os._exit(2)
    os.close(ready_write)
    os.close(go_read)
    os.close(done_write)
    try:
        assert _wait_byte(ready_read) == b"R"
        reader = LabStore(tmp_path)
        with pytest.raises(SelectionSnapshotChanged):
            with reader.selection_read_context():
                assert _identity(reader, input_id) == "8" * 64
                os.write(go_write, b"G")
                assert _wait_byte(done_read) == b"P"
                assert read_current(reader, "healthy-v1").version == previous.version + 1
    finally:
        os.kill(pid, signal.SIGKILL)
        os.waitpid(pid, 0)
        for fd in (ready_read, go_write, done_read):
            os.close(fd)


def test_failed_pointer_directory_sync_keeps_recovery_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, sources, previous, input_id, result = _fixture(tmp_path, monkeypatch)
    first = True

    def recompute(child_store, pending, source_id, **_kwargs):
        nonlocal first
        if first:
            first = False
            child_store.publish_result(result("9" * 64), input_id=input_id)
        return next(member for member in pending.members if member.source_id == source_id)

    monkeypatch.setattr(pon_consumer, "recompute_member", recompute)
    original_sync = cohorts._fsync_dir

    def fail_pointer_sync(directory):
        if directory == store.lab / "cohorts" / "healthy-v1":
            raise OSError("injected pointer directory fsync failure")
        original_sync(directory)

    monkeypatch.setattr(cohorts, "_fsync_dir", fail_pointer_sync)
    with pytest.raises(OSError, match="injected pointer directory fsync failure"):
        build_cohort(store, "healthy-v1")
    assert store.cohort_selection_journal_file.is_file()
    monkeypatch.setattr(cohorts, "_fsync_dir", original_sync)
    reopened = LabStore(tmp_path)
    with reopened.writer("test recovery", "r-20261017-0002-abcd"):
        assert read_current(reopened, "healthy-v1").version == previous.version + 1
    assert not reopened.cohort_selection_journal_file.exists()
    assert _identity(reopened, input_id) == "9" * 64


def test_recovery_rejects_symlinked_result_ancestor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, sources, previous, input_id, result = _fixture(tmp_path, monkeypatch)
    identity = "8" * 64
    selector = store.result_dir(input_id, "copy-number", identity) / "selected.json"
    original_content = selector.read_bytes()
    with store.writer("begin test transaction", "r-20261017-0003-abcd"):
        store.begin_cohort_selection_transaction(
            cohort_id="healthy-v1", old_version=previous.version,
            new_version=previous.version + 1, run_id="r-20261017-0003-abcd",
            input_ids=(input_id,), algorithm_ids=("copy-number",),
            before={selector: original_content},
        )
    input_dir = store.results_dir / input_id
    moved = tmp_path / "saved-result-input"
    input_dir.rename(moved)
    outside = tmp_path / "outside"
    outside_selector = outside / "copy-number" / identity / "selected.json"
    outside_selector.parent.mkdir(parents=True)
    outside_selector.write_bytes(b"external sentinel")
    input_dir.symlink_to(outside, target_is_directory=True)
    with pytest.raises((OSError, ValueError)):
        with store.writer("recover", "r-20261017-0004-abcd"):
            pass
    assert outside_selector.read_bytes() == b"external sentinel"
    assert store.cohort_selection_journal_file.is_file()


def test_first_build_killed_after_generation_publish_remains_unbuilt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = LabStore(tmp_path)
    sources = tuple(_sample(store, index) for index in range(1, 11))
    for source in sources:
        add_sample(store, "healthy-v1", source)
    monkeypatch.setattr(pon, "build_panels", lambda *_: _panel(sources))
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(read_fd)
        try:
            pon_consumer.recompute_member = lambda _store, pending, source_id, **_kwargs: next(
                member for member in pending.members if member.source_id == source_id
            )
            original_write = cohorts.write_atomic

            def pause_before_pointer(path, content):
                if path == store.lab / "cohorts" / "healthy-v1" / "current.json":
                    os.write(write_fd, b"G")
                    time.sleep(120)
                original_write(path, content)

            cohorts.write_atomic = pause_before_pointer
            build_cohort(store, "healthy-v1")
        except BaseException:
            os.write(write_fd, b"E")
        os._exit(2)
    os.close(write_fd)
    try:
        assert _wait_byte(read_fd) == b"G"
    finally:
        os.kill(pid, signal.SIGKILL)
        os.waitpid(pid, 0)
        os.close(read_fd)
    reopened = LabStore(tmp_path)
    assert (reopened.lab / "cohorts" / "healthy-v1" / "generations" / "1").is_dir()
    assert read_current(reopened, "healthy-v1") is None
    with reopened.writer("recover first build", "r-20261017-0005-abcd"):
        assert read_current(reopened, "healthy-v1") is None
    assert not reopened.cohort_selection_journal_file.exists()
    assert (reopened.lab / "cohorts" / "healthy-v1" / "aborted-generations.json").is_file()
    assert read_current(reopened, "healthy-v1") is None


@pytest.mark.parametrize("damage", ["malformed", "oversized", "symlink"])
def test_unsafe_journal_blocks_readers_and_next_writer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, damage: str,
) -> None:
    store, sources, previous, input_id, result = _fixture(tmp_path, monkeypatch)
    journal = store.cohort_selection_journal_file
    selector = store.result_dir(input_id, "copy-number", "8" * 64) / "selected.json"
    original = selector.read_bytes()
    if damage == "malformed":
        journal.write_bytes(b"{not json}")
    elif damage == "oversized":
        with journal.open("wb") as stream:
            stream.truncate(MAX_COHORT_SELECTION_JOURNAL_BYTES + 1)
    else:
        target = tmp_path / "external-journal"
        target.write_bytes(b"external sentinel")
        journal.symlink_to(target)
    with pytest.raises(ResultOrderUnavailable):
        selected_results(LabStore(tmp_path), input_id)
    with pytest.raises(ResultOrderUnavailable):
        with LabStore(tmp_path).writer("blocked by journal", "r-20261017-0006-abcd"):
            pass
    assert selector.read_bytes() == original
    assert read_current(store, "healthy-v1") == previous
