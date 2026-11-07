"""AC26: real status requests during a writer and after a killed logged run."""

from __future__ import annotations

import json
import re
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from tests.lab.test_lab_server import _get, _session
from tests.lab.test_landing import _dom, needs_chrome
from traceback_lab.problems import LabProblem
from traceback_lab.store import LabStore, new_run_id
from traceback_lab.web.server import RunningLabServer


@pytest.mark.parametrize("command", ("run", "simulate split", "cohort add", "cohort build"))
def test_hundred_real_http_status_polls_leave_each_writer_held_and_able_to_finish(
    tmp_path: Path, command: str,
) -> None:
    store = LabStore(tmp_path)
    with RunningLabServer(store) as server:
        cookie = _session(server)
        run_id = new_run_id()
        with store.writer(command, run_id, step="ingest") as lock:
            for number in range(100):
                response, body = _get(server, "/api/status", cookie)
                assert response.status == 200, (number, body)
                active = json.loads(body)["active"]
                assert active["run_id"] == run_id
                assert active["command"] == command
                assert active["step"] == ("align" if number >= 50 else "ingest")
                if number == 49:
                    lock.set_step("align")
            with pytest.raises(LabProblem) as busy:
                with store.writer("run", new_run_id()):
                    pass
            assert busy.value.code == "TBX-LAB-012" and busy.value.exit_code == 3
        response, body = _get(server, "/api/status", cookie)
        assert response.status == 200 and json.loads(body)["active"] is None
        with store.writer("run", new_run_id()):
            pass


@needs_chrome
def test_killed_logged_run_is_interrupted_in_real_api_and_browser(tmp_path: Path) -> None:
    root = tmp_path / "root"
    ready = tmp_path / "writer-ready"
    run_id = new_run_id()
    script = textwrap.dedent("""
        import sys
        import time
        from pathlib import Path
        from traceback_lab.store import LabStore

        root, ready, run_id = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
        store = LabStore(root)
        with store.writer("run", run_id, step="bam-qc"):
            log = store.run_log(run_id)
            log.append("run_start", command="run")
            log.append("sample", source_id="a" * 64, input_id="b" * 64)
            log.append("step_start", step="bam-qc")
            ready.write_text("ready", encoding="ascii")
            time.sleep(60)
    """)
    process = subprocess.Popen(
        [sys.executable, "-c", script, str(root), str(ready), run_id],
        cwd=Path(__file__).resolve().parents[2],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 10
        while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ready.exists() and process.poll() is None, f"logged writer did not start: {process.returncode}"

        store = LabStore(root)
        with RunningLabServer(store) as server:
            cookie = _session(server)
            response, body = _get(server, "/api/status", cookie)
            active = json.loads(body)
            assert response.status == 200
            assert active["active"]["run_id"] == run_id
            assert active["active"]["step"] == "bam-qc"

            process.kill()
            process.wait(timeout=5)
            assert store.active_file.exists()  # SIGKILL did not run WriterLock.stop().
            stale = store.read_active()
            assert stale is not None and not stale.active
            response, body = _get(server, "/api/status", cookie)
            interrupted = json.loads(body)
            assert response.status == 200
            assert interrupted["active"] is None
            assert interrupted["interrupted"] == {
                "run_id": run_id, "step": "bam-qc", "source_id": "a" * 64, "input_id": "b" * 64,
            }
            assert interrupted["run_log_unavailable"] is None

            dom = _dom(server, tmp_path / "browser", route="/",
                       want=lambda page: f"The last run ({run_id}) stopped during bam-qc" in page)
            assert "Results shown are from completed steps" in dom
            assert re.search(r'<p(?=[^>]*id="status")(?=[^>]*role="status")'
                             r'(?=[^>]*aria-live="polite")', dom)
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
