"""An explicit cohort must have a built panel even in a read-only run preview."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from traceback_lab.cohort_contracts import CohortGeneration, CohortMember, CohortPon
from traceback_lab.cohorts import add_sample, build_cohort
from traceback_lab.store import LabStore, new_run_id
from traceback_runner import cli as signed_cli
from traceback_runner.serialization import canonical_json_bytes
from tests.lab.test_cohort_store import _sample


def _snapshot(root: Path) -> dict[str, bytes | None]:
    return {
        str(path.relative_to(root)): path.read_bytes() if path.is_file() else None
        for path in root.rglob("*")
    }


def _dry_run(
    capsys: pytest.CaptureFixture[str], store: LabStore, source_id: str, *, only: str | None = None,
) -> tuple[int, dict]:
    argv = [
        "lab", "run", source_id[:12], "--cohort", "healthy", "--root", str(store.root),
        "--dry-run", "--json",
    ]
    if only is not None:
        argv.extend(("--only", only))
    code = signed_cli.main(argv)
    return code, json.loads(capsys.readouterr().out)


def _publish_built_panel(store: LabStore) -> None:
    sources = tuple(f"{index:064x}" for index in range(1, 11))
    loo = tuple((source, f"{index:064x}") for index, source in enumerate(sources, 101))
    members = tuple(CohortMember(
        source_id=source, input_id=f"{index:064x}", aligner_identity="minimap2 2.28; samtools 1.20",
        results=(), pon_asset_sha256=loo[index - 1][1],
    ) for index, source in enumerate(sources, 1))
    build = CohortPon(
        source_ids=sources, loo_asset_sha256s=loo, reference_sha256="b" * 64,
        aligner_identity="minimap2 2.28; samtools 1.20", grid_sha256="c" * 64,
        counting_parameters_sha256="d" * 64, toolchain_lock_sha256="e" * 64,
        builder_source_sha256="f" * 64, builder_sha256="1" * 64, validator_sha256="2" * 64,
        gc_sha256="3" * 64, map_sha256="4" * 64, centromere_sha256="5" * 64,
    )
    generation = CohortGeneration(
        cohort_id="healthy", version=1, members=members, pon_n=10,
        pon_asset_sha256="a" * 64, pon_build=build,
    )
    directory = store.lab / "cohorts" / "healthy"
    generation_dir = directory / "generations" / "1"
    generation_dir.mkdir(parents=True)
    (generation_dir / "generation.json").write_bytes(
        canonical_json_bytes(generation.model_dump(mode="json", by_alias=True))
    )
    (directory / "current.json").write_bytes(canonical_json_bytes({"version": 1}))


@pytest.mark.parametrize("state", ["missing", "unbuilt", "built"])
@pytest.mark.parametrize("only", [None, "bam-qc"])
def test_explicit_cohort_preview_checks_built_status_without_writes_or_lock(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], state: str, only: str | None,
) -> None:
    store = LabStore(tmp_path)
    source_id = _sample(store, 20)
    # Stored dry-runs validate the same complete reference binding as real runs.
    source = json.loads((store.sources_dir / f"{source_id}.json").read_text())
    manifest_path = store.inputs_dir / source["inputs"][0]["input_id"] / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["reference_id"] = "synthetic"
    manifest_path.write_text(json.dumps(manifest))
    if state == "unbuilt":
        add_sample(store, "healthy", source_id)
        build_cohort(store, "healthy")
    elif state == "built":
        _publish_built_panel(store)

    with store.writer("cohort build", new_run_id()):
        before = _snapshot(tmp_path)
        generation = store.generation()
        code, payload = _dry_run(capsys, store, source_id, only=only)
        assert _snapshot(tmp_path) == before
        assert store.generation() == generation

    if state == "built":
        assert code == 0
        if only == "bam-qc":
            assert all(row["algorithm_id"] != "copy-number" for row in payload["data"]["plan"])
        assert payload["data"]["cohort"] == {
            "id": "healthy", "version": "healthy:1", "panel": "built",
            "compatibility": "during run",
        }
    else:
        assert code == 7  # Existing explicit-panel refusal uses TBX-LAB-020.
        assert payload["data"]["code"] == "TBX-LAB-020"
        assert payload["data"]["field"] == "panel of normals"


def test_new_chunk_preview_defers_compatibility_until_alignment(
    lab_world: dict, capsys: pytest.CaptureFixture[str],
) -> None:
    store = LabStore(lab_world["root"])
    _publish_built_panel(store)
    before = _snapshot(store.root)
    code = signed_cli.main([
        "lab", "run", str(lab_world["chunks"]), "--cohort", "healthy",
        "--root", str(store.root), "--dry-run", "--json",
    ])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["data"]["cohort"]["compatibility"] == "after alignment"
    assert _snapshot(store.root) == before


def test_reused_alignment_preview_checks_cohort_during_run(
    lab_world: dict, capsys: pytest.CaptureFixture[str],
) -> None:
    store = LabStore(lab_world["root"])
    chunks = str(lab_world["chunks"])
    assert signed_cli.main([
        "lab", "run", chunks, "--root", str(store.root), "--only", "bam-qc",
        "--no-downsample", "--no-controls",
    ]) == 0
    capsys.readouterr()
    _publish_built_panel(store)
    before = _snapshot(store.root)
    code = signed_cli.main([
        "lab", "run", chunks, "--cohort", "healthy", "--root", str(store.root),
        "--only", "bam-qc", "--dry-run", "--json",
    ])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["data"]["alignment"] == "reused"
    assert payload["data"]["cohort"]["compatibility"] == "during run"
    assert signed_cli.main([
        "lab", "run", chunks, "--cohort", "healthy", "--root", str(store.root),
        "--only", "bam-qc", "--dry-run",
    ]) == 0
    human = capsys.readouterr().out
    assert "ALIGN     reused" in human
    assert "COHORT    healthy:1 (built; compatibility checked during run)" in human
    assert _snapshot(store.root) == before


def test_example_preview_rejects_explicit_cohort_before_generating_files(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    code = signed_cli.main([
        "lab", "run", "--example", "--cohort", "healthy",
        "--root", str(tmp_path), "--dry-run", "--json",
    ])
    payload = json.loads(capsys.readouterr().out)
    assert code == 3 and payload["data"]["code"] == "TBX-LAB-021"
    assert _snapshot(tmp_path) == {}
