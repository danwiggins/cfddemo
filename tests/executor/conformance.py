"""Runnable synthetic-only executor comparison harness.

This module compares contract fixtures.  It does not launch EPI2ME, Podman,
Nextflow, Docker, Dorado, or any real executor, and cannot produce qualification
evidence.  Run it with ``python -m tests.executor.conformance``.
"""

from __future__ import annotations

import argparse
import json
import platform
import shutil
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Literal, Sequence

from traceback_runner.contracts import InputKind, JobRequest, JobState
from traceback_runner.runner import InjectedCrash, Runner, StageResult, StageSpec
from traceback_runner.serialization import canonical_json_bytes, sha256_bytes
from traceback_runner.snapshots import SnapshotViolation, input_tree_sha256


class Candidate(StrEnum):
    VENDOR_WRAPPER = "vendor_workflow_verifier_wrapper"
    DIRECT_OCI = "direct_oci"
    PINNED_WORKFLOW = "pinned_workflow_adapter"


class EvidenceKind(StrEnum):
    OBSERVED = "observed"
    DOCUMENTED = "documented"
    UNTESTED = "untested"


class RuntimeState(StrEnum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    UNTESTED = "untested"


@dataclass(frozen=True)
class MountFixture:
    source_role: str
    target: str
    read_only: bool

    def __post_init__(self) -> None:
        target = PurePosixPath(self.target)
        if not target.is_absolute() or ".." in target.parts:
            raise ValueError("fixture mount target must be an absolute safe POSIX path")


@dataclass(frozen=True)
class ResourceFixture:
    cpu_count: int
    memory_bytes: int
    disk_bytes: int
    gpu_count: int

    def __post_init__(self) -> None:
        if min(self.cpu_count, self.memory_bytes, self.disk_bytes) <= 0:
            raise ValueError("fixture CPU, memory, and disk limits must be positive")
        if self.gpu_count < 0:
            raise ValueError("fixture GPU count cannot be negative")


@dataclass(frozen=True)
class ExecutorRequestFixture:
    """Comparison input only; never a production execution authority."""

    candidate: Candidate
    workflow_sha256: str
    stage_sha256: str
    snapshot_sha256: str
    argv: tuple[str, ...]
    mounts: tuple[MountFixture, ...]
    resources: ResourceFixture
    timeout_seconds: int
    network: Literal["none"] = "none"
    synthetic_only: Literal[True] = True
    real_data_authorized: Literal[False] = False

    def __post_init__(self) -> None:
        for name in ("workflow_sha256", "stage_sha256", "snapshot_sha256"):
            value = getattr(self, name)
            if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
                raise ValueError(f"{name} must be a lowercase SHA-256 digest")
        if not self.argv or any(not item or "\x00" in item for item in self.argv):
            raise ValueError("fixture argv must contain non-empty NUL-free arguments")
        if self.timeout_seconds <= 0:
            raise ValueError("fixture timeout must be positive")
        writable = [mount for mount in self.mounts if not mount.read_only]
        if len(writable) != 1 or writable[0].source_role != "attempt":
            raise ValueError("fixture requires exactly one writable attempt mount")
        if len({mount.target for mount in self.mounts}) != len(self.mounts):
            raise ValueError("fixture mount targets must be unique")


@dataclass(frozen=True)
class RuntimeObservation:
    candidate: Candidate
    state: RuntimeState
    evidence: EvidenceKind
    reason: str
    commands: tuple[str, ...]


@dataclass(frozen=True)
class TraceResult:
    candidate: Candidate
    scenario: str
    passed: bool
    evidence: EvidenceKind
    executor_exercised: bool
    qualified: bool
    detail: str


@dataclass(frozen=True)
class ComparisonReport:
    schema_version: str
    synthetic_only: bool
    real_data_authorized: bool
    host_system: str
    host_machine: str
    runtime_observations: tuple[RuntimeObservation, ...]
    traces: tuple[TraceResult, ...]

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(asdict(self))


def _command_version(command: str, arguments: Sequence[str]) -> tuple[bool, str]:
    executable = shutil.which(command)
    if executable is None:
        return False, f"{command} is not installed"
    try:
        completed = subprocess.run(
            [executable, *arguments],
            check=False,
            capture_output=True,
            text=True,
            timeout=3,
            env={"PATH": str(Path(executable).parent)},
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"{command} probe failed: {type(exc).__name__}"
    output = (completed.stdout or completed.stderr).strip().splitlines()
    detail = output[0] if output else f"exit {completed.returncode}"
    return completed.returncode == 0, detail[:160]


def inventory_runtimes() -> tuple[RuntimeObservation, ...]:
    system = platform.system()
    epi2me_ok, epi2me_detail = _command_version("epi2me", ("--version",))
    docker_ok, docker_detail = _command_version("docker", ("info", "--format", "{{.ServerVersion}}"))
    podman_ok, podman_detail = _command_version("podman", ("--version",))
    nextflow_ok, nextflow_detail = _command_version("nextflow", ("-version",))
    java_ok, java_detail = _command_version("java", ("-version",))

    vendor_ok = epi2me_ok and docker_ok
    direct_ok = system == "Linux" and podman_ok
    workflow_ok = system == "Linux" and podman_ok and nextflow_ok and java_ok
    return (
        RuntimeObservation(
            candidate=Candidate.VENDOR_WRAPPER,
            state=RuntimeState.AVAILABLE if vendor_ok else RuntimeState.UNAVAILABLE,
            evidence=EvidenceKind.OBSERVED,
            reason=(
                "EPI2ME automation entrypoint and Docker daemon responded"
                if vendor_ok
                else f"EPI2ME={epi2me_detail}; Docker={docker_detail}"
            ),
            commands=("epi2me", "docker"),
        ),
        RuntimeObservation(
            candidate=Candidate.DIRECT_OCI,
            state=RuntimeState.AVAILABLE if direct_ok else RuntimeState.UNAVAILABLE,
            evidence=EvidenceKind.OBSERVED,
            reason=(
                f"Linux rootless-runtime probe available: {podman_detail}"
                if direct_ok
                else f"requires Linux and Podman; host={system}; Podman={podman_detail}"
            ),
            commands=("podman",),
        ),
        RuntimeObservation(
            candidate=Candidate.PINNED_WORKFLOW,
            state=RuntimeState.AVAILABLE if workflow_ok else RuntimeState.UNAVAILABLE,
            evidence=EvidenceKind.OBSERVED,
            reason=(
                "Linux, Podman, Nextflow, and Java probes responded"
                if workflow_ok
                else (
                    f"requires Linux, Podman, Nextflow, and Java; host={system}; "
                    f"Podman={podman_detail}; Nextflow={nextflow_detail}; Java={java_detail}"
                )
            ),
            commands=("podman", "nextflow", "java"),
        ),
    )


def request_fixture(candidate: Candidate, snapshot_sha256: str) -> ExecutorRequestFixture:
    return ExecutorRequestFixture(
        candidate=candidate,
        workflow_sha256=sha256_bytes(b"traceback.synthetic.workflow.v1"),
        stage_sha256=sha256_bytes(f"traceback.synthetic.{candidate}.v1".encode()),
        snapshot_sha256=snapshot_sha256,
        argv=(
            "synthetic-stage",
            "--input",
            "/input/payload",
            "--output",
            "/attempt/result.json",
        ),
        mounts=(
            MountFixture("snapshot", "/input", True),
            MountFixture("attempt", "/attempt", False),
        ),
        resources=ResourceFixture(
            cpu_count=1,
            memory_bytes=64 * 1024 * 1024,
            disk_bytes=64 * 1024 * 1024,
            gpu_count=0,
        ),
        timeout_seconds=30,
    )


def _job_request(source: Path) -> JobRequest:
    return JobRequest(
        sample_token="executor.synthetic",
        input_kind=InputKind.MODBAM,
        input_tree_sha256_local=input_tree_sha256(source, ("payload",)),
        workflow_release_sha256=sha256_bytes(b"traceback.synthetic.workflow.v1"),
        execution_options={"offline": True, "threads": 1},
    )


def _stage(candidate: Candidate, calls: list[str]) -> StageSpec:
    def callback(context: object) -> StageResult:
        calls.append(candidate.value)
        payload = (context.sealed_input_dir / "payload").read_bytes()  # type: ignore[attr-defined]
        fixture = request_fixture(candidate, sha256_bytes(payload))
        result = {
            "candidate": candidate.value,
            "input_sha256": sha256_bytes(payload),
            "request_sha256": sha256_bytes(canonical_json_bytes(asdict(fixture))),
            "synthetic_only": True,
        }
        (context.attempt_dir / "result.json").write_bytes(  # type: ignore[attr-defined]
            canonical_json_bytes(result)
        )
        return StageResult(
            outputs={"result": "result.json"},
            metadata={"candidate": candidate.value, "synthetic_only": True},
            postconditions={"canonical_result": True, "synthetic_only": True},
        )

    return StageSpec(
        "validate",
        f"{candidate.value}.fixture.v1",
        callback,
        {"candidate": candidate.value, "fixture_only": True},
    )


def _source(root: Path) -> Path:
    source = root / "source"
    source.mkdir(parents=True)
    (source / "payload").write_bytes(b"traceback synthetic executor fixture\n")
    return source


def _trace_completion(candidate: Candidate, root: Path) -> TraceResult:
    source = _source(root)
    runner = Runner(root / "state", synthetic_enabled=True)
    job = runner.submit(_job_request(source), source, ("payload",))
    calls: list[str] = []
    result = runner.execute(job.job_id, (_stage(candidate, calls),), worker_id="fixture")
    output = runner.outputs(job.job_id, "validate")["result"]
    passed = result.state == JobState.COMPLETE and output.is_file() and calls == [candidate.value]
    return TraceResult(
        candidate,
        "complete",
        passed,
        EvidenceKind.OBSERVED,
        False,
        False,
        "Traceback fixture receipt adopted; candidate runtime was not launched",
    )


def _trace_changed_input(candidate: Candidate, root: Path) -> TraceResult:
    source = _source(root)
    runner = Runner(root / "state", synthetic_enabled=True)
    job = runner.submit(_job_request(source), source, ("payload",))
    sealed = runner.snapshots_dir / job.job_id / "payload"
    sealed.chmod(0o644)
    sealed.write_bytes(b"changed")
    rejected = False
    try:
        runner.execute(job.job_id, (_stage(candidate, []),), worker_id="fixture")
    except SnapshotViolation:
        rejected = runner.status(job.job_id).state == JobState.TERMINAL_FAILURE
    return TraceResult(
        candidate,
        "changed_input_rejected",
        rejected,
        EvidenceKind.OBSERVED,
        False,
        False,
        "Runner rehashed the sealed fixture before dispatch",
    )


def _trace_kill_restart(candidate: Candidate, root: Path) -> TraceResult:
    source = _source(root)
    fired = False
    now = [1_000.0]

    def crash(point: str) -> None:
        nonlocal fired
        if point == "after_publication" and not fired:
            fired = True
            raise InjectedCrash(point)

    state = root / "state"
    runner = Runner(
        state,
        clock=lambda: now[0],
        synthetic_enabled=True,
        fault_injector=crash,
    )
    job = runner.submit(_job_request(source), source, ("payload",))
    calls: list[str] = []
    try:
        runner.execute(job.job_id, (_stage(candidate, calls),), worker_id="fixture-1")
    except InjectedCrash:
        pass
    now[0] += 31
    recovered = Runner(state, clock=lambda: now[0], synthetic_enabled=True)
    result = recovered.execute(
        job.job_id, (_stage(candidate, calls),), worker_id="fixture-2"
    )
    passed = result.state == JobState.COMPLETE and calls == [candidate.value]
    return TraceResult(
        candidate,
        "offline_restart_after_publication",
        passed,
        EvidenceKind.OBSERVED,
        False,
        False,
        "Published fixture receipt was adopted exactly once after process restart",
    )


def run_comparison() -> ComparisonReport:
    traces: list[TraceResult] = []
    with tempfile.TemporaryDirectory(prefix="traceback-executor-fixture-") as temporary:
        root = Path(temporary)
        for candidate in Candidate:
            traces.extend(
                (
                    _trace_completion(candidate, root / candidate.value / "complete"),
                    _trace_changed_input(candidate, root / candidate.value / "changed"),
                    _trace_kill_restart(candidate, root / candidate.value / "restart"),
                )
            )
    return ComparisonReport(
        schema_version="traceback.executor-comparison-fixture.v1",
        synthetic_only=True,
        real_data_authorized=False,
        host_system=platform.system(),
        host_machine=platform.machine(),
        runtime_observations=inventory_runtimes(),
        traces=tuple(traces),
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="emit canonical JSON")
    args = parser.parse_args(argv)
    report = run_comparison()
    if args.json:
        print(report.canonical_bytes().decode())
    else:
        print("Traceback synthetic executor comparison (not qualification)")
        for observation in report.runtime_observations:
            print(f"{observation.candidate}: {observation.state} — {observation.reason}")
        for result in report.traces:
            label = "PASS" if result.passed else "FAIL"
            print(f"{label} {result.candidate}/{result.scenario}: {result.detail}")
    return 0 if all(result.passed for result in report.traces) else 1


if __name__ == "__main__":
    raise SystemExit(main())
