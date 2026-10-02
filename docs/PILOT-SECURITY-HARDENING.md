# Pilot security hardening

Status: spec, 2026-10-02. Source: a structural security, privacy and trust review of `main` at `eaac96a`, verified against the code on `d3739ca`.

## Context

Traceback's current threat model has three premises. In-process code mutation is out of scope. Races by the same OS user on the filesystem are out of scope. The process/user boundary is the trust boundary. That is correct for a developer's own machine with synthetic data.

E9 (`docs/EPICS.md` § E9) is a paid pilot on a provider workstation with donor data. That setting breaks three assumptions at once:

1. The person at the keyboard may not be the person the operator authorized.
2. Files under `$HOME` get backed up and cloud-synced.
3. Every local account can reach the loopback port.

This spec closes the gaps that code can close now, and names the steps that need a person, a host, or a decision outside the repo.

Who is affected:
- **Provider operators:** a reader link must give a reader only what the grant covers.
- **Donors:** identifier-bearing files must not leave the machine in plaintext.
- **Us:** we must not claim "verified" from a file that a registry revocation never reaches.

## Current state (verified 2026-10-02 on `d3739ca`)

| Exit or control | Today | Gap |
|---|---|---|
| Explorer routes `/api/v1/explorer/catalog`, compare, and result document (`traceback_runner/web/server.py:842-963`) | `boundary.authorize(request)` only | A session created from a reader launch link is a full B01 session. It reads every E04 result and every E06–E13 artifact, whatever its grant scope. |
| Reader launch (`server.py:1367-1389`, `web/reader_session.py:191-219`) | The link carries a fresh B01 bootstrap plus a launch credential. The page exchanges the bootstrap first, then POSTs the credential. | Between the two exchanges the session is unrestricted. Any B01 session can redeem any pending credential, because the two secrets are not bound to each other. |
| Bootstrap exchange (`web/auth.py:245-265`) | The single slot is cleared on every attempt, valid or not. 8 attempts in 60 s also clear it. | Any local process can burn the operator's link (a local denial of service). |
| Session lifetime (`web/auth.py:158`) | 8 h, no idle timeout, no logout route | Revoking a grant ends E12 reads only. The B01 session lives on. |
| Reader authority key (`traceback_runner/reader_cli.py:69-70, 248-257`) | Unencrypted PKCS#8 PEM at 0600 under `~/.traceback/reader-authority`, next to the pins | Anything running as the same user, plus backup and sync tools, can copy it. |
| Registry backups (`backup_bytes()` on all 12 journaled registries) | Plaintext canonical bytes, including the subject, collection, specimen and run tokens in cohort manifests | A backup runbook would produce plaintext identifier dumps. |
| `traceback status` "verified" (`traceback_runner/cli.py:634-642`) | Reads the fixed file `trust/development-result-trust.json` | A result-trust registry revocation never reaches it. |
| D07 public `compare_repeatability(result_trust_document=...)` and the registry's fixed-document branch (`evidence_inspector/repeatability_comparison.py:1309-1310, 1367-1368`; `repeatability_comparison_registry.py:1717-1722`) | Accepts a caller-supplied trust document | This is a downgrade path next to the registry-bound path. |
| `ResultCatalog(trust_store=...)` (`evidence_inspector/result_catalog.py:1119-1150`) | Takes exactly one of `trust_store` or `result_trust_registry`. A catalog root does not record which one it was created with. | A registry-bound catalog can be reopened with a caller store. |
| Public text gate (`traceback_runner/web/contracts.py:55-107`) | A regex blocklist over string values only; dict keys are never checked. It catches paths, URIs, `source/donor/patient/sample/read/query/path` + `id`, credentials, and IUPAC runs of 24 or more. | `subject`, `specimen`, `collection`, `run`, `provider`, `flowcell`, MRN- and date-of-birth-shaped tokens all pass. Operator-entered labels (`AccessibleLabel`/`qc_label`, `result_view.py:96-112`; E06 `CALLER_ASSERTED_FIELDS`; D10 covariate tokens) reach `static/app.js:34-42`, which renders whole artifacts with `JSON.stringify`. |
| Server diagnostics (`server.py:641, 666`) | `handle_error` swallows everything; `log_message` is disabled | An unexpected route exception drops the connection and leaves no record. |
| Launch CLI (`reader_cli.py:827-830`) | Any `RuntimeError` prints "Too many unused links" | After the watchdog shuts the listener (`server.py:1281-1290`), the runtime stays registered and `launch` keeps printing dead links. |
| Error mapping in D08 (`evidence_inspector/longitudinal_workspace.py:361-421, 2062-2068`) | Any `Exception` becomes `integrity_failure` or `permission_denied` | A programming bug reads as tampering or a denial, with no trace. |
| Result-trust registry handle | Every reader handle can call `add_key` | No read-only handle exists. |
| `_authority_recover` (`reader_cli.py:560-609`) | Reads the journal and objects with plain `open`/`read_bytes` | Bypasses the registry's descriptor-hardened readers. |
| Platform | `fcntl` in 21 modules | Linux and macOS only. Many MinKNOW workstations run Windows. |

### Do not touch (correct as built)

- Loopback bind; exact Host/Origin byte match; HttpOnly SameSite=Strict cookie; CSRF header on mutations; fragment-only one-use 60 s links; 256-bit tokens stored only as hashes.
- Reader fence semantics: own-grant re-resolution on entry and exit, head-in-chain, signature against current trust, clock-rollback bound.
- Forward-only result trust with tombstones; D07 per-key trust projection.
- Static error strings, the support-bundle allowlist, no credential in `instance.json`.
- The D08 public projection, which structurally excludes protected identifiers (tested with seeded tokens).

## Deployment profile (new; every refusal below depends on it)

Today only the reader registry has profiles (`ReaderAuthorizationProfile.SYNTHETIC|PROVIDER`, `evidence_inspector/reader_authorization_registry.py:164`). This spec adds one process-wide deployment profile.

- `traceback_runner/deployment_profile.py` defines `DeploymentProfile(StrEnum)` with `DEVELOPMENT = "development"` and `PROVIDER = "provider"`, and `resolve_profile(cli_value: str | None, env: Mapping[str, str], *, forced: DeploymentProfile | None = None) -> DeploymentProfile`.
- `--profile` is a top-level option on the `traceback` parser, before the subcommand (`traceback --profile provider status ...`). `reader_cli.main` calls `resolve_profile` with `forced=PROVIDER`; when `forced` is set and `cli_value` or the env names a different profile, it exits 2.
- Resolution order:
  1. The CLI flag `--profile {development,provider}` on `traceback` and `traceback reader`.
  2. The env var `TRACEBACK_PROFILE`.
  3. The default: `development`.
  - `traceback reader ...` always resolves to `provider`, because `reader_cli.PROFILE` is already `PROVIDER`. An explicit `--profile development` there exits 2.
- An unknown value exits 2 with `unknown profile`.
- Library code never reads the environment. Every library refusal below takes an explicit keyword `profile: DeploymentProfile = DeploymentProfile.DEVELOPMENT`, and the runner passes the resolved value. The default keeps every existing caller and test unchanged.

## Decisions (least-blocking defaults; the operator may override)

| # | Decision | Default taken | Why |
|---|---|---|---|
| P1 | Key custody backend | A passphrase-encrypted PKCS#8 key (`cryptography` `BestAvailableEncryption`, already a dependency at 46.0.3). The passphrase comes from an interactive prompt or `TRACEBACK_READER_KEY_PASSPHRASE`. The `provider` profile refuses an unencrypted key. OS keychain and hardware keys are deferred. | No new dependency, works on macOS and Linux, and closes plain file theft. Keychain or HSM is a host decision. |
| P2 | Encryption at rest for registries | Registries stay plaintext on disk. Exported backups are encrypted. The `provider` profile refuses a registry or key root inside a known sync folder (iCloud Drive, Dropbox, OneDrive, Google Drive). | Whole-store encryption touches every reader in 12 registries. The real exit is exported backups and sync, which this closes. Revisit if a provider's policy requires full-disk-equivalent controls, and use OS full-disk encryption first. |
| P3 | Windows | Unsupported. The `provider` profile refuses to start on a non-POSIX platform, with a clear message. | `fcntl` is everywhere. Porting is a separate project, triggered only once a pilot host is chosen. |
| P4 | Operator-entered text | Classification, not a vocabulary. Every operator-entered string is typed `OperatorText`, rendered as escaped text labelled "operator-entered", and excluded from any export or release surface. The blocklist is extended but is not treated as the control. | A closed vocabulary needs provider input that doesn't exist yet. Classification is honest now and still lets a vocabulary be added later. |
| P5 | Reader access to existing explorer routes | Denied. A session bound to a reader grant may use only the session routes and the reader-gated `/api/v1/longitudinal/*` routes. Explorer and job routes require an operator session. | Per-reader scoping of the E04/E06–E13 explorer has no grant model yet. Denying it is the safe, small change. |
| P6 | Backup encryption scheme | AES-256-GCM with an scrypt-derived key (n=2^17, r=8, p=1) and a versioned header `TBXBK1`, using `cryptography` only. A `.tbxbackup` file wraps the existing canonical bytes unchanged. | Small and auditable, with no new dependency. `restore` takes either the plaintext or the encrypted form. |

## Proposed change: 7 code work items + 1 external

```
H1 Web boundary: session kinds, launch binding, hygiene ─┐
H2 Trust path retirement + read-only trust handle        ├─> H7 Broker-bound D08 binding (after browser PR)
H3 Key custody (P1) + hardened recovery                  │
H4 Backups + sync-folder refusal (P2, P6)                │
H5 Operator text classification + validator (P4)         │
H6 Diagnostics + launch watchdog + error separation ─────┘
X1 External: cross-account test on the pilot host, OS choice (P3)
```

Sequencing:
- H1 and H6 both touch `server.py` and `auth.py`, and they collide with the in-flight E12 browser branch (`epic-e/e12-browser-integration`). Land the browser PR first, then H1, then H6.
- H2, H3, H4 and H5 touch disjoint modules and can run in parallel with the browser work.
- H7 needs the browser PR's composition and routes.

### H1 Web boundary: session kinds, launch binding, hygiene (M)

- `_SessionRecord` gains `kind: Literal["operator", "reader"]`.
- `BootstrapBroker.issue_bootstrap(authority, *, kind="operator", launch_credential_sha256=None)`. The broker keeps its single bootstrap slot, as today, so issuing any new bootstrap or link replaces a pending, unexchanged one.
  - `issue_reader_launch_url` issues the bootstrap with `kind="reader"` and the digest of the credential it puts in the same link.
  - Exchanging a reader bootstrap creates a session with `kind="reader"` and `launch_credential_sha256` set.
  - `ReaderSessionBinder.exchange_launch_credential` refuses `ReaderDenialReason.LAUNCH_CREDENTIAL_INVALID` unless the session is `kind="reader"` and the supplied credential's digest equals the session's `launch_credential_sha256`. This closes the window between the two exchanges and F3.
  - The binder's pending-credential store (16 entries) is unchanged. A credential is consumed on its first presentation, whether or not binding then succeeds, as today. One that is never presented expires at 60 s. Logout or session expiry removes the session's binding; a consumed credential is never reusable.
- Route policy is one exact table, `_ROUTE_KINDS`, in `server.py`. Matching is exact path, or the named compiled regex; there is no prefix matching. A route missing from the table is denied for every kind, and a test asserts that every handled path is listed.

  | Method | Route | Kinds |
  |---|---|---|
  | GET | each path in `application.assets` (the packaged static map, keyed by exact path) | public, unchanged; listed by iterating the map, not by name |
  | POST | `/api/v1/session/bootstrap` | none required (it creates the session) |
  | POST | `/api/v1/session/validate` | operator, reader |
  | POST | `/api/v1/session/reader-launch` | reader |
  | POST | `/api/v1/session/logout` (new) | operator, reader |
  | GET | `/api/v1/jobs` | operator |
  | GET | `/api/v1/explorer/catalog` | operator |
  | GET | `/api/v1/explorer/compare` | operator |
  | GET | `_EXPLORER_RESULT_ROUTE` (`/api/v1/explorer/results/result_<40hex>`) | operator |
  | GET | each `/api/v1/longitudinal/...` route the browser PR adds (H1 lands after it; the H1 builder copies that PR's exact route constants into this table) | reader |
  | GET | `/api/v1/diagnostics` (new, H6) | operator |

  A denied kind returns the existing bounded `TBX-AUTH-*` problem shape, with status 403 and code `TBX-AUTH-007`. The kind check runs after `boundary.authorize`, so an unauthenticated request still gets 401 first.
- Bootstrap exchange no longer burns the slot on a malformed or wrong code. It burns only on a correct code, or when the rate limit trips. The rate limit stays and is counted per window; tripping it clears the slot, as today.
- Idle timeout: `_SessionRecord` gains `last_seen_at`. `require_session` rejects with 401 `TBX-AUTH-001` when `now - last_seen_at > 1200` s, or `now > expires_at` (the 8 h limit, unchanged). `last_seen_at` refreshes only after a request passes authorization and the kind check, so denied requests don't extend a session.
- `POST /api/v1/session/logout` (Origin plus CSRF) deletes the record and returns 204. A repeat returns 401.
- Grant revocation: when `ReaderSessionBinder.reader_authorization` gets `ReaderAuthorizationDenied` because the bound grant is revoked, expired or superseded, it also calls `broker.end_session(session_token)`. The response is the existing bounded `permission_denied` problem (HTTP 403, code `TBX-READER-DENIED`, unchanged). The next request on that cookie gets 401 `TBX-AUTH-001`.

Acceptance:
1. A reader-launched session gets 403 `TBX-AUTH-007` on `/api/v1/explorer/catalog`, compare, result document and `/api/v1/jobs`. An operator session gets 200 on all four, as before.
2. A session from operator bootstrap A cannot redeem the launch credential of link B (403). A reader session from link B redeems only B's credential.
3. A wrong bootstrap code does not invalidate the valid pending code: a POST with garbage followed by the real code gives 401 then 200.
4. After 20 min idle (fake clock), any request gets 401. Logout followed by a request gets 401.
5. Revoking the bound grant, then a longitudinal GET, gives 403 `TBX-READER-DENIED`. A following `POST /api/v1/session/validate` on the same cookie gives 401 `TBX-AUTH-001`.
6. Existing `tests/web/` pass unchanged, except tests that asserted the burn-on-any-attempt behaviour; those are updated with a comment citing H1.

### H2 Trust path retirement and a read-only trust handle (M)

- `traceback status`: the JSON field `verified` becomes a tri-state string: `"verified"`, `"not_verified"` or `"unknown"`. The text output prints the same word. The exit code stays 0 for all three, because `status` reports state and doesn't gate it; nonzero stays reserved for an unknown job, as today.
  - With `--trust-registry PATH` (the flag `traceback_runner/cli.py` already parses for other commands, via `_require_trust_registry_identity`), verification uses `ResultTrustRegistry(...).trust_store()` under its read fence.
  - Without the flag: in `development` it reads `trust/development-result-trust.json` as today and adds `"trust_source": "development_file"`; in `provider` it prints `unknown` with `"trust_source": "none"`.
  - An unreadable or unsafe registry gives `not_verified` with `"trust_source": "registry_error"`. The status command never raises.
- `ResultCatalog` records its trust binding on first creation of a root, in the existing catalog metadata table, as row `trust_binding`. The value is canonical JSON `{"kind":"registry","registry_id":<RegistryId>,"registry_epoch_sha256":<64hex>}` or `{"kind":"caller_store"}`. The ID and epoch come from `ResultTrustRegistry.registry_identity()`, already used by E04 since #74. The epoch is fixed when the registry is created and does not change as keys are added or revoked. It changes only when a registry is restored into a new root, which is deliberately a new trust instance. A catalog bound to the old epoch must then be rebuilt from its bundles; that rebuild is out of scope.
  - On reopen, a mismatch in kind, ID or epoch raises `ResultCatalogUnsafe("catalog trust binding changed")`.
  - An existing root without the row reads as `caller_store` and is never rewritten.
  - With `profile=PROVIDER`, `caller_store` (recorded or requested) raises.
- D07: the `provider` profile refuses `compare_repeatability(result_trust_document=...)` and the registry's fixed-document branch. The `development` profile keeps them.
- `ResultTrustRegistry.reader()` returns a `ResultTrustReader`, a frozen wrapper around the same instance. It exposes only `snapshot()`, `trust_store()`, `authority_read_fence()`, `registry_identity()` and `root`, and no mutators. These signatures accept `ResultTrustRegistry | ResultTrustReader`:
  - `ResultCatalog.__init__(result_trust_registry=)`;
  - `CohortImport` (D06) constructor `result_trust_registry=`;
  - `RepeatabilityComparisonRegistry.__init__(result_trust_registry=)`;
  - `CompositeAuthorityCoordinator` wiring checks, compared by `registry_identity()`.
  Type checks use `isinstance` against both classes.
- `product_gates.py:995` (empty `TrustStore()` on a fixture catalog) is labelled a development fixture and runs under the `development` profile only.

Acceptance:
7. Revoking a key in the registry flips `traceback status` for a bundle signed by that key from verified to not verified, with no file edit.
8. Reopening a registry-bound catalog root with `trust_store=` raises `ResultCatalogUnsafe`. The reverse also raises.
9. In the `provider` profile, D07 with `result_trust_document=` raises. In `development` it behaves as today.
10. `ResultTrustReader` has no `add_key`, `revoke` or `restore` attribute. The E04, D07 and composite-fence tests pass with a reader handle.

### H3 Key custody and hardened recovery (M)

- `reader_cli` writes new authority keys as encrypted PKCS#8 (P1).
- Passphrase rules:
  - read from `TRACEBACK_READER_KEY_PASSPHRASE`, else from `getpass.getpass` when stdin is a TTY, else exit 2 with `passphrase required`;
  - UTF-8 encoded, 12–1024 bytes; empty or short exits 2 on create;
  - never echoed, logged, or included in an exception message.
- In the `provider` profile, loading an unencrypted key raises, with a migration hint: `traceback reader authority rekey --encrypt`.
- New `traceback reader authority rekey --encrypt`. It reads the current key unencrypted, or with the old passphrase from `TRACEBACK_READER_KEY_PASSPHRASE_OLD` or a prompt. It reads the new passphrase from `TRACEBACK_READER_KEY_PASSPHRASE` or a prompt, entered twice on a TTY, and refuses a mismatch. It then re-wraps the existing key with the same public key, so grants don't churn. It writes `<key>.tmp-<32hex>` at 0600, fsyncs, then renames over the key. On any failure the temp file is removed and the original is untouched.
- The key path must not be inside the registry root or the pins directory. The `provider` profile refuses either.
- Sync-folder refusal is shared with H4 (below).
- `_authority_recover` reads the journal and objects through the registry's hardened descriptor readers instead of `open`/`read_bytes`.

Acceptance:
11. A newly created authority key file loads only with its passphrase: `load_pem_private_key(data, password=None)` raises `TypeError`, and its PEM label is the PKCS#8 encrypted-key label.
12. `provider` profile plus an unencrypted key gives a non-zero exit and the rekey hint. After `rekey --encrypt` the same public key loads with the passphrase, and existing grants still verify.
13. A wrong passphrase gives a non-zero exit with no traceback and no key bytes in stderr.
14. A key path under the registry root is refused in `provider`.
15. `_authority_recover` over a symlinked journal is refused (it is followed today).

### H4 Backups and sync-folder refusal (M)

- A new `evidence_inspector/backup_envelope.py` provides `seal_backup(plaintext: bytes, passphrase: str, *, registry_kind: str) -> bytes` and `open_backup(sealed: bytes, passphrase: str) -> tuple[str, bytes]`.
  - Exact bytes: `b"TBXBK1\n"`, then a 4-byte big-endian header length `L`, then `L` bytes of canonical JSON header (sorted keys, no spaces, UTF-8), then the AES-256-GCM ciphertext with its 16-byte tag appended (the `cryptography` `AESGCM` output).
  - Header: `{"kdf":"scrypt","n":131072,"nonce":<b64 12 bytes>,"p":1,"r":8,"registry_kind":<str>,"salt":<b64 16 bytes>,"v":1}`.
  - The associated data is the magic, length and header bytes exactly as written.
  - The plaintext limit is 512 MiB; larger raises before sealing. `L` is at most 4096.
  - Any parse, KDF or tag failure raises `BackupEnvelopeError("backup cannot be opened")`, with no partial output.
  - Passphrase rules are the same as H3. The 12-byte minimum applies only when sealing; opening accepts any non-empty passphrase.
  - `salt` and `nonce` are standard base64 (RFC 4648 §4) with padding.
- New CLI commands:
  - `traceback backup --registry-kind KIND --root DIR --out FILE`. `KIND` is one of `cohort_registry`, `longitudinal_decision_registry`, `repeatability_comparison_registry`, `denominator_policy_registry`, `covariate_context_registry`, `result_view_source_registry`, `measurement_source_artifact_registry`, `anchor_policy_registry`, `projection_policy_registry`, `reader_authorization_registry`, `result_trust_registry` or `longitudinal_comparison_registry`. The fixed table lives in `traceback_runner/backup_cli.py`. It opens the registry at its recorded identity, calls `backup_bytes()`, seals, and writes `FILE` exclusively (`O_CREAT|O_EXCL`, 0600), so an existing file fails.
  - `traceback restore --in FILE --root NEW_DIR`. It opens the backup, reads `registry_kind` from the header, and calls that registry's `restore(NEW_DIR, plaintext, ...)`. `restore` already refuses an existing destination.
  - In `development` only, `--in` may be a plaintext backup when `--plaintext --registry-kind KIND` is given.
- The library `backup_bytes()` methods stay plaintext and unchanged; they are in-process APIs, not exports. The control is that no shipped command writes a plaintext backup in `provider`. A test asserts `traceback backup` has no plaintext flag, and that `restore --plaintext` exits 2 in `provider`.
- `registry_storage.refuse_synced_path(path: Path, *, home: Path, profile: DeploymentProfile) -> str | None`:
  - It resolves with `os.path.realpath`, which follows symlinks, then checks the deepest existing ancestor; a nonexistent leaf is fine.
  - It raises `SyncedPathRefused` in `provider` when the resolved path equals or is under any of `home/"Library/Mobile Documents"`, `home/"Library/CloudStorage"`, `home/"Dropbox"`, `home/"Google Drive"`, or a direct child of `home` whose name starts with `OneDrive` (compared case-insensitively on macOS and case-sensitively on Linux).
  - It also raises when any existing ancestor from the path up to `home` contains an entry named `.dropbox` or `.dropbox.cache`.
  - Paths outside `home` are not checked by markers.
  - Its signature is `-> str | None`. In `provider` it raises `SyncedPathRefused`. In `development` it returns a warning string for the CLI to print, or `None` when the path is clean.
  - Call sites, each a `traceback_runner` function that opens a path:
    1. `reader_cli` registry root;
    2. `reader_cli` key path;
    3. `reader_cli` pins path;
    4. `traceback backup --out` and `traceback restore --root`;
    5. the server's `--catalog-root`/state root at `LocalWebServer` start.
  - Library constructors do not call it.

Acceptance:
16. A sealed-then-opened backup round-trips byte-identical for each of the 12 registries' `backup_bytes()`, and `restore` accepts the result.
17. A wrong passphrase, a tampered header or a tampered ciphertext fails closed with no partial output.
18. `traceback backup` output contains none of the seeded protected tokens; the `protected_tokens` fixture is grepped over the file bytes.
19. In `provider`, a registry or key root under a sync folder (simulated with a temp `$HOME`) is refused. In `development` it is allowed with a warning.

### H5 Operator text classification and validator (M)

- Verified scope: the only free-text operator fields that reach a browser are `AccessibleLabel` values (`result_view.py:107-111`, used by `qc_label` at `:402` and by E06's `accessible_label` caller-asserted field). The other entries in `CALLER_ASSERTED_FIELDS` are policy pins and keys, not free text. D10 tokens are already opaque (`^covariate_[0-9a-f]{32}$`, `covariate_context.py:54-56`) and are out of scope.
- `AccessibleLabel` also rejects control characters and non-NFC input. Serialization is unchanged, so existing digests are stable.
- `static/app.js` already renders every value with `textContent`; there are 0 `innerHTML` uses, so there is no markup injection. Add a module constant `OPERATOR_TEXT_FIELDS = ["accessible_label", "qc_label"]`. When `displayValue` renders an object, it appends ` (operator-entered)` after any value whose key is in that list. No field is dropped.
- Export and release surfaces today: none ship (`release_*` flags are literal false). The rule recorded for future surfaces: an `AccessibleLabel` value is never included in an export or release payload. A test asserts that no `release_*`/`export_*` field is true anywhere in the E14 and D08 public projections.
- `validate_public_text`: add `subject|specimen|collection|run|provider|flowcell` + `id` in the existing identifier regex, the MRN pattern `(?i)\bmrn[\s:#-]*\d{5,}\b`, and the date-of-birth pattern `(?i)\b(?:dob|birth\s*date|date\s*of\s*birth|born)\b\W{0,3}\d{4}-\d{2}-\d{2}`. `validate_public_projection` validates dict keys as well as values.
- `docs/PRIVACY-BOUNDARY.md` (new, short): operator text may contain identifiers, is never exported or released, and the blocklist is defence in depth, not the control.

Acceptance:
20. A control character or non-NFC string in an `AccessibleLabel` raises at model validation. A static-asset test asserts `app.js` contains `OPERATOR_TEXT_FIELDS` and no `innerHTML`, `outerHTML`, `insertAdjacentHTML` or `document.write`.
21. `validate_public_text("subject id 42")`, `"MRN 1234567"` and `"dob 1970-01-01"` each raise. A dict key `"specimen_id"` raises.
22. All existing digest-pinning tests pass unchanged.

### H6 Diagnostics, launch watchdog, error separation (S–M)

- Server diagnostics: `traceback_runner/web/diagnostics.py`, a `collections.deque(maxlen=256)` under a `threading.Lock`.
  - Each entry is `{"t": <int seconds since server start>, "route": <the _ROUTE_KINDS key, or "unmatched">, "status": <int>, "code": <str>}`.
  - Counters is a `dict[str, int]` keyed by code. Both reset only on server restart.
  - `GET /api/v1/diagnostics` (operator only) returns `{"entries": [...], "counters": {...}}`.
  - Recorded: every response sent by `_json`, `_public_json`, `_deny` and the problem path, using route key `_ROUTE_KINDS` key, the regex's name (e.g. `explorer_result`), or `"unmatched"`. Also `handle_error`: `status: 500, code: "TBX-INTERNAL"`, then the bounded 500 problem if headers aren't sent yet.
  - Not recorded: static asset 200s, and client disconnects before a response.
  - It never stores bodies, query strings, paths with IDs, or exception text.
- Watchdog: on watchdog shutdown, the runtime is removed from `_RUNTIMES` and `issue_reader_launch_url` raises `LocalWebServerStopped`. `reader_cli launch` catches only the rate-limit error type (new `ReaderLaunchRateLimited`) for the "wait 60 s" message, and on `LocalWebServerStopped` prints "Server stopped; relaunch with `traceback reader launch`" and exits non-zero.
- D08 `_guarded`/`_authorize`: only `AttributeError`, `TypeError`, `NameError`, `AssertionError`, `KeyError`, `IndexError` and `RecursionError` map to a new boundary code `internal_error`, which the route renders as a 500 with code `TBX-INTERNAL`. Every other exception, including `ValueError` and every store error, keeps its current mapping.

Acceptance:
23. A route whose handler raises `RuntimeError` (test hook) produces one ring entry `{status: 500, code: "TBX-INTERNAL"}`. The response is the bounded 500 problem. The ring entry contains no request path segment after the route key.
24. Tripping the watchdog (test hook), then calling `launch`, gives a non-zero exit and the stopped message; no link is printed.
25. A monkeypatched store raising `AttributeError` inside the D08 build gives `internal_error`, not `integrity_failure`.

### H7 Broker-bound D08 binding (S, after the browser PR)

- `BootstrapBroker` generates `self._binding_key = secrets.token_bytes(32)` at construction; a restart rotates it, and every old binding is then invalid.
- `BootstrapBroker.reader_binding(session_token, *, authority)` keeps its current signature, which already names the session. It returns `BrokerReaderBinding(grant_sha256, registry_head_sha256, session_sha256, mac)`, minted on each call from the `ReaderSessionBinding` already stored on the session record by `bind_reader_session`. Nothing new is stored.
  - `mac = HMAC-SHA256(key, b"traceback.broker-reader-binding.v1\x00" + bytes.fromhex(grant) + bytes.fromhex(head) + session_digest)`.
  - `session_digest` is the broker's stored SHA-256 of the session token.
- `build_longitudinal_workspace(..., reader_binding: BrokerReaderBinding, broker: BootstrapBroker)` calls `broker.verify_reader_binding(binding)`, which recomputes the MAC with `hmac.compare_digest` and requires the session to exist and still carry that binding. It then proceeds as today with `(grant_sha256, registry_head_sha256)`.
- A binding is valid only while its session lives and still carries the same `ReaderSessionBinding`. Logout, expiry or `end_session` invalidates it.
- Migration: `build_longitudinal_workspace`'s current `ReaderGrantBinding` parameter is replaced. Its only callers are `tests/test_longitudinal_workspace.py`, `tests/longitudinal_workspace_world.py` and the browser PR's route handler. Tests use a real `BootstrapBroker` to mint bindings.

Acceptance:
26. A `ReaderGrantBinding` built from journal digests gets `permission_denied`. A broker-minted binding builds.
27. A binding minted by another process's broker gets `permission_denied`.

### X1 External: needs a person, a host or a decision

Owner: the operator (Dan). Cross-account procedure (pass = every step denied):
1. Account A runs `traceback serve` and `traceback reader launch --grant G`.
2. Account B, on the same host:
   - `curl` A's loopback port with A's Host/Origin and no cookie → 401;
   - `ls` and `cat` A's `~/.traceback/*` → permission denied;
   - open A's registry root with `traceback backup` → refused.
3. Known limit, recorded rather than tested away: an unused, unexpired launch link is a bearer secret for up to 60 s. Account B can redeem it if B obtains it, and loopback TCP gives the server no OS-user identity to check. Mitigations:
   - the link is printed only to A's terminal;
   - it is single-use and expires in 60 s;
   - the operator requirement is one OS account per operator and no screen sharing during launch.
   A future Unix-domain-socket transport with peer credentials could close this. It is out of scope here.
4. Account B cannot use A's session cookie: cookies live in A's browser profile, which B cannot read when step 2's file permissions hold. The evidence is the `ls` denial on A's browser profile directory.

Evidence goes in `docs/rollback/` or `docs/PILOT-SECURITY-HARDENING.md` § X1 log, as dated entries naming the host, the OS version, the commands run and pass/fail.

- Pick the pilot host OS. If Windows, P3 becomes a porting project, sized separately: replace `fcntl` in 21 modules.
- Run the cross-account test (`docs/LOCAL-WEB-BOUNDARY.md:116-119`) on that host: two OS accounts, the second cannot reach the first's session or registries.
- Confirm the provider's at-rest policy. If it needs more than OS full-disk encryption plus sealed backups, revisit P2.
- Decide whether shared OS logins are allowed at all. This spec's default: document them as unsupported in `docs/PILOT-SECURITY-HARDENING.md` § Operator requirements, because nothing in code can tell two people apart on one login.

## Operator requirements (pilot)

- One OS account per operator. Shared logins are unsupported.
- OS full-disk encryption on.
- No registry, key or backup under a cloud-sync folder (enforced in `provider`).
- Keep the authority key passphrase out of shell history: prefer the prompt over the environment variable.

## Testing plan

| Layer | What | Count |
|---|---|---|
| Unit | Acceptance criteria 2–4, 11, 13, 16–17, 19–22, 26–27 | one test per criterion, at least |
| Integration | 1, 5, 7–10, 12, 14–15, 18, 23–25: real loopback server, real registries, temp `$HOME` | one test per criterion, at least |
| E2E | 1 and 26 through the real launch flow, after the browser PR | 2 |

Mutation checks, each removing one guard and naming the test that must fail:
- H1: kind check → 1. Launch-digest equality → 2.
- H2: binding comparison → 8.
- H3: unencrypted refusal → 12.
- H4: AAD binding → 17.
- H5: key validation → 21.
- H6: `_RUNTIMES` removal → 24.
- H7: MAC check → 26.

## Rollback

Each H item is its own PR and reverts independently.
- H3 key encryption is forward-only on disk, but `rekey` can re-wrap. Keep a sealed backup of the pre-rekey key during the pilot setup.
- H2's catalog trust-binding field defaults to `caller_store` for old roots, so reverting H2 leaves roots readable.

## Effort

| Item | Estimate |
|---|---|
| H1 | M: broker 0.5 d, route table 0.5 d, tests 0.5 d |
| H2 | M: 1 d |
| H3 | M: 1 d |
| H4 | M: 1 d |
| H5 | M: types 0.5 d, `app.js` rendering 0.5 d, validator 0.25 d |
| H6 | S–M: 0.75 d |
| H7 | S: 0.5 d |

The X1 items are calendar-bound, not effort-bound.

## Out of scope

- Per-reader scoping of the E04/E06–E13 explorer. P5 denies it; a grant model for it is future work.
- In-process tamper resistance: seals and `__getattribute__` guards. Separately, the architecture review recommends removing that layer.
- Whole-store encryption at rest (P2), the Windows port (P3), and hardware or keychain key custody (P1). Each waits on X1.
- Multi-tenant or networked deployment.

## Related

- `docs/E12-INTEGRATION-PLAN.md`; the browser integration branch `epic-e/e12-browser-integration`.
- `docs/READER-AUTHORIZATION-REGISTRY.md`, `docs/RESULT-TRUST-REGISTRY.md` (open decision #3 is closed by H2), `docs/LOCAL-WEB-BOUNDARY.md`.
