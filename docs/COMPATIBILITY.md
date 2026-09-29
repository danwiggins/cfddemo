# Versioned compatibility decisions

`evidence_inspector.compatibility` is the pure E05 compatibility boundary. It
compares two immutable measurement records and can select records only against
an explicit caller-supplied anchor. It does not execute tools, select a provider
primary, change qualification, mutate records, or render a user interface.

Each record binds exact E01 `MethodDefinition` and `CurrentMethodCapability`
identities to result and bundle SHA-256 digests. Its versioned compatibility key
declares the measurement family, quantity, unit, result schema/version,
reference/grid/atlas/panel assets, normalization, coordinate and denominator
semantics, and registered compatibility-policy version. Human labels are not
identity.

Policies authorize exact method-definition digests and exact registry
digest/version identities, not reusable method references alone. Registry or
authority drift therefore fails closed before a compatibility result is used.

Decisions are one of `comparable`, `different_quantity`, `incompatible`, or
`unknown`. Missing metadata, stale policy or authority, failed or not-run
execution, insufficient information, and unverified, revoked, or unknown trust
fail closed as `unknown`; unknown decisions never permit deltas or a shared axis.
Qualification remains independently recorded and does not imply compatibility.

Canonical decisions contain sorted mismatch keys and missing fields, an exact
remediation code, and replay bindings for both result/bundle identities, method
references, method/capability/key digests, registry identity, authority identity,
and policy digest. Selections bind every decision to the explicit anchor and
must replay exactly against the original selection request; a valid self-hash
alone is insufficient.
Canonical JSON and SHA-256 validation reject normalization drift or tampering.
All contracts are closed and bounded and reject donor, sample, patient, run,
read, sequence, and local-path identifier stems, including concatenated or
numbered forms. Exact safe domain lexemes (`runtime`, `runner`, `readout`,
`readiness`, `pathology`, `sampled`, and `sequencer`) are explicitly allowed;
arbitrary suffixes are not.
