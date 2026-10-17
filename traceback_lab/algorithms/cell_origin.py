"""``cell-origin``: Lab-only tissue composition using in-house MM/ML decoding.

The Lab reuses the registered Loyfer assets and the existing UXM, NNLS and
bootstrap computation; the signed modkit job is unchanged. The modified-base
model still comes from the aligned BAM's ``@RG`` header (D9). A derived input
(downsample fraction, split half) is written as
a **transient** filtered BAM in the worker's scratch directory with the same
row predicate as the read-summary masks; the runner deletes the scratch
directory after the result is published (D30, D33).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import numpy as np
import pysam

from ..adapters.mmml_extract import EXTRACTOR_VERSION
from ..identity import canonical_sha256
from ..registry import AlgorithmOutput, InputContext, Ready, Requirements, Skip, ToolPin
from ._base import Base, metric

MIN_ELIGIBLE_READS = 2_500_000
LOYFER_KINDS = ("loyfer-atlas", "loyfer-markers", "loyfer-regions")
_IMPLEMENTATION_FILES = (
    "traceback_lab/algorithms/cell_origin.py",
    "traceback_lab/algorithms/_base.py",
    "traceback_lab/adapters/cell_origin_inhouse.py",
    "traceback_lab/adapters/mmml_extract.py",
    "traceback_lab/algorithms/_records.py",
    "traceback_lab/read_summary.py",
    "traceback_lab/modbase.py",
    "traceback_runner/cell_origin_method.py",
    "evidence_inspector/cell_origin_inputs.py",
    "evidence_inspector/cell_origin_pipeline.py",
    "evidence_inspector/cell_origin_prefilter.py",
    "evidence_inspector/cell_origin_models.py",
    "evidence_inspector/deconvolution.py",
    "evidence_inspector/uxm.py",
)


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


def write_transient_bam(source: Path, destination: Path, derived: dict[str, Any]) -> int:
    """Copy the records a derived input keeps (same predicate as the row masks)."""

    from fractions import Fraction
    from math import ceil

    import pysam

    from ..read_summary import DS_SALT, SPLIT_SALT, h64

    if derived.get("transform") == "downsample":
        threshold = ceil(Fraction(float(derived["fraction"])) * 2**64)
        keep = lambda name: h64(DS_SALT, name) < threshold  # noqa: E731
    elif derived.get("transform") == "split":
        want_a = derived["half"] == "A"
        keep = lambda name: (h64(SPLIT_SALT, name) < 2**63) == want_a  # noqa: E731
    else:
        raise ValueError(f"cell-origin has no transient BAM for {derived.get('transform')!r}")
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


def _downstream_parameters_sha256() -> str:
    """Pin reused UXM/solver settings without claiming the signed modkit path."""

    from traceback_runner.cell_origin_method import default_parameters

    values = default_parameters().model_dump(mode="json")
    values.pop("modkit_arguments", None)
    return canonical_sha256(values)


def _implementation_sha256s() -> dict[str, str]:
    """Pin the Lab decoder and reused solver, independent of checkout newlines."""
    root = Path(__file__).resolve().parents[2]
    return {
        name: hashlib.sha256((root / name).read_bytes().replace(b"\r\n", b"\n")).hexdigest()
        for name in _IMPLEMENTATION_FILES
    }


class CellOrigin(Base):
    """Unsigned Lab result; the signed cell-origin method keeps modkit."""

    def asset_sha256s(self, root: Path) -> list[str]:
        try:
            return sorted(asset.registered.file_sha256 for asset in loyfer_assets(root).values())
        except Exception:  # noqa: BLE001
            return []

    def preflight(self, ctx: InputContext) -> Ready | Skip:
        if ctx.example:
            return Skip("example_reference", "cell-origin needs the hg38 Loyfer atlas; not in the synthetic example")
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

        from ..adapters.cell_origin_inhouse import compute_inhouse
        from traceback_runner.cell_origin_method import default_parameters

        fasta = Path(ctx.extras["reference_fasta"])
        if _sha256_file(fasta) != ctx.reference_sha256:
            raise RuntimeError("the reference FASTA changed since it was registered")
        model_id = header_model(ctx.input_dir / "aligned.bam")
        if model_id is None:
            raise RuntimeError("the BAM no longer declares one modified-base model")
        parameters = default_parameters(modbase_model=model_id)
        assets = loyfer_assets(ctx.root)
        planned = ctx.extras.get("planned_asset_sha256s")
        actual = sorted(asset.registered.file_sha256 for asset in assets.values())
        if planned is not None and sorted(planned) != actual:
            raise RuntimeError("Loyfer atlas registration changed after execution identity was planned")
        with tempfile.TemporaryDirectory(prefix="co-", dir=out.parent) as scratch:
            from traceback_runner.references import copy_registered_asset

            work = Path(scratch)
            copies = {
                kind: copy_registered_asset(ctx.root, asset.registered.asset_id, work,
                                            kind=asset.registered.kind)
                for kind, asset in assets.items()
            }
            bam = ctx.input_dir / "aligned.bam"
            if ctx.derived:
                bam = work / "derived.bam"  # transient: gone when this directory goes
                write_transient_bam(ctx.input_dir / "aligned.bam", bam, dict(ctx.derived))
            measurement = compute_inhouse(
                parameters=parameters,
                bam=bam,
                fasta=fasta,
                atlas=copies["loyfer-atlas"],
                markers=copies["loyfer-markers"],
                regions=copies["loyfer-regions"],
                scratch=work,
            )
        level = float(parameters.bootstrap_confidence_level)
        classified = measurement.classified_fragments
        metrics = []
        rows = []
        intervals = {item.cell_type_id: item for item in measurement.bootstrap.intervals}
        for estimate in measurement.fit.estimates:
            interval = intervals[estimate.cell_type_id]
            ci = ((float(interval.lower_fraction), float(interval.upper_fraction))
                  if interval.lower_fraction is not None else None)
            metrics.append(
                metric(
                    metric_id_for(estimate.cell_type_id),
                    float(estimate.fraction),
                    unit="fraction",
                    n=classified,
                    ci=ci,
                    ci_level=level if ci else None,
                )
            )
            rows.append(
                {
                    "contributor_id": estimate.cell_type_id,
                    "fraction": float(estimate.fraction),
                    "interval": list(ci) if ci else None,
                    "interval_state": str(interval.information_status.value),
                }
            )
        metrics.append(metric("classified_fragments", classified, unit="fragments", n=classified))
        composition = {
            "modbase_model": model_id,
            "confidence_level": level, "contributors": rows,
            "solver_converged": bool(measurement.fit.diagnostics.converged),
            "extraction_qc": {
                "records_with_mm": measurement.extraction.records_with_mm,
                "invalid_mm": measurement.extraction.invalid_mm,
                "dropped_unaligned": measurement.extraction.dropped_unaligned,
                "non_reference_cpg": measurement.extraction.non_reference_cpg,
                "outside_regions": measurement.extraction.outside_regions,
                "low_confidence": measurement.extraction.low_confidence,
                "unsupported_modification": measurement.extraction.unsupported_modification,
            },
        }
        return AlgorithmOutput(
            metrics=metrics,
            panels=[{"panel_id": "composition", "kind": "dot-interval", "data_ref": "artifacts/composition.json"}],
            artifacts={"composition.json": json.dumps(composition, separators=(",", ":")).encode()},
        )


CELL_ORIGIN = CellOrigin(
    "cell-origin",
    version="2",
    family="methylation",
    consumes="full_bam",
    cost="slow",
    requires=Requirements(mm_tags=True, min_eligible_reads=MIN_ELIGIBLE_READS),
    tools=(ToolPin("lab-mmml", EXTRACTOR_VERSION),
           ToolPin("numpy", np.__version__),
           ToolPin("pysam", pysam.__version__),
           ToolPin("htslib", pysam.__samtools_version__)),
    parameters={"method": "lab-mmml-loyfer-uxm-v1", "atlas": "loyfer-u250",
                "downstream_parameters_sha256": _downstream_parameters_sha256(),
                "implementation_sha256s": _implementation_sha256s()},
)
