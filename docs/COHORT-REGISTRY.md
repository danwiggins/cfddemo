# Protected cohort registry

Status: local synthetic D05 persistence contract. It does not authorize real
provider operation, clinical use, scientific claims, export, or Epic D release.

`CohortRegistry` publishes exact canonical D05 v2 manifests as immutable,
content-addressed objects in a provider-private directory. The registry is
bound to one exact protected linkage-store identity and independently captured
provider trust pins. Registration holds one linkage-store authority fence
across its in-fence manifest validation, exclusive registry commit, durable
journal append, final reload, and returned receipt. Reusing the same bytes is
idempotent; a competing version or changed bytes fail closed.

The object directory is append-only. A durable append-only journal assigns each
commit a sequence, predecessor head, exact cohort/version, manifest digest, and
manifest predecessor. The chained journal entry digest is the state head. A
restart of an existing registry requires a separately retained expected head;
deletion, truncation, or rollback therefore fails closed instead of blessing a
recomputed older state. Root, object-directory, journal, lock, metadata, and manifest file types,
ownership, modes, descriptors, and inode bindings are revalidated. Publication
uses descriptor-relative exclusive temporary files, fsync, and hard-link
adoption without overwriting an existing object. An exact content-addressed
object left between object publication and journal append is safely adopted on
retry only when its canonical bytes match. Exact orphan temporary names
from an interrupted publication are removed under the registry lock; unrelated
entries fail closed.

The protected `resolve` result carries the exact manifest, registry identity,
state version, state head, and manifest digest. Resolution and each complete
selector page hold one linkage authority fence and one registry snapshot lock
through validation and return construction. Corrections and tombstones do
not delete historical manifests; they make the selector stale and prevent
protected resolution.

The browser-facing selector projection is deliberately separate. It contains
only a registry-scoped opaque selector, version, content/policy/anchor digests,
bounded counts, and `current|stale` authority state. It never contains cohort,
provider, subject, collection, specimen, run, analysis, timepoint, path,
sequence, or free-text identity values. Pagination is deterministic and
bounded to 100 rows. The loopback session boundary must authorize access before
this projection is served; this module is not an authentication system.

`backup_bytes` captures metadata, the exact journal, and every committed
immutable object under one shared registry lock in a bounded canonical bundle.
`restore` requires an independently supplied expected state head and validates
the complete bundle, journal chain, history, trust pins, and exact linkage-store identity before
creating a new private root; it refuses an existing target and reopens the
result through the normal descriptor and inode checks. Backup bytes contain
protected manifest content and therefore are not an export artifact or safe
browser response. Provider-managed encryption, retention, and backup media
policy remain deployment inputs rather than claims made by this code.

D06, D09, and D08 must bind the registry identity and state head returned by
this module rather than accept caller-built manifest bytes or browser aliases
as authority.
