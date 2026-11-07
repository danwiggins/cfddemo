# ADR 0002: reference-state labels are a Lab-only surface

- Status: accepted; records an existing state, changes no behaviour
- Date: 2026-11-06
- Scope: the Lab page's Findings lens versus the signed operator surface

## Context

Two rules were written on 2026-09-26 for the signed runner wave:

- `DESIGN.md` § Operator state (`DESIGN.md:62-63`, commit `2a77fd20`): "The
  presentation model groups jobs into needs attention, processing, queued,
  completed records, and inactive. It never groups or sorts by biological
  value." The categories are implemented in
  `traceback_runner/operator.py:74-111` from runner `JobState` and blocker
  presence only.
- `docs/EPICS.md` § E10 acceptance (`docs/EPICS.md:322`, commit `e14a7f21`):
  "no healthy/abnormal classification appears in personalized output".
  `docs/PRODUCT-PLAN.md:124-131` lists "healthy, normal, abnormal" among the
  release-blocking words for personalized output, and
  `docs/PRODUCT-PLAN.md:156-160` forbids labelling a result as normal or
  abnormal or prioritising anything by a biological measurement.

On 2026-10-15 the Traceback Lab epic landed the Findings lens (commit
`352b29c9`, PR #136, slice S6c). The Lab is a separate, unsigned subsystem
(`docs/LAB-GUIDE.md:3-8`) whose page opens under "Research use only. Not
CLIA-validated. Not a diagnosis." (`docs/LAB-GUIDE.md:10`). Its Findings lens
does what the signed surface must not:

- each metric is assigned a reference state from the vocabulary
  `in_range`, `above_typical`, `markedly_above`, `below_typical`,
  `markedly_below`, `not_interpretable`, `no_reference`
  (`traceback_lab/web/static/lab-views.js:11-17`);
- the four out-of-range states become cards grouped under "Markedly above",
  "Markedly below", "Above typical", "Below typical", each group sorted by
  tier (PROVEN, NEWER, SPECULATIVE) then by descending severity
  (`traceback_lab/web/static/lab-views.js:515-518, 551-555`;
  `traceback_lab/interpretation/engine.py:409-412`);
- `not_interpretable`, `no_reference` and `in_range` metrics are counted,
  collapsible `<details>` rows (`traceback_lab/web/static/lab-views.js:560-565`);
- the lens carries the pill "FLAG: CLIA — health-assessment labels on an
  individual sample" (`traceback_lab/web/static/lab-views.js:33, 525`), and
  the Compare lens states that a Lab cohort is "an exploratory
  selected-cohort distribution, not a healthy or clinical reference"
  (`docs/LAB-GUIDE.md:803-804`; `lab-views.js:1406`). The default Lab cohort
  is named `healthy-v1` (`docs/LAB-GUIDE.md:859-865`).

`docs/LAB-GUIDE.md:795-801` documents this as the page contract. The
documents were never reconciled: read literally, `DESIGN.md:63` and
`docs/EPICS.md:322` forbid what the Lab's Findings lens does. This ADR
records the resolution that the code already embodies.

## Decision

1. Reference-state labels (the `in_range` / `above_typical` /
   `markedly_above` / `below_typical` / `markedly_below` / `not_interpretable`
   / `no_reference` vocabulary, and the `healthy-v1` cohort name) are allowed
   on the Lab surface only, under the Lab's research-use-only framing and its
   CLIA flag. The Lab may group and sort metrics by reference state, tier and
   severity as `lab-views.js` does today.
2. `DESIGN.md:62-63` ("never groups or sorts by biological value") governs
   the signed operator surface: `traceback_runner/operator.py`, the operator
   site served by the signed `traceback serve`, signed records and bundles,
   and `docs/OPERATOR-GUIDE.md`. It does not govern the Lab's Findings sort.
3. `docs/EPICS.md:322` ("no healthy/abnormal classification appears in
   personalized output") continues to govern E10's signed cell-origin
   measurement and every signed record. The Lab's cell-origin lens is not
   that deliverable (`docs/SIGNAL-METHODS-CELL-ORIGIN-AND-CNA.md:53-54,
   316-318` keeps the signed route free of a reference range for this
   reason).

Neither rule's text changes. This ADR narrows their scope to the surfaces
they were written for.

## Boundary: what must never cross from Lab to signed

- No biological grouping: signed operator categories stay the five in
  `traceback_runner/operator.py:21-26`, derived from job state and blockers.
- No sorting by value: no signed list, view or export orders jobs or records
  by a measurement, a reference state, a severity, or a tier.
- No reference-state wording: none of the Lab's seven state labels, the
  words "healthy", "normal" or "abnormal", or the `healthy-v1` cohort name
  appears in a signed record, bundle, the operator site, or
  `docs/OPERATOR-GUIDE.md`. `docs/PRODUCT-PLAN.md:124-131` remains the
  release-blocking word list for personalized output.
- No Lab cohort as a signed reference: a Lab cohort generation is an
  exploratory distribution and is never cited by a signed record.
- The Lab framing travels with the labels: any surface that shows a
  reference state must carry the research-use-only header and the CLIA flag
  the Lab carries today. A new surface without that framing is a signed-rule
  surface.

## Consequences

- Reviewers reading `DESIGN.md:63` or `docs/EPICS.md:322` against Lab code
  should not file the Findings sort as a violation; reviewers reading them
  against `traceback_runner/`, the operator site or a signed bundle should.
- A change that moves a Findings-style state, group or sort into the signed
  surface needs a new ADR that supersedes this one, not a code review
  exception.
- A Lab export (for example a per-sample report) inherits the Lab rule only
  while it carries the Lab framing; the brief for any such feature must say
  which surface it belongs to.
- No runtime code, fixture or test changes with this ADR.

## Verification

Signed surface (categories and language):

- `tests/test_operator.py::test_operator_state_fixture_contains_expected_release_one_states`
  pins the operator state fixture to the five non-biological categories.
- `tests/test_reporting_language.py` asserts that the demo's personalized
  copy carries no healthy-range or reference-range wording and that every
  result renderer shows the research-use-only banner.
- There is no test that scans `traceback_runner/` for the Lab state
  vocabulary; a reviewer applying this ADR to a signed-surface change should
  grep for it until one exists.

Lab surface (grouping and sort):

- `tests/lab/test_interpretation.py::test_findings_sort_by_state_then_tier_then_severity`
  pins `findings_sort_key` to state group, tier, then severity.
- `tests/lab/test_landing.py::test_findings_grouping_and_flags` asserts the
  Findings DOM: the CLIA pill text, the four summary counts, and the
  `Not interpretable` / `No reference` / `In range` `<details>` rows in that
  order.
- `tests/lab/test_findings_cohort_view.py` and
  `tests/lab/test_acceptance_contract_followup.py::test_findings_details_break_down_each_reason_count`
  cover the counted rows and their reason breakdowns.

Related: [ADR 0001](0001-execution-substrate-comparison.md) for the ADR format.
