"""Synthetic-only tests for pinned ichorCNA preparation and output parsing."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from pydantic import ValidationError

from evidence_inspector.ichor_adapter import (
    HMMCOPY_COMMIT,
    ICHOR_COMMIT,
    ArtifactIdentity,
    CanonicalBin,
    CanonicalBinMask,
    CanonicalGrid,
    CentromereTableBinding,
    CnvAssetSet,
    CnvRunRequest,
    CountingPolicy,
    ExternalComponentBinding,
    IchorOutputError,
    IchorParameterSet,
    PanelOfNormalsBinding,
    PreparedIchorRun,
    RawWigLineage,
    ReferenceFastaBinding,
    RuntimeBinding,
    WigGridBinding,
    contract_sha256,
    prepare_ichor_run,
    validate_ichor_outputs,
)

FIXTURES = Path(__file__).parent / "fixtures" / "ichor"


def _artifact(name: str, marker: str) -> ArtifactIdentity:
    return ArtifactIdentity(
        artifact_id=name,
        content_sha256=marker * 64,
        content_size_bytes=100,
    )


def _component(
    component_id: str,
    commit: str,
    marker: str,
) -> ExternalComponentBinding:
    return ExternalComponentBinding(
        component_id=component_id,
        version_label="pinned-v1",
        source_repository_url=f"https://github.com/example/{component_id}",
        source_commit_sha1=commit,
        source_tree_or_archive_sha256=marker * 64,
        invoked_files=(_artifact(f"{component_id}.invoked", marker),),
        declared_license="GPL-3",
        license_evidence=_artifact(f"{component_id}.license", marker),
        modified=False,
    )


def _grid(*, mask_last: bool = False) -> CanonicalGrid:
    bins = tuple(
        CanonicalBin(
            contig="chr1",
            start=index * 1_000_000,
            end=(index + 1) * 1_000_000,
        )
        for index in range(4)
    )
    return CanonicalGrid(
        contig_order=("chr1",),
        bins=bins,
        masks=(
            (CanonicalBinMask(bin=bins[-1], reason="centromere_or_flank"),)
            if mask_last
            else ()
        ),
        bin_definition_sha256=contract_sha256(bins),
    )


def _request(*, with_pon: bool = False, mask_last: bool = False) -> CnvRunRequest:
    grid = _grid(mask_last=mask_last)
    contigs = "d" * 64
    reference = ReferenceFastaBinding(
        identity=_artifact("reference.hg38", "1"),
        assembly="hg38",
        contig_dictionary_sha256=contigs,
        source_url="https://example.invalid/reference",
        declared_license_or_terms="synthetic fixture only",
    )

    def wig(role: str, marker: str) -> WigGridBinding:
        return WigGridBinding(
            role=role,
            identity=_artifact(f"asset.{role}", marker),
            assembly="hg38",
            contig_dictionary_sha256=contigs,
            span_bp=1_000_000,
            step_bp=1_000_000,
            bin_size_bp=1_000_000,
            canonical_bin_definition_sha256=grid.bin_definition_sha256,
            source_url=f"https://example.invalid/{role}",
            declared_license_or_terms="synthetic fixture only",
        )

    raw = wig("raw_counts_wig", "2")
    pon = (
        PanelOfNormalsBinding(
            identity=_artifact("asset.pon", "5"),
            assembly="hg38",
            contig_dictionary_sha256=contigs,
            bin_size_bp=1_000_000,
            canonical_bin_definition_sha256=grid.bin_definition_sha256,
            donor_authorization_id="synthetic-donors",
            source_url="https://example.invalid/pon",
            declared_license_or_terms="synthetic fixture only",
        )
        if with_pon
        else None
    )
    assets = CnvAssetSet(
        reference=reference,
        raw_counts=raw,
        gc=wig("gc_wig", "3"),
        mappability=wig("map_wig", "4"),
        centromere=CentromereTableBinding(
            identity=_artifact("asset.centromere", "6"),
            assembly="hg38",
            contig_dictionary_sha256=contigs,
            interval_set_sha256="7" * 64,
            source_url="https://example.invalid/centromere",
            declared_license_or_terms="synthetic fixture only",
        ),
        panel_of_normals=pon,
        canonical_grid=grid,
    )
    policy = CountingPolicy(
        minimum_mapping_quality=20,
        exclude_unmapped=True,
        exclude_secondary=True,
        exclude_supplementary=True,
        exclude_qc_failure=True,
        exclude_duplicate=True,
        contigs=("chr1",),
        terminal_bin_policy="exclude_partial",
    )
    input_bam = _artifact("sample.bam", "8")
    input_bai = _artifact("sample.bam.bai", "9")
    return CnvRunRequest(
        sample_id="sample",
        input_bam=input_bam,
        input_bai=input_bai,
        runtime=RuntimeBinding(
            target="preparation_only",
            operating_system="Darwin",
            architecture="arm64",
            components=(
                _component("ichorCNA", ICHOR_COMMIT, "a"),
                _component("HMMcopy", HMMCOPY_COMMIT, "b"),
            ),
            ichor_script_sha256="a" * 64,
        ),
        assets=assets,
        counting_policy=policy,
        raw_wig_lineage=RawWigLineage(
            source_bam_sha256=input_bam.content_sha256,
            source_bai_sha256=input_bai.content_sha256,
            counter_implementation_sha256="e" * 64,
            counting_policy_sha256=contract_sha256(policy),
            wig_content_sha256=raw.identity.content_sha256,
            canonical_bin_definition_sha256=grid.bin_definition_sha256,
        ),
        read_count_source="precomputed_bound_wig",
        pon_mode=("protocol_matched_frozen" if with_pon else "none_development"),
        parameters=IchorParameterSet(
            chromosomes=(1,),
            normal_fraction_starts=(0.95, 0.99, 0.995, 0.999),
            ploidy_starts=(2.0,),
            max_copy_number=3,
            include_subclonal_states=False,
            lambda_policy="automatic",
            lambda_values=None,
            minimum_map_score=0.9,
            centromere_flank_bp=100_000,
            transition_probability=0.9999,
            transition_strength=10_000,
            minimum_segment_bins=50,
            altered_fraction_threshold=0.05,
        ),
    )


def _fixture(tmp_path: Path, name: str) -> Path:
    return Path(shutil.copytree(FIXTURES / name, tmp_path / name))


def test_prepare_is_deterministic_and_separates_authorities() -> None:
    request = _request()
    first = prepare_ichor_run(request)
    second = prepare_ichor_run(request)

    assert first == second
    assert first.request_sha256 == contract_sha256(request)
    assert first.development_input_authorized is True
    assert first.product_release_authorized is False
    assert first.qualification_established is False
    assert first.network == "none"
    assert first.pull_policy == "never"
    assert first.argv[:2] == (
        "Rscript",
        "/runtime/ichorCNA/scripts/runIchorCNA.R",
    )
    assert "--normalPanel" not in first.argv
    assert first.argv[first.argv.index("--lambda") + 1] == "NULL"
    capabilities = {item.role: item.availability for item in first.output_capabilities}
    assert capabilities["combined_corrected_depth"] == "directly_emitted"
    assert capabilities["raw_counts"] == "required_input"
    assert capabilities["gc_only_corrected"] == "not_emitted_by_pinned_upstream"
    assert capabilities["map_only_corrected"] == "not_emitted_by_pinned_upstream"
    assert capabilities["pon_residual"] == "not_emitted_by_pinned_upstream"

    changed_argv = first.model_dump(mode="python")
    changed_argv["argv"] = (*changed_argv["argv"], "--estimatePloidy", "FALSE")
    with pytest.raises(ValueError, match="argv does not match"):
        PreparedIchorRun.model_validate(changed_argv)

    changed_outputs = first.model_dump(mode="python")
    changed_outputs["output_capabilities"][0]["role"] = "invented_output"
    with pytest.raises(ValueError, match="capabilities do not match"):
        PreparedIchorRun.model_validate(changed_outputs)


def test_role_specific_coordinates_lineage_and_pon_are_bound() -> None:
    request = _request(with_pon=True)
    prepared = prepare_ichor_run(request)

    assert request.assets.reference.coordinate_semantics == "sequence"
    assert request.assets.raw_counts.native_coordinates == "one_based_fixed_step"
    assert request.assets.centromere.required_columns == (
        "Chr",
        "Start",
        "End",
        "GapType",
    )
    assert request.assets.centromere.native_coordinates == "one_based_closed_granges"
    assert request.assets.panel_of_normals is not None
    assert request.assets.panel_of_normals.native_coordinates == "one_based_closed"
    assert "--normalPanel" in prepared.argv

    changed = request.model_dump(mode="python")
    changed["counting_policy"]["minimum_mapping_quality"] = 30
    with pytest.raises(ValueError, match="counting policy"):
        CnvRunRequest.model_validate(changed)

    no_pon = request.model_dump(mode="python")
    no_pon["assets"]["panel_of_normals"] = None
    with pytest.raises(ValueError, match="PoN mode"):
        CnvRunRequest.model_validate(no_pon)

    swapped_role = request.model_dump(mode="python")
    swapped_role["assets"]["raw_counts"]["role"] = "gc_wig"
    with pytest.raises(ValueError, match="raw-count asset role"):
        CnvRunRequest.model_validate(swapped_role)

    non_primary = request.model_dump(mode="python")
    non_primary["counting_policy"]["exclude_secondary"] = False
    non_primary["raw_wig_lineage"]["counting_policy_sha256"] = contract_sha256(
        CountingPolicy.model_validate(non_primary["counting_policy"])
    )
    with pytest.raises(ValueError, match="exclusions are fixed true"):
        CnvRunRequest.model_validate(non_primary)

    wrong_contigs = request.model_dump(mode="python")
    wrong_contigs["counting_policy"]["contigs"] = ("chr2",)
    wrong_contigs["raw_wig_lineage"]["counting_policy_sha256"] = contract_sha256(
        CountingPolicy.model_validate(wrong_contigs["counting_policy"])
    )
    with pytest.raises(ValueError, match="canonical grid order"):
        CnvRunRequest.model_validate(wrong_contigs)


def test_source_derived_formats_and_natural_contig_order() -> None:
    upstream = FIXTURES / "upstream"
    centromere_lines = (
        upstream / "GRCh38.centromere.first12.tsv"
    ).read_text().splitlines()
    assert centromere_lines[0].split("\t") == ["Chr", "Start", "End", "GapType"]
    contigs = tuple(line.split("\t", 1)[0] for line in centromere_lines[1:])
    assert contigs == tuple(f"chr{index}" for index in range(1, 13))
    wig_header = (upstream / "gc_hg38_1000kb.first10.wig").read_text().splitlines()[0]
    assert wig_header == "fixedStep chrom=chr1 start=1 step=1000000 span=1000000"

    bins = tuple(
        CanonicalBin(contig=contig, start=0, end=1_000_000)
        for contig in contigs
    )
    grid = CanonicalGrid(
        contig_order=contigs,
        bins=bins,
        bin_definition_sha256=contract_sha256(bins),
    )
    assert tuple(row.contig for row in grid.bins) == contigs


def test_parser_accepts_native_chr1_through_chr12_order(tmp_path: Path) -> None:
    contigs = tuple(f"chr{index}" for index in range(1, 13))
    bins = tuple(
        CanonicalBin(contig=contig, start=0, end=1_000_000)
        for contig in contigs
    )
    grid = CanonicalGrid(
        contig_order=contigs,
        bins=bins,
        bin_definition_sha256=contract_sha256(bins),
    )
    payload = _request().model_dump(mode="python")
    payload["assets"]["canonical_grid"] = grid.model_dump(mode="python")
    for role in ("raw_counts", "gc", "mappability"):
        payload["assets"][role]["canonical_bin_definition_sha256"] = (
            grid.bin_definition_sha256
        )
    payload["counting_policy"]["contigs"] = contigs
    policy = CountingPolicy.model_validate(payload["counting_policy"])
    payload["raw_wig_lineage"]["counting_policy_sha256"] = contract_sha256(policy)
    payload["raw_wig_lineage"]["canonical_bin_definition_sha256"] = (
        grid.bin_definition_sha256
    )
    payload["parameters"]["chromosomes"] = tuple(range(1, 13))
    request = CnvRunRequest.model_validate(payload)

    output = _fixture(tmp_path, "neutral")
    corrected_header = "chr\tstart\tend\tlog2_TNratio_corrected\n"
    (output / "sample.correctedDepth.txt").write_text(
        corrected_header
        + "".join(f"{contig}\t1\t1000000\t0\n" for contig in contigs)
    )
    segment_header = (
        "ID\tchrom\tstart\tend\tnum.mark\tseg.median.logR\t"
        "copy.number\tcall\tsubclone.status\n"
    )
    (output / "sample.seg.txt").write_text(
        segment_header
        + "".join(
            f"sample\t{contig}\t1\t1000000\t1\t0\t2\tNEUT\tFALSE\n"
            for contig in contigs
        )
    )

    result = validate_ichor_outputs(prepare_ichor_run(request), output)

    assert tuple(row.contig for row in result.corrected_bins) == contigs
    assert tuple(row.contig for row in result.segments) == contigs


def test_parse_neutral_fixture_returns_typed_insufficient_result(tmp_path: Path) -> None:
    prepared = prepare_ichor_run(_request())
    result = validate_ichor_outputs(prepared, _fixture(tmp_path, "neutral"))

    assert result.status == "insufficient_information"
    assert result.identifiability == "insufficient_altered_structure"
    assert result.selected_solution.model_fraction == 0
    assert result.selected_solution.matched_candidate_id == "n1.p2"
    assert len(result.candidates) == 2
    assert len(result.corrected_bins) == 4
    assert result.corrected_bins[0].start == 0
    assert result.corrected_bins[-1].end == 4_000_000
    assert len(result.artifacts) == 6
    assert result.product_release_authorized is False
    assert result.qualification_status == "development_unqualified"
    assert result.limitations[-1] == "No protocol-matched panel of normals was supplied."


def test_parse_arm_loss_preserves_candidates_and_descriptive_segment(
    tmp_path: Path,
) -> None:
    prepared = prepare_ichor_run(_request())
    result = validate_ichor_outputs(prepared, _fixture(tmp_path, "arm_loss"))

    assert result.status == "complete"
    assert result.identifiability == "not_assessed"
    assert result.selected_solution.model_fraction == 0.1
    assert result.selected_solution.matched_candidate_id == "n0.9.p2"
    assert [item.call for item in result.segments] == ["HETD", "NEUT"]
    assert result.segments[0].start == 0
    assert result.segments[0].end == 2_000_000
    assert [item.log_likelihood for item in result.candidates] == [-40, -45]


def test_candidate_na_is_typed_and_ambiguous_selected_match_is_not_overclaimed(
    tmp_path: Path,
) -> None:
    output = _fixture(tmp_path, "arm_loss")
    params = output / "sample.params.txt"
    text = params.read_text()
    text = text.replace("n0.9-p2\t0.9\t2\t80\t0\t0\t-40", "n0.9-p2\t0.9\t2\t80\tNA\tNaN\t-40")
    text = text.replace("n0.95-p2\t0.96\t2\t90\t0\t0\t-45", "n0.95-p2\t0.9\t2\t90\t0\t0\t-45")
    params.write_text(text)

    result = validate_ichor_outputs(prepare_ichor_run(_request()), output)

    assert result.candidates[0].fraction_genome_subclonal is None
    assert result.candidates[0].fraction_cna_subclonal is None
    assert result.selected_solution.matched_candidate_id is None
    assert result.selected_solution.selection_resolution == (
        "not_resolved_rounded_collision"
    )
    assert result.identifiability == "not_assessed"


def test_contradictory_selected_summary_is_rejected(tmp_path: Path) -> None:
    output = _fixture(tmp_path, "arm_loss")
    params = output / "sample.params.txt"
    params.write_text(params.read_text().replace("sample\t0.1\t2", "sample\t0.7\t2"))

    with pytest.raises(IchorOutputError, match="contradicts every"):
        validate_ichor_outputs(prepare_ichor_run(_request()), output)


def test_prespecified_mask_is_explicit_and_unexplained_omission_fails(
    tmp_path: Path,
) -> None:
    masked_output = _fixture(tmp_path, "neutral")
    corrected = masked_output / "sample.correctedDepth.txt"
    corrected.write_text(
        corrected.read_text().replace("chr1\t3000001\t4000000\t0.01\n", "")
    )
    segment = masked_output / "sample.seg.txt"
    segment.write_text(segment.read_text().replace("4000000\t4", "3000000\t3"))

    masked = validate_ichor_outputs(
        prepare_ichor_run(_request(mask_last=True)), masked_output
    )
    assert masked.bin_statuses[-1].status == "masked_prespecified"
    assert masked.bin_statuses[-1].mask_reason == "centromere_or_flank"
    assert len(masked.corrected_bins) == 3

    unexplained_output = _fixture(tmp_path, "arm_loss")
    path = unexplained_output / "sample.correctedDepth.txt"
    path.write_text(path.read_text().replace("chr1\t3000001\t4000000\t0.01\n", ""))
    with pytest.raises(IchorOutputError, match="unexplained missing bins"):
        validate_ichor_outputs(prepare_ichor_run(_request()), unexplained_output)


@pytest.mark.parametrize(
    ("mutation", "match"),
    (
        ("missing", "missing required outputs"),
        ("nan", "must be finite"),
        ("coordinate", "outside the canonical grid"),
        ("candidate", "candidate table is absent"),
        ("symlink", "regular non-symlink"),
        ("segment_count", "retained corrected bins"),
    ),
)
def test_parser_fails_closed_on_malformed_outputs(
    tmp_path: Path,
    mutation: str,
    match: str,
) -> None:
    output = _fixture(tmp_path, "arm_loss")
    if mutation == "missing":
        (output / "sample.seg").unlink()
    elif mutation == "nan":
        path = output / "sample.correctedDepth.txt"
        path.write_text(path.read_text().replace("-0.20", "NaN"))
    elif mutation == "coordinate":
        path = output / "sample.correctedDepth.txt"
        path.write_text(path.read_text().replace("chr1\t1\t1000000", "chr1\t2\t1000001"))
    elif mutation == "candidate":
        path = output / "sample.params.txt"
        lines = path.read_text().splitlines()
        path.write_text("\n".join(lines[:8]) + "\n")
    elif mutation == "symlink":
        path = output / "sample.RData"
        path.unlink()
        path.symlink_to(output / "sample.seg")
    elif mutation == "segment_count":
        path = output / "sample.seg.txt"
        path.write_text(
            path.read_text().replace(
                "sample\tchr1\t1\t2000000\t2\t",
                "sample\tchr1\t1\t2000000\t1\t",
            )
        )

    with pytest.raises(IchorOutputError, match=match):
        validate_ichor_outputs(prepare_ichor_run(_request()), output)


def test_runtime_requires_pinned_execution_dependencies() -> None:
    runtime = _request().runtime.model_dump(mode="python")
    runtime["target"] = "oci"
    with pytest.raises(ValidationError, match="exact R"):
        RuntimeBinding.model_validate(runtime)

    runtime["r_version"] = "3.6.3"
    runtime["package_lock_sha256"] = "f" * 64
    with pytest.raises(ValidationError, match="manifest digest"):
        RuntimeBinding.model_validate(runtime)
