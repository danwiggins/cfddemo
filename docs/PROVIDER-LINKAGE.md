# Protected provider-local linkage

`evidence_inspector.provider_linkage` defines the first D01 identity boundary.
It is a pure contract and verifier layer. It does not discover identity, persist
records, create cohorts, or establish scientific compatibility.

Biological identity (`subject -> collection -> specimen -> aliquot`) is separate
from technical processing (`run -> analysis -> measurement`). A rerun or
reanalysis can therefore retain the same collection identity without becoming a
new biological timepoint. Optional links are explicitly `known` or `unknown`;
empty strings and inferred links are invalid.

All identifiers are provider-scoped opaque typed tokens. The contracts contain
no names, free-text labels, donor data, paths, read IDs, sequences, or raw input
hashes. They belong only in provider-managed protected storage and must not be
copied into E04 public catalog pages, E10 drawer artifacts, logs, support
bundles, screenshots, or exports.

## Authority

The caller supplies a `ProviderTrustSnapshot` through an independently
provisioned channel and pins its exact SHA-256 digest. Approvals are Ed25519
signatures over one canonical payload binding provider, issuer/key, opaque
principal, role, purpose, exact proposed linkage revision, exact trust snapshot
ID/revision/digest, nonce, and validity window. An approval cannot predate its
trust snapshot. Trust and approvals are never derived from the linkage proposal.

Initial projection requires one active `linker` approval. Correction or
tombstoning requires exactly two distinct principals: one `linker` and one
`reviewer`, both signing the same revision digest. Reused approvals/nonces,
duplicate principals, stale windows, revoked or unknown issuers, wrong grants,
wrong purpose, stale trust head, altered revision bytes, and invalid signatures
all disable the change. Typed correction reasons must match the exact changed
field class; a subject relink labeled as a technical correction is rejected. A
local B session or two clicks by one principal cannot satisfy dual approval.

When trust or approval input is absent, both `linkage_authorized` and
`comparison_linkage_eligible` are false. A valid signed approval proof may set
`linkage_authorized`, but D01 always leaves `comparison_linkage_eligible` false:
only the later protected transactional store can consume approvals and activate
a revision. Scientific compatibility, immutable membership, missingness and
denominator gates remain independent requirements.

## Replay fence and append-only projection

`prepare_authorized_linkage_revision` produces self-contained proof bytes for a
future protected transaction. It does not consume an approval or activate a
link. The legacy-shaped `authorize_and_consume_linkage_revision` and
`project_active_linkages` entry points fail closed unconditionally in D01. A
caller-created consumption ledger, including one containing perfectly matching
rows, therefore cannot become authority. Durable cross-process replay fencing,
idempotent exact retry and active projection remain disabled until the protected
transactional store atomically commits them.

Every correction is a new `LinkageRevision` binding the digest of its immediate
predecessor. Historical revisions remain unchanged. Structural history
validation rejects broken chains, collection-to-subject,
specimen-to-collection/subject, and aliquot-to-specimen/collection/subject
conflicts. Analysis and measurement identifiers remain unique across the full
append-only history, including after tombstone; corrections may retain them only
within the same linkage chain. Sibling specimens and aliquots under one
collection and distinct technical reruns remain valid. Wrong-subject and
wrong-collection corrections must replace their descendant biological tokens so
the hierarchy never acquires two parents.

This D01 contract does not itself persist record supersession. The additive
D04 `RecordSupersessionStore` consumes this store's live active projection to
provide append-only result supersession, reanalysis-chain enforcement, and
derived-comparison invalidation. Retention execution and provider backup and
restore remain separate deployment responsibilities.
