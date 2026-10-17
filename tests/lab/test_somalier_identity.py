"""Synthetic Somalier identity tests; no donor data or clinical thresholds."""

from __future__ import annotations

import gzip
import hashlib
import json
import subprocess
from types import SimpleNamespace

import pytest

from traceback_lab.algorithms.identity import IDENTITY, _somalier_env
from traceback_lab.identity_pair import (
    IdentityPairError,
    SelectedSketch,
    _selected_sketch,
    compare_pair,
    parse_pair_tsv,
    pairs_for_sample,
)
from traceback_lab.identity_sites import IdentitySitesError, inspect_sites, register_sites
from traceback_lab.registry import InputContext, Skip
from traceback_lab.store import LabStore


def test_sites_are_pinned_and_reject_changed_bytes(tmp_path) -> None:
    store = LabStore(tmp_path / "root")
    source = tmp_path / "sites.vcf.gz"
    with gzip.open(source, "wb") as stream:
        stream.write(b"##fileformat=VCFv4.2\n#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    with pytest.raises(IdentitySitesError, match="expected SHA-256"):
        register_sites(store, source, "0" * 64, "synthetic sites")
    with store.writer("identity sites add", "r-20261016-0000-aabb", step="sites"):
        registered = register_sites(store, source, digest, "synthetic sites")
    assert inspect_sites(store).sha256 == digest == registered.sha256
    registered.file.write_bytes(b"altered")
    with pytest.raises(IdentitySitesError, match="registered bytes"):
        inspect_sites(store)


def test_sites_registration_rejects_truncated_gzip_after_valid_header(tmp_path) -> None:
    store = LabStore(tmp_path / "root")
    source = tmp_path / "truncated.vcf.gz"
    with gzip.open(source, "wb") as stream:
        stream.write(b"##fileformat=VCFv4.2\n#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n")
    source.write_bytes(source.read_bytes()[:-5])
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    with pytest.raises(IdentitySitesError, match="readable gzip VCF"):
        register_sites(store, source, digest, "truncated synthetic sites")


def test_depth_gate_precedes_tool_and_assets(tmp_path) -> None:
    ctx = InputContext(tmp_path, "a" * 64, "b" * 64, "ref", "c" * 64,
                       tmp_path, eligible_reads=9_999_999)
    decision = IDENTITY.preflight(ctx)
    assert isinstance(decision, Skip)
    assert decision.reason_code == "depth"


def test_somalier_environment_discards_inherited_controls(monkeypatch) -> None:
    monkeypatch.setenv("SOMALIER_REPORT_ALL_PAIRS", "1")
    monkeypatch.setenv("SOMALIER_SAMPLE_NAME", "Donor Jane")
    env = _somalier_env("a" * 64)
    assert env["SOMALIER_SAMPLE_NAME"] == "a" * 64
    assert "SOMALIER_REPORT_ALL_PAIRS" not in env


def test_tool_pin_refreshes_when_executable_changes(tmp_path, monkeypatch) -> None:
    from traceback_lab.algorithms import identity as algorithm

    executable = tmp_path / "somalier"
    monkeypatch.setattr(algorithm.shutil, "which", lambda _name: None)
    assert algorithm.somalier_tool() is None
    executable.write_bytes(b"first")
    monkeypatch.setattr(algorithm.shutil, "which", lambda _name: str(executable))
    calls: list[str] = []

    def fake_run(argv, **_kwargs):
        version = "0.3.5" if executable.read_bytes() == b"first" else "0.3.6"
        calls.append(version)
        return subprocess.CompletedProcess(argv, 0, b"", f"somalier version: {version}".encode())

    monkeypatch.setattr(algorithm.subprocess, "run", fake_run)
    assert algorithm.somalier_tool() == (str(executable), "0.3.5")
    assert algorithm.somalier_tool() == (str(executable), "0.3.5")
    executable.write_bytes(b"second version")
    assert algorithm.somalier_tool() == (str(executable), "0.3.6")
    assert calls == ["0.3.5", "0.3.6"]


def test_extract_uses_registered_sites_and_opaque_input_name(tmp_path, monkeypatch) -> None:
    from traceback_lab.algorithms import identity as algorithm

    store = LabStore(tmp_path / "root")
    source = tmp_path / "sites.vcf.gz"
    with gzip.open(source, "wb") as stream:
        stream.write(b"##fileformat=VCFv4.2\n#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n")
    with store.writer("identity sites add", "r-20261016-0000-aabb", step="sites"):
        sites = register_sites(store, source, hashlib.sha256(source.read_bytes()).hexdigest(), "synthetic sites")
    fasta = tmp_path / "reference.fa"
    fasta.write_bytes(b">chr1\nACGT\n")
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    (input_dir / "aligned.bam").write_bytes(b"synthetic BAM placeholder")
    input_id = "a" * 64
    ctx = InputContext(store.root, "b" * 64, input_id, "ref",
                       hashlib.sha256(fasta.read_bytes()).hexdigest(), input_dir,
                       eligible_reads=10_000_000, extras={"reference_fasta": str(fasta)})
    monkeypatch.setattr(algorithm, "somalier_tool", lambda: ("somalier", "0.2.19"))

    def fake_run(argv, **kwargs):
        assert argv[1] == "extract" and argv[argv.index("--sites") + 1] == str(sites.file)
        assert kwargs["env"]["SOMALIER_SAMPLE_NAME"] == input_id
        out_dir = tmp_path / "output" / "somalier"
        (out_dir / f"{input_id}.somalier").write_bytes(b"synthetic sketch")
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(algorithm.subprocess, "run", fake_run)
    assert algorithm.IDENTITY.preflight(ctx).__class__.__name__ == "Ready"
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    output = algorithm.IDENTITY.run(ctx, output_dir)
    assert output.artifacts == {"fingerprint.somalier": b"synthetic sketch"}


def test_pair_tsv_requires_exact_opaque_names_and_finite_metrics() -> None:
    header = b"#sample_a\tsample_b\trelatedness\tibs0\tibs2\tconcordance\tn\n"
    a, b = "a" * 64, "b" * 64
    values = parse_pair_tsv(header + f"{a}\t{b}\t0.48\t1\t20\t0.99\t21\n".encode(), a, b)
    assert values == {"relatedness": 0.48, "concordance": 0.99, "ibs0": 1, "ibs2": 20, "n": 21}
    with pytest.raises(IdentityPairError, match="names"):
        parse_pair_tsv(header + f"private-name\t{b}\t0.48\t1\t20\t0.99\t21\n".encode(), a, b)
    with pytest.raises(IdentityPairError, match="invalid"):
        parse_pair_tsv(header + f"{a}\t{b}\tnan\t1\t20\t0.99\t21\n".encode(), a, b)
    with pytest.raises(IdentityPairError, match="no shared callable sites"):
        parse_pair_tsv(header + f"{a}\t{b}\t0.000\t0\t0\t0.000\t0\n".encode(), a, b)


def test_selected_sketch_requires_current_input_and_execution_identity(tmp_path, monkeypatch) -> None:
    from traceback_lab import identity_pair as pair

    store = LabStore(tmp_path / "root")
    source, input_id, parameters = "a" * 64, "b" * 64, "c" * 64
    provenance = SimpleNamespace(source_id=source, input_id=input_id,
                                 parameters_sha256=parameters, reference_id="ref")
    result = SimpleNamespace(status="ok", provenance=provenance,
                             depth=SimpleNamespace(eligible_reads=10_000_000))
    summary = SimpleNamespace(provenance=SimpleNamespace(parameters_sha256="d" * 64))
    sample = SimpleNamespace(full=SimpleNamespace(input_id=input_id,
                             results={"identity": result, "read-summary": summary}))
    monkeypatch.setattr(pair.views, "load_sample", lambda *_: sample)
    monkeypatch.setattr(pair, "resolve_reference", lambda *_args, **_kwargs: SimpleNamespace(sha256="e" * 64))
    monkeypatch.setattr(pair, "execution_identity", lambda *_args, **_kwargs: parameters)
    artifact_dir = tmp_path / "artifacts"
    artifact_dir.mkdir()
    sketch = artifact_dir / "fingerprint.somalier"
    sketch.write_bytes(b"synthetic sketch")
    monkeypatch.setattr(store, "selected_attempt", lambda *_: SimpleNamespace(name="a-20261016T000000000000-aaaaaaaa"))
    monkeypatch.setattr(store, "selected_artifacts", lambda *_: artifact_dir)
    selected = _selected_sketch(store, source, sites_sha256="f" * 64)
    assert selected.data == b"synthetic sketch" and selected.input_id == input_id
    monkeypatch.setattr(pair, "execution_identity", lambda *_args, **_kwargs: "0" * 64)
    with pytest.raises(IdentityPairError, match="older sites, tool, reference"):
        _selected_sketch(store, source, sites_sha256="f" * 64)
    monkeypatch.setattr(pair, "execution_identity", lambda *_args, **_kwargs: parameters)
    sample.full.input_id = "1" * 64
    with pytest.raises(IdentityPairError, match="current source and input"):
        _selected_sketch(store, source, sites_sha256="f" * 64)


def test_pair_attempt_binds_both_selected_fingerprints_and_caches(tmp_path, monkeypatch) -> None:
    from traceback_lab import identity_pair as pair

    store = LabStore(tmp_path / "root")
    a, b = "a" * 64, "b" * 64
    sketches = {
        a: SelectedSketch(a, "c" * 64, "a-20261016T000000000000-aaaaaaaa", "e" * 64,
                          hashlib.sha256(b"sketch a").hexdigest(), b"sketch a"),
        b: SelectedSketch(b, "d" * 64, "a-20261016T000000000000-bbbbbbbb", "e" * 64,
                          hashlib.sha256(b"sketch b").hexdigest(), b"sketch b"),
    }
    monkeypatch.setattr(pair, "source_ids", lambda _: [a, b])
    monkeypatch.setattr(pair, "resolve_prefix", lambda value, _: SimpleNamespace(state="found", value=a if value.startswith("a") else b))
    monkeypatch.setattr(pair, "inspect_sites", lambda _: SimpleNamespace(file=tmp_path / "sites.vcf.gz", sha256="f" * 64))
    monkeypatch.setattr(pair, "somalier_tool", lambda: ("somalier", "0.2.19"))
    monkeypatch.setattr(pair, "_selected_sketch", lambda _, source, **kwargs: sketches[source])
    calls = []

    def fake_run(argv, **kwargs):
        assert argv[1] == "relate" and kwargs["env"]["SOMALIER_REPORT_ALL_PAIRS"] == "1"
        assert argv[argv.index("--min-depth") + 1] == "7"
        assert argv[argv.index("--min-ab") + 1] == "0.3"
        prefix = argv[argv.index("--output-prefix") + 1]
        with open(prefix + ".pairs.tsv", "wb") as stream:
            stream.write(("#sample_a\tsample_b\trelatedness\tibs0\tibs2\tconcordance\tn\n"
                          f"{sketches[a].input_id}\t{sketches[b].input_id}\t0.5\t0\t10\t0.95\t10\n").encode())
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(pair.subprocess, "run", fake_run)
    with store.writer("identity compare", "r-20261016-0000-aabb", step="relate"):
        first = compare_pair(store, a[:12], b[:12])
        cached = compare_pair(store, b[:12], a[:12])
        old_summary = (store.lab / "identity-pairs" / first["pair_id"] / "attempts"
                       / first["attempt_id"] / "summary.json")
        invalid = json.loads(old_summary.read_text())
        invalid["metrics"]["n"] = 0
        old_summary.write_text(json.dumps(invalid))
        recovered = compare_pair(store, a[:12], b[:12])
        sketches[b] = SelectedSketch(b, sketches[b].input_id,
                                     "a-20261016T000000000000-cccccccc", "e" * 64,
                                     sketches[b].sketch_sha256, sketches[b].data)
        changed = compare_pair(store, a[:12], b[:12])
    assert first["cached"] is False and cached["cached"] is True
    assert recovered["cached"] is False and recovered["attempt_id"] != first["attempt_id"]
    assert changed["cached"] is False and changed["execution_id"] != first["execution_id"]
    assert cached["attempt_id"] == first["attempt_id"] != changed["attempt_id"]
    assert len(calls) == 3
    assert first["participants"][0]["source_id"] == a
    assert first["interpretation"].startswith("exploratory")


def test_pair_list_limits_matching_pairs_after_filtering_unrelated_directories(tmp_path, monkeypatch) -> None:
    from traceback_lab import identity_pair as pair

    store = LabStore(tmp_path / "root")
    pair_root = store.lab / "identity-pairs"
    pair_root.mkdir(parents=True)
    for index in range(65):
        (pair_root / f"{index:064x}").mkdir()
    target, second_target = "f" * 64, "e" * 64
    (pair_root / target).mkdir()
    (pair_root / second_target).mkdir()
    source, peer, second_peer = "a" * 64, "b" * 64, "c" * 64
    sketches = {
        source: SelectedSketch(source, "c" * 64, "a-20261016T000000000000-aaaaaaaa", "e" * 64,
                               hashlib.sha256(b"a").hexdigest(), b"a"),
        peer: SelectedSketch(peer, "d" * 64, "a-20261016T000000000000-bbbbbbbb", "e" * 64,
                             hashlib.sha256(b"b").hexdigest(), b"b"),
        second_peer: SelectedSketch(second_peer, "1" * 64, "a-20261016T000000000000-cccccccc", "e" * 64,
                                    hashlib.sha256(b"c").hexdigest(), b"c"),
    }
    summary = {"pair_id": target, "sites_sha256": "f" * 64,
               "participants": [sketches[source].identity(), sketches[peer].identity()],
               "metrics": {"relatedness": 0.5, "concordance": 0.9, "n": 10},
               "interpretation": "exploratory"}
    second_summary = {**summary, "pair_id": second_target,
                      "participants": [sketches[source].identity(), sketches[second_peer].identity()]}
    monkeypatch.setattr(pair, "_selected_pair", lambda _store, pair_id: {
        target: summary, second_target: second_summary,
    }.get(pair_id))
    monkeypatch.setattr(pair, "inspect_sites", lambda _store: SimpleNamespace(sha256="f" * 64))
    selected_calls: list[str] = []

    def selected(_store, source_id, **_kwargs):
        selected_calls.append(source_id)
        return sketches[source_id]

    monkeypatch.setattr(pair, "_selected_sketch", selected)
    monkeypatch.setattr(pair, "id12_for", lambda _store, source_id: source_id[:12])

    assert pairs_for_sample(store, source) == [
        {"peer_id12": second_peer[:12], "metrics": summary["metrics"],
         "pair_id": second_target, "interpretation": "exploratory"},
        {"peer_id12": peer[:12], "metrics": summary["metrics"],
         "pair_id": target, "interpretation": "exploratory"},
    ]
    assert selected_calls.count(source) == 1


def test_malformed_stored_pair_is_ignored_by_sample_read(tmp_path, monkeypatch) -> None:
    from traceback_lab import identity_pair as pair

    store = LabStore(tmp_path / "root")
    source, peer = "a" * 64, "b" * 64
    pair_id = "f" * 64
    attempt = "a-20261016T000000000000-aaaaaaaa"
    pair_dir = store.lab / "identity-pairs" / pair_id
    summary_dir = pair_dir / "attempts" / attempt
    summary_dir.mkdir(parents=True)
    (pair_dir / "selected.json").write_text(json.dumps({"attempt_id": attempt}))
    sketches = {
        source: SelectedSketch(source, "c" * 64, attempt, "e" * 64,
                               hashlib.sha256(b"a").hexdigest(), b"a"),
        peer: SelectedSketch(peer, "d" * 64, attempt, "e" * 64,
                             hashlib.sha256(b"b").hexdigest(), b"b"),
    }
    summary = {
        "schema": pair.PAIR_SCHEMA, "pair_id": pair_id, "attempt_id": attempt,
        "execution_id": "1" * 64, "sites_sha256": "f" * 64,
        "participants": [sketch.identity() for sketch in sketches.values()],
        "metrics": {"relatedness": 0.5, "concordance": 0.9, "ibs0": 0, "ibs2": 10, "n": 10},
        "interpretation": "exploratory",
    }
    summary_file = summary_dir / "summary.json"
    monkeypatch.setattr(pair, "inspect_sites", lambda _store: SimpleNamespace(sha256="f" * 64))
    monkeypatch.setattr(pair, "_selected_sketch", lambda _store, source_id, **_kwargs: sketches[source_id])
    monkeypatch.setattr(pair, "id12_for", lambda _store, source_id: source_id[:12])

    summary_file.write_text(json.dumps(summary))
    assert len(pairs_for_sample(store, source)) == 1
    summary["metrics"]["n"] = 0
    summary_file.write_text(json.dumps(summary))
    assert pairs_for_sample(store, source) == []
    summary["metrics"]["n"] = 10
    summary["metrics"] = None
    summary_file.write_text(json.dumps(summary))
    assert pairs_for_sample(store, source) == []
    summary["metrics"] = {"relatedness": 0.5, "concordance": 0.9, "ibs0": 0, "ibs2": 10, "n": 10}
    summary["participants"] = [sketches[source].identity(), sketches[source].identity()]
    summary_file.write_text(json.dumps(summary))
    assert pairs_for_sample(store, source) == []
