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

## E14 product gates

`traceback_runner.product_gates` separates a local measurement report from the
release decision. `ProductGateReport.capability_enabled` is fixed to `false`;
editing gate statuses or attaching an arbitrary reference can never enable a
capability. `derive_release_gate` enables only when all measured targets pass,
every gate is an observed pass, the exact approved host is bound, and the four
external artifacts match independently supplied Ed25519-signed evidence under
the external-release namespace and an independently provisioned authority-head
policy. Every external artifact must match its separately pinned signer and
authority-head digest. A caller-created development signer cannot establish
release authority. Approved-host evidence binds the exact run, host profile,
filter/render measurements, and memory measurement. Verification also requires
an independently supplied, non-revoked trust store at an injected, aware
verification time within each artifact's validity window. The external
contracts additionally require five representative users, keyboard plus
screen-reader plus 200 percent zoom audits, and reviewed browser captures.
Missing trust, bad signatures, digest mismatches, incomplete evidence, and
failed measurements remain unmet gates.

The synthetic privacy gate is adversarial rather than an absence check. It
injects each forbidden identifier, path, and sequence class through catalog
serialization, problem responses, and screenshot/accessibility contracts and
requires all three real paths to reject it. Persisted evidence binds the exact
sentinel class and digest, every path result, harness version, run, and host.
The network gate actively probes
`connect_ex` and `sendto` while the process guard also denies connect,
create-connection, DNS lookup, and connected-socket send variants. These local
evidence records bind exact operation names, target digests, denial results,
harness version, run, and host. These local
results still do not replace approved-host, accessibility, screenshot, or
five-provider evidence.

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
