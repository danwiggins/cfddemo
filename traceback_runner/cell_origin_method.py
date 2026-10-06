"""The locked cell-origin method (signal methods CO2): parameters and definition.

``mth_cell_origin_loyfer_uxm`` (family ``cell_origin``) estimates fractions
among the registered Loyfer U250 atlas contributors from fragment-level UXM
counts.  Every result is unqualified, local, not for clinical use and
descriptive only.

Every input that can change a number enters the E01 method definition, so it
enters the method hash, the job's workflow hash and the record identity
(spec §3):

- ``tools[]``: modkit's package-level digest (the conda package sha256 from
  the committed lock; the per-install binary digest is recorded in each
  record's provenance, not here, because it depends on the install path);
- ``assets[]``: the registered reference FASTA and the three registered
  Loyfer assets, by their registration digests;
- ``parameter_schema_sha256``: the SHA-256 of the canonical
  :class:`CellOriginParametersV1` JSON, which also carries the operator's
  ``--modbase-model`` declaration (SH4: a setting is part of the method).

Threat model: in-process code mutation is out of scope.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import Field, StringConstraints

from evidence_inspector.method_registry import (
    AssetReference,
    MethodDefinition,
    MethodFamily,
    ToolReference,
)

from .contracts import RegisteredReference, RunnerContract
from .references import LOYFER_DIRECTORY_FILES, AssetKind, RegisteredAsset
from .serialization import canonical_json_bytes

METHOD_ID = "mth_cell_origin_loyfer_uxm"
METHOD_SLUG = "cell-origin-loyfer-uxm"
QUANTITY_ID = "qty_cell_origin_atlas_contributor_fraction"
UNIT = "unit_fraction"
TOOL_ID = "tool_modkit"
ASSET_VERSION = "1.0.0"
PARAMETERS_SCHEMA = "traceback.cell-origin-parameters.v1"

# Parameters whose value is today's behaviour but which the scientist has not
# signed off (spec §8).  Changing one changes the method hash; nothing reads
# this tuple, it is the in-code flag a reviewer greps for.
PENDING_SCIENTIST_SIGNOFF = (
    # Q3: ``sqrt_count`` reproduces the 2026-10-01 re-creation; the code's own
    # note says ``reference_count`` reproduces the reference implementation.
    "nnls_row_scale",
    # CO3: conservative synthetic-data floors, confirmed at gate G1.
    "min_classified_fragments",
    "min_observed_markers",
)

# Floors below which a record is refused (TBX-METH-004).  Set from synthetic
# data only, never from a real sample; the scientist confirms them at G1.
# The synthetic study (39 contributors, 1,500 single-target markers, a
# six-contributor mixture led by 0.55, 30 seeds per depth, sqrt_count NNLS):
#
#   classified fragments   200   400   800  1600
#   observed markers (med) 187   350   620   982
#   total variation (med) 0.57  0.48  0.33  0.24
#   top-contributor error 0.21  0.16  0.08  0.08
#
# 800 classified fragments is the first depth whose median error on the
# largest contributor is under 0.10; the marker floor sits below the 620
# markers that depth reached, because a smaller atlas observes fewer.
DEFAULT_MIN_CLASSIFIED_FRAGMENTS = 800
DEFAULT_MIN_OBSERVED_MARKERS = 500

# Placeholders for the paths in the locked modkit argv (the job fills them).
ARGV_REFERENCE = "{reference_fasta}"
ARGV_REGIONS = "{loyfer_regions}"
ARGV_MODBAM = "{prefiltered_modbam}"

ModelId = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._@+-]{0,127}$")]


class CellOriginCapsV1(RunnerContract):
    """Bounds on the work one record may do; a hit refuses the record."""

    maximum_calls: int = Field(ge=1)
    maximum_groups: int = Field(ge=1)
    maximum_cpgs_per_group: int = Field(ge=1)


class CellOriginParametersV1(RunnerContract):
    """Every locked parameter of the cell-origin method (spec §3.1 and §11).

    Canonical JSON of this model is hashed into ``parameter_schema_sha256``.
    """

    schema_version: Literal["traceback.cell-origin-parameters.v1"] = PARAMETERS_SCHEMA
    # modkit: the exact argv after the executable, paths as placeholders, with
    # the explicit filter threshold (never modkit's per-run estimate).
    modkit_subcommand: Literal["extract calls"] = "extract calls"
    modkit_arguments: tuple[str, ...] = Field(min_length=1)
    modkit_filter_threshold: float = Field(gt=0.0, lt=1.0)
    # m and h calls are both counted as "methylated".
    modification_codes: tuple[str, ...] = Field(min_length=1)
    modification_collapse: Literal["methylated"] = "methylated"
    uxm_min_cpgs: int = Field(ge=1)
    u_max_exclusive: float = Field(gt=0.0, lt=1.0)
    m_min_inclusive: float = Field(gt=0.0, le=1.0)
    # Q10: a pysam pre-filter before modkit, in the fragment policy's order.
    prefilter_policy: Literal["pysam-before-modkit.v1"] = "pysam-before-modkit.v1"
    prefilter_scope: Literal["alignments_overlapping_marker_regions"] = (
        "alignments_overlapping_marker_regions"
    )
    min_mapq: int = Field(ge=0)
    excluded_alignment_reasons: tuple[str, ...] = Field(min_length=1)
    # Q3: pending scientist sign-off (see PENDING_SCIENTIST_SIGNOFF).
    nnls_row_scale: Literal["sqrt_count", "reference_count", "unweighted"] = "sqrt_count"
    nnls_tolerance: float = Field(gt=0.0)
    nnls_max_iterations: int = Field(ge=1)
    nnls_solver_id: str = Field(min_length=1, max_length=96)
    normalization: Literal["weights_rescaled_to_sum_1_no_unassigned_compartment"] = (
        "weights_rescaled_to_sum_1_no_unassigned_compartment"
    )
    bootstrap_replicates: int = Field(ge=2)
    bootstrap_random_seed: int = Field(ge=0)
    bootstrap_confidence_level: float = Field(gt=0.0, lt=1.0)
    caps: CellOriginCapsV1
    cap_policy: Literal["refuse"] = "refuse"
    min_classified_fragments: int = Field(ge=1)
    min_observed_markers: int = Field(ge=1)
    atlas_contributor_set: Literal["all_registered_atlas_columns"] = (
        "all_registered_atlas_columns"
    )
    reference_range_comparison: Literal["excluded"] = "excluded"
    # SH4: the operator's --modbase-model, or None when the BAM header
    # declares the model.  A new declaration is a new method, so a new job.
    modbase_model_declared: ModelId | None = None


def default_parameters(*, modbase_model: str | None = None) -> CellOriginParametersV1:
    """The locked parameters, read from the code that applies each of them."""

    # Imported here: the pipeline pulls in numpy, and the CLI imports this
    # module on every start.
    from evidence_inspector.cell_origin_models import (
        UXM_METHYLATED_MIN_INCLUSIVE,
        UXM_MINIMUM_CPGS,
        UXM_UNMETHYLATED_MAX_EXCLUSIVE,
    )
    from evidence_inspector.cell_origin_pipeline import (
        DEFAULT_BOOTSTRAP_REPLICATES,
        DEFAULT_MAXIMUM_CALLS,
        DEFAULT_MAXIMUM_CPGS_PER_GROUP,
        DEFAULT_MAXIMUM_GROUPS,
        DEFAULT_MODKIT_FILTER_THRESHOLD,
        DEFAULT_RANDOM_SEED,
        modkit_extract_arguments,
    )
    from evidence_inspector.cell_origin_prefilter import DEFAULT_MIN_MAPQ, PREFILTER_REASONS
    from evidence_inspector.deconvolution import (
        DEFAULT_TOLERANCE,
        NNLS_SOLVER_IMPLEMENTATION_ID,
    )
    from evidence_inspector.uxm import METHYLATED_MODIFICATION_CODES

    return CellOriginParametersV1(
        modkit_arguments=modkit_extract_arguments(
            reference_fasta=Path(ARGV_REFERENCE),
            include_bed=Path(ARGV_REGIONS),
            filter_threshold=DEFAULT_MODKIT_FILTER_THRESHOLD,
            modbam=Path(ARGV_MODBAM),
        ),
        modkit_filter_threshold=DEFAULT_MODKIT_FILTER_THRESHOLD,
        modification_codes=tuple(sorted(METHYLATED_MODIFICATION_CODES)),
        uxm_min_cpgs=UXM_MINIMUM_CPGS,
        u_max_exclusive=UXM_UNMETHYLATED_MAX_EXCLUSIVE,
        m_min_inclusive=UXM_METHYLATED_MIN_INCLUSIVE,
        min_mapq=DEFAULT_MIN_MAPQ,
        excluded_alignment_reasons=tuple(reason.value for reason in PREFILTER_REASONS),
        nnls_row_scale="sqrt_count",
        nnls_tolerance=DEFAULT_TOLERANCE,
        nnls_max_iterations=10_000,
        nnls_solver_id=NNLS_SOLVER_IMPLEMENTATION_ID,
        bootstrap_replicates=DEFAULT_BOOTSTRAP_REPLICATES,
        bootstrap_random_seed=DEFAULT_RANDOM_SEED,
        bootstrap_confidence_level=0.95,
        caps=CellOriginCapsV1(
            maximum_calls=DEFAULT_MAXIMUM_CALLS,
            maximum_groups=DEFAULT_MAXIMUM_GROUPS,
            maximum_cpgs_per_group=DEFAULT_MAXIMUM_CPGS_PER_GROUP,
        ),
        min_classified_fragments=DEFAULT_MIN_CLASSIFIED_FRAGMENTS,
        min_observed_markers=DEFAULT_MIN_OBSERVED_MARKERS,
        modbase_model_declared=modbase_model,
    )


def parameters_sha256(parameters: CellOriginParametersV1) -> str:
    """``parameter_schema_sha256``: SHA-256 of the canonical parameter JSON."""

    return hashlib.sha256(canonical_json_bytes(parameters)).hexdigest()


def _reference_asset(reference: RegisteredReference) -> AssetReference:
    from .local_authority import LOCAL_ASSET_VERSION, _local_asset_id

    # The same asset reference the fragment method binds, so both records of
    # one BAM name the registered FASTA identically.
    return AssetReference(
        asset_id=_local_asset_id(reference.reference_id),
        version=LOCAL_ASSET_VERSION,
        content_sha256=reference.asset_sha256,
    )


def cell_origin_method_definition(
    reference: RegisteredReference,
    assets: Mapping[AssetKind, RegisteredAsset],
    tool: Any,
    parameters: CellOriginParametersV1,
) -> MethodDefinition:
    """The locked E01 definition for one registered reference.

    ``assets`` holds the registration of each Loyfer kind; ``tool`` is the
    platform's modkit ``ToolPin``.  The registered reference sorts first in
    ``assets[]``, so the catalog reads it as the reference asset.
    """

    from .local_authority import local_method_version

    missing = set(LOYFER_DIRECTORY_FILES) - set(assets)
    if missing:
        raise ValueError("every Loyfer asset kind must be registered")
    loyfer = []
    for kind in LOYFER_DIRECTORY_FILES:
        registered = assets[kind]
        if registered.kind is not kind:
            raise ValueError("an asset registration names another kind")
        loyfer.append(
            AssetReference(
                asset_id=registered.asset_id,
                version=ASSET_VERSION,
                content_sha256=registered.file_sha256,
            )
        )
    bindings = sorted(
        (_reference_asset(reference), *loyfer),
        key=lambda item: (item.asset_id, item.version),
    )
    return MethodDefinition(
        method_id=METHOD_ID,
        version=local_method_version(reference.reference_id),
        family=MethodFamily.CELL_ORIGIN,
        quantity_id=QUANTITY_ID,
        unit=UNIT,
        parameter_schema_sha256=parameters_sha256(parameters),
        tools=(
            ToolReference(
                tool_id=TOOL_ID, version=tool.version, artifact_sha256=tool.package_sha256
            ),
        ),
        assets=tuple(bindings),
    )


def registered_loyfer_assets(root: Path) -> dict[AssetKind, RegisteredAsset]:
    """The registration of each Loyfer kind under its fixed ID.

    A missing registration raises TBX-ASSET-004 with the exact register
    command.  Registration records are read, not re-hashed: every job hashes
    its own copy of each file before using it (``copy_registered_asset``).
    """

    from .references import load_asset

    return {
        kind: load_asset(root, asset_id, kind=kind).registered
        for kind, (_, asset_id) in LOYFER_DIRECTORY_FILES.items()
    }


def cell_origin_definition_at(
    root: Path,
    reference: RegisteredReference,
    config: Mapping[str, str],
    *,
    platform_name: str | None = None,
) -> MethodDefinition:
    """The definition ``run --analysis cell-origin`` binds under ``root``.

    ``config`` holds the resolved settings (``modbase_model`` when declared).
    """

    from .toolchain import pin_for

    return cell_origin_method_definition(
        reference,
        registered_loyfer_assets(root),
        pin_for("modkit", platform_name),
        default_parameters(modbase_model=config.get("modbase_model")),
    )


__all__ = [
    "ARGV_MODBAM",
    "ARGV_REFERENCE",
    "ARGV_REGIONS",
    "DEFAULT_MIN_CLASSIFIED_FRAGMENTS",
    "DEFAULT_MIN_OBSERVED_MARKERS",
    "METHOD_ID",
    "METHOD_SLUG",
    "PENDING_SCIENTIST_SIGNOFF",
    "QUANTITY_ID",
    "TOOL_ID",
    "UNIT",
    "CellOriginCapsV1",
    "CellOriginParametersV1",
    "cell_origin_definition_at",
    "cell_origin_method_definition",
    "default_parameters",
    "parameters_sha256",
    "registered_loyfer_assets",
]
