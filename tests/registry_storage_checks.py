"""Shared storage-behaviour checks for the D03-pattern registries.

Each registry test module wires these checks to its own fixtures, so every
registry in the family is held to the same behaviour: torn-tail recovery is
operator-invoked only, interrupted creation and restore leave no half-built
root, owned temporary names are swept (D05 rule), a failed journal append is
truncated on any exception, the lock descriptor is read only under the
process lock that ``close()`` also holds, and the instance-seal integrity
check compares the trusted head with its seal under the instance process
lock that a head acceptance holds.
"""

from __future__ import annotations

import fcntl
import os
import secrets
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

_FD_NAMES = ("_journal_fd", "_metadata_fd", "_lock_fd", "_objects_fd", "_root_fd")


def retained(registry) -> SimpleNamespace:
    """The identity and head an operator retains, shaped like a receipt."""

    return SimpleNamespace(
        registry_id=registry._metadata.registry_id,
        registry_epoch_sha256=registry._metadata.registry_epoch_sha256,
        state_head_sha256=registry._trusted_head_sha256,
    )


def expected(values: SimpleNamespace) -> dict[str, str]:
    return {
        "expected_registry_id": values.registry_id,
        "expected_registry_epoch_sha256": values.registry_epoch_sha256,
        "expected_state_head_sha256": values.state_head_sha256,
    }


def _staged(root: Path) -> list[Path]:
    return sorted(root.parent.glob(f".{root.name}.staging-*"))


def check_torn_tail_recovery(
    registry,
    write_one: Callable[[], object],
    reopen: Callable[[SimpleNamespace], object],
    unsafe: type[Exception],
) -> None:
    """A torn tail fails closed on reopen; only the explicit entry point heals it."""

    cls = type(registry)
    write_one()
    values = retained(registry)
    root = registry.root
    registry.close()
    journal = root / "registry-journal.jsonl"
    committed = journal.read_bytes()
    assert committed.endswith(b"\n")
    torn = b'{"schema_version":"torn'
    journal.write_bytes(committed + torn)

    # Reopen never self-repairs.
    with pytest.raises(unsafe, match="incomplete"):
        reopen(values)
    assert journal.read_bytes() == committed + torn

    # Wrong retained head or identity: refused, nothing truncated.
    for override in (
        {"expected_state_head_sha256": "0" * 64},
        {"expected_registry_epoch_sha256": "f" * 64},
        {
            "expected_registry_id": values.registry_id[:-1]
            + ("1" if values.registry_id.endswith("0") else "0")
        },
    ):
        arguments = {**expected(values), **override}
        if arguments == expected(values):
            continue
        with pytest.raises(unsafe):
            cls.recover_torn_journal_tail(root, **arguments)
        assert journal.read_bytes() == committed + torn

    # A corrupt committed line is never "recovered" by truncation.
    lines = committed.splitlines(keepends=True)
    corrupt = b"".join(lines[:-1]) + lines[-1].replace(b'"sequence"', b'"sequencf"')
    journal.write_bytes(corrupt + torn)
    with pytest.raises(unsafe, match="journal is invalid"):
        cls.recover_torn_journal_tail(root, **expected(values))
    assert journal.read_bytes() == corrupt + torn

    journal.write_bytes(committed + torn)
    # A registry held by any live lock (even a shared fence) is refused, never
    # waited on, so recovery cannot deadlock a fence held by its own caller.
    holder = os.open(root / ".registry.lock", os.O_RDWR)
    try:
        fcntl.flock(holder, fcntl.LOCK_SH)
        with pytest.raises(unsafe, match="in use"):
            cls.recover_torn_journal_tail(root, **expected(values))
    finally:
        os.close(holder)
    assert journal.read_bytes() == committed + torn
    # Another thread holding the registry's process lock: refused, not queued.
    module_lock = sys.modules[cls.__module__]._REGISTRY_PROCESS_LOCK
    held, release = threading.Event(), threading.Event()

    def hold() -> None:
        with module_lock:
            held.set()
            release.wait(timeout=10)

    holder_thread = threading.Thread(target=hold, daemon=True)
    holder_thread.start()
    try:
        assert held.wait(timeout=5)
        with pytest.raises(unsafe, match="in use"):
            cls.recover_torn_journal_tail(root, **expected(values))
    finally:
        release.set()
        holder_thread.join(timeout=5)
    assert journal.read_bytes() == committed + torn
    assert cls.recover_torn_journal_tail(root, **expected(values)) == len(torn)
    assert journal.read_bytes() == committed
    assert cls.recover_torn_journal_tail(root, **expected(values)) == 0
    reopened = reopen(values)
    try:
        assert retained(reopened).state_head_sha256 == values.state_head_sha256
    finally:
        reopened.close()


def check_append_interrupt_truncates(
    registry,
    module: ModuleType,
    write_one: Callable[[], object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An interrupt mid-append truncates its suffix and keeps its own type."""

    journal = registry.root / "registry-journal.jsonl"
    committed = journal.read_bytes()
    journal_fd = registry._journal_fd
    original = module._write_all

    def interrupted(descriptor: int, content: bytes) -> None:
        if descriptor == journal_fd:
            os.write(descriptor, content[: len(content) // 2])
            raise KeyboardInterrupt
        original(descriptor, content)

    monkeypatch.setattr(module, "_write_all", interrupted)
    with pytest.raises(KeyboardInterrupt):
        write_one()
    monkeypatch.setattr(module, "_write_all", original)
    assert journal.read_bytes() == committed
    write_one()
    grown = journal.read_bytes()
    assert (
        grown.startswith(committed) and grown.count(b"\n") == committed.count(b"\n") + 1
    )


def check_lock_reads_descriptor_under_process_lock(
    registry, unsafe: type[Exception], tmp_path: Path
) -> None:
    """A lock waiter must not flock a descriptor number reused after close."""

    errors: list[BaseException] = []
    decoy_path = tmp_path / f"decoy-{secrets.token_hex(4)}"
    decoys: list[int] = []
    holder: int | None = None
    module = sys.modules[type(registry).__module__]
    module_lock = module._REGISTRY_PROCESS_LOCK
    reached = threading.Event()

    class _Signalling:
        # Signals once the waiter reaches the process-lock boundary; any
        # descriptor read before this point is the bug under test.
        def __enter__(self):
            reached.set()
            return module_lock.__enter__()

        def __exit__(self, *exc):
            return module_lock.__exit__(*exc)

    process_lock = registry._process_lock
    process_lock.acquire()
    module._REGISTRY_PROCESS_LOCK = _Signalling()
    try:
        lock_number = registry._lock_fd

        def waiter() -> None:
            try:
                with type(registry)._lock(registry, exclusive=False):
                    pass
            except BaseException as error:  # noqa: BLE001 - recorded for assert
                errors.append(error)

        thread = threading.Thread(target=waiter, daemon=True)
        thread.start()
        assert reached.wait(timeout=5)
        time.sleep(0.05)  # let it block on the instance lock we hold
        # Exactly what close() does, under the lock close() holds.
        for name in _FD_NAMES:
            descriptor = registry.__dict__.get(name)
            if descriptor is not None:
                os.close(descriptor)
                setattr(registry, name, None)
        # Reuse the closed lock descriptor's number for an unrelated file
        # that another description holds exclusively.
        while True:
            descriptor = os.open(decoy_path, os.O_RDWR | os.O_CREAT, 0o600)
            decoys.append(descriptor)
            if descriptor == lock_number or len(decoys) > 16:
                break
        assert lock_number in decoys
        holder = os.open(decoy_path, os.O_RDWR)
        fcntl.flock(holder, fcntl.LOCK_EX)
    finally:
        process_lock.release()
    try:
        thread.join(timeout=3)
        stuck = thread.is_alive()
    finally:
        if holder is not None:
            fcntl.flock(holder, fcntl.LOCK_UN)
            os.close(holder)
        thread.join(timeout=5)
        for descriptor in decoys:
            os.close(descriptor)
        module._REGISTRY_PROCESS_LOCK = module_lock
    assert not stuck, "lock waiter used a reused descriptor number"
    assert len(errors) == 1 and isinstance(errors[0], unsafe)
    assert "closed" in str(errors[0])


def check_integrity_reads_head_and_seal_under_process_lock(
    registry, write_one: Callable[[], object]
) -> None:
    """Head acceptance and the integrity check share the instance process lock.

    ``_accept_observed_head`` assigns the new trusted head and then re-seals
    the instance.  The integrity check that every public method runs first
    compares the instance with that seal.  Both must hold the instance's
    ``_process_lock``; otherwise a reader on another thread can observe the
    new head with the old seal and fail with "authority state changed" while a
    concurrent registration of the same bytes is in flight.  This records, on
    the calling thread, whether that lock is held at every seal write and at
    every snapshot taken for this registry, so it is deterministic: a check
    that takes the lock only around something other than the comparison still
    fails.
    """

    module = sys.modules[type(registry).__module__]
    process_lock = registry.__dict__["_process_lock"]
    original_seal = module._seal_registry_instance
    original_snapshot = module._registry_instance_snapshot
    seal_held: list[bool] = []
    snapshot_held: list[bool] = []

    def recording_seal(target):
        if target is registry:
            seal_held.append(process_lock._is_owned())
        return original_seal(target)

    def recording_snapshot(target):
        if target is registry:
            snapshot_held.append(process_lock._is_owned())
        return original_snapshot(target)

    module._seal_registry_instance = recording_seal
    try:
        module._registry_instance_snapshot = recording_snapshot
        try:
            write_one()
            module._require_registry_integrity(registry)
        finally:
            module._registry_instance_snapshot = original_snapshot
    finally:
        module._seal_registry_instance = original_seal
    assert seal_held and all(seal_held), "head re-sealed outside the instance lock"
    assert snapshot_held and all(snapshot_held), (
        "integrity snapshot taken outside the instance lock"
    )


def check_owned_temporaries(
    registry,
    reopen: Callable[[SimpleNamespace], object],
    unsafe: type[Exception],
) -> None:
    """D05 rule: owned temporary files are swept; a directory fails closed."""

    values = retained(registry)
    root = registry.root
    registry.close()
    directories = [root] + ([root / "objects"] if (root / "objects").is_dir() else [])
    planted = []
    for directory in directories:
        path = directory / f".tmp-{secrets.token_hex(16)}"
        path.write_bytes(b"interrupted")
        path.chmod(0o600)
        planted.append(path)
    reopened = reopen(values)
    reopened.close()
    assert not any(path.exists() for path in planted)

    for directory in directories:
        owned_directory = directory / f".tmp-{secrets.token_hex(16)}"
        owned_directory.mkdir(mode=0o700)
        with pytest.raises(unsafe, match="recovery is unsafe"):
            reopen(values)
        owned_directory.rmdir()
    reopen(values).close()


def check_interrupted_creation(
    create: Callable[[Path], object],
    reopen_at: Callable[[Path, SimpleNamespace], object],
    root: Path,
    module: ModuleType,
    commit_name: str,
    discard_name: str | None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Interrupted creation leaves no root at the final path; a retry works."""

    original_commit = getattr(module, commit_name)

    def crash(*args, **kwargs):
        raise KeyboardInterrupt

    # A hard crash just before publication: no cleanup runs at all.
    monkeypatch.setattr(module, commit_name, crash)
    if discard_name is not None:
        monkeypatch.setattr(module, discard_name, lambda *args, **kwargs: None)
        with pytest.raises(KeyboardInterrupt):
            create(root)
        assert not os.path.lexists(root)
        assert len(_staged(root)) == 1
        monkeypatch.undo()
        monkeypatch.setattr(module, commit_name, crash)

    # An interrupt with cleanup: the staged tree is removed too.
    before = _staged(root)
    with pytest.raises(KeyboardInterrupt):
        create(root)
    assert not os.path.lexists(root)
    assert _staged(root) == before

    # An interrupt after the rename but before the constructor learns of it:
    # the new root (whose identity was never returned) is removed by inode.
    def crash_after_rename(*args, **kwargs):
        original_commit(*args, **kwargs)
        raise KeyboardInterrupt

    monkeypatch.setattr(module, commit_name, crash_after_rename)
    with pytest.raises(KeyboardInterrupt):
        create(root)
    assert not os.path.lexists(root)
    assert _staged(root) == before
    monkeypatch.setattr(module, commit_name, original_commit)

    # An interrupt after publication, while the instance is still being sealed.
    original_seal = module._seal_registry_instance
    monkeypatch.setattr(module, "_seal_registry_instance", crash)
    with pytest.raises(KeyboardInterrupt):
        create(root)
    monkeypatch.setattr(module, "_seal_registry_instance", original_seal)
    assert not os.path.lexists(root)
    assert _staged(root) == before

    # A failure as the registry lock exits, after the instance is sealed: the
    # cleanup handle is still live, so the unreturned root is removed.
    sealed = {"on": False}

    def sealing(*args, **kwargs):
        result = original_seal(*args, **kwargs)
        sealed["on"] = True
        return result

    class _FailingUnlock:
        def __getattr__(self, name):
            return getattr(fcntl, name)

        @staticmethod
        def flock(descriptor, operation):
            if sealed["on"] and operation == fcntl.LOCK_UN:
                fcntl.flock(descriptor, operation)
                raise KeyboardInterrupt
            return fcntl.flock(descriptor, operation)

    monkeypatch.setattr(module, "_seal_registry_instance", sealing)
    monkeypatch.setattr(module, "fcntl", _FailingUnlock())
    with pytest.raises(KeyboardInterrupt):
        create(root)
    monkeypatch.setattr(module, "fcntl", fcntl)
    monkeypatch.setattr(module, "_seal_registry_instance", original_seal)
    assert sealed["on"]
    assert not os.path.lexists(root)
    assert _staged(root) == before

    created = create(root)
    try:
        values = retained(created)
        assert created.root == root
        assert root.is_dir() and (root / "registry-metadata.json").is_file()
    finally:
        created.close()
    reopen_at(root, values).close()


def check_interrupted_restore(
    registry,
    restore: Callable[[Path, bytes, SimpleNamespace], object],
    module: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Restore is staged: an interrupt leaves no target, and a retry works."""

    backup = registry.backup_bytes()
    values = retained(registry)
    target = tmp_path / f"staged-restore-{secrets.token_hex(4)}"
    # A stale staging directory from an earlier hard crash never blocks.
    stale = tmp_path / f".{target.name}.staging-{secrets.token_hex(16)}"
    stale.mkdir(mode=0o700)
    original_commit = module._commit_staging_directory

    def crash(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(module, "_commit_staging_directory", crash)
    with pytest.raises(KeyboardInterrupt):
        restore(target, backup, values)
    monkeypatch.setattr(module, "_commit_staging_directory", original_commit)
    assert not os.path.lexists(target)
    assert _staged(target) == [stale]

    # An interrupt after the rename but before the caller learns of it:
    # cleanup follows the published inode, so no empty target blocks a retry.
    def crash_after_rename(*args, **kwargs):
        original_commit(*args, **kwargs)
        raise KeyboardInterrupt

    monkeypatch.setattr(module, "_commit_staging_directory", crash_after_rename)
    with pytest.raises(KeyboardInterrupt):
        restore(target, backup, values)
    monkeypatch.setattr(module, "_commit_staging_directory", original_commit)
    assert not os.path.lexists(target)
    assert _staged(target) == [stale]

    restored = restore(target, backup, values)
    try:
        assert restored.root == target
        assert retained(restored).state_head_sha256 == values.state_head_sha256
    finally:
        restored.close()
    assert _staged(target) == [stale]


def check_creation_under_symlinked_parent(
    create: Callable[[Path], object],
    reopen_at: Callable[[Path, SimpleNamespace], object],
    tmp_path: Path,
) -> None:
    """A registry can be created and reopened beneath a symlinked parent."""

    real = tmp_path / f"real-parent-{secrets.token_hex(4)}"
    real.mkdir()
    link = tmp_path / f"linked-parent-{secrets.token_hex(4)}"
    link.symlink_to(real, target_is_directory=True)
    root = link / "registry"
    created = create(root)
    try:
        values = retained(created)
    finally:
        created.close()
    assert (real / "registry" / "registry-metadata.json").is_file()
    assert not list(real.glob(".registry.staging-*"))
    reopen_at(root, values).close()
