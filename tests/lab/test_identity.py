"""D31 identities: canonical, sensitive to what matters, blind to what doesn't."""

from __future__ import annotations

import hashlib

import pytest

from traceback_lab import identity

A = "a" * 64
B = "b" * 64
C = "c" * 64


def _aligned(**overrides):
    base = dict(
        source_id=A,
        reference_sha256=B,
        mmi_sha256=C,
        minimap2_version="2.31-r1302",
        samtools_version="1.24",
        aligner_argv=["minimap2", "-ax", "map-ont", "-y"],
    )
    base.update(overrides)
    return identity.aligned_input_id(**base)


def test_canonical_sha256_is_key_order_and_whitespace_free() -> None:
    expected = hashlib.sha256(b'{"a":1,"b":[1,2]}').hexdigest()
    assert identity.canonical_sha256({"b": [1, 2], "a": 1}) == expected


def test_source_id_depends_on_chunk_order_and_exclusions() -> None:
    first = identity.source_id_for_chunks([A, B])
    assert first != identity.source_id_for_chunks([B, A])
    assert first != identity.source_id_for_chunks([A, B], excluded=[3])
    # exclusions are a set: order and repeats do not matter
    assert identity.source_id_for_chunks([A], [2, 1, 2]) == identity.source_id_for_chunks([A], [1, 2])
    with pytest.raises(ValueError):
        identity.source_id_for_chunks([])
    with pytest.raises(ValueError):
        identity.source_id_for_chunks(["not-a-digest"])


def test_aligned_bam_is_its_own_source() -> None:
    assert identity.source_id_for_aligned_bam(A) == A


def test_aligner_version_changes_input_id() -> None:
    assert _aligned() != _aligned(minimap2_version="2.28-r1209")
    assert _aligned() != _aligned(samtools_version="1.20")
    assert _aligned() != _aligned(mmi_sha256=A)
    assert _aligned() != _aligned(aligner_argv=["minimap2", "-ax", "map-ont"])
    assert _aligned() == _aligned()


def test_prealigned_differs_from_lab_aligned() -> None:
    assert identity.prealigned_input_id(source_id=A, reference_sha256=B) != _aligned()


def test_platform_separates_input_result_and_cohort_identities() -> None:
    old = identity.prealigned_input_id(source_id=A, reference_sha256=B)
    assert old == identity.prealigned_input_id(source_id=A, reference_sha256=B, platform="unknown")
    assert old != identity.prealigned_input_id(source_id=A, reference_sha256=B, platform="illumina")
    assert identity.parameters_sha256(**_params()) != identity.parameters_sha256(
        **_params(), platform="illumina",
    )
    shared = dict(algorithm_id="depth", algorithm_version="1", parameters={}, tools=[],
                  reference_sha256=B, aligner_identity={"kind": "pre-aligned"})
    assert identity.cohort_compat_key(**shared, platform="ont") != identity.cohort_compat_key(
        **shared, platform="illumina",
    )


def test_derived_input_ids_differ_by_transform_parameters_and_salt() -> None:
    parent = _aligned()
    half = identity.derived_input_id(
        parent=parent, transform="downsample", parameters={"fraction": 0.5}, salt="ds-v1"
    )
    assert half != identity.derived_input_id(
        parent=parent, transform="downsample", parameters={"fraction": 0.25}, salt="ds-v1"
    )
    assert half != identity.derived_input_id(
        parent=parent, transform="split", parameters={"half": "A"}, salt="split-v1"
    )
    with pytest.raises(ValueError):
        identity.derived_input_id(parent=parent, transform="bogus", parameters={}, salt="x")
    with pytest.raises(ValueError):
        identity.derived_input_id(parent=parent, transform="split", parameters={}, salt="")


@pytest.mark.parametrize("value", [0.5, 0.05, 0.125, 1e-05])
def test_valid_fractions(value: float) -> None:
    assert identity.validate_fraction(value) == value


@pytest.mark.parametrize("value", [0, 1, -0.1, 1.5, float("nan")])
def test_invalid_fractions(value: float) -> None:
    with pytest.raises(ValueError):
        identity.validate_fraction(value)


def test_derived_labels_use_repr() -> None:
    assert identity.derived_label("3f9a1c2b7e04", "downsample", {"fraction": 1e-05}) == "3f9a1c2b7e04@1e-05"
    assert identity.derived_label("3f9a1c2b7e04", "split", {"half": "B"}) == "3f9a1c2b7e04@split-B"
    assert identity.derived_label("3f9a1c2b7e04", "cna", {"tf": 0.1}) == "3f9a1c2b7e04@cna-0.1"


def _params(**overrides):
    base = dict(
        algorithm_id="copy-number",
        algorithm_version="1",
        parameters={"bin_kb": 1000, "pon_policy": "lab_loo_v1"},
        tools=[{"name": "ichorCNA", "version": "0.2.0"}],
        reference_sha256=B,
        asset_sha256s=[C],
    )
    base.update(overrides)
    return base


def test_loo_pon_changes_execution_identity_but_not_compat_key() -> None:
    first = identity.parameters_sha256(**_params(asset_sha256s=[C]))
    second = identity.parameters_sha256(**_params(asset_sha256s=[A]))
    assert first != second
    compat = dict(
        algorithm_id="copy-number",
        algorithm_version="1",
        tools=[{"name": "ichorCNA", "version": "0.2.0"}],
        reference_sha256=B,
        aligner_identity={"minimap2": "2.31", "samtools": "1.24"},
        pon_policy="lab_loo_v1",
        per_sample_asset_keys=["pon_sha256"],
    )
    key_one = identity.cohort_compat_key(parameters={"bin_kb": 1000, "pon_sha256": C}, **compat)
    key_two = identity.cohort_compat_key(parameters={"bin_kb": 1000, "pon_sha256": A}, **compat)
    assert key_one == key_two
    assert key_one != identity.cohort_compat_key(
        parameters={"bin_kb": 1000, "pon_sha256": C}, **{**compat, "pon_policy": "none"}
    )
    assert key_one != identity.cohort_compat_key(
        parameters={"bin_kb": 1000, "pon_sha256": C},
        **{**compat, "aligner_identity": {"minimap2": "2.28", "samtools": "1.24"}},
    )


def test_parameters_sha256_ignores_tool_and_asset_order() -> None:
    left = identity.parameters_sha256(
        **_params(
            tools=[{"name": "a", "version": "1"}, {"name": "b", "version": "2"}],
            asset_sha256s=[A, C],
        )
    )
    right = identity.parameters_sha256(
        **_params(
            tools=[{"name": "b", "version": "2"}, {"name": "a", "version": "1"}],
            asset_sha256s=[C, A],
        )
    )
    assert left == right


def test_upstream_enters_execution_identity() -> None:
    plain = identity.parameters_sha256(**_params())
    chained = identity.parameters_sha256(
        **_params(upstream=[{"algorithm_id": "read-summary", "parameters_sha256": A}])
    )
    assert plain != chained


def test_bad_algorithm_id_is_refused() -> None:
    with pytest.raises(ValueError):
        identity.parameters_sha256(**_params(algorithm_id="Bad_ID"))


def test_shortest_unique_prefix_grows_on_collision() -> None:
    one = "3f9a1c2b7e04" + "0" * 52
    two = "3f9a1c2b7e04" + "1" * 52
    three = "ffff" + "2" * 60
    prefixes = identity.shortest_unique_prefixes([one, two, three])
    assert prefixes[one] == "3f9a1c2b7e040"
    assert prefixes[two] == "3f9a1c2b7e041"
    assert prefixes[three] == "ffff22222222"
    assert all(len(value) >= 12 for value in prefixes.values())


def test_resolve_prefix_states() -> None:
    one = "3f9a1c2b7e04" + "0" * 52
    two = "3f9a1c2b7e04" + "1" * 52
    ids = [one, two]
    assert identity.resolve_prefix("3f9a1c2b7e04", ids).state == "ambiguous"
    assert identity.resolve_prefix("3f9a1c2b7e04", ids).candidates == (one, two)
    assert identity.resolve_prefix("3f9a1c2b7e040", ids).value == one
    assert identity.resolve_prefix("aaaaaaaaaaaa", ids).state == "missing"
    for bad in ("3f9a1c2b7e0", "../../etc", "3F9A1C2B7E04", "latest"):
        assert identity.resolve_prefix(bad, ids).state == "invalid"
