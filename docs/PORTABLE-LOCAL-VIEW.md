# Portable local view contract (E13)

`evidence_inspector.portable_view` creates a bounded, framework-independent local artifact from the validated E02 and E05–E10 contracts. It does not add production UI, perform scientific computation, or authorize clinical interpretation.

The published directory has exactly three files:

- `view.json`: canonical view/filter state, exact source and compatibility identities, schema versions, accessibility metadata, limitations, denominators, and exact-value tables.
- `accessible-table.tsv`: deterministic long-form values for keyboard and screen-reader use. Every value has a typed state and unit; fractions retain numerator and denominator.
- `manifest.json`: exact file sizes and digests plus source, table, and view commitments.

Build, publication, replay, and verification require an independently supplied `PortableTrustContext`. Its exact sorted source identities and E02 manifest digests bind every bundle, result, method/version, asset, capability, compatibility authority, and source contract; a self-consistent rewrite of the artifact is insufficient.

Publication uses a private staging directory, fsyncs each file, then seals files to owner-read-only mode (`0400`) and the directory to owner-read/execute mode (`0500`) before source re-verification. It reopens every staged file through the pinned directory descriptor; each must still be a sealed, single-link regular file with the exact expected stat/digest vector. All verified descriptors remain open across the atomic no-overwrite rename. Before success, the installed directory name must resolve to the same pinned directory, and every installed filename must resolve to the same pinned file inode. The complete stat/digest vector is recomputed in reverse order from all still-open descriptors, followed by a final name-to-inode pass and fsync. An invalid just-published directory is quarantined and removed only when its named root still matches the directory this call installed; a replacement winner is not touched. The named staging and parent roots must still resolve to their pinned device/inode identities. Conflict, storage, permission, source mutation, root replacement, hardlink, symlink, FIFO, mode change, and artifact tamper failures are typed. Failed staging data and publication locks are cleaned up.

Verification opens and retains descriptors for all three sealed artifact files as one pinned inventory. Parsing and cross-file checks use bytes from that inventory, followed by a reverse-order recomputation of the complete stat/digest vector and a final exact filename-to-inode/link/type/mode/size pass while all original and final-name descriptors remain open. Replacing or changing a previously read file cannot validate successfully within that operation.

The `0400`/`0500` seal prevents ordinary accidental writes; it is not an isolation boundary against a malicious process running as the same OS user, because that user can restore write permissions. The implementation detects permission, inode, and content changes during its bounded publication or verification operation. It cannot prevent a same-user process from modifying an artifact after the operation returns. Deployments requiring that threat boundary must use separate OS identities, immutable storage, or equivalent external isolation.

The artifact preserves loading, empty, partial, error, success, stale, and revoked states. Non-ready, stale, and revoked states expose no exact rows. Any non-comparable or unknown compatibility outcome prohibits deltas. Output is restricted to controlled identifiers and aggregate values: aliases, filesystem paths, URIs, raw donor/patient/read/sample identifiers, nested URL/base64 encodings of those identifiers, sequence-like text, and free-form presentation labels are rejected or omitted. Typed SHA-256 fields remain allowed and are structurally distinct from controlled text tokens.

The CNA export includes coordinate grids, assets, methods, dosage chromosomes, bins, corrected-depth rows, masks, segments, model candidates, and insufficiency state. Free-text insufficiency reasons are represented by exact SHA-256 commitments and counts rather than copied presentation text.

Accessibility metadata fixes native table navigation, explicit column headers, deterministic focus order, polite status announcements, 200% zoom support, reflow, and no color-only, hover-only, or keyboard-trap behavior. Consumers still own their HTML semantics and must not weaken these commitments.

This contract is synthetic/local only. `product_release_authorized` and `diagnostic_interpretation_allowed` are always false.
