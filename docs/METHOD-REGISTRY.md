# Versioned method registry

`evidence_inspector.method_registry` is the closed E01 identity and authority
boundary. It is offline data, not a plugin loader or execution service.

Scientific identity and authority are separate:

- `MethodDefinition` immutably binds method ID/version, family, quantity, unit,
  parameter-schema digest, and exact tool and asset versions/digests.
- `QualificationRecord` is an append-only qualification decision.
- `DisplayRoleAssignment` independently assigns research, disabled, or
  explicitly approved provider-primary display authority for one scope.
- `AuthorityRevocation` ends an exact qualification or role record without
  deleting the method definition or its historical research visibility.

Registry snapshots use canonical JSON and deterministic SHA-256 identities.
Definitions, authority records, and revocations are append-only across chained
snapshots. A newly appended authority record cannot predate its publication;
E01 does not model signed historical backfill.

Historical capability replay is explicitly non-authorizing. Current provider
eligibility requires the exact registry snapshot plus an authority head whose
digest is supplied by an external trusted caller. The head binds registry ID,
version, digest, authority revision, and issue time into canonical capability
bytes. A stale registry cannot evaluate current eligibility against a newer
trusted head.

Provider eligibility requires an active qualified record and an active
`provider_primary` assignment with an opaque `approval_...` token in the exact
requested `scope_...`. No default is inferred from version order, research
role, availability, or appearance. At most one primary window may be effective
per family, quantity, and authority scope.

All variable strings use field-specific reserved namespaces such as `mth_`,
`tool_`, `asset_`, `qty_`, `unit_`, `scope_`, `qual_`, `role_`, `revoke_`, and
`approval_`. Reserved privacy terms are rejected. The models expose no donor,
sample, patient, run, read, sequence, path, extension, or input-digest fields.

E01 adds no UI, hosted service, clinical interpretation, dynamic execution, or
real-data fixture.
