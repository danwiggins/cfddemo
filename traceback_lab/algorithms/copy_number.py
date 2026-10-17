"""``copy-number``: tumour fraction, ploidy and the genome log2 track via the Lab ichor adapter (D14).

Consumes a stripped counting BAM: written here from the aligned BAM for the
full input, or handed in by the runner for a derived input (downsample,
split half, synthetic CNA spike).  Without a panel of normals the result is
interpreted as ``not_interpretable(no_pon)`` with the value still visible.
"""

from __future__ import annotations

import json
import tempfile
from collections.abc import Callable
from dataclasses import replace
from functools import lru_cache
from pathlib import Path
from typing import Any

from ..adapters import ichor
from ..registry import AlgorithmOutput, InputContext, Ready, Requirements, Skip, ToolPin
from ._base import Base, metric

MIN_ELIGIBLE_READS = 2_000_000


@lru_cache(maxsize=1)
def _toolchain() -> Any:
    from traceback_runner.toolchain import resolve_copy_number_toolchain

    return resolve_copy_number_toolchain()


def toolchain_or_none() -> Any | None:
    try:
        return _toolchain()
    except Exception:  # noqa: BLE001 - any resolution failure means "missing"
        _toolchain.cache_clear()
        return None


class CopyNumber(Base):
    """Not re-decorated as a dataclass: ``tools`` is a live property (the
    installed toolchain lock enters the execution identity), and Base's
    generated ``__init__`` assignment to it is ignored."""

    @property  # type: ignore[override]
    def tools(self) -> tuple[ToolPin, ...]:
        toolchain = toolchain_or_none()
        tag = toolchain.identity.lock_sha256[:12] if toolchain is not None else "missing"
        return (ToolPin("ichorCNA-toolchain", tag, "gpl-copyleft"),)

    @tools.setter
    def tools(self, value: Any) -> None:
        pass

    def asset_sha256s(self, root: Path) -> list[str]:
        """Registered asset digests for the execution identity ([] when missing:
        preflight then skips with missing_asset)."""

        toolchain = toolchain_or_none()
        if toolchain is None:
            return []
        try:
            assets = ichor.resolve_assets(root, toolchain)
        except Exception:  # noqa: BLE001
            return []
        return sorted(ichor.asset_sha256s(assets).values())

    def preflight(self, ctx: InputContext) -> Ready | Skip:
        if ctx.example:
            return Skip("example_reference", "copy-number needs hg38-scale assets; not in the synthetic example")
        toolchain = toolchain_or_none()
        if toolchain is None:
            return Skip("missing_toolchain", "ichorCNA toolchain not installed: traceback toolchain install ichor")
        try:
            ichor.resolve_assets(ctx.root, toolchain)
        except Exception:  # noqa: BLE001 - TBX-ASSET-* means not registered (or changed)
            return Skip(
                "missing_asset",
                "ichorCNA assets not registered: traceback method-asset register --from-toolchain copy-number",
            )
        return super().preflight(ctx)

    def run(self, ctx: InputContext, out: Path) -> AlgorithmOutput:
        parameters = ichor.default_parameters()
        if "min_counted_reads" in ctx.extras:
            requested = ctx.extras["min_counted_reads"]
            if type(requested) is not int or requested != parameters.signed.min_counted_reads:
                raise ichor.CopyNumberError("counted-read floor override is not part of this execution identity")
        selected = ctx.extras.get("pon_selection")
        if selected is not None:
            from ..pon import require_grid
            from ..pon_consumer import selection_from_payload

            selection = selection_from_payload(selected, ctx.root, ctx.reference_sha256)
            parameters = replace(parameters, pon_policy="lab_loo_v1", pon_sha256=selection.pon_sha256)
            return self.run_configured(
                ctx, out, parameters=parameters, pon=selection.path,
                validate_grid=lambda actual: require_grid(selection, actual),
            )
        return self.run_configured(ctx, out, parameters=parameters)

    def run_configured(
        self, ctx: InputContext, out: Path, *,
        parameters: ichor.LabCopyNumberParameters,
        pon: Path | None = None,
        validate_grid: Callable[[str], None] | None = None,
    ) -> AlgorithmOutput:
        """Execute one pinned Lab CN contract; the caller selects any PoN."""

        if (pon is None) != (parameters.pon_policy == "none"):
            raise ichor.CopyNumberError("a PoN is required exactly for the PoN policy")
        if pon is not None and validate_grid is None:
            raise ichor.CopyNumberError("a selected PoN requires grid verification")
        toolchain = _toolchain()
        assets = ichor.resolve_assets(ctx.root, toolchain)
        with tempfile.TemporaryDirectory(prefix="cn-", dir=out.parent) as scratch:
            work = Path(scratch)
            given = ctx.extras.get("counting_bam")
            if given:
                counting = Path(given)
                counted = int(ctx.extras["counted_reads"])
                scanned = int(ctx.extras.get("records_scanned", counted))
            else:
                counting = work / "counting.bam"
                scanned, _, counted = ichor.write_counting(ctx.input_dir / "aligned.bam", counting, parameters)
            result = ichor.measure(
                counting, counted, scanned, work, toolchain=toolchain, assets=assets, parameters=parameters,
                pon=pon, validate_grid=validate_grid,
            )
        track = {"bins": result.bins}
        segments = {"segments": result.segments}
        return AlgorithmOutput(
            metrics=[
                metric("tumor_fraction", result.tumor_fraction, unit="fraction", n=counted),
                metric("ploidy", result.ploidy, unit="copies", n=counted),
                metric("identifiable", result.identifiable),
                metric("pon_policy", parameters.pon_policy),
                metric("counted_reads", counted, unit="reads", n=counted),
                metric("segments_altered", sum(1 for s in result.segments if s["call"] not in (None, "NEUT")), n=len(result.segments)),
            ],
            panels=[
                {"panel_id": "genome-track", "kind": "genome-track", "data_ref": "artifacts/cn_bins.json"},
                {"panel_id": "segments", "kind": "segments", "data_ref": "artifacts/cn_segments.json"},
            ],
            artifacts={
                "cn_bins.json": json.dumps(track, separators=(",", ":")).encode(),
                "cn_segments.json": json.dumps(segments, separators=(",", ":")).encode(),
            },
        )


COPY_NUMBER = CopyNumber(
    "copy-number",
    version="2",  # v1 could execute below its identity's signed counted-read floor.
    family="copy-number",
    consumes="counting_bam",
    cost="slow",
    requires=Requirements(min_eligible_reads=MIN_ELIGIBLE_READS),
    parameters=ichor.default_parameters().contract(),
)
