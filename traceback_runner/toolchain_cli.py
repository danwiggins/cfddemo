"""``traceback toolchain install TOOL [--yes]``: per-user pinned toolchains.

Without ``--yes`` the command prints the exact micromamba command, the target
cache directory and that it needs the network, changes nothing, and exits 0.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from .toolchain import (
    TOOL_PINS,
    ToolProblem,
    install,
    pin_for,
    plan_install,
    resolve_micromamba,
    toolchain_cache_root,
)

# §11.20: `copy-number` is an alias of `ichor`; neither is pinned yet (CN1).
_TOOL_NAMES = tuple(sorted(TOOL_PINS))


def _absolute_path(value: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise argparse.ArgumentTypeError("--micromamba must be an absolute path")
    return path


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="traceback toolchain")
    commands = parser.add_subparsers(dest="toolchain_command", required=True)
    install_parser = commands.add_parser(
        "install", help="install a pinned tool into the per-user cache"
    )
    install_parser.add_argument("tool", choices=_TOOL_NAMES)
    install_parser.add_argument(
        "--yes", action="store_true", help="run the install (needs the network)"
    )
    install_parser.add_argument("--micromamba", type=_absolute_path)
    install_parser.add_argument("--json", action="store_true", dest="as_json")
    return parser


def main(argv: Sequence[str]) -> int:
    from .cli import ExitCode, _emit, _problem, _result

    args = _parser().parse_args(list(argv))
    command = f"toolchain {args.toolchain_command}"
    try:
        pin = pin_for(args.tool)
        cache_root = toolchain_cache_root()
        missing_micromamba: ToolProblem | None = None
        try:
            micromamba = resolve_micromamba(args.micromamba)
        except ToolProblem as problem:
            if args.yes:
                raise
            # The dry run still shows the plan; installing needs micromamba.
            missing_micromamba, micromamba = problem, Path("micromamba")
        plan = plan_install(pin, cache_root=cache_root, micromamba=micromamba)
        data: dict[str, object] = {
            "tool": pin.tool,
            "version": pin.version,
            "platform": pin.platform,
            "lock_sha256": pin.lock_sha256,
            "lock_line": pin.lock_line,
            "needs_network": True,
            "micromamba_found": missing_micromamba is None,
        }
        if missing_micromamba is not None:
            data["next"] = missing_micromamba.fix
        if not args.yes:
            # Dry run: the human output shows the command and target directory
            # (both under the operator's own home); JSON carries no host path.
            payload = _result(
                command,
                "ok",
                f"Dry run: {pin.tool} {pin.version} would be installed into the "
                "per-user toolchain cache; this needs the network. Re-run with "
                "--yes to install",
                data={**data, "dry_run": True},
            )
            if not args.as_json:
                payload["data"]["command"] = " ".join(plan.argv)
                payload["data"]["target"] = str(plan.prefix)
            _emit(payload, as_json=args.as_json)
            return int(ExitCode.OK)
        tool, installed = install(pin, cache_root=cache_root, micromamba=micromamba)
        payload = _result(
            command,
            "ok",
            f"{pin.tool} {pin.version} "
            + ("installed and verified" if installed else "already installed and verified"),
            data={
                **data,
                "dry_run": False,
                "installed_binary_sha256": tool.identity.installed_binary_sha256,
                "package_binary_sha256": tool.identity.package_binary_sha256,
            },
        )
        _emit(payload, as_json=args.as_json)
        return int(ExitCode.OK)
    except ToolProblem as problem:
        _emit(_problem(command, problem), as_json=args.as_json)
        return int(problem.exit_code)


__all__ = ["main"]
