"""Synthetic acceptance checks at the interpretation/evidence boundary.

The cohort store owns membership filtering.  Its projection must pass values
*after* excluding the scored sample to the read-time interpretation engine.
"""

from __future__ import annotations

from traceback_lab.defended import Candidate, defended, headline
from traceback_lab.envelope import ResultEnvelope, provenance
from traceback_lab.interpretation.engine import Cohort, classify, interpret_result, load_library


def _cohort_entry():
    # Use a real library entry's metadata but a synthetic cohort reference.
    from traceback_lab.interpretation.engine import Entry

    source = load_library().entry("bam-qc", "mapped_fraction")
    assert source is not None
    return Entry(source.algorithm_id, source.metric_id, {**source.raw, "reference": {"kind": "internal_cohort"}})


def test_member_exclusion_precedes_ten_member_gate() -> None:
    entry = _cohort_entry()
    members = [float(i) for i in range(10)]
    # A 10-member generation leaves only 9 comparators for any member.
    result = classify(members[-1], entry, cohort=Cohort(values=members[:-1]))
    assert (result.state, result.reason, result.details["cohort_n"]) == (
        "no_reference", "cohort_too_small", 9,
    )
    # An independent query may use all 10 members, but only exploratorily.
    independent = classify(10.0, entry, cohort=Cohort(values=members))
    assert independent.state == "above_typical"
    assert independent.exploratory_n == 10
    assert independent.percentile is None


def test_member_exclusion_precedes_thirty_member_gate() -> None:
    entry = _cohort_entry()
    members = [float(i) for i in range(30)]
    member = classify(30.0, entry, cohort=Cohort(values=members[:-1]))
    assert member.state == "above_typical"
    assert member.exploratory_n == 29
    assert member.percentile is None
    independent = classify(30.0, entry, cohort=Cohort(values=members))
    assert independent.percentile == 100.0
    assert independent.exploratory_n is None


def test_partial_input_does_not_enter_the_ui_defended_rule() -> None:
    """Characterize D24: software eligibility has no S0 completeness input.

    The UI label therefore must not be reused as a full-run scientific claim.
    A versioned policy decision is needed before changing this behavior.
    """

    candidate = Candidate(
        algorithm_id="fragment-size", label="Synthetic size", value=167,
        unit="bp", tier="NEWER", status="ok", state="no_reference",
        usable_at_half=True, split_stable=True, split_relative_diff=0.01,
        citation_verified=True,
    )
    block = defended([candidate])
    assert block["qualified"] == 1
    assert block["rows"][0]["algorithm_id"] == "fragment-size"
    sentence = headline(0.42, 1, 1, block["qualified"], {}, downsampled=True)
    assert "result is defended" in sentence["sentence"]


def test_missing_library_entry_cannot_gain_citation_or_range_by_inference() -> None:
    absent = classify(0.5, None, depth_x=1.0, cohort=Cohort(values=[0.5] * 30))
    assert (absent.state, absent.reason, absent.reference_kind) == (
        "no_reference", "no_library_entry", "none",
    )
    assert absent.percentile is None


def test_placeholder_header_model_is_not_evidence_of_a_known_model(monkeypatch) -> None:
    """Record the current provenance gap without inventing a model identity."""

    from pathlib import Path

    from traceback_lab.algorithms.cell_origin import header_model
    from traceback_runner import cell_origin

    monkeypatch.setattr(cell_origin, "_header_models", lambda _bam: ["unknown_modbase_model"])
    assert header_model(Path("synthetic.bam")) == "unknown_modbase_model"
    # Header parsing establishes only that one string was declared. The
    # actual model and its calibration remain an external owner decision.


def test_frag_x_meth_entry_states_formula_and_evidence_limit() -> None:
    entry = load_library().entry("frag-x-meth", "short_fragment_hypomethylation_delta")
    assert entry is not None and entry.headline
    assert entry.tier == "SPECULATIVE"
    assert entry["origin_platform"] == "ONT; this Lab implementation"
    assert entry["reference"] == {"kind": "none"}
    assert entry["performance"] is None
    assert entry["citations"] == []
    assert entry["stability"] == {"kind": "absolute", "tolerance": 0.10}
    assert classify(0.12, entry).state == "no_reference"
    assert "151-220 bp minus mean for 100-150 bp" in entry["citation_checked"]["note"]


def test_breakpoint_motifs_entry_uses_valid_end_denominator_without_a_range() -> None:
    entry = load_library().entry("breakpoint-motifs", "cpg_near_end_fraction")
    assert entry is not None and entry.headline
    assert entry.tier == "SPECULATIVE"
    assert entry["reference"] == {"kind": "none"}
    assert entry["performance"] is None
    assert entry["citations"] == []
    assert entry["flags"] == ["LEGAL"]
    assert entry["stability"] == {"kind": "proportion"}
    assert classify(0.5, entry).state == "no_reference"
    note = entry["citation_checked"]["note"]
    assert "Each valid end contributes once to the denominator" in note
    assert "not claimed physical cleavage sites" in note
    assert "10 bp inclusive" in note


def test_donor_and_clock_supporting_metrics_disclose_lab_origin() -> None:
    library = load_library()
    expected = {
        ("dd-cfdna-chry", "chry_nonpar_reads"):
            "ONT; this Lab's D21 non-PAR chrY supporting count",
        ("dd-cfdna-chry", "chry_reads_per_million"):
            "ONT; this Lab's D21 non-PAR chrY supporting rate",
        ("epi-clock", "cg02228185_modified_fraction"):
            "ONT cfDNA; this Lab's per-site 5mC+5hmC call fraction",
        ("epi-clock", "cg25809905_modified_fraction"):
            "ONT cfDNA; this Lab's per-site 5mC+5hmC call fraction",
        ("epi-clock", "pde4c_upstream_of_cg17861230_modified_fraction"):
            "ONT cfDNA; this Lab's per-site 5mC+5hmC call fraction",
    }
    for (algorithm_id, metric_id), platform in expected.items():
        entry = library.entry(algorithm_id, metric_id)
        assert entry is not None
        assert entry.tier == entry["origin_tier"] == "SPECULATIVE"
        assert entry["origin_platform"] == platform
        assert entry["reference"] == {"kind": "none"}
        assert entry["citations"] == []
        assert not entry.citation_verified


def test_lionheart_citation_check_does_not_transfer_illumina_performance() -> None:
    entry = load_library().entry("lionheart", "cell_types_scored")
    assert entry is not None and entry.citation_verified
    assert entry.tier == "SPECULATIVE"
    assert entry["origin_tier"] == "NEWER"
    assert "paired-end short-read" in entry["origin_platform"]
    assert entry["performance"] is None
    assert "No validated ONT performance" in entry["performance_missing_reason"]
    assert entry["citations"] == ["doi:10.1038/s41467-025-66503-3"]
    assert "no fixed paper count" in entry["citation_checked"]["note"]


def test_one_sided_c1_envelope_explains_missing_group() -> None:
    """A null delta with a counted molecule must not say no molecules existed."""

    envelope = ResultEnvelope.model_validate(
        {
            "algorithm_id": "frag-x-meth",
            "algorithm_version": "1",
            "status": "ok",
            "provenance": provenance(
                tool="synthetic", parameters_sha256="a" * 64,
                source_id="b" * 64, input_id="c" * 64,
                reference_id="r", timestamp_utc="2026-10-16T00:00:00Z",
            ),
            "metrics": [
                {
                    "metric_id": "short_fragment_hypomethylation_delta",
                    "value": None,
                    "unit": "fraction",
                    "n": 1,  # one eligible molecule in one size group, zero in the other
                }
            ],
        }
    )
    rows = interpret_result(envelope, load_library(), depth_x=1.0)
    assert len(rows) == 1
    interpretation = rows[0].interpretation
    assert (interpretation.state, interpretation.reason) == ("not_interpretable", "depth")
    assert "at least one size group has no eligible molecules" in interpretation.caption
    assert "short_n and long_n" in interpretation.caption
    assert "No molecules counted" not in interpretation.caption


def test_generic_null_caption_does_not_assert_zero_molecules() -> None:
    entry = load_library().entry("bam-qc", "mapped_fraction")
    assert entry is not None
    interpretation = classify(None, entry, depth_x=1.0)
    assert (interpretation.state, interpretation.reason) == ("not_interpretable", "depth")
    assert interpretation.caption == "Metric unavailable at this depth."
