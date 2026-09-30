# Integrated local result explorer

The packaged loopback service can expose a result explorer when the caller supplies
an `IntegratedExplorerSource`. Existing operator-only callers remain valid and
receive a clear unavailable state for the explorer.

## Authority boundary

The source queries the immutable E04 `ResultCatalog`; it does not maintain a
second catalog. Every detail request reloads the stored reference, re-verifies the
copied bundle, and replays the current registry, authority head, and capability.
Detail artifacts are stored as canonical bytes and reparsed on every read. The
E06 request and view are replayed and bound back to the exact current E04 identity.
Browser responses do not include import roots, bundle paths, private alias maps,
or raw input values.

The renderer is packaged HTML, CSS, and JavaScript. It has no external assets or
network dependencies. It presents:

- two bounded result selectors with shared method filters;
- orthogonal execution, information, trust, qualification, and role states;
- exact denominators and attrition, with missing and withheld values rendered as
  states rather than numeric zero;
- compatibility status and deltas only when an E07 comparison is registered;
- exact result, method-definition, authority, bundle, and filter identities;
- optional registered E07-E11 and E13 fragment, cell-origin, CNA, provenance,
  sensitivity, and portable-view contracts, including their exact values, units,
  uncertainty, and missingness states;
- an explicit unavailable state for E12, which is not implemented.

The API is session-authorized and bounded to 100 catalog references per page.
Unknown filters, malformed identities, and unavailable detail documents fail
closed with sanitized local errors.

## Release eligibility

Research inspection is derived only from the catalog's
`research_inspectable` flag. It is not blocked by E14 release evidence.

Release explorer and release export are always disabled. No authenticated policy
installation boundary exists, so the explorer does not accept a caller-supplied
`ReleaseGateDecision` or any structurally similar object. This preserves research
inspection without turning a Python object into release authority.

## Evidence status

The checked-in foundation harness uses private-SQL E04-shaped fixture rows. Its
10,000-row timing and 100,000-row `tracemalloc` values are labeled `fixture_only`;
they are not verified imports, HTTP/browser render evidence, process RSS, SQLite
or native allocation evidence, or browser-process memory.

The local browser manifest records actual HTTP navigation, JavaScript DOM-ready,
DOM structure, console, screenshot digest, daemon RSS, and renderer JavaScript
heap observations. It is explicitly local and unapproved. Per-Chromium-process
RSS and the required 10,000 verified imports remain unavailable.

Parsed content-addressed contracts exist for browser captures, accessibility
audits, and five-provider task outcomes. Synthetic fixtures and local browser
checks do not satisfy those gates. Accessibility, reviewed browser captures,
the approved host run, and the five-provider study remain external requirements.

## Validation

Automated coverage includes loopback authorization, query bounds, exact read
model serialization, fail-closed release eligibility, private-path exclusion,
semantic table structure, responsive CSS, and offline asset checks. Real-browser
local checks cover bootstrap, catalog/detail requests, keyboard focus order,
mobile reflow, accessibility tree, console errors, and request inventory.
