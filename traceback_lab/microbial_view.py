"""Read-only microbial profile and exact-compatible cohort 97.5th percentiles."""

from __future__ import annotations

import json
import math
import os
import re
from pathlib import Path
from typing import Any

from traceback_runner.serialization import canonical_json_bytes

from .algorithms.microbial import MICROBIAL
from .cohorts import _execution_plan
from .cohort_contracts import CohortGeneration, CohortMember, CohortResult
from .file_access import open_regular_no_follow
from .ingest import input_manifest, resolve_reference
from .interpretation.engine import COHORT_MIN
from .microbial_db import MicrobialDbError, MicrobialDbReceiptChanged, RECEIPT_RECOVERY, inspect_database
from .problems import LabProblem
from .registry import execution_identity
from .store import LabStore
from .views import SampleView

PROFILE_SCHEMA = "traceback.lab.microbial-profile.v1"
_METRIC = re.compile(r"taxon_([1-9][0-9]{0,11})_rpm\Z")


def _read_profile(path: Path) -> dict[str, Any] | None:
    try:
        with os.fdopen(open_regular_no_follow(path), "rb") as stream:
            raw = stream.read(512 * 1024 + 1)
        if len(raw) > 512 * 1024:
            return None
        value = json.loads(raw)
    except (OSError, ValueError):
        return None
    if not isinstance(value, dict) or value.get("schema") != PROFILE_SCHEMA:
        return None
    for key in ("denominator", "candidate_unmapped_reads", "too_short_unmapped_reads",
                "missing_sequence_unmapped_reads", "omitted_taxa"):
        number = value.get(key)
        if not isinstance(number, int) or isinstance(number, bool) or number < 0:
            return None
    if (value["candidate_unmapped_reads"] + value["too_short_unmapped_reads"]
            + value["missing_sequence_unmapped_reads"] > value["denominator"]):
        return None
    rows = value.get("taxa")
    if not isinstance(rows, list) or len(rows) > 256:
        return None
    seen: set[int] = set()
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"taxid", "name", "category", "estimated_reads", "rpm_primary"}:
            return None
        taxid, name = row["taxid"], row["name"]
        if (not isinstance(taxid, int) or isinstance(taxid, bool) or taxid <= 0 or taxid in seen
                or not isinstance(name, str) or not 1 <= len(name) <= 160
                or any(ord(char) < 32 for char in name)
                or row["category"] not in ("bacteria", "archaea", "viruses", "fungi")):
            return None
        seen.add(taxid)
        for key in ("estimated_reads", "rpm_primary"):
            number = row[key]
            if isinstance(number, bool) or not isinstance(number, (int, float)) or not math.isfinite(number) or number < 0:
                return None
    return value


def _profile_matches_metrics(profile: dict[str, Any], static: dict[str, Any]) -> bool:
    """Reject an artifact that disagrees with its selected result envelope."""
    for artifact_key, metric_id in (("denominator", "primary_reads"),
                                    ("candidate_unmapped_reads", "candidate_unmapped_reads"),
                                    ("too_short_unmapped_reads", "too_short_unmapped_reads"),
                                    ("missing_sequence_unmapped_reads", "missing_sequence_unmapped_reads"),
                                    ("omitted_taxa", "omitted_taxa")):
        value = static.get(metric_id)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value != profile[artifact_key]:
            return False
    denominator = profile["denominator"]
    taxon_ids = {f"taxon_{row['taxid']}_rpm" for row in profile["taxa"]}
    if taxon_ids != {key for key in static if _METRIC.fullmatch(key)}:
        return False
    for row in profile["taxa"]:
        value = static.get(f"taxon_{row['taxid']}_rpm")
        if (denominator == 0 or isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or value < 0
                or not math.isclose(value, row["rpm_primary"], rel_tol=1e-12, abs_tol=1e-12)
                or not math.isclose(row["rpm_primary"], row["estimated_reads"] * 1_000_000 / denominator,
                                    rel_tol=1e-12, abs_tol=1e-12)):
            return False
    estimate, rate = static.get("microbial_estimated_reads"), static.get("microbial_rpm_primary")
    assay_unavailable = not profile["candidate_unmapped_reads"] and (
        profile["too_short_unmapped_reads"] + profile["missing_sequence_unmapped_reads"] > 0)
    if denominator and not assay_unavailable:
        if (isinstance(estimate, bool) or not isinstance(estimate, (int, float))
                or not math.isfinite(estimate)
                or (estimate < sum(row["estimated_reads"] for row in profile["taxa"])
                    and not math.isclose(estimate, sum(row["estimated_reads"] for row in profile["taxa"]),
                                         rel_tol=1e-12, abs_tol=1e-12))
                or isinstance(rate, bool) or not isinstance(rate, (int, float)) or not math.isfinite(rate)
                or not math.isclose(rate, estimate * 1_000_000 / denominator,
                                    rel_tol=1e-12, abs_tol=1e-12)):
            return False
    return True


def _p975(values: list[float]) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * 0.975
    left = int(position)
    fraction = position - left
    return ordered[left] * (1 - fraction) + ordered[min(left + 1, len(ordered) - 1)] * fraction


def _cohort_results(generation: CohortGeneration | None, target: CohortMember | None,
                    source_id: str, identity: str) -> list[CohortResult]:
    if generation is None or target is None:
        return []
    target_result = next((result for result in target.results
                          if result.algorithm_id == "microbial" and result.parameters_sha256 == identity), None)
    if target_result is None:
        return []
    return [result for member in generation.members
            if member.source_id != source_id and member.aligner_identity == target.aligner_identity
            for result in member.results
            if (result.algorithm_id == "microbial" and result.parameters_sha256 == identity
                and result.cohort_compat_key == target_result.cohort_compat_key
                and isinstance(result.metrics.get("primary_reads"), (int, float))
                and not isinstance(result.metrics["primary_reads"], bool)
                and math.isfinite(result.metrics["primary_reads"])
                and result.metrics["primary_reads"] > 0
                and all(isinstance(result.metrics.get(key), (int, float))
                        and not isinstance(result.metrics[key], bool)
                        and math.isfinite(result.metrics[key]) and result.metrics[key] >= 0
                        for key in ("candidate_unmapped_reads", "too_short_unmapped_reads",
                                    "missing_sequence_unmapped_reads"))
                and not (result.metrics["candidate_unmapped_reads"] == 0
                         and (result.metrics["too_short_unmapped_reads"]
                              + result.metrics["missing_sequence_unmapped_reads"]) > 0)
                and isinstance(result.metrics.get("microbial_rpm_primary"), (int, float))
                and not isinstance(result.metrics["microbial_rpm_primary"], bool)
                and math.isfinite(result.metrics["microbial_rpm_primary"])
                and result.metrics["microbial_rpm_primary"] >= 0)]


def profile_for_sample(store: LabStore, sample: SampleView, generation: CohortGeneration | None,
                       target: CohortMember | None, cohort_state: str) -> dict[str, Any] | None:
    envelope = sample.full.results.get("microbial")
    if envelope is None:
        return None
    if envelope.status != "ok":
        return {"state": "measurement_unavailable", "reason": envelope.skip_message or "Microbial measurement unavailable",
                "taxa": []}
    summary = sample.full.results.get("read-summary")
    if summary is None or summary.status != "ok":
        return {"state": "upstream_unavailable", "reason": "Selected read summary is unavailable; rerun it before viewing this microbial profile",
                "taxa": []}
    try:
        database = inspect_database(store, verify_bytes=False)
        reference = resolve_reference(store.root, envelope.provenance.reference_id, example=True)
    except MicrobialDbReceiptChanged:
        return {"state": "asset_unavailable", "reason": RECEIPT_RECOVERY, "taxa": []}
    except (MicrobialDbError, LabProblem, OSError, ValueError):
        return {"state": "asset_unavailable", "reason": "Pinned microbial database or reference is unavailable",
                "taxa": []}
    input_id = sample.full.input_id
    identity = envelope.provenance.parameters_sha256
    try:
        manifest = input_manifest(store, input_id)
    except (OSError, ValueError, TypeError):
        manifest = None
    if (not isinstance(manifest, dict)
            or manifest.get("input_id") != input_id
            or manifest.get("source_id") != sample.source_id
            or manifest.get("reference_id") != envelope.provenance.reference_id
            or manifest.get("reference_sha256") != reference.sha256
            or manifest.get("platform") != sample.platform):
        return {"state": "stale", "reason": "Stored microbial input manifest differs from this result; restore the recorded input or rerun microbial profiling",
                "taxa": []}
    if (envelope.algorithm_version != MICROBIAL.version
            or envelope.provenance.source_id != sample.source_id or envelope.provenance.input_id != input_id
            or summary.provenance.source_id != sample.source_id
            or summary.provenance.input_id != input_id
            or summary.provenance.reference_id != envelope.provenance.reference_id):
        return {"state": "stale", "reason": "Microbial input or selected read summary differs; rerun microbial profiling",
                "taxa": []}
    selected = store.selected_result_snapshot(input_id, "microbial", identity)
    if (selected is None or selected[1] != envelope
            or (sample.full.attempt_ids.get("microbial") is not None
                and selected[0].name != sample.full.attempt_ids["microbial"])):
        return {"state": "stored_profile_unavailable", "reason": "Selected microbial result changed while loading its profile",
                "taxa": []}
    summary_identity = summary.provenance.parameters_sha256
    summary_selected = store.selected_result_snapshot(input_id, "read-summary", summary_identity)
    if (summary_selected is None or summary_selected[1] != summary
            or (sample.full.attempt_ids.get("read-summary") is not None
                and summary_selected[0].name != sample.full.attempt_ids["read-summary"])):
        return {"state": "upstream_unavailable", "reason": "Selected read summary changed; reload the sample or rerun microbial profiling",
                "taxa": []}
    attempt = selected[0]
    plan = _execution_plan(attempt)
    if plan is None:
        return {"state": "stored_profile_unavailable", "reason": "Selected microbial producer plan is missing or invalid; rerun microbial profiling",
                "taxa": []}
    upstream = [{"algorithm_id": "read-summary", "parameters_sha256": summary_identity}]
    try:
        valid_plan = (
            plan.get("schema") == "traceback.lab.execution-plan.v1"
            and plan.get("algorithm_id") == MICROBIAL.id
            and plan.get("algorithm_version") == MICROBIAL.version
            and plan.get("input_id") == input_id
            and plan.get("reference_sha256") == reference.sha256
            and plan.get("platform") == sample.platform
            and plan.get("upstream") == upstream
            and plan.get("asset_sha256s") == [database.sha256]
            and isinstance(plan.get("parameters"), dict)
            and canonical_json_bytes(plan["parameters"]) == canonical_json_bytes(MICROBIAL.parameters)
            and isinstance(plan.get("tools"), list)
            and len(plan["tools"]) == 2
            and [pin.get("name") for pin in plan["tools"]] == ["kraken2", "bracken"]
            and all(isinstance(pin.get("version"), str) and re.fullmatch(r"[0-9a-f]{64}", pin["version"])
                    for pin in plan["tools"])
        )
        expected = (execution_identity(
            MICROBIAL, reference_sha256=reference.sha256,
            asset_sha256s=plan["asset_sha256s"], upstream=upstream,
            platform=sample.platform if sample.platform != "ont" else None,
            tool_pins=plan["tools"], parameters=plan["parameters"],
        ) if valid_plan else None)
    except (TypeError, ValueError, KeyError, AttributeError, RecursionError):
        expected = None
    if expected != identity:
        return {"state": "stale", "reason": "Selected microbial producer plan, reference, or database differs; restore the recorded inputs or rerun microbial profiling",
                "taxa": []}
    if store.upstream_selections_at(attempt) != ({
        "algorithm_id": "read-summary", "input_id": input_id,
        "parameters_sha256": summary_identity, "attempt_id": summary_selected[0].name,
    },):
        return {"state": "stale", "reason": "Selected read-summary attempt differs from the microbial producer plan; rerun microbial profiling",
                "taxa": []}
    profile = _read_profile(attempt / "artifacts" / "microbial_profile.json")
    if (profile is None or profile.get("source_id") != sample.source_id
            or profile.get("input_id") != sample.full.input_id
            or profile.get("database_sha256") != database.sha256
            or profile.get("database_source_label") != database.source_label
            or profile.get("read_length") != database.read_length):
        return {"state": "stored_profile_unavailable", "reason": "Selected microbial profile is incomplete",
                "taxa": []}
    static = {metric.metric_id: metric.value for metric in envelope.metrics}
    if not _profile_matches_metrics(profile, static):
        return {"state": "stored_profile_unavailable", "reason": "Selected microbial profile disagrees with its result metrics",
                "taxa": []}
    if profile["denominator"] == 0:
        return {"state": "measurement_unavailable",
                "reason": "No primary QC-passing non-duplicate reads; microbial rate and cohort comparison are unavailable",
                "taxa": []}
    if (profile["candidate_unmapped_reads"] == 0
            and profile["too_short_unmapped_reads"] + profile["missing_sequence_unmapped_reads"] > 0):
        return {"state": "measurement_unavailable",
                "reason": "Unmapped reads lacked enough sequence for the registered read length; microbial rate and cohort comparison are unavailable",
                "taxa": []}
    rate = static.get("microbial_rpm_primary")
    if (isinstance(rate, bool) or not isinstance(rate, (int, float))
            or not math.isfinite(rate) or rate < 0):
        return {"state": "stored_profile_unavailable", "reason": "Selected microbial metrics are incomplete",
                "taxa": []}
    compatible = _cohort_results(generation, target, sample.source_id, expected)
    cohort_reason = ("Build the healthy-v1 cohort to obtain a protocol-matched reference"
                     if generation is None else
                     "Cohort target is unavailable or changed" if cohort_state != "ready" or target is None else
                     "Fewer than 10 compatible independent cohort samples")
    available = cohort_state == "ready" and len(compatible) >= COHORT_MIN
    total_values = [float(value) for result in compatible
                    if (isinstance(value := result.metrics.get("microbial_rpm_primary"), (int, float))
                        and not isinstance(value, bool) and math.isfinite(value) and value >= 0)]
    total_threshold = _p975(total_values) if available and len(total_values) >= COHORT_MIN else None
    rows = []
    for row in profile["taxa"]:
        metric_id = f"taxon_{row['taxid']}_rpm"
        if not _METRIC.fullmatch(metric_id):
            continue
        values: list[float] = []
        for result in compatible:
            value = result.metrics.get(metric_id)
            if (isinstance(value, (int, float)) and not isinstance(value, bool)
                    and math.isfinite(value) and value >= 0):
                values.append(float(value))
            elif metric_id not in result.metrics and result.metrics.get("omitted_taxa") == 0:
                values.append(0.0)
        threshold = _p975(values) if available and len(values) >= COHORT_MIN else None
        rows.append({**row, "cohort_p975": threshold, "cohort_n": len(values),
                     "above_cohort_p975": row["rpm_primary"] > threshold if threshold is not None else None})
    return {"state": "ready" if total_threshold is not None else "cohort_unavailable",
            "reason": None if total_threshold is not None else cohort_reason,
            "database_sha256": database.sha256, "read_length": database.read_length,
            "database_source_label": database.source_label,
            "denominator": profile.get("denominator"), "candidate_unmapped_reads": profile.get("candidate_unmapped_reads"),
            "too_short_unmapped_reads": profile.get("too_short_unmapped_reads"),
            "missing_sequence_unmapped_reads": profile.get("missing_sequence_unmapped_reads"),
            "omitted_taxa": profile.get("omitted_taxa"),
            "microbial_rpm_primary": float(static["microbial_rpm_primary"]),
            "cohort_p975": total_threshold, "cohort_n": len(total_values),
            "cohort_version": generation.version_id if generation else None, "taxa": rows,
            "interpretation": "exploratory; not an infection diagnosis"}
