"""Usability wave 1 (A2, A3, A4a-c): input checks, failure reasons, jobs,
catalog list, labels and CSV export.

Every record here is generated test data: unqualified, local, not for clinical use.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import shutil
import sqlite3
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from traceback_runner import cli
from traceback_runner.contracts import JobState
from traceback_runner.fixtures import create_local_golden_path_inputs
from traceback_runner.problems import PROBLEM_TABLE
from traceback_runner.store import JobStore

REPO = Path(__file__).resolve().parents[1]
GUIDE = REPO / "docs" / "OPERATOR-GUIDE.md"
LABEL = "batch two sample A"


def _json(*argv: object) -> tuple[int, dict]:
    stream = io.StringIO()
    with redirect_stdout(stream):
        code = cli.main([*map(str, argv), "--json"])
    text = stream.getvalue()
    return code, json.loads(text.splitlines()[-1])


def _text(*argv: object) -> tuple[int, str]:
    stream = io.StringIO()
    with redirect_stdout(stream):
        code = cli.main([*map(str, argv)])
    return code, stream.getvalue()


def _job_rows(root: Path) -> list[str]:
    database = root / "runner" / "runner.sqlite3"
    if not database.exists():
        return []
    with sqlite3.connect(database) as connection:
        return [row[0] for row in connection.execute("SELECT job_id FROM jobs")]


def _tree_sha256(directory: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(directory.rglob("*")):
        digest.update(path.relative_to(directory).as_posix().encode())
        if path.is_file():
            digest.update(path.read_bytes())
    return digest.hexdigest()


@pytest.fixture(scope="module")
def base(tmp_path_factory: pytest.TempPathFactory):
    """One ROOT, one reference, three distinct BAMs run; the first two imported."""

    work = tmp_path_factory.mktemp("lists")
    inputs = [
        create_local_golden_path_inputs(work / f"inputs-{seed}", seed=seed)
        for seed in (20261002, 11, 12)
    ]
    root = work / "root"
    code, payload = _json(
        "reference", "register", "--fasta", inputs[0].fasta_path, "--id", "ref", "--root", root
    )
    assert code == 0, payload
    records = []
    for index, item in enumerate(inputs):
        argv = ["run", item.bam_path, "--reference", "ref", "--root", root]
        if index < 2:
            argv += ["--import", "--label", f"{LABEL} {index}"]
        code, payload = _json(*argv)
        assert code == 0, payload
        records.append(payload["data"])
    return root, inputs, records


@pytest.fixture
def world(base, tmp_path: Path):
    root, inputs, records = base
    copy = tmp_path / "root"
    shutil.copytree(root, copy, symlinks=True)
    return copy, inputs, records


def _no_label_or_path(payload: dict, root: Path) -> None:
    text = json.dumps(payload)
    assert LABEL not in text
    assert str(root.resolve()) not in text and str(root) not in text
    assert not re.search(r'"/(Users|home|private|tmp|var)/', text)


# --- A2: up-front input checks -----------------------------------------------


def test_missing_bam_is_tbx_run_008_and_creates_no_job(tmp_path: Path) -> None:
    inputs = create_local_golden_path_inputs(tmp_path / "inputs")
    root = tmp_path / "root"
    assert _json("reference", "register", "--fasta", inputs.fasta_path, "--id", "ref",
                 "--root", root)[0] == 0
    code, payload = _json("run", tmp_path / "absent.bam", "--reference", "ref", "--root", root)
    assert code == cli.ExitCode.NOT_FOUND
    assert payload["data"]["code"] == "TBX-RUN-008"
    assert "NOT_FOUND job, bundle, or trust material" not in json.dumps(payload)
    assert "was not found" not in payload["summary"]
    assert _job_rows(root) == []
    assert not (root / "authority").exists()
    assert str(tmp_path) not in json.dumps(payload)


def test_input_check_order_is_bam_then_index_then_bgzf(tmp_path: Path) -> None:
    inputs = create_local_golden_path_inputs(tmp_path / "inputs")
    root = tmp_path / "root"
    _json("reference", "register", "--fasta", inputs.fasta_path, "--id", "ref", "--root", root)
    code, payload = _json("run", inputs.bam_path, "--index", tmp_path / "missing.txt",
                          "--reference", "ref", "--root", root)
    assert (code, payload["data"]["code"]) == (cli.ExitCode.NOT_FOUND, "TBX-RUN-009")
    fake = tmp_path / "reads.bam"
    fake.write_text("not a bam")
    code, payload = _json("run", fake, "--reference", "ref", "--root", root)
    assert (code, payload["data"]["code"]) == (cli.ExitCode.NOT_FOUND, "TBX-RUN-009")
    assert _job_rows(root) == []


def test_missing_index_is_tbx_run_009_and_creates_no_job(tmp_path: Path) -> None:
    inputs = create_local_golden_path_inputs(tmp_path / "inputs")
    root = tmp_path / "root"
    assert _json("reference", "register", "--fasta", inputs.fasta_path, "--id", "ref",
                 "--root", root)[0] == 0
    Path(f"{inputs.bam_path}.bai").unlink()
    code, payload = _json("run", inputs.bam_path, "--reference", "ref", "--root", root)
    assert code == cli.ExitCode.NOT_FOUND
    assert payload["data"]["code"] == "TBX-RUN-009"
    assert "samtools index" in payload["data"]["fix"] and "--index" in payload["data"]["fix"]
    assert _job_rows(root) == []
    code, human = _text("run", inputs.bam_path, "--reference", "ref", "--root", root)
    assert "CODE  TBX-RUN-009" in human and "CAUSE  " in human and "FIX  " in human


def test_a_text_file_named_bam_is_tbx_run_010_and_creates_no_job(tmp_path: Path) -> None:
    inputs = create_local_golden_path_inputs(tmp_path / "inputs")
    root = tmp_path / "root"
    assert _json("reference", "register", "--fasta", inputs.fasta_path, "--id", "ref",
                 "--root", root)[0] == 0
    fake = tmp_path / "reads.bam"
    fake.write_text("@read1\nACGT\n+\nIIII\n")
    Path(f"{fake}.bai").write_bytes(b"BAI\x01")
    code, payload = _json("run", fake, "--reference", "ref", "--root", root)
    assert code == cli.ExitCode.BLOCKED
    assert payload["data"]["code"] == "TBX-RUN-010"
    assert "Aligning MinKNOW output" in payload["data"]["fix"]
    assert _job_rows(root) == []


# --- A3: JOB_ID on refusals; failure reason in status and logs ---------------


def test_tbx_run_005_prints_its_job_and_status_explains_it(tmp_path: Path) -> None:
    empty = create_local_golden_path_inputs(tmp_path / "inputs", eligible=False)
    root = tmp_path / "root"
    assert _json("reference", "register", "--fasta", empty.fasta_path, "--id", "ref",
                 "--root", root)[0] == 0
    code, payload = _json("run", empty.bam_path, "--reference", "ref", "--root", root)
    assert code == cli.ExitCode.BLOCKED and payload["data"]["code"] == "TBX-RUN-005"
    (job_id,) = _job_rows(root)
    assert payload["data"]["job_id"] == job_id
    code, human = _text("run", empty.bam_path, "--reference", "ref", "--root", root)
    assert f"JOB_ID  {job_id}" in human

    code, status = _json("status", job_id, "--root", root)
    assert code == cli.ExitCode.OK
    assert status["summary"].startswith("FAILED: TBX-RUN-005 ")
    failure = status["data"]["failure"]
    assert failure["code"] == "TBX-RUN-005"
    assert failure["cause"] == PROBLEM_TABLE["TBX-RUN-005"].cause
    assert failure["fix"] == PROBLEM_TABLE["TBX-RUN-005"].fix
    code, human = _text("status", job_id, "--root", root)
    assert human.startswith("PASS  FAILED: TBX-RUN-005")
    assert "MAPQ 20" in human

    code, logs = _json("logs", job_id, "--root", root)
    assert code == cli.ExitCode.OK
    assert logs["data"]["failure"]["code"] == "TBX-RUN-005"
    for event in logs["data"]["events"]:
        assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\+00:00", event["occurred_at"])

    code, jobs = _json("jobs", "--root", root)
    assert code == cli.ExitCode.OK
    assert [(row["job_id"], row["failure_code"]) for row in jobs["data"]["jobs"]] == [
        (job_id, "TBX-RUN-005")
    ]
    code, human = _text("jobs", "--root", root)
    assert job_id[:12] in human and "TBX-RUN-005" in human and "terminal_failure" in human


def test_an_uncoded_failure_reason_is_never_printed(tmp_path: Path) -> None:
    empty = create_local_golden_path_inputs(tmp_path / "inputs", eligible=False)
    root = tmp_path / "root"
    _json("reference", "register", "--fasta", empty.fasta_path, "--id", "ref", "--root", root)
    _json("run", empty.bam_path, "--reference", "ref", "--root", root)
    (job_id,) = _job_rows(root)
    secret = "SnapshotViolation: /Users/someone/donor-0042/sample.bam changed"
    with sqlite3.connect(root / "runner" / "runner.sqlite3") as connection:
        connection.execute("UPDATE jobs SET last_error=? WHERE job_id=?", (secret, job_id))
    for command in ("status", "logs"):
        code, payload = _json(command, job_id, "--root", root)
        assert code == cli.ExitCode.OK
        assert payload["data"]["failure"] == {
            "code": None,
            "summary": "uncoded failure; see traceback support-bundle",
            "cause": None,
            "fix": None,
        }
        code, human = _text(command, job_id, "--root", root)
        for output in (json.dumps(payload), human):
            assert "donor-0042" not in output and "/Users/" not in output
    code, jobs = _json("jobs", "--root", root)
    assert jobs["data"]["jobs"][0]["failure_code"] is None
    assert "donor-0042" not in _text("jobs", "--root", root)[1]


def test_a_live_worker_lease_is_tbx_job_002_with_its_job(tmp_path: Path, monkeypatch) -> None:
    inputs = create_local_golden_path_inputs(tmp_path / "inputs")
    root = tmp_path / "root"
    _json("reference", "register", "--fasta", inputs.fasta_path, "--id", "ref", "--root", root)
    original = cli._local_stages

    def pausing(*args, **kwargs):
        stages = list(original(*args, **kwargs))
        first = stages[0]

        def callback(context):
            result = first.callback(context)
            cli._existing_runner(root).request_pause(context.job_id)
            return result

        stages[0] = type(first)(
            name=first.name, version=first.version, callback=callback, parameters=first.parameters
        )
        return tuple(stages)

    monkeypatch.setattr(cli, "_local_stages", pausing)
    code, paused = _json("run", inputs.bam_path, "--reference", "ref", "--root", root)
    assert code == 0 and paused["data"]["state"] == "paused", paused
    monkeypatch.setattr(cli, "_local_stages", original)
    job_id = paused["data"]["job_id"]
    # Another (just-killed or still running) process holds the job's lease.
    with sqlite3.connect(root / "runner" / "runner.sqlite3") as connection:
        connection.execute(
            "UPDATE jobs SET state='running', lease_owner='other', lease_token=99, "
            "lease_expires_at=? WHERE job_id=?",
            (10**12, job_id),
        )
    for argv in (
        ("run", inputs.bam_path, "--reference", "ref", "--root", root),
        ("resume", job_id, "--root", root),
    ):
        code, payload = _json(*argv)
        assert code == cli.ExitCode.BLOCKED, payload
        assert payload["data"]["code"] == "TBX-JOB-002"
        assert payload["data"]["job_id"] == job_id
        assert payload["data"]["retryable"] is True
    # Two concurrent runs: the second waits on ROOT's operator lock, and names
    # the job the first one is running.
    with cli._operator_lock(root):
        code, payload = _json("run", inputs.bam_path, "--reference", "ref", "--root", root)
    assert code == cli.ExitCode.BLOCKED, payload
    assert (payload["data"]["code"], payload["data"]["job_id"]) == ("TBX-JOB-002", job_id)


def test_a_sealing_failure_still_names_the_job_it_created(tmp_path: Path, monkeypatch) -> None:
    from traceback_runner import runner as runner_module

    inputs = create_local_golden_path_inputs(tmp_path / "inputs")
    root = tmp_path / "root"
    _json("reference", "register", "--fasta", inputs.fasta_path, "--id", "ref", "--root", root)

    def failing_capture(*args, **kwargs):
        raise OSError("simulated read failure while sealing")

    monkeypatch.setattr(runner_module, "capture_snapshot", failing_capture)
    code, payload = _json("run", inputs.bam_path, "--reference", "ref", "--root", root)
    assert code != cli.ExitCode.OK
    (job_id,) = _job_rows(root)
    assert payload["data"]["job_id"] == job_id


def test_a_post_publication_label_failure_keeps_the_record_and_job(
    tmp_path: Path, monkeypatch
) -> None:
    from traceback_runner import labels

    inputs = create_local_golden_path_inputs(tmp_path / "inputs")
    root = tmp_path / "root"
    _json("reference", "register", "--fasta", inputs.fasta_path, "--id", "ref", "--root", root)

    def failing_write(*args, **kwargs):
        raise OSError("simulated fsync failure")

    monkeypatch.setattr(labels, "write_label", failing_write)
    code, payload = _json("run", inputs.bam_path, "--reference", "ref", "--label", "note",
                          "--root", root)
    assert code == cli.ExitCode.RETRYABLE_FAILURE, payload
    (job_id,) = _job_rows(root)
    assert payload["data"]["job_id"] == job_id
    assert (root / "records" / payload["data"]["record_id"]).is_dir()
    assert "record was made" in payload["summary"]
    assert "note" not in json.dumps(payload)


def test_every_problem_code_has_a_table_row_and_a_guide_row() -> None:
    literal = re.compile(r"TBX-[A-Z]+(?:-[A-Z]+)?(?:-[0-9]{3})?(?![-\w])")
    codes: set[str] = set()
    for package in ("traceback_runner", "evidence_inspector"):
        for path in (REPO / package).rglob("*.py"):
            codes.update(literal.findall(path.read_text(encoding="utf-8")))
    # No code is built from an f-string today; list any here if one is added.
    built: set[str] = set()
    codes |= built
    assert {"TBX-RUN-005", "TBX-JOB-002", "TBX-RUN-008", "TBX-AUTH-LOCAL-001",
            "TBX-INTERNAL"} <= codes
    assert sorted(codes - set(PROBLEM_TABLE)) == []
    anchors = set(re.findall(r'<a id="([a-z0-9-]+)"></a>', GUIDE.read_text(encoding="utf-8")))
    assert sorted(code for code in codes if code.lower() not in anchors) == []
    for code, text in PROBLEM_TABLE.items():
        assert text.cause and text.fix, code


# --- A4a: jobs, catalog list, run --import, import by ID, serve reload --------


def test_jobs_without_a_runner_database_says_no_jobs_yet(tmp_path: Path) -> None:
    code, payload = _json("jobs", "--root", tmp_path / "empty")
    assert code == cli.ExitCode.OK
    assert payload["data"]["jobs"] == [] and payload["summary"].startswith("No jobs yet")
    assert not (tmp_path / "empty").exists()
    code, human = _text("jobs", "--root", tmp_path / "empty")
    assert "No jobs yet" in human
    code, payload = _json("catalog", "list", "--root", tmp_path / "empty")
    assert code == cli.ExitCode.OK and payload["summary"].startswith("No records yet")


def test_catalog_list_shows_three_records_one_not_imported(world) -> None:
    root, _, records = world
    code, payload = _json("catalog", "list", "--root", root)
    assert code == cli.ExitCode.OK, payload
    rows = {row["record_id"]: row for row in payload["data"]["records"]}
    assert set(rows) == {record["record_id"] for record in records}
    assert [rows[record["record_id"]]["imported"] for record in records] == [True, True, False]
    for record in records:
        row = rows[record["record_id"]]
        assert row["verification"] == "verified"
        assert row["eligible_alignments"] == record["eligible_alignments"]
        assert row["reference_id"] == "ref" and row["policy"] == "built-in"
    _no_label_or_path(payload, root)
    code, human = _text("catalog", "list", "--root", root)
    assert human.count("not imported") == 1
    third = cli._short_record(records[2]["record_id"])
    assert f"traceback catalog import {third}" in human
    assert f"{LABEL} 0" in human and f"{LABEL} 1" in human
    assert "not part of the signed record" in human


def test_jobs_lists_every_job_with_labels_only_in_human_output(world) -> None:
    root, _, records = world
    code, payload = _json("jobs", "--root", root)
    assert code == cli.ExitCode.OK
    assert {row["job_id"] for row in payload["data"]["jobs"]} == {
        record["job_id"] for record in records
    }
    assert all(row["state"] == "complete" for row in payload["data"]["jobs"])
    _no_label_or_path(payload, root)
    code, payload = _json("jobs", "--root", root, "--limit", "1")
    assert len(payload["data"]["jobs"]) == 1
    code, human = _text("jobs", "--root", root)
    assert f"{LABEL} 0" in human and "built-in" in human


def test_jobs_reads_without_changing_the_store_and_orders_by_creation(world) -> None:
    root, _, records = world
    runner = root / "runner"
    database = runner / "runner.sqlite3"
    with sqlite3.connect(database) as connection:
        # The oldest job was updated last; newest-first is by creation time.
        connection.execute(
            "UPDATE jobs SET updated_at=updated_at+100000 WHERE job_id=?",
            (records[0]["job_id"],),
        )
    runner.chmod(0o750)
    watched = [runner, database]
    before = [(path.stat().st_mode, path.stat().st_mtime_ns) for path in watched]
    code, payload = _json("jobs", "--root", root, "--limit", "1")
    assert code == cli.ExitCode.OK
    assert [row["job_id"] for row in payload["data"]["jobs"]] == [records[2]["job_id"]]
    assert [(path.stat().st_mode, path.stat().st_mtime_ns) for path in watched] == before


def test_run_import_and_import_by_record_id(world, monkeypatch) -> None:
    root, inputs, records = world
    first = records[0]
    assert first["imported"] is True and "result_id" not in first
    assert not any(command.startswith("traceback catalog import") for command in first["next_commands"])
    third = records[2]["record_id"]
    # A same-named entry in the current directory never shadows the ROOT record.
    monkeypatch.chdir(root.parent)
    (root.parent / cli._short_record(third)).mkdir()
    code, payload = _json("catalog", "import", cli._short_record(third), "--root", root)
    assert code == cli.ExitCode.OK, payload
    assert payload["data"]["record_id"] == third
    code, listed = _json("catalog", "list", "--root", root)
    assert all(row["imported"] for row in listed["data"]["records"])
    code, missing = _json("catalog", "import", "record-ffffffffffffffff", "--root", root)
    assert code == cli.ExitCode.BLOCKED and missing["data"]["code"] == "TBX-CAT-001"


def test_a_record_imported_while_serving_appears_without_a_restart(world) -> None:
    from tests.web.test_loopback_server import _exchange
    from tests.web.test_serve import _get, _serving

    root, _, records = world
    third = records[2]["record_id"]
    with _serving(root) as (service, _):
        cookie, _ = _exchange(service)
        status, page = _get(service, "/api/v1/explorer/catalog?limit=10", cookie)
        assert status == 200 and len(page["results"]) == 2
        code, imported = _json("catalog", "import", third, "--root", root)
        assert code == 0, imported
        status, page = _get(service, "/api/v1/explorer/catalog?limit=10", cookie)
        assert status == 200
        rows = {item["ref"]["result_id"]: item for item in page["results"]}
        assert rows[imported["data"]["result_id"]]["has_registered_view"] is True
        status, _ = _get(service, f"/api/v1/explorer/results/{imported['data']['result_id']}", cookie)
        assert status == 200


# --- A4b: labels --------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "rule"),
    [
        ("x" * 81, "1-80 characters"),
        ("", "1-80 characters"),
        ("batch/2", "/ or \\"),
        ("tab\there", "control characters"),
        ("a..b", "public-text"),
        ("open javascript:alert", "public-text"),
    ],
)
def test_a_bad_label_is_a_usage_error_naming_the_rule(
    world, text: str, rule: str, capsys
) -> None:
    root, _, records = world
    with pytest.raises(SystemExit) as raised:
        cli.main(["label", records[0]["record_id"], text, "--root", str(root)])
    assert raised.value.code == 2
    assert rule in capsys.readouterr().err


def test_setting_a_label_changes_no_record_byte_and_never_reaches_json(world) -> None:
    root, inputs, records = world
    before = _tree_sha256(root / "records")
    record_id = records[2]["record_id"]
    code, payload = _json("label", cli._short_record(record_id), "fresh note", "--root", root)
    assert code == cli.ExitCode.OK
    assert payload["data"] == {"record_id": record_id, "label_set": True, "label_replaced": False}
    assert "fresh note" not in json.dumps(payload)
    assert _tree_sha256(root / "records") == before
    stored = root / "labels" / f"{record_id}.json"
    assert (stored.stat().st_mode & 0o777) == 0o600
    code, human = _text("label", record_id, "newer note", "--root", root)
    assert "Label changed from fresh note to newer note" in human
    # run on a reused record with a different --label says so, in human output only.
    code, human = _text("run", inputs[0].bam_path, "--reference", "ref", "--label", "relabel",
                        "--root", root)
    assert code == 0 and f"Label changed from {LABEL} 0 to relabel" in human
    code, payload = _json("run", inputs[0].bam_path, "--reference", "ref", "--label", "relabel two",
                          "--root", root)
    assert code == 0 and "relabel" not in json.dumps(payload)
    assert _tree_sha256(root / "records") == before


def test_a_symlinked_or_damaged_label_file_shows_no_label(world, tmp_path: Path) -> None:
    from traceback_runner.labels import read_label

    root, _, records = world
    record_id = records[0]["record_id"]
    path = root / "labels" / f"{record_id}.json"
    target = tmp_path / "elsewhere.json"
    target.write_text(json.dumps({"label": "from a link"}))
    path.unlink()
    path.symlink_to(target)
    assert read_label(root, record_id) is None
    path.unlink()
    path.write_text(json.dumps({"label": "x/y"}))
    assert read_label(root, record_id) is None


def test_the_guide_warns_against_identifiers_in_labels() -> None:
    assert "Do not put\n  donor names or identifiers in labels" in GUIDE.read_text(
        encoding="utf-8"
    ).replace("Do not put donor", "Do not put\n  donor")


# --- A4c: CSV export ------------------------------------------------------------


def test_csv_export_matches_the_signed_counts_and_never_overwrites(world, tmp_path: Path) -> None:
    from traceback_runner.bundles import verify_bundle
    from traceback_runner.signing import load_development_trust

    root, _, records = world
    out = tmp_path / "counts.csv"
    code, payload = _json("catalog", "export", "--csv", out, "--root", root)
    assert code == cli.ExitCode.OK, payload
    assert payload["data"]["records"] == 2
    rows = list(csv.DictReader(out.open(encoding="utf-8")))
    assert list(rows[0]) == list(cli._CSV_COLUMNS)
    trust = load_development_trust((root / "trust/development-result-trust.json").read_bytes())
    for record in records[:2]:
        mine = [row for row in rows if row["record_id"] == record["record_id"]]
        measurement = verify_bundle(root / "records" / record["record_id"], trust).measurement
        assert [int(row["count"]) for row in mine] == [item.count for item in measurement.histogram]
        assert sum(int(row["count"]) for row in mine) == record["eligible_alignments"]
        assert {row["eligible"] for row in mine} == {str(record["eligible_alignments"])}
        assert {(row["policy_id"], row["min_mapq"]) for row in mine} == {("built-in", "20")}
        assert mine[-1]["bin_upper"] == ""
    assert not any(row["record_id"] == records[2]["record_id"] for row in rows)
    text = out.read_text(encoding="utf-8")
    assert LABEL not in text and "/" not in text
    before = out.read_bytes()
    code, refused = _json("catalog", "export", "--csv", out, "--root", root)
    assert code == cli.ExitCode.BLOCKED and refused["data"]["code"] == "TBX-CAT-003"
    assert out.read_bytes() == before


def test_csv_export_refuses_when_an_imported_record_does_not_verify(world, tmp_path: Path) -> None:
    root, _, records = world
    report = root / "records" / records[1]["record_id"] / "report.html"
    report.chmod(0o600)
    report.write_text(report.read_text() + "<!-- edited -->")
    out = tmp_path / "counts.csv"
    code, payload = _json("catalog", "export", "--csv", out, "--root", root)
    assert code == cli.ExitCode.BLOCKED, payload
    assert payload["data"]["code"] == "TBX-CAT-001"
    assert records[1]["record_id"] in payload["data"]["cause"]
    assert not out.exists()


def test_status_of_a_complete_job_has_no_failure_block(world) -> None:
    root, _, records = world
    code, status = _json("status", records[0]["job_id"], "--root", root)
    assert code == 0 and "failure" not in status["data"]
    with JobStore(root / "runner" / "runner.sqlite3") as store:
        assert store.get(records[0]["job_id"]).state == JobState.COMPLETE
