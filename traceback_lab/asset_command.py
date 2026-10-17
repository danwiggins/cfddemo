"""Parser and result handlers for operator-supplied Lab assets."""

from __future__ import annotations

import argparse
import shlex
from collections.abc import Callable
from pathlib import Path
from typing import Any

from traceback_runner.cli import _local_envelope, _result, _with_human

from .assets import AssetError, AssetPublishUncertain, add_asset, inspect_asset
from .epi_clock_model import ClockMapPublishUncertain, ClockModelError, inspect_map, register_map
from .methylbert_assets import MethylbertPublishUncertain, inspect_methylbert_bundle, register_methylbert_bundle
from .problems import EXIT_BLOCKED, EXIT_OK
from .store import LabStore, new_run_id
from .viral_assets import add_viral_reference, inspect_viral_reference


def add_asset_parser(commands: Any, common: Callable[[argparse.ArgumentParser], None]) -> None:
    asset = commands.add_parser("asset", help="register or inspect local Lab assets")
    actions = asset.add_subparsers(dest="asset_action", required=True)
    add = actions.add_parser("add", help="register operator-supplied pinned bytes")
    add.add_argument("kind", choices=("pmd", "tss-annotation", "methylbert-bundle", "viral-fasta"))
    add.add_argument("--file", type=Path, required=True, help="local pinned asset file")
    add.add_argument("--targets", type=Path, help="viral-fasta TSV: contig, virus, accession")
    add.add_argument(
        "--source-label", "--provenance", dest="source_label", required=True,
        help="short public source description, never a donor path",
    )
    add.add_argument("--terms", choices=("unknown", "restricted", "known"), required=True)
    add.add_argument("--license-id", help="required when terms are known")
    common(add)
    add.set_defaults(handler=_add)
    inspect = actions.add_parser("inspect", help="rehash and describe a local asset")
    inspect.add_argument("kind", choices=("pmd", "tss-annotation", "methylbert-bundle", "viral-fasta"))
    inspect.add_argument("--sha256", help="registered MethylBERT bundle digest")
    common(inspect)
    inspect.set_defaults(handler=_inspect)

    clock = actions.add_parser("register-clock-map", help="register a SHA-pinned hg38 Weidner assay-site map")
    clock.add_argument("--file", type=Path, required=True)
    clock.add_argument("--sha256", required=True, help="expected exact-byte SHA-256")
    common(clock)
    clock.set_defaults(handler=_register_clock_map)

    clock_inspect = actions.add_parser("inspect-clock-map", help="verify the registered Weidner assay-site map")
    common(clock_inspect)
    clock_inspect.set_defaults(handler=_inspect_clock_map)


def _clock_payload(store: LabStore, model: Any) -> dict[str, Any]:
    return {"root": str(store.root), "model": "Weidner-3", "genome_build": "hg38",
            "sha256": model.sha256, "source_label": model.source_label,
            "sites": [{"cpg_id": name, "chrom": chrom, "pos0": pos0} for name, chrom, pos0 in model.sites]}


def _register_clock_map(args: argparse.Namespace, store: LabStore) -> tuple[int, dict[str, Any]]:
    store.check_version()
    try:
        with store.writer("asset register-clock-map", new_run_id(), step="epi-clock-map"):
            model = register_map(store, args.file, args.sha256)
    except ClockMapPublishUncertain as exc:
        payload = _result_payload("lab asset register-clock-map", "blocked", "Clock map publication uncertain", store,
                                  {"cause": str(exc)})
        return EXIT_BLOCKED, _with_human(payload, [f"BLOCKED {exc}"])
    except ClockModelError as exc:
        payload = _result_payload("lab asset register-clock-map", "blocked", "Clock map not registered", store,
                                  {"cause": str(exc)})
        return EXIT_BLOCKED, _with_human(payload, [f"BLOCKED {exc}"])
    payload = _result_payload("lab asset register-clock-map", "ok", "Clock map registered", store, _clock_payload(store, model))
    return EXIT_OK, _with_human(payload, ["MODEL   Weidner-3", f"SHA256  {model.sha256}"])


def _inspect_clock_map(args: argparse.Namespace, store: LabStore) -> tuple[int, dict[str, Any]]:
    store.check_version()
    try:
        model = inspect_map(store)
    except ClockModelError as exc:
        payload = _result_payload("lab asset inspect-clock-map", "blocked", "Clock map unavailable", store,
                                  {"cause": str(exc)})
        return EXIT_BLOCKED, _with_human(payload, [f"BLOCKED {exc}"])
    payload = _result_payload("lab asset inspect-clock-map", "ok", "Clock map verified", store, _clock_payload(store, model))
    return EXIT_OK, _with_human(payload, ["MODEL   Weidner-3", f"SHA256  {model.sha256}"])


def _result_payload(
    command: str, status: str, summary: str, store: LabStore, details: dict[str, Any]
) -> dict[str, Any]:
    return _local_envelope(_result(command, status, summary, data={"root": str(store.root), **details}))


def _add(args: argparse.Namespace, store: LabStore) -> tuple[int, dict[str, Any]]:
    store.check_version()
    try:
        with store.writer("asset add", new_run_id(), step=args.kind):
            if args.kind == "methylbert-bundle":
                if getattr(args, "targets", None) is not None:
                    raise AssetError("only viral-fasta accepts --targets")
                record = register_methylbert_bundle(
                    store, args.file, source_label=args.source_label,
                    terms=args.terms, license_id=args.license_id,
                )
            elif args.kind == "viral-fasta":
                if getattr(args, "targets", None) is None:
                    raise AssetError("viral-fasta needs --targets TSV")
                record = add_viral_reference(
                    store, args.file, args.targets,
                    source_label=args.source_label, terms=args.terms, license_id=args.license_id,
                )
            else:
                if getattr(args, "targets", None) is not None:
                    raise AssetError("only viral-fasta accepts --targets")
                record = add_asset(
                    store, args.kind, args.file,
                    source_label=args.source_label, terms=args.terms, license_id=args.license_id,
                )
    except AssetPublishUncertain as exc:
        command = ["traceback", "lab", "asset", "inspect", args.kind, "--root", str(store.root)]
        if isinstance(exc, MethylbertPublishUncertain):
            command.extend(["--sha256", exc.sha256])
        payload = _result_payload(
            "lab asset add", "blocked", "Asset registration incomplete; inspect before retry", store,
            {"kind": args.kind, "cause": str(exc), "fix": shlex.join(command)},
        )
        return EXIT_BLOCKED, _with_human(payload, [f"ASSET   {args.kind}", f"INCOMPLETE {exc}"])
    except AssetError as exc:
        payload = _result_payload(
            "lab asset add", "blocked", "Asset not registered", store,
            {"kind": args.kind, "cause": str(exc)},
        )
        return EXIT_BLOCKED, _with_human(payload, [f"ASSET   {args.kind}", f"BLOCKED {exc}"])
    payload = _result_payload(
        "lab asset add", "ok", "Pinned Lab asset registered", store,
        {"asset": record.public()},
    )
    return EXIT_OK, _with_human(
        payload,
        [
            f"ASSET   {record.asset_id}",
            f"SHA256  {record.sha256}",
            f"TERMS   {record.descriptor.terms}",
            f"FILE    {record.file}",
        ],
    )


def _inspect(args: argparse.Namespace, store: LabStore) -> tuple[int, dict[str, Any]]:
    store.check_version()
    try:
        if args.kind == "methylbert-bundle":
            if not args.sha256:
                raise AssetError("MethylBERT bundle inspection needs --sha256")
            record = inspect_methylbert_bundle(store, args.sha256)
        else:
            if args.sha256:
                raise AssetError("--sha256 is only for methylbert-bundle")
            record = inspect_viral_reference(store) if args.kind == "viral-fasta" else inspect_asset(store, args.kind)
    except AssetError as exc:
        payload = _result_payload(
            "lab asset inspect", "blocked", "Asset unavailable", store,
            {"kind": args.kind, "cause": str(exc)},
        )
        return EXIT_BLOCKED, _with_human(payload, [f"ASSET   {args.kind}", f"BLOCKED {exc}"])
    payload = _result_payload(
        "lab asset inspect", "ok", "Pinned Lab asset verified", store,
        {"asset": record.public()},
    )
    return EXIT_OK, _with_human(
        payload,
        [
            f"ASSET   {record.asset_id}",
            f"SHA256  {record.sha256}",
            f"TERMS   {record.descriptor.terms}" + (" (legacy file)" if getattr(record, "legacy", False) else ""),
            f"FILE    {record.file}",
        ],
    )
