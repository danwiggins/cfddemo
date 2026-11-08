# ADR 0002: reference-state labels are a Lab-only surface

- Status: accepted; records the existing split between the Lab and the signed surfaces and states, as rules going forward, the requirements marked "(rule)" below; changes no behaviour
- Date: 2026-11-06
- Scope: the Lab (`traceback_lab/`: page, CLI prints, exports, cohort names) versus every other surface
- Line numbers in this ADR refer to this branch after this change (the
  commit that adds this file); see "How the anchors were taken" at the end.

## Surfaces named in this ADR

One enumeration is used throughout:

- Lab: `traceback_lab/` (the page, CLI prints, exports).
- Signed: `traceback_runner/` (signed records and bundles; the operator site
  served by the signed `traceback serve`).
- Demo: `app.py` (the Streamlit demo; personalized output, not signed).
  Removed on 2026-11-08; the rules below that name it no longer apply.
- Operator guide: `docs/OPERATOR-GUIDE.md` (operator-facing text; it is not
  an output surface and appears only under the vocabulary rule below).

"Personalized output" means the signed records and bundles, the operator
site, and the demo. It is wider than "signed": the demo is personalized
output that is not signed.

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
CLIA-validated. Not a diagnosis." (`docs/LAB-GUIDE.md:10`). Its Findings
lens does what the other surfaces must not:

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
`docs/EPICS.md:322` about E10's personalized output; neither names the Lab.
Their spirit, no biological grouping or sorting on an operator-facing
surface, is what needed a recorded boundary once the Lab shipped a lens that
groups and sorts by reference state. This ADR records the resolution that
the code already embodies.

## Decision

1. Reference-state labels (the `in_range` / `above_typical` /
   `markedly_above` / `below_typical` / `markedly_below` / `not_interpretable`
   / `no_reference` vocabulary, and the `healthy-v1` cohort name) are allowed
   on the Lab surface only. The Lab may group and sort metrics by reference
   state, tier and severity as `lab-views.js` does today. The framing the
   Lab must carry is stated in the Boundary below.
2. `DESIGN.md:62-63` ("never groups or sorts by biological value") governs
   the signed operator surface: `traceback_runner/operator.py` and the
   operator site. It does not govern the Lab's Findings sort, and it is not
   the rule that governs the demo.
3. `docs/EPICS.md:322` ("no healthy/abnormal classification appears in
   personalized output") governs personalized output on every surface other
   than the Lab — signed records and bundles, the operator site, and the
   demo; the Lab's research-use surface is the one recorded exception. The
   Lab's cell-origin lens is not the E10 deliverable
   (`docs/SIGNAL-METHODS-CELL-ORIGIN-AND-CNA.md:53-54, 316-318` keeps the
   signed route free of a reference range for this reason).
4. The `docs/PRODUCT-PLAN.md:124-131` word list and the
   `docs/PRODUCT-PLAN.md:156-162` "must not" list apply to personalized
   output on every surface other than the Lab — signed records and bundles,
   the operator site, and the demo. They do not apply to the Lab because of
   what it is: `traceback_lab/` is an unsigned research tool, not a consumer
   product surface. A Lab string that reaches a consumer or provider surface
   comes under those lists at that point.

Neither rule's text changes. This ADR narrows their scope to the components
they were written for.

## Boundary: scoped by component

The rule follows the component, not the framing on the page.

- Lab (`traceback_lab/`): may show reference states, group and sort by
  them, and must carry the research-use-only header and the CLIA flag
  wherever a reference state appears. Today the page header
  (`docs/LAB-GUIDE.md:10`) and the Findings pill (`lab-views.js:34, 574`)
  do. Any Lab CLI output or Lab export that prints a reference state must
  do the same; none was checked for this ADR. A Lab export remains Lab
  output; it never becomes a signed record.
- Signed (`traceback_runner/`: signed records and bundles; the operator
  site): never shows a reference state, regardless of any header, flag or
  disclaimer placed beside it. Specifically:
  - no biological grouping: operator categories stay the five in
    `traceback_runner/operator.py:21-26`, derived from job state and
    blockers;
  - no sorting by value (rule): no signed list, view or export orders jobs
    or records by a measurement, a reference state, a severity, or a tier.
    No test verifies ordering today; `tests/test_operator.py` pins only the
    five categories;
  - no reference-state wording in exported bundle strings (rule): the
    export guard (`traceback_runner/export.py:115-119, 141-142`, quoted
    below) refuses `healthy` and `normal` (with `cancer`, `diagnos*`,
    `disease`, `reassur*`, `screen*`, `treat*`, `clean`); it does NOT match
    `abnormal` or any of the seven state labels. No check covers those in
    records or bundles today; extending the guard's word list to the seven
    labels and `abnormal`, with a test, is the open item recorded under
    Consequences;
  - no Lab cohort as a signed reference: a Lab cohort generation is an
    exploratory distribution and is never cited by a signed record.
- Vocabulary in source and guide text (`traceback_runner/` source and
  `docs/OPERATOR-GUIDE.md`): none of the ten terms `in_range`,
  `above_typical`, `markedly_above`, `below_typical`, `markedly_below`,
  `not_interpretable`, `no_reference`, `healthy`, `abnormal`, `healthy-v1`
  is used as a label in that text. As of this branch, the ten-term grep
  recorded below (identifier forms only, `-w`; display forms such as
  "Markedly above" or "No reference" were not searched) found 3 lines, all
  about the export guard (the guard's own regex and two comments saying
  "healthy" stays out of record text); no hit is a label in output text. "normal" is not one of the ten terms because
  it has uses on these surfaces that are not classificatory labels (listed
  below); the export guard still rejects it in exported strings.
- Demo (`app.py`): personalized output; the `docs/EPICS.md:322` rule and the
  `docs/PRODUCT-PLAN.md` lists apply, and `tests/test_reporting_language.py`
  guards its copy.

## Consequences

- Open item: extend `_FORBIDDEN_CLAIM` in `traceback_runner/export.py` to
  `abnormal` and the seven state labels, with a test in
  `tests/test_bundle_v4.py`, so the bundle guard matches the Boundary's rule.
  Until then the Boundary's bundle bullet is a rule, not a verified state.
- Reviewers reading `DESIGN.md:63` against Lab code should not file the
  Findings sort as a violation; reviewers reading it against
  `traceback_runner/operator.py` or the operator site should. Reviewers
  reading `docs/EPICS.md:322` or the `docs/PRODUCT-PLAN.md` lists against the
  signed records and bundles, the operator site, or the demo should file
  reference-state labels there as violations.
- A change that moves a Findings-style state, group or sort into
  `traceback_runner/` or `app.py` needs a new ADR that supersedes this one,
  not a code review exception. Adding the Lab's header or flag to a
  non-Lab surface does not make it a Lab surface.
- A Lab export (for example a per-sample report) is Lab output and carries
  the Lab framing; the brief for any such feature must say so and must not
  route it through the signed record path.
- No runtime code, fixture or test changes with this ADR.

## Verification

What exists and what was run; nothing beyond it is claimed.

Signed surface:

- `tests/test_operator.py::test_operator_state_fixture_contains_expected_release_one_states`
  pins the operator state fixture to the five non-biological categories.
- `traceback_runner/export.py:115-119` defines the guard regex, quoted
  literally:

  ```python
  _FORBIDDEN_CLAIM = re.compile(
      r"\b(?:cancer(?:-free)?|diagnos(?:e|ed|is|tic)|disease|healthy|normal|"
      r"reassur(?:e|ing)|screen(?:ing)?|treat(?:ment)?|clean)\b",
      re.IGNORECASE,
  )
  ```

  `traceback_runner/export.py:141-142` raises `ExportBoundaryError` when
  it matches an exported string.
  `tests/test_bundle_v4.py::test_v4_report_must_carry_the_banner_and_no_claim`
  asserts a bundle report containing "healthy" is refused.
- No test scans `traceback_runner/` source, the operator site templates or
  `docs/OPERATOR-GUIDE.md` for the ten-term vocabulary. The grep below is
  the only check, run by hand on this branch over identifier forms; a
  reviewer applying this ADR to a signed-surface change should rerun it,
  and also search the display forms, until a test exists.

Demo:

- `tests/test_reporting_language.py` asserts that the demo's personalized
  copy carries no healthy-range or reference-range wording and that every
  result renderer shows the research-use-only banner.

Lab surface:

- `tests/lab/test_interpretation.py::test_findings_sort_by_state_then_tier_then_severity`
  pins `findings_sort_key` to state group, tier, then severity.
- `tests/lab/test_landing.py::test_findings_grouping_and_flags` asserts the
  Findings DOM: the CLIA pill text, the four summary counts, and the
  `Not interpretable` / `No reference` / `In range` `<details>` rows in that
  order.
- `tests/lab/test_findings_cohort_view.py` and
  `tests/lab/test_acceptance_contract_followup.py::test_findings_details_break_down_each_reason_count`
  cover the counted rows and their reason breakdowns.

## How the anchors were taken

Dates and introducing commits:

```sh
git log --format='%h %ad %s' --date=short -S'never groups or sorts by biological value' -- DESIGN.md
# 2a77fd20 2026-09-26 feat: add synthetic operator CLI foundation
git log --format='%h %ad %s' --date=short -S'no healthy/abnormal classification' -- docs/EPICS.md
# e14a7f21 2026-09-26 chore: preserve reviewed Traceback plan and synthetic runner baseline
git log --format='%h %ad %s' --date=short -S'function findings' -- traceback_lab/web/static/lab-views.js
# 352b29c9 2026-10-15 feat(lab): landing page, Findings and Stability lenses, defended results and the CLIA explainer (slice S6c) (#136)
```

Each `-S` search identifies the introducing commit as long as the quoted
string survives unchanged.

Vocabulary grep, run on this branch after this change, output pasted
verbatim (an earlier draft of this ADR used `\b` in `git grep -E`, which
this host's `git grep` does not honour and which produced a false "0 hits";
`-w` is used instead):

```sh
$ git grep -n -i -w -E "in_range|above_typical|markedly_above|below_typical|markedly_below|not_interpretable|no_reference|healthy|abnormal|healthy-v1" -- traceback_runner docs/OPERATOR-GUIDE.md
traceback_runner/cell_origin.py:73:# The fixed limitation statements (spec §3.1).  "Diagnosis" and "healthy"
traceback_runner/copy_number.py:103:# The fixed limitation statements (spec §3.2).  "Diagnosis" and "healthy"
traceback_runner/export.py:116:    r"\b(?:cancer(?:-free)?|diagnos(?:e|ed|is|tic)|disease|healthy|normal|"
```

Classification of the 3 hits: `export.py:116` is the guard regex itself;
`cell_origin.py:73` and `copy_number.py:103` are comments stating that
"healthy" stays out of the fixed limitation statements because every record
text passes the export claim check. None is a label in output text.
`docs/OPERATOR-GUIDE.md` had no hit.

```sh
$ git grep -n -i -w normal -- traceback_runner docs/OPERATOR-GUIDE.md
docs/OPERATOR-GUIDE.md:140:`--root` retains its normal CLI meaning: the Traceback product root. Asset
docs/OPERATOR-GUIDE.md:300:   registered references, free disk and trust. `PASS ... (1 warning)` is normal
docs/OPERATOR-GUIDE.md:314:     (TBX-MOD-001 WARN, normal after the alignment command above). The run
docs/OPERATOR-GUIDE.md:433:  deleted after `readCounter`), no reference panel (PoN), normal-fraction
traceback_runner/export.py:116:    r"\b(?:cancer(?:-free)?|diagnos(?:e|ed|is|tic)|disease|healthy|normal|"
traceback_runner/toolchain_locks/runIchorCNA.R:20:  "--id", "--WIG", "--gcWig", "--centromere", "--normal", "--ploidy", "--maxCN",
traceback_runner/toolchain_locks/runIchorCNA.R:76:  normal = numeric_vector_text("--normal"),
```

Classification of the 7 hits, none classificatory: `OPERATOR-GUIDE.md:140`,
`:300`, `:314` use "normal" for ordinary CLI meaning and expected warnings;
`OPERATOR-GUIDE.md:433` names ichorCNA's normal-fraction, a biological
estimate the tool reports, not a label applied to a result; `export.py:116`
is the guard regex; `runIchorCNA.R:20`, `:76` pass the tool's `--normal`
parameter.

Line numbers throughout this ADR refer to this branch after this change;
`docs/LAB-GUIDE.md:867-872` is the Findings bullet as edited by this change
(two lines longer than on `main`), and the Compare and cohort anchors shift
by the same two lines relative to `main`.

Related: [ADR 0001](0001-execution-substrate-comparison.md) for the ADR format.
