"""Synthetic worker acceptance for chrY calibration across derived row masks.

The fetal fixture relaxes only its 2M-read entry floor so a tiny BAM can reach
the unchanged registered depth gate; this does not qualify either assay.
"""

from __future__ import annotations

from dataclasses import replace
from fractions import Fraction
from math import ceil
from pathlib import Path

import pytest

from traceback_lab.algorithms.dd_cfdna_chry import DD_CFDNA_CHRY
from traceback_lab.algorithms.fetal_fraction_chry import FETAL_FRACTION_CHRY
from traceback_lab.algorithms.read_summary import READ_SUMMARY
from traceback_lab.controls import run_derived
from traceback_lab.dd_contract import register_contract as register_donor
from traceback_lab.derived import downsample, split
from traceback_lab.fetal_chry_contract import register_contract as register_fetal
from traceback_lab.read_summary import DS_SALT, SPLIT_SALT, ReadSummary, depth_stamp, derived_mask, h64
from traceback_lab.registry import InputContext, PlanItem, Ready
from traceback_lab.runner import RunTarget, run_plan
from traceback_lab.scrub import Scrubber
from traceback_lab.store import LabStore, new_run_id

from .bam_fixtures import Read, context_for
from .test_dd_cfdna_chry import INPUT, REFERENCE, SOURCE, _file as donor_file, _stored_input
from .test_fetal_chry import _file as fetal_file


# The child worker imports this module-level algorithm. The only test seam is
# the fetal method's 2M-read entry floor; registered calibration and worker
# counting/depth checks remain production code.
FETAL_SMALL_BAM = replace(
    FETAL_FRACTION_CHRY,
    requires=replace(FETAL_FRACTION_CHRY.requires, min_eligible_reads=0),
)
BACKGROUND_Y_PER_MILLION = 20_000
FULL_MALE_Y_PER_MILLION = 800_000


def _selected(name: str, spec) -> bool:
    if spec.transform == "downsample":
        return h64(DS_SALT, name) < ceil(Fraction(spec.parameters["fraction"]) * 2**64)
    return (h64(SPLIT_SALT, name) < 2**63) == (spec.parameters["half"] == "A")


@pytest.mark.parametrize("kind", ["donor", "fetal"])
def test_derived_worker_uses_selected_depth_and_pinned_parent_calibration(
    tmp_path: Path, kind: str,
) -> None:
    store = LabStore(tmp_path / "root")
    store.ensure()
    contigs = [(f"chr{i}", 248_956_422 if i == 1 else 1_000_000) for i in range(1, 23)]
    contigs += [("chrX", 1_000_000), ("chrY", 57_227_415)]
    reads = [Read(f"auto-{i}", "chr1", 1_000 + i * 2_000, "A" * 1_500) for i in range(80)]
    reads += [Read(f"y-{i}", "chrY", 3_000_000 + i * 2_000, "A" * 1_500) for i in range(20)]
    fixture = context_for(store.root, contigs, reads)
    _stored_input(store)

    parent = RunTarget(INPUT, SOURCE, fixture.input_dir, "hg38", REFERENCE)
    run_id = new_run_id()
    log = store.run_log(run_id)
    scrubber = Scrubber(root=store.root)
    summary_result, = run_plan(
        store, None, log, parent, [PlanItem(READ_SUMMARY, True)],
        {"read-summary": "read_summary:READ_SUMMARY"},
        run_id=run_id, scrubber=scrubber, timeout_s=60,
    )
    assert summary_result.status == "ok" and summary_result.artifacts_dir
    summary = ReadSummary.open(Path(summary_result.artifacts_dir) / "columns")
    specs = [downsample(0.75), downsample(0.25), split("A"), split("B")]
    stamps = {spec.label(): depth_stamp(summary, derived_mask(summary, spec.mask_spec())) for spec in specs}
    for spec in specs:
        assert stamps[spec.label()]["eligible_reads"] == sum(_selected(read.name, spec) for read in reads)
    full_stamp = depth_stamp(summary)
    high = stamps[specs[0].label()]["approx_depth_x"]
    low = max(stamps[spec.label()]["approx_depth_x"] for spec in specs[1:])
    assert full_stamp["eligible_reads"] == len(reads)
    assert full_stamp["approx_depth_x"] > high > low > 0
    floor = (high + low) / 2
    assert parent.eligible_reads == full_stamp["eligible_reads"]
    assert parent.approx_depth_x == full_stamp["approx_depth_x"]

    if kind == "donor":
        assert DD_CFDNA_CHRY.requires.min_eligible_reads == 0
        path, digest = donor_file(
            tmp_path, min_depth_x=floor,
            female_background_y_per_million=BACKGROUND_Y_PER_MILLION,
            male_full_donor_y_per_million=FULL_MALE_Y_PER_MILLION,
        )
        contract = register_donor(store, path, digest)
        algorithm = DD_CFDNA_CHRY
        entry = "dd_cfdna_chry:DD_CFDNA_CHRY"
        fraction_metric, count_metric, refusal = "dd_cfdna_fraction", "chry_nonpar_reads", "depth"
    else:
        path, digest = fetal_file(
            tmp_path, min_depth_x=floor,
            female_background_y_per_million=BACKGROUND_Y_PER_MILLION,
            full_male_y_per_million=FULL_MALE_Y_PER_MILLION,
            validated_min_fraction=0, validated_max_fraction=1,
        )
        contract = register_fetal(store, path, digest)
        algorithm = FETAL_SMALL_BAM
        entry = "tests.lab.test_s4_derived_worker_acceptance:FETAL_SMALL_BAM"
        fraction_metric, count_metric, refusal = "fetal_fraction", "fetal_chry_nonpar_reads", "depth"

    # The parent registration is the only calibration. Derived IDs must resolve
    # it through their published manifests, never through a copied contract.
    full_result, = run_plan(
        store, None, log, parent, [PlanItem(algorithm, False)],
        {algorithm.id: entry}, run_id=run_id, scrubber=scrubber, timeout_s=60,
        upstream_results={"read-summary": summary_result},
    )
    assert full_result.status == "ok"
    full_envelope = store.read_result(INPUT, algorithm.id, full_result.parameters_sha256)
    assert full_envelope is not None and full_envelope.depth is not None
    assert full_envelope.depth.eligible_reads == full_stamp["eligible_reads"]
    assert full_envelope.depth.approx_depth_x == full_stamp["approx_depth_x"]
    full_metrics = {metric.metric_id: metric for metric in full_envelope.metrics}
    full_fraction = (
        20 * 1_000_000 / len(reads) - BACKGROUND_Y_PER_MILLION
    ) / (FULL_MALE_Y_PER_MILLION - BACKGROUND_Y_PER_MILLION)
    assert full_metrics[fraction_metric].value == pytest.approx(full_fraction)
    assert full_metrics[fraction_metric].value != pytest.approx(20 / len(reads))

    runs = run_derived(
        store, None, log, parent, specs, [PlanItem(algorithm, False)],
        {algorithm.id: entry}, run_id=run_id, read_summary=summary_result,
        timeout_s=60, scrubber=scrubber,
    )
    assert [run.derived.spec.label() for run in runs] == [spec.label() for spec in specs]
    for run in runs:
        assert len(run.results) == 1, run.derived.spec.label()
        result, = run.results
        stamp = stamps[run.derived.spec.label()]
        assert algorithm.asset_sha256s_for_input(store.root, run.derived.input_id) == [contract.manifest_sha256]
        saved = store.read_result(run.derived.input_id, algorithm.id, result.parameters_sha256)
        assert saved is not None and saved.depth is not None
        assert saved.depth.eligible_reads == stamp["eligible_reads"]
        assert saved.depth.approx_depth_x == stamp["approx_depth_x"]
        assert saved.provenance.input_id == run.derived.input_id
        # At the parent's depth the same derived input and planned digest
        # must pass preflight, leaving subset depth as the published refusal.
        at_parent_depth = InputContext(
            root=store.root, source_id=SOURCE, input_id=run.derived.input_id,
            reference_id="hg38", reference_sha256=REFERENCE, input_dir=fixture.input_dir,
            platform=parent.platform,
            eligible_reads=stamp["eligible_reads"], approx_depth_x=full_stamp["approx_depth_x"],
            derived=run.derived.spec.mask_spec(),
            extras={"upstream": {"read-summary": summary_result.artifacts_dir},
                    "planned_asset_sha256s": [contract.manifest_sha256]},
        )
        assert isinstance(algorithm.preflight(at_parent_depth), Ready)
        if run.derived.spec == specs[0]:
            assert result.status == "ok"
            metrics = {metric.metric_id: metric for metric in saved.metrics}
            selected_y = sum(read.name.startswith("y-") and _selected(read.name, run.derived.spec)
                             for read in reads)
            assert metrics[count_metric].value == selected_y
            assert metrics[count_metric].n == stamp["eligible_reads"]
            expected_fraction = (
                selected_y * 1_000_000 / stamp["eligible_reads"] - BACKGROUND_Y_PER_MILLION
            ) / (FULL_MALE_Y_PER_MILLION - BACKGROUND_Y_PER_MILLION)
            assert metrics[fraction_metric].value == pytest.approx(expected_fraction)
        else:
            assert (result.status, result.skip_reason) == ("skipped", refusal)
            assert saved.metrics == []
