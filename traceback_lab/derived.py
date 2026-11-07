"""Derived inputs: downsample fractions, split halves and the synthetic CNA spike (D25, D31, D33).

A derived input is a row-mask view of its parent aligned input.  Pysam-only
algorithms read the parent's read summary through the mask; nothing is
materialized for them.  Copy-number needs a file, so every derived input gets
a **stripped counting BAM** (position, MAPQ and CIGAR only, as the signed
``write_counting_bam`` writes), about 1-2% of the aligned BAM.  All derived
counting BAMs of one parent are written in **one pass** over the parent.

Synthetic CNA spike (D25 a): for a one-copy gain at tumour fraction TF the
expected coverage ratio is 1 + TF/2, so each eligible read in a gain arm is
duplicated with probability TF/2; each eligible read in a loss arm is removed
with probability TF/2.  The draw is ``h64("cna-<tf>" ‖ qname) < TF/2 · 2⁶⁴``,
so it is deterministic.  Duplicates are renamed ``<qname>#dup1`` before any
predicate sees them.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from fractions import Fraction
from math import ceil
from pathlib import Path
from typing import Any

from traceback_runner.serialization import canonical_json_bytes

from .identity import derived_input_id, derived_label, validate_fraction
from .read_summary import DS_SALT, SPLIT_SALT, h64
from .store import LabStore

DERIVED_SCHEMA = "traceback.lab.derived-input.v1"
COUNTING_BAM = "counting.bam"
DEFAULT_TFS = (0.03, 0.10, 0.30)
DEFAULT_FRACTIONS = (0.5, 0.25, 0.1)
GAIN_ARMS = ("1q", "8q")
LOSS_ARMS = ("13q", "17p")


@dataclass(frozen=True)
class Arm:
    name: str  # e.g. "1q"
    contig: str
    start: int
    end: int
    kind: str  # gain | loss

    def public(self) -> dict[str, Any]:
        return {"name": self.name, "contig": self.contig, "start": self.start, "end": self.end, "kind": self.kind}


@dataclass(frozen=True)
class DerivedSpec:
    transform: str  # downsample | split | cna
    parameters: Mapping[str, Any]
    salt: str

    def input_id(self, parent: str) -> str:
        return derived_input_id(parent=parent, transform=self.transform, parameters=dict(self.parameters), salt=self.salt)

    def label(self) -> str:
        """Short label without the sample prefix: ``0.5``, ``split-A``, ``cna-0.1``."""

        return derived_label("x", self.transform, self.parameters).split("@", 1)[1]

    def mask_spec(self) -> dict[str, Any]:
        """What a row-mask consumer needs (``read_summary.derived_mask``)."""

        if self.transform == "downsample":
            return {"transform": "downsample", "fraction": self.parameters["fraction"]}
        if self.transform == "split":
            return {"transform": "split", "half": self.parameters["half"]}
        return {"transform": "cna", "tf": self.parameters["tf"]}


def downsample(fraction: float) -> DerivedSpec:
    return DerivedSpec("downsample", {"fraction": validate_fraction(fraction)}, DS_SALT.decode())


def split(half: str) -> DerivedSpec:
    if half not in {"A", "B"}:
        raise ValueError("a split half is A or B")
    return DerivedSpec("split", {"half": half}, SPLIT_SALT.decode())


def cna(tf: float, arms: Sequence[Arm]) -> DerivedSpec:
    tf = validate_fraction(tf)
    return DerivedSpec("cna", {"tf": tf, "arms": [arm.public() for arm in arms]}, f"cna-{tf!r}")


def hg38_arms(centromeres: Mapping[str, tuple[int, int]], lengths: Mapping[str, int]) -> list[Arm]:
    """Default arms (gains 1q, 8q; losses 13q, 17p) from centromere intervals."""

    arms: list[Arm] = []
    for name, kind in [*((arm, "gain") for arm in GAIN_ARMS), *((arm, "loss") for arm in LOSS_ARMS)]:
        contig = f"chr{name[:-1]}"
        start, end = centromeres[contig]
        if name.endswith("p"):
            arms.append(Arm(name, contig, 0, start, kind))
        else:
            arms.append(Arm(name, contig, end, lengths[contig], kind))
    return arms


def centromeres_from_table(path: Path) -> dict[str, tuple[int, int]]:
    """Outer bounds of each contig's centromere rows (ichorCNA's GRCh38 acen table)."""

    from evidence_inspector.ichor_adapter import parse_centromere_table

    bounds: dict[str, tuple[int, int]] = {}
    for interval in parse_centromere_table(path):
        contig = interval.contig
        start, end = int(interval.start), int(interval.end)
        if contig in bounds:
            start, end = min(start, bounds[contig][0]), max(end, bounds[contig][1])
        bounds[contig] = (start, end)
    return bounds


def _threshold(probability: float) -> int:
    return ceil(Fraction(probability) * 2**64)


@dataclass
class _Writer:
    spec: DerivedSpec
    staging: Path
    handle: Any
    counted: int = 0
    duplicated: int = 0
    removed: int = 0


COUNTING_DIR = "counting"


@dataclass(frozen=True)
class DerivedInput:
    """A derived input: ``manifest.json`` always; ``counting/`` (a stripped
    counting BAM) only once a counting-BAM consumer has needed it."""

    spec: DerivedSpec
    input_id: str
    parent_input_id: str
    directory: Path
    manifest: Mapping[str, Any] = field(default_factory=dict)

    @property
    def counting_dir(self) -> Path:
        return self.directory / COUNTING_DIR

    @property
    def counting_bam(self) -> Path:
        return self.counting_dir / COUNTING_BAM

    @property
    def counting(self) -> Mapping[str, Any] | None:
        try:
            return json.loads((self.counting_dir / "counting.json").read_bytes())
        except (FileNotFoundError, ValueError):
            return None

    @property
    def counted_reads(self) -> int | None:
        counting = self.counting
        return int(counting["counted_reads"]) if counting else None

    @property
    def records_scanned(self) -> int | None:
        counting = self.counting
        return int(counting["records_scanned"]) if counting else None


def load_derived(store: LabStore, parent_input_id: str, spec: DerivedSpec) -> DerivedInput | None:
    input_id = spec.input_id(parent_input_id)
    directory = store.inputs_dir / input_id
    try:
        manifest = json.loads((directory / "manifest.json").read_bytes())
    except (FileNotFoundError, ValueError):
        return None
    return DerivedInput(spec, input_id, parent_input_id, directory, manifest)


def _unique(specs: Iterable[DerivedSpec], parent_input_id: str) -> list[DerivedSpec]:
    wanted: list[DerivedSpec] = []
    for spec in specs:  # one entry per derived identity (``--tf 0.1,0.10`` is one spike)
        if all(spec.input_id(parent_input_id) != other.input_id(parent_input_id) for other in wanted):
            wanted.append(spec)
    return wanted


def materialize(
    store: LabStore,
    parent_input_id: str,
    parent_bam: Path,
    specs: Iterable[DerivedSpec],
    *,
    counting_contigs: Sequence[str],
    min_mapq: int,
    counting: bool = True,
) -> dict[str, DerivedInput]:
    """Publish every missing derived input of ``parent`` (callers hold the lock).

    Manifests are cheap (no read pass).  With ``counting`` every derived input
    without a counting BAM gets one, all written in **one** pass over the
    parent; summary-only runs never touch the BAM (D33).
    """

    wanted = _unique(specs, parent_input_id)
    found: dict[str, DerivedInput] = {}
    for spec in wanted:
        existing = load_derived(store, parent_input_id, spec)
        if existing is None:
            input_id = spec.input_id(parent_input_id)
            manifest = {
                "schema": DERIVED_SCHEMA,
                "input_id": input_id,
                "parent_input_id": parent_input_id,
                "transform": spec.transform,
                "parameters": dict(spec.parameters),
                "salt": spec.salt,
                "label": spec.label(),
                "created_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }

            def build(staging: Path, manifest: dict[str, Any] = manifest) -> None:
                (staging / "manifest.json").write_bytes(canonical_json_bytes(manifest))

            try:
                store.publish_directory(store.inputs_dir, input_id, build)
            except FileExistsError:
                pass
            store.bump_generation()
            existing = load_derived(store, parent_input_id, spec)
            assert existing is not None
        found[spec.label()] = existing
    if counting:
        need = [item for item in found.values() if item.counting is None]
        for item in found.values():
            current = item.counting
            if current is not None and (
                current.get("counting_contigs") != list(counting_contigs) or current.get("min_mapq") != min_mapq
            ):
                raise ValueError("a stored derived input was counted with other contigs or MAPQ")
        if need:
            _write_counting(store, parent_bam, need, counting_contigs=counting_contigs, min_mapq=min_mapq)
    return found


def _write_counting(
    store: LabStore,
    parent_bam: Path,
    items: Sequence[DerivedInput],
    *,
    counting_contigs: Sequence[str],
    min_mapq: int,
) -> None:
    import shutil

    import pysam

    from evidence_inspector.cell_origin_prefilter import prefilter_exclusion

    staging_root = store.lab / "tmp" / f"derive-{time.time_ns()}"
    staging_root.mkdir(parents=True)
    writers: list[_Writer] = []
    scanned = 0
    try:
        with pysam.AlignmentFile(str(parent_bam), "rb") as reader:
            header = pysam.AlignmentHeader.from_dict(
                {
                    "HD": {"VN": "1.6", "SO": "coordinate"},
                    "SQ": [{"SN": name, "LN": reader.get_reference_length(name)} for name in reader.references],
                }
            )
            for item in items:
                directory = staging_root / item.input_id
                directory.mkdir()
                writers.append(
                    _Writer(item.spec, directory, pysam.AlignmentFile(str(directory / COUNTING_BAM), "wb", header=header))
                )
            predicates = [_predicate(writer.spec) for writer in writers]
            for contig in counting_contigs:
                tid = header.get_tid(contig)
                for record in reader.fetch(contig):
                    scanned += 1
                    if prefilter_exclusion(record.flag, record.mapping_quality, min_mapq=min_mapq) is not None:
                        continue
                    hashes = _Hashes(record.query_name or "")
                    stripped = pysam.AlignedSegment(header)
                    stripped.query_name = "r"
                    stripped.flag = 0
                    stripped.reference_id = tid
                    stripped.reference_start = record.reference_start
                    stripped.mapping_quality = record.mapping_quality
                    stripped.cigartuples = record.cigartuples
                    for writer, predicate in zip(writers, predicates):
                        copies = predicate(hashes, contig, record.reference_start)
                        for _ in range(copies):
                            writer.handle.write(stripped)
                        writer.counted += copies
                        if copies == 0 and writer.spec.transform == "cna":
                            writer.removed += 1
                        if copies == 2:
                            writer.duplicated += 1
        for writer in writers:
            writer.handle.close()
            pysam.index(str(writer.staging / COUNTING_BAM))
        for item, writer in zip(items, writers):
            facts = {
                "counting_contigs": list(counting_contigs),
                "min_mapq": min_mapq,
                "records_scanned": scanned,
                "counted_reads": writer.counted,
                "duplicated": writer.duplicated,
                "removed": writer.removed,
            }

            def build(staging: Path, source: Path = writer.staging, facts: dict[str, Any] = facts) -> None:
                for path in source.iterdir():
                    path.rename(staging / path.name)
                (staging / "counting.json").write_bytes(canonical_json_bytes(facts))

            try:
                store.publish_directory(item.directory, COUNTING_DIR, build)
            except FileExistsError:
                pass
            store.bump_generation()
    finally:
        for writer in writers:
            try:
                writer.handle.close()
            except Exception:  # noqa: BLE001
                pass
        shutil.rmtree(staging_root, ignore_errors=True)


class _Hashes:
    """Per-record salted hashes, each computed at most once per record."""

    __slots__ = ("name", "_cache")

    def __init__(self, name: str) -> None:
        self.name = name
        self._cache: dict[bytes, int] = {}

    def __call__(self, salt: bytes) -> int:
        value = self._cache.get(salt)
        if value is None:
            value = self._cache[salt] = h64(salt, self.name)
        return value


def _predicate(spec: DerivedSpec):  # type: ignore[no-untyped-def]
    """``(hashes, contig, start) -> copies written`` (0, 1 or 2)."""

    if spec.transform == "downsample":
        threshold = _threshold(float(spec.parameters["fraction"]))
        return lambda hashes, contig, start: int(hashes(DS_SALT) < threshold)
    if spec.transform == "split":
        want_a = spec.parameters["half"] == "A"
        return lambda hashes, contig, start: int((hashes(SPLIT_SALT) < 2**63) == want_a)
    if spec.transform == "cna":
        tf = float(spec.parameters["tf"])
        threshold = _threshold(tf / 2)
        salt = spec.salt.encode()
        arms = [Arm(**arm) for arm in spec.parameters["arms"]]

        def spike(hashes: _Hashes, contig: str, start: int) -> int:
            for arm in arms:
                if arm.contig == contig and arm.start <= start < arm.end:
                    drawn = hashes(salt) < threshold
                    if arm.kind == "gain":
                        # The duplicate is a renamed copy (<qname>#dup1); a
                        # stripped counting BAM carries no names at all.
                        return 2 if drawn else 1
                    return 0 if drawn else 1
            return 1

        return spike
    raise ValueError(f"unknown transform {spec.transform!r}")


def record_derived(store: LabStore, source_id: str, derived: Iterable[DerivedInput]) -> None:
    """List derived inputs on the source record (hashes only)."""

    from .ingest import source_record
    from .store import write_atomic

    record = source_record(store, source_id)
    if record is None:
        return
    entries = {entry["input_id"]: entry for entry in record.get("derived", [])}
    for item in derived:
        entries[item.input_id] = {
            "input_id": item.input_id,
            "parent_input_id": item.parent_input_id,
            "label": item.spec.label(),
            "transform": item.spec.transform,
            "parameters": dict(item.spec.parameters),
        }
    record["derived"] = sorted(entries.values(), key=lambda entry: (entry["transform"], entry["label"]))
    write_atomic(store.sources_dir / f"{source_id}.json", canonical_json_bytes(record))


__all__ = [
    "Arm",
    "DEFAULT_FRACTIONS",
    "DEFAULT_TFS",
    "DerivedInput",
    "DerivedSpec",
    "centromeres_from_table",
    "cna",
    "downsample",
    "hg38_arms",
    "load_derived",
    "materialize",
    "record_derived",
    "split",
]
