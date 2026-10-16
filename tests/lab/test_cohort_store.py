"""Synthetic Lab cohort storage and publication checks."""

import json
from types import SimpleNamespace

import pytest

from traceback_lab import cohorts
from traceback_lab.cohorts import add_sample, build_cohort, project_current_metric, project_sample_metric, read_current, read_roster
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


def _sample(store: LabStore, index: int, *, example: bool = False, value: float = 100.0, stale: bool = False, aligned: bool = True) -> str:
    source_id, input_id = _sha(index), _sha(index + 100)
    parameters = parameters_sha256(
        algorithm_id="fragment-size", algorithm_version="1",
        parameters={"changed": True} if stale else {}, tools=[], reference_sha256=_sha(900),
    )
    store.sources_dir.mkdir(parents=True, exist_ok=True)
    (store.sources_dir / f"{source_id}.json").write_text(json.dumps({
        "source_id": source_id, "example": example,
        "inputs": [{"input_id": input_id, "kind": "aligned" if aligned else "prealigned", "used_utc": "2026-10-16T00:00:00Z"}],
    }))
    input_dir = store.inputs_dir / input_id
    input_dir.mkdir(parents=True, exist_ok=True)
    (input_dir / "manifest.json").write_text(json.dumps({
        "kind": "aligned" if aligned else "prealigned", "source_id": source_id,
        "input_id": input_id, "reference_sha256": _sha(900),
        "tools": {"samtools": "1.20", "minimap2": "2.28"} if aligned else None,
        "mmi_sha256": _sha(901) if aligned else None,
        "aligner_argv": ["minimap2", "-ax", "map-ont"] if aligned else None,
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
    assert one.members[0].results[0].cohort_compat_key == one.members[1].results[0].cohort_compat_key
    compat = one.members[0].results[0].cohort_compat_key
    projected = project_current_metric(store, "healthy", algorithm_id="fragment-size", metric_id="mode_bp",
                                       cohort_compat_key=compat, target_source_id=first)
    assert projected is not None
    assert projected.values == (110.0,)
    assert projected.exclusions == {"self": 1}
    assert project_sample_metric(store, "healthy", target_source_id=first,
                                 algorithm_id="fragment-size", metric_id="mode_bp") == projected
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
    with pytest.raises(LabProblem, match="TBX-LAB-022"):
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
    with pytest.raises(LabProblem, match="TBX-LAB-022"):
        read_current(store, "healthy")
    with pytest.raises(LabProblem, match="TBX-LAB-022"):
        build_cohort(store, "healthy")
    assert list(outside.iterdir()) == []


def test_unknown_prealigned_provenance_never_pools_distinct_inputs(store: LabStore) -> None:
    first = _sample(store, 1, aligned=False)
    second = _sample(store, 2, aligned=False)
    add_sample(store, "healthy", first)
    add_sample(store, "healthy", second)
    generation = build_cohort(store, "healthy")
    first_key = generation.members[0].results[0].cohort_compat_key
    second_key = generation.members[1].results[0].cohort_compat_key
    assert first_key != second_key
    assert all("provenance unknown" in member.aligner_identity for member in generation.members)
    projection = project_sample_metric(store, "healthy", target_source_id=first,
                                       algorithm_id="fragment-size", metric_id="mode_bp")
    assert projection is not None
    assert projection.percentile_n == 0
    assert projection.exclusions == {"self": 1, "incompatible": 1}
    from traceback_lab.cohort_command import _build
    _, payload = _build(SimpleNamespace(cohort="healthy", kind="internal_cohort"), store)
    assert "each input is isolated" in payload["data"]["comparability_note"]
    assert any("provenance is unknown" in line for line in payload["_human"]["lines"])


@pytest.mark.parametrize("field,bad", [("reference_sha256", None), ("tools", [])])
def test_corrupt_selected_input_manifest_preserves_current(
    store: LabStore, field: str, bad: object,
) -> None:
    source_id = _sample(store, 1)
    add_sample(store, "healthy", source_id)
    old = build_cohort(store, "healthy")
    before = store.generation()
    manifest_path = store.inputs_dir / _sha(101) / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest[field] = bad
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(LabProblem, match="TBX-LAB-020") as error:
        build_cohort(store, "healthy")
    assert error.value.data["field"] == ("aligner tools" if field == "tools" else field)
    assert read_current(store, "healthy") == old
    assert store.generation() == before
    assert not (store.lab / "cohorts" / "healthy" / "generations" / "2").exists()


def test_mismatched_source_record_refuses_add(store: LabStore) -> None:
    source_id = _sample(store, 1)
    source_path = store.sources_dir / f"{source_id}.json"
    record = json.loads(source_path.read_text())
    record["source_id"] = _sha(2)
    source_path.write_text(json.dumps(record))
    with pytest.raises(LabProblem, match="TBX-LAB-020") as error:
        add_sample(store, "healthy", source_id)
    assert error.value.data["field"] == "source_id"
    assert read_roster(store, "healthy") == ()


def test_mismatched_source_record_preserves_current_generation(store: LabStore) -> None:
    source_id = _sample(store, 1)
    add_sample(store, "healthy", source_id)
    old = build_cohort(store, "healthy")
    before = store.generation()
    source_path = store.sources_dir / f"{source_id}.json"
    record = json.loads(source_path.read_text())
    record["source_id"] = _sha(2)
    source_path.write_text(json.dumps(record))
    with pytest.raises(LabProblem, match="TBX-LAB-020") as error:
        build_cohort(store, "healthy")
    assert error.value.data["field"] == "source_id"
    assert read_current(store, "healthy") == old
    assert store.generation() == before
    assert not (store.lab / "cohorts" / "healthy" / "generations" / "2").exists()


@pytest.mark.parametrize("contents,field", [
    ('{"version":true}', "current version"),
    ('{"version":1', "current.json"),
    ('[]', "current.json"),
])
def test_corrupt_current_pointer_has_actionable_problem(
    store: LabStore, contents: str, field: str,
) -> None:
    directory = store.lab / "cohorts" / "healthy"
    directory.mkdir(parents=True)
    (directory / "current.json").write_text(contents)
    with pytest.raises(LabProblem, match="TBX-LAB-022") as error:
        read_current(store, "healthy")
    assert error.value.data == {"cohort_id": "healthy", "field": field}
    assert error.value.fix


def test_corrupt_pointer_cannot_be_overwritten_by_build(store: LabStore) -> None:
    source_id = _sample(store, 1)
    add_sample(store, "healthy", source_id)
    old = build_cohort(store, "healthy")
    pointer = store.lab / "cohorts" / "healthy" / "current.json"
    pointer.write_text("{")
    before = store.generation()
    with pytest.raises(LabProblem, match="TBX-LAB-022"):
        build_cohort(store, "healthy")
    assert pointer.read_text() == "{"
    assert store.generation() == before
    assert (store.lab / "cohorts" / "healthy" / "generations" / str(old.version) / "generation.json").is_file()


def test_corrupt_roster_and_missing_source_fail_before_publish(store: LabStore) -> None:
    source_id = _sample(store, 1)
    add_sample(store, "healthy", source_id)
    old = build_cohort(store, "healthy")
    before = store.generation()
    roster = store.lab / "cohorts" / "healthy" / "members.json"
    saved = roster.read_bytes()
    roster.write_text("{")
    with pytest.raises(LabProblem, match="TBX-LAB-022") as error:
        build_cohort(store, "healthy")
    assert error.value.data["field"] == "members.json"
    roster.write_bytes(saved)
    (store.sources_dir / f"{source_id}.json").unlink()
    with pytest.raises(LabProblem, match="TBX-LAB-020") as error:
        build_cohort(store, "healthy")
    assert error.value.data["field"] == "source record"
    assert read_current(store, "healthy") == old
    assert store.generation() == before


@pytest.mark.parametrize("damage,field", [
    ("missing", "generation.json"),
    ("json", "generation.json"),
    ("identity", "generation identity"),
    ("source", "generation.json"),
])
def test_damaged_generation_has_actionable_problem(
    store: LabStore, damage: str, field: str,
) -> None:
    source_id = _sample(store, 1)
    add_sample(store, "healthy", source_id)
    old = build_cohort(store, "healthy")
    path = store.lab / "cohorts" / "healthy" / "generations" / "1" / "generation.json"
    if damage == "missing":
        path.unlink()
    elif damage == "json":
        path.write_text("{")
    else:
        record = json.loads(path.read_text())
        if damage == "identity":
            record["cohort_id"] = "other"
        else:
            record["members"][0]["source_id"] = "not-a-sha"
        path.write_text(json.dumps(record))
    with pytest.raises(LabProblem, match="TBX-LAB-022") as error:
        read_current(store, "healthy")
    assert error.value.data == {"cohort_id": "healthy", "field": field}
    assert old.version == 1


def test_missing_pointer_with_existing_generation_fails_closed(store: LabStore) -> None:
    source_id = _sample(store, 1)
    add_sample(store, "healthy", source_id)
    build_cohort(store, "healthy")
    (store.lab / "cohorts" / "healthy" / "current.json").unlink()
    with pytest.raises(LabProblem, match="TBX-LAB-022") as error:
        read_current(store, "healthy")
    assert error.value.data["field"] == "current.json"


def test_orphan_generation_can_be_rebuilt_from_intact_roster(store: LabStore) -> None:
    source_id = _sample(store, 1)
    add_sample(store, "healthy", source_id)
    first = build_cohort(store, "healthy")
    (store.lab / "cohorts" / "healthy" / "current.json").unlink()
    second = build_cohort(store, "healthy")
    assert second.version == 2
    assert read_current(store, "healthy") == second
    assert (store.lab / "cohorts" / "healthy" / "generations" / "1" / "generation.json").is_file()
    assert first.members == second.members


def test_invalid_generation_name_blocks_build_without_changing_pointer(store: LabStore) -> None:
    source_id = _sample(store, 1)
    add_sample(store, "healthy", source_id)
    old = build_cohort(store, "healthy")
    before = store.generation()
    (store.lab / "cohorts" / "healthy" / "generations" / "01").mkdir()
    with pytest.raises(LabProblem, match="TBX-LAB-022") as error:
        build_cohort(store, "healthy")
    assert error.value.data["field"] == "generation name"
    assert read_current(store, "healthy") == old
    assert store.generation() == before


def test_damaged_old_generation_does_not_hide_valid_current(store: LabStore) -> None:
    source_id = _sample(store, 1)
    add_sample(store, "healthy", source_id)
    build_cohort(store, "healthy")
    current = build_cohort(store, "healthy")
    (store.lab / "cohorts" / "healthy" / "generations" / "1" / "generation.json").write_text("{")
    assert read_current(store, "healthy") == current


def test_cohort_listing_keeps_healthy_entries_when_one_is_damaged(store: LabStore) -> None:
    from traceback_lab.cohorts import list_cohorts

    source_id = _sample(store, 1)
    for cohort_id in ("broken", "healthy"):
        add_sample(store, cohort_id, source_id)
        build_cohort(store, cohort_id)
    add_sample(store, "pending", source_id)
    (store.lab / "cohorts" / "broken" / "current.json").write_text("{")
    entries = list_cohorts(store)
    assert [entry.cohort_id for entry in entries] == ["broken", "healthy"]
    assert entries[0].generation is None
    assert entries[0].problem is not None
    assert entries[0].problem.code == "TBX-LAB-022"
    assert entries[1].generation == read_current(store, "healthy")
    assert entries[1].problem is None


def test_cohort_listing_reports_symlink_without_following_it(store: LabStore, tmp_path) -> None:
    from traceback_lab.cohorts import list_cohorts

    source_id = _sample(store, 1)
    add_sample(store, "healthy", source_id)
    healthy = build_cohort(store, "healthy")
    outside = tmp_path / "outside-cohort"
    outside.mkdir()
    (store.lab / "cohorts" / "broken").symlink_to(outside, target_is_directory=True)
    entries = list_cohorts(store)
    assert [entry.cohort_id for entry in entries] == ["broken", "healthy"]
    assert entries[0].problem is not None
    assert entries[0].problem.data["field"] == "cohort directory"
    assert entries[1].generation == healthy
