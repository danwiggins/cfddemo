# Versioned method registry

`evidence_inspector.method_registry` defines the closed E01 registry and
capability contract. It is an offline identity and authority boundary, not a
plugin loader or execution system.

Each method binds an exact method ID/version, measurement family, quantity,
unit, parameter-schema digest, and ordered tool and asset identities. Tool and
asset versions must exist in the same registry with matching digests. Unknown
methods, versions, assets, tools, fields, and noncanonical JSON fail closed.

Qualification, display role, research availability, and provider availability
remain separate. `provider_primary` requires an explicit approval reference,
authority scope, and effective time. Provider availability additionally
requires `qualified`, a matching authority scope, and an effective,
non-revoked time window. No latest-version or research-role fallback exists.

Registry snapshots and capability decisions use exact canonical JSON. Registry
snapshots have deterministic SHA-256 identities. Capability decisions carry
that digest and must replay exactly from the same registry, method reference,
authority scope, and effective time.
Transitions are append-only: published versions advance by one, chain to the
prior digest, retain tool/asset/method identity, and may only add a bounded
revocation timestamp to an existing method. Overlapping provider-primary
windows for one family/quantity/unit/authority scope are invalid.

The schema has no donor, sample, run, read, sequence, path, extension, or input
digest fields. E01 adds no UI, hosted service, dynamic plugin execution,
clinical interpretation, or real-data fixture.
