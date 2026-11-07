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
  `docs/PRODUCT-PLAN.md:156-162` forbids labelling a result as normal or
  abnormal or prioritising anything by a biological measurement.

On 2026-10-15 the Traceback Lab epic landed the Findings lens (commit
`352b29c9`, PR #136, slice S6c). The Lab is a separate, unsigned subsystem
(`docs/LAB-GUIDE.md:3-8`) whose page opens under "Research use only. Not
CLIA-validated. Not a diagnosis." (`docs/LAB-GUIDE.md:10`). Its Findings lens
does what the signed operator surface must not:

- each metric is assigned a reference state from the vocabulary
  `in_range`, `above_typical`, `markedly_above`, `below_typical`,
  `markedly_below`, `not_interpretable`, `no_reference`
  (`traceback_lab/web/static/lab-views.js:11-17`);
- the four out-of-range states become cards grouped under "Markedly above",
  "Markedly below", "Above typical", "Below typical", each group sorted by
  tier (PROVEN, NEWER, SPECULATIVE) then by descending severity
  (`traceback_lab/web/static/lab-views.js:564-567, 600-604`;
  `traceback_lab/interpretation/engine.py:432-435`);
- `not_interpretable`, `no_reference` and `in_range` metrics are counted,
  collapsible `<details>` rows (`traceback_lab/web/static/lab-views.js:609-618`);
- the lens carries the pill "FLAG: CLIA — health-assessment labels on an
  individual sample" (`traceback_lab/web/static/lab-views.js:34, 574`), and
  the Compare lens states that a Lab cohort is "an exploratory
  selected-cohort distribution, not a healthy or clinical reference"
  (`docs/LAB-GUIDE.md:873-875`; `lab-views.js:1940`). The default Lab cohort
  is named `healthy-v1` (`docs/LAB-GUIDE.md:1036-1042`).

`docs/LAB-GUIDE.md:867-872` documents this as the page contract.
`DESIGN.md:62-63` is written about the signed job presentation model, and
`docs/EPICS.md:322` about E10's signed cell-origin output; neither names
the Lab. Their spirit, no biological grouping or sorting on the operator
surface, is what needed a recorded boundary once the Lab shipped a lens that
groups and sorts by reference state. This ADR records the resolution that
the code already embodies.

## Decision

1. Reference-state labels (the `in_range` / `above_typical` /
   `markedly_above` / `below_typical` / `markedly_below` / `not_interpretable`
   / `no_reference` vocabulary, and the `healthy-v1` cohort name) are allowed
   on the Lab surface only: the `traceback_lab/` page, its CLI prints and its
   exports, which carry the research-use-only header and the CLIA flag. The
   Lab may group and sort metrics by reference state, tier and severity as
   `lab-views.js` does today.
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
4. The `docs/PRODUCT-PLAN.md:124-131` word list and the
   `docs/PRODUCT-PLAN.md:156-162` "must not" list describe the consumer
   product's personalized output, which is signed output. They apply to
   every signed record, bundle and operator-site string. They do not apply
   to the Lab, which is not a consumer product surface: it is an unsigned
   research tool whose every page carries the research-use-only header and
   whose reference-state lens carries the CLIA flag. A Lab string that
   reaches a consumer or provider surface would come under those lists at
   that point.

Neither rule's text changes. This ADR narrows their scope to the components
they were written for.

## Boundary: scoped by component

The rule follows the component, not the framing on the page.

- Lab (`traceback_lab/`: the page, CLI prints, exports): may show reference
  states, group and sort by them, and must carry the research-use-only
  header and the CLIA flag wherever a reference state appears. A Lab export
  remains Lab output; it never becomes a signed record.
- Signed (`traceback_runner/`: records, bundles, the operator site,
  `docs/OPERATOR-GUIDE.md`): never shows a reference state, regardless of
  any header, flag or disclaimer placed beside it. Specifically:
  - no biological grouping: operator categories stay the five in
    `traceback_runner/operator.py:21-26`, derived from job state and
    blockers;
  - no sorting by value: no signed list, view or export orders jobs or
    records by a measurement, a reference state, a severity, or a tier;
  - no reference-state wording: none of the Lab's seven state labels, the
    words "healthy", "normal" or "abnormal", or the `healthy-v1` cohort name
    appears in a signed record, bundle, the operator site, or
    `docs/OPERATOR-GUIDE.md`;
  - no Lab cohort as a signed reference: a Lab cohort generation is an
    exploratory distribution and is never cited by a signed record.

## Consequences

- Reviewers reading `DESIGN.md:63` or `docs/EPICS.md:322` against Lab code
  should not file the Findings sort as a violation; reviewers reading them
  against `traceback_runner/`, the operator site or a signed bundle should.
- A change that moves a Findings-style state, group or sort into
  `traceback_runner/` needs a new ADR that supersedes this one, not a code
  review exception. Adding the Lab's header or flag to a signed surface does
  not make it a Lab surface.
- A Lab export (for example a per-sample report) is Lab output and carries
  the Lab framing; the brief for any such feature must say so and must not
  route it through the signed record path.
- No runtime code, fixture or test changes with this ADR.

## Verification

Signed surface (categories):

- `tests/test_operator.py::test_operator_state_fixture_contains_expected_release_one_states`
  pins the operator state fixture to the five non-biological categories.
- There is no test that scans `traceback_runner/` for the Lab state
  vocabulary; a reviewer applying this ADR to a signed-surface change should
  grep for it until one exists.

Streamlit demo (`app.py`, not a signed surface):

- `tests/test_reporting_language.py` asserts that the demo's personalized
  copy carries no healthy-range or reference-range wording and that every
  result renderer shows the research-use-only banner.

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

## How the dates were anchored

```sh
git log --format='%h %ad %s' --date=short -S'never groups or sorts by biological value' -- DESIGN.md
# 2a77fd20 2026-09-26 feat: add synthetic operator CLI foundation
git log --format='%h %ad %s' --date=short -S'no healthy/abnormal classification' -- docs/EPICS.md
# e14a7f21 2026-09-26 chore: preserve reviewed Traceback plan and synthetic runner baseline
git log --format='%h %ad %s' --date=short -S'function findings' -- traceback_lab/web/static/lab-views.js
# 352b29c9 2026-10-15 feat(lab): landing page, Findings and Stability lenses, defended results and the CLIA explainer (slice S6c) (#136)
```

Line anchors in this ADR were read at `main` commit `19c00184`
and may drift; the `-S` searches above do not.

Related: [ADR 0001](0001-execution-substrate-comparison.md) for the ADR format.
