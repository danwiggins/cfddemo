"""One algorithm in its own process (and process group): ``python -m traceback_lab.worker``.

The runner starts this module through ``pipeline.run_pipeline``, so a timeout
or an interrupt kills the worker and anything it started (D20).  The worker
loads the algorithm from its registry entry, runs it on the input context and
writes ``output.json`` (metrics and panels) plus ``artifacts/`` into the
attempt scratch directory.  A crash exits non-zero with a traceback on stderr,
which the runner scrubs before persisting (D34).
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from pathlib import Path

from traceback_runner.serialization import canonical_json_bytes

from .registry import AlgorithmOutput, Skip, context_from_json, load_entry


def _descendants(root_pid: int) -> list[tuple[int, int]]:
    """(pid, pgid) of every descendant process, via ``ps`` (macOS and Linux)."""

    try:
        listing = subprocess.run(
            ["/bin/ps", "-A", "-o", "pid=,ppid=,pgid="], capture_output=True, text=True, timeout=5, check=False
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    children: dict[int, list[tuple[int, int]]] = {}
    for line in listing.splitlines():
        parts = line.split()
        if len(parts) == 3 and all(part.isdigit() for part in parts):
            pid, ppid, pgid = map(int, parts)
            children.setdefault(ppid, []).append((pid, pgid))
    found: list[tuple[int, int]] = []
    frontier = [root_pid]
    while frontier:
        for pid, pgid in children.get(frontier.pop(), []):
            found.append((pid, pgid))
            frontier.append(pid)
    return found


def _escaped_groups_handler(signum: int, frame: object) -> None:
    """On SIGTERM, kill descendants that left our process group.

    Some tools start in a new session (the signed ``exec_pinned`` runs modkit
    with ``start_new_session=True``), so the runner's group kill would miss
    them; D20 says no grandchild outlives the step.
    """

    own = os.getpgid(0)
    for pid, pgid in _descendants(os.getpid()):
        try:
            if pgid != own:
                os.killpg(pgid, signal.SIGKILL)
            else:
                os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    os._exit(128 + signum)


def main(argv: list[str]) -> int:
    signal.signal(signal.SIGTERM, _escaped_groups_handler)
    if len(argv) != 1:
        print("usage: python -m traceback_lab.worker REQUEST.json", file=sys.stderr)
        return 2
    request = json.loads(Path(argv[0]).read_bytes())
    algorithm = load_entry(request["entry"])
    context = context_from_json(request["context"])
    out = Path(request["out_dir"])
    artifacts = out / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    output = algorithm.run(context, artifacts)
    if isinstance(output, Skip):
        (out / "output.json").write_bytes(canonical_json_bytes({
            "skip": {"reason_code": output.reason_code, "message": output.message},
        }))
        return 0
    if not isinstance(output, AlgorithmOutput):
        raise TypeError(f"{algorithm.id}.run must return AlgorithmOutput or Skip")
    for relative, content in output.artifacts.items():
        target = artifacts / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    (out / "output.json").write_bytes(
        canonical_json_bytes({"metrics": output.metrics, "panels": output.panels})
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
