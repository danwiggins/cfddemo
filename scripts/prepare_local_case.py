"""Prepare an ignored, immutable local query-length bundle from BAM files."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evidence_inspector.models import SelectionParameters
from evidence_inspector.preparation import prepare_length_artifact


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Stream BAM records into a bounded derived-length artifact. "
            "Raw sequences, read IDs, filenames, and local paths are not published."
        )
    )
    parser.add_argument("input_dir", type=Path)
    parser.add_argument("output_dir", type=Path)
    parser.add_argument("--pattern", default="*.bam")
    parser.add_argument("--max-accepted-reads", type=int, default=100_000)
    parser.add_argument("--max-inspected-records", type=int, default=1_000_000)
    parser.add_argument("--max-elapsed-seconds", type=float, default=600.0)
    parser.add_argument(
        "--max-serialized-artifact-bytes", type=int, default=2_097_152
    )
    parser.add_argument("--max-read-length-bp", type=int, default=1_000_000)
    parser.add_argument(
        "--complete-registered-collection",
        action="store_true",
        help="Mark selected BAMs as the complete registered collection.",
    )
    return parser


def main() -> int:
    args = _parser().parse_args()
    inputs = sorted(args.input_dir.glob(args.pattern))
    if not inputs:
        raise SystemExit("no BAM inputs matched the requested pattern")
    parameters = SelectionParameters(
        ordering_rule=f"lexicographic ordering of registered inputs matching {args.pattern!r}",
        max_accepted_reads=args.max_accepted_reads,
        max_inspected_records=args.max_inspected_records,
        max_elapsed_seconds=args.max_elapsed_seconds,
        max_serialized_artifact_bytes=args.max_serialized_artifact_bytes,
        max_read_length_bp=args.max_read_length_bp,
    )
    manifest = prepare_length_artifact(
        inputs,
        args.output_dir,
        selection_parameters=parameters,
        partial_collection=not args.complete_registered_collection,
    )
    summary = {
        "artifact_id": manifest.artifact.id,
        "artifact_sha256": manifest.artifact.sha256,
        "artifact_size_bytes": manifest.artifact.size_bytes,
        "accepted_count": manifest.accepted_count,
        "inspected_count": manifest.inspected_count,
        "stop_reason": manifest.stop_reason,
        "scanned_complete_input": manifest.scanned_complete_input,
    }
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
