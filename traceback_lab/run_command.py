"""``traceback lab run``: ingest, align, run the registry, summarize (D9, D20, D22, D36)."""

from __future__ import annotations

import argparse
import errno
import shlex
import signal
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from traceback_runner.cli import _local_envelope, _result, _with_human

from . import registry
from .ingest import (
    AlignedInput,
    ChunkSet,
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
    align,
    require_budget,
    resolve_reference,
    resolve_sample,
    record_source_sex,
    source_record,
    write_source,
    aligner_argv_identity,
    cached_sha256,
    GIB,
    EXAMPLE_REFERENCE_ID,
)
from .identity import aligned_input_id, prealigned_input_id
from .platforms import for_input, from_rg_lines
from .problems import (
    EXIT_OK, EXIT_PARTIAL, EXIT_USAGE, LabProblem, problem_lines, problem_payload,
)
from .runner import DEFAULT_TIMEOUT_S, RunTarget, StepResult, run_plan
from .scrub import Scrubber
from .sample_metadata import with_sex_assertion
from .store import LabStore, new_run_id
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
    run.add_argument("--cohort", metavar="ID", help="use this built protocol-matched cohort's panel of normals")
    run.add_argument("--downsample", default="0.5,0.25,0.1", metavar="F,F,F", help="depth-curve fractions (default 0.5,0.25,0.1)")
    run.add_argument("--no-downsample", action="store_true", help="skip the depth curve")
    run.add_argument("--controls", dest="controls", action="store_true", default=True, help="run the positive controls (default)")
    run.add_argument("--no-controls", dest="controls", action="store_false", help="skip the CNA spike and split halves")
    run.add_argument("--tf", default="0.03,0.10,0.30", metavar="TF,TF", help="synthetic CNA spike tumour fractions")
    run.add_argument("--skip-bad-chunks", action="store_true", help="exclude truncated chunks (provisional)")
    run.add_argument("--sex", choices=("M", "F"), help="operator-declared sample sex for a read-time pre-analytic check")
    run.add_argument("--force", action="store_true", help="recompute every result")
    run.add_argument("--timeout-s", type=float, default=DEFAULT_TIMEOUT_S, metavar="S")
    run.add_argument("--threads", type=_threads, metavar="N", help="alignment threads (default min(8, cores-2))")
    run.add_argument("--sort-mem", type=_sort_mem, default="1G", metavar="MEM", help="samtools sort memory per thread (e.g. 768M, 2G)")
    run.add_argument("--mmi", type=Path, metavar="PATH", help="reuse an existing minimap2 index")
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
    }


def _is_stored(text: str) -> bool:
    return is_sample_reference(text) and not Path(text).exists()


def _target_from_input(
    aligned: AlignedInput, reference_id: str, reference_sha256: str, example: bool, store: LabStore
) -> RunTarget:
    extras: dict[str, Any] = {}
    try:
        extras["reference_fasta"] = str(resolve_reference(store.root, reference_id, example=True).fasta)
    except LabProblem:
        pass  # algorithms that need the FASTA fail or skip on their own
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


def _declared_platform(chunkset: ChunkSet) -> str:
    declared = {from_rg_lines(chunk.rg_lines) for chunk in chunkset.chunks}
    if len(declared) != 1 or "unknown" in declared:
        raise LabProblem(
            "TBX-LAB-024", cause="BAM read groups have no unanimous supported platform declaration",
            fix="Use input whose read groups all declare PL:ONT or PL:ILLUMINA",
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


def _dry_run(args: argparse.Namespace, store: LabStore, progress: Callable[[str], None]) -> tuple[int, dict[str, Any]]:
    store.check_version()
    entries = registry.registry_entries()
    items = registry.plan([alg for _, alg in entries], only=_only(args.only), profile=args.profile)
    plan_rows = [
        {"algorithm_id": item.algorithm.id, "upstream": item.upstream_only, "decision": "decided after alignment"}
        for item in items
    ]
    data: dict[str, Any] = {"root": str(store.root), "dry_run": True, "plan": plan_rows}
    lines = [f"ROOT      {store.root}"]
    if len(args.inputs) == 1 and _is_stored(args.inputs[0]):
        source_id = resolve_sample(store, args.inputs[0])
        with_sex_assertion(source_record(store, source_id) or {}, args.sex)
        aligned, _ = _stored_input(store, source_id)
        data["sample"] = {"id12": id12_for(store, source_id), "input_id": aligned.input_id,
                          "platform": _stored_platform(aligned)}
        data["alignment"] = "reused (stored sample)"
        lines.append(f"SAMPLE    {id12_for(store, source_id)} (stored)")
        lines.append("ALIGN     reused (stored sample)")
    else:
        paths = collect_paths(args.inputs)
        chunkset = inspect_chunks(paths, store, skip_bad_chunks=args.skip_bad_chunks, write_cache=False)
        input_platform = _declared_platform(chunkset)
        data["sample"] = {"source_id": chunkset.source_id,
                          "platform": input_platform if chunkset.aligned else "ont"}
        with_sex_assertion(source_record(store, chunkset.source_id) or {}, args.sex)
        reference = resolve_reference(store.root, args.reference_id)
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
            existing = mmi_path(store, reference)
            if samtools.version and minimap2.version and existing.is_file() and args.mmi is None:
                cache = store.load_hash_cache()
                key = store.hash_cache_key(existing)
                mmi_sha = cache.get(key, {}).get("sha256")
                if mmi_sha:
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
            data["estimate_min"] = [round(low, 1), round(high, 1)]
            lines.append(
                f"ALIGN     {reason}" + ("" if reason == "reused" else f" · estimate {low:.0f}-{high:.0f} min")
                + (" · minimap2 index built first" if mmi_needed else "")
            )
    for row in plan_rows:
        lines.append(f"PLAN      {row['algorithm_id']}{' (upstream)' if row['upstream'] else ''}: {row['decision']}")
    if not plan_rows:
        lines.append("PLAN      no algorithms registered")
    payload = _local_envelope(_result("lab run", "ok", "dry run: nothing written", data=data))
    return EXIT_OK, _with_human(payload, lines, replace_data=True)


def run_command(args: argparse.Namespace, store: LabStore) -> tuple[int, dict[str, Any]]:
    if args.example:
        if args.inputs:
            raise LabProblem(
                "TBX-LAB-005",
                cause="--example takes no input paths",
                fix="Run `traceback lab run --example` on its own",
                data={"argument": 1, "kind": "an input given with --example"},
            )
        from .example import example_dir, prepared

        store.check_version()
        if args.dry_run and not prepared(store.root):
            payload = _local_envelope(
                _result("lab run", "ok", "dry run: nothing written", data={
                    "root": str(store.root), "dry_run": True, "example": True,
                    "alignment": "example not generated yet: generated and aligned on first run (~200k reads, under a minute)",
                })
            )
            return EXIT_OK, _with_human(payload, [f"ROOT      {store.root}", "EXAMPLE   not generated yet: generated and aligned on first run (~200k reads, under a minute)"], replace_data=True)
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
                example = False
                if stored:
                    source_id = resolve_sample(store, args.inputs[0])
                    aligned, record = _stored_input(store, source_id)
                    record = record_source_sex(store, source_id, args.sex)
                    example = bool(record.get("example"))
                    reference_id = aligned.manifest["reference_id"]
                    reference_sha256 = aligned.manifest["reference_sha256"]
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
                        aligned = ingest_prealigned(store, chunkset, reference, progress=progress)
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
                            override=args.mmi,
                            scrubber=scrubber,
                            timeout_s=ALIGN_TIMEOUT_S,
                            echo=_echo(args.verbose),
                            progress=progress,
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
                target = _target_from_input(aligned, reference_id, reference_sha256, example, store)
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
    seconds = int(round(seconds))
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
        tool = {"copy-number": "ichor", "cell-origin": "modkit"}.get(algorithm_id)
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
