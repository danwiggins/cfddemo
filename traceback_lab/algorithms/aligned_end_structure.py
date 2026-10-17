"""Exploratory ONT aligned-end asymmetry at pinned open-chromatin and end sites.

Aligned start/end-1 are proxies for physical cleavage positions. This is not
tissue-origin inference or a reimplementation of paired-end short-read OCF.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from ..end_catalog_assets import EndCatalogueError, end_catalogue_identity, inspect_end_catalogues
from ..read_summary import CHUNK_ROWS, FLAG_EXCLUDE, MIN_MAPQ, depth_stamp, validate_fraction
from ..registry import AlgorithmOutput, InputContext, Ready, Requirements, Skip
from ._base import Base, load_summary, metric, ratio


RADIUS = 100
MIN_DEPTH_X = 10.0


def _selection(derived: Mapping[str, Any]) -> dict[str, Any]:
    """Canonical single-step control; derived IDs bind it to the parent input."""

    if not isinstance(derived, Mapping):
        raise ValueError("invalid derived selection")
    transform = derived.get("transform")
    if transform == "downsample" and set(derived) == {"transform", "fraction"}:
        fraction = derived["fraction"]
        if isinstance(fraction, bool) or not isinstance(fraction, (int, float)):
            raise ValueError("invalid downsample fraction")
        return {"transform": "downsample", "fraction": validate_fraction(float(fraction))}
    if transform == "split" and set(derived) == {"transform", "half"} and derived["half"] in {"A", "B"}:
        return {"transform": "split", "half": derived["half"]}
    raise ValueError("unsupported derived selection")


def _relative_counts(positions: np.ndarray, centers: np.ndarray) -> np.ndarray:
    counts = np.zeros(2 * RADIUS + 1, dtype=np.int64)
    if not len(positions) or not len(centers):
        return counts
    right = np.searchsorted(centers, positions)
    left_index = np.clip(right - 1, 0, len(centers) - 1)
    right_index = np.clip(right, 0, len(centers) - 1)
    left_distance = np.abs(positions - centers[left_index])
    right_distance = np.abs(positions - centers[right_index])
    nearest = np.where((right == 0) | ((right < len(centers)) & (right_distance < left_distance)),
                       right_index, left_index)
    offsets = positions - centers[nearest]
    kept = offsets[(offsets >= -RADIUS) & (offsets <= RADIUS)]
    counts += np.bincount(kept + RADIUS, minlength=len(counts))
    return counts


def _site_hits(positions: np.ndarray, sites: np.ndarray) -> int:
    if not len(positions) or not len(sites):
        return 0
    at = np.searchsorted(sites, positions)
    return int(np.count_nonzero((at < len(sites)) & (sites[np.minimum(at, len(sites) - 1)] == positions)))


@dataclass
class AlignedEndStructure(Base):
    family: str = "fragmentomics"
    cost: str = "slow"
    requires: Requirements = field(default_factory=Requirements)
    parameters: dict[str, Any] = field(default_factory=lambda: {
        "measurement": "ONT aligned-span end structure; exploratory",
        "min_observed_depth_x": MIN_DEPTH_X,
        "ocf_like_windows": {"left": [-70, -50], "right": [50, 70], "coordinates": "inclusive_integer_offsets"},
        "profile_offsets": [-RADIUS, RADIUS],
        "end_coordinates": "start_and_end_minus_one_0_based",
        "preferred_support": "exact_U_or_D_catalogue_coordinate_per_valid_end",
        "filter": f"D21 autosomal primary MAPQ{MIN_MAPQ} exclude-flags-{FLAG_EXCLUDE}; one ONT record per molecule",
    })

    def asset_sha256s(self, root: Path) -> list[str]:
        try:
            return [end_catalogue_identity(root)]
        except EndCatalogueError:
            return []

    def preflight(self, ctx: InputContext) -> Ready | Skip:
        if ctx.platform != "ont":
            return Skip("platform", "aligned-end structure requires an ONT-declared input")
        if ctx.example:
            return Skip("example_reference", "aligned-end structure requires a genuine hg38 ONT input")
        if ctx.derived is not None:
            # DerivedSpec is one transform of a full parent, never a chained lineage.
            try:
                _selection(ctx.derived)
            except (ValueError, KeyError, TypeError):
                return Skip("derived_input", "aligned-end structure requires a valid derived row mask")
        if ctx.reference_id != "hg38":
            return Skip("reference", "aligned-end structure requires a full, genuine hg38 ONT input")
        try:
            manifest = json.loads((ctx.input_dir / "manifest.json").read_bytes())
        except (OSError, ValueError):
            return Skip("input_kind", "aligned-end structure requires a stored native ONT alignment")
        if not isinstance(manifest, dict) or manifest.get("kind") != "aligned":
            return Skip("input_kind", "aligned-end structure requires a native ONT alignment")
        # Only the native ONT aligner publishes kind=aligned; imported BAMs are prealigned.
        depth = ctx.approx_depth_x
        if ctx.derived is not None:
            try:
                summary, mask = load_summary(ctx)
                depth = depth_stamp(summary, mask)["approx_depth_x"]
            except (OSError, RuntimeError):
                return Skip("upstream", "aligned-end structure could not read the derived read summary")
        if depth is None or not np.isfinite(depth) or depth < MIN_DEPTH_X:
            return Skip("depth", "aligned-end structure needs at least 10x observed eligible depth")
        expected = ctx.extras.get("planned_asset_sha256s")
        if expected is not None:
            if not expected:
                return Skip("missing_asset", "verified hg38 open-chromatin and preferred-end catalogues are required")
        else:
            try:
                end_catalogue_identity(ctx.root)
            except EndCatalogueError:
                return Skip("missing_asset", "verified hg38 open-chromatin and preferred-end catalogues are required")
        return Ready()

    def run(self, ctx: InputContext, out: Path) -> AlgorithmOutput:
        del out
        if ctx.platform != "ont":
            raise ValueError("aligned-end structure requires an ONT-declared input")
        catalogues = inspect_end_catalogues(ctx.root)
        expected = ctx.extras.get("planned_asset_sha256s")
        if expected is not None and expected != [catalogues.identity]:
            raise EndCatalogueError("pinned end catalogues changed after execution identity was set")
        summary, mask = load_summary(ctx)
        selection = _selection(ctx.derived) if ctx.derived is not None else None
        observed_depth_x = depth_stamp(summary, mask)["approx_depth_x"] if selection is not None else ctx.approx_depth_x
        if selection is not None and (
            observed_depth_x is None or not np.isfinite(observed_depth_x) or observed_depth_x < MIN_DEPTH_X
        ):
            raise RuntimeError("derived aligned-end depth fell below 10x after preflight")
        names = summary.contigs
        lengths = summary.lengths
        allowed = set(int(cid) for cid in summary.autosome_ids())
        centers_by_id: dict[int, np.ndarray] = {}
        preferred_u_by_id: dict[int, np.ndarray] = {}
        preferred_d_by_id: dict[int, np.ndarray] = {}
        valid_centers = valid_preferred_sites = 0
        for cid, name in enumerate(names):
            if cid not in allowed:
                continue
            catalogue_name = name if name.startswith("chr") else f"chr{name}"
            centers = catalogues.centers.get(catalogue_name, np.empty(0, dtype=np.int64))
            bounded = centers[(centers >= RADIUS) & (centers + RADIUS < lengths[cid])]
            centers_by_id[cid] = bounded
            valid_centers += len(bounded)
            preferred_u_by_id[cid] = catalogues.preferred_u.get(catalogue_name, np.empty(0, dtype=np.int64))
            preferred_d_by_id[cid] = catalogues.preferred_d.get(catalogue_name, np.empty(0, dtype=np.int64))
            valid_preferred_sites += int(np.count_nonzero(preferred_u_by_id[cid] < lengths[cid]))
            valid_preferred_sites += int(np.count_nonzero(preferred_d_by_id[cid] < lengths[cid]))
        u_counts = np.zeros(2 * RADIUS + 1, dtype=np.int64)
        d_counts = np.zeros(2 * RADIUS + 1, dtype=np.int64)
        valid_molecules = u_hits = d_hits = 0
        span_length_bins = {"lt100": 0, "100_200": 0, "201_500": 0,
                            "501_2000": 0, "gt2000": 0}
        contigs = summary.column("contig")
        starts = summary.column("start")
        ends = summary.column("end")
        flags = summary.column("flag")
        mapq = summary.column("mapq")
        for lo in range(0, summary.rows, CHUNK_ROWS):
            hi = min(summary.rows, lo + CHUNK_ROWS)
            selected = ((flags[lo:hi] & FLAG_EXCLUDE) == 0) & (mapq[lo:hi] >= MIN_MAPQ)
            selected &= np.isin(contigs[lo:hi], list(allowed))
            if mask is not None:
                selected &= mask[lo:hi]
            if not np.any(selected):
                continue
            chunk_contigs = contigs[lo:hi][selected]
            chunk_starts = starts[lo:hi][selected].astype(np.int64)
            chunk_ends = ends[lo:hi][selected].astype(np.int64)
            for cid in np.unique(chunk_contigs):
                cid = int(cid)
                on_contig = chunk_contigs == cid
                first = chunk_starts[on_contig]
                exclusive_end = chunk_ends[on_contig]
                valid = (first >= 0) & (exclusive_end > first) & (exclusive_end <= lengths[cid])
                u = first[valid]
                d = exclusive_end[valid] - 1
                spans = exclusive_end[valid] - u
                span_length_bins["lt100"] += int(np.count_nonzero(spans < 100))
                span_length_bins["100_200"] += int(np.count_nonzero((spans >= 100) & (spans <= 200)))
                span_length_bins["201_500"] += int(np.count_nonzero((spans > 200) & (spans <= 500)))
                span_length_bins["501_2000"] += int(np.count_nonzero((spans > 500) & (spans <= 2000)))
                span_length_bins["gt2000"] += int(np.count_nonzero(spans > 2000))
                valid_molecules += len(u)
                u_counts += _relative_counts(u, centers_by_id.get(cid, np.empty(0, dtype=np.int64)))
                d_counts += _relative_counts(d, centers_by_id.get(cid, np.empty(0, dtype=np.int64)))
                u_hits += _site_hits(u, preferred_u_by_id.get(cid, np.empty(0, dtype=np.int64)))
                d_hits += _site_hits(d, preferred_d_by_id.get(cid, np.empty(0, dtype=np.int64)))
        # Original OCF direction; these are ONT aligned-end proxies, so use an explicit OCF-like name.
        left = int((d_counts[30:51] - u_counts[30:51]).sum())
        right = int((u_counts[150:171] - d_counts[150:171]).sum())
        left_ends = int(u_counts[30:51].sum() + d_counts[30:51].sum())
        right_ends = int(u_counts[150:171].sum() + d_counts[150:171].sum())
        measurement_ends = left_ends + right_ends
        ocf_like = left + right if valid_centers and left_ends and right_ends else None
        left_d_peak = int(np.argmax(d_counts[:RADIUS + 1])) - RADIUS if np.any(d_counts[:RADIUS + 1]) else None
        right_u_peak = int(np.argmax(u_counts[RADIUS:])) if np.any(u_counts[RADIUS:]) else None
        ends_total = 2 * valid_molecules
        artifact = {
            "schema": "traceback.lab.aligned-end-structure.v1",
            "catalogue_identity": catalogues.identity,
            "centers_sha256": catalogues.centers_sha256,
            "preferred_sha256": catalogues.preferred_sha256,
            "source_label": catalogues.source_label,
            "centers_source": catalogues.centers_source,
            "preferred_source": catalogues.preferred_source,
            "terms": catalogues.terms,
            "observed_depth_x": observed_depth_x,
            "depth_basis": "selected_read_summary" if selection is not None else "run_target",
            "derived_selection": selection,
            "interpretation": "ONT aligned-span proxy; exploratory; no tissue-origin calibration",
            "ocf_like_windows": self.parameters["ocf_like_windows"],
            "offset_bp": list(range(-RADIUS, RADIUS + 1)),
            "u_count": u_counts.tolist(), "d_count": d_counts.tolist(),
            "ocf_like_left": left, "ocf_like_right": right,
            "ocf_like_left_ends": left_ends, "ocf_like_right_ends": right_ends,
            "ocf_like_measurement_ends": measurement_ends,
            "left_d_peak_offset_bp": left_d_peak,
            "right_u_peak_offset_bp": right_u_peak,
            "catalogue_centers": catalogues.center_count, "eligible_centers": valid_centers,
            "catalogue_preferred_sites": catalogues.preferred_count,
            "eligible_preferred_sites": valid_preferred_sites,
            "valid_molecules": valid_molecules, "valid_ends": ends_total,
            "aligned_span_length_bins_bp": span_length_bins,
            "preferred_u_hits": u_hits, "preferred_d_hits": d_hits,
        }
        return AlgorithmOutput(
            metrics=[
                metric("ocf_like_end_asymmetry", ocf_like, unit="end-count difference", n=measurement_ends),
                metric("preferred_end_support_fraction", ratio(u_hits + d_hits, ends_total)
                       if valid_preferred_sites and ends_total else None,
                       unit="fraction", n=ends_total),
            ],
            panels=[{"panel_id": "aligned-end-structure", "kind": "density",
                     "data_ref": "artifacts/aligned_end_structure.json"}],
            artifacts={"aligned_end_structure.json": json.dumps(artifact, separators=(",", ":")).encode("utf-8")},
        )


ALIGNED_END_STRUCTURE = AlignedEndStructure("aligned-end-structure")
