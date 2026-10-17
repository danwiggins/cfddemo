"""Read-only, bounded persistent-byte inventory for Lab D30/AC4 verification."""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
from collections import defaultdict
from pathlib import Path
from typing import Any

SHA256 = re.compile(r"[0-9a-f]{64}\Z")
MAX_RECORD_BYTES = 1024 * 1024


class InventoryError(ValueError):
    """The inventory cannot safely make the requested claim."""


def _sha(value: Any, label: str) -> str:
    if not isinstance(value, str) or not SHA256.fullmatch(value):
        raise InventoryError(f"{label} must be a lowercase SHA256")
    return value


def _record(lab: Path, source_id: str) -> dict[str, Any]:
    path = lab / "sources" / f"{source_id}.json"
    try:
        if not stat.S_ISREG(path.lstat().st_mode):
            raise InventoryError("source record is not a regular file")
        with path.open("rb") as stream:
            data = stream.read(MAX_RECORD_BYTES + 1)
    except OSError as exc:
        raise InventoryError("source record is unavailable") from exc
    if len(data) > MAX_RECORD_BYTES:
        raise InventoryError("source record exceeds size limit")
    try:
        record = json.loads(data)
    except (UnicodeError, ValueError) as exc:
        raise InventoryError("source record is invalid JSON") from exc
    if not isinstance(record, dict) or record.get("source_id") != source_id:
        raise InventoryError("source record identity does not match")
    return record


def _inventory(lab: Path, max_entries: int) -> tuple[dict[str, int], int, set[str]]:
    """Count each path once; reject symlinks, special files and incomplete walks."""
    sizes: dict[str, int] = defaultdict(int)
    pending = [lab]
    seen = 0
    staging_entries = 0
    input_entries: set[str] = set()
    while pending:
        directory = pending.pop()
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    seen += 1
                    if seen > max_entries:
                        raise InventoryError("inventory entry limit exceeded")
                    try:
                        info = entry.stat(follow_symlinks=False)
                    except OSError as exc:
                        raise InventoryError("inventory entry became unreadable") from exc
                    if directory == lab / "inputs":
                        input_entries.add(entry.name)
                    if entry.name.startswith(".staging-"):
                        staging_entries += 1
                    if stat.S_ISDIR(info.st_mode):
                        pending.append(Path(entry.path))
                    elif stat.S_ISREG(info.st_mode):
                        relative = Path(entry.path).relative_to(lab)
                        sizes[relative.as_posix()] = info.st_size
                    else:
                        raise InventoryError("inventory contains a link or special file")
        except OSError as exc:
            raise InventoryError("inventory directory is unreadable") from exc
    return dict(sizes), staging_entries, input_entries


def account(root: Path, source_id: str, input_id: str | None = None, *,
            max_entries: int = 100_000) -> dict[str, Any]:
    """Measure one stored sample and the complete Lab store at one instant."""
    _sha(source_id, "source_id")
    if input_id is not None:
        _sha(input_id, "input_id")
    if max_entries < 1:
        raise InventoryError("max_entries must be positive")
    lab = root / "lab"
    record = _record(lab, source_id)
    full = record.get("inputs")
    derived = record.get("derived")
    if not isinstance(full, list) or not isinstance(derived, list):
        raise InventoryError("source input lists are malformed")
    chunks = record.get("chunks")
    if (isinstance(chunks, list) and chunks
            and all(isinstance(row, dict) and type(row.get("size")) is int
                    and row["size"] >= 0 for row in chunks)):
        recorded_chunk_bytes: int | None = sum(row["size"] for row in chunks)
    else:
        recorded_chunk_bytes = None
    full_ids = []
    for row in full:
        if not isinstance(row, dict) or row.get("kind") not in {"aligned", "prealigned"}:
            raise InventoryError("source full input row is malformed")
        full_ids.append(_sha(row.get("input_id"), "full input_id"))
    if not full_ids or (input_id is None and len(full_ids) != 1):
        raise InventoryError("select one full input_id from the source record")
    selected = input_id or full_ids[0]
    if selected not in full_ids:
        raise InventoryError("input_id is not a full input of this source")
    derived_ids = []
    for row in derived:
        if not isinstance(row, dict):
            raise InventoryError("source derived input row is malformed")
        parent = _sha(row.get("parent_input_id"), "derived parent_input_id")
        derived_id = _sha(row.get("input_id"), "derived input_id")
        if parent == selected:
            derived_ids.append(derived_id)
    sample_ids = {selected, *derived_ids}
    sizes, staging_entries, input_entries = _inventory(lab, max_entries)
    bam_bytes = sizes.get(f"inputs/{selected}/aligned.bam")
    bai_bytes = sizes.get(f"inputs/{selected}/aligned.bam.bai")
    if bam_bytes is None or bai_bytes is None:
        raise InventoryError("required aligned BAM or index is unavailable")
    denominator = bam_bytes + bai_bytes
    if denominator == 0:
        raise InventoryError("aligned BAM and index have zero combined bytes")
    other_input_present = bool(input_entries - sample_ids - {"index.json"})
    sample_inputs = 0
    other_inputs = 0
    shared_input_cache = 0
    sample_results = 0
    other_results = 0
    result_algorithms: dict[str, int] = defaultdict(int)
    shared_index = 0
    staging = 0
    temporary = 0
    for name, size in sizes.items():
        parts = name.split("/")
        if any(part.startswith(".staging-") for part in parts):
            staging += size
        if parts[0] == "tmp":
            temporary += size
        if parts[0] == "inputs":
            if len(parts) > 2 and parts[1] in sample_ids:
                sample_inputs += size
            elif name == "inputs/index.json":
                shared_input_cache += size
            else:
                other_inputs += size
        elif parts[0] == "results":
            if len(parts) > 2 and parts[1] in sample_ids:
                sample_results += size
                if len(parts) > 2:
                    result_algorithms[parts[2]] += size
            else:
                other_results += size
        elif parts[0] == "refs" and name.endswith(".mmi"):
            shared_index += size
    literal_inputs = sample_inputs + shared_input_cache + other_inputs
    complete_lab = sum(sizes.values())
    other_lab = complete_lab - literal_inputs - sample_results - other_results - shared_index
    return {
        "schema": "traceback.lab.storage-accounting.v1",
        "source_id": source_id, "full_input_id": selected,
        "derived_input_ids": sorted(sample_ids - {selected}),
        "recorded_included_chunk_bytes": recorded_chunk_bytes,
        "excluded_chunk_bytes": None,
        "aligned_bam_bytes": bam_bytes, "aligned_index_bytes": bai_bytes,
        "aligned_bam_plus_index_bytes": denominator,
        "ac4_numerator_scope": "all apparent file bytes under lab/inputs",
        "ac4_denominator_basis": "selected full aligned.bam plus aligned.bam.bai",
        "literal_lab_inputs_bytes": literal_inputs,
        "literal_lab_inputs_ratio": literal_inputs / denominator if not other_input_present else None,
        "literal_ac4_single_sample_applicable": not other_input_present,
        "literal_ac4_at_most_1_2": (literal_inputs * 5 <= denominator * 6)
        if not other_input_present else None,
        "sample_input_bytes": sample_inputs,
        "shared_input_hash_cache_bytes": shared_input_cache,
        "other_input_bytes": other_inputs,
        "sample_result_bytes": sample_results,
        "sample_result_bytes_by_algorithm": dict(sorted(result_algorithms.items())),
        "other_result_bytes": other_results,
        "shared_mmi_bytes": shared_index,
        "other_lab_bytes": other_lab,
        "staging_bytes": staging, "staging_entry_count": staging_entries,
        "temporary_bytes": temporary,
        "complete_lab_persistent_bytes": complete_lab,
        "accounting_basis": "apparent file bytes; each hard-link path is counted separately",
        "complete_lab_file_count": len(sizes),
        "inventory_consistency": "best-effort snapshot; stop Lab writers for a stable receipt",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--source-id", required=True)
    parser.add_argument("--input-id")
    parser.add_argument("--max-entries", type=int, default=100_000)
    args = parser.parse_args()
    try:
        result = account(args.root, args.source_id, args.input_id,
                         max_entries=args.max_entries)
    except InventoryError as exc:
        parser.exit(2, f"storage inventory refused: {exc}\n")
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
