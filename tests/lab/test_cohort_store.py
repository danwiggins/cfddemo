"""Synthetic Lab cohort storage and publication checks."""

import json
from types import SimpleNamespace

import pytest

from traceback_lab import cohorts
from traceback_lab.cohorts import add_sample, build_cohort, project_current_metric, read_current, read_roster
from traceback_lab.envelope import Metric, ResultEnvelope, provenance
from traceback_lab.identity import parameters_sha256
from traceback_lab.problems import LabProblem
from traceback_lab.store import LabStore


def _sha(index: int) -> str:
    return f"{index:064x}"


@pytest.fixture
def store(tmp_path, monkeypatch) -> LabStore:
    lab = LabStore(tmp_path)
    algorithm = SimpleNamespace(id="fragment-size", version="1", parameters={}, tools=[], upstream=())
    monkeypatch.setattr(cohorts, "load_registry", lambda: (algorithm,))
    return lab


def _sample(store: LabStore, index: int, *, example: bool = False, value: float = 100.0, stale: bool = False) -> str:
    source_id, input_id = _sha(index), _sha(index + 100)
    parameters = parameters_sha256(
        algorithm_id="fragment-size", algorithm_version="1",
        parameters={"changed": True} if stale else {}, tools=[], reference_sha256=_sha(900),
    )
    store.sources_dir.mkdir(parents=True, exist_ok=True)
    (store.sources_dir / f"{source_id}.json").write_text(json.dumps({
        "source_id": source_id, "example": example,
        "inputs": [{"input_id": input_id, "kind": "prealigned", "used_utc": "2026-10-16T00:00:00Z"}],
    }))
    input_dir = store.inputs_dir / input_id
    input_dir.mkdir(parents=True, exist_ok=True)
    (input_dir / "manifest.json").write_text(json.dumps({
        "kind": "prealigned", "source_id": source_id, "reference_sha256": _sha(900),
    }))
    result = ResultEnvelope(
        algorithm_id="fragment-size", algorithm_version="1", status="ok",
        provenance=provenance(tool="fixture", parameters_sha256=parameters,
                              source_id=source_id, input_id=input_id, reference_id="synthetic"),
        metrics=[Metric(metric_id="mode_bp", value=value)],
    )
    store.publish_result(result, input_id=input_id)
    return source_id


def test_add_is_idempotent_and_build_publishes_immutable_generations(store: LabStore) -> None:
    first = _sample(store, 1)
    second = _sample(store, 2, value=110)
    assert add_sample(store, "healthy", first)
    assert not add_sample(store, "healthy", first)
    assert add_sample(store, "healthy", second)
    assert read_roster(store, "healthy") == (first, second)
    before = store.generation()
    one = build_cohort(store, "healthy")
    assert store.generation() == before + 1
    assert one.version_id == "healthy:1"
    assert one.percentile_n == 2
    compat = one.members[0].results[0].cohort_compat_key
    projected = project_current_metric(store, "healthy", algorithm_id="fragment-size", metric_id="mode_bp",
                                       cohort_compat_key=compat, target_source_id=first)
    assert projected is not None
    assert projected.values == (110.0,)
    assert projected.exclusions == {"self": 1}
    two = build_cohort(store, "healthy", kind="provisional_external")
    assert two.version_id == "healthy:2"
    assert two.kind == "provisional_external"
    assert (store.lab / "cohorts" / "healthy" / "generations" / "1" / "generation.json").is_file()
    assert read_current(store, "healthy") == two


def test_example_is_rejected_before_roster_write(store: LabStore) -> None:
    source_id = _sample(store, 1, example=True)
    with pytest.raises(LabProblem, match="TBX-LAB-021"):
        add_sample(store, "healthy", source_id[:12])
    assert read_roster(store, "healthy") == ()


def test_failed_publish_keeps_previous_pointer(store: LabStore, monkeypatch) -> None:
    source_id = _sample(store, 1)
    add_sample(store, "healthy", source_id[:12])
    first = build_cohort(store, "healthy")
    before = store.generation()

    def fail(*args, **kwargs):
        raise OSError("synthetic write failure")

    monkeypatch.setattr(store, "publish_directory", fail)
    with pytest.raises(OSError, match="synthetic write failure"):
        build_cohort(store, "healthy")
    assert read_current(store, "healthy") == first
    assert store.generation() == before


def test_second_writer_is_refused(store: LabStore) -> None:
    source_id = _sample(store, 1)
    with store.writer("run", "r-20261016-0000-abcd"):
        with pytest.raises(LabProblem, match="TBX-LAB-012"):
            add_sample(store, "healthy", source_id[:12])


def test_stale_execution_is_not_relabelled_as_current_compatible(store: LabStore) -> None:
    source_id = _sample(store, 1, stale=True)
    add_sample(store, "healthy", source_id)
    generation = build_cohort(store, "healthy")
    assert generation.percentile_n == 1
    assert generation.members[0].results == ()


def test_corrupt_pointer_and_unsafe_cohort_id_are_refused(store: LabStore) -> None:
    source_id = _sample(store, 1)
    add_sample(store, "healthy", source_id)
    build_cohort(store, "healthy")
    pointer = store.lab / "cohorts" / "healthy" / "current.json"
    pointer.write_text('{"version":true}')
    with pytest.raises(ValueError, match="current version"):
        read_current(store, "healthy")
    with pytest.raises(ValueError, match="cohort id"):
        read_current(store, "../escape")


def test_reader_sees_complete_old_or_new_generation(store: LabStore, monkeypatch) -> None:
    from threading import Event, Thread

    source_id = _sample(store, 1)
    add_sample(store, "healthy", source_id)
    old = build_cohort(store, "healthy")
    at_switch, resume = Event(), Event()
    original = cohorts.write_atomic

    def pause_at_switch(path, content):
        if path.name == "current.json":
            at_switch.set()
            assert resume.wait(10)
        return original(path, content)

    monkeypatch.setattr(cohorts, "write_atomic", pause_at_switch)
    built = []
    thread = Thread(target=lambda: built.append(build_cohort(store, "healthy")), daemon=True)
    thread.start()
    assert at_switch.wait(10)
    assert read_current(store, "healthy") == old
    resume.set()
    thread.join(10)
    assert not thread.is_alive()
    assert built[0].version == old.version + 1
    assert read_current(store, "healthy") == built[0]


def test_changed_asset_digest_cannot_reuse_old_result(store: LabStore) -> None:
    source_id = _sample(store, 1)
    algorithm = cohorts.load_registry()[0]
    algorithm.asset_sha256s = lambda root: (_sha(901),)
    current_hash = parameters_sha256(
        algorithm_id="fragment-size", algorithm_version="1", parameters={}, tools=[],
        reference_sha256=_sha(900), asset_sha256s=(_sha(901),),
    )
    input_id = _sha(101)
    result = ResultEnvelope(
        algorithm_id="fragment-size", algorithm_version="1", status="ok",
        provenance=provenance(tool="fixture", parameters_sha256=current_hash,
                              source_id=source_id, input_id=input_id, reference_id="synthetic"),
        metrics=[Metric(metric_id="mode_bp", value=100.0)],
    )
    store.publish_result(result, input_id=input_id)
    add_sample(store, "healthy", source_id)
    assert len(build_cohort(store, "healthy").members[0].results) == 1
    algorithm.asset_sha256s = lambda root: (_sha(902),)
    assert build_cohort(store, "healthy").members[0].results == ()


def test_symlinked_cohort_directory_is_refused(store: LabStore, tmp_path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (store.lab / "cohorts").mkdir(parents=True)
    (store.lab / "cohorts" / "healthy").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="may not be a symlink"):
        read_current(store, "healthy")


def test_parser_hook_exposes_add_and_build_without_cli_edit() -> None:
    import argparse
    from traceback_lab.cohort_command import add_cohort_parser

    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    add_cohort_parser(commands, lambda child: None)
    add = parser.parse_args(["cohort", "add", "abc", "--cohort", "healthy"])
    build = parser.parse_args(["cohort", "build", "--cohort", "healthy"])
    assert add.cohort_action == "add" and callable(add.handler)
    assert build.cohort_action == "build" and callable(build.handler)
    with pytest.raises(SystemExit):
        parser.parse_args(["cohort", "build", "--cohort", "../escape"])


def test_symlinked_generations_are_refused_by_read_and_build(store: LabStore, tmp_path) -> None:
    directory = store.lab / "cohorts" / "healthy"
    directory.mkdir(parents=True)
    outside = tmp_path / "outside-generations"
    outside.mkdir()
    (directory / "generations").symlink_to(outside, target_is_directory=True)
    (directory / "current.json").write_text('{"version":1}')
    with pytest.raises(ValueError, match="generations may not be a symlink"):
        read_current(store, "healthy")
    with pytest.raises(ValueError, match="generations may not be a symlink"):
        build_cohort(store, "healthy")
    assert list(outside.iterdir()) == []
