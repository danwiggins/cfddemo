"""Synthetic-only durable local stage runner.

The in-process callback boundary is a development harness, not a container,
zero-egress, hardware, genomic-data, or scientific qualification boundary.
"""

from __future__ import annotations

import hashlib
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Callable, Iterable, Mapping, Sequence

from .contracts import ArtifactDigest, JobRecord, JobRequest, JobState, StageName
from .receipts import (
    OutputCorrupt,
    ReceiptEnvelope,
    ReceiptCorrupt,
    build_receipt,
    hash_outputs,
    verify_receipt,
    write_receipt,
)
from .snapshots import SnapshotViolation, capture_snapshot
from .serialization import canonical_json_bytes
from .store import AttemptLease, JobStore, StoredJobRecord, StoreError


class RunnerError(RuntimeError):
    """Base class for orchestration failures."""


class SyntheticExecutionDisabled(RunnerError):
    """In-process synthetic callbacks were not explicitly enabled."""


class StageDefinitionError(RunnerError):
    """A stage is ambiguous or unsafe to reuse."""


class OrphanQuarantined(RunnerError):
    """Recovery moved an unverifiable or stale publication aside."""


class InjectedCrash(BaseException):
    """Test-only abrupt process boundary; deliberately bypasses failure handling."""


@dataclass(frozen=True)
class StageResult:
    outputs: Mapping[str, str]
    metadata: Mapping[str, bool | int | str] = field(default_factory=dict)
    postconditions: Mapping[str, bool] = field(default_factory=lambda: {"validated": True})

    def __post_init__(self) -> None:
        object.__setattr__(self, "outputs", MappingProxyType(dict(self.outputs)))
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))
        object.__setattr__(
            self, "postconditions", MappingProxyType(dict(self.postconditions))
        )


@dataclass(frozen=True)
class StageContext:
    job_id: str
    stage: StageName
    attempt: int
    lease_token: int
    sealed_input_dir: Path
    prior_stage_dirs: tuple[Path, ...]
    attempt_dir: Path
    heartbeat: Callable[[], None]


@dataclass(frozen=True)
class StageSpec:
    name: StageName | str
    version: str
    callback: Callable[[StageContext], StageResult]
    parameters: Mapping[str, bool | int | str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        try:
            object.__setattr__(self, "name", StageName(self.name))
        except ValueError as exc:
            raise StageDefinitionError(f"unknown shared stage name: {self.name!r}") from exc
        if not self.version.strip():
            raise StageDefinitionError("stage version is required")
        object.__setattr__(
            self, "parameters", MappingProxyType(dict(self.parameters))
        )

    @property
    def definition_sha256(self) -> str:
        return hashlib.sha256(
            canonical_json_bytes(
                {
                    "schema_version": "traceback.stage-definition.v1",
                    "name": self.name.value,
                    "version": self.version,
                    "parameters": dict(self.parameters),
                }
            )
        ).hexdigest()


@dataclass(frozen=True)
class RecoveryReport:
    adopted: tuple[str, ...]
    quarantined: tuple[str, ...]


class Runner:
    """Coordinate snapshots, fenced attempts, publication, and recovery."""

    def __init__(
        self,
        root: Path,
        *,
        clock: Callable[[], float] = time.time,
        lease_seconds: float = 30.0,
        synthetic_enabled: bool = False,
        fault_injector: Callable[[str], None] | None = None,
    ) -> None:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        self.root = root
        self.clock = clock
        self.lease_seconds = lease_seconds
        self.synthetic_enabled = synthetic_enabled
        self.fault_injector = fault_injector
        self.snapshots_dir = root / "snapshots"
        self.attempts_dir = root / "attempts"
        self.artifacts_dir = root / "artifacts"
        self.quarantine_dir = root / "quarantine"
        for directory in (
            self.snapshots_dir,
            self.attempts_dir,
            self.artifacts_dir,
            self.quarantine_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)
        self.store = JobStore(root / "runner.sqlite3", clock=clock)

    def _fault(self, point: str) -> None:
        if self.fault_injector is not None:
            self.fault_injector(point)

    def submit(
        self,
        request: JobRequest,
        source_root: Path,
        relative_files: Iterable[str],
        *,
        idempotency_key: str | None = None,
    ) -> JobRecord:
        """Atomically deduplicate a request and bind it to copied sealed bytes."""

        record = self.store.submit(request, idempotency_key)
        if record.state != JobState.DISCOVERED:
            return self._public_record(record)
        try:
            self.store.transition(record.job_id, JobState.SNAPSHOTTING, "capturing sealed input")
        except ValueError:
            return self.status(record.job_id)
        try:
            snapshot = capture_snapshot(
                source_root,
                relative_files,
                self.snapshots_dir,
                snapshot_id=record.job_id,
            )
            if snapshot.content_sha256 != request.input_tree_sha256_local:
                self._quarantine(snapshot.path, "snapshot-digest-mismatch")
                raise SnapshotViolation(
                    "captured input tree does not match JobRequest.input_tree_sha256_local"
                )
            self.store.attach_snapshot(
                record.job_id,
                snapshot.snapshot_id,
                snapshot.manifest_sha256,
                snapshot.summary(),
            )
            self.store.transition(record.job_id, JobState.VALIDATING, "snapshot sealed")
            self.store.transition(record.job_id, JobState.READY, "snapshot digest verified")
            return self.status(record.job_id)
        except SnapshotViolation as exc:
            self.store.transition(record.job_id, JobState.TERMINAL_FAILURE, str(exc))
            raise
        except Exception as exc:
            self.store.transition(record.job_id, JobState.RETRYABLE_FAILURE, str(exc))
            raise

    def status(self, job_id: str) -> JobRecord:
        return self._public_record(self.store.get(job_id))

    def snapshot(self, job_id: str) -> dict[str, object]:
        """Return a read-only value view with relative paths only."""

        return self.store.snapshot_summary(job_id)

    def outputs(
        self,
        job_id: str,
        stage: StageName | str,
        *,
        stage_definition_sha256: str | None = None,
    ) -> Mapping[str, Path]:
        """Return verified runner-owned output files for one committed stage.

        Paths are a local programmatic capability and must not be serialized to
        export records, logs, or UI fixtures. Callers should use receipt roles.
        """

        stage_name = StageName(stage)
        committed = self.store.committed_stage_definitions(job_id)
        definition = stage_definition_sha256 or committed.get(stage_name.value)
        if definition is None:
            raise KeyError(f"no committed stage: {stage_name.value}")
        directory, receipt = self._verified_publication(
            job_id, stage_name.value, definition
        )
        return {
            output.role: directory / receipt.output_locators[output.role]
            for output in receipt.contract.outputs
        }

    def request_pause(self, job_id: str) -> JobRecord:
        self.store.transition(job_id, JobState.PAUSE_REQUESTED, "operator requested pause")
        return self.status(job_id)

    def retry(self, job_id: str) -> JobRecord:
        self.store.transition(job_id, JobState.QUEUED, "operator requested retry")
        return self.status(job_id)

    def resume(
        self, job_id: str, stages: Sequence[StageSpec], *, worker_id: str
    ) -> JobRecord:
        record = self.status(job_id)
        if record.state == JobState.PAUSED:
            self.store.transition(job_id, JobState.QUEUED, "operator resumed job")
        elif record.state == JobState.RETRYABLE_FAILURE:
            self.retry(job_id)
        return self.execute(job_id, stages, worker_id=worker_id)

    def execute(
        self, job_id: str, stages: Sequence[StageSpec], *, worker_id: str
    ) -> JobRecord:
        if not self.synthetic_enabled:
            raise SyntheticExecutionDisabled(
                "synthetic in-process stages require synthetic_enabled=True; "
                "real-data execution is not enabled"
            )
        if not stages:
            raise StageDefinitionError("at least one stage is required")
        if len({stage.name for stage in stages}) != len(stages):
            raise StageDefinitionError("stage names must be unique within a workflow")

        self.recover(job_id)
        record = self.status(job_id)
        if record.state in {JobState.READY, JobState.QUEUED}:
            record = self.store.transition(job_id, JobState.RUNNING, "synthetic execution started")
        if record.state != JobState.RUNNING:
            raise RunnerError(f"job cannot execute from state {record.state.value}")

        committed = self.store.committed_stage_definitions(job_id)
        prior_dirs: list[Path] = []
        ordered_inputs = self._snapshot_input_digests(job_id)
        for stage in stages:
            existing = committed.get(stage.name)
            if existing == stage.definition_sha256:
                directory, receipt = self._verified_publication(job_id, stage.name, existing)
                prior_dirs.append(directory)
                ordered_inputs.extend(
                    ArtifactDigest(
                        role=f"{stage.name.value}.{output.role}",
                        sha256=output.sha256,
                        size_bytes=output.size_bytes,
                    )
                    for output in receipt.contract.outputs
                )
                continue

            lease = self.store.acquire_lease(
                job_id,
                stage.name.value,
                stage.definition_sha256,
                worker_id,
                self.lease_seconds,
            )
            attempt_dir = self.attempts_dir / job_id / f"{stage.name.value}-{lease.token}"
            attempt_dir.mkdir(parents=True, mode=0o700)
            current_lease = lease

            def heartbeat() -> None:
                nonlocal current_lease
                current_lease = self.store.heartbeat(current_lease, self.lease_seconds)

            context = StageContext(
                job_id=job_id,
                stage=stage.name,
                attempt=lease.attempt,
                lease_token=lease.token,
                sealed_input_dir=self.snapshots_dir / job_id,
                prior_stage_dirs=tuple(prior_dirs),
                attempt_dir=attempt_dir,
                heartbeat=heartbeat,
            )
            try:
                result = stage.callback(context)
                if not result.postconditions or not all(result.postconditions.values()):
                    raise OutputCorrupt("stage postconditions did not all pass")
                outputs = hash_outputs(attempt_dir, result.outputs)
                request = self.store.request(job_id)
                summary = self.snapshot(job_id)
                receipt = build_receipt(
                    job_id=job_id,
                    stage=stage.name,
                    attempt=lease.attempt,
                    lease_token=lease.token,
                    stage_definition_sha256=stage.definition_sha256,
                    workflow_release_sha256=request.workflow_release_sha256,
                    input_manifest_sha256=str(summary["manifest_sha256"]),
                    ordered_inputs=tuple(ordered_inputs),
                    outputs=outputs,
                    metadata=result.metadata,
                    postconditions=result.postconditions,
                )
                write_receipt(attempt_dir, receipt)
                self._seal_attempt(attempt_dir)
                self._fault("after_receipt")
                current_lease = self.store.heartbeat(
                    current_lease, self.lease_seconds
                )
                publication = self._publication_path(receipt)
                publication.parent.mkdir(parents=True, exist_ok=True)
                if publication.exists():
                    raise StoreError("publication identity already exists")
                os.replace(attempt_dir, publication)
                publication.chmod(0o555)
                self._fsync_directory(publication.parent)
                self._fault("after_publication")
                self.store.commit_attempt(
                    current_lease,
                    str(publication.relative_to(self.root)),
                    receipt.sha256,
                )
                self._fault("after_db_commit")
                prior_dirs.append(publication)
                ordered_inputs.extend(
                    ArtifactDigest(
                        role=f"{stage.name.value}.{output.role}",
                        sha256=output.sha256,
                        size_bytes=output.size_bytes,
                    )
                    for output in outputs
                )
            except InjectedCrash:
                raise
            except Exception as exc:
                self.store.fail_attempt(current_lease, str(exc), retryable=True)
                raise

            if self.status(job_id).state == JobState.PAUSE_REQUESTED:
                self.store.transition(job_id, JobState.PAUSED, "paused at stage boundary")
                return self.status(job_id)

        self.store.transition(job_id, JobState.VALIDATING_OUTPUT, "all stage receipts verified")
        self.store.transition(job_id, JobState.SIGNING, "synthetic completion receipt stage passed")
        self.store.transition(job_id, JobState.COMPLETE, "synthetic workflow complete")
        return self.status(job_id)

    def recover(self, job_id: str) -> RecoveryReport:
        """Adopt exactly-current publications and quarantine every other orphan."""

        adopted: list[str] = []
        quarantined: list[str] = []
        job_root = self.artifacts_dir / job_id
        if job_root.exists():
            for directory in sorted(path for path in job_root.rglob("*") if path.is_dir()):
                if not (directory / "receipt.json").exists():
                    continue
                try:
                    receipt = verify_receipt(directory)
                    lease = AttemptLease(
                        receipt.contract.job_id,
                        receipt.contract.stage.value,
                        receipt.contract.attempt,
                        receipt.contract.fencing_token,
                        "recovery",
                        0,
                    )
                    request = self.store.request(job_id)
                    workflow_matches = (
                        receipt.contract.workflow_release_sha256
                        == request.workflow_release_sha256
                    )
                    if (
                        receipt.contract.job_id != job_id
                        or not workflow_matches
                        or not self.store.adopt_published_attempt(
                            lease,
                            str(directory.relative_to(self.root)),
                            receipt.sha256,
                            receipt.stage_definition_sha256,
                            receipt.input_manifest_sha256,
                        )
                    ):
                        quarantined.append(self._quarantine(directory, "stale-publication"))
                    else:
                        adopted.append(receipt.contract.stage.value)
                except (ReceiptCorrupt, OutputCorrupt) as exc:
                    quarantined.append(self._quarantine(directory, "corrupt-publication"))
                    raise OrphanQuarantined(str(exc)) from exc

        private_root = self.attempts_dir / job_id
        if private_root.exists():
            for directory in sorted(path for path in private_root.iterdir() if path.is_dir()):
                quarantined.append(self._quarantine(directory, "private-orphan"))
        return RecoveryReport(tuple(adopted), tuple(quarantined))

    def backup(self, destination: Path) -> Path:
        return self.store.backup(destination)

    def _snapshot_input_digests(self, job_id: str) -> list[ArtifactDigest]:
        files = self.snapshot(job_id)["files"]
        assert isinstance(files, list)
        return [
            ArtifactDigest(
                role=f"snapshot.{index:04d}",
                sha256=str(item["sha256_local"]),
                size_bytes=int(item["size_bytes"]),
            )
            for index, item in enumerate(files)
        ]

    def _publication_path(self, receipt: ReceiptEnvelope) -> Path:
        return (
            self.artifacts_dir
            / receipt.contract.job_id
            / receipt.contract.stage.value
            / (
                f"{receipt.stage_definition_sha256[:16]}-"
                f"a{receipt.contract.attempt}-t{receipt.contract.fencing_token}"
            )
        )

    def _verified_publication(
        self, job_id: str, stage: str, definition_sha256: str
    ) -> tuple[Path, ReceiptEnvelope]:
        root = self.artifacts_dir / job_id / stage
        candidates: list[tuple[Path, ReceiptEnvelope]] = []
        if root.exists():
            for path in root.iterdir():
                if path.is_dir():
                    receipt = verify_receipt(path)
                    if receipt.stage_definition_sha256 == definition_sha256:
                        candidates.append((path, receipt))
        if len(candidates) != 1:
            raise ReceiptCorrupt(
                f"expected exactly one verified publication for {stage}, found {len(candidates)}"
            )
        return candidates[0]

    def _public_record(self, record: StoredJobRecord) -> JobRecord:
        request = self.store.request(record.job_id)
        active_stage = StageName(record.current_stage) if record.current_stage else None
        return JobRecord(
            job_id=record.job_id,
            job_key=record.request_key,
            state=record.state,
            workflow_release_sha256=request.workflow_release_sha256,
            input_snapshot_sha256=record.snapshot_manifest_sha256,
            active_stage=active_stage,
            lease_fencing_token=(record.lease_token if record.lease_owner else None),
        )

    def _quarantine(self, path: Path, reason: str) -> str:
        destination = self.quarantine_dir / f"{path.name}-{reason}-{time.time_ns()}"
        destination.parent.mkdir(parents=True, exist_ok=True)
        # POSIX rename is governed by parent permissions, while some local
        # filesystems also reject moving a non-writable directory.  Only the
        # directory mode changes; receipt and output bytes remain untouched.
        path.chmod(0o700)
        os.replace(path, destination)
        destination.chmod(0o555)
        self._fsync_directory(destination.parent)
        return destination.name

    @staticmethod
    def _seal_attempt(directory: Path) -> None:
        for path in sorted(directory.rglob("*"), reverse=True):
            path.chmod(0o555 if path.is_dir() else 0o444)
        # Keep the unpublished root writable so it can be atomically renamed.
        # It becomes read-only immediately after publication.

    @staticmethod
    def _fsync_directory(directory: Path) -> None:
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
