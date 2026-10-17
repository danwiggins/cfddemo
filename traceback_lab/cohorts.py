"""Lab cohort membership, immutable generation publish and pinned readers.

Writers use the existing Lab flock and publication primitives.  A reader opens
``current.json`` once and then reads that immutable generation, so a build
cannot mix old and new members in one response.
"""

from __future__ import annotations

import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from traceback_runner.serialization import canonical_json_bytes

from .cohort_contracts import CohortGeneration, CohortMember, CohortResult, MetricProjection, project_metric
from .identity import SHA256_PATTERN, cohort_compat_key
from .ingest import resolve_sample, source_ids
from .problems import LabProblem
from .registry import execution_identity, load_registry
from .store import LabStore, new_run_id, open_regular_no_follow, write_atomic

if TYPE_CHECKING:
    from .pon import PonInput

_COHORT_ID = re.compile(r"^[a-z][a-z0-9-]{0,39}$")
_ROSTER_SCHEMA = "traceback.lab.cohort-roster.v1"
_VERSION_NAME = re.compile(r"^[1-9][0-9]*$")


@dataclass(frozen=True)
class CohortListing:
    """One cohort's pinned generation or its independent storage problem."""

    cohort_id: str
    generation: CohortGeneration | None
    problem: LabProblem | None


def _storage_problem(cohort_id: str, field: str, *, fix: str | None = None) -> LabProblem:
    if fix is None and field == "generation name":
        fix = "Inspect the unexpected generations entry and move it aside after preserving it; restore any damaged numeric generation from a known-good backup"
    return LabProblem(
        "TBX-LAB-022",
        cause=f"stored cohort {cohort_id} has missing or invalid {field}",
        fix=fix or "Restore the matching cohort file from a known-good backup before retrying",
        data={"cohort_id": cohort_id, "field": field},
    )


def _cohort_dir(store: LabStore, cohort_id: str) -> Path:
    if not _COHORT_ID.fullmatch(cohort_id):
        raise ValueError("cohort id must match ^[a-z][a-z0-9-]{0,39}$")
    base = store.lab / "cohorts"
    directory = base / cohort_id
    if base.is_symlink() or directory.is_symlink():
        raise ValueError("cohort directory may not be a symlink")
    return directory


def _read_cohort_json(path: Path, cohort_id: str) -> dict[str, Any] | None:
    if path.is_symlink():
        raise _storage_problem(cohort_id, path.name)
    try:
        content = path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise _storage_problem(cohort_id, path.name) from exc
    try:
        value = json.loads(content)
    except (ValueError, UnicodeDecodeError) as exc:
        raise _storage_problem(cohort_id, path.name) from exc
    if not isinstance(value, dict):
        raise _storage_problem(cohort_id, path.name)
    return value


def _generation_versions(directory: Path, cohort_id: str) -> tuple[int, ...]:
    generations = directory / "generations"
    if generations.is_symlink() or (generations.exists() and not generations.is_dir()):
        raise _storage_problem(cohort_id, "generations")
    if not generations.exists():
        return ()
    try:
        children = tuple(generations.iterdir())
    except OSError as exc:
        raise _storage_problem(cohort_id, "generations") from exc
    versions: list[int] = []
    for child in children:
        try:
            if child.name == ".DS_Store" and child.is_file() and not child.is_symlink():
                continue
            if child.name.startswith(".staging-") and child.is_dir() and not child.is_symlink():
                continue
            if child.is_symlink() or not child.is_dir() or not _VERSION_NAME.fullmatch(child.name):
                raise _storage_problem(cohort_id, "generation name")
        except OSError as exc:
            raise _storage_problem(cohort_id, "generation name") from exc
        try:
            versions.append(int(child.name))
        except ValueError as exc:
            raise _storage_problem(cohort_id, "generation name") from exc
    return tuple(sorted(versions))


def read_roster(store: LabStore, cohort_id: str) -> tuple[str, ...]:
    """Read pending source identities; no lock or generation scan needed."""

    data = _read_cohort_json(_cohort_dir(store, cohort_id) / "members.json", cohort_id)
    if data is None:
        return ()
    if data.get("schema") != _ROSTER_SCHEMA:
        raise _storage_problem(cohort_id, "roster schema")
    members = data.get("source_ids")
    if not isinstance(members, list) or any(not isinstance(item, str) or not SHA256_PATTERN.fullmatch(item) for item in members):
        raise _storage_problem(cohort_id, "roster source identities")
    if len(members) != len(set(members)):
        raise _storage_problem(cohort_id, "roster source identities")
    return tuple(members)


def _read_stored_metadata(path: Path, problem: LabProblem) -> Any | None:
    """Read a store record without traversing a substituted symlink."""

    try:
        with os.fdopen(open_regular_no_follow(path), "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise OSError("stored metadata is not a regular file")
            return json.loads(stream.read())
    except FileNotFoundError:
        return None
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        raise problem from exc


def _checked_source_record(store: LabStore, source_id: str) -> dict[str, Any]:
    missing = LabProblem(
        "TBX-LAB-020", cause=f"stored source record {source_id[:12]} is missing, malformed or unsafe",
        fix="Restore a regular source record matching its filename before cohort add/build",
        data={"source_id": source_id, "field": "source record"},
    )
    record = _read_stored_metadata(store.sources_dir / f"{source_id}.json", missing)
    if record is None:
        raise missing
    if not isinstance(record, dict) or record.get("source_id") != source_id:
        raise LabProblem(
            "TBX-LAB-020",
            cause=f"stored source record {source_id[:12]} has a mismatched source_id",
            fix="Restore the source record matching its filename before cohort add/build",
            data={"source_id": source_id, "field": "source_id"},
        )
    return record


def _resolve_cohort_sample(store: LabStore, sample_ref: str) -> str:
    if sample_ref != "latest":
        return resolve_sample(store, sample_ref)
    ids = source_ids(store)
    if not ids:
        return resolve_sample(store, sample_ref)
    dated: list[tuple[str, str]] = []
    for source_id in ids:
        record = _checked_source_record(store, source_id)
        inputs = record.get("inputs", [])
        if not isinstance(inputs, list):
            raise LabProblem(
                "TBX-LAB-020", cause=f"stored source record {source_id[:12]} has invalid inputs",
                fix="Restore the intact source record before cohort add",
                data={"source_id": source_id, "field": "inputs"},
            )
        timestamps: list[str] = []
        for item in inputs:
            if not isinstance(item, dict):
                field = "inputs"
            elif not isinstance(item.get("used_utc", ""), str):
                field = "used_utc"
            else:
                timestamps.append(item.get("used_utc", ""))
                continue
            raise LabProblem(
                "TBX-LAB-020", cause=f"stored source record {source_id[:12]} has invalid {field}",
                fix="Restore the intact source record before cohort add",
                data={"source_id": source_id, "field": field},
            )
        newest = max(timestamps, default="")
        dated.append((newest, source_id))
    return max(dated)[1]


def add_sample(store: LabStore, cohort_id: str, sample_ref: str) -> bool:
    """Admit a stored full-input source once; return False on an exact duplicate."""

    with store.writer("cohort add", new_run_id()):
        source_id = _resolve_cohort_sample(store, sample_ref)
        record = _checked_source_record(store, source_id)
        if record.get("example"):
            raise LabProblem(
                "TBX-LAB-021", cause=f"{source_id[:12]} is an EXAMPLE sample",
                fix="Add a real kit sample", data={"id12": source_id[:12]},
            )
        if not _full_input(store, record):
            raise ValueError("the source has no stored full aligned input")
        directory = _cohort_dir(store, cohort_id)
        roster_path = directory / "members.json"
        if not roster_path.exists() and not roster_path.is_symlink():
            pointer_path = directory / "current.json"
            if pointer_path.exists() or pointer_path.is_symlink() or _generation_versions(directory, cohort_id):
                raise _storage_problem(cohort_id, "members.json")
        roster = read_roster(store, cohort_id)
        if source_id in roster:
            return False
        write_atomic(directory / "members.json", canonical_json_bytes({
            "schema": _ROSTER_SCHEMA, "source_ids": [*roster, source_id],
        }))
        store.bump_generation()
        return True


def _invalid_input(input_id: str, field: str) -> LabProblem:
    return LabProblem(
        "TBX-LAB-020",
        cause=f"stored full input {input_id[:12]} has invalid {field}",
        fix="Rerun input ingest or restore the intact Lab input manifest before cohort build",
        data={"input_id": input_id, "field": field},
    )


def _validate_full_manifest(manifest: Any, input_id: str, source_id: str) -> dict[str, Any]:
    """Refuse corrupt identity metadata before result eligibility is considered."""

    if not isinstance(manifest, dict):
        raise _invalid_input(input_id, "manifest")
    if manifest.get("input_id") != input_id or manifest.get("source_id") != source_id:
        raise _invalid_input(input_id, "input/source identity")
    if manifest.get("kind") not in ("aligned", "prealigned"):
        raise _invalid_input(input_id, "input kind")
    reference = manifest.get("reference_sha256")
    if not isinstance(reference, str) or not SHA256_PATTERN.fullmatch(reference):
        raise _invalid_input(input_id, "reference_sha256")
    if manifest["kind"] == "aligned":
        tools = manifest.get("tools")
        if not isinstance(tools, dict) or any(
            not isinstance(tools.get(name), str) or not tools[name]
            for name in ("samtools", "minimap2")
        ):
            raise _invalid_input(input_id, "aligner tools")
        mmi = manifest.get("mmi_sha256")
        if not isinstance(mmi, str) or not SHA256_PATTERN.fullmatch(mmi):
            raise _invalid_input(input_id, "mmi_sha256")
        argv = manifest.get("aligner_argv")
        if not isinstance(argv, list) or not argv or any(
            not isinstance(part, str) or not part for part in argv
        ):
            raise _invalid_input(input_id, "aligner_argv")
    return manifest


def _full_input(store: LabStore, record: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    source_id = record.get("source_id")
    if not isinstance(source_id, str) or not SHA256_PATTERN.fullmatch(source_id):
        raise _invalid_input("unknown", "source_id")
    inputs = record.get("inputs", [])
    if not isinstance(inputs, list):
        raise _invalid_input("unknown", "source inputs")
    for item in reversed(inputs):
        if not isinstance(item, dict):
            raise _invalid_input("unknown", "source input entry")
        input_id = item.get("input_id")
        if not isinstance(input_id, str) or not SHA256_PATTERN.fullmatch(input_id):
            raise _invalid_input("unknown", "input_id")
        if item.get("kind") not in ("aligned", "prealigned"):
            raise _invalid_input(input_id, "source input kind")
        manifest_path = store.inputs_dir / input_id / "manifest.json"
        manifest = _read_stored_metadata(manifest_path, _invalid_input(input_id, "manifest"))
        manifest = _validate_full_manifest(manifest, input_id, source_id)
        if manifest["kind"] != item["kind"]:
            raise _invalid_input(input_id, "source/manifest kind")
        return input_id, manifest
    return None


def _aligner(manifest: dict[str, Any], input_id: str) -> tuple[str, dict[str, Any]]:
    if manifest["kind"] == "prealigned":
        # No recorded aligner/tool argv: do not pool distinct external pipelines.
        return (
            f"pre-aligned; provenance unknown ({input_id[:12]})",
            {"kind": "pre-aligned", "input_id": input_id},
        )
    tools = manifest["tools"]
    label = f"minimap2 {tools['minimap2']}; samtools {tools['samtools']}"
    return label, {
        "kind": "aligned", "tools": tools, "mmi_sha256": manifest["mmi_sha256"],
        "aligner_argv": manifest["aligner_argv"],
    }


def _selected_results(store: LabStore, input_id: str, manifest: dict[str, Any]) -> tuple[CohortResult, ...]:
    """Snapshot only selected status-ok results on the full input."""

    label, aligner = _aligner(manifest, input_id)
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
        scoped_assets_of = getattr(algorithm, "asset_sha256s_for_input", None)
        try:
            assets = scoped_assets_of(store.root, input_id) if callable(scoped_assets_of) else (
                assets_of(store.root) if callable(assets_of) else ()
            )
            identity = execution_identity(
                algorithm, reference_sha256=manifest["reference_sha256"], upstream=upstream,
                asset_sha256s=assets, tool_pins=[tool.identity() for tool in algorithm.tools],
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

    record = _checked_source_record(store, source_id)
    if record.get("example"):
        raise LabProblem("TBX-LAB-021", cause=f"{source_id[:12]} is an EXAMPLE sample",
                         fix="Add a real kit sample", data={"id12": source_id[:12]})
    selected = _full_input(store, record)
    if selected is None:
        raise ValueError("admitted source has no full aligned input")
    input_id, manifest = selected
    label, _ = _aligner(manifest, input_id)
    return CohortMember(source_id=source_id, input_id=input_id, aligner_identity=label,
                        results=_selected_results(store, input_id, manifest))


def pon_inputs_for_members(store: LabStore, members: tuple[CohortMember, ...]) -> tuple[PonInput, ...]:
    """Resolve eligible full aligned inputs for the isolated PoN builder.

    Pre-aligned inputs have unknown protocol provenance and cannot establish a
    protocol-matched panel. The caller holds the cohort writer lock.
    """

    from .pon import PonInput

    eligible: list[PonInput] = []
    for member in members:
        selected = _full_input(store, _checked_source_record(store, member.source_id))
        if selected is None or selected[0] != member.input_id:
            raise _invalid_input(member.input_id, "selected full input changed")
        input_id, manifest = selected
        label, _ = _aligner(manifest, input_id)
        if label != member.aligner_identity:
            raise _invalid_input(input_id, "aligner identity changed")
        if manifest["kind"] != "aligned":
            continue
        eligible.append(PonInput(
            source_id=member.source_id, input_id=input_id,
            reference_sha256=manifest["reference_sha256"], aligner_identity=label,
            bam_path=store.inputs_dir / input_id / "aligned.bam",
        ))
    return tuple(eligible)


def read_current(store: LabStore, cohort_id: str) -> CohortGeneration | None:
    """Pin one complete immutable generation with a single pointer read."""

    directory = _cohort_dir(store, cohort_id)
    pointer = _read_cohort_json(directory / "current.json", cohort_id)
    if pointer is None:
        if _generation_versions(directory, cohort_id):
            raise _storage_problem(
                cohort_id, "current.json",
                fix="If the member roster is intact, run lab cohort build with the same --root to publish a new generation; otherwise restore current.json from a known-good backup",
            )
        return None
    version = pointer.get("version")
    if type(version) is not int or version < 1:
        raise _storage_problem(cohort_id, "current version")
    generations = directory / "generations"
    if generations.is_symlink() or (generations.exists() and not generations.is_dir()):
        raise _storage_problem(cohort_id, "generations")
    if not (generations / str(version)).is_dir():
        raise _storage_problem(cohort_id, "generation.json")
    generation_dir = generations / str(version)
    generation_file = generation_dir / "generation.json"
    if generation_dir.is_symlink() or generation_file.is_symlink():
        raise _storage_problem(cohort_id, "generation.json")
    try:
        content = generation_file.read_bytes()
        generation = CohortGeneration.model_validate_json(content)
    except (OSError, ValidationError) as exc:
        raise _storage_problem(cohort_id, "generation.json") from exc
    if generation.cohort_id != cohort_id or generation.version != version:
        raise _storage_problem(cohort_id, "generation identity")
    return generation


def list_cohorts(store: LabStore) -> tuple[CohortListing, ...]:
    """Inspect each named cohort without hiding intact neighbors."""

    base = store.lab / "cohorts"
    try:
        if base.is_symlink():
            raise _storage_problem("cohorts", "cohort root")
        if not base.is_dir():
            return ()
        directories = sorted(base.iterdir())
    except OSError as exc:
        raise _storage_problem("cohorts", "cohort root") from exc
    rows: list[CohortListing] = []
    for directory in directories:
        if not _COHORT_ID.fullmatch(directory.name):
            continue
        try:
            valid_directory = not directory.is_symlink() and directory.is_dir()
        except OSError:
            valid_directory = False
        if not valid_directory:
            rows.append(CohortListing(directory.name, None, _storage_problem(directory.name, "cohort directory")))
            continue
        try:
            generation = read_current(store, directory.name)
        except LabProblem as problem:
            rows.append(CohortListing(directory.name, None, problem))
        except (OSError, ValueError):
            rows.append(CohortListing(directory.name, None, _storage_problem(directory.name, "cohort metadata")))
        else:
            if generation is not None:
                rows.append(CohortListing(directory.name, generation, None))
    return tuple(rows)


def build_cohort(store: LabStore, cohort_id: str, *, kind: str = "internal_cohort") -> CohortGeneration:
    """Publish a full snapshot atomically, preserving every older generation."""

    with store.writer("cohort build", new_run_id()):
        directory = _cohort_dir(store, cohort_id)
        if _read_cohort_json(directory / "current.json", cohort_id) is not None:
            read_current(store, cohort_id)
        generations = directory / "generations"
        versions = _generation_versions(directory, cohort_id)
        if (versions or (directory / "current.json").exists()) and not (directory / "members.json").is_file():
            raise _storage_problem(cohort_id, "members.json")
        members = tuple(member_from_store(store, source_id) for source_id in read_roster(store, cohort_id))
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


def project_sample_metric(
    store: LabStore, cohort_id: str, *, target_source_id: str,
    algorithm_id: str, metric_id: str,
) -> MetricProjection | None:
    """S5 adapter: derive the target's current compatible key, then project.

    Returns ``None`` if there is no current generation or target status-ok
    result.  It pins one generation and excludes the target by source ID even
    when its selected input or execution hash differs from its stored member.
    """

    generation = read_current(store, cohort_id)
    if generation is None:
        return None
    target = member_from_store(store, target_source_id)
    result = next((item for item in target.results if item.algorithm_id == algorithm_id), None)
    if result is None:
        return None
    return project_metric(
        generation, algorithm_id=algorithm_id, metric_id=metric_id,
        cohort_compat_key=result.cohort_compat_key, target_source_id=target_source_id,
    )


__all__ = [
    "CohortListing", "add_sample", "build_cohort", "list_cohorts", "member_from_store", "pon_inputs_for_members",
    "project_current_metric", "project_sample_metric", "read_current", "read_roster",
]
