# Synthetic release and qualification evidence

Status: Sprint 02 development contract. Synthetic evidence only.

These additive contracts do not change historical v1 runner or signature
bytes. They do not enable real input, a qualification probe, scientific
qualification, production keys, or execution authorization.

## Contract separation

`traceback_runner.release_evidence` defines immutable facts:

- `AssetReference` separates exact uncompressed framed payload identity from
  source/license provenance and current lifecycle metadata;
- `WorkstationProfile` identifies an exact synthetic host envelope without
  claiming that a host passed it;
- `ProtocolReference` records the reported external state but cannot approve
  itself;
- `QualificationEvidenceManifest` requires one explicit state for intended
  measurement and claim, exact SOP, dataset, rights/use/retention, comparator,
  replicates, partitions/holdouts, acceptance criteria, failure accounting,
  and requalification triggers; and
- `ReleaseEvidencePackage` binds the exact workflow, workstation, protocol,
  evidence and asset-reference digests.

Unknown and pending evidence remain present in the manifest. They never become
defaults or inferred approval.

`canonical_domain_bytes` prefixes canonical JSON with an exact domain tag and
rejects a model whose schema does not match that domain. Asset content SHA-256
means the exact uncompressed framed payload bytes. The full asset-reference
domain digest additionally covers source, license and lifecycle metadata.

## External authority

`traceback_runner.qualification` reuses the existing development Ed25519
`release` key purpose without changing `signing.py` or its v1 envelope. Signed
release packages and qualification decisions are separately domain-tagged.

A signature or body-declared role is insufficient. Verification requires:

1. an independently loaded public `TrustStore`;
2. an independently supplied `QualificationTrustPolicy` binding key, role,
   scope, exact release/profile/protocol/evidence identity and a UTC window;
3. caller-supplied expected identities established outside the envelope;
4. an independently trusted, fresh authority head; and
5. an injected aware UTC `now`.

The qualification verifier checks every signed append-only decision and then
requires the history tip to match the independent head's exact sequence and
digest. A truncated history therefore cannot replay an old approval after a
later revocation. The head cannot attest decisions after its `as_of` time.
At `now >= expires_at`, authority is expired.

For a verified result, `fresh_until` is the earliest expiry among every
authority dependency: the head, trust policy, each relied-on role grant, and
the latest qualification decision where applicable. At that exact instant the
result is no longer current. Unknown and invalid results expose no verification
timestamp or freshness TTL.

Offline verification is explicitly **as of** the supplied authority snapshot.
Missing, future, expired or mismatched authority remains `unknown`; it is not a
claim about live current state.

## Asset installation boundary

`verify_release_asset_authorization` reports authority
`verified|invalid|unknown` separately from lifecycle
`active|revoked|unknown`. It returns `authorized_reference` only for a fresh,
exact, verified and active reference. The asset registry invokes this helper
itself from signed bytes and external trust inputs; a caller-created result or
boolean is not installation authority. Content integrity of installed bytes is
a separate registry result.

All verification results retain:

```text
real_data_authorized = false
qualification_probe_authorized = false
```

An `approved` development decision may count only as signed development test
evidence. It is not scientific approval and cannot authorize execution.
