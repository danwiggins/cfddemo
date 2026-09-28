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
    CanonicalGrid,
    CnvAssetSet,
    CnvRunRequest,
    CountingPolicy,
    ExclusionBedBinding,
    ExternalComponentBinding,
    IchorOutputError,
    IchorParameterSet,
    PanelOfNormalsBinding,
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


def _grid() -> CanonicalGrid:
    bins = tuple(
        CanonicalBin(
            contig="chr1",
            start=index * 1_000_000,
            end=(index + 1) * 1_000_000,
        )
        for index in range(4)
    )
    return CanonicalGrid(bins=bins, bin_definition_sha256=contract_sha256(bins))


def _request(*, with_pon: bool = False) -> CnvRunRequest:
    grid = _grid()
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
        exclusion=ExclusionBedBinding(
            identity=_artifact("asset.exclusion", "6"),
            assembly="hg38",
            contig_dictionary_sha256=contigs,
            interval_set_sha256="7" * 64,
            source_url="https://example.invalid/exclusion",
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


def test_role_specific_coordinates_lineage_and_pon_are_bound() -> None:
    request = _request(with_pon=True)
    prepared = prepare_ichor_run(request)

    assert request.assets.reference.coordinate_semantics == "sequence"
    assert request.assets.raw_counts.native_coordinates == "one_based_fixed_step"
    assert request.assets.exclusion.native_coordinates == "zero_based_half_open"
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
    assert result.identifiability == "competitive_solutions"


@pytest.mark.parametrize(
    ("mutation", "match"),
    (
        ("missing", "missing required outputs"),
        ("nan", "must be finite"),
        ("coordinate", "outside the canonical grid"),
        ("candidate", "candidate table is absent"),
        ("symlink", "regular non-symlink"),
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
