"""Synthetic Lab cohort storage and publication checks."""

import json
from types import SimpleNamespace

import pytest
import pysam

from traceback_lab import cohorts
from traceback_lab.cohorts import add_sample, build_cohort, project_current_metric, project_sample_metric, read_current, read_roster, remove_sample
from traceback_lab.envelope import Metric, ResultEnvelope, provenance
from traceback_lab.identity import parameters_sha256
from traceback_lab.ingest import resolve_sample
from traceback_lab.problems import LabProblem
from traceback_lab.store import LabStore
from traceback_runner import cli as signed_cli


def _sha(index: int) -> str:
    return f"{index:064x}"


@pytest.fixture
def store(tmp_path, monkeypatch) -> LabStore:
    lab = LabStore(tmp_path)
    algorithm = SimpleNamespace(id="fragment-size", version="1", parameters={}, tools=[], upstream=())
    monkeypatch.setattr(cohorts, "load_registry", lambda: (algorithm,))
    return lab


def _sample(
    store: LabStore, index: int, *, example: bool = False, value: float = 100.0,
    stale: bool = False, aligned: bool = True, minimap2: str = "2.28",
) -> str:
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
        "tools": {"samtools": "1.20", "minimap2": minimap2} if aligned else None,
        "mmi_sha256": _sha(901) if aligned else None,
        "aligner_argv": ["minimap2", "-ax", "map-ont"] if aligned else None,
    }))
    header = {"HD": {"VN": "1.6"}, "SQ": [{"SN": "chr1", "LN": 1000}],
              "RG": [{"ID": "one", "PL": "ONT"}]}
    with pysam.AlignmentFile(str(input_dir / "aligned.bam"), "wb", header=header):
        pass
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


def test_cohort_cli_reports_distinct_aligner_versions(store: LabStore, capsys) -> None:
    first = _sample(store, 1, minimap2="2.28")
    second = _sample(store, 2, minimap2="2.99-r9999")
    for source in (first, second):
        assert signed_cli.main(["lab", "cohort", "add", source, "--cohort", "healthy", "--root", str(store.root)]) == 0
        capsys.readouterr()
    argv = ["lab", "cohort", "build", "--cohort", "healthy", "--root", str(store.root)]
    assert signed_cli.main(argv) == 0
    human = capsys.readouterr().out
    assert "ALIGNER minimap2 2.28; samtools 1.20: 1" in human
    assert "ALIGNER minimap2 2.99-r9999; samtools 1.20: 1" in human
    assert signed_cli.main([*argv, "--json"]) == 0
    data = json.loads(capsys.readouterr().out)["data"]
    assert data["aligner_counts"] == {
        "minimap2 2.28; samtools 1.20": 1,
        "minimap2 2.99-r9999; samtools 1.20": 1,
    }


def test_remove_unrecoverable_member_preserves_published_generation_until_rebuild(
    store: LabStore, capsys,
) -> None:
    first = _sample(store, 1)
    damaged = _sample(store, 2)
    add_sample(store, "healthy", first)
    add_sample(store, "healthy", damaged)
    published = build_cohort(store, "healthy")
    (store.sources_dir / f"{damaged}.json").unlink()
    with pytest.raises(LabProblem):
        build_cohort(store, "healthy")
    assert read_current(store, "healthy") == published

    code = signed_cli.main([
        "lab", "cohort", "remove", damaged, "--cohort", "healthy",
        "--root", str(store.root), "--json",
    ])
    assert code == 0
    assert json.loads(capsys.readouterr().out)["data"]["source_id"] == damaged
    assert read_roster(store, "healthy") == (first,)
    assert read_current(store, "healthy") == published
    assert (store.inputs_dir / _sha(102) / "aligned.bam").is_file()
    assert (store.results_dir / _sha(102)).is_dir()

    rebuilt = build_cohort(store, "healthy")
    assert rebuilt.version_id == "healthy:2"
    assert tuple(member.source_id for member in rebuilt.members) == (first,)
    assert read_current(store, "healthy") == rebuilt


def test_remove_requires_unique_roster_member_without_mutation(store: LabStore) -> None:
    first = _sample(store, 1)
    second = _sample(store, 2)
    add_sample(store, "healthy", first)
    add_sample(store, "healthy", second)
    before = read_roster(store, "healthy")
    with pytest.raises(LabProblem) as ambiguous:
        remove_sample(store, "healthy", first[:12])
    assert ambiguous.value.code == "TBX-LAB-017"
    assert "matches several" in ambiguous.value.cause
    with pytest.raises(LabProblem) as missing:
        remove_sample(store, "healthy", "f" * 12)
    assert missing.value.code == "TBX-LAB-017"
    assert "not in this cohort" in missing.value.cause
    assert read_roster(store, "healthy") == before


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
    add_sample(store, "healthy", source_id)
    before = read_roster(store, "healthy")
    with store.writer("run", "r-20261016-0000-abcd"):
        with pytest.raises(LabProblem, match="TBX-LAB-012"):
            add_sample(store, "healthy", source_id[:12])
        with pytest.raises(LabProblem, match="TBX-LAB-012"):
            remove_sample(store, "healthy", source_id[:12])
    assert read_roster(store, "healthy") == before


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


def test_parser_hook_exposes_add_remove_and_build_without_cli_edit() -> None:
    import argparse
    from traceback_lab.cohort_command import add_cohort_parser

    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    add_cohort_parser(commands, lambda child: None)
    add = parser.parse_args(["cohort", "add", "abc", "--cohort", "healthy"])
    remove = parser.parse_args(["cohort", "remove", "abc", "--cohort", "healthy"])
    build = parser.parse_args(["cohort", "build", "--cohort", "healthy"])
    assert add.cohort_action == "add" and callable(add.handler)
    assert remove.cohort_action == "remove" and callable(remove.handler)
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


@pytest.mark.parametrize("damage", ["missing", "unreadable", "mismatched", "unknown"])
@pytest.mark.parametrize("aligned", [False, True])
def test_bam_platform_damage_preserves_current_generation(
    store: LabStore, damage: str, aligned: bool,
) -> None:
    source_id = _sample(store, 1, aligned=aligned)
    add_sample(store, "healthy", source_id)
    old = build_cohort(store, "healthy")
    before = store.generation()
    bam_path = store.inputs_dir / _sha(101) / "aligned.bam"
    if damage == "missing":
        bam_path.unlink()
    elif damage == "unreadable":
        bam_path.write_bytes(b"not a BAM")
    else:
        header = {"HD": {"VN": "1.6"}, "SQ": [{"SN": "chr1", "LN": 1000}]}
        if damage == "mismatched":
            header["RG"] = [{"ID": "one", "PL": "ILLUMINA"}]
            manifest_path = bam_path.parent / "manifest.json"
            manifest = json.loads(manifest_path.read_text())
            manifest["platform"] = "ont"
            manifest_path.write_text(json.dumps(manifest))
        with pysam.AlignmentFile(str(bam_path), "wb", header=header):
            pass
    with pytest.raises(LabProblem, match="TBX-LAB-024"):
        build_cohort(store, "healthy")
    assert read_current(store, "healthy") == old
    assert store.generation() == before
    assert not (store.lab / "cohorts" / "healthy" / "generations" / "2").exists()


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


def test_add_refuses_missing_roster_after_publication_without_replacing_history(store: LabStore) -> None:
    first = _sample(store, 1)
    second = _sample(store, 2)
    add_sample(store, "healthy", first)
    build_cohort(store, "healthy")
    directory = store.lab / "cohorts" / "healthy"
    pointer = directory / "current.json"
    generation = directory / "generations" / "1" / "generation.json"
    pointer_before, generation_before = pointer.read_bytes(), generation.read_bytes()
    (directory / "members.json").unlink()
    before = store.generation()

    with pytest.raises(LabProblem, match="TBX-LAB-022") as error:
        add_sample(store, "healthy", second)
    assert error.value.data == {"cohort_id": "healthy", "field": "members.json"}
    assert not (directory / "members.json").exists()
    assert pointer.read_bytes() == pointer_before
    assert generation.read_bytes() == generation_before
    assert store.generation() == before


def test_missing_pointer_problem_explains_roster_rebuild(store: LabStore) -> None:
    first = _sample(store, 1)
    add_sample(store, "healthy", first)
    build_cohort(store, "healthy")
    (store.lab / "cohorts" / "healthy" / "current.json").unlink()
    with pytest.raises(LabProblem, match="TBX-LAB-022") as error:
        read_current(store, "healthy")
    assert error.value.data["field"] == "current.json"
    assert "cohort build" in error.value.fix
    assert "roster" in error.value.fix


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


@pytest.mark.parametrize("field,value", [("version", "1"), ("pon_n", "0")])
def test_generation_rejects_coerced_integer_fields(store: LabStore, field: str, value: str) -> None:
    source_id = _sample(store, 1)
    add_sample(store, "healthy", source_id)
    build_cohort(store, "healthy")
    directory = store.lab / "cohorts" / "healthy"
    pointer = directory / "current.json"
    before_pointer = pointer.read_bytes()
    before_generation = store.generation()
    generation_file = directory / "generations" / "1" / "generation.json"
    record = json.loads(generation_file.read_text())
    record[field] = value
    generation_file.write_text(json.dumps(record))
    with pytest.raises(LabProblem, match="TBX-LAB-022") as error:
        read_current(store, "healthy")
    assert error.value.data["field"] == "generation.json"
    with pytest.raises(LabProblem, match="TBX-LAB-022"):
        build_cohort(store, "healthy")
    assert pointer.read_bytes() == before_pointer
    assert store.generation() == before_generation
    assert not (directory / "generations" / "2").exists()


@pytest.mark.parametrize("location", ["source", "manifest"])
def test_symlinked_source_metadata_refuses_cohort_add(store: LabStore, location: str) -> None:
    source_id = _sample(store, 1)
    input_id = _sha(101)
    original = (store.sources_dir / f"{source_id}.json" if location == "source"
                else store.inputs_dir / input_id / "manifest.json")
    surrogate = store.root / f"surrogate-{location}.json"
    surrogate.write_bytes(original.read_bytes())
    original.unlink()
    original.symlink_to(surrogate)
    with pytest.raises(LabProblem, match="TBX-LAB-020"):
        add_sample(store, "healthy", source_id)
    assert read_roster(store, "healthy") == ()


def test_latest_cohort_add_does_not_read_symlinked_source(store: LabStore) -> None:
    source_id = _sample(store, 1)
    original = store.sources_dir / f"{source_id}.json"
    surrogate = store.root / "surrogate-source.json"
    surrogate.write_bytes(original.read_bytes())
    original.unlink()
    original.symlink_to(surrogate)
    with pytest.raises(LabProblem, match="TBX-LAB-020"):
        add_sample(store, "healthy", "latest")
    assert read_roster(store, "healthy") == ()


@pytest.mark.parametrize("bad_input,field", [(None, "inputs"), ({"used_utc": 42}, "used_utc")])
def test_latest_cohort_add_rejects_malformed_source_inputs(store: LabStore, bad_input: object, field: str) -> None:
    source_id = _sample(store, 1)
    path = store.sources_dir / f"{source_id}.json"
    record = json.loads(path.read_text())
    record["inputs"].append(bad_input)
    path.write_text(json.dumps(record))
    with pytest.raises(LabProblem, match="TBX-LAB-020") as error:
        add_sample(store, "healthy", "latest")
    assert error.value.data == {"source_id": source_id, "field": field}
    assert read_roster(store, "healthy") == ()


@pytest.mark.parametrize("first_time,second_time,expected_index", [
    ("2026-10-17T00:00:00Z", "2026-10-16T00:00:00Z", 1),
    ("2026-10-16T00:00:00Z", "2026-10-16T00:00:00Z", 2),
])
def test_latest_cohort_add_matches_well_formed_sample_resolution(
    store: LabStore, first_time: str, second_time: str, expected_index: int,
) -> None:
    first, second = _sample(store, 1), _sample(store, 2)
    for source_id, used_utc in ((first, first_time), (second, second_time)):
        path = store.sources_dir / f"{source_id}.json"
        record = json.loads(path.read_text())
        record["inputs"][0]["used_utc"] = used_utc
        path.write_text(json.dumps(record))
    expected = _sha(expected_index)
    assert resolve_sample(store, "latest") == expected
    assert add_sample(store, "healthy", "latest")
    assert read_roster(store, "healthy") == (expected,)


def test_pon_input_selection_uses_only_verified_full_aligned_inputs(store: LabStore) -> None:
    aligned = _sample(store, 1)
    prealigned = _sample(store, 2, aligned=False)
    members = (cohorts.member_from_store(store, prealigned), cohorts.member_from_store(store, aligned))
    selected = cohorts.pon_inputs_for_members(store, members)
    assert tuple(item.source_id for item in selected) == (aligned,)
    assert selected[0].bam_path == store.inputs_dir / _sha(101) / "aligned.bam"
    assert selected[0].reference_sha256 == _sha(900)


@pytest.mark.parametrize("location", ["sources_dir", "input_dir"])
def test_cohort_add_refuses_symlinked_metadata_directory(store: LabStore, location: str) -> None:
    source_id = _sample(store, 1)
    original = store.sources_dir if location == "sources_dir" else store.inputs_dir / _sha(101)
    surrogate = store.root / f"surrogate-{location}"
    original.rename(surrogate)
    original.symlink_to(surrogate, target_is_directory=True)
    with pytest.raises(LabProblem, match="TBX-LAB-020"):
        add_sample(store, "healthy", source_id)
    assert read_roster(store, "healthy") == ()


@pytest.mark.parametrize("location", ["source", "manifest"])
def test_symlinked_source_metadata_preserves_current_generation(store: LabStore, location: str) -> None:
    source_id = _sample(store, 1)
    add_sample(store, "healthy", source_id)
    old = build_cohort(store, "healthy")
    before = store.generation()
    input_id = _sha(101)
    original = (store.sources_dir / f"{source_id}.json" if location == "source"
                else store.inputs_dir / input_id / "manifest.json")
    surrogate = store.root / f"surrogate-{location}.json"
    surrogate.write_bytes(original.read_bytes())
    original.unlink()
    original.symlink_to(surrogate)
    with pytest.raises(LabProblem, match="TBX-LAB-020"):
        build_cohort(store, "healthy")
    assert read_current(store, "healthy") == old
    assert store.generation() == before
    assert not (store.lab / "cohorts" / "healthy" / "generations" / "2").exists()


def test_cohort_listing_keeps_healthy_entry_after_one_stat_error(store: LabStore, monkeypatch) -> None:
    from pathlib import Path
    from traceback_lab.cohorts import list_cohorts

    source_id = _sample(store, 1)
    add_sample(store, "healthy", source_id)
    healthy = build_cohort(store, "healthy")
    (store.lab / "cohorts" / "broken").mkdir()
    original = Path.is_dir

    def fail_one(directory: Path) -> bool:
        if directory.name == "broken":
            raise OSError("synthetic stat failure")
        return original(directory)

    monkeypatch.setattr(Path, "is_dir", fail_one)
    entries = list_cohorts(store)
    assert [entry.cohort_id for entry in entries] == ["broken", "healthy"]
    assert entries[0].problem is not None
    assert entries[0].problem.data["field"] == "cohort directory"
    assert entries[1].generation == healthy


def test_symlinked_cohort_root_has_typed_problem(store: LabStore, tmp_path) -> None:
    from traceback_lab.cohorts import list_cohorts

    outside = tmp_path / "outside-root"
    outside.mkdir()
    (store.lab / "cohorts").parent.mkdir(parents=True)
    (store.lab / "cohorts").symlink_to(outside, target_is_directory=True)
    with pytest.raises(LabProblem, match="TBX-LAB-022") as error:
        list_cohorts(store)
    assert error.value.data["field"] == "cohort root"


def test_finder_metadata_does_not_block_next_generation(store: LabStore) -> None:
    source_id = _sample(store, 1)
    add_sample(store, "healthy", source_id)
    build_cohort(store, "healthy")
    (store.lab / "cohorts" / "healthy" / "generations" / ".DS_Store").write_bytes(b"finder")
    assert build_cohort(store, "healthy").version == 2
