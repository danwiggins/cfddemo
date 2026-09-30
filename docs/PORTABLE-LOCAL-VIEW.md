# Portable local view contract (E13)

`evidence_inspector.portable_view` creates a bounded, framework-independent local artifact from the validated E02 and E05–E10 contracts. It does not add production UI, perform scientific computation, or authorize clinical interpretation.

The published directory has exactly three files:

- `view.json`: canonical view/filter state, exact source and compatibility identities, schema versions, accessibility metadata, limitations, denominators, and exact-value tables.
- `accessible-table.tsv`: deterministic long-form values for keyboard and screen-reader use. Every value has a typed state and unit; fractions retain numerator and denominator.
- `manifest.json`: exact file sizes and digests plus source, table, and view commitments.

Build, publication, replay, and verification require an independently supplied `PortableTrustContext`. Its exact sorted source identities and E02 manifest digests bind every bundle, result, method/version, asset, capability, compatibility authority, and source contract; a self-consistent rewrite of the artifact is insufficient.

Publication uses a private staging directory, fsyncs each file, re-verifies the sorted source identities immediately before publication, then reopens every staged file through the pinned directory descriptor. Each file must still be a single-link regular file with the exact expected size, digest, and bytes before a second fsync and atomic no-overwrite rename. The named staging and parent roots must still resolve to their pinned device/inode identities. Conflict, storage, permission, source mutation, root replacement, hardlink, symlink, FIFO, and artifact tamper failures are typed. Failed staging data and publication locks are cleaned up.

The artifact preserves loading, empty, partial, error, success, stale, and revoked states. Non-ready, stale, and revoked states expose no exact rows. Any non-comparable or unknown compatibility outcome prohibits deltas. Output is restricted to controlled identifiers and aggregate values: aliases, filesystem paths, URIs, raw donor/patient/read/sample identifiers, nested URL/base64 encodings of those identifiers, sequence-like text, and free-form presentation labels are rejected or omitted. Typed SHA-256 fields remain allowed and are structurally distinct from controlled text tokens.

The CNA export includes coordinate grids, assets, methods, dosage chromosomes, bins, corrected-depth rows, masks, segments, model candidates, and insufficiency state. Free-text insufficiency reasons are represented by exact SHA-256 commitments and counts rather than copied presentation text.

Accessibility metadata fixes native table navigation, explicit column headers, deterministic focus order, polite status announcements, 200% zoom support, reflow, and no color-only, hover-only, or keyboard-trap behavior. Consumers still own their HTML semantics and must not weaken these commitments.

This contract is synthetic/local only. `product_release_authorized` and `diagnostic_interpretation_allowed` are always false.
