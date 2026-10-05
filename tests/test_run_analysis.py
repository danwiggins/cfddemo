"""`traceback run --analysis` (signal methods SH4): one job and one record per analysis.

Cell origin and copy number have no real stages yet (CO3/CN3).  The tests
register fake analyses through the stage registry: one that signs a v4 probe
record, one that refuses terminally and one whose tool is missing (retryable).
Every record here is generated test data: unqualified, local, not for
clinical use.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

import traceback_runner.analyses as analyses_module
from evidence_inspector.method_registry import MethodFamily, method_definition_sha256
from tests.test_bundle_v4 import probe  # noqa: F401  (fixture: registers the v4 probe schema)
from traceback_runner import cli
from traceback_runner.analyses import (
    ANALYSES,
    CELL_ORIGIN,
    COPY_NUMBER,
    FRAGMENT,
    RESERVED_TOKEN_SUFFIXES,
    AnalysisStages,
    is_reserved_policy_id,
    parse_analysis_list,
    parse_sample_token,
    read_job_config,
    register_analysis_stages,
    sample_token,
)
from traceback_runner.bundles import build_result_bundle, verify_bundle
from traceback_runner.contracts import (
    ArtifactCommitment,
    BundleMethodIdentity,
    ExportRunProvenance,
    InputKind,
    JobState,
    StageName,
)
from traceback_runner.fixtures import create_local_golden_path_inputs
from traceback_runner.local_authority import local_method_definition
from traceback_runner.runner import Runner, StageResult, StageSpec
from traceback_runner.serialization import canonical_json_bytes
from traceback_runner.signing import load_development_trust
from traceback_runner.store import JobStore
from traceback_runner.toolchain import TOOL_MISSING, ToolProblem

# canonical_json_bytes(JobRequest) of the fragment run on the generated
# golden-path inputs, captured from main (c2d4ea1) before SH4.
MAIN_FRAGMENT_REQUEST_SHA256 = (
    "72f61884fb1b3e9076e44a92c64765183c2b6eda5642da234eb274264a00676b"
)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _json(capsys: pytest.CaptureFixture[str], *argv: object) -> tuple[int, dict]:
    code = cli.main([*map(str, argv), "--json"])
    return code, json.loads(capsys.readouterr().out)


@pytest.fixture
def inputs(tmp_path: Path):
    return create_local_golden_path_inputs(tmp_path / "inputs")


@pytest.fixture
def root(tmp_path: Path, inputs, capsys) -> Path:
    root = tmp_path / "root"
    code, payload = _json(
        capsys, "reference", "register", "--fasta", inputs.fasta_path, "--id", "ref",
        "--root", root,
    )
    assert code == cli.ExitCode.OK, payload
    return root


@pytest.fixture(autouse=True)
def registry(monkeypatch: pytest.MonkeyPatch) -> dict[str, AnalysisStages]:
    """A stage registry private to each test."""

    private: dict[str, AnalysisStages] = {}
    monkeypatch.setattr(analyses_module, "_REGISTRY", private)
    return private


def _run(capsys, root: Path, bam: Path, *extra: object) -> tuple[int, dict]:
    return _json(capsys, "run", bam, "--reference", "ref", "--root", root, *extra)


def _rows(payload: dict) -> dict[str, dict]:
    return {row["analysis"]: row for row in payload["data"]["analyses"]}


def _records(root: Path) -> list[str]:
    records = root / "records"
    return sorted(p.name for p in records.iterdir() if not p.name.startswith(".")) if (
        records.exists()
    ) else []


def _request(root: Path, job_id: str):
    with JobStore(root / "runner" / "runner.sqlite3") as store:
        return store.request(job_id)


def _state(root: Path, job_id: str) -> JobState:
    return Runner(root / "runner", local_unqualified_enabled=True).status(job_id).state


def _definition(analysis: str, family: MethodFamily, tag: str = "v1"):
    def build(loaded: Any, config: Mapping[str, str]):
        parameters = {"analysis": analysis, "tag": tag, "config": dict(sorted(config.items()))}
        return local_method_definition(loaded.registered).model_copy(
            update={
                "method_id": f"mth_{analysis.replace('-', '_')}_probe",
                "family": family,
                "parameter_schema_sha256": hashlib.sha256(
                    canonical_json_bytes(parameters)
                ).hexdigest(),
            }
        )

    return build


class FakeAnalysis:
    """A test analysis: validate, measure (a v4 probe measurement), sign.

    ``mode`` is ``ok``, ``refuse`` (terminal refusal in measure), ``tool``
    (the pinned tool is missing in measure, until ``installed``) or
    ``tool-refusal`` (a stage that wrongly refuses terminally over a tool).
    """

    def __init__(self, analysis: str, mode: str = "ok", *, config_keys=frozenset(), tag="v1"):
        self.analysis = analysis
        self.mode = mode
        self.installed = False
        self.config_seen: list[dict[str, str]] = []
        family = MethodFamily.CELL_ORIGIN if analysis == CELL_ORIGIN else MethodFamily.COPY_NUMBER
        self.definition = _definition(analysis, family, tag)
        self.spec = AnalysisStages(
            analysis=analysis,
            method_slug=f"{analysis}-probe",
            definition=self.definition,
            stages=self.stages,
            config_keys=frozenset(config_keys),
        )

    def register(self) -> FakeAnalysis:
        register_analysis_stages(self.spec)
        return self

    def stages(self, context: Any) -> tuple[StageSpec, ...]:
        self.config_seen.append(dict(context.config))
        definition = self.definition(context.loaded, context.config)
        identity = BundleMethodIdentity(
            method_id=definition.method_id,
            version=definition.version,
            method_definition_sha256=method_definition_sha256(definition),
        )
        reference_id = context.loaded.registered.reference_id

        def validate(stage: Any) -> StageResult:
            output = stage.attempt_dir / "check.json"
            output.write_bytes(b"{}")
            return StageResult(outputs={"check": output.name})

        def measure(stage: Any) -> StageResult:
            if self.mode == "refuse":
                raise cli.LocalStageRefusal(
                    "TBX-RUN-005", "probe refused", cause="probe cause", fix="probe fix"
                )
            if self.mode == "tool" and not self.installed:
                raise ToolProblem(
                    TOOL_MISSING, "probe-tool is not installed", tool="probe-tool",
                    cause="not installed", fix="install it",
                )
            if self.mode == "tool-refusal" and not self.installed:
                raise cli.LocalStageRefusal(
                    "TBX-TOOL-001", "probe-tool is not installed", cause="c", fix="f"
                )
            output = stage.attempt_dir / "measurement.json"
            output.write_bytes(
                canonical_json_bytes(
                    {
                        "schema_version": "traceback.sh3-probe-measurement.v1",
                        "approval_state": "unapproved_local",
                        "reference_id": reference_id,
                        "reads_counted": 7,
                        "values": [3, 4] if self.analysis == CELL_ORIGIN else [5, 6],
                    }
                )
            )
            return StageResult(outputs={"measurement": output.name})

        def sign(stage: Any) -> StageResult:
            measurement = json.loads((stage.prior_stage_dirs[-1] / "measurement.json").read_bytes())
            bundle = build_result_bundle(
                stage.attempt_dir / "bundle",
                measurement=measurement,
                provenance=ExportRunProvenance(
                    run_token=f"local-run-{stage.job_id[:16]}",
                    input_kind=InputKind.MODBAM,
                    protocol_run_token="no-approved-protocol",
                    workflow_release_id="local-unqualified-v0",
                    artifacts=(
                        ArtifactCommitment(
                            role="analysis_bam",
                            artifact_token="local-analysis-bam",
                            size_bytes=1,
                            provider_hmac_sha256="a" * 64,
                        ),
                    ),
                ),
                method=identity,
                signing_key=context.signing_key,
                reference_match="name_and_length_only",
            )
            files = sorted(path for path in bundle.rglob("*") if path.is_file())
            return StageResult(
                outputs={
                    f"bundle_{index:02d}": path.relative_to(stage.attempt_dir).as_posix()
                    for index, path in enumerate(files)
                }
            )

        return (
            StageSpec(name=StageName.VALIDATE, version="1", callback=validate),
            StageSpec(name=StageName.MEASURE, version="1", callback=measure),
            StageSpec(name=StageName.SIGN, version="1", callback=sign),
        )


# --------------------------------------------------------------------------
# Golden: the default fragment JobRequest is main's, byte for byte
# --------------------------------------------------------------------------


@pytest.mark.parametrize("extra", [(), ("--analysis", "fragment")])
def test_default_fragment_request_bytes_equal_mains(root, inputs, capsys, extra) -> None:
    code, payload = _run(capsys, root, inputs.bam_path, *extra)
    assert code == cli.ExitCode.OK, payload
    assert "analyses" not in payload["data"]  # today's single-record output
    stored = _request(root, payload["data"]["job_id"])
    assert stored.sample_token == "local-ref"
    assert (
        hashlib.sha256(canonical_json_bytes(stored)).hexdigest()
        == MAIN_FRAGMENT_REQUEST_SHA256
    )


# --------------------------------------------------------------------------
# Grammar
# --------------------------------------------------------------------------


def test_analysis_list_is_closed_ordered_and_unique() -> None:
    assert ANALYSES == (FRAGMENT, CELL_ORIGIN, COPY_NUMBER)
    assert parse_analysis_list("copy-number,fragment") == (FRAGMENT, COPY_NUMBER)
    assert parse_analysis_list(" cell-origin ") == (CELL_ORIGIN,)
    for bad in ("", "fragment,", "fragment,fragment", "methylation", "Fragment"):
        with pytest.raises(ValueError):
            parse_analysis_list(bad)


def test_cell_origin_and_copy_number_are_reserved_token_suffixes() -> None:
    # Spec §11 item 24: never usable as a research (D2) policy ID.
    assert RESERVED_TOKEN_SUFFIXES == {CELL_ORIGIN, COPY_NUMBER}
    for name in (CELL_ORIGIN, COPY_NUMBER, FRAGMENT):
        assert is_reserved_policy_id(name)
    assert not is_reserved_policy_id("research-span-v1")
    assert sample_token("ref", FRAGMENT) == "local-ref"
    assert sample_token("ref", CELL_ORIGIN) == "local-ref:cell-origin"
    assert parse_sample_token("local-ref") == ("ref", FRAGMENT)
    assert parse_sample_token("local-ref:cell-origin") == ("ref", CELL_ORIGIN)
    assert parse_sample_token("local-ref:copy-number") == ("ref", COPY_NUMBER)
    # A policy-style suffix, an explicit fragment suffix and damage name no analysis.
    for token in ("local-ref:research-span-v1", "local-ref:fragment", "local-:cell-origin",
                  "synthetic-sample-token", "local-ref:cell-origin:x"):
        assert parse_sample_token(token) is None
    with pytest.raises(ValueError):
        sample_token("ref", "research-span-v1")


def test_only_reserved_analyses_take_registered_stages() -> None:
    fake = FakeAnalysis(CELL_ORIGIN)
    for analysis in (FRAGMENT, "other"):
        with pytest.raises(ValueError):
            register_analysis_stages(
                AnalysisStages(analysis, "x-probe", fake.definition, fake.stages)
            )
    with pytest.raises(ValueError):  # copy number cannot take cell origin's setting
        register_analysis_stages(
            AnalysisStages(COPY_NUMBER, "cn-probe", fake.definition, fake.stages,
                           config_keys=frozenset({"modbase_model"}))
        )


def test_modbase_model_needs_cell_origin_and_preflight_analysis_needs_reference(
    root, inputs, capsys
) -> None:
    with pytest.raises(SystemExit) as stopped:
        cli.main(["run", str(inputs.bam_path), "--reference", "ref", "--root", str(root),
                  "--modbase-model", "model-a"])
    assert stopped.value.code == 2
    with pytest.raises(SystemExit) as stopped:
        cli.main(["preflight", str(inputs.bam_path), "--analysis", "fragment",
                  "--root", str(root)])
    assert stopped.value.code == 2
    assert not (root / "runner").exists()


def test_worst_exit_ranks_terminal_above_retryable() -> None:
    E = cli.ExitCode
    assert cli._worst_exit([E.OK, E.RETRYABLE_FAILURE]) == E.RETRYABLE_FAILURE
    assert cli._worst_exit([E.RETRYABLE_FAILURE, E.BLOCKED, E.OK]) == E.BLOCKED
    assert cli._worst_exit([E.NOT_FOUND, E.BLOCKED]) == E.BLOCKED
    assert cli._worst_exit([E.BLOCKED, E.INTERNAL_ERROR]) == E.INTERNAL_ERROR
    assert cli._worst_exit([E.OK, E.OK]) == E.OK


# --------------------------------------------------------------------------
# Orchestrator
# --------------------------------------------------------------------------


def test_fragment_record_is_published_when_another_analysis_refuses(
    root, inputs, capsys
) -> None:
    # Mandatory (SH4): no cell-origin stages exist in this build.
    code, payload = _run(capsys, root, inputs.bam_path, "--analysis", "fragment,cell-origin")
    assert code == cli.ExitCode.BLOCKED, payload
    rows = _rows(payload)
    assert [row["analysis"] for row in payload["data"]["analyses"]] == [FRAGMENT, CELL_ORIGIN]
    fragment, cell = rows[FRAGMENT], rows[CELL_ORIGIN]
    assert fragment["status"] == "ok" and fragment["exit_code"] == 0
    assert cell["status"] == "blocked" and cell["code"] == "TBX-RUN-011"
    assert "job_id" not in cell
    assert _records(root) == [fragment["record_id"]]
    trust = load_development_trust((root / "trust/development-result-trust.json").read_bytes())
    assert verify_bundle(root / "records" / fragment["record_id"], trust)
    assert payload["summary"].startswith("1 of 2 analyses made a signed local record")


def test_fake_success_gives_two_jobs_two_records_and_dedupes(
    root, inputs, capsys, probe  # noqa: F811
) -> None:
    FakeAnalysis(CELL_ORIGIN).register()
    code, first = _run(capsys, root, inputs.bam_path, "--analysis", "cell-origin,fragment")
    assert code == cli.ExitCode.OK, first
    rows = _rows(first)
    jobs = {name: row["job_id"] for name, row in rows.items()}
    assert len(set(jobs.values())) == 2
    assert _request(root, jobs[CELL_ORIGIN]).sample_token == "local-ref:cell-origin"
    assert _request(root, jobs[FRAGMENT]).sample_token == "local-ref"
    assert rows[CELL_ORIGIN]["measurement_schema"] == "measurements/sh3-probe.v1.json"
    assert sorted(_records(root)) == sorted(row["record_id"] for row in rows.values())
    # The method store for the new analysis is keyed by its definition hash.
    assert (root / "method-authority" / "ref" / "cell-origin-probe").is_dir()

    code, second = _run(capsys, root, inputs.bam_path, "--analysis", "fragment,cell-origin")
    assert code == cli.ExitCode.OK, second
    assert {name: row["job_id"] for name, row in _rows(second).items()} == jobs
    assert len(_records(root)) == 2


def test_human_output_prints_each_job_id_then_a_table(
    root, inputs, capsys, probe  # noqa: F811
) -> None:
    FakeAnalysis(CELL_ORIGIN).register()
    code = cli.main(["run", str(inputs.bam_path), "--reference", "ref", "--root", str(root),
                     "--analysis", "fragment,cell-origin,copy-number"])
    out = capsys.readouterr().out
    assert code == cli.ExitCode.BLOCKED
    lines = out.splitlines()
    job_lines = [line for line in lines if line.startswith("JOB  ")]
    assert [line.split()[1] for line in job_lines] == [FRAGMENT, CELL_ORIGIN]
    header = next(index for index, line in enumerate(lines) if line.startswith("ANALYSIS "))
    # Job IDs are printed before the result table, as each job is admitted.
    assert all(lines.index(line) < header for line in job_lines)
    assert "copy-number: The copy-number analysis is not available in this build" in out
    assert "  FIX  " in out


def test_terminal_refusal_of_one_analysis_is_scoped_to_it(
    root, inputs, capsys, probe  # noqa: F811
) -> None:
    FakeAnalysis(COPY_NUMBER, "refuse").register()
    code, first = _run(capsys, root, inputs.bam_path, "--analysis", "fragment,copy-number")
    assert code == cli.ExitCode.BLOCKED, first
    rows = _rows(first)
    assert rows[FRAGMENT]["status"] == "ok"
    refused = rows[COPY_NUMBER]
    assert refused["code"] == "TBX-RUN-005" and refused["job_id"]
    assert _state(root, refused["job_id"]) == JobState.TERMINAL_FAILURE

    # Again: the failed copy-number job refuses only copy number.
    code, second = _run(capsys, root, inputs.bam_path, "--analysis", "fragment,copy-number")
    assert code == cli.ExitCode.BLOCKED, second
    rows = _rows(second)
    assert rows[FRAGMENT]["status"] == "ok"
    assert rows[FRAGMENT]["record_id"] == _rows(first)[FRAGMENT]["record_id"]
    assert rows[COPY_NUMBER]["job_id"] == refused["job_id"]
    assert "copy-number analysis already failed terminally" in rows[COPY_NUMBER]["summary"]
    # A plain fragment run on the same input is not refused either.
    code, plain = _run(capsys, root, inputs.bam_path)
    assert code == cli.ExitCode.OK, plain


@pytest.mark.parametrize("mode", ["tool", "tool-refusal"])
def test_missing_tool_at_stage_time_is_retryable_then_resumes(
    root, inputs, capsys, probe, mode  # noqa: F811
) -> None:
    fake = FakeAnalysis(CELL_ORIGIN, mode).register()
    code, payload = _run(capsys, root, inputs.bam_path, "--analysis", "fragment,cell-origin")
    assert code == cli.ExitCode.RETRYABLE_FAILURE, payload
    row = _rows(payload)[CELL_ORIGIN]
    assert row["code"] == "TBX-TOOL-001" and row["retryable"] is True
    assert row["exit_code"] == cli.ExitCode.RETRYABLE_FAILURE
    job_id = row["job_id"]
    assert row["next_action"] == f"traceback resume {job_id} --root <same-root>"
    assert _state(root, job_id) == JobState.RETRYABLE_FAILURE
    assert _rows(payload)[FRAGMENT]["status"] == "ok"

    fake.installed = True
    code, resumed = _json(capsys, "resume", job_id, "--root", root)
    assert code == cli.ExitCode.OK, resumed
    assert resumed["data"]["state"] == "complete"
    assert resumed["data"]["record_id"] in _records(root)


def test_free_space_is_checked_once_for_every_analysis(
    root, inputs, capsys, monkeypatch
) -> None:
    size = inputs.bam_path.stat().st_size + Path(f"{inputs.bam_path}.bai").stat().st_size
    real = shutil.disk_usage

    def usage(path):
        return real(path)._replace(free=3 * size)  # enough for one copy, not two

    monkeypatch.setattr(cli.shutil, "disk_usage", usage)
    code, payload = _run(capsys, root, inputs.bam_path, "--analysis", "fragment,cell-origin")
    assert code == cli.ExitCode.BLOCKED
    assert payload["data"]["code"] == "TBX-RUN-004"
    assert payload["data"]["required_bytes"] == 2 * 2 * size
    assert not (root / "runner").exists()
    code, payload = _run(capsys, root, inputs.bam_path)
    assert code == cli.ExitCode.OK, payload


# --------------------------------------------------------------------------
# Identity: resolved settings
# --------------------------------------------------------------------------


def test_modbase_model_is_part_of_the_job_identity(
    root, inputs, capsys, probe  # noqa: F811
) -> None:
    fake = FakeAnalysis(CELL_ORIGIN, config_keys={"modbase_model"}).register()
    code, first = _run(capsys, root, inputs.bam_path, "--analysis", "cell-origin",
                       "--modbase-model", "model-a")
    assert code == cli.ExitCode.OK, first
    code, second = _run(capsys, root, inputs.bam_path, "--analysis", "cell-origin",
                        "--modbase-model", "model-b")
    assert code == cli.ExitCode.OK, second
    a, b = _rows(first)[CELL_ORIGIN]["job_id"], _rows(second)[CELL_ORIGIN]["job_id"]
    assert a != b
    assert fake.config_seen == [{"modbase_model": "model-a"}, {"modbase_model": "model-b"}]
    workflow = _request(root, a).workflow_release_sha256
    assert read_job_config(root, workflow) == ("ref", CELL_ORIGIN, {"modbase_model": "model-a"})


def test_a_definition_that_ignores_a_setting_is_refused(
    root, inputs, capsys, probe  # noqa: F811
) -> None:
    fake = FakeAnalysis(CELL_ORIGIN, config_keys={"modbase_model"})
    unbound = _definition(CELL_ORIGIN, MethodFamily.CELL_ORIGIN)
    register_analysis_stages(
        AnalysisStages(
            CELL_ORIGIN, "cell-origin-probe", lambda loaded, config: unbound(loaded, {}),
            fake.stages, config_keys=frozenset({"modbase_model"}),
        )
    )
    code, _ = _run(capsys, root, inputs.bam_path, "--analysis", "cell-origin",
                   "--modbase-model", "model-a")
    assert code == cli.ExitCode.OK
    code, payload = _run(capsys, root, inputs.bam_path, "--analysis", "cell-origin",
                         "--modbase-model", "model-b")
    assert code == cli.ExitCode.BLOCKED, payload
    row = _rows(payload)[CELL_ORIGIN]
    assert row["code"] == "TBX-INTERNAL-001" and "job_id" not in row


# --------------------------------------------------------------------------
# Resume
# --------------------------------------------------------------------------


def _pause_after_first_stage(fake: FakeAnalysis, root: Path) -> None:
    original = fake.stages

    def pausing(context: Any) -> tuple[StageSpec, ...]:
        stages = list(original(context))
        first = stages[0]

        def callback(stage: Any) -> Any:
            result = first.callback(stage)
            cli._existing_runner(root).request_pause(stage.job_id)
            return result

        stages[0] = StageSpec(name=first.name, version=first.version, callback=callback)
        return tuple(stages)

    fake.paused_stages = original
    object.__setattr__(fake.spec, "stages", pausing)


def test_resuming_an_interrupted_cell_origin_job_resumes_cell_origin(
    root, inputs, capsys, probe  # noqa: F811
) -> None:
    fake = FakeAnalysis(CELL_ORIGIN, config_keys={"modbase_model"}).register()
    _pause_after_first_stage(fake, root)
    code, payload = _run(capsys, root, inputs.bam_path, "--analysis", "cell-origin",
                         "--modbase-model", "model-a")
    assert code == cli.ExitCode.OK, payload
    row = _rows(payload)[CELL_ORIGIN]
    assert row["state"] == "paused" and "record_id" not in row
    object.__setattr__(fake.spec, "stages", fake.paused_stages)

    code, resumed = _json(capsys, "resume", row["job_id"], "--root", root)
    assert code == cli.ExitCode.OK, resumed
    assert resumed["data"]["state"] == "complete"
    # The kept setting rebuilt the same stages.
    assert fake.config_seen[-1] == {"modbase_model": "model-a"}
    trust = load_development_trust((root / "trust/development-result-trust.json").read_bytes())
    verified = verify_bundle(root / resumed["data"]["bundle"], trust)
    assert verified.manifest.schema_version == "traceback.result-bundle.v4"


def test_resume_refuses_tbx_job_003_when_the_method_changed(
    root, inputs, capsys, probe, registry  # noqa: F811
) -> None:
    fake = FakeAnalysis(CELL_ORIGIN).register()
    _pause_after_first_stage(fake, root)
    code, payload = _run(capsys, root, inputs.bam_path, "--analysis", "cell-origin")
    job_id = _rows(payload)[CELL_ORIGIN]["job_id"]
    changed = FakeAnalysis(CELL_ORIGIN, tag="v2")
    registry[CELL_ORIGIN] = changed.spec
    code, refused = _json(capsys, "resume", job_id, "--root", root)
    assert code == cli.ExitCode.BLOCKED, refused
    assert refused["data"]["code"] == "TBX-JOB-003"
    assert _state(root, job_id) == JobState.PAUSED
    # No store was created for the method that was not resumed.
    stores = list((root / "method-authority" / "ref" / "cell-origin-probe").iterdir())
    assert len(stores) == 1


def test_resume_refuses_tbx_job_003_for_a_changed_fragment_method(
    root, inputs, capsys, monkeypatch
) -> None:
    original = cli._local_stages

    def pausing(*args, **kwargs):
        stages = list(original(*args, **kwargs))
        first = stages[0]

        def callback(context):
            result = first.callback(context)
            cli._existing_runner(root).request_pause(context.job_id)
            return result

        stages[0] = StageSpec(name=first.name, version=first.version, callback=callback,
                              parameters=first.parameters)
        return tuple(stages)

    with monkeypatch.context() as patch:
        patch.setattr(cli, "_local_stages", pausing)
        code, paused = _run(capsys, root, inputs.bam_path)
    assert paused["data"]["state"] == "paused", paused
    monkeypatch.setattr(cli, "_local_method_sha256", lambda registered: "e" * 64)
    code, refused = _json(capsys, "resume", paused["data"]["job_id"], "--root", root)
    assert code == cli.ExitCode.BLOCKED, refused
    assert refused["data"]["code"] == "TBX-JOB-003"


def test_resume_refuses_a_token_that_names_no_analysis(root, inputs, capsys) -> None:
    from traceback_runner.contracts import JobRequest

    runner = Runner(root / "runner", local_unqualified_enabled=True)
    source = inputs.bam_path.parent
    names = (inputs.bam_path.name, f"{inputs.bam_path.name}.bai")
    from traceback_runner.snapshots import input_tree_sha256

    record = runner.submit(
        JobRequest(
            sample_token="local-ref:research-span-v1",
            input_kind=InputKind.MODBAM,
            input_tree_sha256_local=input_tree_sha256(source, names),
            workflow_release_sha256="d" * 64,
        ),
        source,
        names,
    )
    if record.state != JobState.QUEUED:
        runner.store.transition(record.job_id, JobState.QUEUED, "test: queue it")
    code, payload = _json(capsys, "resume", record.job_id, "--root", root)
    assert code == cli.ExitCode.BLOCKED, payload
    assert "not one this version of traceback can resume" in payload["summary"]


# --------------------------------------------------------------------------
# preflight --analysis
# --------------------------------------------------------------------------


def test_preflight_analysis_reports_readiness(root, inputs, capsys, monkeypatch) -> None:
    def missing(**_: Any):
        raise ToolProblem(TOOL_MISSING, "modkit 0.6.4 is not installed", tool="modkit",
                          cause="c", fix="Run `traceback toolchain install modkit`")

    monkeypatch.setattr("traceback_runner.toolchain.resolve_modkit", missing)
    argv = ("preflight", inputs.bam_path, "--reference", "ref", "--root", root)
    code, plain = _json(capsys, *argv)
    assert "analyses" not in plain["data"]
    code, payload = _json(capsys, *argv, "--analysis", "fragment,cell-origin,copy-number")
    assert code == plain_code(plain)
    results = {item["analysis"]: item for item in payload["data"]["analyses"]}
    assert results[FRAGMENT]["readiness"] == "ready"
    cell = {check["code"]: check for check in results[CELL_ORIGIN]["checks"]}
    assert results[CELL_ORIGIN]["readiness"] == "blocked"
    assert cell["TBX-TOOL-001"]["outcome"] == "missing"
    assert cell["TBX-RUN-011"]["outcome"] == "blocked"
    copy = {check["code"] for check in results[COPY_NUMBER]["checks"]}
    assert copy == {"TBX-RUN-011"}
    assert not (root / "runner").exists()


def plain_code(payload: dict) -> int:
    return cli.ExitCode.OK if payload["status"] == "ok" else cli.ExitCode.BLOCKED


def test_modbase_readiness_names_the_flag(monkeypatch) -> None:
    from traceback_runner.contracts import PreflightOutcome

    class Check:
        def __init__(self, code, outcome):
            self.code, self.outcome, self.problem = code, outcome, "p"

    class Report:
        def __init__(self, outcome):
            self.checks = [Check("TBX-MOD-001", outcome)]

    row = cli._modbase_readiness(Report(PreflightOutcome.WARN), None)
    assert (row.code, row.outcome) == ("TBX-MOD-001", "blocked")
    assert row.detail == "BLOCKED for cell origin: pass `--modbase-model`"
    assert cli._modbase_readiness(Report(PreflightOutcome.WARN), "m").outcome == "ready"
    assert cli._modbase_readiness(Report(PreflightOutcome.PASS), None).outcome == "ready"
    assert cli._modbase_readiness(Report(PreflightOutcome.PARTIAL), "m").outcome == "blocked"


def test_no_host_path_in_json_rows(root, inputs, capsys) -> None:
    code, payload = _run(capsys, root, inputs.bam_path, "--analysis", "fragment,copy-number")
    text = json.dumps(_rows(payload)[COPY_NUMBER])
    assert str(inputs.bam_path) not in text and os.fspath(root) not in text
