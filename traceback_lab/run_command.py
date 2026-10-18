"""``traceback lab run``: ingest, align, run the registry, summarize (D9, D20, D22, D36)."""

from __future__ import annotations

import argparse
import errno
import shlex
import shutil
import signal
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from traceback_runner.cli import _local_envelope, _result, _with_human

from . import registry
from .cohort_command import _cohort_id
from .identity import SHA256_PATTERN, aligned_input_id, prealigned_input_id
from .ingest import (
    EXAMPLE_REFERENCE_ID,
    GIB,
    AlignedInput,
    Budget,
    Chunk,
    ChunkSet,
    _existing_ancestor,
    _check_regular,
    _read_header,
    align,
    aligner_argv_identity,
    cached_sha256,
    collect_paths,
    disk_budget,
    ensure_mmi,
    estimate_align_minutes,
    id12_for,
    ingest_prealigned,
    input_manifest,
    inspect_chunks,
    is_sample_reference,
    mmi_path,
    record_source_sex,
    require_budget,
    resolve_reference,
    resolve_sample,
    source_record,
    write_source,
)
from .platforms import for_input, from_rg_lines
from .reference_integrity import verify_reference_source
from .problems import (
    EXIT_OK,
    EXIT_PARTIAL,
    EXIT_USAGE,
    LabProblem,
    problem_lines,
    problem_payload,
)
from .registry import execution_identity
from .runner import DEFAULT_TIMEOUT_S, RunTarget, StepResult, run_plan
from .sample_metadata import with_sex_assertion
from .scrub import Scrubber
from .store import LabStore, ResultOrderUnavailable, SelectionSnapshotChanged, new_run_id
from .tools import find_minimap2, find_samtools, require_aligner_tools

ALIGN_TIMEOUT_S = 48 * 3600.0
QUICKSTART = (
    "traceback lab doctor",
    "traceback lab run --example",
    "traceback lab serve --open",
)


def add_run_parser(commands: Any, common: Callable[[argparse.ArgumentParser], None]) -> None:
    run = commands.add_parser(
        "run",
        help="ingest (and align) a kit BAM, then run every algorithm it supports",
        description="Run the Lab on a BAM, uBAM, chunk directory, or a stored sample (id12 | latest).",
    )
    run.add_argument("inputs", nargs="*", metavar="INPUT", help="BAM | uBAM | chunk dir | id12 | latest")
    run.add_argument("--example", action="store_true", help="synthetic walkthrough generated at runtime (EXAMPLE sample)")
    run.add_argument("--reference", dest="reference_id", metavar="ID", help="registered reference id")
    run.add_argument("--profile", choices=("all", "fast"), default="all")
    run.add_argument("--only", metavar="A,B", help="only these algorithms (overrides --profile)")
    run.add_argument("--cohort", type=_cohort_id, metavar="ID", help="use this built protocol-matched cohort's panel of normals")
    run.add_argument("--downsample", default="0.5,0.25,0.1", metavar="F,F,F", help="depth-curve fractions (default 0.5,0.25,0.1)")
    run.add_argument("--no-downsample", action="store_true", help="skip the depth curve")
    run.add_argument("--controls", dest="controls", action="store_true", default=True, help="run the positive controls (default)")
    run.add_argument("--no-controls", dest="controls", action="store_false", help="skip the CNA spike and split halves")
    run.add_argument("--tf", default="0.03,0.10,0.30", metavar="TF,TF", help="synthetic CNA spike tumour fractions")
    run.add_argument("--skip-bad-chunks", action="store_true", help="exclude truncated chunks (provisional)")
    run.add_argument("--sex", choices=("M", "F"), help="operator assertion for a pre-analytic comparison; not a diagnostic sex or gender determination")
    run.add_argument("--force", action="store_true", help="recompute every result")
    run.add_argument("--timeout-s", type=float, default=DEFAULT_TIMEOUT_S, metavar="S")
    run.add_argument("--threads", type=_threads, metavar="N", help="alignment threads (default min(8, cores-2))")
    run.add_argument("--sort-mem", type=_sort_mem, default="1G", metavar="MEM", help="samtools sort memory per thread (e.g. 768M, 2G)")
    run.add_argument("--mmi", type=Path, metavar="PATH", help="reuse an existing minimap2 index")
    run.add_argument("--methylbert-bundle-sha256", type=_sha256, metavar="SHA256", help="operator-trusted registered MethylBERT tumour/normal bundle")
    run.add_argument("--methylbert-code-sha256", type=_sha256, metavar="SHA256", help="trusted digest of the pinned installed MethylBERT Python sources")
    run.add_argument("--methylbert-min-classified", type=_positive_count, metavar="N", help="independently qualified minimum unique classified reads for this MethylBERT model")
    run.add_argument("--dry-run", action="store_true", help="print the plan; write nothing, take no lock")
    run.add_argument("--verbose", action="store_true", help="stream tool stderr live (never stored)")
    common(run)
    run.set_defaults(handler=run_command)


def _sort_mem(value: str) -> str:
    import re

    if not re.fullmatch(r"[1-9][0-9]{0,5}[KMG]?", value):
        raise argparse.ArgumentTypeError("--sort-mem is a size such as 768M or 2G")
    return value


def _threads(value: str) -> int:
    number = int(value)
    if not 1 <= number <= 256:
        raise argparse.ArgumentTypeError("--threads is 1-256")
    return number


def _sha256(value: str) -> str:
    import re

    if not re.fullmatch(r"[0-9a-f]{64}", value):
        raise argparse.ArgumentTypeError("MethylBERT SHA-256 must be 64 lowercase hexadecimal characters")
    return value


def _positive_count(value: str) -> int:
    if not value.isdecimal() or len(value) > 12 or int(value) < 1:
        raise argparse.ArgumentTypeError("MethylBERT minimum classified reads must be a positive integer")
    return int(value)


def _progress(as_json: bool) -> Callable[[str], None]:
    stream = sys.stderr if as_json else sys.stdout

    def emit(line: str) -> None:
        print(line, file=stream, flush=True)

    return emit


def _echo(verbose: bool) -> Callable[[str], None] | None:
    if not verbose:
        return None

    def echo(line: str) -> None:
        print(f"  | {line}", file=sys.stderr, flush=True)

    return echo


def _only(text: str | None) -> list[str] | None:
    if text is None:
        return None
    ids = [item.strip() for item in text.split(",") if item.strip()]
    if not ids:
        raise LabProblem(
            "TBX-LAB-001",
            cause="--only names no algorithm",
            fix="`traceback lab algorithms` lists ids",
            data={"unknown": []},
        )
    return ids


def _interrupted_handler(signum: int, frame: Any) -> None:
    raise KeyboardInterrupt


def _usage_quickstart(store: LabStore) -> tuple[int, dict[str, Any]]:
    payload = _local_envelope(
        _result(
            "lab run",
            "usage",
            "lab run needs an input (BAM, uBAM, chunk dir, id12 or latest)",
            data={"root": str(store.root), "quickstart": list(QUICKSTART)},
        )
    )
    return EXIT_USAGE, _with_human(
        payload, [f"ROOT  {store.root}", "Quickstart:", *(f"  {line}" for line in QUICKSTART)], replace_data=True
    )


def _flags(args: argparse.Namespace) -> dict[str, Any]:
    """Flags for the run log: no paths, ever."""

    return {
        "profile": args.profile,
        "only": args.only,
        "cohort": args.cohort,
        "skip_bad_chunks": args.skip_bad_chunks,
        "force": args.force,
        "timeout_s": args.timeout_s,
        "threads": args.threads,
        "sort_mem": args.sort_mem,
        "mmi_override": args.mmi is not None,
        "reference_id": args.reference_id,
        "declared_sex": args.sex,
        "stored_sample": len(args.inputs) == 1 and _is_stored(args.inputs[0]),
        "methylbert_bundle_sha256": getattr(args, "methylbert_bundle_sha256", None),
        "methylbert_code_sha256": getattr(args, "methylbert_code_sha256", None),
        "methylbert_min_classified_reads": getattr(args, "methylbert_min_classified", None),
    }


def _is_stored(text: str) -> bool:
    return is_sample_reference(text) and not Path(text).exists()


def _target_from_input(
    aligned: AlignedInput, reference_id: str, reference_sha256: str, example: bool, store: LabStore,
    *, methylbert_bundle_sha256: str | None = None, methylbert_code_sha256: str | None = None,
    methylbert_min_classified_reads: int | None = None,
) -> RunTarget:
    extras: dict[str, Any] = {}
    extras["reference_fasta"] = str(resolve_reference(store.root, reference_id, example=True).fasta)
    extras["methylbert_bundle_sha256"] = methylbert_bundle_sha256
    extras["methylbert_code_sha256"] = methylbert_code_sha256
    extras["methylbert_min_classified_reads"] = methylbert_min_classified_reads
    platform = _stored_platform(aligned)
    return RunTarget(
        input_id=aligned.input_id,
        source_id=aligned.source_id,
        input_dir=aligned.directory,
        reference_id=reference_id,
        reference_sha256=reference_sha256,
        platform=platform,
        label="full",
        example=example,
        extras=extras,
        reference_integrity_required=True,
    )


def _stored_platform(aligned: AlignedInput) -> str:
    try:
        platform = for_input(aligned.manifest, aligned.directory / "aligned.bam")
    except (OSError, ValueError) as exc:
        raise LabProblem(
            "TBX-LAB-024", cause=f"stored input {aligned.input_id[:12]} platform or BAM is invalid",
            fix="Restore this stored input from a known-good backup, or re-ingest the original BAM under a fresh ROOT",
            data={"input_id": aligned.input_id},
        ) from exc
    if platform == "unknown":
        raise LabProblem(
            "TBX-LAB-024", cause=f"stored input {aligned.input_id[:12]} has no verifiable platform declaration",
            fix="Verify instrument provenance and re-ingest a BAM with PL:ONT or PL:ILLUMINA under a fresh ROOT",
            data={"input_id": aligned.input_id},
        )
    return platform


def _stored_reference_binding(aligned: AlignedInput) -> tuple[str, str]:
    manifest = aligned.manifest
    if (
        not isinstance(manifest, dict)
        or not isinstance(manifest.get("reference_id"), str)
        or not manifest["reference_id"]
        or not isinstance(manifest.get("reference_sha256"), str)
        or not SHA256_PATTERN.fullmatch(manifest["reference_sha256"])
    ):
        raise LabProblem(
            "TBX-LAB-017",
            cause="the stored aligned input has no valid reference binding",
            fix="Restore the stored input from a known-good backup, or re-ingest under a fresh ROOT",
            data={"input_id": aligned.input_id},
        )
    return manifest["reference_id"], manifest["reference_sha256"]


def _declared_platform(chunkset: ChunkSet) -> str:
    declared = {from_rg_lines(chunk.rg_lines) for chunk in chunkset.chunks}
    if len(declared) != 1 or "unknown" in declared:
        raise LabProblem(
            "TBX-LAB-024", cause="BAM read groups have no unanimous supported platform declaration",
            fix="Verify instrument provenance and regenerate a BAM with truthful unanimous PL:ONT or PL:ILLUMINA read groups",
        )
    platform = next(iter(declared))
    if not chunkset.aligned and platform != "ont":
        raise LabProblem(
            "TBX-LAB-024", cause="unaligned Illumina input cannot use the ONT alignment path",
            fix="Align the short reads externally and supply a prealigned BAM",
        )
    return platform


def _stored_input(store: LabStore, source_id: str) -> tuple[AlignedInput, dict[str, Any]]:
    record = source_record(store, source_id) or {}
    for entry in sorted(record.get("inputs", []), key=lambda item: item.get("used_utc", ""), reverse=True):
        manifest = input_manifest(store, entry["input_id"])
        if manifest is not None:
            return (
                AlignedInput(
                    entry["input_id"], source_id, store.inputs_dir / entry["input_id"], manifest, reused=True
                ),
                record,
            )
    raise LabProblem(
        "TBX-LAB-017",
        cause="the sample has no aligned input left under this ROOT",
        fix="Rerun with the chunk paths: `traceback lab run <chunk dir>`",
        data={"candidates": []},
    )


def _aligner_change(store: LabStore, source_id: str, tools: dict[str, str | None]) -> dict[str, Any] | None:
    record = source_record(store, source_id)
    if not record:
        return None
    previous = [entry for entry in record.get("inputs", []) if entry.get("kind") == "aligned"]
    if not previous:
        return None
    last = max(previous, key=lambda entry: entry.get("used_utc", ""))
    if last.get("tools") == tools:
        return None
    return {"old": last.get("tools"), "new": tools}


def _aligner_warning(change: dict[str, Any], low: float, high: float) -> dict[str, Any]:
    old = change["old"] if isinstance(change["old"], dict) else {}
    new = change["new"] if isinstance(change["new"], dict) else {}
    versions = ", ".join(
        f"{tool} {old.get(tool) or 'unknown'} -> {new.get(tool) or 'unknown'}"
        for tool in sorted(set(old) | set(new))
    )
    problem = LabProblem(
        "TBX-LAB-019",
        cause=(
            f"aligner versions changed: {versions}; "
            f"estimated realignment {low:.0f}-{high:.0f} min"
        ),
        fix=(
            "No action is required for this run. Use --dry-run before future runs "
            "and pin aligner versions to keep cohorts comparable."
        ),
    )
    return {
        "summary": problem.summary,
        **problem_payload("lab run", problem)["data"],
        "old": change["old"],
        "new": change["new"],
        "estimate_min": [round(low, 1), round(high, 1)],
    }


def _memory_warning() -> dict[str, Any] | None:
    from .doctor import MIN_RAM, _ram

    try:
        physical_ram = _ram()
    except Exception:  # noqa: BLE001 - an advisory probe must never abort a run
        return None
    if physical_ram is None or physical_ram >= MIN_RAM:
        return None
    recommended_gib = MIN_RAM // GIB
    physical_gib = physical_ram / GIB
    return {
        "kind": "low_memory",
        "code": "TBX-LAB-025",
        "cause": f"physical RAM is {physical_gib:g} GiB, below the {recommended_gib} GiB alignment recommendation",
        "retryable": False,
        "docs": "docs/LAB-GUIDE.md#tbx-lab-025",
        "summary": f"Less than {recommended_gib} GiB of physical RAM detected; alignment may fail or be slow",
        "physical_ram_bytes": physical_ram,
        "recommended_bytes": MIN_RAM,
        "fix": f"Use a machine with at least {recommended_gib} GiB RAM for a full hg38 alignment",
    }


def _memory_warning_lines(warning: dict[str, Any]) -> list[str]:
    return [
        f"WARN      {warning['code']}: {warning['summary']}",
        *problem_lines(LabProblem(
            warning["code"], cause=warning["cause"], fix=warning["fix"],
        )),
    ]


def _dry_run_read_summary(store: LabStore, input_id: str, identity: str) -> Any:
    """Read one selected summary without crossing a cohort selector change."""

    for _ in range(3):
        try:
            with store.selection_read_context():
                return store.read_result(input_id, "read-summary", identity)
        except SelectionSnapshotChanged:
            continue
        except ResultOrderUnavailable as exc:
            raise LabProblem(
                "TBX-LAB-022",
                cause="the selected read-summary or cohort selection metadata is unreadable",
                fix="Restore the affected selection metadata from a known-good backup, then retry the dry run",
            ) from exc
    raise LabProblem(
        "TBX-LAB-012",
        summary="Selected result changed during dry run",
        cause="a concurrent cohort update changed the selected read-summary during this preview",
        fix="Retry the dry run after the cohort update finishes",
    )


def _dry_run(args: argparse.Namespace, store: LabStore, progress: Callable[[str], None]) -> tuple[int, dict[str, Any]]:
    store.check_version()
    from .pon_consumer import require_built_cohort, require_cohort_input

    entries = registry.registry_entries()
    items = registry.plan([alg for _, alg in entries], only=_only(args.only), profile=args.profile)
    plan_rows = [
        {"algorithm_id": item.algorithm.id, "upstream": item.upstream_only, "decision": "decided after alignment"}
        for item in items
    ]
    memory_warning = _memory_warning()
    data: dict[str, Any] = {"root": str(store.root), "dry_run": True, "plan": plan_rows,
                            "warnings": [memory_warning] if memory_warning else []}
    lines = [f"ROOT      {store.root}"]
    if memory_warning:
        lines.extend(_memory_warning_lines(memory_warning))
    alignment_required = False
    example = bool(args.example)

    def mark_platform_skips(platform: str) -> None:
        for row, item in zip(plan_rows, items):
            if platform not in item.algorithm.requires.platforms:
                row["decision"] = "skip expected (platform)"
        if args.controls and platform != "ont":
            data["controls"] = {"cna_skipped": "platform"}
            lines.append("CONTROL   CNA spike not built (platform)")

    if len(args.inputs) == 1 and _is_stored(args.inputs[0]):
        source_id = resolve_sample(store, args.inputs[0])
        aligned, record = _stored_input(store, source_id)
        platform = _stored_platform(aligned)
        example = bool(record.get("example"))
        reference_id, reference_sha256 = _stored_reference_binding(aligned)
        registered = resolve_reference(store.root, reference_id, example=True)
        if registered.sha256 != reference_sha256:
            raise LabProblem(
                "TBX-LAB-004",
                cause="stored input and current reference registration have different SHA-256 values",
                fix="Restore the original reference registration for this stored input",
            )
        verify_reference_source(store, registered, write_cache=False)
        with_sex_assertion(record, args.sex)
        data["sample"] = {
            "id12": id12_for(store, source_id), "input_id": aligned.input_id, "platform": platform,
        }
        data["reference_id"] = reference_id
        data["alignment"] = "reused (stored sample)"
        excluded = record.get("excluded", [])
        if not isinstance(excluded, list) or not all(
            isinstance(index, int) and not isinstance(index, bool) and index >= 0
            for index in excluded
        ):
            excluded = None
        chunks = record.get("chunks")
        if isinstance(chunks, list) and chunks and all(
            isinstance(chunk, dict) and isinstance(chunk.get("size"), int)
            and not isinstance(chunk["size"], bool) and chunk["size"] >= 0
            for chunk in chunks
        ):
            data["chunks"] = {
                "count": len(chunks), "bytes": sum(chunk["size"] for chunk in chunks),
                "excluded": excluded,
            }
        else:
            data["chunks"] = {"count": None, "bytes": None, "excluded": excluded}
        data["budget"] = {
            "required_bytes": 0,
            "free_bytes": shutil.disk_usage(store.root).free,
            "parts": {},
            "scope": "ingest and alignment (reused)",
            "analysis_scratch": "not predicted",
        }
        lines.append(f"SAMPLE    {id12_for(store, source_id)} (stored)")
        lines.append(f"REFERENCE {data['reference_id']}")
        suffix = " · exclusions unknown" if excluded is None else (f" · excluded {excluded}" if excluded else "")
        if data["chunks"]["count"] is not None:
            lines.append(
                f"INPUT     {data['chunks']['count']} stored chunk(s) · "
                f"{data['chunks']['bytes'] / GIB:.2f} GB{suffix}"
            )
        else:
            lines.append(f"INPUT     unknown (stored chunk record incomplete){suffix}")
        lines.append("ALIGN     reused (stored sample)")
        lines.append(
            f"DISK      ingest/align needs 0 GB, has {data['budget']['free_bytes'] / GIB:.1f} GB "
            "(reused; not a sufficiency check)"
        )
        lines.append("ANALYSIS  scratch disk not predicted")
        mark_platform_skips(platform)
        summary = next((item.algorithm for item in items if item.algorithm.id == "read-summary"), None)
        if summary is not None and not args.force:
            identity = execution_identity(
                summary, reference_sha256=reference_sha256,
                platform=platform if platform != "ont" else None,
            )
            selected = _dry_run_read_summary(store, aligned.input_id, identity)
            if selected is not None and selected.status == "ok":
                plan_rows[next(index for index, item in enumerate(items) if item.algorithm.id == "read-summary")][
                    "decision"
                ] = "reused (selected result)"
                values = {metric.metric_id: metric.value for metric in selected.metrics}
                eligible = values.get("eligible_reads")
                depth_x = values.get("approx_depth_x")
                if (
                    isinstance(eligible, int) and not isinstance(eligible, bool) and eligible >= 0
                    and isinstance(depth_x, (int, float)) and not isinstance(depth_x, bool)
                ):
                    data["eligible_reads"] = eligible
                    lines.append(f"DEPTH     {eligible:,} eligible reads (selected read-summary)")
                    for row, item in zip(plan_rows, items):
                        if row["decision"] != "decided after alignment":
                            continue
                        minimum = item.algorithm.requires.min_eligible_reads
                        if minimum and eligible < minimum:
                            row["decision"] = "skip expected below depth minimum (unless cached)"
                            row["eligible_reads"] = eligible
                            row["minimum_eligible_reads"] = minimum
                        elif minimum:
                            row["decision"] = "depth minimum met; other checks at run time"
        for row in plan_rows:
            if row["decision"] == "decided after alignment":
                row["decision"] = "decided at run time"
    else:
        paths = collect_paths(args.inputs)
        chunkset = inspect_chunks(paths, store, skip_bad_chunks=args.skip_bad_chunks, write_cache=False)
        input_platform = _declared_platform(chunkset)
        platform = input_platform if chunkset.aligned else "ont"
        data["sample"] = {"source_id": chunkset.source_id,
                          "platform": platform}
        mark_platform_skips(platform)
        with_sex_assertion(source_record(store, chunkset.source_id) or {}, args.sex)
        reference = resolve_reference(store.root, args.reference_id)
        verify_reference_source(store, reference, write_cache=False)
        data["reference_id"] = reference.reference_id
        data["chunks"] = {"count": len(chunkset.chunks), "bytes": chunkset.total_bytes, "excluded": chunkset.excluded_indexes}
        lines.append(f"REFERENCE {reference.reference_id}")
        lines.append(
            f"INPUT     {len(chunkset.chunks)} chunk(s) · {chunkset.total_bytes / GIB:.2f} GB · "
            f"{chunkset.total_records:,} records" + (f" · excluded {chunkset.excluded_indexes}" if chunkset.excluded else "")
        )
        mmi_needed = not chunkset.aligned and args.mmi is None and not mmi_path(store, reference).is_file()
        budget = disk_budget(
            store, chunkset, mmi_needed=mmi_needed, fasta_bytes=reference.loaded.source.fasta_size_bytes
        )
        data["budget"] = budget.public()
        lines.append(
            f"DISK      needs {budget.required / GIB:.1f} GB, has {budget.free / GIB:.1f} GB "
            f"({'ok' if budget.ok else 'SHORT: TBX-LAB-010'})"
        )
        if chunkset.aligned:
            data["alignment"] = "not needed (aligned input)"
            lines.append("ALIGN     not needed (aligned input)")
        else:
            samtools, minimap2 = find_samtools(), find_minimap2()
            low, high = estimate_align_minutes(chunkset.total_bytes)
            reason = "new source"
            existing = (
                _check_regular(args.mmi.absolute(), 0, args.mmi.name)
                if args.mmi is not None else mmi_path(store, reference)
            )
            if samtools.version and minimap2.version and existing.is_file():
                cache = store.load_hash_cache()
                key = store.strong_hash_cache_key(existing)
                mmi_sha = cache.get(key, {}).get("sha256")
                previous = source_record(store, chunkset.source_id) or {}
                previous_inputs = previous.get("inputs", [])
                has_aligned_input = any(
                    isinstance(row, dict) and row.get("kind") == "aligned"
                    for row in previous_inputs
                ) if isinstance(previous_inputs, list) else False
                if not mmi_sha and has_aligned_input:
                    reason = "reuse unknown until index hashed"
                elif mmi_sha:
                    input_id = aligned_input_id(
                        source_id=chunkset.source_id,
                        reference_sha256=reference.sha256,
                        mmi_sha256=mmi_sha,
                        minimap2_version=str(minimap2.version),
                        samtools_version=str(samtools.version),
                        aligner_argv=aligner_argv_identity(chunkset.rg_lines),
                    )
                    if input_manifest(store, input_id) is not None:
                        reason = "reused"
                    elif _aligner_change(store, chunkset.source_id, {"samtools": samtools.version, "minimap2": minimap2.version}):
                        reason = "aligner identity changed (TBX-LAB-019): realigns"
            data["alignment"] = reason
            alignment_required = reason != "reused"
            data["estimate_min"] = [round(low, 1), round(high, 1)]
            if reason == "reused":
                estimate = ""
            elif reason == "reuse unknown until index hashed":
                estimate = f" · if realignment needed, estimate {low:.0f}-{high:.0f} min"
            else:
                estimate = f" · estimate {low:.0f}-{high:.0f} min"
            lines.append(
                f"ALIGN     {reason}" + estimate
                + (" · minimap2 index built first" if mmi_needed else "")
            )
    if args.cohort is not None:
        require_cohort_input(args.cohort, platform=platform, example=example)
        cohort = require_built_cohort(store, args.cohort)
        compatibility = "after alignment" if alignment_required else "during run"
        data["cohort"] = {
            "id": cohort.cohort_id, "version": cohort.version_id,
            "panel": "built", "compatibility": compatibility,
        }
        lines.append(f"COHORT    {cohort.version_id} (built; compatibility checked {compatibility})")
    for row in plan_rows:
        lines.append(f"PLAN      {row['algorithm_id']}{' (upstream)' if row['upstream'] else ''}: {row['decision']}")
    if not plan_rows:
        lines.append("PLAN      no algorithms registered")
    payload = _local_envelope(_result("lab run", "ok", "dry run: nothing written", data=data))
    return EXIT_OK, _with_human(payload, lines, replace_data=True)


def _preflight_budget(args: argparse.Namespace, store: LabStore, *, example_needs_generation: bool) -> None:
    """Refuse a known disk shortfall before the writer creates Lab bookkeeping.

    The cheap bound avoids a second chunk hash on ordinary runs. A possible
    shortfall gets the exact, read-only reuse and D30 check. The writer still
    repeats validation under its lock because inputs and capacity can change.
    """

    if example_needs_generation:
        from .doctor import EXAMPLE_BYTES

        budget = Budget({"example": EXAMPLE_BYTES}, shutil.disk_usage(_existing_ancestor(store.root)).free)
        require_budget(budget, "example")
        return
    if len(args.inputs) == 1 and _is_stored(args.inputs[0]):
        return

    paths = collect_paths(args.inputs)
    reference = resolve_reference(store.root, args.reference_id, example=args.example)
    # Reading one BAM header is cheap. Only one aligned BAM is valid; otherwise
    # the unaligned formula bounds every accepted chunk without hashing it.
    chunks = [Chunk(path=path, name=path.name, index=None, size=path.stat().st_size) for path in paths]
    if len(chunks) == 1:
        try:
            _read_header(chunks[0])
        except Exception:  # noqa: BLE001 - this optional probe must not replace the ingest error
            # Let the normal ingestion path report a malformed BAM.
            chunks[0].aligned = False
    known_mmi = args.mmi if args.mmi is not None else mmi_path(store, reference)
    upper = disk_budget(
        store, ChunkSet(chunks), mmi_needed=not known_mmi.is_file(),
        fasta_bytes=reference.loaded.source.fasta_size_bytes,
    )
    if upper.ok:
        return

    chunkset = inspect_chunks(paths, store, skip_bad_chunks=args.skip_bad_chunks, write_cache=False)
    input_platform = _declared_platform(chunkset)
    with_sex_assertion(source_record(store, chunkset.source_id) or {}, args.sex)
    verify_reference_source(store, reference, write_cache=False)
    if chunkset.aligned:
        candidate = prealigned_input_id(
            source_id=chunkset.source_id, reference_sha256=reference.sha256,
            platform=input_platform,
        )
        if input_manifest(store, candidate) is not None:
            return
        require_budget(disk_budget(store, chunkset, mmi_needed=False, fasta_bytes=0), "ingest")
        return

    samtools, minimap2 = require_aligner_tools()
    if known_mmi.is_file():
        candidate = aligned_input_id(
            source_id=chunkset.source_id,
            reference_sha256=reference.sha256,
            mmi_sha256=cached_sha256(store, known_mmi, write_cache=False),
            minimap2_version=str(minimap2.version),
            samtools_version=str(samtools.version),
            aligner_argv=aligner_argv_identity(chunkset.rg_lines),
        )
        if input_manifest(store, candidate) is not None:
            return
    require_budget(
        disk_budget(
            store, chunkset, mmi_needed=not known_mmi.is_file(),
            fasta_bytes=reference.loaded.source.fasta_size_bytes,
        ),
        "align",
    )


def run_command(args: argparse.Namespace, store: LabStore) -> tuple[int, dict[str, Any]]:
    example_needs_generation = False
    if args.example:
        if args.cohort is not None:
            raise LabProblem(
                "TBX-LAB-021", cause="an EXAMPLE input cannot use a real cohort panel",
                fix="Omit --cohort for EXAMPLE, or use a real ONT input",
                data={"cohort_id": args.cohort, "field": "EXAMPLE"},
            )
        if args.inputs:
            raise LabProblem(
                "TBX-LAB-005",
                cause="--example takes no input paths",
                fix="Run `traceback lab run --example` on its own",
                data={"argument": 1, "kind": "an input given with --example"},
            )
        from .example import example_dir, prepared

        store.check_version()
        example_needs_generation = not prepared(store.root)
        if args.dry_run and example_needs_generation:
            memory_warning = _memory_warning()
            payload = _local_envelope(
                _result("lab run", "ok", "dry run: nothing written", data={
                    "root": str(store.root), "dry_run": True, "example": True,
                    "alignment": "example not generated yet: generated and aligned on first run (~200k reads, under a minute)",
                    "warnings": [memory_warning] if memory_warning else [],
                })
            )
            lines = [f"ROOT      {store.root}", "EXAMPLE   not generated yet: generated and aligned on first run (~200k reads, under a minute)"]
            if memory_warning:
                lines.extend(_memory_warning_lines(memory_warning))
            return EXIT_OK, _with_human(payload, lines, replace_data=True)
        # Generation and registration happen under the writer lock (below).
        args.inputs = [str(example_dir(store.root) / "chunks")]
        args.reference_id = EXAMPLE_REFERENCE_ID
    if not args.inputs:
        return _usage_quickstart(store)
    from .simulate_command import parse_fractions

    fractions = () if args.no_downsample else parse_fractions(args.downsample, "--downsample")
    tfs = parse_fractions(args.tf, "--tf") if args.controls else ()
    progress = _progress(args.as_json)
    if args.dry_run:
        return _dry_run(args, store, progress)
    store.check_version()
    entries = registry.registry_entries()
    by_id = {alg.id: entry for entry, alg in entries}
    items = registry.plan([alg for _, alg in entries], only=_only(args.only), profile=args.profile)
    _preflight_budget(args, store, example_needs_generation=example_needs_generation)
    run_id = new_run_id()
    started = time.monotonic()
    progress(f"ROOT  {store.root}")
    previous_term = signal.signal(signal.SIGTERM, _interrupted_handler)
    try:
        with store.writer("run", run_id, step="ingest") as lock:
            log = store.run_log(run_id)
            log.append("run_start", command="run", pid=_pid(), flags=_flags(args))
            try:
                stored = len(args.inputs) == 1 and _is_stored(args.inputs[0])
                chunkset: ChunkSet | None = None
                warnings: list[dict[str, Any]] = []
                memory_warning = _memory_warning()
                if memory_warning:
                    warnings.append(memory_warning)
                    for line in _memory_warning_lines(memory_warning):
                        progress(line)
                example = False
                if stored:
                    source_id = resolve_sample(store, args.inputs[0])
                    aligned, record = _stored_input(store, source_id)
                    _stored_platform(aligned)
                    reference_id, reference_sha256 = _stored_reference_binding(aligned)
                    registered = resolve_reference(store.root, reference_id, example=True)
                    if registered.sha256 != reference_sha256:
                        raise LabProblem(
                            "TBX-LAB-004",
                            cause="stored input and current reference registration have different SHA-256 values",
                            fix="Restore the original reference registration for this stored input",
                        )
                    verify_reference_source(store, registered, write_cache=True)
                    record = record_source_sex(store, source_id, args.sex)
                    example = bool(record.get("example"))
                    scrubber = Scrubber(root=store.root)
                    progress(f"ingest                   stored sample {id12_for(store, source_id)}")
                else:
                    log.append("step_start", step="ingest")
                    if args.example:
                        from .example import prepare

                        progress(f"{'example':<24} generating the synthetic reference and chunks (once)")
                        prepare(store.root)
                    paths = collect_paths(args.inputs)
                    scrubber = Scrubber(root=store.root, chunk_stems=[p.name[:-4] for p in paths])
                    chunkset = inspect_chunks(
                        paths, store, skip_bad_chunks=args.skip_bad_chunks, progress=progress
                    )
                    input_platform = _declared_platform(chunkset)
                    with_sex_assertion(source_record(store, chunkset.source_id) or {}, args.sex)
                    reference = resolve_reference(store.root, args.reference_id, example=args.example)
                    reference_proof = verify_reference_source(store, reference, write_cache=True)
                    reference_id, reference_sha256 = reference.reference_id, reference.sha256
                    example = bool(args.example)
                    progress(
                        f"ingest                   {len(chunkset.chunks)} chunk(s) · "
                        f"{chunkset.total_records:,} records · reference {reference_id}"
                    )
                    log.append(
                        "step_end",
                        step="ingest",
                        status="ok",
                        chunks=len(chunkset.chunks),
                        records=chunkset.total_records,
                        excluded=chunkset.excluded_indexes,
                        source_id=chunkset.source_id,
                    )
                    lock.set_step("align")
                    log.append("step_start", step="align")
                    if chunkset.aligned:
                        prealigned_id = prealigned_input_id(
                            source_id=chunkset.source_id, reference_sha256=reference.sha256,
                            platform=input_platform,
                        )
                        if input_manifest(store, prealigned_id) is None:
                            budget = disk_budget(store, chunkset, mmi_needed=False, fasta_bytes=0)
                            require_budget(budget, "ingest")
                        aligned = ingest_prealigned(
                            store, chunkset, reference, reference_proof=reference_proof,
                            progress=progress,
                        )
                    else:
                        samtools, minimap2 = require_aligner_tools()
                        # Reuse needs no disk: decide reuse before the budget.
                        known_mmi = args.mmi if args.mmi is not None else mmi_path(store, reference)
                        reusable = False
                        if known_mmi.is_file():
                            candidate = aligned_input_id(
                                source_id=chunkset.source_id,
                                reference_sha256=reference.sha256,
                                mmi_sha256=cached_sha256(store, known_mmi),
                                minimap2_version=str(minimap2.version),
                                samtools_version=str(samtools.version),
                                aligner_argv=aligner_argv_identity(chunkset.rg_lines),
                            )
                            reusable = input_manifest(store, candidate) is not None
                        if not reusable:
                            budget = disk_budget(
                                store,
                                chunkset,
                                mmi_needed=not known_mmi.is_file(),
                                fasta_bytes=reference.loaded.source.fasta_size_bytes,
                            )
                            require_budget(budget, "align")
                        change = _aligner_change(
                            store, chunkset.source_id, {"samtools": samtools.version, "minimap2": minimap2.version}
                        )
                        mmi, mmi_sha = ensure_mmi(
                            store,
                            reference,
                            minimap2,
                            reference_proof=reference_proof,
                            override=args.mmi,
                            scrubber=scrubber,
                            timeout_s=ALIGN_TIMEOUT_S,
                            echo=_echo(args.verbose),
                            progress=progress,
                        )
                        verify_reference_source(
                            store, reference, write_cache=True, expected_proof=reference_proof,
                        )
                        if change is not None and not reusable:
                            low, high = estimate_align_minutes(chunkset.total_bytes)
                            warnings.append(_aligner_warning(change, low, high))
                            progress(
                                f"WARN TBX-LAB-019 aligner identity changed {change['old']} -> {change['new']}; "
                                f"realigning (~{low:.0f}-{high:.0f} min)"
                            )
                        aligned = align(
                            store,
                            chunkset,
                            reference,
                            samtools=samtools,
                            minimap2=minimap2,
                            mmi=mmi,
                            mmi_sha256=mmi_sha,
                            reference_proof=reference_proof,
                            threads=args.threads,
                            sort_mem=args.sort_mem,
                            timeout_s=ALIGN_TIMEOUT_S,
                            scrubber=scrubber,
                            echo=_echo(args.verbose),
                            progress=progress,
                        )
                    write_source(store, chunkset, aligned, example=example, declared_sex=args.sex)
                    align_info = aligned.manifest.get("align", {})
                    log.append(
                        "step_end",
                        step="align",
                        status="ok",
                        reused=aligned.reused,
                        input_id=aligned.input_id,
                        duration_s=None if aligned.reused else align_info.get("duration_s"),
                        peak_rss_bytes=None if aligned.reused else align_info.get("peak_rss_bytes"),
                        peak_disk_bytes=None if aligned.reused else align_info.get("peak_disk_bytes"),
                    )
                    progress(
                        f"align                    {'reused' if aligned.reused else 'done'} · input {aligned.input_id[:12]}"
                    )
                id12 = id12_for(store, aligned.source_id)
                target = _target_from_input(
                    aligned, reference_id, reference_sha256, example, store,
                    methylbert_bundle_sha256=getattr(args, "methylbert_bundle_sha256", None),
                    methylbert_code_sha256=getattr(args, "methylbert_code_sha256", None),
                    methylbert_min_classified_reads=getattr(args, "methylbert_min_classified", None),
                )
                log.append("sample", sample=id12, source_id=aligned.source_id, input_id=aligned.input_id,
                           platform=target.platform)
                from .pon_consumer import bind_target

                items = bind_target(store, items, target, aligned.manifest, cohort_id=args.cohort)
                results = run_plan(
                    store,
                    lock,
                    log,
                    target,
                    items,
                    by_id,
                    run_id=run_id,
                    force=args.force,
                    timeout_s=args.timeout_s,
                    scrubber=scrubber,
                    progress=progress,
                    echo=_echo(args.verbose),
                )
                controls = _derived_phase(
                    store, lock, log, target, items, by_id, results, args,
                    run_id=run_id, fractions=fractions, tfs=tfs, scrubber=scrubber, progress=progress,
                )
                results = [*results, *controls["results"]]
                code, payload = _summary(
                    store, run_id, aligned, id12, chunkset, results, warnings, time.monotonic() - started,
                    depth=target.depth.model_dump() if target.depth is not None else None,
                    controls=controls["summary"], platform=target.platform,
                )
                log.append("run_end", status=payload["status"], exit_code=code)
                return code, payload
            except KeyboardInterrupt:
                current = lock.step
                log.append("run_end", status="interrupted", exit_code=6, step=current)
                raise LabProblem(
                    "TBX-LAB-015",
                    cause=f"Interrupted during {current}. Completed results are kept.",
                    fix="Rerun the same command; only missing results recompute",
                    data={"step": current, "run_id": run_id},
                ) from None
            except LabProblem as problem:
                log.append("run_end", status="blocked", exit_code=problem.exit_code, code=problem.code, step=lock.step)
                raise
            except OSError as exc:
                if exc.errno != errno.ENOSPC:
                    log.append("run_end", status="internal_error", exit_code=7, error=type(exc).__name__, step=lock.step)
                    raise
                log.append("run_end", status="blocked", exit_code=3, code="TBX-LAB-011", step=lock.step)
                raise LabProblem(
                    "TBX-LAB-011",
                    cause=f"the volume filled up during {lock.step}; staging removed",
                    fix="Free space, or use --root on a larger volume; then rerun",
                    data={"step": lock.step, "volume": "ROOT"},
                ) from exc
            except Exception as exc:
                log.append("run_end", status="internal_error", exit_code=7, error=type(exc).__name__, step=lock.step)
                raise
    finally:
        signal.signal(signal.SIGTERM, previous_term)


def _derived_phase(
    store: LabStore,
    lock: Any,
    log: Any,
    target: RunTarget,
    items: Any,
    by_id: dict[str, str],
    results: list[StepResult],
    args: argparse.Namespace,
    *,
    run_id: str,
    fractions: tuple[float, ...],
    tfs: tuple[float, ...],
    scrubber: Scrubber,
    progress: Callable[[str], None],
) -> dict[str, Any]:
    """Default downsample curve and positive controls (D24, D25, D36)."""

    from .controls import cna_specs, run_derived, split_specs, synthetic_summary
    from .derived import downsample

    summary: dict[str, Any] = {"downsample": list(fractions), "split": False, "cna": [], "cna_skipped": None}
    read_summary = next((r for r in results if r.algorithm_id == "read-summary" and r.status == "ok"), None)
    if read_summary is None:
        summary["note"] = "read-summary not ok; no derived inputs"
        return {"results": [], "summary": summary}
    specs = [downsample(fraction) for fraction in fractions]
    if args.controls:
        specs.extend(split_specs())
        summary["split"] = True
        if target.platform == "ont":
            built = cna_specs(store.root, target.input_dir / "aligned.bam", tfs)
            if isinstance(built, str):
                summary["cna_skipped"] = "example_reference" if target.example else built
            else:
                specs.extend(built)
        else:
            summary["cna_skipped"] = "platform"
    if not specs:
        return {"results": [], "summary": summary}
    runs = run_derived(
        store, lock, log, target, specs, items, by_id, run_id=run_id, read_summary=read_summary,
        force=args.force, timeout_s=args.timeout_s, scrubber=scrubber, progress=progress,
        echo=_echo(args.verbose),
        parent_upstream=_parent_intermediates(items, results),
    )
    summary["cna"] = synthetic_summary(store, runs)
    return {"results": [r for run in runs for r in run.results], "summary": summary}


def _parent_intermediates(items: Any, results: list[StepResult]) -> dict[str, StepResult]:
    """Ok full-input results of intermediates (D33: derived inputs reuse them)."""

    names = registry.intermediate_ids([item.algorithm for item in items])
    return {r.algorithm_id: r for r in results if r.status == "ok" and r.input_label == "full" and r.algorithm_id in names}


def _pid() -> int:
    import os

    return os.getpid()


def _duration(seconds: float) -> str:
    seconds = round(seconds)
    if seconds < 60:
        return f"{seconds}s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {seconds:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


def _skip_remedy(algorithm_id: str, reason: str, root: Path) -> dict[str, Any] | None:
    """Give each missing dependency its own recovery action, including Lab assets."""

    argv: list[str]
    asset: str
    if reason == "missing_toolchain":
        tool = {"copy-number": "ichor"}.get(algorithm_id)
        if tool is None:
            return None
        asset, argv = tool, ["traceback", "toolchain", "install", tool]
    elif reason == "missing_asset" and algorithm_id == "copy-number":
        asset = "ichorCNA toolchain assets"
        argv = [
            "traceback", "method-asset", "register", "--from-toolchain",
            "copy-number", "--root", str(root),
        ]
    elif reason == "missing_asset" and algorithm_id == "cell-origin":
        asset = "Loyfer atlas"
        argv = [
            "traceback", "method-asset", "register", "--from-dir",
            "LOYFER_DIR", "--root", str(root),
        ]
    elif reason == "missing_asset" and algorithm_id == "pmd-score":
        asset = "Zhou 2018 hg38 commonPMD BED"
        argv = [
            "traceback", "lab", "asset", "add", "pmd", "--file",
            "YOUR_LOCAL_PMD_coordinates_hg38.bed.gz", "--source-label",
            "Zhou-2018-operator-supplied", "--terms", "unknown", "--root", str(root),
        ]
    elif reason == "missing_asset" and algorithm_id == "meth-tf":
        asset = "compatible tumour/healthy methylation marker reference"
        argv = [
            "traceback", "lab", "asset", "add", "meth-tf-reference", "--file",
            "YOUR_LOCAL_MARKER_REFERENCE.json", "--source-label", "operator-marker-panel",
            "--terms", "unknown", "--root", str(root),
        ]
    else:
        return None
    return {"asset": asset, "argv": argv, "command": shlex.join(argv)}

def _summary(
    store: LabStore,
    run_id: str,
    aligned: AlignedInput,
    id12: str,
    chunkset: ChunkSet | None,
    results: list[StepResult],
    warnings: list[dict[str, Any]],
    elapsed: float,
    *,
    depth: dict[str, Any] | None = None,
    controls: dict[str, Any] | None = None,
    platform: str = "ont",
) -> tuple[int, dict[str, Any]]:
    ok = sum(result.status == "ok" for result in results)
    skipped: dict[str, int] = {}
    for result in results:
        if result.status == "skipped" and result.skip_reason:
            skipped[result.skip_reason] = skipped.get(result.skip_reason, 0) + 1
    failed = [result for result in results if result.status == "failed"]
    records = aligned.manifest.get("records", {})
    reads = records.get("ubam") or records.get("aligned_primary_plus_unmapped")
    chunks = aligned.manifest.get("chunks")
    next_argv = [["traceback", "lab", "serve", "--open"]]
    if failed:
        next_argv.insert(0, ["traceback", "lab", "runs", run_id, "--step", failed[0].algorithm_id])
    remedies: list[dict[str, Any]] = []
    seen_remedies: set[tuple[str, str]] = set()
    for result in results:
        if result.status != "skipped" or result.skip_reason is None:
            continue
        key = (result.algorithm_id, result.skip_reason)
        if key in seen_remedies:
            continue
        seen_remedies.add(key)
        remedy = _skip_remedy(*key, store.root)
        if remedy is not None:
            remedies.append({"algorithm_id": key[0], "reason": key[1], **remedy})
    data: dict[str, Any] = {
        "root": str(store.root),
        "run_id": run_id,
        "sample": {"id12": id12, "source_id": aligned.source_id, "input_id": aligned.input_id,
                   "platform": platform},
        "depth": depth,
        "counts": {"ok": ok, "skipped": skipped, "failed": len(failed)},
        "failed": [{"algorithm_id": result.algorithm_id, "first_line": result.first_line} for result in failed],
        "steps": [result.public() for result in results],
        "controls": controls,
        "excluded_chunks": aligned.manifest.get("excluded_chunks", []),
        "warnings": warnings,
        "remediations": remedies,
        "next": next_argv,
    }
    skipped_text = ", ".join(
        f"{count} skipped ({reason}: "
        + ", ".join(dict.fromkeys(r.algorithm_id for r in results if r.status == "skipped" and r.skip_reason == reason))
        + ")"
        for reason, count in sorted(skipped.items())
    )
    done = f"{_duration(elapsed)} · {ok} ok" + (f" · {skipped_text}" if skipped_text else "") + (
        f" · {len(failed)} failed" if failed else ""
    )
    sample_line = f"{id12}   " + " · ".join(
        part
        for part in (
            f"{chunks} chunks" if chunks else None,
            f"{reads:,} reads" if isinstance(reads, int) else None,
            f"{depth['approx_depth_x']:.3g}x eligible depth" if depth else "depth not measured",
        )
        if part
    )
    lines = [
        f"ROOT    {store.root}",
        f"RUN     {run_id}",
        f"SAMPLE  {sample_line}",
        f"DONE    {done}",
    ]
    if controls is not None:
        parts = []
        if controls.get("downsample"):
            parts.append("depth curve at " + "/".join(f"{f:g}" for f in controls["downsample"]))
        if controls.get("cna"):
            parts.append("CNA spike at " + "/".join(f"{row['tf_expected']:g}" for row in controls["cna"]))
        elif controls.get("cna_skipped"):
            parts.append(f"CNA spike not built ({controls['cna_skipped']})")
        if controls.get("split"):
            parts.append("split halves A/B")
        if parts:
            lines.append("        controls: " + " · ".join(parts))
        for row in controls.get("cna", []):
            recovered = "-" if row["tf_recovered"] is None else f"{row['tf_recovered']:.3f}"
            verdict = "recovered" if row["recovered_within_tolerance"] else "not recovered"
            lines.append(f"        spike TF {row['tf_expected']:g} -> {recovered} ({verdict})")
    if aligned.manifest.get("excluded_chunks"):
        lines.append(f"        provisional: excluded chunks {aligned.manifest['excluded_chunks']}")
    for result in failed:
        lines.append(f"FAILED  {result.algorithm_id}: {result.first_line}")
        lines.append(f"        details: traceback lab runs {run_id} --step {result.algorithm_id}")
    for warning in warnings:
        if warning.get("kind") == "low_memory":
            continue  # already shown before alignment, including the next action
        lines.append(f"WARN    {warning['summary']}")
        lines.extend(problem_lines(LabProblem(
            warning["code"], cause=warning["cause"], fix=warning["fix"],
        )))
    partial_problem = None
    if failed:
        partial_problem = LabProblem(
            "TBX-LAB-016",
            cause=f"{len(failed)} algorithm(s) failed: " + ", ".join(r.algorithm_id for r in failed),
            fix=(
                f"`traceback lab runs {run_id} --step {failed[0].algorithm_id}`; "
                "rerun recomputes only failed results"
            ),
        )
        lines.extend(problem_lines(partial_problem))
    for remedy in remedies:
        lines.append(
            f"SKIP    {remedy['algorithm_id']} skipped ({remedy['reason']}): "
            f"{remedy['asset']}: {remedy['command']}"
        )
    for argv in next_argv:
        lines.append(f"NEXT    {' '.join(argv)}")
    summary = f"run {run_id}: {ok} ok, {sum(skipped.values())} skipped, {len(failed)} failed"
    if partial_problem is not None:
        data.update(problem_payload("lab run", partial_problem)["data"])
        payload = _local_envelope(_result("lab run", "partial_failure", summary, data=data))
        return EXIT_PARTIAL, _bare(_with_human(payload, lines, replace_data=True))
    payload = _local_envelope(_result("lab run", "ok", summary, data=data))
    return EXIT_OK, _bare(_with_human(payload, lines, replace_data=True))


def _bare(payload: dict[str, Any]) -> dict[str, Any]:
    payload["_human"]["bare"] = True
    return payload


__all__ = ["add_run_parser", "run_command"]
