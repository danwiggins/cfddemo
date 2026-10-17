"""Synthetic test plug-ins for the Lab runner (never registered in production)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from traceback_lab.registry import (
    AlgorithmOutput,
    InputContext,
    Ready,
    Requirements,
    Skip,
    ToolPin,
)


@dataclass
class _Base:
    id: str
    version: str = "1"
    family: str = "qc"
    requires: Requirements = field(default_factory=Requirements)
    consumes: str = "read_summary"
    cost: str = "fast"
    parameters: dict[str, Any] = field(default_factory=dict)
    tools: tuple[ToolPin, ...] = ()
    upstream: tuple[str, ...] = ()

    def preflight(self, ctx: InputContext) -> Ready | Skip:
        return Ready()


@dataclass
class Counter(_Base):
    """Counts records in the aligned BAM; writes one panel artifact."""

    def run(self, ctx: InputContext, out: Path) -> AlgorithmOutput:
        import pysam

        count = int(str(pysam.view("-c", str(ctx.input_dir / "aligned.bam"))).strip())
        marker = os.environ.get("LAB_TEST_MARKER")
        if marker:
            with open(marker, "a", encoding="ascii") as stream:
                stream.write(self.id + "\n")
        return AlgorithmOutput(
            metrics=[{"metric_id": "records", "value": count, "unit": "reads", "n": count}],
            panels=[{"panel_id": "counts", "kind": "table", "data_ref": "artifacts/counts.json"}],
            artifacts={"counts.json": json.dumps({"records": count}).encode()},
        )


@dataclass
class Fails(_Base):
    def run(self, ctx: InputContext, out: Path) -> AlgorithmOutput:
        print("[E::open] cannot read /Users/someone/Downloads/DonorName_plasma.bam", file=sys.stderr)
        raise RuntimeError("synthetic failure")


@dataclass
class Hangs(_Base):
    """Forks a sleeping grandchild (pid written to LAB_TEST_PIDFILE), then hangs."""

    def run(self, ctx: InputContext, out: Path) -> AlgorithmOutput:
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
        Path(os.environ["LAB_TEST_PIDFILE"]).write_text(str(child.pid))
        time.sleep(600)
        return AlgorithmOutput()


@dataclass
class SessionHangs(_Base):
    """Starts a child in a NEW session (like exec_pinned's modkit), then hangs."""

    def run(self, ctx: InputContext, out: Path) -> AlgorithmOutput:
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"], start_new_session=True)
        Path(os.environ["LAB_TEST_PIDFILE"]).write_text(str(child.pid))
        time.sleep(600)
        return AlgorithmOutput()


@dataclass
class Waits(_Base):
    """Writes LAB_TEST_READY, then sleeps (for interrupt tests)."""

    def run(self, ctx: InputContext, out: Path) -> AlgorithmOutput:
        Path(os.environ["LAB_TEST_READY"]).write_text("ready")
        time.sleep(600)
        return AlgorithmOutput()


@dataclass
class SkipsDepth(_Base):
    def preflight(self, ctx: InputContext) -> Ready | Skip:
        return Skip("depth", "needs 2,000,000 eligible reads")

    def run(self, ctx: InputContext, out: Path) -> AlgorithmOutput:  # pragma: no cover
        raise AssertionError("a skipped algorithm never runs")


@dataclass
class BadPanel(_Base):
    def run(self, ctx: InputContext, out: Path) -> AlgorithmOutput:
        return AlgorithmOutput(panels=[{"panel_id": "p", "kind": "table", "data_ref": "artifacts/missing.json"}])


@dataclass
class AssetProbe(_Base):
    """Records the planned asset and tool identity received by a real worker."""

    def asset_sha256s(self, root: Path) -> list[str]:
        return ["a" * 64]

    def run(self, ctx: InputContext, out: Path) -> AlgorithmOutput:
        return AlgorithmOutput(artifacts={
            "worker_identity.json": json.dumps({
                "assets": ctx.extras.get("asset_sha256s"),
                "tools": ctx.extras.get("tool_versions"),
            }).encode(),
        })


SUMMARY = Counter("read-summary")
COUNTER = Counter("fragment-size", upstream=("read-summary",))
SECOND = Counter("bam-qc", upstream=("read-summary",))
FAILS = Fails("copy-number", cost="slow")
HANGS = Hangs("cell-origin", cost="slow")
WAITS = Waits("cell-origin", cost="slow")
SKIPS = SkipsDepth("instability", cost="slow")
BAD_PANEL = BadPanel("mtdna")
ASSET_PROBE = AssetProbe("asset-probe", tools=(ToolPin("probe", "2.2.0"),))
DEPENDS_ON_FAILURE = Counter("tf-concordance", family="composite", upstream=("copy-number",))
SESSION_HANGS = SessionHangs("cell-origin", cost="slow")
