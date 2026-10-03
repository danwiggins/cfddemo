"""Generate the frozen pre-``development-local`` contract fixtures.

Run once, before the measurement v2 / bundle v3 / trust-namespace change, and
commit the output.  The fixtures are frozen: rerunning this script creates a
new random development key and therefore different bytes, so never rerun it
to "fix" a failing frozen-fixture test.

Outputs, under ``tests/fixtures/``:

- ``bundles/v2-synthetic/``: a real ``traceback demo`` result bundle
  (``traceback.result-bundle.v2``, ``development-synthetic`` signature).
- ``bundles/v2-synthetic.trust.json``: the public development trust document
  that verifies it.
- ``bundles/v2-synthetic.sha256``: SHA-256 of every file above.
- ``result_trust_registry/v1/``: the files of a ``v1`` result-trust registry
  directory holding the same public key, plus ``identity.json`` with the
  retained registry ID, epoch, and head needed to reopen it.

Usage: ``uv run python scripts/generate_frozen_contract_fixtures.py``
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
FIXTURES = REPO / "tests" / "fixtures"
BUNDLE_FIXTURE = FIXTURES / "bundles" / "v2-synthetic"
TRUST_FIXTURE = FIXTURES / "bundles" / "v2-synthetic.trust.json"
DIGEST_FIXTURE = FIXTURES / "bundles" / "v2-synthetic.sha256"
REGISTRY_FIXTURE = FIXTURES / "result_trust_registry" / "v1"


def _demo(root: Path) -> tuple[Path, Path]:
    result = subprocess.run(
        [sys.executable, "-m", "traceback_runner", "demo", "--root", str(root), "--json"],
        check=True,
        capture_output=True,
        text=True,
    )
    data = json.loads(result.stdout)["data"]
    return root / data["bundle"], root / data["trust_store"]


def _write_registry(trust_path: Path, destination: Path) -> None:
    from evidence_inspector.result_trust_registry import ResultTrustRegistry
    from traceback_runner.signing import DevelopmentTrustDocument

    document = DevelopmentTrustDocument.model_validate_json(trust_path.read_bytes())
    with tempfile.TemporaryDirectory() as scratch:
        root = Path(scratch) / "registry"
        with ResultTrustRegistry(root) as registry:
            for key in document.keys:
                registry.add_key(key)
            snapshot = registry.current_trust()
        destination.mkdir(parents=True)
        for name in ("registry-metadata.json", "registry-journal.jsonl"):
            shutil.copyfile(root / name, destination / name)
        identity = {
            "registry_id": snapshot.registry_id,
            "registry_epoch_sha256": snapshot.registry_epoch_sha256,
            "state_head_sha256": snapshot.state_head_sha256,
        }
        (destination / "identity.json").write_text(
            json.dumps(identity, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )


def main() -> None:
    for path in (BUNDLE_FIXTURE, TRUST_FIXTURE, DIGEST_FIXTURE, REGISTRY_FIXTURE):
        if path.exists():
            raise SystemExit(f"refusing to overwrite frozen fixture: {path}")
    with tempfile.TemporaryDirectory() as scratch:
        bundle, trust = _demo(Path(scratch) / "demo")
        shutil.copytree(bundle, BUNDLE_FIXTURE)
        for path in BUNDLE_FIXTURE.rglob("*"):
            path.chmod(0o755 if path.is_dir() else 0o644)
        shutil.copyfile(trust, TRUST_FIXTURE)
        _write_registry(TRUST_FIXTURE, REGISTRY_FIXTURE)
    lines = []
    for path in sorted(
        [*BUNDLE_FIXTURE.rglob("*"), TRUST_FIXTURE], key=lambda item: item.as_posix()
    ):
        if path.is_file():
            relative = path.relative_to(DIGEST_FIXTURE.parent).as_posix()
            lines.append(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {relative}\n")
    DIGEST_FIXTURE.write_text("".join(lines), encoding="ascii")


if __name__ == "__main__":
    main()
