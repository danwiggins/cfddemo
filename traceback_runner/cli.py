"""Command-line entry point for synthetic runner contract work."""

from __future__ import annotations

import argparse
from collections.abc import Sequence

from evidence_inspector.models import canonical_json_bytes, sha256_bytes

from .contracts import InputKind, JobRequest, job_key


def _synthetic_request() -> JobRequest:
    return JobRequest(
        sample_token="sample.synthetic",
        input_kind=InputKind.MODBAM,
        input_tree_sha256_local=sha256_bytes(b"synthetic-modbam"),
        workflow_release_sha256=sha256_bytes(b"synthetic-workflow"),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="traceback")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("doctor", help="check the synthetic contract runtime")
    demo = subparsers.add_parser("demo", help="run the offline contract demo")
    demo.add_argument("--json", action="store_true", dest="as_json")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "doctor":
        print("PASS  Python contract runtime is available")
        print("NOTE  Real-data execution is not enabled")
        return 0

    request = _synthetic_request()
    result = {
        "schema_version": "traceback.synthetic-demo.v1",
        "job_key": job_key(request),
        "input_kind": request.input_kind,
        "status": "contract_validated",
        "real_data_enabled": False,
    }
    if args.as_json:
        print(canonical_json_bytes(result).decode("utf-8"))
    else:
        print("PASS  Synthetic job contract validated")
        print(f"JOB   {result['job_key']}")
        print("NOTE  No genomic data was read and no record was signed")
    return 0
