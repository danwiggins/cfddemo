"""Transactional SQLite state for the local runner."""

from __future__ import annotations

import json
import os
import sqlite3
import stat
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .contracts import JobRequest, JobState, job_key, validate_transition
from .serialization import canonical_json_bytes

SCHEMA_VERSION = 1


class StoreError(RuntimeError):
    """Base class for durable runner state errors."""


class UnsupportedSchema(StoreError):
    """The database needs a migration this runner does not implement."""


class IdempotencyConflict(StoreError):
    """An idempotency key was reused for a different request."""


class LeaseBusy(StoreError):
    """Another non-expired worker owns the job."""


class StaleLease(StoreError):
    """A worker tried to mutate state with an expired or superseded fence."""


@dataclass(frozen=True)
class StoredJobRecord:
    job_id: str
    request_key: str
    state: JobState
    snapshot_id: str | None
    snapshot_manifest_sha256: str | None
    current_stage: str | None
    lease_token: int
    lease_owner: str | None
    lease_expires_at: float | None
    created_at: float
    updated_at: float
    last_error: str | None


@dataclass(frozen=True)
class StoredJobProjectionSnapshot:
    record: StoredJobRecord
    revision: int


@dataclass(frozen=True)
class AttemptLease:
    job_id: str
    stage: str
    attempt: int
    token: int
    worker_id: str
    expires_at: float


class JobStore:
    """Small connection-per-operation SQLite store with monotonic fencing."""

    def __init__(self, path: Path, *, clock: Callable[[], float] = time.time) -> None:
        self.path = path
        self.clock = clock
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        parent = path.parent.lstat()
        if not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.geteuid():
            raise StoreError("runner store directory must be owned by this OS user")
        os.chmod(path.parent, 0o700)
        self._directory_identity = (parent.st_dev, parent.st_ino)
        self._database_identity: tuple[int, int] | None = None
        self._sidecar_identities: dict[Path, tuple[int, int]] = {}
        # The runner's lease keeper thread uses this store concurrently with
        # the worker thread; the pinned-identity bookkeeping is not atomic.
        self._storage_lock = threading.RLock()
        self._secure_storage()
        self._initialize()
        self._secure_storage()
        database = self.path.lstat()
        self._database_identity = (database.st_dev, database.st_ino)

    def _secure_storage(self) -> None:
        with self._storage_lock:
            self._secure_storage_locked()

    def _secure_storage_locked(self) -> None:
        parent = self.path.parent.lstat()
        if (
            not stat.S_ISDIR(parent.st_mode)
            or parent.st_uid != os.geteuid()
            or (parent.st_dev, parent.st_ino) != self._directory_identity
            or stat.S_IMODE(parent.st_mode) != 0o700
        ):
            raise StoreError("runner store directory identity changed")
        for candidate in (
            self.path,
            Path(f"{self.path}-wal"),
            Path(f"{self.path}-shm"),
        ):
            try:
                metadata = candidate.lstat()
            except FileNotFoundError:
                continue
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.geteuid()
                or metadata.st_nlink != 1
            ):
                raise StoreError("runner store files must be private regular files")
            identity = (metadata.st_dev, metadata.st_ino)
            if candidate == self.path:
                if self._database_identity is None:
                    os.chmod(candidate, 0o600)
                elif (
                    identity != self._database_identity
                    or stat.S_IMODE(metadata.st_mode) != 0o600
                ):
                    raise StoreError("runner store database identity changed")
            else:
                previous = self._sidecar_identities.get(candidate)
                if previous is None:
                    os.chmod(candidate, 0o600)
                    self._sidecar_identities[candidate] = identity
                elif previous != identity or stat.S_IMODE(metadata.st_mode) != 0o600:
                    raise StoreError("runner store sidecar identity changed")
        for candidate in tuple(self._sidecar_identities):
            if not candidate.exists():
                self._sidecar_identities.pop(candidate, None)
        if self._database_identity is not None:
            database = self.path.lstat()
            if (database.st_dev, database.st_ino) != self._database_identity:
                raise StoreError("runner store database identity changed")

    def _connect(self) -> sqlite3.Connection:
        self._secure_storage()
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        self._secure_storage()
        return connection

    @contextmanager
    def journal_anchor(self) -> Iterator[None]:
        """Hold one read connection so the WAL and SHM sidecars stay in place.

        SQLite deletes ``-wal``/``-shm`` when the last connection closes.  If
        another store instance (for example ``traceback pause`` in a second
        process) opens and closes the database between two of this instance's
        calls, the sidecars come back with new inodes and the pinned-identity
        check above refuses them.  While this anchor is open, no other close is
        the last one, so the sidecars and their identities persist.
        """

        connection = self._connect()
        try:
            connection.execute("SELECT count(*) FROM jobs").fetchone()
            yield
        finally:
            connection.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        new = not self.path.exists()
        with self._connect() as connection:
            if new:
                connection.executescript(
                    """
                    PRAGMA journal_mode=WAL;
                    PRAGMA synchronous=FULL;
                    CREATE TABLE metadata (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    );
                    INSERT INTO metadata(key, value) VALUES ('schema_version', '1');
                    CREATE TABLE jobs (
                        job_id TEXT PRIMARY KEY,
                        idempotency_key TEXT NOT NULL UNIQUE,
                        request_key TEXT NOT NULL UNIQUE,
                        request_json BLOB NOT NULL,
                        state TEXT NOT NULL,
                        snapshot_id TEXT,
                        snapshot_manifest_sha256 TEXT,
                        snapshot_summary_json BLOB,
                        current_stage TEXT,
                        lease_token INTEGER NOT NULL DEFAULT 0,
                        lease_owner TEXT,
                        lease_expires_at REAL,
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL,
                        last_error TEXT
                    );
                    CREATE TABLE audit (
                        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                        job_id TEXT NOT NULL REFERENCES jobs(job_id),
                        occurred_at REAL NOT NULL,
                        previous_state TEXT,
                        next_state TEXT NOT NULL,
                        reason TEXT NOT NULL,
                        lease_token INTEGER NOT NULL
                    );
                    CREATE TABLE attempts (
                        job_id TEXT NOT NULL REFERENCES jobs(job_id),
                        stage TEXT NOT NULL,
                        attempt INTEGER NOT NULL,
                        lease_token INTEGER NOT NULL,
                        stage_definition_sha256 TEXT NOT NULL,
                        worker_id TEXT NOT NULL,
                        status TEXT NOT NULL,
                        started_at REAL NOT NULL,
                        receipt_path TEXT,
                        receipt_sha256 TEXT,
                        PRIMARY KEY(job_id, stage, attempt),
                        UNIQUE(job_id, lease_token)
                    );
                    """
                )
            else:
                try:
                    row = connection.execute(
                        "SELECT value FROM metadata WHERE key='schema_version'"
                    ).fetchone()
                except sqlite3.DatabaseError as exc:
                    raise UnsupportedSchema(
                        "database has no recognized schema"
                    ) from exc
                if row is None or int(row[0]) != SCHEMA_VERSION:
                    found = "missing" if row is None else row[0]
                    raise UnsupportedSchema(
                        f"database schema {found!r} is unsupported; expected {SCHEMA_VERSION}"
                    )
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("PRAGMA synchronous=FULL")

    @staticmethod
    def _record(row: sqlite3.Row) -> StoredJobRecord:
        return StoredJobRecord(
            job_id=row["job_id"],
            request_key=row["request_key"],
            state=JobState(row["state"]),
            snapshot_id=row["snapshot_id"],
            snapshot_manifest_sha256=row["snapshot_manifest_sha256"],
            current_stage=row["current_stage"],
            lease_token=row["lease_token"],
            lease_owner=row["lease_owner"],
            lease_expires_at=row["lease_expires_at"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            last_error=row["last_error"],
        )

    def submit(
        self, request: JobRequest, idempotency_key: str | None = None
    ) -> StoredJobRecord:
        request_identity = job_key(request)
        key = idempotency_key or request_identity
        now = self.clock()
        with self._transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM jobs WHERE idempotency_key=? OR request_key=?",
                (key, request_identity),
            ).fetchone()
            if existing is not None:
                if existing["request_key"] != request_identity:
                    raise IdempotencyConflict(
                        "idempotency key belongs to another request"
                    )
                return self._record(existing)
            job_id = uuid.uuid4().hex
            connection.execute(
                """INSERT INTO jobs(
                    job_id, idempotency_key, request_key, request_json, state,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    job_id,
                    key,
                    request_identity,
                    canonical_json_bytes(request),
                    JobState.DISCOVERED.value,
                    now,
                    now,
                ),
            )
            connection.execute(
                """INSERT INTO audit(
                    job_id, occurred_at, previous_state, next_state, reason, lease_token
                ) VALUES (?, ?, NULL, ?, ?, 0)""",
                (job_id, now, JobState.DISCOVERED.value, "submitted"),
            )
            row = connection.execute(
                "SELECT * FROM jobs WHERE job_id=?", (job_id,)
            ).fetchone()
            assert row is not None
            return self._record(row)

    def get(self, job_id: str) -> StoredJobRecord:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM jobs WHERE job_id=?", (job_id,)
            ).fetchone()
        if row is None:
            raise KeyError(job_id)
        return self._record(row)

    def list_jobs(self, *, limit: int = 100) -> tuple[StoredJobRecord, ...]:
        """Return a bounded authoritative queue snapshot for local projections."""

        if not 1 <= limit <= 1000:
            raise ValueError("job list limit must be between 1 and 1000")
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM jobs ORDER BY updated_at DESC, job_id LIMIT ?",
                (limit,),
            ).fetchall()
        return tuple(self._record(row) for row in rows)

    @classmethod
    def _projection_snapshot(cls, row: sqlite3.Row) -> StoredJobProjectionSnapshot:
        return StoredJobProjectionSnapshot(
            record=cls._record(row),
            revision=int(row["projection_revision"]),
        )

    def get_projection_snapshot(self, job_id: str) -> StoredJobProjectionSnapshot:
        """Read state and audit revision in one SQLite statement."""

        with self._connect() as connection:
            row = connection.execute(
                """SELECT jobs.*,
                          COALESCE((SELECT MAX(sequence) FROM audit
                                    WHERE audit.job_id=jobs.job_id), 0)
                              AS projection_revision
                   FROM jobs WHERE jobs.job_id=?""",
                (job_id,),
            ).fetchone()
        if row is None:
            raise KeyError(job_id)
        return self._projection_snapshot(row)

    def list_projection_snapshots(
        self, *, limit: int = 100
    ) -> tuple[StoredJobProjectionSnapshot, ...]:
        """Read a bounded queue whose state and revision share one snapshot."""

        if not 1 <= limit <= 100:
            raise ValueError("projection limit must be between 1 and 100")
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT jobs.*,
                          COALESCE((SELECT MAX(sequence) FROM audit
                                    WHERE audit.job_id=jobs.job_id), 0)
                              AS projection_revision
                   FROM jobs ORDER BY updated_at DESC, job_id LIMIT ?""",
                (limit,),
            ).fetchall()
        return tuple(self._projection_snapshot(row) for row in rows)

    def request(self, job_id: str) -> JobRequest:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT request_json FROM jobs WHERE job_id=?", (job_id,)
            ).fetchone()
        if row is None:
            raise KeyError(job_id)
        return JobRequest.model_validate_json(row[0])

    def transition(self, job_id: str, state: JobState, reason: str) -> StoredJobRecord:
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM jobs WHERE job_id=?", (job_id,)
            ).fetchone()
            if row is None:
                raise KeyError(job_id)
            previous = JobState(row["state"])
            validate_transition(previous, state)
            now = self.clock()
            connection.execute(
                "UPDATE jobs SET state=?, updated_at=? WHERE job_id=?",
                (state.value, now, job_id),
            )
            connection.execute(
                """INSERT INTO audit(
                    job_id, occurred_at, previous_state, next_state, reason, lease_token
                ) VALUES (?, ?, ?, ?, ?, ?)""",
                (job_id, now, previous.value, state.value, reason, row["lease_token"]),
            )
            updated = connection.execute(
                "SELECT * FROM jobs WHERE job_id=?", (job_id,)
            ).fetchone()
            assert updated is not None
            return self._record(updated)

    def attach_snapshot(
        self,
        job_id: str,
        snapshot_id: str,
        manifest_sha256: str,
        summary: dict[str, object],
    ) -> None:
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT snapshot_id FROM jobs WHERE job_id=?", (job_id,)
            ).fetchone()
            if row is None:
                raise KeyError(job_id)
            if row["snapshot_id"] not in (None, snapshot_id):
                raise StoreError("job already has a different immutable snapshot")
            connection.execute(
                """UPDATE jobs SET snapshot_id=?, snapshot_manifest_sha256=?,
                    snapshot_summary_json=?, updated_at=? WHERE job_id=?""",
                (
                    snapshot_id,
                    manifest_sha256,
                    canonical_json_bytes(summary),
                    self.clock(),
                    job_id,
                ),
            )

    def snapshot_summary(self, job_id: str) -> dict[str, object]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT snapshot_summary_json FROM jobs WHERE job_id=?", (job_id,)
            ).fetchone()
        if row is None:
            raise KeyError(job_id)
        if row[0] is None:
            raise StoreError("job has no snapshot")
        return json.loads(row[0])

    def acquire_lease(
        self,
        job_id: str,
        stage: str,
        stage_definition_sha256: str,
        worker_id: str,
        lease_seconds: float,
    ) -> AttemptLease:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM jobs WHERE job_id=?", (job_id,)
            ).fetchone()
            if row is None:
                raise KeyError(job_id)
            now = self.clock()
            if row["lease_owner"] is not None and row["lease_expires_at"] > now:
                raise LeaseBusy(f"job lease is held by {row['lease_owner']}")
            token = row["lease_token"] + 1
            attempt_row = connection.execute(
                "SELECT COALESCE(MAX(attempt), 0) + 1 FROM attempts WHERE job_id=? AND stage=?",
                (job_id, stage),
            ).fetchone()
            attempt = int(attempt_row[0])
            expires = now + lease_seconds
            connection.execute(
                """UPDATE jobs SET current_stage=?, lease_token=?, lease_owner=?,
                    lease_expires_at=?, updated_at=? WHERE job_id=?""",
                (stage, token, worker_id, expires, now, job_id),
            )
            connection.execute(
                """INSERT INTO attempts(
                    job_id, stage, attempt, lease_token, stage_definition_sha256,
                    worker_id, status, started_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'running', ?)""",
                (
                    job_id,
                    stage,
                    attempt,
                    token,
                    stage_definition_sha256,
                    worker_id,
                    now,
                ),
            )
            return AttemptLease(job_id, stage, attempt, token, worker_id, expires)

    def heartbeat(self, lease: AttemptLease, lease_seconds: float) -> AttemptLease:
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM jobs WHERE job_id=?", (lease.job_id,)
            ).fetchone()
            now = self.clock()
            if row is None or not self._lease_matches(row, lease, now):
                raise StaleLease("lease expired or was superseded")
            expires = now + lease_seconds
            connection.execute(
                "UPDATE jobs SET lease_expires_at=?, updated_at=? WHERE job_id=?",
                (expires, now, lease.job_id),
            )
            return AttemptLease(
                lease.job_id,
                lease.stage,
                lease.attempt,
                lease.token,
                lease.worker_id,
                expires,
            )

    @staticmethod
    def _lease_matches(row: sqlite3.Row, lease: AttemptLease, now: float) -> bool:
        return (
            row["lease_token"] == lease.token
            and row["lease_owner"] == lease.worker_id
            and row["current_stage"] == lease.stage
            and row["lease_expires_at"] is not None
            and row["lease_expires_at"] >= now
        )

    def publish_attempt(self, lease: AttemptLease, publish: Callable[[], None]) -> None:
        """Run ``publish`` only while ``lease`` is current, fenced against takeover.

        The check and ``publish`` run inside one ``BEGIN IMMEDIATE`` write
        transaction; ``acquire_lease`` needs the same write lock, so no other
        worker can take the job over between the fence check and the rename.
        Keep ``publish`` short (a rename, chmod and fsync).
        """

        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM jobs WHERE job_id=?", (lease.job_id,)
            ).fetchone()
            if row is None or not self._lease_matches(row, lease, self.clock()):
                raise StaleLease("stale worker cannot publish a stage")
            publish()

    def commit_attempt(
        self, lease: AttemptLease, receipt_path: str, receipt_sha256: str
    ) -> None:
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM jobs WHERE job_id=?", (lease.job_id,)
            ).fetchone()
            now = self.clock()
            if row is None or not self._lease_matches(row, lease, now):
                raise StaleLease("stale worker cannot commit a stage")
            attempt = connection.execute(
                """SELECT status, receipt_sha256 FROM attempts
                   WHERE job_id=? AND stage=? AND attempt=? AND lease_token=?""",
                (lease.job_id, lease.stage, lease.attempt, lease.token),
            ).fetchone()
            if attempt is None:
                raise StaleLease("attempt does not own this fence")
            if attempt["status"] == "committed":
                if attempt["receipt_sha256"] != receipt_sha256:
                    raise StoreError("committed attempt receipt cannot be replaced")
                return
            connection.execute(
                """UPDATE attempts SET status='committed', receipt_path=?, receipt_sha256=?
                   WHERE job_id=? AND stage=? AND attempt=?""",
                (
                    receipt_path,
                    receipt_sha256,
                    lease.job_id,
                    lease.stage,
                    lease.attempt,
                ),
            )
            connection.execute(
                """UPDATE jobs SET lease_owner=NULL, lease_expires_at=NULL, updated_at=?
                   WHERE job_id=?""",
                (now, lease.job_id),
            )

    def adopt_published_attempt(
        self,
        lease: AttemptLease,
        receipt_path: str,
        receipt_sha256: str,
        stage_definition_sha256: str,
        input_manifest_sha256: str,
    ) -> bool:
        """Adopt publication after a DB-boundary crash, even if its lease expired."""

        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM jobs WHERE job_id=?", (lease.job_id,)
            ).fetchone()
            if row is None:
                return False
            attempt = connection.execute(
                """SELECT status, receipt_sha256, stage_definition_sha256 FROM attempts
                   WHERE job_id=? AND stage=? AND attempt=? AND lease_token=?""",
                (lease.job_id, lease.stage, lease.attempt, lease.token),
            ).fetchone()
            if attempt is None:
                return False
            if (
                attempt["stage_definition_sha256"] != stage_definition_sha256
                or row["snapshot_manifest_sha256"] != input_manifest_sha256
            ):
                return False
            if attempt["status"] == "committed":
                return attempt["receipt_sha256"] == receipt_sha256
            if row["lease_token"] != lease.token:
                return False
            if (
                row["lease_owner"] is not None
                and row["lease_expires_at"] >= self.clock()
            ):
                raise LeaseBusy("publication still belongs to a live worker")
            connection.execute(
                """UPDATE attempts SET status='committed', receipt_path=?, receipt_sha256=?
                   WHERE job_id=? AND stage=? AND attempt=?""",
                (
                    receipt_path,
                    receipt_sha256,
                    lease.job_id,
                    lease.stage,
                    lease.attempt,
                ),
            )
            connection.execute(
                """UPDATE jobs SET lease_owner=NULL, lease_expires_at=NULL, updated_at=?
                   WHERE job_id=?""",
                (self.clock(), lease.job_id),
            )
            return True

    def committed_stages(self, job_id: str) -> set[str]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT DISTINCT stage FROM attempts WHERE job_id=? AND status='committed'",
                (job_id,),
            ).fetchall()
        return {row[0] for row in rows}

    def committed_stage_definitions(self, job_id: str) -> dict[str, str]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT stage, stage_definition_sha256 FROM attempts
                   WHERE job_id=? AND status='committed' ORDER BY attempt DESC""",
                (job_id,),
            ).fetchall()
        result: dict[str, str] = {}
        for row in rows:
            result.setdefault(row["stage"], row["stage_definition_sha256"])
        return result

    @contextmanager
    def private_recovery_guard(self, job_id: str) -> Iterator[str | None]:
        """Classify and fence private attempts while excluding new lease grants.

        The caller must perform its directory scan/renames inside this guard.
        A recovery crash can roll back the fence, but an expired worker still
        cannot heartbeat/commit; a repeated recovery is safe.
        """

        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM jobs WHERE job_id=?", (job_id,)
            ).fetchone()
            if row is None:
                raise KeyError(job_id)
            active = None
            now = self.clock()
            if row["lease_owner"] is not None:
                if (
                    row["lease_expires_at"] is not None
                    and row["lease_expires_at"] >= now
                ):
                    active = f"{row['current_stage']}-{row['lease_token']}"
                else:
                    connection.execute(
                        """UPDATE jobs SET lease_token=lease_token+1, lease_owner=NULL,
                           lease_expires_at=NULL, updated_at=? WHERE job_id=?""",
                        (now, job_id),
                    )
                    connection.execute(
                        """UPDATE attempts SET status='abandoned'
                           WHERE job_id=? AND lease_token=? AND status='running'""",
                        (job_id, row["lease_token"]),
                    )
            yield active

    def fail_attempt(
        self, lease: AttemptLease, message: str, *, retryable: bool
    ) -> None:
        target = JobState.RETRYABLE_FAILURE if retryable else JobState.TERMINAL_FAILURE
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM jobs WHERE job_id=?", (lease.job_id,)
            ).fetchone()
            now = self.clock()
            # Same fence as heartbeat and commit: an expired or superseded
            # worker has no authority to decide the job's failure state.
            if row is None or not self._lease_matches(row, lease, now):
                raise StaleLease("stale worker cannot record failure")
            previous = JobState(row["state"])
            validate_transition(previous, target)
            connection.execute(
                """UPDATE attempts SET status='failed'
                   WHERE job_id=? AND stage=? AND attempt=?""",
                (lease.job_id, lease.stage, lease.attempt),
            )
            connection.execute(
                """UPDATE jobs SET state=?, lease_owner=NULL, lease_expires_at=NULL,
                    last_error=?, updated_at=? WHERE job_id=?""",
                (target.value, message, now, lease.job_id),
            )
            connection.execute(
                """INSERT INTO audit(
                    job_id, occurred_at, previous_state, next_state, reason, lease_token
                ) VALUES (?, ?, ?, ?, ?, ?)""",
                (lease.job_id, now, previous.value, target.value, message, lease.token),
            )

    def backup(self, destination: Path) -> Path:
        """Create a consistent SQLite backup; never copy live WAL files directly."""

        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            raise FileExistsError(destination)
        source = self._connect()
        target = sqlite3.connect(destination)
        try:
            source.backup(target)
            target.execute("PRAGMA integrity_check").fetchone()
        except BaseException:
            target.close()
            source.close()
            if destination.exists():
                destination.unlink()
            raise
        target.close()
        source.close()
        os.chmod(destination, 0o600)
        return destination

    def audit(self, job_id: str) -> tuple[dict[str, object], ...]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT sequence, occurred_at, previous_state, next_state, reason, lease_token
                   FROM audit WHERE job_id=? ORDER BY sequence""",
                (job_id,),
            ).fetchall()
        return tuple(dict(row) for row in rows)
