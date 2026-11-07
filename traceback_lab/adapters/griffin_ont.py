"""Single-end ONT port of the pinned Griffin GC correction and site profiling.

The pinned upstream Griffin scripts read fragments only from paired-end
records: ``griffin_GC_counts.py`` and ``griffin_coverage.py`` require
``is_paired`` and take the fragment from the forward mate's positive TLEN.
A native ONT cfDNA read spans its whole molecule, so this module applies the
same arithmetic to the Lab's own fragment definition instead: a D21-eligible
primary autosomal record's aligned reference span ``[start, end)``.

Each step follows the pinned upstream script it names, at commit
``b624c7a2ea7b8758b6eebd424066c815bfeece61``, with the Lab's existing upstream
configuration (GC bias over 15-500 bp, profiles over 100-200 bp, MAPQ 20,
``mappability_correction: False``, normalisation window -5000..5000 bp, saved
window -1000..1000 bp, 15 bp step, Savitzky-Golay smoothing over 165 bp,
outlier and zero-mappability exclusion, no CNA normalisation):

* ``griffin_GC_counts.py`` - fragments x number of overlapping mappable
  intervals, by length and reference G/C count (ambiguous bases drawn with
  ``numpy.random.default_rng(fragment_start)``, as upstream does).
* ``griffin_GC_bias.py`` - observed / genome frequency, mean-normalised per
  length, 5% running-median smoothed across neighbouring lengths.
* ``griffin_coverage.py`` - fragment-midpoint counts, each GC-corrected
  midpoint weighted by ``1 / smoothed bias`` (bias below 0.05 excluded),
  rounded to 5 decimals and stored as float32 like the upstream bigWig.
* ``griffin_merge_sites.py`` - 15 bp bins, excluded-region and
  zero-mappability masks, the 10 SD outlier mask, site averaging, smoothing,
  depth normalisation and the central-coverage and amplitude features.

No surrogate paired reads are created and no upstream code is executed. The
output is the upstream ``*.GC_corrected.coverage.tsv`` schema, validated by the
same strict parser as the Illumina path.
"""

from __future__ import annotations

import csv
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

import numpy as np

from .griffin import GriffinProfile, read_corrected_profiles
from .griffin_run import (
    GRIFFIN_COMMIT, MIN_ELIGIBLE_AUTOSOMAL_PAIRS, PROFILE_TLEN_RANGE, _REF_FILES, _digest,
    _require_chrom_sizes, _require_site_contigs,
)

ONT_METHOD = "lab-griffin-ont-port-v1"
MIN_ELIGIBLE_AUTOSOMAL_READS = MIN_ELIGIBLE_AUTOSOMAL_PAIRS
GC_SIZE_RANGE = (15, 500)
MAP_Q = 20
STEP = 15
SMOOTHING_LENGTH = 165
FFT_INDEX = 10
_FREQUENCY_PREFIX = "k100_minus_exclusion_lists.mappable_regions.hg38."
_MAPPABLE = "k100_minus_exclusion_lists.mappable_regions.hg38.bed"
_MAPPABILITY = "k100.Umap.MultiTrackMappability.bw"
_EXCLUDE = (
    "encode_unified_GRCh38_exclusion_list.bed", "hg38_centromeres.bed", "hg38_gaps.bed",
    "hg38_fix_patches.bed", "hg38_alternative_haplotypes.bed",
)
# griffin_GC_counts.py keeps only these contigs from the mappable regions.
_GC_COUNT_CHROMS = frozenset(f"chr{number}" for number in range(1, 23))
ELIGIBILITY_PREDICATE = {
    "version": "griffin-ont-d21-autosomal-reads-v1",
    "contigs": "canonical chr1–chr22",
    "counted_record": "one primary mapped single-end record per molecule",
    "fragment": "aligned reference span [start, end)",
    "excluded": "secondary, supplementary, duplicate, QC-fail, unmapped",
    "minimum_mapq": MAP_Q,
}


def _inner(window: tuple[int, int]) -> tuple[int, int]:
    """griffin_merge_sites.py rounds each window to the nearest inner step."""

    return (int(np.ceil(window[0] / STEP) * STEP), int(np.floor(window[1] / STEP) * STEP))


NORM_WINDOW = _inner((-5000, 5000))
SAVE_WINDOW = _inner((-1000, 1000))
CENTER_WINDOW = _inner((-30, 30))
FFT_WINDOW = _inner((-960, 960))
NORM_COLUMNS = np.arange(NORM_WINDOW[0], NORM_WINDOW[1], STEP)
SAVE_COLUMNS = np.arange(SAVE_WINDOW[0], SAVE_WINDOW[1], STEP)
_WIDTH = NORM_WINDOW[1] - NORM_WINDOW[0]
_SAVE_INDEX = ((SAVE_COLUMNS - NORM_WINDOW[0]) // STEP)
_CENTER_INDEX = ((np.arange(CENTER_WINDOW[0], CENTER_WINDOW[1], STEP) - SAVE_WINDOW[0]) // STEP)
_FFT_INDEX = ((np.arange(FFT_WINDOW[0], FFT_WINDOW[1], STEP) - SAVE_WINDOW[0]) // STEP)
_SITE_CHUNK = 256

MappabilityValues = Callable[[str, int, int], np.ndarray]


class GriffinOntDepth(ValueError):
    """The input has too few qualifying fragments for a finite profile."""


@dataclass(frozen=True)
class Fragments:
    """D21-eligible autosomal aligned spans, grouped by contig name."""

    spans: Mapping[str, tuple[np.ndarray, np.ndarray]]
    eligible_autosomal_reads: int


@dataclass(frozen=True)
class GriffinOntRun:
    input_id: str
    fragments: Fragments
    contig_lengths: Mapping[str, int]
    reference_fasta: Path
    reference_id: str
    reference_sha256: str
    ref_assets: Path
    site_lists: Mapping[str, Path]
    attempt_dir: Path
    registration_sha256: str | None = None
    minimum_reads: int = MIN_ELIGIBLE_AUTOSOMAL_READS


# ----------------------------------------------------------------- inputs
def _bed(path: Path, chroms: frozenset[str] | set[str]) -> dict[str, tuple[np.ndarray, np.ndarray]]:
    """BED intervals on ``chroms`` as sorted start and end arrays per contig."""

    rows: dict[str, tuple[list[int], list[int]]] = {}
    with path.open("r", encoding="utf-8") as stream:
        for line in stream:
            if not line.strip() or line.startswith(("#", "track", "browser")):
                continue
            fields = line.rstrip("\n").split("\t")
            if len(fields) < 3:
                raise ValueError("Griffin BED asset has a malformed row")
            try:
                start, end = int(fields[1]), int(fields[2])
            except ValueError:
                raise ValueError("Griffin BED asset has a malformed row") from None
            if fields[0] not in chroms:
                continue
            if start < 0 or end < start:
                raise ValueError("Griffin BED asset has a malformed interval")
            starts, ends = rows.setdefault(fields[0], ([], []))
            starts.append(start)
            ends.append(end)
    return {name: (np.sort(np.asarray(s, dtype=np.int64)), np.sort(np.asarray(e, dtype=np.int64)))
            for name, (s, e) in rows.items()}


def _overlaps(intervals: tuple[np.ndarray, np.ndarray] | None, left: np.ndarray, right: np.ndarray) -> np.ndarray:
    """Intervals overlapping each half-open query: #{start < right} - #{end <= left}."""

    if intervals is None:
        return np.zeros(np.shape(left), dtype=np.int64)
    starts, ends = intervals
    return (np.searchsorted(starts, right, side="left") - np.searchsorted(ends, left, side="right")).astype(np.int64)


def _sites(path: Path, chroms: tuple[str, ...]) -> list[tuple[str, int, int]]:
    """griffin_functions.import_and_filter_sites with sort_by none: (chrom, position, orientation)."""

    allowed = set(chroms)
    sites: list[tuple[str, int, int]] = []
    with path.open("r", encoding="utf-8", newline="") as stream:
        reader = csv.reader(stream, delimiter="\t", strict=True)
        header = next(reader, None)
        if header is None or header.count("Chrom") != 1 or header.count("position") != 1:
            raise ValueError("Griffin TF-site list needs one Chrom and one position header")
        chrom_index, position_index = header.index("Chrom"), header.index("position")
        strand_index = header.index("Strand") if header.count("Strand") == 1 else None
        for fields in reader:
            if len(fields) != len(header):
                raise ValueError("Griffin TF-site list has a malformed row")
            chrom = fields[chrom_index].strip()
            if chrom not in allowed:
                continue
            if not re.fullmatch(r"[0-9]+", fields[position_index].strip()):
                raise ValueError("Griffin TF-site positions must be nonnegative integers")
            strand = fields[strand_index].strip() if strand_index is not None else "0"
            orientation = 1 if strand in {"+", "1"} else -1 if strand in {"-", "-1"} else 0
            sites.append((chrom, int(fields[position_index]), orientation))
    return sites


def _frequency(directory: Path) -> np.ndarray:
    """Genome GC frequency per (length, num_GC) from the registered 15-500 bp tables."""

    low, high = GC_SIZE_RANGE
    table = np.zeros((high - low + 1, high + 1), dtype=np.float64)
    for length in range(low, high + 1):
        path = directory / f"{_FREQUENCY_PREFIX}{length}bp.GC_frequency.txt"
        with path.open("r", encoding="utf-8", newline="") as stream:
            reader = csv.DictReader(stream, delimiter="\t")
            if reader.fieldnames is None or not {"length", "num_GC", "number_of_fragments"} <= set(reader.fieldnames):
                raise ValueError("Griffin genome GC-frequency table is malformed")
            seen: set[int] = set()
            for row in reader:
                try:
                    row_length, num_gc, count = int(row["length"]), int(row["num_GC"]), float(row["number_of_fragments"])
                except (TypeError, ValueError):
                    raise ValueError("Griffin genome GC-frequency table is malformed") from None
                if row_length != length or not 0 <= num_gc <= length or num_gc in seen or not math.isfinite(count):
                    raise ValueError("Griffin genome GC-frequency table is malformed")
                seen.add(num_gc)
                table[length - low, num_gc] = count
        if len(seen) != length + 1:
            raise ValueError("Griffin genome GC-frequency table is incomplete")
    return table


# ----------------------------------------------------------- GC counting
_STRONG = np.zeros(256, dtype=bool)
_WEAK = np.zeros(256, dtype=bool)
_AMBIGUOUS = np.zeros(256, dtype=bool)
_STRONG[list(b"CGS")] = True
_WEAK[list(b"ATW")] = True
_AMBIGUOUS[list(b"NRYKMBDHV")] = True
_BLOCK = 1 << 22


def fragment_gc(fasta: object, contig: str, starts: np.ndarray, ends: np.ndarray) -> np.ndarray:
    """Reference G/C count of each span, with upstream's seeded ambiguous-base draw."""

    gc = np.zeros(len(starts), dtype=np.int64)
    if not len(starts):
        return gc
    order = np.argsort(starts, kind="stable")
    sorted_starts, sorted_ends = starts[order], ends[order]
    index = 0
    while index < len(order):
        block_start = int(sorted_starts[index])
        stop = int(np.searchsorted(sorted_starts, block_start + _BLOCK, side="left"))
        block_end = int(sorted_ends[index:stop].max())
        sequence = fasta.fetch(contig, block_start, block_end).upper().encode("ascii")  # type: ignore[attr-defined]
        if len(sequence) != block_end - block_start:
            raise ValueError("fragment span extends beyond the registered reference contig")
        codes = np.frombuffer(sequence, dtype=np.uint8)
        strong, ambiguous = _STRONG[codes], _AMBIGUOUS[codes]
        if not np.all(strong | _WEAK[codes] | ambiguous):
            raise ValueError("registered reference contains a base code Griffin cannot count")
        strong_sum = np.concatenate(([0], np.cumsum(strong, dtype=np.int64)))
        ambiguous_sum = np.concatenate(([0], np.cumsum(ambiguous, dtype=np.int64)))
        left = sorted_starts[index:stop] - block_start
        right = sorted_ends[index:stop] - block_start
        counts = strong_sum[right] - strong_sum[left]
        drawn = ambiguous_sum[right] - ambiguous_sum[left]
        for offset in np.flatnonzero(drawn):
            rng = np.random.default_rng(int(sorted_starts[index + offset]))
            counts[offset] += int(rng.integers(2, size=int(drawn[offset])).sum())
        gc[order[index:stop]] = counts
        index = stop
    return gc


# --------------------------------------------------------------- GC bias
def _median_smoothing(values: np.ndarray, positions: np.ndarray, fraction: float) -> np.ndarray:
    """griffin_GC_bias.median_smoothing, evaluated only at ``positions``."""

    size = len(values)
    bin_size = max(int(size * fraction), 50)
    out = np.empty(len(positions), dtype=np.float64)
    with np.errstate(all="ignore"):
        for slot, i in enumerate(positions):
            start, end = int(i - bin_size / 2), int(i + bin_size / 2)
            if start < 0:
                start, end = 0, bin_size
            if end >= size:
                start, end = size - bin_size, size
            window = values[start:end]
            out[slot] = np.nan if np.all(np.isnan(window)) else np.nanmedian(window)
    return out


def gc_bias(counts: np.ndarray, frequency: np.ndarray) -> np.ndarray:
    """Smoothed GC bias per (length - 15, num_GC); NaN where never observable."""

    low, high = GC_SIZE_RANGE
    lengths, gcs, bias, positions = [], [], [], []
    with np.errstate(all="ignore"):
        for length in range(low, high + 1):
            row = length - low
            observed = counts[row, :length + 1].astype(np.float64)
            expected = frequency[row, :length + 1]
            ratio = observed / expected
            finite = ratio[~np.isnan(ratio)]
            mean = finite.mean() if finite.size else np.nan
            lengths.append(np.full(length + 1, length))
            gcs.append(np.arange(length + 1))
            bias.append(ratio / mean)
            positions.append(expected)
    length_all = np.concatenate(lengths)
    gc_all = np.concatenate(gcs)
    bias_all = np.concatenate(bias)
    positions_all = np.concatenate(positions)
    content = gc_all / length_all
    order = np.lexsort((length_all, content))
    length_all, gc_all = length_all[order], gc_all[order]
    bias_all, positions_all = bias_all[order], positions_all[order]
    smoothed = np.full((high - low + 1, high + 1), np.nan, dtype=np.float64)
    for length in range(low, high + 1):
        window = np.flatnonzero((length_all >= int(length - 10)) & (length_all <= int(length + 10)))
        target = np.flatnonzero(length_all[window] == length)
        values = _median_smoothing(bias_all[window], target, 0.05)
        values = np.where(positions_all[window][target] == 0, np.nan, values)
        with np.errstate(all="ignore"):
            finite = values[~np.isnan(values)]
            values = values / (finite.mean() if finite.size else np.nan)
        smoothed[length - low, gc_all[window][target]] = values
    return smoothed


# ------------------------------------------------------- site profiling
def _savgol(values: np.ndarray, window: int, order: int) -> np.ndarray:
    """scipy.signal.savgol_filter(mode='interp') for a 1-D array."""

    half = window // 2
    offsets = np.arange(-half, half + 1, dtype=np.float64)
    design = offsets[:, None] ** np.arange(order + 1)[None, :]
    weights = np.linalg.pinv(design)[0]
    out = np.convolve(values, weights[::-1], mode="same")
    if not np.all(np.isfinite(values)):
        return np.full_like(values, np.nan)
    x = np.arange(window, dtype=np.float64)
    head = np.polyval(np.polyfit(x, values[:window], order), x[:half])
    tail = np.polyval(np.polyfit(x, values[-window:], order), x[window - half:])
    out[:half] = head
    out[-half:] = tail
    return out


def _bin(values: np.ndarray) -> np.ndarray:
    return np.sum(values.reshape(len(values), _WIDTH // STEP, STEP), axis=2)


def _per_bp(points: np.ndarray, weights: np.ndarray, positions: np.ndarray) -> np.ndarray:
    index = np.searchsorted(points, positions)
    hit = index < len(points)
    hit[hit] = points[index[hit]] == positions[hit]
    return np.where(hit, weights[np.minimum(index, max(len(points) - 1, 0))] if len(points) else 0.0, 0.0)


def _site_bins(
    sites: list[tuple[str, int, int]], contig_lengths: Mapping[str, int],
    midpoints: Mapping[str, tuple[np.ndarray, np.ndarray, np.ndarray]],
    excluded: dict[str, tuple[np.ndarray, np.ndarray]], mappability: MappabilityValues,
) -> tuple[np.ndarray, np.ndarray]:
    """Uncorrected and GC-corrected 15 bp bins for every site, upstream-oriented."""

    raw = np.empty((len(sites), _WIDTH // STEP), dtype=np.float64)
    corrected = np.empty_like(raw)
    offsets = np.arange(NORM_WINDOW[0], NORM_WINDOW[1], dtype=np.int64)
    for chunk in range(0, len(sites), _SITE_CHUNK):
        batch = sites[chunk:chunk + _SITE_CHUNK]
        rows = []
        for chrom, position, orientation in batch:
            length = contig_lengths[chrom]
            positions = position + offsets
            inside = (positions >= 0) & (positions < length)
            points, uncorrected, gc_weighted = midpoints.get(
                chrom, (np.zeros(0, np.int64), np.zeros(0), np.zeros(0)))
            u = np.where(inside, _per_bp(points, uncorrected, positions), np.nan)
            g = np.where(inside, _per_bp(points, gc_weighted, positions), np.nan)
            fetch_start = max(position + NORM_WINDOW[0], 0)
            fetch_end = min(position + NORM_WINDOW[1], length)
            mapped = np.full(_WIDTH, np.nan)
            if fetch_end > fetch_start:
                values = np.nan_to_num(np.asarray(mappability(chrom, fetch_start, fetch_end), dtype=np.float64))
                if values.shape != (fetch_end - fetch_start,):
                    raise ValueError("Griffin mappability track returned an unexpected interval")
                mapped[fetch_start - (position + NORM_WINDOW[0]):fetch_end - (position + NORM_WINDOW[0])] = values
            mapped = np.where(mapped == 0, np.nan, mapped)
            left = position + NORM_WINDOW[0] + np.arange(0, _WIDTH, STEP, dtype=np.int64)
            bin_excluded = _overlaps(excluded.get(chrom), left, left + STEP) > 0
            if orientation < 0:
                u, g, mapped, bin_excluded = u[::-1], g[::-1], mapped[::-1], bin_excluded[::-1]
            rows.append((u, g, mapped, bin_excluded))
        u_bins = _bin(np.stack([row[0] for row in rows]))
        g_bins = _bin(np.stack([row[1] for row in rows]))
        mapped_bins = _bin(np.stack([row[2] for row in rows]))
        excluded_bins = np.stack([row[3] for row in rows])
        for bins in (u_bins, g_bins):
            bins[excluded_bins] = np.nan
            with np.errstate(invalid="ignore"):
                bins[~(mapped_bins > 0)] = np.nan
        raw[chunk:chunk + len(batch)] = u_bins
        corrected[chunk:chunk + len(batch)] = g_bins
    return raw, corrected


def _outlier_mask(raw: np.ndarray) -> tuple[np.ndarray, float]:
    """griffin_merge_sites.make_outlier_mask over the uncorrected bins."""

    with np.errstate(all="ignore"):
        finite = raw[~np.isnan(raw)]
        mean = finite.mean() if finite.size else np.nan
        std = finite.std() if finite.size else np.nan
        scores = (raw - mean) / std
        mask = np.where(scores < 10, 1.0, np.nan)
    masked = raw * mask
    peak = np.nanmax(masked) if np.any(~np.isnan(masked)) else np.nan
    if peak < 2:
        mask = np.where(raw <= 2, 1.0, np.nan)
        masked = raw * mask
    cutoff = float(np.nanmax(masked)) if np.any(~np.isnan(masked)) else float("nan")
    return mask, cutoff


def _normalize(bins: np.ndarray) -> tuple[np.ndarray, float, float]:
    """normalize_and_smooth with individual False, smoothing True, CNA False."""

    with np.errstate(all="ignore"), _quiet():
        per_site_norm = np.nanmean(bins, axis=1) / STEP
        per_site_save = np.nanmean(bins[:, _SAVE_INDEX], axis=1) / STEP
        mean_norm = float(np.nanmean(per_site_norm))
        mean_save = float(np.nanmean(per_site_save))
        averaged = np.nanmean(bins, axis=0)
    window = int(np.floor(int(np.round(SMOOTHING_LENGTH / STEP) * STEP) / STEP))
    window = window + 1 if window % 2 == 0 else window
    smoothed = _savgol(averaged, window, 3)
    with np.errstate(all="ignore"), _quiet():
        smoothed = smoothed / np.nanmean(smoothed)
    return smoothed[_SAVE_INDEX], mean_norm, mean_save


class _quiet:
    """Silence numpy's empty-slice RuntimeWarning exactly where upstream would emit it."""

    def __enter__(self) -> None:
        import warnings

        self._catch = warnings.catch_warnings()
        self._catch.__enter__()
        warnings.simplefilter("ignore", RuntimeWarning)

    def __exit__(self, *args: object) -> None:
        self._catch.__exit__(*args)


def _format(value: float) -> str:
    return "" if math.isnan(value) else "%.5f" % value


def _write_tsv(path: Path, sample: str, rows: list[dict]) -> None:
    header = [str(int(column)) for column in SAVE_COLUMNS] + [
        "mean_reads_per_bp_in_normalization_window", "mean_reads_per_bp_in_saved_window",
        "mean_coverage", "central_coverage", "amplitude", "outlier_cutoff",
        "exclude_zero_mappability", "correction", "number_of_sites", "site_name",
        "smoothing", "CNA_normalization", "sample",
    ]
    lines = ["\t".join(header)]
    for row in rows:
        profile = row["profile"]
        with np.errstate(all="ignore"):
            mean_coverage = float(np.mean(profile))
            central = float(np.mean(profile[_CENTER_INDEX]))
            amplitude = float(np.abs(np.fft.fft(profile[_FFT_INDEX]))[FFT_INDEX])
        lines.append("\t".join([_format(value) for value in profile] + [
            _format(row["mean_norm"]), _format(row["mean_save"]), _format(mean_coverage),
            _format(central), _format(amplitude), _format(row["outlier_cutoff"]),
            "True", row["correction"], str(row["site_count"]), row["site_name"], "True", "False", sample,
        ]))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ------------------------------------------------------------------ run
def _require_reference(reference: Path, contig_lengths: Mapping[str, int]) -> None:
    import pysam

    with pysam.FastaFile(str(reference)) as fasta:
        expected = dict(zip(fasta.references, fasta.lengths))
    if not expected or dict(contig_lengths) != expected:
        raise ValueError("read-summary contigs do not match the registered reference FASTA")


def _open_mappability(path: Path) -> tuple[MappabilityValues, Callable[[], None]]:
    """Per-base values of the registered k100 Umap bigWig (pyBigWig)."""

    import pyBigWig

    track = pyBigWig.open(str(path))

    def values(chrom: str, start: int, end: int) -> np.ndarray:
        return np.asarray(track.values(chrom, start, end, numpy=True), dtype=np.float64)

    return values, track.close


def run_ont(config: GriffinOntRun, mappability: MappabilityValues | None = None) -> tuple[GriffinProfile, ...]:
    """Compute GC-corrected site profiles from single-end aligned spans.

    Writes the corrected TSV, the smoothed GC-bias table and a path-free
    provenance record into a fresh attempt directory, then validates the TSV
    with the Illumina path's strict parser.
    """

    import pysam

    if config.fragments.eligible_autosomal_reads < config.minimum_reads:
        raise GriffinOntDepth("Griffin requires more D21-eligible autosomal reads")
    if config.reference_id != "hg38":
        raise ValueError("Griffin is validated for hg38 only")
    if not re.fullmatch(r"[a-z0-9]{12,64}", config.input_id):
        raise ValueError("input_id must be a Lab hex identifier")
    if not config.site_lists:
        raise ValueError("Griffin needs TF-site lists")
    reference = config.reference_fasta
    if _digest(reference) != config.reference_sha256:
        raise ValueError("reference FASTA differs from the registered Lab reference")
    if not reference.with_suffix(reference.suffix + ".fai").is_file():
        raise ValueError("the registered reference FASTA needs an index for Griffin")
    _require_reference(reference, config.contig_lengths)
    assets = config.ref_assets
    for filename in _REF_FILES:
        if not (assets / filename).is_file():
            raise ValueError("Griffin reference asset is absent")
    chroms = _require_chrom_sizes(reference, assets / "hg38.standard.chrom.sizes")
    site_input_rows = {name: _require_site_contigs(path, chroms) for name, path in config.site_lists.items()}
    frequency = _frequency(assets / "genome_GC_frequency")
    mappable = _bed(assets / _MAPPABLE, _GC_COUNT_CHROMS)
    excluded: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    pieces: dict[str, tuple[list[np.ndarray], list[np.ndarray]]] = {}
    for filename in _EXCLUDE:
        for chrom, (starts, ends) in _bed(assets / filename, set(chroms)).items():
            pieces.setdefault(chrom, ([], []))[0].append(starts)
            pieces[chrom][1].append(ends)
    for chrom, (starts, ends) in pieces.items():
        excluded[chrom] = (np.sort(np.concatenate(starts)), np.sort(np.concatenate(ends)))

    low, high = GC_SIZE_RANGE
    counts = np.zeros((high - low + 1, high + 1), dtype=np.int64)
    profile_low, profile_high = PROFILE_TLEN_RANGE
    profile_fragments: dict[str, tuple[np.ndarray, np.ndarray, np.ndarray]] = {}
    with pysam.FastaFile(str(reference)) as fasta:
        for chrom, (starts, ends) in sorted(config.fragments.spans.items()):
            starts = np.asarray(starts, dtype=np.int64)
            ends = np.asarray(ends, dtype=np.int64)
            lengths = ends - starts
            keep = (lengths >= low) & (lengths <= high)
            starts, ends, lengths = starts[keep], ends[keep], lengths[keep]
            gc = fragment_gc(fasta, chrom, starts, ends)
            if chrom in _GC_COUNT_CHROMS:
                multiplicity = _overlaps(mappable.get(chrom), starts, ends)
                np.add.at(counts, (lengths - low, gc), multiplicity)
            profile = (lengths >= profile_low) & (lengths <= profile_high)
            if chrom in chroms:
                # Compact per-contig storage: midpoints fit int32, 100-200 bp lengths and GC fit int16.
                profile_fragments[chrom] = (
                    ((starts[profile] + ends[profile]) // 2).astype(np.int32),
                    lengths[profile].astype(np.int16), gc[profile].astype(np.int16))
            del starts, ends, lengths, gc
    if not counts.any():
        raise GriffinOntDepth("no qualifying fragments overlap the registered mappable regions")
    bias = gc_bias(counts, frequency)
    lookup = np.where(bias < 0.05, np.nan, bias)
    midpoints: dict[str, tuple[np.ndarray, np.ndarray, np.ndarray]] = {}
    for chrom in sorted(profile_fragments):
        points, lengths, gc = profile_fragments.pop(chrom)
        weight = lookup[lengths.astype(np.int64) - low, gc.astype(np.int64)]
        with np.errstate(divide="ignore"):
            weight = np.where(np.isnan(weight), 0.0, 1.0 / weight)
        unique, inverse = np.unique(points.astype(np.int64), return_inverse=True)
        uncorrected = np.bincount(inverse, minlength=len(unique)).astype(np.float64)
        corrected = np.zeros(len(unique), dtype=np.float64)
        np.add.at(corrected, inverse, weight)
        corrected = np.round(corrected, 5).astype(np.float32).astype(np.float64)
        midpoints[chrom] = (unique, uncorrected, corrected)

    close: Callable[[], None] | None = None
    values_of = mappability
    if values_of is None:
        values_of, close = _open_mappability(assets / _MAPPABILITY)
    contig_lengths = dict(config.contig_lengths)
    sample = "lab_" + config.input_id
    rows: list[dict] = []
    try:
        for name, path in config.site_lists.items():
            sites = _sites(path, chroms)
            if not sites:
                raise ValueError("Griffin TF-site list has no scoped sites")
            raw, corrected = _site_bins(sites, contig_lengths, midpoints, excluded, values_of)
            mask, cutoff = _outlier_mask(raw)
            profile, mean_norm, mean_save = _normalize(corrected * mask)
            if not np.all(np.isfinite(profile)):
                raise GriffinOntDepth("Griffin's site profile has bins without usable coverage")
            rows.append({"profile": profile, "mean_norm": mean_norm, "mean_save": mean_save,
                         "outlier_cutoff": cutoff, "correction": "GC_corrected",
                         "site_count": len(sites), "site_name": name})
    finally:
        if close is not None:
            close()
    attempt = config.attempt_dir
    attempt.mkdir(mode=0o700)
    output = attempt / f"{sample}.GC_corrected.coverage.tsv"
    _write_tsv(output, sample, rows)
    bias_path = attempt / f"{sample}.GC_bias.txt"
    bias_lines = ["length\tnum_GC\tsmoothed_GC_bias"]
    for length in range(low, high + 1):
        for num_gc in range(length + 1):
            bias_lines.append(f"{length}\t{num_gc}\t{float(bias[length - low, num_gc])!r}")
    bias_path.write_text("\n".join(bias_lines) + "\n", encoding="utf-8")
    profiles = read_corrected_profiles(output, expected_sample=sample)
    if {profile.site_name for profile in profiles} != set(config.site_lists):
        raise ValueError("Griffin ONT output site lists do not match the requested site lists")
    if any(profile.site_count > site_input_rows[profile.site_name] for profile in profiles):
        raise ValueError("Griffin ONT output exceeds the requested TF-site list")
    manifest = {
        "status": "complete", "input_id": config.input_id, "method": ONT_METHOD,
        "ported_from_upstream_commit": GRIFFIN_COMMIT, "reference_id": config.reference_id,
        "platform": "ont", "registration_sha256": config.registration_sha256,
        "eligible_autosomal_reads": config.fragments.eligible_autosomal_reads,
        "minimum_eligible_autosomal_reads": config.minimum_reads,
        "eligibility_source": "read_summary", "eligibility_predicate": ELIGIBILITY_PREDICATE,
        "site_list_input_rows": site_input_rows,
        "gc_bias_size_range": list(GC_SIZE_RANGE), "profile_size_range": list(PROFILE_TLEN_RANGE),
        "gc_bias_sha256": _digest(bias_path), "output_sha256": _digest(output),
        "profile_count": len(profiles),
    }
    (attempt / "provenance.json").write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
    return profiles


_AUTOSOME = re.compile(r"chr(?:[1-9]|1[0-9]|2[0-2])")


def fragments_from_summary(summary: object, mask: np.ndarray | None) -> tuple[Fragments, dict[str, int]]:
    """D21-eligible primary autosomal spans of a read summary, under a derived mask."""

    contigs, lengths = summary.contigs, summary.lengths  # type: ignore[attr-defined]
    autosomes = np.array([index for index, name in enumerate(contigs) if _AUTOSOME.fullmatch(name)], dtype=np.int16)
    if not autosomes.size:
        raise ValueError("Griffin requires canonical hg38 autosomes")
    eligible = summary.eligible(contigs=autosomes)  # type: ignore[attr-defined]
    if mask is not None:
        eligible = eligible & mask
    if np.any(summary.column("flag")[eligible] & 0x1):  # type: ignore[attr-defined]
        raise ValueError("Griffin's ONT path requires single-end reads; this input has paired records")
    contig = summary.column("contig")  # type: ignore[attr-defined]
    start, end = summary.column("start"), summary.column("end")  # type: ignore[attr-defined]
    spans = {}
    # One contig at a time keeps peak memory near the final int32 span arrays.
    for cid in autosomes:
        selected = eligible & (contig == cid)
        if selected.any():
            spans[contigs[int(cid)]] = (np.asarray(start[selected]), np.asarray(end[selected]))
    return Fragments(spans, int(eligible.sum())), dict(zip(contigs, lengths))
