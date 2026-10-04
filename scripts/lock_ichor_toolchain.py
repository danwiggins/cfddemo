"""Regenerate the committed ``@EXPLICIT`` lock files for the ichorCNA toolchain.

Usage::

    uv run python scripts/lock_ichor_toolchain.py --micromamba /ABS/PATH/micromamba
    uv run python scripts/lock_ichor_toolchain.py --refresh-driver

The first form asks micromamba for a dry-run solve per platform (network
required) and writes ``traceback_runner/toolchain_locks/ichor-<platform>.lock``:
one pinned package URL per line with its ``#sha256:`` digest, and the driver's
SHA-256 in a comment.  ``--refresh-driver`` rewrites only that comment after a
driver edit, keeping every package.  Either way the lock's SHA-256 changes, so
update ``ICHOR_PINS`` in ``traceback_runner/toolchain.py`` (the script prints
the values) and review the diff before committing.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from traceback_runner.toolchain import (  # noqa: E402
    ICHOR_CHANNELS,
    ICHOR_DRIVER_NAME,
    ICHOR_PLATFORMS,
    ICHOR_SPECS,
    LOCK_DIRECTORY,
    parse_explicit_lock,
    render_ichor_lock,
)

_DRIVER_LINE = re.compile(rf"^# driver: {re.escape(ICHOR_DRIVER_NAME)} sha256:[0-9a-f]{{64}}$", re.M)


def _driver_sha256() -> str:
    return hashlib.sha256((LOCK_DIRECTORY / ICHOR_DRIVER_NAME).read_bytes()).hexdigest()


def _solve(micromamba: Path, platform: str, scratch: Path) -> list[tuple[str, str]]:
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(scratch),
        "MAMBA_ROOT_PREFIX": str(scratch / "root"),
        "CONDA_SUBDIR": platform,
        # Virtual packages of the CI runner class; a solve on another host
        # cannot detect them.
        "CONDA_OVERRIDE_GLIBC": "2.28",
        "CONDA_OVERRIDE_LINUX": "5.15",
        "CONDA_OVERRIDE_OSX": "13.0",
    }
    for key in ("HTTPS_PROXY", "HTTP_PROXY", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE"):
        if key in os.environ:
            env[key] = os.environ[key]
    argv = [
        str(micromamba), "create", "--yes", "--dry-run", "--json", "--no-rc",
        "--prefix", str(scratch / f"dry-{platform}"), "--platform", platform,
        "--override-channels", "--strict-channel-priority",
    ]
    for channel in ICHOR_CHANNELS:
        argv += ["-c", channel]
    argv += list(ICHOR_SPECS)
    completed = subprocess.run(argv, env=env, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        sys.stderr.write(completed.stderr[-4000:])
        raise SystemExit(f"solve failed for {platform}")
    links = json.loads(completed.stdout)["actions"]["LINK"]
    return [(item["url"], item["sha256"]) for item in links]


def main() -> int:
    parser = argparse.ArgumentParser()
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--micromamba", type=Path)
    source.add_argument("--refresh-driver", action="store_true")
    args = parser.parse_args()
    driver = _driver_sha256()
    with tempfile.TemporaryDirectory() as scratch:
        for platform in ICHOR_PLATFORMS:
            target = LOCK_DIRECTORY / f"ichor-{platform}.lock"
            if args.refresh_driver:
                text, count = _DRIVER_LINE.subn(
                    f"# driver: {ICHOR_DRIVER_NAME} sha256:{driver}",
                    target.read_text(encoding="utf-8"),
                )
                if count != 1:
                    raise SystemExit(f"{target.name}: no single driver line")
            else:
                if not args.micromamba.is_absolute():
                    parser.error("--micromamba must be an absolute path")
                text = render_ichor_lock(
                    platform, _solve(args.micromamba, platform, Path(scratch)), driver
                )
            parse_explicit_lock(text, platform_name=platform)  # refuse what we would not install
            target.write_text(text, encoding="utf-8")
            digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
            print(f"{target.relative_to(REPO)}: lock_sha256={digest}")
    print(f"driver_sha256={driver}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
