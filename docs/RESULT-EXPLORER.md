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

The explorer binds an exact open `ResultCatalog` installation and captures its
unbound verification chain. Closed, substituted, subclassed, instance-shadowed,
or class-shadowed readers fail before detail projection. The binding includes the
root, database, object directory, connection, and trust-store identities. Runtime
replacement of installed module bytecode is a process-integrity concern and is
outside this boundary.

All nested public strings pass the shared web privacy validator during canonical
artifact ingestion and again immediately before HTTP serialization. It repeatedly
percent-decodes and Unicode-normalizes text and rejects paths, URIs, traversal,
reserved identifier stems, credentials, and full-IUPAC sequence strings. Only
schema-typed digest values receive the digest exemption.

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
- an explicit unavailable state for E12 inside each result document (the
  per-result `longitudinal_state` field is unchanged).

E12 longitudinal comparison is a separate, optional adapter:
`IntegratedExplorerSource(..., longitudinal=LongitudinalExplorerSource(...))`,
bound to the same E04 catalog. It adds the reader-authorized
`/api/v1/longitudinal/*` routes and the longitudinal section of the packaged
page (cohort/version, measurement, explicit anchor, version diff, source table,
segment-only chart, covariate panel, provenance drawer, Save and Reopen). See
`docs/LONGITUDINAL-BROWSER.md`.

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

The harness also runs the packaged loopback service under the process socket
guard. It proves that an unauthenticated catalog request is denied, the
authenticated 100-row response repeats byte-identically, bundled assets contain
no remote references, every catalog row keeps release explorer/export disabled,
and hostile sentinel filters fail without reflection. That is local service
evidence, not approved-host or browser evidence.

The local browser manifest records actual HTTP navigation, JavaScript DOM-ready,
DOM structure, console, screenshot digest, daemon RSS, and renderer JavaScript
heap observations. It is explicitly local and unapproved. Per-Chromium-process
RSS and the required 10,000 verified imports remain unavailable.

Parsed content-addressed contracts exist for browser captures, accessibility
audits, and the exact five-provider task matrix. Synthetic fixtures and local
browser checks do not satisfy those gates. Keyboard-only operation,
screen-reader operation, 200 percent zoom/reflow, reviewed browser captures,
the approved host run, and the five-provider study remain explicitly unmet
external requirements. The E14 evidence report does not cover the E12
longitudinal view, so it binds that missing evidence to disabled release
explorer/export controls.

## Validation

Automated coverage includes loopback authorization, query bounds, exact read
model serialization, fail-closed release eligibility, private-path exclusion,
semantic table structure, responsive CSS, and offline asset checks. Real-browser
local checks cover bootstrap, catalog/detail requests, keyboard focus order,
mobile reflow, accessibility tree, console errors, and request inventory.
