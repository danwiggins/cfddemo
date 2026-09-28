#!/usr/bin/env python3
"""Explicit research-only entry point for the pinned Modkit 0.6.4 adapter."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from pydantic import ValidationError

from evidence_inspector.cell_origin_inputs import CellOriginInputError
from evidence_inspector.modkit_adapter import (
    ModkitExecutionManifest,
    load_modkit_extract_full_064,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Create a local v2 methylation research record from a pinned "
            "Modkit 0.6.4 extract-full execution."
        )
    )
    parser.add_argument(
        "--methylation-input-mode",
        required=True,
        choices=("modkit-0.6.4-full-cmh-v2",),
        help="Required non-default development opt-in.",
    )
    parser.add_argument("--extract-full", required=True, type=Path)
    parser.add_argument("--prefiltered-bam", required=True, type=Path)
    parser.add_argument("--execution-manifest", required=True, type=Path)
    parser.add_argument("--reference-fasta", required=True, type=Path)
    parser.add_argument("--reference-fai", required=True, type=Path)
    parser.add_argument("--reference-id", required=True)
    parser.add_argument("--source-model-id", required=True)
    parser.add_argument("--source-model-version", required=True)
    parser.add_argument("--combined-call-threshold", required=True, type=float)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--maximum-rows", type=int, default=1_000_000)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    salt = os.environ.get("TRACEBACK_FRAGMENT_HASH_SALT")
    if not salt:
        print(
            "Modkit adaptation is blocked: set TRACEBACK_FRAGMENT_HASH_SALT",
            file=sys.stderr,
        )
        return 2
    try:
        manifest = ModkitExecutionManifest.model_validate_json(
            arguments.execution_manifest.read_bytes()
        )
        result = load_modkit_extract_full_064(
            arguments.extract_full,
            manifest=manifest,
            prefiltered_bam_path=arguments.prefiltered_bam,
            fasta_path=arguments.reference_fasta,
            fai_path=arguments.reference_fai,
            fragment_hash_salt=salt.encode("utf-8"),
            probability_threshold=arguments.combined_call_threshold,
            source_model_id=arguments.source_model_id,
            source_model_version=arguments.source_model_version,
            reference_id=arguments.reference_id,
            max_rows=arguments.maximum_rows,
        )
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        temporary = arguments.output.with_name(arguments.output.name + ".tmp")
        temporary.write_text(result.model_dump_json(indent=2) + "\n", encoding="utf-8")
        temporary.replace(arguments.output)
    except (CellOriginInputError, OSError, ValidationError, ValueError) as exc:
        print(f"Modkit adaptation failed: {exc}", file=sys.stderr)
        return 1
    print(result.normalized_output_sha256)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
