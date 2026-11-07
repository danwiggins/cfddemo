"""The Ubuntu reproduction container pins the same tools as the hosted CI job.

scripts/ci/ubuntu-container/Dockerfile exists to reproduce `ci (ubuntu-latest)`
failures on a Mac. It is only useful while it installs the same samtools and
minimap2 releases, by the same URLs and sha256 checksums, and the same uv and
Python versions as .github/workflows/ci.yml. Both files are parsed as text;
Docker is not needed.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
CI_WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
DOCKERFILE = ROOT / "scripts" / "ci" / "ubuntu-container" / "Dockerfile"

_DOWNLOAD = re.compile(r"curl\s+\S+\s+-o\s+(?P<file>\S+)\s+(?P<url>https://\S+)")
_CHECKSUM = re.compile(r'echo\s+"(?P<sha256>[0-9a-f]{64})\s+(?P<file>\S+)"\s*\|\s*sha256sum\s+-c\s+-')


def _pinned_downloads(text: str) -> dict[str, str]:
    """Map each downloaded URL to the sha256 its `sha256sum -c` line verifies."""

    checksums = {m.group("file"): m.group("sha256") for m in _CHECKSUM.finditer(text)}
    downloads = {m.group("file"): m.group("url") for m in _DOWNLOAD.finditer(text)}
    assert set(downloads) == set(checksums), (sorted(downloads), sorted(checksums))
    return {url: checksums[file] for file, url in downloads.items()}


def _ci_steps() -> list[dict]:
    workflow = yaml.safe_load(CI_WORKFLOW.read_text(encoding="utf-8"))
    return workflow["jobs"]["ci"]["steps"]


def test_aligner_tool_pins_match_ci_workflow() -> None:
    steps = [s for s in _ci_steps() if s.get("name") == "Install Lab aligner tools (Linux)"]
    assert len(steps) == 1, [s.get("name") for s in _ci_steps()]
    ci_pins = _pinned_downloads(steps[0]["run"])
    docker_pins = _pinned_downloads(DOCKERFILE.read_text(encoding="utf-8"))

    assert len(ci_pins) == 2, ci_pins
    assert any("minimap2" in url for url in ci_pins), ci_pins
    assert any("samtools" in url for url in ci_pins), ci_pins
    assert docker_pins == ci_pins


def test_uv_and_python_pins_match_ci_workflow() -> None:
    setup = [s for s in _ci_steps() if str(s.get("uses", "")).startswith("astral-sh/setup-uv@")]
    assert len(setup) == 1
    uv_version = str(setup[0]["with"]["version"])
    python_version = str(setup[0]["with"]["python-version"])

    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    assert f"https://astral.sh/uv/{uv_version}/install.sh" in dockerfile
    assert re.search(rf"uv python install {re.escape(python_version)}\b", dockerfile), python_version
