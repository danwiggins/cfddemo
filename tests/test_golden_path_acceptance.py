"""Run scripts/golden_path_acceptance.sh (DoD steps 1-7) on generated inputs."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "golden_path_acceptance.sh"


@pytest.mark.skipif(shutil.which("bash") is None or shutil.which("perl") is None,
                    reason="the acceptance script needs bash and perl")
def test_golden_path_acceptance_steps_1_to_7(tmp_path: Path) -> None:
    python = shlex.quote(sys.executable)
    environment = {
        **os.environ,
        "TRACEBACK": f"{python} -m traceback_runner",
        "PYTHON": python,
        "TMPDIR": str(tmp_path),
    }
    environment.pop("FASTA", None)
    environment.pop("BAM", None)
    environment.pop("KEEP_ROOT", None)
    completed = subprocess.run(
        ["bash", str(SCRIPT)],
        cwd=SCRIPT.parents[1],
        env=environment,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "ACCEPTANCE PASSED (DoD steps 1-7)" in completed.stdout
    assert "preflight outcome: partial" in completed.stdout
    assert "catalog qualification_state: development_unqualified" in completed.stdout
    assert "explorer rows listed: 1" in completed.stdout
    # Steps 6-7: serve in the background, operator GET of the catalog over HTTP.
    assert "served catalog rows listed: 1" in completed.stdout
    assert "served qualification_state: development_unqualified" in completed.stdout
    assert "serve stopped: exit=0" in completed.stdout
    # The one-use operator link is never echoed to a log.
    assert "bootstrap=" not in completed.stdout + completed.stderr
    # The script removes its temporary root.
    assert not any(tmp_path.glob("traceback-golden.*"))
