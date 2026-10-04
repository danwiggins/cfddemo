"""Regenerate the committed ``@EXPLICIT`` lock files for the ichorCNA toolchain.

Usage::

    uv run python scripts/lock_ichor_toolchain.py --micromamba /ABS/PATH/micromamba

It asks micromamba for a dry-run solve per platform (network required) and
writes ``traceback_runner/toolchains/ichor-<platform>.lock``: one pinned
package URL per line with its ``#sha256:`` digest.  A new lock has a new
SHA-256, so it installs into a new cache directory and changes the method hash.
Review the diff before committing.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from traceback_runner.toolchain import (  # noqa: E402
    ICHOR_CHANNELS,
    ICHOR_SPECS,
    LOCK_DIRECTORY,
    SUPPORTED_PLATFORMS,
    driver_sha256,
    parse_lock,
    render_lock,
)


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
    parser.add_argument("--micromamba", required=True, type=Path)
    args = parser.parse_args()
    if not args.micromamba.is_absolute():
        parser.error("--micromamba must be an absolute path")
    with tempfile.TemporaryDirectory() as scratch:
        for platform in SUPPORTED_PLATFORMS:
            packages = _solve(args.micromamba, platform, Path(scratch))
            text = render_lock(platform, packages, driver_sha256())
            parse_lock(text.encode("utf-8"), platform)  # refuse what we would not install
            target = REPO / LOCK_DIRECTORY / f"ichor-{platform}.lock"
            target.write_text(text, encoding="utf-8")
            print(f"{target.relative_to(REPO)}: {len(packages)} packages")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
