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
import sys
from pathlib import Path

from traceback_runner.serialization import canonical_json_bytes

from .registry import AlgorithmOutput, context_from_json, load_entry


def main(argv: list[str]) -> int:
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
    if not isinstance(output, AlgorithmOutput):
        raise TypeError(f"{algorithm.id}.run must return AlgorithmOutput")
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
