# Integrated local result explorer

The packaged loopback service can expose a result explorer when the caller supplies
an `IntegratedExplorerSource`. Existing operator-only callers remain valid and
receive a clear unavailable state for the explorer.

## Authority boundary

The source queries the immutable E04 `ResultCatalog`; it does not maintain a
second catalog. Detail documents accept only validated E06-E13 contracts and bind
the E06 row identity back to the exact E04 catalog reference. Browser responses do
not include import roots, bundle paths, private alias maps, or raw input values.

The renderer is packaged HTML, CSS, and JavaScript. It has no external assets or
network dependencies. It presents:

- two bounded result selectors with shared method filters;
- orthogonal execution, information, trust, qualification, and role states;
- exact denominators and attrition, with missing and withheld values rendered as
  states rather than numeric zero;
- compatibility status and deltas only when an E07 comparison is registered;
- exact result, method-definition, authority, bundle, and filter identities;
- optional registered E07-E13 fragment, cell-origin, CNA, provenance,
  sensitivity, cohort/timepoint, and portable-view contracts.

The API is session-authorized and bounded to 100 catalog references per page.
Unknown filters, malformed identities, and unavailable detail documents fail
closed with sanitized local errors.

## Release eligibility

Research inspection is derived only from the catalog's
`research_inspectable` flag. It is not blocked by E14 release evidence.

Release explorer and release export eligibility require all of:

1. a `ReleaseGateDecision`;
2. `capability_enabled=true` on that exact decision;
3. `current_provider_eligible=true` on the exact catalog result.

Missing or failed release evidence therefore disables release-only surfaces
without disabling authorized research inspection.

## Evidence status

The local harness now measures the real E04 SQLite catalog and public API
serialization path:

- 10,000-result filter/query p95;
- 10,000-result initial page serialization p95;
- 100,000-result bounded-memory population/query/serialization.

These measurements are local and unapproved. They do not satisfy the approved
host gate.

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

