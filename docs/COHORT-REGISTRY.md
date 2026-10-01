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
manifest predecessor. The chain starts from a genesis digest over the immutable
registry metadata, so the state head authenticates both identity and history. A
restart of any existing root requires separately retained registry ID, registry
epoch, and expected head; missing metadata is never recreated. Deletion,
truncation, metadata substitution, or rollback therefore fails closed instead
of blessing a recomputed older state. A process-wide monotonic head fence also
prevents one live instance from accepting rollback to its own stale pre-append
head after a peer advances the registry. Root, object-directory, journal, lock,
metadata, and manifest file types,
ownership, modes, descriptors, and inode bindings are revalidated. Publication
uses descriptor-relative exclusive temporary files, fsync, and hard-link
adoption without overwriting an existing object. An exact content-addressed
object left between object publication and journal append is safely adopted on
retry only when its canonical bytes match. Exact orphan temporary names
from an interrupted publication are removed under the registry lock; unrelated
entries fail closed. A failed journal append truncates the journal back to its
pre-append size, so a torn suffix cannot wedge the registry; the retry adopts
the exact published object.

Every public entrypoint also verifies a process-private seal over the registry's
authority-critical instance state. The registry metadata is reread from its
bound descriptor, and the genesis and process-head key are rederived from those
canonical bytes plus the live root descriptor. Replacing caller-visible
metadata, trust pins, linkage store, storage identities, head key, or trusted
head therefore fails closed before the value can authorize a result.

The protected `resolve` result carries the exact manifest, registry identity,
state version, state head, and manifest digest. Resolution and each complete
selector page hold one linkage authority fence and one registry snapshot lock
through validation and return construction. Corrections and tombstones do
not delete historical manifests; they make the selector stale and prevent
protected resolution.

`resolve_history_view(selector_id, cohort_version)` is the separate bounded
read for history and reopen. It returns `CohortHistoryView`
(`traceback.cohort-history-view.v1`), not `RegisteredCohortHistory`: the exact
immutable registered manifests through the requested version (same selector
and 1..100,000 version bounds as `resolve_history`), registry ID/epoch, state
version/head, the latest registered version for the selector, and the
`current|stale` authority state observed under the same linkage fence and
shared registry lock held through return construction. A stale version carries
the controlled reason `linkage_authority_not_current`; the contract rejects a
stale state without a reason and a current state with one. The view is marked
`protected_only: true` and `presented_as_current: false` in every state, uses
distinct field names (`historical_manifests`,
`historical_selected_manifest_sha256`), and fails `RegisteredCohortHistory`
validation, so it cannot be accepted where current cohort authority is
required. A caller that needs current authority must call `resolve` or
`resolve_history`, which still reject stale authority unchanged. Authority
state reflects live linkage validity of the selected version only, matching
the selector page; a newer registered version is reported separately through
`latest_registered_cohort_version` rather than as staleness.

The browser-facing selector projection is deliberately separate. It contains
only a registry-scoped opaque selector, version, content/policy/anchor digests,
bounded counts, and `current|stale` authority state. It never contains cohort,
provider, subject, collection, specimen, run, analysis, timepoint, path,
sequence, or free-text identity values. Pagination is deterministic and
bounded to 100 rows. The loopback session boundary must authorize access before
this projection is served; this module is not an authentication system.

`backup_bytes` captures metadata, the exact journal, and every committed
immutable object under one shared registry lock in a bounded canonical bundle.
`restore` requires independently supplied expected registry ID, epoch, and
state head and validates the complete bundle, metadata-bound journal chain,
history, trust pins, and exact linkage-store identity before
creating a new private root; it refuses an existing target and reopens the
result through the normal descriptor and inode checks. The root and objects
directories must be empty when verified, and restore holds the new target's
registry lock exclusively while publishing into it. If restore fails after
creating the target, including when the final reopen fails, it reacquires that
lock and cleans up only when the target holds nothing but what restore wrote
(no extra entries, journal bytes unchanged); a target another instance has
committed into is left untouched. Cleanup removes only the exact entries it
recorded creating (by name, never by directory sweep), then the directories
with `rmdir`, and fsyncs the parent, so the same target can be retried. When the root cannot be opened it removes only an empty target; any
entry it did not create keeps its directory in place. Backup bytes contain
protected manifest content and therefore are not an export artifact or safe
browser response. Provider-managed encryption, retention, and backup media
policy remain deployment inputs rather than claims made by this code.

D06, D09, and D08 must bind the registry identity and state head returned by
this module rather than accept caller-built manifest bytes or browser aliases
as authority.
