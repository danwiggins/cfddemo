# Method comparison explorer: states and interactions

Status: disposable E03 design fixture. This is not production UI and does not choose a frontend framework.

## Core interaction

- Panel A and Panel B select methods independently. Method identity, quantity, unit, qualification, denominator, exclusions, strengths, limitations, and intended use remain visible.
- Linked filters apply the same depth subset and display range to both panels. Unlinking reveals panel-local controls.
- Crosshair synchronization and numerical deltas are enabled only when both panels are complete and the registered quantity, unit, depth subset, and display range match. Different quantities, units, or filter subsets retain independent axes and never show a delta.
- Every plotted bin is keyboard focusable. The accessible table is generated from the same in-memory series used by the SVG charts.
- The provenance drawer traps focus, closes with Escape, restores focus to its trigger, and contains synthetic aggregate identities only.

## Result-state fixture

The Panel B state selector exercises explicit `complete`, `loading`, `empty`, `partial`, `failed`, `insufficient`, `revoked`, `unsupported`, and `unavailable` states. Non-complete states suppress deltas and synchronized axes. Missing or withheld results render as words and dashes, never numerical zero.

`partial` is the only non-complete state that exposes chart rows. It plots only the completed partition, marks the state above the chart, and shows incomplete attrition in the denominator strip.

## Responsive behavior

- Desktop: side-by-side A/B panels and a five-column difference strip.
- Wider tablet: filters wrap while panels remain side by side, and difference fields reduce to two or three columns.
- Compact tablet and mobile at 768px or narrower: panels stack; mobile also uses one-column controls, two-column denominator blocks, large touch targets, and a full-width provenance drawer.
- The layout uses fluid widths and no fixed text height, so browser zoom to 200% reflows without clipping. Reduced-motion mode removes transitions and smooth scrolling.

## Visual semantics

- Off-white paper and dark ink establish the neutral base.
- Teal identifies baseline A; cobalt identifies challenger B.
- Amber marks withheld, incomplete, or attention states. Status labels always include text, so meaning does not depend on color.
- No red/green clinical semantics, diagnosis language, gauges, or pie charts are used.

## Deliberate limits

- All values are deterministic synthetic aggregate fixtures.
- No donor identifiers, raw read identifiers, local filesystem paths, external fonts, network requests, model calls, or production runtime hooks are present.
- Cell-origin and copy-number views remain outside this one-page fragment-comparison fixture.
