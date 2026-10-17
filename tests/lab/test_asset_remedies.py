"""Algorithm-specific setup instructions for skipped Lab results."""

from __future__ import annotations

from pathlib import Path
import shlex
from types import SimpleNamespace

from traceback_lab.run_command import _skip_remedy, _summary
from traceback_lab.runner import StepResult
from traceback_lab.store import LabStore


def test_missing_dependencies_name_the_algorithm_and_asset(tmp_path: Path) -> None:
    root = tmp_path / "store"
    expected = {
        ("pmd-score", "missing_asset"): ("Zhou 2018", "traceback lab asset add pmd"),
        ("copy-number", "missing_asset"): ("ichorCNA", "method-asset register --from-toolchain copy-number"),
        ("cell-origin", "missing_asset"): ("Loyfer", "method-asset register --from-dir LOYFER_DIR"),
        ("copy-number", "missing_toolchain"): ("ichor", "toolchain install ichor"),
    }
    for (algorithm, reason), (asset, command) in expected.items():
        remedy = _skip_remedy(algorithm, reason, root)
        assert remedy is not None
        assert asset in remedy["asset"]
        assert command in remedy["command"]
    assert _skip_remedy("unknown", "missing_asset", root) is None
    assert _skip_remedy("cell-origin", "missing_toolchain", root) is None


def test_human_and_json_remediation_agree_and_deduplicate(tmp_path: Path) -> None:
    store = LabStore(tmp_path / "store")
    aligned = SimpleNamespace(
        manifest={},
        source_id="a" * 64,
        input_id="b" * 64,
    )
    skipped = [
        StepResult("pmd-score", "skipped", False, "c" * 64, skip_reason="missing_asset"),
        StepResult("copy-number", "skipped", False, "d" * 64, skip_reason="missing_asset"),
        StepResult("cell-origin", "skipped", False, "e" * 64, skip_reason="missing_asset"),
    ]
    code, payload = _summary(
        store, "r-20261016-1200-0000", aligned, "a" * 12,
        None, skipped + skipped[:1], [], 1.0,
    )
    assert code == 0
    remedies = payload["data"]["remediations"]
    assert len(remedies) == 3
    by_algorithm = {row["algorithm_id"]: row for row in remedies}
    assert "lab asset add pmd" in by_algorithm["pmd-score"]["command"]
    assert "copy-number" not in by_algorithm["pmd-score"]["command"]
    lines = payload["_human"]["lines"]
    for remedy in remedies:
        assert any(
            line.startswith(f"SKIP    {remedy['algorithm_id']} skipped")
            and remedy["asset"] in line
            and remedy["command"] in line
            for line in lines
        )

def test_remediation_quotes_operator_root_and_preserves_json_argv(tmp_path: Path) -> None:
    root = tmp_path / "root with a'quote"
    for algorithm, reason in (
        ("pmd-score", "missing_asset"),
        ("copy-number", "missing_asset"),
        ("cell-origin", "missing_asset"),
    ):
        remedy = _skip_remedy(algorithm, reason, root)
        assert remedy is not None
        assert shlex.split(remedy["command"]) == remedy["argv"]
        assert remedy["argv"][-1] == str(root)
    pmd = _skip_remedy("pmd-score", "missing_asset", root)
    assert pmd is not None
    assert "YOUR_LOCAL_PMD_coordinates_hg38.bed.gz" in pmd["argv"]
