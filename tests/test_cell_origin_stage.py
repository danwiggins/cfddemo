"""CO3: cell origin as a signed local record (`run --analysis cell-origin`).

Every input is generated: a chr1 FASTA, a modBAM of planted U/M fragments,
a 3-marker mini-atlas of two contributors, and a stand-in modkit that reads
the MM/ML tags of the pre-filtered BAM.  Nothing here is real data or a real
number; every record is unqualified, local and not for clinical use.
"""

from __future__ import annotations

import hashlib
import json
import re
import stat
import sys
from pathlib import Path
from typing import Any

import pytest

import traceback_runner.analyses as analyses_module
import traceback_runner.cell_origin as cell_origin_module
from evidence_inspector.cell_origin_models import (
    LOYFER_UXM_METHOD,
    AtlasUMatrix,
    AtlasUMatrixRow,
    AtlasUValue,
    DigestArtifact,
    MarkerCountRow,
    NnlsRowScale,
    ValidationCheck,
)
from evidence_inspector.cell_origin_pipeline import validation_report
from evidence_inspector.deconvolution import bootstrap_uxm_v2, deconvolve_uxm_v2
from traceback_runner import cli
from traceback_runner.analyses import CELL_ORIGIN, FRAGMENT
from traceback_runner.cell_origin import (
    PATH_STEM,
    CellOriginAnalysis,
    CellOriginMeasurementV1,
    resolve_modbase_model,
)
from traceback_runner.cell_origin_method import default_parameters
from traceback_runner.contracts import JobState
from traceback_runner.fixtures import FAKE_MODKIT_SOURCE, create_cell_origin_inputs
from traceback_runner.runner import Runner
from traceback_runner.toolchain import TOOL_MISSING, PinnedTool, ToolIdentity, ToolProblem

REPO = Path(__file__).resolve().parents[1]
MEASUREMENT = f"measurements/{PATH_STEM}.json"


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _json(capsys: pytest.CaptureFixture[str], *argv: object) -> tuple[int, dict]:
    code = cli.main([*map(str, argv), "--json"])
    return code, json.loads(capsys.readouterr().out)


def _fake_modkit(directory: Path) -> PinnedTool:
    path = directory / "toolchain" / "bin" / "modkit"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!{sys.executable}\n{FAKE_MODKIT_SOURCE}", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return PinnedTool(
        path=path,
        identity=ToolIdentity(
            tool_id="modkit",
            version="0.6.4",
            platform="osx-arm64",
            lock_sha256="1" * 64,
            lock_line="https://conda.anaconda.org/bioconda/osx-arm64/x.conda#sha256:" + "2" * 64,
            package_sha256="2" * 64,
            package_binary_sha256="3" * 64,
            installed_binary_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        ),
    )


def _test_parameters(**overrides: Any):
    """The locked parameters with floors a 3-marker mini-atlas can reach."""

    def build(*, modbase_model: str | None = None):
        values = {"min_classified_fragments": 10, "min_observed_markers": 3, **overrides}
        return default_parameters(modbase_model=modbase_model).model_copy(update=values)

    return build


class Setup:
    def __init__(self, tmp_path: Path, capsys, monkeypatch, **fixture: Any) -> None:
        self.tmp_path = tmp_path
        self.capsys = capsys
        self.monkeypatch = monkeypatch
        self.inputs = create_cell_origin_inputs(tmp_path / "inputs", **fixture)
        self.tool = _fake_modkit(tmp_path)
        self.registry: dict[str, Any] = {}
        monkeypatch.setattr(analyses_module, "_REGISTRY", self.registry)
        self.use(CellOriginAnalysis(
            parameters=_test_parameters(), modkit=lambda: self.tool, platform_name="osx-arm64"
        ))

    def use(self, analysis: CellOriginAnalysis) -> CellOriginAnalysis:
        self.registry.clear()
        analyses_module.register_analysis_stages(analysis.spec)
        self.analysis = analysis
        return analysis

    def root(self, name: str = "root", *, assets: bool = True) -> Path:
        root = self.tmp_path / name
        code, payload = _json(
            self.capsys, "reference", "register", "--fasta", self.inputs.fasta_path,
            "--id", "ref", "--root", root,
        )
        assert code == 0, payload
        if assets:
            code, payload = _json(
                self.capsys, "method-asset", "register", "--from-dir", self.inputs.loyfer_dir,
                "--root", root,
            )
            assert code == 0, payload
        return root

    def run(self, root: Path, *extra: object, analyses: str = "cell-origin") -> tuple[int, dict]:
        return _json(
            self.capsys, "run", self.inputs.bam_path, "--reference", "ref", "--root", root,
            "--analysis", analyses, *extra,
        )


def _row(payload: dict, analysis: str = CELL_ORIGIN) -> dict:
    return {row["analysis"]: row for row in payload["data"]["analyses"]}[analysis]


def _measurement(root: Path, payload: dict) -> tuple[bytes, CellOriginMeasurementV1]:
    record = root / "records" / _row(payload)["record_id"]
    content = (record / MEASUREMENT).read_bytes()
    return content, CellOriginMeasurementV1.model_validate_json(content)


@pytest.fixture
def setup(tmp_path: Path, capsys, monkeypatch) -> Setup:
    return Setup(tmp_path, capsys, monkeypatch)


# --------------------------------------------------------------------------
# Registration
# --------------------------------------------------------------------------


def test_the_cli_registers_the_schema_and_the_stages() -> None:
    from traceback_runner.measurement_schemas import measurement_schema

    spec = measurement_schema("traceback.cell-origin-measurement.v1")
    assert spec is cell_origin_module.MEASUREMENT_SCHEMA
    assert spec.measurement_path == "measurements/cell-origin.v1.json"
    assert spec.chart_path == "charts/cell-origin.v1.json"
    assert spec.catalog.method_slug == "cell-origin-loyfer-uxm"
    assert cell_origin_module.DEFAULT_ANALYSIS.spec.takes_root


# --------------------------------------------------------------------------
# The planted fixture end to end: counts, mixture, record, verify, import
# --------------------------------------------------------------------------


def test_planted_fragments_are_classified_and_counted(setup: Setup) -> None:
    root = setup.root()
    code, payload = setup.run(root, "--modbase-model", "model-x")
    assert code == 0, payload
    _, measurement = _measurement(root, payload)
    planted = setup.inputs.planted
    assert [(row.u, row.m, row.x) for row in measurement.marker_counts] == [
        (u, m, 0) for u, m in planted
    ]
    denominators = measurement.denominators
    marker_reads = sum(u + m for u, m in planted)
    assert denominators.records_scanned == marker_reads + 4 + 10
    excluded = denominators.alignment_exclusions
    assert (excluded.duplicate, excluded.secondary, excluded.qc_failure,
            excluded.low_mapping_quality, excluded.unmapped, excluded.supplementary) == (
        1, 1, 1, 1, 0, 0
    )
    assert denominators.eligible_alignments == marker_reads + 10
    assert denominators.alignments_with_mod_tags == marker_reads + 10
    assert denominators.classified_fragments == marker_reads
    assert denominators.mixed_fragments == 0
    calls = measurement.cpg_calls
    assert (calls.extracted, calls.dropped_at_load, calls.inspected) == (
        marker_reads * 10, 0, marker_reads * 10
    )
    assert (denominators.observed_markers, denominators.registered_markers) == (3, 3)
    assert measurement.modbase_model.model_dump() == {
        "id": "model-x", "source": "operator_declared"
    }
    assert measurement.solver.converged is True
    assert measurement.solver.row_scale == "sqrt_count"


@pytest.mark.parametrize("mixture", [(0.7, 0.3), (0.2, 0.8)])
def test_a_two_contributor_mixture_is_recovered(tmp_path, capsys, monkeypatch, mixture) -> None:
    setup = Setup(tmp_path, capsys, monkeypatch, mixture=mixture, reads_per_marker=200)
    root = setup.root()
    code, payload = setup.run(root, "--modbase-model", "model-x")
    assert code == 0, payload
    _, measurement = _measurement(root, payload)
    fractions = {item.contributor_id: item.fraction for item in measurement.estimates}
    assert fractions == pytest.approx({"TypeA": mixture[0], "TypeB": mixture[1]}, abs=1e-6)
    assert [item.contributor_id for item in measurement.estimates] == ["TypeA", "TypeB"]


def test_the_measurement_is_byte_identical_across_two_fresh_roots(setup: Setup) -> None:
    first = setup.root("first")
    second = setup.root("second")
    contents = []
    for root in (first, second):
        code, payload = setup.run(root, "--modbase-model", "model-x")
        assert code == 0, payload
        contents.append(_measurement(root, payload)[0])
    assert contents[0] == contents[1]


def test_the_record_verifies_imports_and_binds_its_method_store(setup: Setup) -> None:
    from evidence_inspector.method_registry import method_definition_sha256

    root = setup.root()
    code, payload = setup.run(root, "--modbase-model", "model-x", analyses="fragment,cell-origin")
    assert code == 0, payload
    row = _row(payload)
    assert row["measurement_schema"] == MEASUREMENT
    assert _row(payload, FRAGMENT)["status"] == "ok"
    record = root / "records" / row["record_id"]
    manifest = json.loads((record / "bundle-manifest.json").read_bytes())
    from traceback_runner.references import load_reference

    definition = setup.analysis.definition(
        load_reference(root, "ref"), {"modbase_model": "model-x"}, root=root
    )
    assert manifest["method"]["method_definition_sha256"] == method_definition_sha256(definition)
    assert (root / "method-authority" / "ref" / "cell-origin-loyfer-uxm"
            / method_definition_sha256(definition)).is_dir()
    provenance = json.loads((record / "provenance.json").read_bytes())
    assert [item["role"] for item in provenance["artifacts"]] == ["analysis_bam", "tool_binary"]
    report = (record / "report.html").read_text()
    assert "declared by the operator" in report and "TypeA" not in report
    code, verified = _json(setup.capsys, "verify", record, "--trust-store",
                           root / "trust" / "development-result-trust.json")
    assert code == 0, verified
    code, imported = _json(setup.capsys, "catalog", "import", record, "--root", root)
    assert code == 0, imported
    code, again = _json(setup.capsys, "catalog", "import", record, "--root", root)
    assert code == 0, again
    # A mixed ROOT keeps every catalog command working.
    code, listed = _json(setup.capsys, "catalog", "list", "--root", root)
    assert code == 0, listed
    eligible = {row["record_id"]: row["eligible_alignments"] for row in listed["data"]["records"]}
    measurement = _measurement(root, payload)[1]
    assert eligible[row["record_id"]] == measurement.denominators.eligible_alignments
    code, exported = _json(setup.capsys, "catalog", "export", "--csv", setup.tmp_path / "x.csv",
                           "--root", root)
    assert code == 0, exported


def test_the_explorer_artifact_binds_the_registered_atlas(setup: Setup) -> None:
    """CO4: the imported record's E05 key carries the exact registered atlas."""

    from traceback_runner.local_catalog import explorer_paths
    from traceback_runner.references import LOYFER_DIRECTORY_FILES, AssetKind
    from traceback_runner.web.explorer import ExplorerArtifactRecord

    root = setup.root()
    code, payload = setup.run(root, "--modbase-model", "model-x", analyses="fragment,cell-origin")
    assert code == 0, payload
    keys = {}
    for analysis in (FRAGMENT, CELL_ORIGIN):
        record = root / "records" / _row(payload, analysis)["record_id"]
        code, imported = _json(setup.capsys, "catalog", "import", record, "--root", root)
        assert code == 0, imported
        artifact_path, _ = explorer_paths(root, imported["data"]["result_id"])
        artifact = ExplorerArtifactRecord.model_validate_json(artifact_path.read_bytes())
        keys[analysis] = artifact.result_view_request.sources[0].record.compatibility_key
    file_name, atlas_id = LOYFER_DIRECTORY_FILES[AssetKind.LOYFER_ATLAS]
    atlas = keys[CELL_ORIGIN].atlas_asset
    assert atlas is not None
    assert atlas.asset_id == atlas_id
    assert atlas.content_sha256 == hashlib.sha256(
        (setup.inputs.loyfer_dir / file_name).read_bytes()
    ).hexdigest()
    assert keys[CELL_ORIGIN].reference_asset != atlas
    assert keys[CELL_ORIGIN].grid_asset is None and keys[CELL_ORIGIN].panel_asset is None
    # Fragment length binds no grid, atlas or panel, as before.
    fragment = keys[FRAGMENT]
    assert (fragment.grid_asset, fragment.atlas_asset, fragment.panel_asset) == (None,) * 3


def test_the_atlas_role_refuses_a_definition_that_does_not_bind_it(setup: Setup) -> None:
    from traceback_runner.local_catalog import _compatibility_assets
    from traceback_runner.references import LOYFER_DIRECTORY_FILES, AssetKind, load_reference

    root = setup.root()
    definition = setup.analysis.definition(
        load_reference(root, "ref"), {"modbase_model": "model-x"}, root=root
    )
    atlas_id = LOYFER_DIRECTORY_FILES[AssetKind.LOYFER_ATLAS][1]
    found = _compatibility_assets(definition, (("atlas_asset", atlas_id),))
    assert found["atlas_asset"].asset_id == atlas_id
    with pytest.raises(ValueError, match="does not bind its atlas_asset"):
        _compatibility_assets(definition, (("atlas_asset", "asset_not_in_definition"),))
    with pytest.raises(ValueError, match="does not bind its atlas_asset"):
        _compatibility_assets(definition, (("atlas_asset", definition.assets[0].asset_id),))
    assert _compatibility_assets(definition, ()) == {}


def test_a_header_declared_model_needs_no_flag(tmp_path, capsys, monkeypatch) -> None:
    setup = Setup(tmp_path, capsys, monkeypatch, header_model="model-h")
    root = setup.root()
    code, payload = setup.run(root)
    assert code == 0, payload
    _, measurement = _measurement(root, payload)
    assert measurement.modbase_model.model_dump() == {"id": "model-h", "source": "header"}


# --------------------------------------------------------------------------
# Guards (each mutation-checked; see the PR)
# --------------------------------------------------------------------------


def test_meth_001_absent_modification_tags_refuse(tmp_path, capsys, monkeypatch) -> None:
    setup = Setup(tmp_path, capsys, monkeypatch, modification_tags=False)
    root = setup.root()
    code, payload = setup.run(root, "--modbase-model", "model-x")
    assert code == cli.ExitCode.BLOCKED, payload
    assert _row(payload)["code"] == "TBX-METH-001"
    assert not list((root / "records").glob("record-*")) if (root / "records").exists() else True


def test_mn_is_optional(tmp_path, capsys, monkeypatch) -> None:
    setup = Setup(tmp_path, capsys, monkeypatch, mn_tag=False)
    root = setup.root()
    code, payload = setup.run(root, "--modbase-model", "model-x")
    assert code == 0, payload


def test_meth_001_contradictory_tags_refuse(tmp_path: Path) -> None:
    import array

    import pysam

    from traceback_runner.cell_origin import modification_refusal, sample_modification_tags

    inputs = create_cell_origin_inputs(tmp_path / "inputs")
    broken = tmp_path / "broken.bam"
    with pysam.AlignmentFile(str(inputs.bam_path), "rb") as reader, pysam.AlignmentFile(
        str(broken), "wb", template=reader
    ) as writer:
        for record in reader.fetch(until_eof=True):
            record.set_tag("ML", array.array("B", [5]))  # one value for ten calls
            writer.write(record)
    tagged, invalid = sample_modification_tags(broken)
    assert tagged and invalid
    assert modification_refusal(tagged, invalid).code == "TBX-METH-001"
    assert modification_refusal(*sample_modification_tags(inputs.bam_path)) is None


def test_meth_007_a_claim_word_in_the_declaration_refuses_terminally(setup: Setup) -> None:
    root = setup.root()
    code, payload = setup.run(root, "--modbase-model", "healthy")
    assert code == cli.ExitCode.BLOCKED, payload
    row = _row(payload)
    assert row["code"] == "TBX-METH-007" and "export_boundary" in row["cause"]


@pytest.mark.parametrize(
    ("fixture", "flag", "expected"),
    [
        ({"mn_tag": False}, "model-x", ("TBX-MOD-001", "ready")),
        ({}, None, ("TBX-MOD-001", "blocked")),
        ({"header_model": "model-h"}, "model-b", ("TBX-METH-002", "blocked")),
        ({"header_model": "model-h"}, None, ("TBX-MOD-001", "ready")),
        ({"modification_tags": False}, "model-x", ("TBX-METH-001", "blocked")),
    ],
)
def test_preflight_readiness_agrees_with_the_stage(
    tmp_path, capsys, monkeypatch, fixture, flag, expected
) -> None:
    setup = Setup(tmp_path, capsys, monkeypatch, **fixture)
    root = setup.root()
    extra = ("--modbase-model", flag) if flag else ()
    _, payload = _json(
        setup.capsys, "preflight", setup.inputs.bam_path, "--reference", "ref", "--root", root,
        "--analysis", "cell-origin", *extra,
    )
    rows = [(row["code"], row["outcome"]) for row in payload["data"]["analyses"][0]["checks"]]
    assert rows[0] == ("TBX-BAM-001", "ready")
    assert rows[1] == expected
    if flag is None and "header_model" not in fixture:
        assert payload["data"]["analyses"][0]["checks"][1]["detail"] == (
            "BLOCKED for cell origin: pass `--modbase-model`"
        )


@pytest.mark.parametrize("index", ["matching", "contradictory"])
def test_readiness_includes_the_shared_bam_checks(
    tmp_path, capsys, monkeypatch, index
) -> None:
    from traceback_runner.analyses import ReadinessRow
    from traceback_runner.fixtures import create_local_golden_path_inputs

    setup = Setup(tmp_path, capsys, monkeypatch)
    monkeypatch.setattr(
        cli, "_modkit_readiness", lambda: ReadinessRow("TBX-TOOL-001", "ready", "verified")
    )
    root = setup.root()
    if index == "contradictory":
        other = create_local_golden_path_inputs(tmp_path / "other", reads=10)
        setup.inputs.index_path.write_bytes(other.index_path.read_bytes())
    _, payload = _json(
        setup.capsys, "preflight", setup.inputs.bam_path, "--reference", "ref", "--root", root,
        "--analysis", "cell-origin", "--modbase-model", "model-x",
    )
    result = payload["data"]["analyses"][0]
    assert result["readiness"] == ("ready" if index == "matching" else "blocked"), result
    if index == "contradictory":
        assert result["checks"][0]["outcome"] == "blocked"
        assert result["checks"][0]["code"].startswith("TBX-BAM-")


def test_meth_002_an_unusable_header_model_id_refuses() -> None:
    with pytest.raises(cli.LocalStageRefusal) as refused:
        resolve_modbase_model(("model/path",), None)
    assert refused.value.code == "TBX-METH-002"


@pytest.mark.parametrize("when", ["after_header", "before_header"])
def test_a_killed_modkit_is_retryable(setup: Setup, when: str) -> None:
    killed = setup.tmp_path / "killed" / "modkit"
    killed.parent.mkdir()
    header = (
        "print('\\t'.join(('read_id', 'ref_position', 'chrom', 'mod_strand', "
        "'modified_primary_base', 'fail', 'call_code', 'call_prob')), flush=True)\n"
        if when == "after_header" else ""
    )
    killed.write_text(
        f"#!{sys.executable}\nimport os, signal\n" + header
        + "os.kill(os.getpid(), signal.SIGKILL)\n"
    )
    killed.chmod(0o755)
    tool = PinnedTool(path=killed, identity=setup.tool.identity.model_copy(
        update={"installed_binary_sha256": hashlib.sha256(killed.read_bytes()).hexdigest()}
    ))
    setup.use(CellOriginAnalysis(
        parameters=_test_parameters(), modkit=lambda: tool, platform_name="osx-arm64"
    ))
    root = setup.root()
    code, payload = setup.run(root, "--modbase-model", "model-x")
    assert code == cli.ExitCode.RETRYABLE_FAILURE, payload
    assert _row(payload)["code"] == "TBX-JOB-001"


def test_a_modkit_that_exits_cleanly_with_a_bad_header_stays_terminal(setup: Setup) -> None:
    bad = setup.tmp_path / "bad" / "modkit"
    bad.parent.mkdir()
    bad.write_text(f"#!{sys.executable}\nprint('not\\ta\\tmodkit\\textract', flush=True)\n")
    bad.chmod(0o755)
    tool = PinnedTool(path=bad, identity=setup.tool.identity.model_copy(
        update={"installed_binary_sha256": hashlib.sha256(bad.read_bytes()).hexdigest()}
    ))
    setup.use(CellOriginAnalysis(
        parameters=_test_parameters(), modkit=lambda: tool, platform_name="osx-arm64"
    ))
    root = setup.root()
    code, payload = setup.run(root, "--modbase-model", "model-x")
    assert code == cli.ExitCode.BLOCKED, payload
    assert _row(payload)["code"] == "TBX-METH-001"


def test_an_interrupt_during_the_exit_grace_wait_kills_modkit(tmp_path, monkeypatch) -> None:
    import subprocess

    import evidence_inspector.cell_origin_pipeline as pipeline

    inputs = create_cell_origin_inputs(tmp_path / "inputs")
    script = tmp_path / "hang" / "modkit"
    script.parent.mkdir()
    script.write_text(
        f"#!{sys.executable}\nimport time\nprint('not\\ta\\theader', flush=True)\n"
        "time.sleep(60)\n"
    )
    script.chmod(0o755)
    tool = PinnedTool(path=script, identity=_fake_modkit(tmp_path).identity.model_copy(
        update={"installed_binary_sha256": hashlib.sha256(script.read_bytes()).hexdigest()}
    ))
    started: list[subprocess.Popen] = []
    real_exec = pipeline.exec_pinned

    def exec_and_keep(*args: Any, **kwargs: Any):
        process = real_exec(*args, **kwargs)
        started.append(process)
        return process

    real_wait = subprocess.Popen.wait

    def interrupted_wait(self, timeout=None):
        if timeout == pipeline.MODKIT_EXIT_GRACE_SECONDS:
            raise KeyboardInterrupt
        return real_wait(self, timeout=timeout)

    monkeypatch.setattr(pipeline, "exec_pinned", exec_and_keep)
    monkeypatch.setattr(subprocess.Popen, "wait", interrupted_wait)
    loyfer = inputs.loyfer_dir
    config = pipeline.PipelineConfig(
        marker_bed=loyfer / "Regions.U250.l4.hg38.bed",
        marker_metadata=loyfer / "Markers.U250.hg38.tsv",
        atlas_u_matrix=loyfer / "Atlas.U250.l4.hg38.full.tsv",
        output_path=tmp_path / "unused.json",
        aligned_modbam=inputs.bam_path,
        reference_fasta=inputs.fasta_path,
        job_directory=tmp_path / "job",
    )
    with pytest.raises(KeyboardInterrupt):
        pipeline._extract_modbam(config, modkit=tool)
    assert started and started[0].poll() is not None  # killed and reaped


def test_meth_002_without_a_declared_model_refuses(setup: Setup) -> None:
    root = setup.root()
    code, payload = setup.run(root)
    assert code == cli.ExitCode.BLOCKED, payload
    assert _row(payload)["code"] == "TBX-METH-002"


def test_meth_002_a_declaration_contradicting_the_header_refuses() -> None:
    assert resolve_modbase_model(("a",), "a").source == "header"
    assert resolve_modbase_model((), "b").source == "operator_declared"
    for models, declared in (
        (("a",), "b"), ((), None), (("a", "b"), None), (("a", "b"), "a")
    ):
        with pytest.raises(cli.LocalStageRefusal) as refused:
            resolve_modbase_model(models, declared)
        assert refused.value.code == "TBX-METH-002"


def test_meth_003_region_contigs_must_be_registered(setup: Setup) -> None:
    from traceback_runner.cell_origin import contig_refusal
    from traceback_runner.references import load_reference

    root = setup.root()
    registered = load_reference(root, "ref").registered
    assert contig_refusal({"chr1"}, registered) is None
    refusal = contig_refusal({"chr1", "chr2", "chrX"}, registered)
    assert refusal.code == "TBX-METH-003" and "chr2" in refusal.cause
    code, payload = _json(
        setup.capsys, "preflight", setup.inputs.bam_path, "--reference", "ref", "--root", root,
        "--analysis", "cell-origin", "--modbase-model", "model-x",
    )
    checks = {row["code"]: row for row in payload["data"]["analyses"][0]["checks"]}
    assert checks["TBX-METH-003"]["outcome"] == "ready"


def test_preflight_names_unregistered_assets(setup: Setup) -> None:
    root = setup.root(assets=False)
    code, payload = _json(
        setup.capsys, "preflight", setup.inputs.bam_path, "--reference", "ref", "--root", root,
        "--analysis", "cell-origin", "--modbase-model", "model-x",
    )
    rows = [row for row in payload["data"]["analyses"][0]["checks"]
            if row["code"] == "TBX-ASSET-004"]
    assert len(rows) == 3 and all(row["outcome"] == "not_set_up" for row in rows)
    assert "method-asset register --from-dir" in rows[0]["detail"]


def test_meth_004_the_default_floors_refuse_the_mini_atlas(setup: Setup) -> None:
    setup.use(CellOriginAnalysis(modkit=lambda: setup.tool, platform_name="osx-arm64"))
    root = setup.root()
    code, payload = setup.run(root, "--modbase-model", "model-x")
    assert code == cli.ExitCode.BLOCKED, payload
    row = _row(payload)
    assert row["code"] == "TBX-METH-004"
    assert "300 classified fragments at 3 of 3 markers" in row["cause"]


@pytest.mark.parametrize("floor", ["min_classified_fragments", "min_observed_markers"])
def test_meth_004_each_floor_refuses_one_below(tmp_path, capsys, monkeypatch, floor) -> None:
    setup = Setup(tmp_path, capsys, monkeypatch)
    value = 301 if floor == "min_classified_fragments" else 4
    setup.use(CellOriginAnalysis(
        parameters=_test_parameters(**{floor: value}), modkit=lambda: setup.tool,
        platform_name="osx-arm64",
    ))
    root = setup.root()
    code, payload = setup.run(root, "--modbase-model", "model-x")
    assert code == cli.ExitCode.BLOCKED and _row(payload)["code"] == "TBX-METH-004"


def test_meth_005_a_call_cap_hit_refuses(setup: Setup) -> None:
    caps = default_parameters().caps.model_copy(update={"maximum_calls": 50})
    setup.use(CellOriginAnalysis(
        parameters=_test_parameters(caps=caps), modkit=lambda: setup.tool,
        platform_name="osx-arm64",
    ))
    root = setup.root()
    code, payload = setup.run(root, "--modbase-model", "model-x")
    assert code == cli.ExitCode.BLOCKED and _row(payload)["code"] == "TBX-METH-005"


def test_meth_005_a_group_cap_hit_refuses(setup: Setup) -> None:
    caps = default_parameters().caps.model_copy(update={"maximum_groups": 5})
    setup.use(CellOriginAnalysis(
        parameters=_test_parameters(caps=caps), modkit=lambda: setup.tool,
        platform_name="osx-arm64",
    ))
    root = setup.root()
    code, payload = setup.run(root, "--modbase-model", "model-x")
    assert code == cli.ExitCode.BLOCKED and _row(payload)["code"] == "TBX-METH-005"


def test_meth_005_an_oversized_group_refuses(setup: Setup) -> None:
    caps = default_parameters().caps.model_copy(update={"maximum_cpgs_per_group": 5})
    setup.use(CellOriginAnalysis(
        parameters=_test_parameters(caps=caps), modkit=lambda: setup.tool,
        platform_name="osx-arm64",
    ))
    root = setup.root()
    code, payload = setup.run(root, "--modbase-model", "model-x")
    assert code == cli.ExitCode.BLOCKED and _row(payload)["code"] == "TBX-METH-005"


def test_meth_006_a_solver_that_raises_on_nonconvergence_refuses(
    setup: Setup, monkeypatch
) -> None:
    import evidence_inspector.deconvolution as deconvolution

    def raising(*args: Any, **kwargs: Any):
        raise deconvolution.DeconvolutionError("NNLS did not converge within 1 iterations")

    monkeypatch.setattr(deconvolution, "deconvolve_uxm_v2", raising)
    root = setup.root()
    code, payload = setup.run(root, "--modbase-model", "model-x")
    assert code == cli.ExitCode.BLOCKED and _row(payload)["code"] == "TBX-METH-006"


def test_an_extraction_io_error_is_retryable(setup: Setup, monkeypatch) -> None:
    import evidence_inspector.cell_origin_pipeline as pipeline

    def full_disk(*args: Any, **kwargs: Any):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(pipeline, "write_prefiltered_bam", full_disk)
    root = setup.root()
    code, payload = setup.run(root, "--modbase-model", "model-x")
    assert code == cli.ExitCode.RETRYABLE_FAILURE, payload
    assert _row(payload)["code"] == "TBX-JOB-001"


def test_meth_006_an_unconverged_fit_refuses(setup: Setup, monkeypatch) -> None:
    import evidence_inspector.deconvolution as deconvolution

    real = deconvolution.deconvolve_uxm_v2

    def unconverged(*args: Any, **kwargs: Any):
        fit = real(*args, **kwargs)
        return fit.model_copy(
            update={"diagnostics": fit.diagnostics.model_copy(update={"converged": False})}
        )

    monkeypatch.setattr(deconvolution, "deconvolve_uxm_v2", unconverged)
    root = setup.root()
    code, payload = setup.run(root, "--modbase-model", "model-x")
    assert code == cli.ExitCode.BLOCKED and _row(payload)["code"] == "TBX-METH-006"


def test_meth_007_a_failed_validation_check_refuses(setup: Setup, monkeypatch) -> None:
    import evidence_inspector.cell_origin_pipeline as pipeline

    real = pipeline.validation_report

    def failing(**kwargs: Any):
        report = real(**kwargs)
        return report.model_copy(
            update={"records": tuple(
                record.model_copy(update={"passed": False})
                if record.check == ValidationCheck.DIGESTS_VERIFIED else record
                for record in report.records
            )}
        )

    monkeypatch.setattr(pipeline, "validation_report", failing)
    root = setup.root()
    code, payload = setup.run(root, "--modbase-model", "model-x")
    assert code == cli.ExitCode.BLOCKED
    row = _row(payload)
    assert row["code"] == "TBX-METH-007" and "digests_verified" in row["cause"]


def test_a_missing_tool_is_retryable_then_resumes(setup: Setup) -> None:
    installed = {"yes": False}

    def modkit() -> PinnedTool:
        if not installed["yes"]:
            raise ToolProblem(TOOL_MISSING, "modkit 0.6.4 is not installed", tool="modkit",
                              cause="not installed", fix="install it")
        return setup.tool

    setup.use(CellOriginAnalysis(
        parameters=_test_parameters(), modkit=modkit, platform_name="osx-arm64"
    ))
    root = setup.root()
    code, payload = setup.run(root, "--modbase-model", "model-x")
    assert code == cli.ExitCode.RETRYABLE_FAILURE, payload
    row = _row(payload)
    assert row["code"] == "TBX-TOOL-001" and row["retryable"] is True
    state = Runner(root / "runner", local_unqualified_enabled=True).status(row["job_id"]).state
    assert state == JobState.RETRYABLE_FAILURE
    installed["yes"] = True
    code, resumed = _json(setup.capsys, "resume", row["job_id"], "--root", root)
    assert code == 0, resumed
    assert resumed["data"]["state"] == "complete"


def test_an_asset_the_definition_does_not_name_is_refused(setup: Setup) -> None:
    class Rebound(CellOriginAnalysis):
        def definition(self, loaded: Any, config: Any, *, root: Path) -> Any:
            definition = super().definition(loaded, config, root=root)
            assets = tuple(
                item.model_copy(update={"content_sha256": "9" * 64})
                if item.asset_id.startswith("asset_loyfer_atlas") else item
                for item in definition.assets
            )
            return definition.model_copy(update={"assets": assets})

    setup.use(Rebound(
        parameters=_test_parameters(), modkit=lambda: setup.tool, platform_name="osx-arm64"
    ))
    root = setup.root()
    code, payload = setup.run(root, "--modbase-model", "model-x")
    assert code == cli.ExitCode.BLOCKED, payload
    assert _row(payload)["code"] == "TBX-JOB-003"


@pytest.mark.parametrize("change", ["append", "same_size"])
def test_a_changed_reference_fasta_is_retryable(setup: Setup, change: str) -> None:
    root = setup.root()
    fasta = setup.inputs.fasta_path
    if change == "append":
        with fasta.open("a") as handle:
            handle.write(">extra\nACGT\n")
    else:
        content = bytearray(fasta.read_bytes())
        position = content.index(b"\n") + 1  # the first sequence base
        content[position] = ord("A") if content[position] != ord("A") else ord("T")
        fasta.write_bytes(bytes(content))
    code, payload = setup.run(root, "--modbase-model", "model-x")
    assert code == cli.ExitCode.RETRYABLE_FAILURE, payload
    assert _row(payload)["code"] == "TBX-REF-001"


def test_a_changed_asset_copy_is_refused(setup: Setup) -> None:
    root = setup.root()
    regions = setup.inputs.loyfer_dir / "Regions.U250.l4.hg38.bed"
    regions.write_text(regions.read_text() + "chr1\t10000\t10100\n")
    code, payload = setup.run(root, "--modbase-model", "model-x")
    assert code == cli.ExitCode.RETRYABLE_FAILURE, payload
    assert _row(payload)["code"] == "TBX-ASSET-002"


# --------------------------------------------------------------------------
# Contract and validation report
# --------------------------------------------------------------------------


def _valid_measurement(setup: Setup) -> dict:
    root = setup.root()
    code, payload = setup.run(root, "--modbase-model", "model-x")
    assert code == 0, payload
    return json.loads(_measurement(root, payload)[0])


def test_the_contract_refuses_inconsistent_measurements(setup: Setup) -> None:
    value = _valid_measurement(setup)
    CellOriginMeasurementV1.model_validate(value)

    def refused(mutate) -> bool:
        changed = json.loads(json.dumps(value))
        mutate(changed)
        try:
            CellOriginMeasurementV1.model_validate(changed)
        except ValueError:
            return True
        return False

    assert refused(lambda v: v["denominators"].update(classified_fragments=1))
    assert refused(lambda v: v["denominators"].update(mixed_fragments=1))
    assert refused(lambda v: v["denominators"].update(observed_markers=2))
    assert refused(lambda v: v["denominators"].update(eligible_alignments=1))
    assert refused(lambda v: v["solver"].update(converged=False))
    assert refused(lambda v: v["estimates"].reverse())
    assert refused(lambda v: v["estimates"][0].update(fraction=0.9))
    assert refused(lambda v: v.update(definition_id="cell-origin-loyfer-uxm-v1.other"))
    assert refused(lambda v: v["marker_counts"].append(dict(v["marker_counts"][0])))
    assert refused(lambda v: v["denominators"].update(marker_overlapping_fragments=0))
    assert refused(lambda v: v["cpg_calls"].update(retained=0))
    assert refused(lambda v: v["cpg_calls"].update(dropped_at_load=1))
    assert refused(lambda v: v["denominators"].update(alignments_with_mod_tags=0))


def _small_fit():
    cells = ("TypeA", "TypeB")
    atlas = AtlasUMatrix(
        atlas_id="atlas.test", method=LOYFER_UXM_METHOD, cell_type_ids=cells,
        rows=tuple(
            AtlasUMatrixRow(marker_id=f"m{i}", values=tuple(
                AtlasUValue(cell_type_id=c, u_fraction=u) for c, u in zip(cells, us, strict=True)
            ))
            for i, us in enumerate(((0.9, 0.1), (0.1, 0.9), (0.5, 0.5)))
        ),
        source_ids=("source.test",),
    )
    rows = tuple(
        MarkerCountRow(marker_id=f"m{i}", u_count=u, x_count=0, m_count=100 - u,
                       classified_fragment_count=100, u_fraction=u / 100)
        for i, u in enumerate((66, 34, 50))
    )
    fit = deconvolve_uxm_v2(rows, atlas, row_scale=NnlsRowScale.SQRT_COUNT)
    bootstrap = bootstrap_uxm_v2(rows, atlas, fit, replicates=20, random_seed=7)
    return atlas, rows, fit, bootstrap


def test_validation_report_computes_every_check(tmp_path: Path) -> None:
    atlas, rows, fit, bootstrap = _small_fit()
    path = tmp_path / "asset.tsv"
    path.write_text("x")
    artifact = DigestArtifact(artifact_id="artifact.x",
                              sha256=hashlib.sha256(b"x").hexdigest(), size_bytes=1)
    base = dict(marker_counts=rows, classified_fragment_marker_count=300, atlas=atlas,
                deconvolution=fit, bootstrap=bootstrap, artifacts=((artifact, path),))
    assert validation_report(**base).passed

    def failed(**changes: Any) -> set[str]:
        report = validation_report(**{**base, **changes})
        return {record.check.value for record in report.records if not record.passed}

    assert failed(classified_fragment_marker_count=299) == {"uxm_counts_reconciled"}
    path.write_text("y")
    assert failed() == {"digests_verified"}
    path.write_text("x")
    assert failed(artifacts=()) == {"digests_verified"}
    assert failed(marker_counts=rows[:2]) >= {"markers_match_atlas"}
    skewed = fit.model_construct(**{**dict(fit), "estimates": tuple(
        item.model_construct(**{**dict(item), "fraction": 0.9}) for item in fit.estimates
    )})
    assert "fractions_normalized" in failed(deconvolution=skewed)
    unsafe = rows[0].model_construct(**{**dict(rows[0]), "marker_id": "reads.bam"})
    assert "model_safe" in failed(marker_counts=(unsafe, *rows[1:]))


# --------------------------------------------------------------------------
# No local data paths in the method code
# --------------------------------------------------------------------------


def test_no_data_local_path_in_the_cell_origin_method_code() -> None:
    sources = [
        REPO / "traceback_runner" / "cell_origin.py",
        REPO / "traceback_runner" / "cell_origin_method.py",
        REPO / "evidence_inspector" / "cell_origin_prefilter.py",
    ]
    pipeline = (REPO / "evidence_inspector" / "cell_origin_pipeline.py").read_text()
    # The standalone script's argparse defaults are the only place the
    # pipeline names data/local; the stage never reaches them.
    library, _, script = pipeline.partition("def build_argument_parser(")
    assert "data/local" in script
    for text in (*(path.read_text() for path in sources), library):
        assert not re.search(r"data/local", text)
