# Protected cohort registry

Status: local synthetic D05 persistence contract. It does not authorize real
provider operation, clinical use, scientific claims, export, or Epic D release.

`CohortRegistry` publishes exact canonical D05 v2 manifests as immutable,
content-addressed objects in a provider-private directory. The registry is
bound to one exact protected linkage-store identity and independently captured
provider trust pins. Registration validates the manifest before taking the
registry lock, revalidates its complete consecutive history and live authority
under the exclusive lock, then atomically adopts one immutable object. Reusing
the same bytes is idempotent; a competing version or changed bytes fail closed.

The object directory is append-only. State identity is the deterministic hash
of every registered digest and canonical byte string, not filesystem order or
mtime. Root, object-directory, lock, metadata, and manifest file types,
ownership, modes, descriptors, and inode bindings are revalidated. Publication
uses descriptor-relative exclusive temporary files, fsync, and hard-link
adoption without overwriting an existing object. Exact orphan temporary names
from an interrupted publication are removed under the registry lock; unrelated
entries fail closed.

The protected `resolve` result carries the exact manifest, registry identity,
state version, state head, and manifest digest. It revalidates current linkage
and collection-event authority before returning. Corrections and tombstones do
not delete historical manifests; they make the selector stale and prevent
protected resolution.

The browser-facing selector projection is deliberately separate. It contains
only a registry-scoped opaque selector, version, content/policy/anchor digests,
bounded counts, and `current|stale` authority state. It never contains cohort,
provider, subject, collection, specimen, run, analysis, timepoint, path,
sequence, or free-text identity values. Pagination is deterministic and
bounded to 100 rows. The loopback session boundary must authorize access before
this projection is served; this module is not an authentication system.

Current limitations are explicit: a supported encrypted backup/restore bundle
and cross-process restore rehearsal remain required before durable cohort
publication can be called operationally complete. D06, D09, and D08 must bind
the registry identity and state head returned by this module rather than accept
caller-built manifest bytes or browser aliases as authority.
