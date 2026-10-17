"""Parser and result handlers for operator-supplied Lab assets."""

from __future__ import annotations

import argparse
import shlex
from collections.abc import Callable
from pathlib import Path
from typing import Any

from traceback_runner.cli import _local_envelope, _result, _with_human

from .assets import AssetError, AssetPublishUncertain, add_asset, inspect_asset
from .problems import EXIT_BLOCKED, EXIT_OK
from .store import LabStore, new_run_id


def add_asset_parser(commands: Any, common: Callable[[argparse.ArgumentParser], None]) -> None:
    asset = commands.add_parser("asset", help="register or inspect local Lab assets")
    actions = asset.add_subparsers(dest="asset_action", required=True)
    add = actions.add_parser("add", help="register operator-supplied pinned bytes")
    add.add_argument("kind", choices=("pmd", "tss-annotation"))
    add.add_argument("--file", type=Path, required=True, help="local pinned asset file")
    add.add_argument(
        "--source-label", "--provenance", dest="source_label", required=True,
        help="short public source description, never a donor path",
    )
    add.add_argument("--terms", choices=("unknown", "restricted", "known"), required=True)
    add.add_argument("--license-id", help="required when terms are known")
    common(add)
    add.set_defaults(handler=_add)
    inspect = actions.add_parser("inspect", help="rehash and describe a local asset")
    inspect.add_argument("kind", choices=("pmd", "tss-annotation"))
    common(inspect)
    inspect.set_defaults(handler=_inspect)
    ends = actions.add_parser("end-catalogues-register", help="pin hg38 open-chromatin centers and preferred-end sites")
    ends.add_argument("--centers", type=Path, required=True, help="sorted hg38 BED3 open-chromatin regions")
    ends.add_argument("--preferred", type=Path, required=True, help="sorted hg38 BED4 one-base U/D end sites")
    ends.add_argument("--source-label", required=True, help="short public source description, not a file path")
    ends.add_argument("--centers-source", required=True, help="open-chromatin accession and release")
    ends.add_argument("--preferred-source", required=True, help="preferred-end accession and release")
    ends.add_argument("--terms", choices=("unknown", "restricted", "known"), required=True)
    ends.add_argument("--license-id", help="required when terms are known")
    common(ends)
    ends.set_defaults(handler=_end_catalogues_register)
    check_ends = actions.add_parser("end-catalogues-inspect", help="rehash registered end catalogues")
    common(check_ends)
    check_ends.set_defaults(handler=_end_catalogues_inspect)


def _result_payload(
    command: str, status: str, summary: str, store: LabStore, details: dict[str, Any]
) -> dict[str, Any]:
    return _local_envelope(_result(command, status, summary, data={"root": str(store.root), **details}))


def _add(args: argparse.Namespace, store: LabStore) -> tuple[int, dict[str, Any]]:
    store.check_version()
    try:
        with store.writer("asset add", new_run_id(), step=args.kind):
            record = add_asset(
                store, args.kind, args.file,
                source_label=args.source_label, terms=args.terms, license_id=args.license_id,
            )
    except AssetPublishUncertain as exc:
        payload = _result_payload(
            "lab asset add", "blocked", "Asset registration incomplete; inspect before retry", store,
            {"kind": args.kind, "cause": str(exc), "fix": shlex.join(["traceback", "lab", "asset", "inspect", args.kind, "--root", str(store.root)])},
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
        record = inspect_asset(store, args.kind)
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
            f"TERMS   {record.descriptor.terms}" + (" (legacy file)" if record.legacy else ""),
            f"FILE    {record.file}",
        ],
    )


def _end_catalogues_register(args: argparse.Namespace, store: LabStore) -> tuple[int, dict[str, Any]]:
    from .end_catalog_assets import EndCatalogueError, register_end_catalogues

    store.check_version()
    try:
        with store.writer("asset end-catalogues-register", new_run_id(), step="aligned-ends"):
            record = register_end_catalogues(
                store.root, centers=args.centers, preferred=args.preferred,
                source_label=args.source_label, centers_source=args.centers_source,
                preferred_source=args.preferred_source, terms=args.terms, license_id=args.license_id,
            )
    except (EndCatalogueError, OSError):
        payload = _result_payload("lab asset end-catalogues-register", "blocked", "End catalogues not registered", store,
                                  {"cause": "End catalogues are absent, unsafe, invalid or could not be published"})
        return EXIT_BLOCKED, _with_human(payload, ["BLOCKED End catalogues are absent, unsafe, invalid or could not be published"])
    payload = _result_payload("lab asset end-catalogues-register", "ok", "End catalogues pinned", store,
                              {"identity": record.identity, "centers": record.center_count,
                               "preferred_sites": record.preferred_count, "terms": record.terms})
    return EXIT_OK, _with_human(payload, [f"IDENTITY {record.identity}", f"CENTERS {record.center_count}",
                                         f"SITES {record.preferred_count}"])


def _end_catalogues_inspect(args: argparse.Namespace, store: LabStore) -> tuple[int, dict[str, Any]]:
    from .end_catalog_assets import EndCatalogueError, inspect_end_catalogues

    store.check_version()
    try:
        record = inspect_end_catalogues(store.root)
    except (EndCatalogueError, OSError):
        payload = _result_payload("lab asset end-catalogues-inspect", "blocked", "End catalogues unavailable", store,
                                  {"cause": "End catalogues are absent, unsafe, invalid or changed"})
        return EXIT_BLOCKED, _with_human(payload, ["BLOCKED End catalogues are absent, unsafe, invalid or changed"])
    payload = _result_payload("lab asset end-catalogues-inspect", "ok", "End catalogues verified", store,
                              {"identity": record.identity, "centers": record.center_count,
                               "preferred_sites": record.preferred_count, "terms": record.terms})
    return EXIT_OK, _with_human(payload, [f"IDENTITY {record.identity}", f"CENTERS {record.center_count}",
                                         f"SITES {record.preferred_count}"])
