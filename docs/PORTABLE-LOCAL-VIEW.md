# Portable local view contract (E13)

`evidence_inspector.portable_view` creates a bounded, framework-independent local artifact from the validated E02 and E05–E10 contracts. It does not add production UI, perform scientific computation, or authorize clinical interpretation.

The published directory has exactly three files:

- `view.json`: canonical view/filter state, exact source and compatibility identities, schema versions, accessibility metadata, limitations, denominators, and exact-value tables.
- `accessible-table.tsv`: deterministic long-form values for keyboard and screen-reader use. Every value has a typed state and unit; fractions retain numerator and denominator.
- `manifest.json`: exact file sizes and digests plus source, table, and view commitments.

Build, publication, replay, and verification require an independently supplied `PortableTrustContext`. Its exact sorted source identities and E02 manifest digests bind every bundle, result, method/version, asset, capability, compatibility authority, and source contract; a self-consistent rewrite of the artifact is insufficient.

Publication uses a private staging directory, fsyncs each file, then seals files to owner-read-only mode (`0400`) and the directory to owner-read/execute mode (`0500`) before source re-verification. It reopens every staged file through the pinned directory descriptor; each must still be a sealed, single-link regular file with the exact expected stat/digest vector. All verified descriptors remain open across the atomic no-overwrite rename. The installed directory and files receive bounded best-effort name, inode, mode, size, digest, inventory, and fsync checks. An invalid just-published directory is quarantined and removed only when its named root still matches the directory this call installed; a replacement winner is not touched. Conflict, storage, permission, source mutation, root replacement, hardlink, symlink, FIFO, mode change, and artifact tamper failures are typed. Failed staging data and publication locks are cleaned up.

Publication returns `PublishedPortableView`, not a path assertion. Its `snapshot` is the authoritative, immutable, content-addressed result; `untrusted_projection_path` is only the location where the mutable projection was installed. Verification likewise returns a `VerifiedPortableView` built entirely from one descriptor-pinned byte snapshot. It binds the parsed manifest and view, accessible-table bytes, trust-context digest, and an exact `snapshot_sha256`. Both result types expose `filesystem_projection_current` as the literal value `false`. Consumers must use the returned snapshot content, not reread the path and describe those later bytes as verified.

Verification opens and retains descriptors for all three sealed artifact files as one pinned inventory. Parsing and cross-file checks use bytes from that inventory, followed by bounded reverse-order stat/digest and filename checks. Those checks detect many races, but sequential filesystem observations cannot prove that a same-user-mutable directory is current at one atomic instant. A writer can restore modes, replace a file after its final observation, or insert a name after the final inventory read. The returned snapshot remains authoritative in those cases; the projection does not.

The `0400`/`0500` seal prevents ordinary accidental writes; it is not a security boundary against a malicious process running as the same OS user, because that user can restore write permissions. Deployments that need a current verified filesystem projection must add external isolation such as a separate OS identity, an immutable/content-addressed store, or an equivalent snapshot mechanism, then re-establish currency under that boundary.

The artifact preserves loading, empty, partial, error, success, stale, and revoked states. Non-ready, stale, and revoked states expose no exact rows. Any non-comparable or unknown compatibility outcome prohibits deltas. Output is restricted to controlled identifiers and aggregate values: aliases, filesystem paths, URIs, raw donor/patient/read/sample identifiers, nested URL/base64 encodings of those identifiers, sequence-like text, and free-form presentation labels are rejected or omitted. Typed SHA-256 fields remain allowed and are structurally distinct from controlled text tokens.

The CNA export includes coordinate grids, assets, methods, dosage chromosomes, bins, corrected-depth rows, masks, segments, model candidates, and insufficiency state. Free-text insufficiency reasons are represented by exact SHA-256 commitments and counts rather than copied presentation text.

Accessibility metadata fixes native table navigation, explicit column headers, deterministic focus order, polite status announcements, 200% zoom support, reflow, and no color-only, hover-only, or keyboard-trap behavior. Consumers still own their HTML semantics and must not weaken these commitments.

This contract is synthetic/local only. `product_release_authorized` and `diagnostic_interpretation_allowed` are always false.
