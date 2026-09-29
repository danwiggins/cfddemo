# Provenance and method-difference drawer

`evidence_inspector.provenance_drawer` is a framework-independent read model.
It does not render a frontend, read files, contact a service, select records, or
infer scientific compatibility. The caller supplies two already-selected
aggregate records and one E05 compatibility request and decision.

## Contract boundaries

The builder consumes existing contracts rather than creating parallel sources
of truth, and the canonical output retains a privacy-bounded replay request:

- E01 `MethodDefinition`, `CurrentMethodCapability`, method asset references,
  registry identity, and authority-head identity;
- E02 installed-asset verification plus the signed release envelope and exact
  fresh active authorization decision;
- E04 `CatalogResultRef` bundle, manifest, method, registry, and authority
  bindings;
- E05 `VerifiedMeasurementRecord`, `CompatibilityRequest`, and replayed
  `CompatibilityDecision`.

Catalog and measurement identities must agree exactly. Every displayed
scientific value is included in one result-signed canonical evidence payload
that also binds the E04 result, bundle, and manifest identities. Its digest
must equal the verified measurement's `result_sha256`; values cannot change
while retaining the result identity. Method assets must have one uniquely
sorted E02 proof each, with matching ID, version, content size and digest,
registered reference digest, current reference digest, active lifecycle, valid
integrity, release/version/package identity, and fresh authority. Compatibility
is replayed from the original request; a valid decision from another request is
rejected. Building and canonical parsing require a separate
`DrawerVerificationContext`: independently supplied result trust roots, exact
E04 catalog bindings, and E02 release authorizations containing the trusted
policy, authority head, expected binding, and expected package digest. None of
those trust assertions are accepted from serialized drawer bytes. Validation
reruns the result signature, E02 authorization, E05 decision, freshness
checks, and every derived row.

Unknown compatibility, stale authority, revoked or invalid assets, unavailable
results, incomplete denominator accounting, and any cross-contract mismatch
fail closed. `different_quantity` and `incompatible` decisions may render their
differences, but retain the E05 prohibition on deltas and shared axes.

## Fixed visible fields

The v1 drawer always emits these ten rows in this order:

1. measurement;
2. bundle;
3. method definition;
4. capability and authority;
5. assets;
6. denominator;
7. reconciled counts;
8. filters;
9. limitations;
10. compatibility decision.

Every row carries left and right lineage resolving to the exact bundle and
manifest, method-definition digest, capability digest, registry and authority
head, authority scope and capability time, asset-reference digests and
freshness window, denominator digest, three count digests, filter digests,
limitation digests, and compatibility-decision digest. A field is
`unchanged` only when its exact value identity is equal; display text alone is
never used for comparison. `changed_fields` and `unchanged_fields` must be an
ordered, exhaustive partition of all ten rows.

## Determinism and privacy

All contracts are immutable, closed, bounded, finite-number-only models.
Canonical JSON is byte-stable, and both side provenance and the complete drawer
carry validated SHA-256 identities. Canonical loading rejects whitespace or key
ordering drift as well as semantic mutation. A schema-owned byte ceiling is
checked before JSON parsing, and every collection has an explicit item bound.

The serialized read model contains controlled aggregate identifiers and
digests only. It excludes catalog record IDs, workflow identifiers, aliases,
local paths, raw source identifiers, and sequence. Controlled text is
Unicode-normalized and repeatedly percent-decoded before rejecting
privacy-reserved identifier stems, traversal, paths, URIs, and the full IUPAC
sequence alphabet. Only typed digest and cryptographic encoding fields bypass
text scanning.
Measurement display is derived from the exact numeric value and registered
unit, so callers cannot provide a conflicting label.

Synthetic fixtures under `tests/fixtures/provenance_drawer/` pin the canonical
drawer digest, changed/unchanged partition, and adversarial fail-closed cases.
They contain no donor data or clinical claims.

## Scope

This is research inspection infrastructure. It does not authorize execution,
establish qualification, interpret health status, or choose a frontend
framework.
