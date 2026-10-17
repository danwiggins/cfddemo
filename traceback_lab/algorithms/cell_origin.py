"""``cell-origin``: tissue composition through the public, lease-free
``cell_origin.compute_measurement`` (D14, L0.5b).

The Lab calls the signed method's own computation with ``heartbeat=None``,
plain asset paths resolved (and re-verified) through the method-asset store,
and the modified-base model passed explicitly from the aligned BAM's ``@RG``
header (D9).  A derived input (downsample fraction, split half) is written as
a **transient** filtered BAM in the worker's scratch directory with the same
row predicate as the read-summary masks; the runner deletes the scratch
directory after the result is published (D30, D33).
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable

from ..registry import AlgorithmOutput, InputContext, Ready, Requirements, Skip, ToolPin
from ._base import Base, metric

MIN_ELIGIBLE_READS = 2_500_000
LOYFER_KINDS = ("loyfer-atlas", "loyfer-markers", "loyfer-regions")


@lru_cache(maxsize=1)
def _modkit() -> Any:
    from traceback_runner.toolchain import resolve_modkit

    return resolve_modkit()


def modkit_or_none() -> Any | None:
    try:
        return _modkit()
    except Exception:  # noqa: BLE001 - any resolution failure means "missing"
        _modkit.cache_clear()
        return None


def loyfer_assets(root: Path) -> dict[str, Any]:
    """The registered Loyfer atlas files, re-verified by digest (kind -> loaded asset)."""

    from traceback_runner.references import LOYFER_DIRECTORY_FILES, verify_registered_asset

    return {
        kind.value: verify_registered_asset(root, asset_id, kind=kind)
        for kind, (_, asset_id) in LOYFER_DIRECTORY_FILES.items()
    }


def _sha256_file(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(16 << 20):
            digest.update(block)
    return digest.hexdigest()


def metric_id_for(contributor: str) -> str:
    return "fraction_" + "".join(ch if ch.isalnum() else "_" for ch in contributor.lower()).strip("_")


def header_model(bam: Path) -> str | None:
    from traceback_runner.cell_origin import _header_models

    models = _header_models(bam)
    return models[0] if len(models) == 1 else None


def derived_name_predicate(derived: dict[str, Any]) -> Callable[[str], bool]:
    """The shared read-name selection used by row masks and transient BAMs."""

    from fractions import Fraction
    from math import ceil

    from ..read_summary import DS_SALT, SPLIT_SALT, h64, validate_fraction

    if derived.get("transform") == "downsample":
        threshold = ceil(Fraction(validate_fraction(float(derived["fraction"]))) * 2**64)
        return lambda name: h64(DS_SALT, name) < threshold
    elif derived.get("transform") == "split":
        if derived.get("half") not in {"A", "B"}:
            raise ValueError("a split half is A or B")
        want_a = derived["half"] == "A"
        return lambda name: (h64(SPLIT_SALT, name) < 2**63) == want_a
    else:
        raise ValueError(f"cell-origin has no transient BAM for {derived.get('transform')!r}")


def write_transient_bam(source: Path, destination: Path, derived: dict[str, Any]) -> int:
    """Copy the records a derived input keeps (same predicate as the row masks)."""

    import pysam

    keep = derived_name_predicate(derived)
    kept = 0
    with pysam.AlignmentFile(str(source), "rb") as reader, pysam.AlignmentFile(
        str(destination), "wb", template=reader
    ) as writer:
        for record in reader.fetch(until_eof=True):
            if keep(record.query_name or ""):
                writer.write(record)
                kept += 1
    pysam.index(str(destination))
    return kept


def _signed_parameters_sha256() -> str:
    """The locked signed parameters (without a model) enter the execution identity."""

    from traceback_runner.cell_origin_method import default_parameters, parameters_sha256

    return parameters_sha256(default_parameters())


class CellOrigin(Base):
    """Not re-decorated: ``tools`` is a live property (the installed modkit
    pin enters the execution identity)."""

    @property  # type: ignore[override]
    def tools(self) -> tuple[ToolPin, ...]:
        modkit = modkit_or_none()
        version = modkit.identity.version if modkit is not None else "missing"
        return (ToolPin("modkit", version, "modkit-ont-public"),)

    @tools.setter
    def tools(self, value: Any) -> None:
        pass

    def asset_sha256s(self, root: Path) -> list[str]:
        try:
            return sorted(asset.registered.file_sha256 for asset in loyfer_assets(root).values())
        except Exception:  # noqa: BLE001
            return []

    def preflight(self, ctx: InputContext) -> Ready | Skip:
        if ctx.example:
            return Skip("example_reference", "cell-origin needs the hg38 Loyfer atlas; not in the synthetic example")
        if modkit_or_none() is None:
            return Skip("missing_toolchain", "modkit not installed: traceback toolchain install modkit")
        try:
            loyfer_assets(ctx.root)
        except Exception:  # noqa: BLE001
            return Skip("missing_asset", "Loyfer atlas not registered: traceback method-asset register --from-dir LOYFER_DIR")
        if not ctx.extras.get("reference_fasta"):
            return Skip("reference", "the registered reference FASTA is not available")
        if header_model(ctx.input_dir / "aligned.bam") is None:
            return Skip("missing_tags", "the BAM's @RG lines declare no single modified-base model")
        return super().preflight(ctx)

    def run(self, ctx: InputContext, out: Path) -> AlgorithmOutput:
        import tempfile

        from traceback_runner.cell_origin import ModbaseModel, compute_measurement
        from traceback_runner.cell_origin_method import default_parameters

        fasta = Path(ctx.extras["reference_fasta"])
        if _sha256_file(fasta) != ctx.reference_sha256:
            # The signed stage refuses this too: the FASTA on disk is not the
            # registered reference this input was aligned against.
            raise RuntimeError("the reference FASTA changed since it was registered")
        model_id = header_model(ctx.input_dir / "aligned.bam")
        model = ModbaseModel(id=model_id, source="header")
        parameters = default_parameters(modbase_model=model_id)
        assets = loyfer_assets(ctx.root)
        with tempfile.TemporaryDirectory(prefix="co-", dir=out.parent) as scratch:
            work = Path(scratch)
            bam = ctx.input_dir / "aligned.bam"
            if ctx.derived:
                bam = work / "derived.bam"  # transient: gone when this directory goes
                write_transient_bam(ctx.input_dir / "aligned.bam", bam, dict(ctx.derived))
            measurement = compute_measurement(
                parameters=parameters,
                reference_id=ctx.reference_id,
                modbase_model=model,
                bam=bam,
                fasta=fasta,
                atlas=Path(assets["loyfer-atlas"].source.file_path),
                markers=Path(assets["loyfer-markers"].source.file_path),
                regions=Path(assets["loyfer-regions"].source.file_path),
                job_directory=work,
                modkit=_modkit(),
                heartbeat=None,
            )
        level = float(parameters.bootstrap_confidence_level)
        classified = int(measurement.denominators.classified_fragments)
        metrics = []
        rows = []
        for estimate in measurement.estimates:
            interval = estimate.interval
            ci = (float(interval.low), float(interval.high)) if interval is not None else None
            metrics.append(
                metric(
                    metric_id_for(estimate.contributor_id),
                    float(estimate.fraction),
                    unit="fraction",
                    n=classified,
                    ci=ci,
                    ci_level=level if ci else None,
                )
            )
            rows.append(
                {
                    "contributor_id": estimate.contributor_id,
                    "fraction": float(estimate.fraction),
                    "interval": list(ci) if ci else None,
                    "interval_state": str(estimate.interval_state),
                }
            )
        metrics.append(metric("classified_fragments", classified, unit="fragments", n=classified))
        composition = {"confidence_level": level, "contributors": rows, "solver_converged": bool(measurement.solver.converged)}
        return AlgorithmOutput(
            metrics=metrics,
            panels=[{"panel_id": "composition", "kind": "dot-interval", "data_ref": "artifacts/composition.json"}],
            artifacts={"composition.json": json.dumps(composition, separators=(",", ":")).encode()},
        )


CELL_ORIGIN = CellOrigin(
    "cell-origin",
    family="methylation",
    consumes="full_bam",
    cost="slow",
    requires=Requirements(mm_tags=True, min_eligible_reads=MIN_ELIGIBLE_READS),
    parameters={"method": "cell_origin.compute_measurement", "atlas": "loyfer-u250", "signed_parameters_sha256": _signed_parameters_sha256()},
)
