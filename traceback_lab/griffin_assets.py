"""Operator registration of exact Griffin code, reference and TF-site inputs.

Registration records local paths and content hashes; it does not redistribute
the upstream assets. Every resolution rehashes before a Griffin attempt.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from .adapters.griffin_run import GRIFFIN_COMMIT, _REF_FILES, _digest, _safe_path

SCHEMA = "traceback.lab.griffin-assets.v1"
MAX_REGISTRATION_BYTES = 128_000
_FREQUENCY_PREFIX = "k100_minus_exclusion_lists.mappable_regions.hg38."
_HEX = re.compile(r"^[a-f0-9]{64}$")


class GriffinAssetError(ValueError):
    """Registration is missing, changed, or unsuitable for Griffin."""


@dataclass(frozen=True)
class GriffinAssets:
    upstream_root: Path
    ref_assets: Path
    site_lists: Mapping[str, Path]
    primary_site: str
    sha256: str
    source_label: str
    terms: str
    license_id: str | None


def _location(root: Path) -> Path:
    return root / "lab" / "assets" / "griffin" / "registration.json"


def registration_available(root: Path) -> bool:
    """Cheap preflight gate; full inspection still rehashes all pinned bytes."""

    location = _location(root)
    try:
        return (not location.parent.is_symlink() and not location.is_symlink()
                and location.is_file() and location.stat().st_size <= MAX_REGISTRATION_BYTES)
    except OSError:
        return False


def _file(path: Path) -> Path:
    if path.is_symlink() or not path.is_file():
        raise GriffinAssetError(f"Griffin input is absent or a symlink: {path}")
    return _safe_path(path)


def _checkout(root: Path) -> Path:
    source = _safe_path(root)
    try:
        head = subprocess.run(
            ["git", "-C", str(source), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "-C", str(source), "status", "--porcelain"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        raise GriffinAssetError("Griffin checkout cannot be verified") from exc
    if head != GRIFFIN_COMMIT or dirty:
        raise GriffinAssetError("Griffin checkout must be clean at the pinned commit")
    return source


def _fingerprints(upstream: Path, assets: Path, sites: Mapping[str, Path]) -> dict[str, str]:
    files = {f"ref/{name}": assets / name for name in _REF_FILES}
    files.update({
        f"ref/genome_GC_frequency/{_FREQUENCY_PREFIX}{length}bp.GC_frequency.txt":
        assets / "genome_GC_frequency" / f"{_FREQUENCY_PREFIX}{length}bp.GC_frequency.txt"
        for length in range(15, 501)
    })
    files.update({f"sites/{name}": path for name, path in sites.items()})
    for name in ("griffin_GC_counts.py", "griffin_GC_bias.py", "griffin_coverage.py", "griffin_merge_sites.py"):
        files[f"scripts/{name}"] = upstream / "scripts" / name
    try:
        return {name: _digest(_file(path)) for name, path in sorted(files.items())}
    except (OSError, ValueError) as exc:
        raise GriffinAssetError("Griffin registration input is absent or unsafe") from exc


def _identity(data: dict) -> str:
    canonical = json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(canonical).hexdigest()


def register_griffin_assets(
    root: Path, *, upstream_root: Path, ref_assets: Path,
    site_lists: Mapping[str, Path], primary_site: str,
    source_label: str, terms: str, license_id: str | None = None,
) -> GriffinAssets:
    """Atomically publish one local registration after hashing every input."""

    if not source_label.strip() or len(source_label) > 160 or terms not in {"known", "restricted", "unknown"}:
        raise GriffinAssetError("source label and terms are required")
    if terms == "known" and not license_id:
        raise GriffinAssetError("known terms require a license ID")
    if license_id is not None and not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", license_id):
        raise GriffinAssetError("invalid license ID")
    if not site_lists or primary_site not in site_lists:
        raise GriffinAssetError("primary TF site list must be among the registered lists")
    if any(not re.fullmatch(r"[A-Za-z0-9_-]+", name) for name in site_lists):
        raise GriffinAssetError("TF site-list names must be shell-safe")
    try:
        upstream = _checkout(upstream_root)
        assets = _safe_path(ref_assets)
        sites = {name: _file(path) for name, path in site_lists.items()}
        fingerprints = _fingerprints(upstream, assets, sites)
    except (OSError, ValueError) as exc:
        if isinstance(exc, GriffinAssetError):
            raise
        raise GriffinAssetError("Griffin registration input is absent or unsafe") from exc
    data = {
        "schema": SCHEMA, "upstream_root": str(upstream),
        "upstream_commit": GRIFFIN_COMMIT, "ref_assets": str(assets),
        "site_lists": {name: str(path) for name, path in sorted(sites.items())},
        "primary_site": primary_site, "source_label": source_label.strip(),
        "terms": terms, "license_id": license_id, "fingerprints": fingerprints,
    }
    data["sha256"] = _identity(data)
    serialized = json.dumps(data, sort_keys=True).encode("utf-8")
    if len(serialized) > MAX_REGISTRATION_BYTES:
        raise GriffinAssetError("Griffin registration exceeds the supported size")
    location = _location(root)
    if location.parent.is_symlink():
        raise GriffinAssetError("Griffin asset directory must not be a symlink")
    location.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".griffin-", dir=location.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(serialized)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, location)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return GriffinAssets(upstream, assets, sites, primary_site, data["sha256"], source_label.strip(), terms, license_id)


def inspect_griffin_assets(root: Path) -> GriffinAssets:
    location = _location(root)
    try:
        if location.parent.is_symlink() or location.is_symlink() or location.stat().st_size > MAX_REGISTRATION_BYTES:
            raise GriffinAssetError("Griffin registration is unsafe or oversized")
        data = json.loads(location.read_text(encoding="utf-8"))
        if set(data) != {
            "schema", "upstream_root", "upstream_commit", "ref_assets", "site_lists",
            "primary_site", "source_label", "terms", "license_id", "fingerprints", "sha256",
        } or data["schema"] != SCHEMA or data["upstream_commit"] != GRIFFIN_COMMIT:
            raise GriffinAssetError("invalid Griffin registration")
        digest = data.pop("sha256")
        if not isinstance(digest, str) or not _HEX.fullmatch(digest) or _identity(data) != digest:
            raise GriffinAssetError("Griffin registration changed")
        upstream = _checkout(Path(data["upstream_root"]))
        assets = _safe_path(Path(data["ref_assets"]))
        sites = {name: _file(Path(path)) for name, path in data["site_lists"].items()}
        if data["primary_site"] not in sites:
            raise GriffinAssetError("primary TF site list is absent")
        if _fingerprints(upstream, assets, sites) != data["fingerprints"]:
            raise GriffinAssetError("Griffin asset bytes changed since registration")
        return GriffinAssets(
            upstream, assets, sites, data["primary_site"], digest,
            data["source_label"], data["terms"], data["license_id"],
        )
    except (OSError, KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, GriffinAssetError):
            raise
        raise GriffinAssetError("Griffin assets are not registered or cannot be verified") from exc
