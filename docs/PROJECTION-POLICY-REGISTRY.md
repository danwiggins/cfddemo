# Protected E12 projection-policy registry

Status: local synthetic persistence contract. It does not authorize real
provider operation, clinical use, scientific claims, export, or Epic D release.

`ProjectionPolicyRegistry` (`evidence_inspector/projection_policy_registry.py`)
is the protected `projection_policy_registry` that the E12 builder requires (see
`docs/E12-INTEGRATION-PLAN.md`, "Closed source-value projection family"). It
stores closed source-value projection policies behind an opaque selector and
version. It does not implement the E07/E08/E09 family value adapters, and it
never reads an artifact value.

## The policy contract

`ProjectionPolicy` is a discriminated union on `family`. Each member is a
versioned, closed model (`extra="forbid"`, frozen):

| Family | Model | Coordinate scheme | Scheme-level binding | Component | Statistics (unit) |
| --- | --- | --- | --- | --- | --- |
| `fragment` (E07) | `FragmentProjectionPolicyV1` | `e07_panel_chart_bin.v1` | one `PanelId` (`a`/`b`) and one `FragmentQuantity` | `bin_index` into the verified E02 chart rows, exact `lower_inclusive`, `upper_exclusive` (null only for the unbounded final bin) | `count` (`unit_alignment_count`), `fraction` (`unit_fraction`) |
| `cell_origin` (E08) | `CellOriginProjectionPolicyV1` | `e08_registered_atlas_contributor.v1` | `atlas_id`, `atlas_sha256` | registered `contributor_id` | `estimated_fraction` (`unit_fraction`) |
| `cna_chromosome` (E09 `dosage_qc`) | `CnaChromosomeProjectionPolicyV1` | `e09_dosage_qc_chromosome.v1` | the exact `dosage_qc` `CoordinateGridLayer` and its digest | `chr1`..`chr22`, declared by the grid | `accepted_read_count` (`unit_read_count`), `relative_diploid_dosage` (`unit_relative_dosage`), `log2_ratio` (`unit_log2_ratio`) |
| `cna_segment` (E09 `segmented_cna`) | `CnaSegmentProjectionPolicyV1` | `e09_segmented_cna_segment.v1` | the exact `segmented_cna` `CoordinateGridLayer` and its digest | `segment_index`, `contig`, zero-based half-open `start`/`end` | `median_log2` (`unit_log2_ratio`), `upstream_copy_number` (`unit_copy_number`), `retained_bin_count` and `native_span_bin_count` (`unit_bin_count`) |

The panel IDs, fragment quantities, quantity-ID table, bin bound, contributor
bound and privacy check, grid model, and `CnaSource` values are imported from
the E07/E08/E09 modules, not restated. Every CNA statistic is an existing
numeric field of the E09 `DosageChromosomeLayer` or `SegmentLayer`; categorical
fields (`dosage_direction`, `upstream_call`, `subclone_status`) are not
statistics.

Every policy also carries:

- `policy_id` (`projpol_…`) and `version`;
- `measurement`: a `ProjectionMeasurementBinding` holding the full E01
  `MethodDefinition` and the exact D02 tuple (method reference, E01 method
  definition digest, quantity ID, unit, and the D02
  `measurement_definition_sha256` over those four). The validator recomputes
  the E01 digest and the D02 digest, and requires the quantity and unit to be
  the definition's own;
- `measurement_anchor`: the D05 `MeasurementAnchor`. Its
  `measurement_definition_sha256` must equal the E01 method-definition digest.
  This is the D06 import rule (`cohort_import.py`): despite its name, the D05
  field holds the E01 method digest, not the D02 digest;
- the selection rule and its components or statistics.

Family-specific checks:

- the E01 `MethodFamily` must match: `fragment_measurement`, `cell_origin`, or
  `copy_number` for both CNA families;
- fragment: the D02 quantity ID must be E07's ID for the policy's
  `FragmentQuantity`, and the unit must be `unit_bp`;
- fragment bins: one index names one boundary pair; bins are ordered by index
  and disjoint; adjacent indices must be contiguous
  (`upper_exclusive == next.lower_inclusive`); only the highest bin may be
  unbounded;
- CNA: the grid's `source` must be the family's E09 source, and
  `coordinate_grid_sha256` must equal `cna_coordinate_grid_sha256(grid)`, which
  uses E09's canonical encoding. The grid's `source` is part of the digest, so a
  dosage grid and a segmented grid with identical contigs and bins have
  different digests;
- segments: one index names one interval; segments follow grid contig order
  without overlap; contigs must be declared by the grid;
- components are uniquely sorted by coordinate, then statistic order, and each
  component's `statistic_unit` must be the statistic's controlled unit.

Dosage and segment coordinates never alias: they are different families with
different component models (a whole chromosome versus an indexed interval),
different grid sources, and different digests. A component, family literal,
scheme, or source copied from one into the other fails validation.

### Selection rules

`ProjectionSelectionRule` has exactly two members:

- `finite_components`: one or more explicit components and no statistics list;
- `canonical_all_components`: a canonical, non-empty statistics list and no
  components. The adapter must emit the complete bounded vector in canonical
  order and reject a caller subset. Canonical order is chart-row index for
  fragment, contributor ID (lexicographic) for cell origin, `chr1`..`chr22` for
  dosage, and segment index for segments. For cell origin this is deliberately
  not E08's display rank, which orders rows by estimate.

No rule, statistic, or field can express top, largest, most-changed,
minimum/maximum, or any other value-ranked choice:

- the rule and statistic vocabularies are closed enums, and extra fields are
  rejected;
- every coordinate is matched by exact equality, never by value;
- an import-time guard fails the module if a rule, statistic, or unit member is
  added whose words include a ranking word (`top`, `max`, `min`, `largest`,
  `most`, `rank`, …) or that has no controlled unit.

Upstream-registered identifiers (E08 contributor and atlas IDs, E09 contigs)
are not word-checked. They are biology vocabulary matched exactly, so a word
check could only reject real names such as `BEST4_enterocyte`; a contributor
whose name contains a ranking word is still one named contributor.

## Registration and versions

`register_policy(policy)` is the protected operator path. It accepts only an
exact instance of one family model. It captures the instance as bounded
canonical bytes without invoking caller hooks and revalidates those bytes, so a
`model_construct` forgery, subclass, private/extra state, dict, bytes or digest
is rejected. It then binds the policy to this registry's ID and epoch in a
`RegisteredProjectionPolicyObject` and publishes it.

The selector `projection_policy_…` is derived from the registry epoch and
`policy_id`. Version `N` requires versions `1..N-1` of that selector, a version
number is never reused with other content, and every version of one selector
keeps the same family and coordinate scheme. Older versions stay resolvable;
nothing is superseded or retired. Exact re-registration is idempotent.

## Resolution

`resolve(selector_id, policy_version)` takes only the opaque selector and an
integer version. Under the shared registry lock it loads the committed state,
finds exactly one object, builds a `ResolvedProjectionPolicy` (registry ID,
epoch, state version and head, selector, version, object digest, registered
policy digest, and the policy), and re-reads the journal immediately before
return. If the head changed, it raises `ProjectionPolicyRegistryUnsafe`. The
result's validator re-derives the selector from the policy ID and checks the
version and policy digest.

### No live authority replay

The registry replays nothing against live authority, and
`ResolvedProjectionPolicy.live_authority_replayed` is literally `false`. The
candidates were considered and rejected:

- D05: a measurement anchor is not a registered entity. It is a field of each
  immutable D05 manifest. A policy is chosen independently of the cohort
  selector, so the registry has no cohort to check against. A check that "some
  manifest has this anchor" could not fail in a way that protects the builder.
- E01: there is no durable method-authority store (see the E12 blockers).
  `MethodDefinition` is immutable content, so the E01 binding is structural and
  is rechecked whenever an object is parsed.

Every read therefore re-proves storage integrity only: journal chain, object
digests, canonical bytes, contract validation, selector history, rollback
fence, and final head. Binding to live authority belongs to the E12 builder,
which must call `require_projection_policy_binding(resolved, …)` with the anchor
of its live-resolved D05 manifest and the method/quantity/unit of the E04/E06
source. A mismatch raises `ProjectionPolicyRegistryConflict`.

## Selector page

`list_selectors(after_selector_id=None, after_policy_version=None, limit=50)`
takes a page limit of 1 to 100 and returns up to that many rows (none for an
empty registry or an exhausted cursor), ordered by `(selector, version)`. A row carries only the
selector, version, `latest_version`, object and policy digests, family,
selection rule, component count, D02 measurement-definition digest, and D05
anchor-definition digest. It never contains the policy ID, method or quantity
IDs, atlas or contributor IDs, chromosomes, coordinates, or free text.

## Storage

Storage is at D03 parity (`docs/LONGITUDINAL-DECISION-REGISTRY.md`): a private
`0700` root with `0600` files; descriptor-relative publication with fsync and
hard-link adoption; a hash-chained journal from a genesis digest over immutable
metadata; reopen requires the retained registry ID, epoch and head; a
process-wide head fence plus a per-instance trusted head detect rollback;
control files are inode-bound; a torn journal append is truncated to the
committed size; the journal is the commit point and at most one uncommitted
object is tolerated and removed by the next registration; method, alias and
instance-state seals; and `backup_bytes`/`restore` into a new root, with a
failed restore (including a failed final reopen) removing its target. The
metadata binds no external store. As in D03, reads tolerate one uncommitted
object without parsing it; the next registration reads it and fails closed if
its bytes do not match its name, which blocks registration until an operator
removes it. The shared storage follow-up in the E12 plan (interrupted root
creation, torn tails found on reopen, staged restore, and parse-everything
reads) applies here too.

Threat model: in-process code mutation and same-user filesystem races are out
of scope. The seals and storage checks fail closed on accidental drift; they do
not defend against a hostile process running as the same user.

## Open decisions

- The E08 and E09 artifact contracts do not pin a D02 quantity ID or unit.
  Those policies bind whatever exact tuple the registrant supplies; only E07
  pins its quantity IDs and `unit_bp`.
- The statistic unit IDs (`unit_alignment_count`, `unit_read_count`,
  `unit_relative_dosage`, `unit_log2_ratio`, `unit_copy_number`,
  `unit_bin_count`) are defined here. No upstream unit vocabulary exists for
  them.
- E09 segments are per-sample segmentations. A finite segment policy matches
  only a member with the identical segment, so longitudinally the realistic
  segment rule is `canonical_all_components`.
- The fragment `bin_index` is an E02 chart-row index. E07 panel rows are
  filtered by display controls, so the adapter must resolve the bin from the
  verified source chart and require it exactly once in the panel rows and the
  table.
- Registration is a local operator call with no authenticated registrant
  identity, and there is no retirement or revocation of a registered policy
  version.
- The registry lock is independent. Joining it to the composable E12 authority
  fence is left to that prerequisite.
