"""Bind one selected panel to copy-number's per-run execution contract.

The cohort layer chooses and verifies a PoN. This module only translates that
selection to a Lab CN parameter identity; the worker's execution path must
consume the same selected bytes before an attempt can be published.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any, Protocol

from .adapters import ichor
from .problems import LabProblem
from .registry import Algorithm, PlanItem
from .store import LabStore

DEFAULT_COHORT = "healthy-v1"
MEMBER_RECOMPUTE_TERMINALS = ("instability", "arm-summary")


class SelectedPon(Protocol):
    pon_policy: str
    pon_sha256: str


def bind_copy_number_algorithm(algorithm: Algorithm, selection: SelectedPon) -> Algorithm:
    """Return a per-invocation algorithm whose identity includes the PoN."""

    if selection.pon_policy != "lab_loo_v1":
        raise ValueError("a selected PoN must use lab_loo_v1")
    parameters = replace(
        ichor.default_parameters(), pon_policy="lab_loo_v1", pon_sha256=selection.pon_sha256
    ).contract()
    return replace(algorithm, parameters=parameters)


def bind_copy_number(items: Sequence[PlanItem], selection: SelectedPon) -> list[PlanItem]:
    """Use the selected PoN digest in copy-number's D19 execution identity."""

    return [
        PlanItem(bind_copy_number_algorithm(item.algorithm, selection), item.upstream_only)
        if item.algorithm.id == "copy-number" else item
        for item in items
    ]


def member_recompute_plan(entries: Sequence[tuple[str, Algorithm]]) -> list[PlanItem]:
    """One dependency-expanded plan for member work and selector rollback."""

    from . import registry

    return registry.plan([algorithm for _, algorithm in entries], only=list(MEMBER_RECOMPUTE_TERMINALS))


def selection_for_target(
    store: LabStore, *, cohort_id: str | None, source_id: str,
    reference_sha256: str, aligner_identity: str,
) -> Any | None:
    """Pin one verified cohort PoN, or keep the unbuilt default in no-PoN mode."""

    from .cohorts import read_current

    chosen = cohort_id or DEFAULT_COHORT
    generation = read_current(store, chosen)
    if generation is None or generation.pon_asset_sha256 is None:
        if cohort_id is None:
            return None
        raise LabProblem(
            "TBX-LAB-020", cause=f"selected cohort {chosen} has no built panel of normals",
            fix="Build a verified protocol-matched panel or omit --cohort for the no-PoN baseline",
            data={"cohort_id": chosen, "field": "panel of normals"},
        )
    return selection_for_generation(
        store, generation, source_id=source_id,
        reference_sha256=reference_sha256, aligner_identity=aligner_identity,
    )


def selection_for_generation(
    store: LabStore, generation: Any, *, source_id: str,
    reference_sha256: str, aligner_identity: str,
) -> Any:
    """Bind a prospective or published immutable generation to one source."""

    from .algorithms.copy_number import toolchain_or_none
    from .pon import binding_for
    from traceback_runner.copy_number_method import parameters_sha256 as signed_parameters_sha256
    from traceback_runner.references import ReferenceProblem

    chosen = generation.cohort_id
    if generation.kind != "internal_cohort":
        raise LabProblem(
            "TBX-LAB-020", cause=f"selected cohort {chosen} is not an internal protocol-matched cohort",
            fix="Use a verified internal cohort with a built panel of normals",
            data={"cohort_id": chosen, "field": "panel of normals"},
        )
    toolchain = toolchain_or_none()
    if toolchain is None:
        raise LabProblem(
            "TBX-LAB-020", cause="the selected PoN's ichor toolchain is unavailable",
            fix="Install the matching ichor toolchain before running with this cohort",
            data={"cohort_id": chosen, "field": "toolchain"},
        )
    try:
        assets = ichor.resolve_assets(store.root, toolchain)
        hashes = ichor.asset_sha256s(assets)
    except (OSError, ValueError, RuntimeError, ReferenceProblem) as exc:
        raise LabProblem(
            "TBX-LAB-020", cause="the selected PoN's registered ichor assets are unavailable",
            fix="Restore the matching registered ichor assets before rerunning",
            data={"cohort_id": chosen, "field": "method assets"},
        ) from exc
    return binding_for(
        store, generation, source_id, reference_sha256=reference_sha256,
        aligner_identity=aligner_identity,
        toolchain_lock_sha256=toolchain.identity.lock_sha256,
        counting_parameters_sha256=signed_parameters_sha256(ichor.default_parameters().signed),
        gc_sha256=hashes["gc_wig"], map_sha256=hashes["map_wig"],
        centromere_sha256=hashes["centromere"],
    )


def selection_payload(selection: Any) -> dict[str, Any]:
    """JSON-safe copy of a pinned selection for the isolated worker."""

    payload = asdict(selection)
    payload["path"] = str(selection.path)
    return payload


def selection_from_payload(payload: dict[str, Any], root: Path, reference_sha256: str) -> Any:
    """Reconstruct the pinned selection and refuse a path outside its digest."""

    from .pon import PonSelection

    data = dict(payload)
    data["path"] = Path(data["path"])
    data["panel_source_ids"] = tuple(data["panel_source_ids"])
    selection = PonSelection(**data)
    expected = root / "lab" / "assets" / "pon" / f"{selection.pon_sha256}.rds"
    if selection.path != expected or selection.reference_sha256 != reference_sha256:
        raise ichor.CopyNumberError("selected PoN path or reference does not match this input")
    return selection


def bind_target(
    store: LabStore, items: Sequence[PlanItem], target: Any, manifest: dict[str, Any],
    *, cohort_id: str | None,
) -> list[PlanItem]:
    """Pin a selection once for a full input and every derived control."""

    has_copy_number = any(item.algorithm.id == "copy-number" for item in items)
    if not has_copy_number and cohort_id is None:
        return list(items)
    if cohort_id is not None and target.example:
        raise LabProblem(
            "TBX-LAB-021",
            cause="an EXAMPLE input cannot use a real cohort panel",
            fix="Omit --cohort for EXAMPLE, or use a real ONT input",
            data={"cohort_id": cohort_id, "field": "EXAMPLE"},
        )
    if cohort_id is not None and target.platform != "ont":
        raise LabProblem(
            "TBX-LAB-024",
            cause="the selected ONT panel cannot be applied to a non-ONT input",
            fix="Use a real ONT input for this cohort, or omit --cohort",
            data={"cohort_id": cohort_id, "field": "platform"},
        )
    if cohort_id is None and (target.platform != "ont" or target.example):
        # The implicit ONT kit panel does not apply to short-read or EXAMPLE
        # inputs. Explicit --cohort still validates or refuses its selection.
        return list(items)
    target.extras.pop("pon_unavailable", None)
    from .cohorts import _aligner

    aligner_label, _ = _aligner(manifest, target.input_id)
    try:
        selection = selection_for_target(
            store, cohort_id=cohort_id, source_id=target.source_id,
            reference_sha256=target.reference_sha256, aligner_identity=aligner_label,
        )
    except LabProblem as problem:
        if (cohort_id is not None or problem.code != "TBX-LAB-020"
                or problem.data.get("field") not in ("toolchain", "method assets")):
            raise
        # Preserve the built panel's fail-closed boundary: the runner must
        # publish a CN skip, never reuse or execute an uncorrected CN result.
        target.extras["pon_unavailable"] = {
            "reason_code": "missing_toolchain" if problem.data["field"] == "toolchain" else "missing_asset",
            "message": f"built panel unavailable: {problem.cause}",
        }
        return list(items)
    if selection is None:
        return list(items)
    if not has_copy_number:
        return list(items)
    target.extras["pon_selection"] = selection_payload(selection)
    return bind_copy_number(items, selection)


def recompute_member(
    store: LabStore, generation: Any, source_id: str, *,
    lock: Any, log: Any, run_id: str, timeout_s: float,
) -> Any:
    """Recompute one member's CN and instability with its pinned LOO panel."""

    from . import registry
    from .cohorts import _aligner, _checked_source_record, _full_input, member_from_store
    from .runner import RunTarget, run_plan
    from .scrub import Scrubber

    stored = _full_input(store, _checked_source_record(store, source_id))
    if stored is None:
        raise LabProblem(
            "TBX-LAB-020", cause="PoN member has no full aligned input",
            fix="Restore the member's full aligned input before cohort build",
            data={"field": "panel of normals"},
        )
    input_id, manifest = stored
    label, _ = _aligner(manifest, input_id)
    selection = selection_for_generation(
        store, generation, source_id=source_id,
        reference_sha256=manifest["reference_sha256"], aligner_identity=label,
    )
    entries = registry.registry_entries()
    by_id = {algorithm.id: entry for entry, algorithm in entries}
    items = member_recompute_plan(entries)
    items = bind_copy_number(items, selection)
    target = RunTarget(
        input_id=input_id, source_id=source_id, input_dir=store.inputs_dir / input_id,
        reference_id=manifest["reference_id"], reference_sha256=manifest["reference_sha256"],
        extras={"pon_selection": selection_payload(selection)},
    )
    if log is not None:
        from .ingest import id12_for

        log.append("sample", sample=id12_for(store, source_id), source_id=source_id, input_id=input_id)
    results = run_plan(
        store, lock, log, target, items, by_id, run_id=run_id,
        timeout_s=timeout_s, scrubber=Scrubber(root=store.root),
    )
    failed = [
        result.algorithm_id for result in results
        if result.status != "ok" and not (result.algorithm_id == "arm-summary" and result.status == "skipped")
    ]
    if failed:
        raise LabProblem(
            "TBX-LAB-020", cause=f"PoN member recomputation did not finish: {', '.join(failed)}",
            fix="Inspect the cohort build run log and restore the member or toolchain before retrying",
            data={"field": "panel of normals", "algorithms": failed},
        )
    return member_from_store(store, source_id, pon_selection=selection)
