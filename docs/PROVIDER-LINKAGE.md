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
`comparison_linkage_eligible` are false. A true linkage eligibility flag means
only that protected identity authority passed; E05/Epic D scientific
compatibility, immutable membership, missingness and denominator gates remain
independent requirements.

## Replay fence and append-only projection

`authorize_and_consume_linkage_revision` returns a self-contained authorized
record plus an append-only approval-consumption ledger. Approval IDs and nonces
are single-use across revisions; an exact retry is idempotent. Active projection
replays every signature and decision, requires an independently supplied trust
pin for every provider, and refuses any approval absent from the consumption
ledger. A self-consistent authorization object is not authority.

The consumption ledger is a pure contract in D01. Durable atomic commit of the
authorized revision and consumption entries remains disabled until D04 supplies
the protected transactional store. Callers must not treat an in-memory ledger as
a production replay fence.

Every correction is a new `LinkageRevision` binding the digest of its immediate
predecessor. Historical revisions remain unchanged. The pure active projection
selects only the latest valid revision, rejects broken chains, conflicting
collection/specimen/aliquot parentage, duplicate analysis or measurement
identities, and ledgers above the explicit bound. It allows distinct technical
analyses of the same biological collection when their analysis and measurement
identities remain distinct, and omits a tombstoned chain without deleting its
history.

Durable SQLite schema, concurrency, backup/restore, retention execution,
derived-record invalidation and supersession-cycle enforcement belong to the
later D04/D05 persistence PR. This contract does not claim they exist.
