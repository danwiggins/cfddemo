"""D3/AC3: a live library edit changes presentation, never computation."""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from traceback_lab import registry, views
from traceback_lab.store import LabStore
from traceback_lab.web import api as api_module
from traceback_runner import cli


def test_disk_footnote_reload_preserves_results_and_does_not_execute_workers(
    lab_world, shared_mmi, tmp_path, monkeypatch, capsys,
) -> None:
    monkeypatch.setattr(registry, "ALGORITHMS", (
        "tests.lab.fake_algorithms:SUMMARY", "tests.lab.fake_algorithms:COUNTER",
    ))
    marker = tmp_path / "executed.txt"
    monkeypatch.setenv("LAB_TEST_MARKER", str(marker))
    library_path = tmp_path / "library.yaml"
    library = yaml.safe_load(api_module.LIBRARY_FILE.read_text(encoding="utf-8"))
    entry = next(row for row in library["entries"]
                 if row["algorithm_id"] == "fragment-size" and row["headline"])
    original_footnote = entry["performance"]["value"]
    library_path.write_text(yaml.safe_dump(library), encoding="utf-8")
    monkeypatch.setattr(api_module, "LIBRARY_FILE", library_path)

    def run(source: str) -> dict:
        code = cli.main([
            "lab", "run", source, "--root", str(lab_world["root"]),
            "--mmi", str(shared_mmi), "--no-controls", "--no-downsample", "--json",
        ])
        captured = capsys.readouterr()
        assert code == 0, captured.out + captured.err
        return json.loads(captured.out)["data"]

    first = run(str(lab_world["chunks"]))
    assert {step["algorithm_id"] for step in first["steps"]} == {"read-summary", "fragment-size"}
    assert all(not step["cached"] for step in first["steps"])
    executions = marker.read_bytes()
    assert executions.splitlines() == [b"read-summary", b"fragment-size"]
    store = LabStore(lab_world["root"])

    def persisted() -> dict[Path, bytes]:
        return {path.relative_to(store.results_dir): path.read_bytes()
                for path in store.results_dir.rglob("*")
                if path.is_file() and (path.name == "result.json" or "artifacts" in path.parts)}

    def attempts() -> set[Path]:
        return {path.relative_to(store.results_dir)
                for directory in store.results_dir.rglob("attempts")
                for path in directory.iterdir() if path.is_dir()}

    def identities() -> dict[str, str]:
        return {name: envelope.provenance.parameters_sha256 for name, envelope in
                views.selected_results(store, first["sample"]["input_id"]).items()}

    def selections() -> dict:
        return {name: store.selected_attempt(first["sample"]["input_id"], name, identity)
                for name, identity in identities().items()}

    before = persisted()
    before_attempts = attempts()
    before_identities = identities()
    assert set(before_identities) == {"read-summary", "fragment-size"}
    before_selections = selections()
    assert before and len(before_attempts) == 2
    assert all(path is not None for path in before_selections.values())
    api = api_module.LabApi(store)
    sample_id = first["sample"]["id12"]

    def footnote() -> str:
        response = api.sample(sample_id)
        assert response["found"]
        assert response["library_health"]["error"] is None
        return next(row["footnote"] for row in response["catalog"]
                    if row["algorithm_id"] == "fragment-size")

    assert footnote() == original_footnote
    edited_footnote = original_footnote + " D3 acceptance: revised evidence wording."
    entry["performance"]["value"] = edited_footnote
    # Size changes guarantee reload detection even on coarse-mtime filesystems.
    library_path.write_text(yaml.safe_dump(library), encoding="utf-8")
    assert footnote() == edited_footnote  # Same API/holder; no cache clearing or restart.
    assert persisted() == before
    assert attempts() == before_attempts
    assert identities() == before_identities
    assert selections() == before_selections
    assert marker.read_bytes() == executions

    again = run("latest")
    assert all(step["cached"] for step in again["steps"])
    assert again["sample"] == first["sample"]
    assert identities() == before_identities
    assert persisted() == before
    assert attempts() == before_attempts
    assert selections() == before_selections
    assert marker.read_bytes() == executions
    assert footnote() == edited_footnote
