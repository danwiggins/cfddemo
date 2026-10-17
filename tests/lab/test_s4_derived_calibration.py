"""Derived chrY results use selected reads and a verified parent calibration."""

from __future__ import annotations

import json
from dataclasses import replace
from fractions import Fraction
from math import ceil
from pathlib import Path

import pytest

from traceback_lab.algorithms import dd_cfdna_chry as donor
from traceback_lab.algorithms import fetal_fraction_chry as fetal
from traceback_lab.dd_contract import register_contract as register_donor
from traceback_lab.derived import cna, downsample, materialize, split
from traceback_lab.derived_calibration import calibrated_input_id
from traceback_lab.fetal_chry_contract import register_contract as register_fetal
from traceback_lab.read_summary import DS_SALT, SPLIT_SALT, h64
from traceback_lab.registry import Ready, Requirements, Skip
from traceback_lab.store import LabStore

from .bam_fixtures import Read, context_for
from .test_dd_cfdna_chry import INPUT, REFERENCE, SOURCE, _file as donor_file
from .test_fetal_chry import _file as fetal_file


def _selected(name: str, spec) -> bool:
    if spec.transform == "downsample":
        threshold = ceil(Fraction(spec.parameters["fraction"]) * 2**64)
        return h64(DS_SALT, name) < threshold
    return (h64(SPLIT_SALT, name) < 2**63) == (spec.parameters["half"] == "A")


@pytest.mark.parametrize("kind", ["donor", "fetal"])
def test_calibrated_derived_counts_only_selected_reads(tmp_path: Path, kind: str) -> None:
    contigs = [(f"chr{i}", 248_956_422 if i == 1 else 1_000_000) for i in range(1, 23)]
    contigs += [("chrX", 1_000_000), ("chrY", 57_227_415)]
    autosomes = [Read(f"auto-{i}", "chr1", 1000 + i * 1000, "A" * 75) for i in range(40)]
    y_reads = [Read(f"y-{i}", "chrY", 3_000_000 + i * 1000, "A" * 75) for i in range(10)]
    reads = autosomes + y_reads
    context = context_for(tmp_path, contigs, reads, eligible_reads=len(reads))
    store = LabStore(tmp_path)
    if kind == "donor":
        path, digest = donor_file(tmp_path, min_depth_x=0.001,
                                  female_background_y_per_million=0,
                                  male_full_donor_y_per_million=1_000_000)
        contract = register_donor(store, path, digest)
        algorithm = donor.DD_CFDNA_CHRY
        count_metric = "chry_nonpar_reads"
        rate_metric = "chry_reads_per_million"
        fraction_metric = "dd_cfdna_fraction"
    else:
        path, digest = fetal_file(tmp_path, female_background_y_per_million=0,
                                  full_male_y_per_million=1_000_000,
                                  validated_min_fraction=0, validated_max_fraction=1)
        contract = register_fetal(store, path, digest)
        algorithm = replace(fetal.FETAL_FRACTION_CHRY, requires=Requirements(min_eligible_reads=0))
        count_metric = "fetal_chry_nonpar_reads"
        rate_metric = "fetal_chry_reads_per_million"
        fraction_metric = "fetal_fraction"
    specs = [downsample(0.5), downsample(0.25), split("A"), split("B")]
    materialize(store, INPUT, context.input_dir / "aligned.bam", specs,
                counting_contigs=[name for name, _ in contigs], min_mapq=20, counting=False)
    full = replace(context, source_id=SOURCE, input_id=INPUT, reference_id="hg38",
                   reference_sha256=REFERENCE, approx_depth_x=0.01,
                   extras={**context.extras, "planned_asset_sha256s": [contract.manifest_sha256]})
    assert algorithm.asset_sha256s_for_input(store.root, INPUT) == [contract.manifest_sha256]
    full_metrics = {row["metric_id"]: row for row in algorithm.run(full, tmp_path).metrics}
    assert full_metrics[count_metric]["value"] == len(y_reads)

    for spec in specs:
        derived_id = spec.input_id(INPUT)
        chosen = {read.name for read in reads if _selected(read.name, spec)}
        expected_y = sum(read.name in chosen for read in y_reads)
        derived = replace(full, input_id=derived_id, eligible_reads=len(chosen),
                          derived=spec.mask_spec())
        assert calibrated_input_id(store, derived_id, derived.derived) == INPUT
        assert algorithm.asset_sha256s_for_input(store.root, derived_id) == [contract.manifest_sha256]
        assert isinstance(algorithm.preflight(derived), Ready)
        metrics = {row["metric_id"]: row for row in algorithm.run(derived, tmp_path).metrics}
        assert metrics[count_metric]["value"] == expected_y
        assert metrics[count_metric]["n"] == len(chosen)
        assert metrics[rate_metric]["value"] == pytest.approx(expected_y * 1_000_000 / len(chosen))
        assert metrics[fraction_metric]["value"] == pytest.approx(expected_y / len(chosen))
        assert derived_id != INPUT
        assert isinstance(algorithm.preflight(replace(derived, source_id="f" * 64)), Skip)
        assert isinstance(algorithm.preflight(replace(derived, reference_sha256="f" * 64)), Skip)
        assert algorithm.preflight(replace(derived, approx_depth_x=0.0001)).reason_code in (
            "depth", "missing_asset")

    spike = cna(0.1, [])
    materialize(store, INPUT, context.input_dir / "aligned.bam", [spike],
                counting_contigs=[name for name, _ in contigs], min_mapq=20, counting=False)
    assert algorithm.asset_sha256s_for_input(store.root, spike.input_id(INPUT)) == []
    assert algorithm.preflight(replace(full, input_id=spike.input_id(INPUT),
                                       derived=spike.mask_spec())).reason_code == "unsupported_input"

    broken = specs[0].input_id(INPUT)
    manifest_path = store.inputs_dir / broken / "manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    manifest["salt"] = "wrong"
    manifest_path.write_text(json.dumps(manifest))
    assert algorithm.asset_sha256s_for_input(store.root, broken) == []
    assert calibrated_input_id(store, broken, specs[0].mask_spec()) is None
    assert (algorithm.preflight(replace(full, input_id=broken, derived=specs[0].mask_spec()))
            .reason_code == "unsupported_input")
