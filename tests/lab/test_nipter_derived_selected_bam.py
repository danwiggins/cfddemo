"""NIPTeR bins selected patient reads against the unchanged pinned controls."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from fractions import Fraction
from math import ceil
from pathlib import Path
from types import SimpleNamespace

import pysam

from traceback_lab.algorithms import fetal_fraction as fetal
from traceback_lab.derived import cna, downsample, materialize, split
from traceback_lab.fetal_contract import register_contract
from traceback_lab.read_summary import DS_SALT, SPLIT_SALT, ReadSummary, depth_stamp, derived_mask, h64
from traceback_lab.registry import Ready, Requirements
from traceback_lab.store import LabStore

from .bam_fixtures import Read, context_for
from .test_fetal_nipter import INPUT, REFERENCE, SOURCE, _contract, _stored_input


def _selected(name: str, spec) -> bool:
    if spec.transform == "downsample":
        return h64(DS_SALT, name) < ceil(Fraction(spec.parameters["fraction"]) * 2**64)
    return (h64(SPLIT_SALT, name) < 2**63) == (spec.parameters["half"] == "A")


def test_nipter_derived_bins_selected_bam_with_parent_controls(tmp_path: Path, monkeypatch) -> None:
    contigs = list(zip(fetal._CONTIGS, fetal._HG38_LENGTHS))
    reads = [Read(f"auto-{i}", "chr1", 1000 + i * 1000, "A" * 75) for i in range(40)]
    reads += [Read(f"c13-{i}", "chr13", 1000 + i * 1000, "A" * 75) for i in range(10)]
    context = context_for(tmp_path, contigs, reads, eligible_reads=len(reads))
    store = LabStore(tmp_path)
    _stored_input(store)
    path, digest, control = _contract(tmp_path)
    contract = register_contract(store, path, digest)
    runtime = tmp_path / "Rscript"
    runtime.write_bytes(b"synthetic Rscript")
    runtime_sha = hashlib.sha256(runtime.read_bytes()).hexdigest()
    monkeypatch.setattr(fetal, "_rscript", lambda: (runtime, runtime_sha))
    monkeypatch.setattr(fetal, "_nipter_package_sha", lambda path, **_kwargs: "e" * 64)
    algorithm = replace(fetal.FETAL_FRACTION, requires=Requirements(min_eligible_reads=0))
    assets = algorithm.asset_sha256s_for_input(store.root, INPUT)
    assert assets == [contract.manifest_sha256, contract.control_sha256,
                      fetal.SCRIPT_SHA256, runtime_sha, "e" * 64]
    full = replace(context, root=store.root, source_id=SOURCE, input_id=INPUT,
                   reference_id="hg38", reference_sha256=REFERENCE,
                   extras={**context.extras, "planned_asset_sha256s": assets})
    specs = [downsample(0.5), downsample(0.25), split("A"), split("B")]
    materialize(store, INPUT, context.input_dir / "aligned.bam", specs,
                counting_contigs=list(fetal._CONTIGS), min_mapq=20, counting=False)
    summary = ReadSummary.open(tmp_path / "read-summary" / "columns")
    expected_names: set[str] = set()

    def fake_r_process(argv, **kwargs):
        sample_bam = Path(argv[3])
        assert sample_bam != context.input_dir / "aligned.bam"
        assert sample_bam.parent != tmp_path / "worker" / "artifacts"
        assert Path(argv[4]).read_bytes() == control.read_bytes()
        assert kwargs["timeout_seconds"] == 900
        with pysam.AlignmentFile(str(sample_bam), "rb") as bam:
            observed = {record.query_name for record in bam.fetch(until_eof=True)}
        assert observed == expected_names
        return SimpleNamespace(outcome="exited", returncode=0,
                               stdout=b"13\t0.5\n18\t-0.25\n21\t1.75\nEND\n",
                               stdout_truncated=False)

    monkeypatch.setattr(fetal, "_run_bounded_r", fake_r_process)
    out = tmp_path / "worker" / "artifacts"
    for spec in specs:
        selected_mask = derived_mask(summary, spec.mask_spec())
        stamp = depth_stamp(summary, selected_mask)
        expected_names = {read.name for read in reads if _selected(read.name, spec)}
        assert stamp["eligible_reads"] == len(expected_names)
        derived = replace(full, input_id=spec.input_id(INPUT), derived=spec.mask_spec(),
                          eligible_reads=stamp["eligible_reads"],
                          approx_depth_x=stamp["approx_depth_x"])
        assert algorithm.asset_sha256s_for_input(store.root, derived.input_id) == assets
        assert fetal.FETAL_FRACTION.preflight(derived).reason_code == "depth"
        assert algorithm.preflight(replace(derived, eligible_reads=None)).reason_code == "depth"
        assert isinstance(algorithm.preflight(derived), Ready)
        output = algorithm.run(derived, out)
        assert {row["metric_id"]: row["value"] for row in output.metrics} == {
            "fetal_fraction": None, "trisomy_13_z": 0.5,
            "trisomy_18_z": -0.25, "trisomy_21_z": 1.75,
        }
        assert not list((tmp_path / "worker").glob("nipter-private-*"))

    spike = cna(0.1, [])
    materialize(store, INPUT, context.input_dir / "aligned.bam", [spike],
                counting_contigs=list(fetal._CONTIGS), min_mapq=20, counting=False)
    assert algorithm.asset_sha256s_for_input(store.root, spike.input_id(INPUT)) == []
    assert algorithm.preflight(replace(full, input_id=spike.input_id(INPUT),
                                       derived=spike.mask_spec())).reason_code == "unsupported_input"
