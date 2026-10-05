"""``traceback toolchain install TOOL [--yes]``: per-user pinned toolchains.

Without ``--yes`` the command prints the exact micromamba command, the target
cache directory and that it needs the network, changes nothing, and exits 0.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from .toolchain import (
    TOOL_ALIASES,
    TOOL_PINS,
    IchorPin,
    ToolchainProblem,
    ToolProblem,
    install,
    install_ichor,
    pin_for,
    plan_install,
    resolve_micromamba,
    toolchain_cache_root,
)

# §11.20: `copy-number` is an alias of `ichor`.
_TOOL_NAMES = tuple(sorted({*TOOL_PINS, *TOOL_ALIASES}))


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
    def progress(line: str) -> None:
        if not as_json:
            print(line, flush=True)

    args = _parser().parse_args(list(argv))
    as_json = args.as_json
    if TOOL_ALIASES.get(args.tool, args.tool) == "ichor":
        return _main_ichor(args, progress)
    return _main_tool(args)


def _main_tool(args: argparse.Namespace) -> int:
    from .cli import ExitCode, _emit, _problem, _result

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


def _main_ichor(args: argparse.Namespace, progress) -> int:
    """The copy-number toolchain: same contract as modkit, TBX-TOOL-002."""

    from .cli import ExitCode, _emit, _problem, _result

    command = f"toolchain {args.toolchain_command}"
    try:
        pin = pin_for(args.tool)
        assert isinstance(pin, IchorPin)
        cache_root = toolchain_cache_root()
        missing_micromamba: ToolProblem | None = None
        try:
            micromamba = resolve_micromamba(args.micromamba)
        except ToolProblem as problem:
            if args.yes:
                raise
            missing_micromamba, micromamba = problem, Path("micromamba")
        plan = plan_install(pin, cache_root=cache_root, micromamba=micromamba)  # type: ignore[arg-type]
        data: dict[str, object] = {
            "tool": pin.tool,
            "version": pin.version,
            "platform": pin.platform,
            "lock_sha256": pin.lock_sha256,
            "lock_line": pin.lock_line,
            "packages": sum(
                1 for line in pin.lock_path.read_text(encoding="utf-8").splitlines()
                if line.startswith("https://")
            ),
            "needs_network": True,
            "micromamba_found": missing_micromamba is None,
        }
        if missing_micromamba is not None:
            data["next"] = missing_micromamba.fix
        if not args.yes:
            payload = _result(
                command,
                "ok",
                f"Dry run: the ichorCNA {pin.version} toolchain (R 4.4, HMMcopy, "
                "readCounter) would be installed into the per-user toolchain cache; "
                "this needs the network and about 3 GB. Re-run with --yes to install",
                data={**data, "dry_run": True},
            )
            if not args.as_json:
                payload["data"]["command"] = " ".join(plan.argv)
                payload["data"]["target"] = str(plan.prefix)
            _emit(payload, as_json=args.as_json)
            return int(ExitCode.OK)
        toolchain, installed = install_ichor(
            pin, cache_root=cache_root, micromamba=micromamba, progress=progress
        )
        payload = _result(
            command,
            "ok",
            f"ichorCNA {pin.version} toolchain "
            + ("installed and verified" if installed else "already installed and verified"),
            data={
                **data,
                "dry_run": False,
                "identity": toolchain.identity.model_dump(mode="json"),
                "installed": toolchain.installed.model_dump(mode="json"),
            },
        )
        _emit(payload, as_json=args.as_json)
        return int(ExitCode.OK)
    except ToolProblem as problem:
        if not isinstance(problem, ToolchainProblem):
            # micromamba missing (or any shared check): one code per toolchain.
            problem = ToolchainProblem(
                problem.reason,
                problem.summary,
                tool=problem.tool,
                cause=problem.cause,
                fix=problem.fix,
            )
        _emit(_problem(command, problem), as_json=args.as_json)
        return int(problem.exit_code)


__all__ = ["main"]
