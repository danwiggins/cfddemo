"""Lab cohort membership, immutable generation publish and pinned readers.

Writers use the existing Lab flock and publication primitives.  A reader opens
``current.json`` once and then reads that immutable generation, so a build
cannot mix old and new members in one response.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from traceback_runner.serialization import canonical_json_bytes

from .cohort_contracts import CohortGeneration, CohortMember, CohortResult, MetricProjection, project_metric
from .identity import SHA256_PATTERN, cohort_compat_key
from .ingest import input_manifest, resolve_sample, source_record
from .problems import LabProblem
from .registry import execution_identity, load_registry
from .store import LabStore, new_run_id, write_atomic

_COHORT_ID = re.compile(r"^[a-z][a-z0-9-]{0,39}$")
_ROSTER_SCHEMA = "traceback.lab.cohort-roster.v1"


def _cohort_dir(store: LabStore, cohort_id: str) -> Path:
    if not _COHORT_ID.fullmatch(cohort_id):
        raise ValueError("cohort id must match ^[a-z][a-z0-9-]{0,39}$")
    base = store.lab / "cohorts"
    directory = base / cohort_id
    if base.is_symlink() or directory.is_symlink():
        raise ValueError("cohort directory may not be a symlink")
    return directory


def _read_json(path: Path) -> dict[str, Any] | None:
    if path.is_symlink():
        raise ValueError("cohort metadata may not be a symlink")
    try:
        value = json.loads(path.read_bytes())
    except FileNotFoundError:
        return None
    if not isinstance(value, dict):
        raise ValueError(f"invalid Lab cohort metadata in {path.name}")
    return value


def read_roster(store: LabStore, cohort_id: str) -> tuple[str, ...]:
    """Read pending source identities; no lock or generation scan needed."""

    data = _read_json(_cohort_dir(store, cohort_id) / "members.json")
    if data is None:
        return ()
    if data.get("schema") != _ROSTER_SCHEMA:
        raise ValueError("unsupported cohort roster schema")
    members = data.get("source_ids")
    if not isinstance(members, list) or any(not isinstance(item, str) or not SHA256_PATTERN.fullmatch(item) for item in members):
        raise ValueError("invalid cohort source identities")
    if len(members) != len(set(members)):
        raise ValueError("duplicate cohort source identity")
    return tuple(members)


def add_sample(store: LabStore, cohort_id: str, sample_ref: str) -> bool:
    """Admit a stored full-input source once; return False on an exact duplicate."""

    with store.writer("cohort add", new_run_id()):
        source_id = resolve_sample(store, sample_ref)
        record = source_record(store, source_id)
        if record is None:
            raise ValueError("the sample has no source record")
        if record.get("example"):
            raise LabProblem(
                "TBX-LAB-021", cause=f"{source_id[:12]} is an EXAMPLE sample",
                fix="Add a real kit sample", data={"id12": source_id[:12]},
            )
        if not _full_input(store, record):
            raise ValueError("the source has no stored full aligned input")
        roster = read_roster(store, cohort_id)
        if source_id in roster:
            return False
        directory = _cohort_dir(store, cohort_id)
        write_atomic(directory / "members.json", canonical_json_bytes({
            "schema": _ROSTER_SCHEMA, "source_ids": [*roster, source_id],
        }))
        store.bump_generation()
        return True


def _full_input(store: LabStore, record: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    for item in reversed(record.get("inputs", [])):
        input_id = item.get("input_id")
        if not isinstance(input_id, str) or not SHA256_PATTERN.fullmatch(input_id):
            continue
        manifest = input_manifest(store, input_id)
        if manifest is not None and manifest.get("kind") in ("aligned", "prealigned") and manifest.get("source_id") == record.get("source_id"):
            return input_id, manifest
    return None


def _aligner(manifest: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    if manifest["kind"] == "prealigned":
        return "pre-aligned", {"kind": "pre-aligned"}
    tools = manifest.get("tools") or {}
    label = f"minimap2 {tools.get('minimap2', '?')}; samtools {tools.get('samtools', '?')}"
    return label, {
        "kind": "aligned", "tools": tools, "mmi_sha256": manifest.get("mmi_sha256"),
        "aligner_argv": manifest.get("aligner_argv"),
    }


def _selected_results(store: LabStore, input_id: str, manifest: dict[str, Any]) -> tuple[CohortResult, ...]:
    """Snapshot only selected status-ok results on the full input."""

    label, aligner = _aligner(manifest)
    del label
    found: list[CohortResult] = []
    identities: dict[str, str] = {}
    for algorithm in load_registry():
        upstream = [
            {"algorithm_id": name, "parameters_sha256": identities[name]}
            for name in algorithm.upstream if name in identities
        ]
        if len(upstream) != len(algorithm.upstream):
            continue
        assets_of = getattr(algorithm, "asset_sha256s", None)
        try:
            identity = execution_identity(
                algorithm, reference_sha256=manifest["reference_sha256"], upstream=upstream,
                asset_sha256s=assets_of(store.root) if callable(assets_of) else (),
            )
        except (OSError, ValueError, KeyError, LabProblem):
            # An unresolved asset cannot make an old result current-compatible.
            continue
        chosen = store.read_result(input_id, algorithm.id, identity)
        if chosen is None or chosen.status != "ok":
            continue
        if chosen.algorithm_version != algorithm.version or chosen.provenance.source_id != manifest["source_id"]:
            continue
        identities[algorithm.id] = identity
        metrics = {
            metric.metric_id: metric.value
            for metric in chosen.metrics
            if metric.value is None or (isinstance(metric.value, (int, float)) and not isinstance(metric.value, bool))
        }
        compat = cohort_compat_key(
            algorithm_id=algorithm.id, algorithm_version=algorithm.version,
            parameters=algorithm.parameters, tools=[tool.identity() for tool in algorithm.tools],
            reference_sha256=manifest["reference_sha256"], aligner_identity=aligner,
            pon_policy=str(algorithm.parameters.get("pon_policy", "none")),
            per_sample_asset_keys=("pon_asset_sha256",),
        )
        found.append(CohortResult(
            algorithm_id=algorithm.id, parameters_sha256=chosen.provenance.parameters_sha256,
            cohort_compat_key=compat, metrics=metrics,
        ))
    return tuple(found)


def member_from_store(store: LabStore, source_id: str) -> CohortMember:
    """Pin current full-input result attempts for one admitted source."""

    record = source_record(store, source_id)
    if record is None:
        raise ValueError("admitted source is missing")
    if record.get("example"):
        raise LabProblem("TBX-LAB-021", cause=f"{source_id[:12]} is an EXAMPLE sample",
                         fix="Add a real kit sample", data={"id12": source_id[:12]})
    selected = _full_input(store, record)
    if selected is None:
        raise ValueError("admitted source has no full aligned input")
    input_id, manifest = selected
    label, _ = _aligner(manifest)
    return CohortMember(source_id=source_id, input_id=input_id, aligner_identity=label,
                        results=_selected_results(store, input_id, manifest))


def read_current(store: LabStore, cohort_id: str) -> CohortGeneration | None:
    """Pin one complete immutable generation with a single pointer read."""

    directory = _cohort_dir(store, cohort_id)
    pointer = _read_json(directory / "current.json")
    if pointer is None:
        return None
    version = pointer.get("version")
    if type(version) is not int or version < 1:
        raise ValueError("invalid cohort current version")
    generations = directory / "generations"
    if generations.is_symlink():
        raise ValueError("cohort generations may not be a symlink")
    generation_dir = generations / str(version)
    generation_file = generation_dir / "generation.json"
    if generation_dir.is_symlink() or generation_file.is_symlink():
        raise ValueError("cohort generation may not be a symlink")
    content = generation_file.read_bytes()
    generation = CohortGeneration.model_validate_json(content)
    if generation.cohort_id != cohort_id or generation.version != version:
        raise ValueError("cohort generation does not match current pointer")
    return generation


def build_cohort(store: LabStore, cohort_id: str, *, kind: str = "internal_cohort") -> CohortGeneration:
    """Publish a full snapshot atomically, preserving every older generation."""

    with store.writer("cohort build", new_run_id()):
        directory = _cohort_dir(store, cohort_id)
        members = tuple(member_from_store(store, source_id) for source_id in read_roster(store, cohort_id))
        generations = directory / "generations"
        if generations.is_symlink():
            raise ValueError("cohort generations may not be a symlink")
        versions = [int(path.name) for path in generations.iterdir() if path.is_dir() and path.name.isdigit()] if generations.is_dir() else []
        version = max(versions, default=0) + 1
        snapshot = CohortGeneration(cohort_id=cohort_id, version=version, kind=kind, members=members)

        def write(staging: Path) -> None:
            (staging / "generation.json").write_bytes(canonical_json_bytes(snapshot.model_dump(mode="json", by_alias=True)))

        store.publish_directory(generations, str(version), write)
        write_atomic(directory / "current.json", canonical_json_bytes({"version": version}))
        store.bump_generation()
        return snapshot


def project_current_metric(
    store: LabStore, cohort_id: str, *, algorithm_id: str, metric_id: str,
    cohort_compat_key: str, target_source_id: str | None = None,
) -> MetricProjection | None:
    """S5 read API: one pinned version, eligible counts, values and reasons."""

    generation = read_current(store, cohort_id)
    if generation is None:
        return None
    return project_metric(generation, algorithm_id=algorithm_id, metric_id=metric_id,
                          cohort_compat_key=cohort_compat_key, target_source_id=target_source_id)


__all__ = [
    "add_sample", "build_cohort", "member_from_store", "project_current_metric",
    "read_current", "read_roster",
]
