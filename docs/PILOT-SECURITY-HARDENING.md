<!-- /autoplan restore point: /Users/danwiggins/.gstack/projects/danwiggins-cfddemo/docs-pilot-security-hardening-autoplan-restore-20261002-113316.md -->
# Pilot security hardening

Status: spec, 2026-10-02. Source: a structural security, privacy and trust review of `main` at `eaac96a`, verified against the code on `d3739ca`.

Update 2026-10-03: two audit facts below are now out of date. CI exists (`.github/workflows/ci.yml`, #91). The browser branch `epic-e/e12-browser-integration` was committed and merged as #93 (`bc237e0`). Still true: no production composition root wires an explorer; `traceback reader launch` starts the service with `explorer=None`. The audit text is kept as written on 2026-10-02.

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

---

# /autoplan review (2026-10-02)

Reviewed on branch `docs/pilot-security-hardening` at `8943d56` (base `main`, `d3739ca`). Pipeline: CEO (SELECTIVE EXPANSION) → Design (skipped: no UI scope) → Eng (FULL_REVIEW) → DX (DX POLISH). Each phase ran two voices: a Claude subagent and Codex (`codex-cli 0.157.1`, read-only). Every intermediate question was auto-decided with the six autoplan principles; each decision is logged in the Decision Audit Trail. The premise gate and the User Challenges are **not** decided. They are listed under "Pending user gates".

UI scope: no. The only matches were "screen" (in "screen sharing") and "form" (in "wrapped form"). DX scope: yes. The plan adds CLI commands (`backup`, `restore`, `authority rekey`), a `--profile` flag, environment variables and operator error messages.

## Headline

**The plan is accurate about the code. It is wrong about the timing.** Every gap in the Current-state table exists in the code; we re-checked the cited lines. But the gap at the top of the table can't be reached in production:

- `traceback reader launch` (`traceback_runner/reader_cli.py:812-816`) starts the web service with `store=` and `reader_registry=` only. It passes no `explorer`.
- With no explorer, every explorer route returns 404 (`server.py:842-844`).
- The one route a reader session can actually reach today is `GET /api/v1/jobs`, and it lists synthetic jobs only.
- The CLI describes itself as "synthetic-only" (`cli.py:1`), and `run` is "process a real input (disabled)" (`cli.py:85`).
- E9 depends on E0–E8 (`docs/EPICS.md:302`), and none of them is done.

So this spec hardens a deployment that doesn't exist yet, on code that the architecture review says will be restructured: there is no composition root, there are 12 copies of the storage engine, and there is a seal layer the threat model excludes. Both outside voices, and the parallel plan-vs-reality review, reach the same conclusion: **build the small, reachable part inside the browser PR. Defer the rest behind named triggers. Put CI first.**

## Phase 1: CEO review (strategy and scope)

### Pre-review system audit

- **Repo age and pace.** First commit 2026-09-26; 477 commits in 7 days (153 on 2026-09-29). No `.github/workflows`, so no CI (as of 2026-10-02; CI landed in #91).
- **The branch diff vs. local `main`** shows D10 files as well, because local `main` is behind `origin`. This branch's only own change is this spec.
- **TODOS.md (16 lines)** already defers "additional operating systems … after the paid provider pilot", which is consistent with P3. It has no security items.
- **The in-flight browser work** was uncommitted at review time in worktree `agent-a2f921bc99d6aee7a` (branch `epic-e/e12-browser-integration`, then at `eaac96a`; merged since as #93). Diff: +323 lines across `server.py`, `reader_session.py`, `explorer.py`, `app.js` and `index.html`, plus new files `web/longitudinal.py` (2185 lines) and `static/longitudinal.js`.
  - It adds 3 GET and 4 POST longitudinal routes.
  - It adds `ReaderSessionBinder.session_credential`, which builds the D08 `ReaderGrantBinding` from **server-side session state**. That already covers the realistic part of H7.
- **Design doc:** none for this branch. The office-hours prerequisite was skipped (P6).
- **Retrospective:** the recent history is a long run of registry and lock hardening fixes (#80–#82: content fences, staged-root cleanup, torn-tail recovery). This area keeps needing rework, and H2, H4 and H7 add more surface to it.
- **Landscape check:** no web search; we used in-distribution knowledge. Layer 1 (standard practice) for a single-host local tool is: rely on OS accounts plus full-disk encryption, keep secrets in the OS keychain, and fail closed on the network surface. Layer 3 (first principles): the boundary that matters most here is loopback TCP, which carries no OS-user identity. A Unix-domain socket with peer credentials would close that whole class of problem; the spec dismisses it.

### Step 0A: Premise challenge

| # | Premise (as the plan states or assumes it) | Verdict | Evidence |
|---|---|---|---|
| PR1 | The E9 setting (a paid pilot on a provider workstation with donor data) is the near-term deployment this spec protects. | **Challenge.** | E9 depends on E0–E8. E0 is not approved, E4 has no POD5/Dorado code, `run` is disabled, and there is no donor-data path. |
| PR2 | The three assumptions break in that setting: the person at the keyboard isn't the authorized person; `$HOME` is synced; every local account can reach the loopback port. | **Accept.** | True of real provider workstations. `LOCAL-WEB-BOUNDARY.md:116-119` already lists the cross-account test as "explicitly unmet". |
| PR3 | The current-state gaps are real exits **today**. | **Challenge, partly.** | They are real in the code, but the explorer gap can't be reached in production (no explorer is wired). The plaintext key (H3), `status` reading a fixed trust file (H2), the dead launch link (H6), the bootstrap DoS and the launch-binding window (H1) can be reached, but only on synthetic data. |
| PR4 | "Close the gaps that code can close **now**" is the right timing. | **Challenge.** | There is no CI to hold 27 acceptance criteria. The storage engine is due for consolidation, and H2's metadata row and H4's twelve-registry backup table are written against it. The plan-vs-reality review warns against exactly this kind of pass. |
| PR5 | The process/OS-user boundary stays the trust boundary, and in-process mutation stays out of scope. | **Accept**, which removes H7. | `READER-AUTHORIZATION-REGISTRY.md:14-15` and `LONGITUDINAL-WORKSPACE.md:15-16` say so. H7 defends against an in-process caller forging a binding, and the browser branch already sources the binding from the session. |
| PR6 | P3: Windows is unsupported, and the provider profile refuses non-POSIX hosts. | **Challenge the timing.** | `LOCAL-WEB-BOUNDARY.md:118` names "the approved Linux host", but no provider has confirmed its OS. The spec itself says many MinKNOW hosts run Windows, and 21 modules import `fcntl`. That is one email to providers; send it before any POSIX-specific custody work. |
| PR7 | P1: a passphrase-encrypted PKCS#8 key is the right custody default. | **Accept as a fallback.** | No new dependency is the right instinct. But an environment-variable passphrase is weak; prompt-first is right. Keychain versus passphrase should be decided once the host OS is known (see PR6). |
| PR8 | P5: deny reader sessions on explorer and job routes. | **Accept.** | Small, safe, and it fixes the one reachable leak (`/api/v1/jobs`). |

Premises PR1, PR3, PR4 and PR6 go to the premise gate, which is not auto-decided.

### Step 0B: Existing code leverage

| Sub-problem | Existing code | Plan reuses it? |
|---|---|---|
| Session kinds and reader binding | `BootstrapBroker.bind_reader_session` and `reader_binding` (`auth.py:302-326`); `ReaderSessionBinding` (`auth.py:123`) | Yes (H1, H7) |
| Reader-route gating and the D08 credential | Browser branch `ReaderSessionBinder.session_credential` and `reader_authorization_in_held_fence` | Not referenced. H7 rebuilds it. |
| Live trust for `status` | `ResultTrustRegistry` pinned open (`cli.py:790`, the `verify` path with id/epoch/head) | Partly. H2 cites the wrong flag shape. |
| Backups | 12 `backup_bytes()` methods plus per-registry `restore(...)` | Yes, but `restore` signatures differ per kind (Eng finding E6). |
| Public-text gate | `validate_public_text` / `validate_public_projection` (`contracts.py:55-107`) | Yes (H5) |
| Safe rendering | `app.js` uses `textContent` only; no `innerHTML` (verified: 0 matches) | Yes |
| Error mapping | D08 `_guarded` (`longitudinal_workspace.py:361-421`); the browser branch adds its own top-level mapping | H6 edits only D08 and misses the browser layer. |
| Watchdog | `server.py:1281-1290` | Yes (H6) |

### Step 0C: Dream state

```
  CURRENT STATE                      THIS PLAN (as written)                 12-MONTH IDEAL
  synthetic-only CLI; no CI;    ---> +profile system, +backup CLI      ---> one qualified host (OS known);
  reader launch = jobs only;         +crypto envelope, +catalog            real modBAM -> signed record;
  E12 stores unwired;                 migration, +HMAC binding,             one composition root, one
  12 storage-engine copies;           +diagnostics ring, 27 criteria        storage engine; deny-by-default
  explorer gap latent                 on code that will be restructured     routes; OS keychain custody;
                                                                            cross-account test passed on the
                                                                            real host; CI holds every control
```

As written, the plan moves **sideways**. It adds surface that the consolidation will have to carry or rewrite. The re-sequenced version (below) moves toward the ideal: the route policy lands with the composition root, the key-custody choice follows the host, and CI exists before any of it.

### Step 0C-bis: Implementation alternatives

```
APPROACH A: Build as written (H1-H7, X1)
  Summary: 7 PRs, ~5.5-6 days of human-equivalent work, sequenced around the browser PR.
  Effort:  L        Risk: High
  Pros:    complete paper coverage of every finding; one coherent spec
  Cons:    hardens unreachable and synthetic-only paths; no CI to hold it; H2/H4 written
           against a storage engine due for consolidation; 4 factual errors (below)
  Reuses:  broker, registries, public gate

APPROACH B: Re-sequence. Reachable core now, triggers for the rest   <- RECOMMENDED
  Summary: CI first. Then fold the H1 core (session kind, exact deny-by-default route table
           covering GET+POST+HEAD+job regex, launch-to-bootstrap binding, no burn on a wrong code)
           into the browser PR. Then a small H6 (launch message, dispatch-boundary 500 + record).
           Then H2-min (status says "unknown" in provider without live trust). Defer H3/H4/H5-type/X1
           procedure behind named triggers. Cut H7.
  Effort:  S-M (~1.5 d human, ~1-2 h CC)    Risk: Low
  Pros:    every control built now is reachable and tested; lands where the routes are born;
           nothing written against code due for consolidation
  Cons:    the spec stops being a single "pilot-ready security" claim; deferred items need
           owners and triggers
  Reuses:  browser-branch session_credential, broker, existing verify-path trust pins

APPROACH C: Structural transport change (Unix socket + peer credentials)
  Summary: replace loopback TCP with an AF_UNIX socket in a 0700 directory and check
           SO_PEERCRED/getpeereid, so the OS-user identity check happens at accept().
  Effort:  M        Risk: Med (browser access needs a small local proxy or native shell; not portable to Windows)
  Pros:    closes the cross-account and bearer-link class structurally (X1 step 3)
  Cons:    browsers can't talk to a Unix socket directly; conflicts with P3 if the host is Windows
  Reuses:  boundary checks stay; transport changes
```

**Recommendation: B.** It is the most complete option for code that can actually be reached (P1 applied to the real blast radius), and the cleanest (P5). A and B are not close: A builds roughly 4 days of work against code that will be restructured. C is a real alternative for later. It is logged as a deferred item with a trigger (the host OS is chosen and is POSIX).

Changing the user's chosen structure (build H1–H7 autonomously) needs a User Challenge (UC1–UC4). This is not a taste call, so it is not auto-decided. The rest of this review evaluates the plan **as written**, and marks per item what Approach B would keep.

### Step 0D: Mode analysis (SELECTIVE EXPANSION)

**Complexity check.** The plan touches more than 20 files: `server.py`, `auth.py`, `reader_session.py`, `contracts.py`, `app.js`, `cli.py`, `reader_cli.py`, `result_catalog.py`, `result_view.py`, `longitudinal_workspace.py`, the repeatability modules, `registry_storage.py`, the 12 registries (H4), `product_gates.py`, and new `deployment_profile.py`, `backup_envelope.py`, `backup_cli.py`, `diagnostics.py` and `PRIVACY-BOUNDARY.md`. It adds 6 or more new types: `DeploymentProfile`, `ResultTrustReader`, `BrokerReaderBinding`, `BackupEnvelopeError`, `SyncedPathRefused`, `ReaderLaunchRateLimited` and `LocalWebServerStopped`. Both are over the smell thresholds (8 files, 2 classes).

**Minimum set that achieves the stated goal.** The goal is that a reader link gives a reader only what the grant covers, that identifiers don't leave in plaintext, and that we never claim "verified" from a stale file. That needs:
1. H1's session kind plus the route table, built with the routes.
2. H2's `status` tri-state.

Everything else can wait without blocking the goal, because no deployment exists to exploit it.

**Expansion scan.** These are candidates, not added to scope.
- **10x check:** a single composition root that refuses to start in `provider` unless the route table, key custody and sync checks all pass. That gives one startup gate instead of controls scattered across modules. It becomes the E9 "go/no-go" check.
- **Delight opportunities, each about 30 minutes:**
  1. `traceback doctor --profile provider` prints each operator requirement as pass or fail.
  2. Launch prints the link's expiry time.
  3. Logout from the page.
  4. `status` prints which trust source it used.
  5. A refused synced path names the sync provider it detected.
- **Platform potential:** the route-kind table is the natural place for future per-reader scoping.

The cherry-pick ceremony was auto-decided. The doctor gate is deferred to TODOS with the trigger "a composition root exists" (P3: outside the blast radius now). Delight items 2 and 4 are accepted into the H1 and H2 scope (P2: in blast radius, under 1 day, same files). Items 1, 3 and 5 are deferred or ride with their parent item.

### Step 0E: Temporal interrogation

```
HOUR 1 (foundations): Where does the profile come from inside the server process? (reader_cli forces
                      PROVIDER, but LocalWebServer is library code that "never reads env".) Resolve: pass
                      profile into RunningLocalWebService.start explicitly.
HOUR 2-3 (core):      Single-slot broker + reader link: an operator bootstrap issued after a reader link
                      replaces it (by design). Does the reader page show a clear error? Unspecified.
                      Idle refresh vs logout race (Eng E3).
HOUR 4-5 (integrate): The browser PR's 7 longitudinal routes include 4 POSTs; H1's table lists GET only.
                      H6 must catch at dispatch, not in handle_error (which only gets the socket).
HOUR 6+ (tests):      There is no CI, so the 27 criteria and 7 mutation checks are not enforced after merge.
```
(At CC pace these are about 10 minutes each. The decisions are the same.)

### Step 0F: Mode

The autoplan override gives SELECTIVE EXPANSION. The implementation approach under that mode is B (it needs a User Challenge to change the user's structure).

### Dual voices (CEO)

**CLAUDE SUBAGENT (CEO: strategic independence).** Verdict: "don't build this as written; about 1.5 of the 8 items fix something a pilot can hit today."
- F1 (critical): the headline gap is unreachable; only `/api/v1/jobs` leaks.
- F2 (critical): the spec assumes a pilot it can't reach.
- F3 (high): this is the anti-pattern the reality review warned against; CI comes first, then storage consolidation, before crypto and backup surfaces.
- F4 (high): ask providers about their host OS now.
- F5 (medium): Unix socket and keychain alternatives were dismissed.
- F6 (medium): factual slips (`--trust-registry` needs id/epoch/head; there is no `traceback serve`).
- F7 (medium): collision with in-flight work.
- Item calls:
  - H1: partly now, inside the browser PR.
  - H2: defer.
  - H3: defer, except the key-path rule.
  - H4: cut for now.
  - H5: validator half now.
  - H6: smallest version now.
  - H7: cut.
  - X1: ask the OS and policy questions now; defer the procedure.

**CODEX SAYS (CEO: strategy challenge).** "This plan optimizes the security of an imagined product." The 10x reframe: prove one supervised single-user workflow (E0, real modBAM, E3) before building a general authorization and registry platform. Item calls:
- H1: build now, narrowly, as a browser-PR merge gate.
- H2: build now, drastically narrowed (provider `status` = unknown).
- H3: defer until delegated reader links are needed.
- H4: cut.
- H5: cut as designed ("compliance theater"; `AccessibleLabel` also types `error_message`).
- H6: defer, except the launch message.
- H7: cut.
- X1: defer until E0 is approved and the host is named, then require it before donor data.

Codex also noted that the browser branch's store container is "an injectable composition object, not a production composition root": `_launch` still doesn't pass it.

```
CEO DUAL VOICES — CONSENSUS TABLE:
═══════════════════════════════════════════════════════════════════════
  Dimension                            Claude   Codex   Consensus
  ──────────────────────────────────── ──────── ─────── ─────────────
  1. Premises valid?                   No       No      CONFIRMED (PR1/PR3/PR4 wrong)
  2. Right problem to solve now?       No       No      CONFIRMED (real-data path + CI first)
  3. Scope calibration correct?        No       No      CONFIRMED (overbuilt ~4x)
  4. Alternatives sufficiently explored? No     No      CONFIRMED (UDS, keychain, delete seals)
  5. Competitive/market risks covered? No       No      CONFIRMED (opportunity cost)
  6. 6-month trajectory sound?         No       No      CONFIRMED (rewritten after consolidation)
═══════════════════════════════════════════════════════════════════════
  Per-item: H1 now-in-browser-PR (both) | H3 defer (both) | H4 cut (both) | H7 cut (both)
            H2: Claude defer / Codex narrow-now  -> DISAGREE (taste T1)
            H5: Claude validator-now / Codex cut  -> DISAGREE (taste T2)
            H6: both launch-msg now; Claude adds handle_error record -> partial (taste T3)
            X1: both ask OS/policy now-ish; procedure deferred -> CONFIRMED
```
6/6 confirmed. Both voices agree that the user's direction should change, so those items become User Challenges.

### Section 1: Architecture

```
                     traceback (cli.py)             traceback reader (reader_cli.py)
                      │  --profile (NEW)              │ forced=PROVIDER (NEW)
                      ▼                               ▼
              deployment_profile.py (NEW) ──────► resolve_profile()
                      │ profile kwarg threaded into library calls (NEW coupling)
   ┌──────────────────┼──────────────────────────┬─────────────────────────┐
   ▼                  ▼                          ▼                         ▼
 status (H2)    ResultCatalog (H2)        backup_cli.py (H4 NEW)    RunningLocalWebService
 ResultTrust    trust_binding row         └► backup_envelope.py      ├─ BootstrapBroker (H1: kind,
 Reader (H2 NEW) (schema change)             (TBXBK1, scrypt,        │   last_seen, logout, launch
                                              AES-GCM) NEW           │   digest; H7 HMAC key)
 registry_storage.refuse_synced_path (H4 NEW) ◄── reader_cli, backup  ├─ _ROUTE_KINDS (H1 NEW)
                                                                     ├─ diagnostics ring (H6 NEW)
                                                                     └─ [browser PR] longitudinal.py
                                                                          └► D08 builder (H7 sig change)
```

Coupling introduced:
- `DeploymentProfile` becomes a keyword on library constructors in `evidence_inspector`, which today has no notion of deployment. That is acceptable because it is explicit with a safe default (P5). But it is a 4th profile concept alongside `ReaderAuthorizationProfile`, the synthetic flag and the `development_file` trust.
- `diagnostics.py` couples to the route table key set.
- H7 couples D08 (`evidence_inspector`) to `BootstrapBroker` (`traceback_runner.web`). That is a layering inversion: the library would import or accept a web-layer type.

**Findings, auto-decided:**
- A1. The H7 layering inversion plus a threat the model excludes → cut H7 (User Challenge UC2, since both voices agree).
- A2. The route table must enumerate GET, POST and HEAD, plus the job-detail regex → fix in the plan (P1).
- A3. The profile should reach the server explicitly, as a parameter of `RunningLocalWebService.start` → fix (P5).
- A4. **Rollback.** Each H item is its own PR. H2's schema change is not cleanly revertable (Eng E5).
- **Scaling:** not material; this is a single-user loopback server with at most 32 sessions.
- **Single points of failure:** the single bootstrap slot, by design.

### Section 2: Error and rescue map

```
  METHOD/CODEPATH                    | WHAT CAN GO WRONG                     | EXCEPTION / CODE
  -----------------------------------|---------------------------------------|---------------------------
  resolve_profile                    | unknown value; forced conflict        | SystemExit(2) "unknown profile"
  BootstrapBroker.exchange           | wrong code; rate limit; expired       | BoundaryDenied 401 TBX-AUTH-001 / 429 -005
  require_session (+idle)            | idle > 1200 s; expired; logout race   | BoundaryDenied 401 TBX-AUTH-001
  route kind check                   | reader on operator route; unlisted    | BoundaryDenied 403 TBX-AUTH-007
  exchange_launch_credential         | digest mismatch; operator session     | ReaderAuthorizationDenied LAUNCH_CREDENTIAL_INVALID
  reader_authorization (revoked)     | grant revoked mid-session             | 403 TBX-READER-DENIED + end_session
  status trust (H2)                  | registry unreadable; pins missing     | "not_verified"/registry_error (never raises)
  ResultCatalog reopen (H2)          | binding changed; 2nd metadata row     | ResultCatalogUnsafe / CatalogUnsupportedSchema  <- GAP
  load key (H3)                      | wrong passphrase; unencrypted in prov | ValueError/TypeError from cryptography -> exit 2
  rekey (H3)                         | disk full mid-write; mismatch         | OSError -> temp removed
  seal/open_backup (H4)              | tag fail; bad header; huge n          | BackupEnvelopeError        <- GAP: n/r/p not pinned
  restore (H4)                       | per-kind dependencies missing         | TypeError (signature)      <- GAP: not implementable
  refuse_synced_path (H4)            | realpath on unreadable ancestor       | OSError                    <- GAP: unspecified
  dispatch exception (H6)            | handler raises RuntimeError           | must be caught at dispatch <- GAP: plan puts it in handle_error
  D08 _guarded (H6)                  | ValueError programming bug            | stays integrity_failure    <- GAP: allow-list too narrow
  launch loop (H6)                   | server stopped                        | LocalWebServerStopped -> exit !=0

  EXCEPTION / CODE             | RESCUED? | RESCUE ACTION                     | USER SEES
  -----------------------------|----------|-----------------------------------|--------------------------
  TBX-AUTH-001/005/007         | Y        | bounded problem                   | 401/403/429 page state
  LAUNCH_CREDENTIAL_INVALID    | Y        | deny binding                      | "link invalid" (page)
  CatalogUnsupportedSchema     | N <- GAP | —                                 | catalog unusable after H2
  BackupEnvelopeError          | Y        | no partial output                 | "backup cannot be opened"
  restore TypeError            | N <- GAP | —                                 | traceback; cut H4 instead
  dispatch RuntimeError        | N <- GAP | —                                 | dropped connection (today)
  cryptography ValueError      | Y (plan) | exit 2, no key bytes              | "wrong passphrase"
```
Gaps were auto-decided as follows:
- The catalog schema gap and the `restore` gap are resolved by deferring the H2 catalog half and H4 (taste T1; UC3).
- The dispatch gap is fixed by moving the catch to the handler dispatch boundary (P5).
- The `_guarded` allow-list is inverted: only named domain exceptions keep their mapping, and everything else becomes `internal_error` (P1).

### Section 3: Security and threat model

| Threat | Likelihood | Impact | Plan mitigates? |
|---|---|---|---|
| Reader session reads explorer artifacts | Low today (no explorer is wired); High once wired | High | Yes, through the H1 table. **This must land with the composition root.** |
| Reader session lists jobs | Med (reachable) | Low (synthetic) | Yes (H1) |
| Any B01 session redeems a pending launch credential | Low (needs a 256-bit credential) | Med | Yes (H1 digest binding) |
| Local process burns the operator link (DoS) | Med | Low | Yes (H1) |
| Key copied by backup or sync | Med on a real host | High (signs grants) | H3 and H4 refusal. Deferred until there is a host. |
| Plaintext identifiers in exported backups | None today (no command writes backups) | High | H4. Cut until a runbook exists. |
| Identifier-shaped operator text reaches the page | Low | Med | H5 validator. **New risk:** validating keys rejects the public key `source_id` (`portable_view.py:218`). |
| Passphrase in the environment leaks via `/proc` or shell history | Med | High | Partly. The plan prefers the prompt. **Gap:** the env var should be refused in `provider` unless an explicit non-interactive flag is given (deferred with H3). |
| Malicious backup header sets a huge scrypt `n` (CPU DoS) | Low | Low | **Gap:** pin n, r and p exactly on open. Moot if H4 is cut. |
| Diagnostics endpoint leaks request metadata | Low | Low | The plan stores route keys only. OK. |
| In-process forging of a D08 binding | Out of model | — | H7. **Cut**, because it defends an excluded threat. |
| Cross-account access via a bearer link within 60 s | Low | Med | X1 records it. A Unix socket would close it (Approach C, deferred). |

Audit logging: grant revocation and logout have no durable audit record. The diagnostics ring is in memory only. This was accepted for the pilot stage (P6). It is recorded as a TODO alongside E9 "redacted diagnostics".

### Section 4: Data flow and interaction edge cases

```
  LAUNCH LINK ──▶ PAGE PARSES FRAGMENT ──▶ POST bootstrap ──▶ POST reader-launch ──▶ BOUND READER SESSION
     │                 │                        │                   │                      │
  [expired 60s]   [fragment missing]      [wrong code: no burn]  [digest != session]   [grant revoked → end_session]
  [replaced by    [reader_launch absent]  [rate-limit: burn]     [operator session]    [idle 20 min → 401]
   newer link]                            [32 sessions → 403]    [credential reused]   [logout → 401]
```

| Interaction | Edge case | Handled? | How |
|---|---|---|---|
| Open reader link | A newer link was issued first (single slot) | **Partly** | Old bootstrap returns 401. The page message is unspecified. **Fix:** the page says "link replaced; press Enter in the terminal for a new one" (P1, accepted). |
| Open reader link | Double-click or two tabs | Yes | Second exchange returns 401 (one use) |
| Reader page left open | Idle for 20 minutes | Yes | 401. The page must show a "session ended" state (accepted into H1). |
| Save/Reopen (browser PR) | Grant revoked between save and reopen | Yes | 403, then 401 (criterion 5) |
| `backup` | `--out` already exists | Yes | `O_EXCL` fails |
| `rekey` | Ctrl-C mid-write | Yes | Temp file removed; original untouched |
| `launch` | Server watchdog tripped | Yes | H6 |

### Section 5: Code quality

- **DRY.** Passphrase rules are stated twice (H3 and H4) → make them one helper (accepted).
- **DRY.** `refuse_synced_path` sits next to the existing ownership checks in `registry_storage` (fine).
- **Over-engineering.**
  - H7's HMAC over a value that never leaves the process.
  - H6's diagnostics ring and route, when E9 already lists "redacted diagnostics" as its own deliverable.
  - H4's bespoke envelope, when OS full-disk encryption plus an encrypted OS backup covers the pilot.
- **Under-engineering.**
  - The H1 table omits POST, HEAD and the job-detail route.
  - The H6 `_guarded` allow-list is the wrong polarity.
- **Naming.** `kind` on the session record is clear. `BrokerReaderBinding` vs `ReaderSessionBinding` vs `ReaderGrantBinding` makes three near-identical names; that is one more reason to cut H7.
- **Cyclomatic complexity.** `resolve_profile` (flag, env, default, forced conflict, unknown) has 5 branches, which is fine.

### Section 6: Tests

The plan's own test table maps every criterion to a layer, which is good. Gaps (detail in Phase 3):
- No CI, so none of this is enforced after merge.
- Criterion 1 can't be tested through the production launcher.
- Criterion 22 doesn't catch the H5 key regression.
- Race tests are missing: logout vs. request, revocation vs. request.
- There is no test that every route in the browser PR appears in `_ROUTE_KINDS`.
- The test hostile QA would write: issue a reader link, then immediately issue an operator bootstrap, and check that the reader page fails clearly rather than silently.
- Flakiness: the idle-timeout tests must use the injected `now`, never a real clock.

### Section 7: Performance

No material issues.
- Per request, the route table lookup is O(1) and the deque append takes a lock; that is fine for 32 sessions.
- scrypt with n=2^17 and r=8 uses about 128 MiB of RAM and roughly 0.5–1 s per seal or open. That is acceptable for a CLI, and it argues for pinning the parameters (a large `n` is a DoS).
- The 512 MiB plaintext cap means seal holds the plaintext plus the ciphertext in memory (about 1 GiB). This is noted as a TODO with H4.

### Section 8: Observability

- Today `handle_error` and `log_message` are silent (`server.py:641, 666`), and the plan correctly flags that.
- The H6 ring is in memory and resets on restart, so it can't reconstruct an incident three weeks later.
- For a pilot, the useful minimum is the one Approach B keeps: catch at dispatch, return `TBX-INTERNAL`, and keep a bounded counter.
- A durable, redacted diagnostics log is E9's deliverable. It is deferred with the trigger "E9 diagnostics work starts".
- **Runbook gap:** X1 has an evidence log but no "link stopped working" runbook. That is fixed by the H6 message.

### Section 9: Deployment and rollout

- There is no deployment pipeline. The "deployment" is the operator's checkout. Rollout risk is merge-order risk.
- The browser PR, then H1, then H6 all rebase on `server.py`. Approach B removes two of those rebases.
- H3 is forward-only on disk. The plan's rollback ("keep a sealed backup of the pre-rekey key") depends on H4, which is cut. **Fix:** `rekey` writes `<key>.pre-rekey` at 0600 and the operator deletes it after verifying (deferred with H3).
- Post-deploy verification: none is possible without a host (X1).

### Section 10: Long-term trajectory

- **Reversibility: 3 of 5.** The H2 catalog row and H3 key encryption are one-way on disk. Everything else reverts by PR.
- **Debt introduced:** a 4th profile concept; a crypto file format to support forever (`TBXBK1`); and 12-registry restore dispatch on top of a storage engine with 12 copies.
- **The 1-year question.** A new engineer will find hardening for a deployment shape that may not match the eventual host. That is acceptable only if each control traces to a trigger.
- **Platform potential.** The route-kind table is the right seed for per-reader scoping. Keep it.

### Section 11: Design and UX

Skipped: no UI scope detected. The reader page states (link replaced, session ended) are covered under Section 4.

### NOT in scope (CEO)

| Item | Rationale |
|---|---|
| Per-reader scoping of the explorer | No grant model; P5 denies it. |
| Unix-domain socket transport (Approach C) | Trigger: the host OS is known and is POSIX, and cross-account risk is judged material. |
| OS keychain or HSM custody | Trigger: the host OS is chosen. Decide then between keychain and passphrase. |
| Whole-store encryption | Provider policy (X1) first. |
| Windows port | Trigger: the provider host is Windows. |
| Storage-engine consolidation | Owned by the architecture review. It should precede H2-catalog and H4. |
| Durable diagnostics log | E9 "redacted diagnostics" deliverable. |

### What already exists (CEO)

See Step 0B. The key reuse the plan misses is the browser branch's `session_credential`, which makes H7 redundant.

### Dream state delta

As written, the plan leaves us with more controls than the ideal needs, and still without CI, a real-data path or a known host. Re-sequenced (Approach B), we end with a deny-by-default route table born with the composition root, an honest `status`, and named triggers for the rest. That is where the 12-month ideal wants us.

### Error and rescue registry (CEO)

This is the Section 2 table. Of 16 codepaths, 5 have gaps: catalog schema, `restore` signature, synced-path `OSError`, dispatch catch, and the `_guarded` polarity. Four of the five go away under Approach B. The fifth (the dispatch catch) is fixed in H6-min.

### Failure modes registry (CEO)

```
  CODEPATH              | FAILURE MODE                      | RESCUED? | TEST?        | USER SEES?            | LOGGED?
  ----------------------|-----------------------------------|----------|--------------|-----------------------|--------
  handler dispatch      | unexpected exception              | N        | N (planned   | dropped connection    | N   <- CRITICAL GAP (today)
                        |                                   |          |  wrong layer)|  (silent)             |
  launch loop           | server stopped, link printed      | N        | N            | dead link (silent)    | N   <- CRITICAL GAP (today)
  route table           | browser POST route not listed     | Y (deny) | N            | 403 on Save           | N
  H5 key validation     | rejects source_id in portable view| N        | N            | explorer 500/denied   | N
  H2 catalog reopen     | 2nd metadata row                  | N        | N            | catalog unsupported   | N
  reader page           | link replaced by newer bootstrap  | Y (401)  | N            | unspecified message   | N
```
There are 2 critical gaps, and both exist **today**. Both are fixed by H6-min, which Approach B builds now.

### CEO completion summary

```
  +====================================================================+
  |            MEGA PLAN REVIEW — COMPLETION SUMMARY                   |
  +====================================================================+
  | Mode selected        | SELECTIVE EXPANSION (autoplan override)     |
  | System Audit         | 7-day repo, no CI, browser work uncommitted;|
  |                      | (both as of 2026-10-02; #91, #93 since)     |
  |                      | headline gap unreachable in production      |
  | Step 0               | Approach B recommended (needs UC1-UC4)      |
  | Section 1  (Arch)    | 4 issues found                              |
  | Section 2  (Errors)  | 16 error paths mapped, 5 GAPS               |
  | Section 3  (Security)| 12 threats, 2 High-impact deferred w/ host  |
  | Section 4  (Data/UX) | 7 edge cases mapped, 1 unhandled (fixed)    |
  | Section 5  (Quality) | 6 issues found                              |
  | Section 6  (Tests)   | Diagram in Phase 3, 6 gaps                  |
  | Section 7  (Perf)    | 0 issues (scrypt params noted)              |
  | Section 8  (Observ)  | 2 gaps found                                |
  | Section 9  (Deploy)  | 3 risks flagged                             |
  | Section 10 (Future)  | Reversibility: 3/5, debt items: 3           |
  | Section 11 (Design)  | SKIPPED (no UI scope)                       |
  +--------------------------------------------------------------------+
  | NOT in scope         | written (7 items)                           |
  | What already exists  | written                                     |
  | Dream state delta    | written                                     |
  | Error/rescue registry| 16 methods, 5 GAPS (4 removed by B)         |
  | Failure modes        | 6 total, 2 CRITICAL GAPS (both pre-existing)|
  | TODOS.md updates     | 6 items proposed (see Phase 3)              |
  | Scope proposals      | 6 proposed, 2 accepted, 4 deferred          |
  | CEO plan             | written (~/.gstack ceo-plans)               |
  | Outside voice        | ran (codex + claude)                        |
  | Lake Score           | 9/11 recommendations chose complete option  |
  | Diagrams produced    | 5 (arch, data flow, error, dream, launch)   |
  | Stale diagrams found | 1 (plan's H1-H7 graph shows H7 dependency)  |
  | Unresolved decisions | premise gate + 4 User Challenges (pending)  |
  +====================================================================+
```

**Phase 1 complete.** Codex raised 8 item-level concerns. The Claude subagent raised 7 findings (2 critical). Consensus: 6/6 confirmed; 3 per-item disagreements go to the gate as taste decisions. The premise gate is pending (not auto-decided). Passing to Phase 2.

## Phase 2: Design review

Skipped, no UI scope (see Phase 0).

## Phase 3: Eng review (FULL_REVIEW)

### Step 0: Scope challenge, checked against the code

Every claim below was checked in the code. The plan's Current-state table is **accurate**: the cited lines match `d3739ca`. The **proposed changes** contain factual errors that would stop a builder:

| # | Plan says | Code says | Effect |
|---|---|---|---|
| S1 | `status` gains `--trust-registry PATH`, "the flag `cli.py` already parses" | The flag exists only on `verify`. It requires `--trust-registry-id/epoch/head`, and `_require_trust_registry_identity` returns early unless the command is `verify` (`cli.py:1244-1262`). `status` has no JSON `verified` field; it passes a boolean into `build_job_view` (`cli.py:644`). | H2's `status` half has to be re-specified. |
| S2 | `ResultTrustReader` exposes `snapshot()`, `trust_store()`, `authority_read_fence()` and `registry_identity()`; type checks use `isinstance` | The real API is `read_fence()`, `current_trust()`, `current_trust_store()` and `revoke_key()` (`result_trust_registry.py:1132-1191`). Consumers check `type(x) is not ResultTrustRegistry` (`result_catalog.py:1160`; `repeatability_comparison_registry.py:1063, 2223`). Identity is read through `object.__getattribute__(...)["_metadata"]` (`result_catalog.py:603`). | The H2 wrapper is not M-sized. |
| S3 | The catalog adds a `trust_binding` metadata row | Validation requires `metadata_count == 1` (`result_catalog.py:1911-1916`), and metadata is a content table, so the row would change `content_sha256` and every E04 dependency head. | Every catalog becomes `CatalogUnsupportedSchema`, and saved comparisons go stale. |
| S4 | A restore into a new root changes the epoch | "The restored registry keeps its identity" (`result_trust_registry.py:1222`). | The H2 rebuild reasoning is wrong. |
| S5 | `traceback restore --in FILE --root DIR` calls `restore(NEW_DIR, plaintext, ...)` | Every `restore`/open needs identity pins plus live dependencies: a linkage store and trust for cohort/D07, `dependency_fence` for D11, `configured_trust` plus `time_source` for the reader registry. | H4 can't be built as written. |
| S6 | X1: "Account A runs `traceback serve`" | There is no `serve` subcommand (`cli.py:65-131`). Only `reader launch` and `product_gates` start the server. | Fix the procedure. |
| S7 | Acceptance 5 expects `TBX-READER-DENIED` | That string appears nowhere in the repo (0 matches). The reader denial is `permission_denied`. | Fix the criterion. |
| S8 | H3 refuses a key path inside the pins directory | Keys and `authority.json` share `~/.traceback/reader-authority` by design (`reader_cli.py:69`). | Every default install would be refused. |
| S9 | `traceback --profile provider reader ...` | `main` dispatches on `raw[:1] == ["reader"]` (`cli.py:1267`). | The flag-first form breaks `reader`. |
| S10 | H1 route table: longitudinal routes are GET, reader | The browser branch has 3 GET (`selectors`, `diff`, `saved`) and 4 POST (`workspace`, `source`, `save`, `reopen`) routes. `GET /api/v1/jobs/job_<32hex>` and `HEAD` (which delegates to GET) are also live. | A fail-closed table would break job detail and every reader Save/Reopen. |
| S11 | H5 key validation is safe; digests are unchanged | `PortableSourceIdentity.source_id` (`portable_view.py:220`) reaches `validate_public_projection` through the explorer, and `source`+`id` is already in the regex. | Every portable artifact would be rejected. Criterion 22 (digests) wouldn't catch it. |
| S12 | H1 makes reader sessions get 403 on `/api/v1/jobs` | After binding, `app.js:207` calls `await renderJobs()` inside the outer try. The 403 then shows "Local session unavailable; relaunch Traceback" (`app.js:214`). | **Regression:** every successful reader launch would look broken. |
| S13 | H1 rate-limit trip still clears the slot | That keeps the DoS H1 claims to close: 8 garbage POSTs still burn the link (`auth.py:252-256`). | With 256-bit codes, a trip should return 429 and **keep** the slot. |

**Minimum change set.** Approach B (Phase 1): H1-core inside the browser PR, H6-min, H2-status-min. Everything else is deferred or cut, pending UC1–UC4.

**Complexity check.** Over 20 files and 7 new types trip the smell thresholds. The scope reduction is a User Challenge (both voices agree), not an auto-decision. P2 in this phase says never reduce, so the eng review proceeds on the full plan and marks which findings Approach B removes.

**Search check.** No web search; we used in-distribution knowledge.
- [Layer 1] Python `cryptography`'s `BestAvailableEncryption` and `AESGCM` are the standard primitives.
- [Layer 1] `hashlib.scrypt` is standard too.
- [Layer 3] A bespoke backup container is a format we would maintain forever. age, or an OS-encrypted archive, is the boring choice. That is one more reason to defer H4.

**TODOS cross-reference.** TODOS.md has no security items. The new deferred items are listed below as TODO proposals.

**Distribution.** No new artifact type; nothing to flag.

### Dual voices (Eng)

**CLAUDE SUBAGENT (eng: independent review), 20 findings.**
- Critical:
  - (1) the H2 metadata row breaks every catalog and E04 heads (S3);
  - (2) H5 key validation breaks the explorer (S11);
  - (3) the H1 table omits live routes (S10);
  - (4) H2 names an API that doesn't exist (S2);
  - (5) H4 restore isn't buildable (S5).
- High:
  - (6) restore keeps identity (S4);
  - (7) the `status` fields and flags don't exist (S1);
  - (8) a rate-limit trip still burns the slot (S13);
  - (9) `--profile` placement breaks reader dispatch (S9);
  - (10) forcing provider breaks the existing reader tests, and the H3 key/pins-directory rule refuses default installs (S8);
  - (11) the profile defaults to the permissive DEVELOPMENT in library code ("third downstream guard"; bind the profile at the producer);
  - (12) revocation-ends-session is under-specified: no "expired/superseded" reasons exist, `end_session` doesn't exist, and the browser save path swallows the reason;
  - (13) wrong denial code (S7).
- Medium:
  - (14) H6 error separation is masked by the browser branch's catch-all, `do_GET`'s `TypeError`→400, and D08 `:2531`;
  - (15) `LocalWebServerError` already subclasses `RuntimeError`;
  - (16) the `(operator-entered)` suffix has no hook in `JSON.stringify`, and NFC rejection could fail to load legacy records;
  - (17) the two-secret launch is redundant once bound: bind the grant at bootstrap exchange;
  - (18) the explorer gap is unreachable today;
  - (19) the backup passphrase needs its own variable;
  - (20) factual errors: `serve`, the `--catalog-root` flag, `PROFILE` type, `rekey` vs. write-once `_write_new`, and unauthenticated 401s can flush the diagnostics ring.

**CODEX SAYS (eng: architecture challenge), 8 findings.**
- P0, 5 findings:
  - (1) the H1 table is incomplete (S10);
  - (2) criterion 1 is false for the real reader composition;
  - (4) H2 `status` can't use the registry as specified (S1);
  - (5) the H2 catalog migration and rollback claims break the one-row metadata invariant (S3);
  - (6) H4 generic restore isn't implementable, and scrypt parameters must be pinned (S5).
- P1, 3 findings:
  - (3) kind-check and idle-refresh need one atomic broker operation (a logout/revocation race);
  - (7) H5 key validation breaks `source_id` (S11);
  - (8) `handle_error` only gets the socket; catch at dispatch, and invert the `_guarded` allow-list.

Verdict: "Cut H7. Defer H3/H4 until the real host and custody path exist. CI and the H1 browser-PR changes are the only immediate critical path."

```
ENG DUAL VOICES — CONSENSUS TABLE:
═══════════════════════════════════════════════════════════════════════
  Dimension                            Claude   Codex   Consensus
  ──────────────────────────────────── ──────── ─────── ─────────────
  1. Architecture sound?               No       No      CONFIRMED (H2/H4/H7 unsound; H1 core sound)
  2. Test coverage sufficient?         No       No      CONFIRMED (crit 1, 5, 22, 23, 25 wrong/untestable)
  3. Performance risks addressed?      Yes      Yes*    CONFIRMED (*pin scrypt params)
  4. Security threats covered?         Partly   Partly  CONFIRMED (DoS fix incomplete; race; H7 out-of-model)
  5. Error paths handled?              No       No      CONFIRMED (dispatch catch; _guarded polarity)
  6. Deployment risk manageable?       No       No      CONFIRMED (H2 one-way schema; reader tests break)
═══════════════════════════════════════════════════════════════════════
  No DISAGREE rows. Claude-only critical: S12 app.js regression (verified at app.js:207) -> flagged.
```

### Section 1: Architecture

```
  BROWSER PR (in flight)                          H1 CORE (fold in)                 LATER (triggers)
  ┌──────────────────────────┐   routes born   ┌───────────────────────────┐
  │ web/longitudinal.py      │ ───────────────►│ _ROUTE_KINDS generated     │
  │  GET selectors/diff/saved│                 │  from GET_/POST_ROUTE_PATHS│
  │  POST workspace/source/  │                 │  + job regex + HEAD→GET    │
  │       save/reopen        │                 │  missing ⇒ deny (all kinds)│
  │ reader_session.          │                 └─────────────┬─────────────┘
  │  session_credential() ───┼──► D08 builder                │ kind check
  └──────────────────────────┘    (binding from              ▼
                                   server state;   ┌──────────────────────────────┐
                                   H7 redundant)   │ BootstrapBroker               │
                                                   │  issue_bootstrap(kind, digest)│
                                                   │  authorize_session(kind, csrf,│
                                                   │   idle) ONE lock  (NEW)       │
                                                   │  end_session/logout (NEW)     │
                                                   └──────────────────────────────┘
  app.js: skip renderJobs/catalog for reader sessions (NEW, regression fix)

  H2-status-min: status --trust-registry{,-id,-epoch,-head} (parser change) → current_trust_store()
  H6-min: catch at _Handler dispatch → TBX-INTERNAL + counter; launch: LocalWebServerStopped ≠ RateLimited
  DEFERRED: H2-catalog (schema v+1), H3 (custody), H4 (backup), H5-type (OperatorText), X1-procedure
  CUT: H7
```

**Findings, auto-decided (P5 and P3 dominate):**
- E1. Generate `_ROUTE_KINDS` from the route constants, by method, and assert that every handler branch is listed (S10). Fix in plan.
- E2. One broker operation, `authorize_session(token, *, kind, csrf, now)`, validates expiry, idle, authority, kind and CSRF, then refreshes `last_seen_at` under one lock. `end_session` deletes the record, and a refresh never re-inserts one. Fix in plan (codex 3, Claude 12).
- E3. Bind the profile at the producer: `RunningLocalWebService.start(profile=...)` takes it explicitly with no default, and a registry or catalog records its profile at creation. Library defaults stay DEVELOPMENT only for the pure functions (Claude 11). This is a taste call (**T4**): the plan's default keeps every test unchanged, while a required parameter is safer but churns callers. Recommended: required on the server, and recorded at the root later together with H2-catalog.
- E4. Cut H7: in-process threat, a layering inversion, and the browser branch's `session_credential` already sources the binding from the session. This is User Challenge **UC2**.
- E5. Drop the second exchange: if the bootstrap carries the credential digest, bind at exchange (Claude 17). Taste (**T5**). Recommended: keep the two-step flow for now. The browser PR's page already does it, and changing the page flow mid-PR is churn (P3). The digest binding closes the hole.

**Production failure scenario, one per integration point:**
- The route table misses a new route, so the reader gets 403 on Save. Mitigated by the generated table plus the "every route listed" test.
- The broker race resurrects a logged-out session. Mitigated by E2 and a barrier test.
- A watchdog trip leaves `launch` printing dead links. Mitigated by H6-min.

### Section 2: Code quality

- **DRY.** Passphrase rules are duplicated between H3 and H4 → one `read_passphrase(env_var, *, confirm, min_bytes)` helper. Backups get their own `TRACEBACK_BACKUP_PASSPHRASE` (Claude 19, codex DX 6). Deferred with H3/H4.
- **DRY.** H5 should reuse the browser branch's `validate_longitudinal_public` key grammar (`_KEY` plus `_PROTECTED_KEYS`) instead of running the value regex over keys (S11). Fix in plan (P4).
- **Error polarity.** H6's `_guarded` builtin allow-list is backwards. Fix: named domain exceptions keep their mapping, and *anything else* becomes `internal_error`. The browser branch's `handle_longitudinal_route` catch-all needs the same treatment (P1). This also gets rid of the `except Exception` smell at `longitudinal_workspace.py:412` and `:2062`.
- **Naming.** Rename `rekey --encrypt` to `authority set-passphrase` (DX).
- **Over-engineering.** The H6 diagnostics ring, route and deque: replace them with a per-code counter on the existing problem path. Unauthenticated 401s can flush a 256-entry ring (Claude 20). Under H6-min the ring is cut; this is taste **T3**.
- **Stale diagram.** The plan's H1–H7 graph shows H7 depending on all six. Update it after the gate.

### Section 3: Test review

Framework: `RUNTIME:python`, pytest (`uv run pytest`). There is no CI, so **nothing here is enforced after merge**. CI is task T0.

```
CODE PATHS                                                USER FLOWS
[+] web/auth.py BootstrapBroker                           [+] Reader launch (operator terminal → browser)
  ├── issue_bootstrap(kind, digest)                         ├── [GAP] [→E2E] link → bootstrap → reader-launch → longitudinal page
  │   ├── [GAP] reader kind stores digest                   │        renders (NOT "Local session unavailable")  ← REGRESSION S12
  │   └── [GAP] new issue replaces pending (single slot)    ├── [GAP] link replaced by newer link → clear page message
  ├── exchange()                                            ├── [GAP] idle 20 min → "session ended" page state
  │   ├── [★★ TESTED today] valid code                       └── [GAP] logout → 401 on next request
  │   ├── [GAP] wrong code keeps slot (crit 3)            [+] Operator flows
  │   └── [GAP] rate-limit trip: 429, slot KEPT (S13)       ├── [★★ TESTED] jobs list / explorer (tests/web)
  ├── authorize_session(kind,csrf,idle)  ONE lock           └── [GAP] [→E2E] operator session unaffected by reader routes
  │   ├── [GAP] idle boundary 1200 s exact / 1201 s         [+] CLI
  │   ├── [GAP] denied request does not refresh             ├── [GAP] launch after watchdog → nonzero + "server stopped"
  │   └── [GAP] logout-vs-request barrier race              ├── [GAP] launch rate-limit → "wait 60 s" only for that type
  └── end_session / logout                                  └── [GAP] status (provider, no trust) → "unknown" + hint
      ├── [GAP] repeat logout → 401
      └── [GAP] refresh never resurrects deleted record
[+] web/server.py _ROUTE_KINDS
  ├── [GAP] every handled path×method listed (generated; meta-test)
  ├── [GAP] reader → 403 TBX-AUTH-007 on jobs, job detail, catalog, compare, result
  ├── [GAP] unlisted path → denied for every kind
  └── [GAP] HEAD follows GET policy
[+] web/reader_session.py
  ├── [GAP] operator session redeeming credential → LAUNCH_CREDENTIAL_INVALID (crit 2)
  ├── [GAP] reader session B redeems only B's credential
  └── [GAP] revoked grant → 403 permission_denied (NOT TBX-READER-DENIED) then 401 (crit 5 fixed)
        └── [GAP] SCOPE_MISMATCH / REGISTRY_UNAVAILABLE do NOT end session
[+] web/server.py dispatch (H6-min)
  ├── [GAP] handler RuntimeError → 500 TBX-INTERNAL, counter +1, no path in record
  └── [GAP] headers already sent → connection closed, counter +1
[+] cli.py status (H2-min)
  ├── [GAP] registry revocation flips verified→not_verified with no file edit (crit 7)
  ├── [GAP] registry unreadable → not_verified + registry_error, never raises
  └── [GAP] provider without trust → unknown + trust_source none
[+] contracts.py validator (H5-validator, if taken)
  ├── [GAP] "subject id 42", "MRN 1234567", "dob 1970-01-01" raise
  └── [GAP] REGRESSION GUARD: every explorer projection incl. portable source_id still validates
[DEFERRED/CUT] H2-catalog, H3, H4, H5-type/app.js tagging, H7 — tests ride with them (criteria 8-15, 16-19, 20, 26-27)

COVERAGE (Approach B scope): 1/36 paths tested today (3%) | GAPS: 35 (4 E2E)
QUALITY: ★★★:0 ★★:1 ★:0
```

**Regressions, the IRON RULE (added as critical requirements, not optional):**
- **R1 (S12):** a reader launch must render the reader view, not "Local session unavailable". E2E through the real page, plus a DOM-harness unit test.
- **R2 (S11):** if the H5 validator lands, every existing explorer projection, including `PortableLocalView.source_id`, must still validate.
- **R3 (S10):** every browser-PR route (GET and POST) must keep working for the right kind after H1.
- **R4 (S9):** `traceback reader ...` dispatch still works, and `traceback --profile X reader` either works or exits 2 with a clear message.

**Criteria to rewrite:**

| Criterion | Problem | Fix |
|---|---|---|
| 1 | Production launch wires no explorer, so "200 on all four" is false | Test against the composition the browser PR wires (it supplies an explorer), and test reader 403 on jobs and job detail. |
| 5 | `TBX-READER-DENIED` doesn't exist | Use `permission_denied`, 403. |
| 22 | Digests unchanged, which doesn't catch a validator rejection | Add R2. |
| 23 | `handle_error` can't send a bounded 500 | Catch at the dispatch boundary. |
| 25 | `AttributeError` mapping is masked by the browser catch-all and D08 `:2531` | Specify which layer emits `TBX-INTERNAL`, and test through the route. |

**Flakiness.** Idle and expiry tests must use the broker's injected `now`. Race tests should use `threading.Barrier`, never `sleep`.

**Hostile-QA test.** Issue a reader link, then an operator bootstrap, then open the reader link. Expected: 401 and a clear "link replaced" page state.

**Chaos test.** Kill the server between bootstrap exchange and reader-launch. Expected: the page shows the stopped state, and `launch` exits non-zero.

**Test plan artifact:** `~/.gstack/projects/danwiggins-cfddemo/danwiggins-docs-pilot-security-hardening-eng-review-test-plan-20261002-*.md`.

### Section 4: Performance

- The route lookup is a dict hit.
- The broker lock is held slightly longer for the combined check, which is negligible at 32 sessions.
- scrypt (deferred with H4): about 128 MiB and about 1 s. Pin n, r and p on open so a crafted header can't demand 2^30 (codex 6).
- The 512 MiB backup cap means about 1 GiB peak memory. Stream it if H4 is ever built.
- No N+1 or caching concerns.

### NOT in scope (Eng)

| Item | Rationale |
|---|---|
| H2 catalog `trust_binding` | Needs a schema-version bump and migration plus a decision about digests. Trigger: storage consolidation lands, or a provider catalog is opened by production code. |
| H2 `ResultTrustReader` | Has to be rewritten against the real API and the `type() is` checks. Same trigger. |
| H3 key custody | Trigger: the pilot host OS is chosen (keychain vs. passphrase), before the first real reader grant. |
| H4 backup/restore | Trigger: an E9 backup or retention runbook exists, after consolidation. Use OS full-disk encryption plus OS backup until then. |
| H5 `OperatorText` type and `app.js` tagging | Trigger: the first operator-entered field reaches a real reader. |
| H6 diagnostics ring and route | Trigger: E9 "redacted diagnostics" work starts. |
| H7 | Cut (out of the threat model). |
| A single-exchange launch (bind at bootstrap) | Taste T5; revisit after the browser PR. |

### What already exists (Eng)

- **Broker binding:** `bind_reader_session` / `reader_binding` (`auth.py:302-326`).
- **Server-side D08 credential** (browser branch): `ReaderSessionBinder.session_credential`.
- **Public key grammar** (browser branch): `validate_longitudinal_public`.
- **Pinned trust opening:** the `verify` path (`cli.py:790`, plus identity flags).
- **Route constants** (browser branch): `GET_ROUTE_PATHS` / `POST_ROUTE_PATHS`.
- **Exit-code table:** OPERATOR-GUIDE (usage 2, blocked 3, verification 5).

### Failure modes registry (Eng)

| Codepath | Realistic failure | Test? | Error handling? | User sees | Critical gap? |
|---|---|---|---|---|---|
| Reader page after H1 | `renderJobs` 403 | Planned (R1) | No | "relaunch Traceback" (misleading) | **Yes, until R1 lands** |
| Route table | Browser route missing | Planned (meta-test) | Deny | 403 on Save | No |
| Broker refresh/logout | Race resurrects a session | Planned (barrier) | Single lock | Session survives logout | No (once E2 lands) |
| Handler dispatch | Unexpected exception | Planned | Dispatch catch | Today: dropped connection, silent | **Yes (pre-existing)** |
| `launch` loop | Watchdog stopped the server | Planned (24) | New exception type | Today: dead link, silent | **Yes (pre-existing)** |
| `status` (provider) | No trust source | Planned | `unknown` | "unknown" + hint | No |
| Validator keys | `source_id` rejected | Planned (R2) | No | Explorer error | No (once R2 lands) |

There are 3 critical gaps. All 3 are closed by tasks T1, T2 and T3.

### Worktree parallelization

| Step | Modules touched | Depends on |
|---|---|---|
| T0 CI | `.github/` | — |
| T1 H1-core + R1 | `traceback_runner/web/` (`auth`, `server`, `reader_session`, `static`) | Browser PR (same lane; fold in) |
| T2 H6-min | `traceback_runner/web/server`, `traceback_runner/reader_cli` | T1 (shared `server.py`) |
| T3 H2-status-min | `traceback_runner/cli`, `traceback_runner/operator` | — |
| T4 H5-validator (if T2-taste accepted) | `traceback_runner/web/contracts` | — |

- Lane A: browser PR → T1 → T2 (sequential; shared `web/`).
- Lane B: T0.
- Lane C: T3, then T4.

Launch A, B and C in parallel. Lanes A and C both touch `traceback_runner/` but different modules. T4 (`contracts.py`) is used by `web/`, so rebase it after A merges.

### Implementation tasks (Eng)

- [ ] **T0 (P1, human: ~2h / CC: ~15min) CI.** Add a GitHub Actions workflow running `uv sync && uv run pytest` and the integrity checks on PRs.
  - Surfaced by: the plan-vs-reality review, and both CEO voices (F3).
  - Files: `.github/workflows/ci.yml`
  - Verify: the workflow is green on a PR.
- [ ] **T1 (P1, human: ~1d / CC: ~45min) H1-core in the browser PR.**
  - Scope:
    - session `kind` plus launch digest;
    - one-lock `authorize_session`;
    - a generated `_ROUTE_KINDS` (GET, POST, HEAD, job regex);
    - a wrong code doesn't burn the slot, and a rate-limit trip returns 429 and keeps the slot;
    - logout, idle timeout and `end_session` on revocation reasons `GRANT_REVOKED` and `GRANT_NOT_CURRENT` only;
    - `app.js` skips jobs and catalog for reader sessions (R1).
  - Surfaced by: Eng S10, S12, S13, E1, E2; codex 1-3.
  - Files: `traceback_runner/web/auth.py`, `server.py`, `reader_session.py`, `static/app.js`, `tests/web/*`
  - Verify: `uv run pytest tests/web`, plus E2E reader launch.
- [ ] **T2 (P1, human: ~3h / CC: ~20min) H6-min.**
  - Scope:
    - `LocalWebServerStopped` vs. `ReaderLaunchRateLimited` in `launch`;
    - a catch at `_Handler` dispatch returning 500 `TBX-INTERNAL` plus a per-code counter;
    - an inverted `_guarded` polarity in D08 and in the browser catch-all.
  - Surfaced by: failure-modes critical gaps; codex 8; Claude 14-15.
  - Files: `traceback_runner/web/server.py`, `traceback_runner/reader_cli.py`, `evidence_inspector/longitudinal_workspace.py`, `traceback_runner/web/longitudinal.py`
  - Verify: criteria 23-25 as rewritten.
- [ ] **T3 (P2, human: ~3h / CC: ~20min) H2-status-min.**
  - Scope:
    - `status` gains `--trust-registry` with `-id/-epoch/-head`;
    - a new `trust_state` tri-state and a `trust_source` field (`signature_verified` is kept);
    - in provider with no trust source, `unknown` plus a hint.
  - Surfaced by: Eng S1; codex CEO H2.
  - Files: `traceback_runner/cli.py`, `traceback_runner/operator.py`, `tests/test_cli*.py`
  - Verify: criterion 7.
- [ ] **T4 (P3, human: ~2h / CC: ~15min) H5 validator half.**
  - Scope:
    - the MRN, DOB and identifier patterns on values;
    - a key grammar reused from `validate_longitudinal_public`, not the value regex;
    - the R2 guard.
  - Taste T2.
  - Files: `traceback_runner/web/contracts.py`, `tests/web/test_contracts*.py`
  - Verify: criterion 21, plus R2.

### Eng completion summary

- Step 0, scope challenge: the scope reduction is recommended (Approach B). It is pending User Challenges UC1–UC4 and was not auto-applied.
- Architecture review: 5 issues (E1–E5).
- Code quality review: 5 issues.
- Test review: diagram produced; 35 gaps; 4 regressions (R1–R4); 5 criteria rewritten.
- Performance review: 1 issue (pin the scrypt parameters; deferred with H4).
- NOT in scope: written (8 items).
- What already exists: written.
- TODOS.md updates: 6 items proposed (below). Not written to TODOS.md, because this commit stages only the plan file.
- Failure modes: 3 critical gaps flagged (2 pre-existing).
- Outside voice: ran (codex and claude).
- Parallelization: 3 lanes, 2 parallel and 1 sequential.
- Lake Score: 10/12 recommendations chose the complete option. The two that didn't, T5 and the cut of the ring, chose explicit over complete.

**Phase 3 complete.** Codex raised 8 concerns (5 P0). The Claude subagent raised 20 issues (5 critical). Consensus: 6/6 confirmed, 0 disagreements. Passing to Phase 3.5.

## Phase 3.5: DX review (DX POLISH)

**Product type:** a CLI tool (operator CLI), plus a local web reader. **Mode:** DX POLISH (the autoplan override).

### Developer persona (auto-decided, P6)

```
TARGET DEVELOPER PERSONA
========================
Who:       Provider lab operator on one workstation (MinKNOW host), set up by the solo founder-engineer
Context:   First pilot setup, then daily "issue a reader link" and occasional backup/restore
Tolerance: ~10 minutes for setup with a written runbook; zero tolerance for a link that "looks broken"
Expects:   One copy-paste setup section, prompts not env-var archaeology, errors that name the fix
```

### Empathy narrative (traced against the current docs)

> I open README.md. It explains the Streamlit demo and `uv run streamlit run app.py`. Nothing mentions `traceback reader`. OPERATOR-GUIDE.md has stable exit codes and preflight guidance, but no reader section. I find `READER-AUTHORIZATION-REGISTRY.md` and piece together `traceback reader authority init`, then `grant issue --cohort … --measurement F:Q:U`. I don't know where a cohort ID comes from. After this plan, `init` asks for a passphrase twice and `grant issue` asks again. I run `traceback reader launch --grant SEL` and open the link, and with H1 as written the page says "Local session unavailable; relaunch Traceback", because the page still asks for the jobs list and gets a 403. I think I did something wrong. If I had `TRACEBACK_PROFILE=development` exported from earlier testing, every `traceback reader` command exits 2 before I get that far.

### Competitive DX benchmark (reference benchmarks; no web search)

| Tool | Comparable task | Notable DX choice |
|---|---|---|
| `ssh-keygen -p` | Add or change a key passphrase | One verb, prompt twice, never changes the public key |
| `age` / `restic` | Encrypted backup | Repository- or recipient-oriented; no internal type names; the passphrase is prompted or read from a file |
| `op run` (1Password) | Non-interactive secrets | The secret never touches shell history |
| **Traceback (this plan)** | Reader link in provider | 4 commands, 2–3 passphrase prompts, about 10 min if the cohort ID is known; unbounded if it isn't |

Target (auto-decided, P5): **Competitive (2–5 min) for an operator following a written runbook.** "Champion" isn't meaningful for a provider-installed tool.

### Magical moment

The operator presses Enter and gets a link. The reader opens it and sees *only* their grant's comparison. Delivery vehicle: a copy-paste runbook section in OPERATOR-GUIDE (option B, the lowest effort, P5). **Today the plan breaks this moment** (S12, R1).

### Developer journey map

| Stage | Developer does | Friction | Status |
|---|---|---|---|
| 1. Discover | Reads README | No pointer to the operator CLI | Fixed in plan: one README line → OPERATOR-GUIDE |
| 2. Install | `uv sync` | none | ok |
| 3. Hello world | `authority init` → `grant issue` → `launch` → open link | No runbook; unknown cohort ID; page shows "relaunch" after H1 | Fixed: runbook section + R1 |
| 4. Real usage | New link per reader; Save/Reopen | Route table would 403 Save | Fixed: generated table (T1) |
| 5. Debug | Link dead after watchdog | "Too many unused links" lie | Fixed: T2 |
| 6. Upgrade | Existing unencrypted key, existing catalogs | Provider refusal fires only where the key is loaded (not in `launch`); there is no way to back up the key; the key/pins-directory rule refuses default installs | Deferred with H3; requirements recorded below |

### First-time developer confusion report

```
T+0:00  README → Streamlit only. Where is the operator CLI?            (fix: README pointer)
T+1:00  READER-AUTHORIZATION-REGISTRY → which cohort ID? which F:Q:U?   (fix: runbook with sample values)
T+3:00  `traceback --profile provider reader launch` → argparse error   (fix: R4; drop --profile from reader)
T+4:00  `traceback reader launch` → exit 2 (TRACEBACK_PROFILE=development exported)  (fix: message names the env var)
T+6:00  Link opens → "Local session unavailable; relaunch Traceback"    (fix: R1)
T+8:00  Press Enter again after watchdog → "Too many unused links"      (fix: T2)
```
Auto-decision: address all of them (P1).

### Dual voices (DX)

**CLAUDE SUBAGENT (DX: independent review).**
- Critical:
  - `--profile` before `reader` breaks dispatch (S9);
  - **after H1, every reader launch shows "Local session unavailable"** (S12, verified at `app.js:207/214`).
- High:
  - `TRACEBACK_PROFILE=development` makes `reader` exit 2 with no explanation;
  - `rekey --encrypt` is misleading next to `authority rotate` → `authority set-passphrase`;
  - refusals lack messages and exit codes; map them to OPERATOR-GUIDE codes 2/3/5;
  - there is no OPERATOR-GUIDE reader section;
  - `serve` and `--catalog-root` don't exist;
  - the backup passphrase variable isn't named → `TRACEBACK_BACKUP_PASSPHRASE`, with double entry when sealing;
  - the unencrypted-key check never runs in `launch`/`show` → check the PEM label in every reader command;
  - no command can back up the key;
  - there is no runbook for re-pinning the reader registry after a restore.
- Medium:
  - `launch --root` is relative to the current directory;
  - the backup/restore flags clash (`--root`, `--out`/`--in` vs. `--output`);
  - `--plaintext` is redundant with the magic header;
  - 12 invocations per full backup;
  - the idle-expiry message is missing.
- Outside DX: iCloud "Desktop & Documents" sync isn't caught by the sync-folder list.

**CODEX SAYS (DX: developer experience challenge).**
- P0:
  1. no reliable working product path;
  2. `--profile` breaks `reader`;
  3. the key-location rule rejects the default install.
- P1:
  4. TTHW isn't measurable; add one tested quickstart with fewer than 6 commands and one prompt;
  5. `status` is misleading (exit 0 on registry I/O failure; keep `signature_verified`);
  6. the backup UX exposes internal kind names; infer the kind; add `backup inspect`;
  7. errors lack cause and remedy; define a CLI error matrix;
  8. there is no safe upgrade story; `migrate check/apply`.

```
DX DUAL VOICES — CONSENSUS TABLE:
═══════════════════════════════════════════════════════════════════════
  Dimension                            Claude   Codex   Consensus
  ──────────────────────────────────── ──────── ─────── ─────────────
  1. Getting started < 5 min?          No       No      CONFIRMED (no runbook; S12 breaks it)
  2. API/CLI naming guessable?         No       No      CONFIRMED (--profile placement; rekey; kinds)
  3. Error messages actionable?        No       No      CONFIRMED (need problem+cause+fix+exit code)
  4. Docs findable & complete?         No       No      CONFIRMED (no reader section; serve)
  5. Upgrade path safe?                No       No      CONFIRMED (key migration; no key backup)
  6. Dev environment friction-free?    Partly   Partly  CONFIRMED (env-var profile trap)
═══════════════════════════════════════════════════════════════════════
  Disagreement: status exit code on registry I/O failure — plan: 0 always; Codex: nonzero
  operational exit; Claude: silent. -> taste T6 (recommend keep 0 + distinct trust_source, P5:
  status reports state, scripts read the field).
```

### Passes 1–8

| Pass | Score (as written → with fixes) | Evidence and fix (auto-decided) |
|---|---|---|
| 1. Getting started | 3 → 7 | No runbook; S12 breaks the first link. **Fix:** an OPERATOR-GUIDE "Provider pilot setup" section with exact commands, sample cohort/measurement values, expected output and cleanup (rides with T1). R1. |
| 2. CLI design | 4 → 7 | `--profile` placement (S9); `rekey --encrypt`; `--root` overload; `--out` vs. `--output`. **Fix:** `--profile` only on non-reader commands; `reader` rejects `--profile` with a clear message; rename to `authority set-passphrase` (with H3); `--registry-root`/`--output`/`--input` (with H4). |
| 3. Errors | 3 → 7 | Refusals say "non-zero" or give no text. **Fix:** every new refusal gets problem, cause, fix and an exit code from the OPERATOR-GUIDE table (2 usage, 3 blocked, 5 verification). The unknown-profile message names its source (flag or env). Wrong passphrase: "could not decrypt operator key: wrong passphrase or damaged key file (passphrase from TRACEBACK_READER_KEY_PASSPHRASE)". Server stopped gives a reason. The reader page has "link replaced" and "session ended (idle 20 min)" states. |
| 4. Docs | 3 → 7 | Update OPERATOR-GUIDE (runbook, profile, environment variables, exit codes), LOCAL-WEB-BOUNDARY (session kinds, 403, idle, logout) and READER-AUTHORIZATION-REGISTRY (key encryption, when H3 lands). README gets one pointer. Fix `serve` and `--catalog-root`. |
| 5. Upgrade | 2 → 6 (deferred with H3) | Requirements recorded for H3: check the PEM label in every reader command; exempt `set-passphrase`; `set-passphrase` keeps `<key>.pre-rekey` at 0600 until confirmed; a layout migration for the key/pins rule, or drop the rule (S8). |
| 6. Environment | 5 → 7 | `TRACEBACK_PROFILE` trap (fixed message); `launch --root` relative to the current directory (default to `~/.traceback/runner` in provider, which rides with T1); document `op run` / direnv for non-interactive passphrases. |
| 7. Community | n/a → n/a | A single-provider pilot tool on a public repo; there is no community surface to invest in. Nothing flagged after checking README and docs. |
| 8. Measurement | 2 → 4 | No TTHW instrumentation. Accepted for now: X1's dated evidence log doubles as the setup-time record (log setup minutes there). A durable metric waits for E9 "operator metrics". |

**Overall DX: 3/10 as written → 6.5/10 with the in-scope fixes.** TTHW is about 10 minutes (unbounded without a cohort ID) as written, with a target of 3–5 minutes using the runbook.

### DX implementation checklist (rides with T1–T3 unless noted)

- [ ] OPERATOR-GUIDE "Provider pilot setup" section: copy-paste, sample values, expected output.
- [ ] README: one-line pointer to the operator CLI.
- [ ] `traceback reader` rejects `--profile`. `TRACEBACK_PROFILE=development` with `reader` exits 2 with "traceback reader always runs in the provider profile; unset TRACEBACK_PROFILE (currently 'development')".
- [ ] Reader page states: link replaced, session ended (idle), session revoked, server stopped.
- [ ] `launch`: a distinct message for a stopped server vs. the rate limit; print the link's expiry.
- [ ] Every new refusal maps to exit 2, 3 or 5 and says problem, cause and fix.
- [ ] (Deferred with H3) `authority set-passphrase`; PEM-label check in every command; `.pre-rekey` copy; key layout.
- [ ] (Deferred with H4) `--registry-root/--output/--input`; `TRACEBACK_BACKUP_PASSPHRASE`; double-entry seal; infer the kind from the magic; `backup --all`.
- [ ] (Deferred with H4) Add `~/Desktop` and `~/Documents` to the iCloud check, or document the gap.

### NOT in scope (DX)

- `traceback migrate check/apply` (codex): no on-disk migration ships in Approach B. Trigger: H2-catalog or H3 lands.
- `backup inspect` and `backup --all`: ride with H4.
- TTHW telemetry: E9 operator metrics.

### What already exists (DX)

- The OPERATOR-GUIDE stable exit-code table (lines 101–114).
- The `support-bundle --output` convention.
- The `reader launch` link and its 60-second message (`reader_cli.py:818-824`).
- The bounded problem shape `TBX-AUTH-*`.

**Phase 3.5 complete.** DX overall is 3/10 as written, 6.5/10 with fixes. TTHW is about 10 minutes now, with a 3–5 minute target. Codex raised 8 concerns. The Claude subagent raised 18 issues. Consensus: 6/6 confirmed, with 1 disagreement that goes to the gate as a taste decision. Passing to Phase 4.

## Cross-phase themes

- **Theme: the plan hardens code that production doesn't reach.** Flagged in CEO (both voices), Eng (S6, Claude 18, codex 2) and DX (codex 1). This is a high-confidence signal: the explorer gap, H4 and H7 all assume a composition root and a deployment that don't exist yet.
- **Theme: H1 has to be born with the browser routes.** Flagged in CEO, Eng (S10) and DX (S12). The route table, the reader page states and R1 all live in the same files as the in-flight browser work.
- **Theme: factual errors would stop a builder.** Flagged in CEO (F6), Eng (S1–S13) and DX: `serve`, the `status` flags, `TBX-READER-DENIED`, the trust-registry API and the key/pins directory.
- **Theme: decide host facts first.** Flagged in CEO (PR6) and DX (upgrade). The OS and the at-rest policy decide H3, H4, P3 and Approach C.

## TODO proposals (auto-decided "A) add"; not written to TODOS.md because this commit stages only the plan file)

1. **Storage consolidation before H2-catalog and H4.** Owner: the architecture-review follow-up. Priority P2. Depends on: CI.
2. **H3 key custody: keychain or passphrase.** Trigger: the pilot host OS is known, before the first real reader grant. The DX requirements are in Phase 3.5. P2.
3. **H4 backup and restore with per-kind pins.** Trigger: an E9 retention runbook exists. P3.
4. **H2-catalog `trust_binding`, schema v+1.** Trigger: production code opens a provider catalog. P3.
5. **Unix-domain socket transport (Approach C).** Trigger: a POSIX host is chosen and cross-account risk is judged material. P3.
6. **`traceback doctor --profile provider` go/no-go gate.** Trigger: a composition root exists. P3.

## Gate decisions (resolved 2026-10-02 by the operator)

| Gate | Decision |
|---|---|
| Premises | Accepted the review's view: E9 is **not** near-term enough to drive build order. CI and the golden path come first; security work follows reachability. Windows support waits until providers answer on host OS (X1). |
| Challenge 1: sequencing | **Re-sequence.** Now: CI → H1 core → H6-min → H2-status-min → H5 validator. Everything else is deferred behind the named triggers below. |
| Challenges 2–4: cuts | **Accepted.** H7 cut. H4 deferred until an E9 retention runbook exists, after storage consolidation. H3 deferred until the host OS is chosen. |
| H1 placement | **A separate PR after the browser PR lands**, not folded into it. |

| X1: host | **Resolved 2026-10-02.** For now the host is a team member's own macOS workstation (the operator's or a co-founder's), not a provider PC. Windows (P3) stays unsupported. The cross-account test is not applicable on single-user machines. H3's trigger moves from "host OS chosen" to **"first non-team user or first real donor data"**; when it fires, use macOS Keychain as the key backend. P2 relies on FileVault. |

The original gate text follows for the record.

## Pending user gates (now resolved, see above)

These are **not decided**. The plan as written stands until Dan answers.

### Gate 1: Premise confirmation

| # | Premise | Recommendation |
|---|---|---|
| PR1 | E9 (a provider workstation with donor data) is the near-term deployment. | **Challenge.** E0–E8 aren't done, and there's no real-data path. |
| PR2 | The three assumptions break on a real provider host. | **Accept.** |
| PR3 | The current-state gaps are exits today. | **Challenge.** Only `/api/v1/jobs`, the bootstrap DoS, the launch window, the dead link and the fixed-file `status` are reachable, and only on synthetic data. The explorer gap is latent. |
| PR4 | Build now is the right timing. | **Challenge.** No CI, and the storage engine is due for consolidation. |
| PR5 | The OS-user boundary stays the trust boundary. | **Accept.** That implies cutting H7. |
| PR6 | P3: Windows is unsupported. | **Challenge the timing.** Ask the providers which OS they run first. |
| PR7 | P1: passphrase-encrypted key. | **Accept as a fallback.** Decide once the host is known. |
| PR8 | P5: deny readers on explorer and job routes. | **Accept.** |

### Gate 2: User Challenges (both models agree your direction should change)

**Challenge 1: re-sequence instead of building H1–H7 autonomously** (CEO, Eng, DX)
You said: turn the finding into a spec, autoplan it, then build it autonomously.
Both models recommend:
- CI first (T0).
- Then build only T1 (H1-core inside the browser PR), T2 (H6-min) and T3 (H2-status-min).
- Defer the rest behind named triggers.

Why: most items harden paths production can't reach, against code due for restructuring, with no CI to hold 27 criteria.
What we might be missing: a provider conversation that needs a security story now; a reviewer or counsel who expects the full spec.
If we're wrong, the cost is: about 4 days of deferred controls are built later, under time pressure, before donor data.

**Challenge 2: cut H7** (CEO, Eng)
You said: build broker-bound D08 binding.
Both models recommend: cut it.
Why: it defends against an in-process attacker, which the threat model excludes. It inverts layering (a library depending on a web type). The browser branch's `session_credential` already sources the binding from server state.
What we might be missing: a planned move to a multi-process or plugin model.
If we're wrong, the cost is: about 0.5 day to add it later.

**Challenge 3: cut or defer H4 (backups and sync refusal)** (CEO, Eng)
You said: build the sealed backup CLI for 12 registries.
Both models recommend: defer until an E9 retention runbook exists and storage is consolidated. Use OS full-disk encryption plus OS backup until then.
Why: no command writes backups today; `restore` needs per-kind pins and dependencies (S5); it adds a crypto format to maintain forever.
What we might be missing: a provider policy that requires app-level encrypted exports.
If we're wrong, the cost is: building H4 later, about 1.5 days once sized correctly.

**Challenge 4: defer H3 (key custody) until the host OS is chosen** (CEO, Eng, DX)
You said: build passphrase PKCS#8 now.
Both models recommend: defer to the host decision; keychain may be the better choice.
Why: the key/pins-directory rule refuses default installs (S8); forcing provider breaks the reader tests; the upgrade path isn't designed.
What we might be missing: issuing a real reader grant before the host is known.
If we're wrong, the cost is: a plaintext key on a real host. Mitigate with the operator requirements (0700 directory, full-disk encryption, no sync).

Both models flag none of these as a security vulnerability *created* by changing direction. The cut items close latent or out-of-model risks.

### Taste decisions (auto-decided; override at the gate)

- **T1, H2 scope.** Recommended: build the `status` half now (T3) and defer the catalog half. Alternative (Claude CEO): defer all of H2, which leaves `status` claiming "verified" from a fixed file.
- **T2, H5.** Recommended: the validator half with a key grammar and the R2 guard (T4, P3). Alternative (codex): cut it as "compliance theater" and decide a vocabulary during E0.
- **T3, H6.** Recommended: dispatch catch plus a counter plus the launch fix; cut the ring and route. Alternative: build the ring as specified, which can be flushed by 401s.
- **T4, profile binding.** Recommended: profile is a required parameter on the server; record it at the root later. Alternative: the plan's DEVELOPMENT default everywhere.
- **T5, launch exchange.** Recommended: keep two steps plus digest binding. Alternative: bind at bootstrap exchange (simpler, but changes the page flow mid-PR).
- **T6, `status` exit code on registry I/O failure.** Recommended: keep 0 and report a distinct `trust_source`. Alternative (codex): a non-zero operational exit.

### Recommended build order (if Challenge 1 is accepted)

1. T0 CI.
2. T1 H1-core, folded into the browser PR, with R1, R3 and R4.
3. T2 H6-min.
4. T3 H2-status-min. This can run in parallel with T1 and T2.
5. T4 H5-validator (only if T2 stands).
6. A provider email about the host OS and at-rest policy (X1, human).

Everything else waits for its trigger.

<!-- AUTONOMOUS DECISION LOG -->
## Decision Audit Trail

| # | Phase | Decision | Classification | Principle | Rationale | Rejected |
|---|---|---|---|---|---|---|
| 1 | 0 | Skip office-hours prerequisite | Mechanical | P6 | Plan is detailed; autoplan intake | Run office-hours |
| 2 | 0 | UI scope = no; DX scope = yes | Mechanical | P1 | 0 real UI terms; CLI/flags/env | — |
| 3 | 0 | Leave TODOS.md untouched; TODOs listed in plan | Mechanical | user brief | Stage only the plan file | Write TODOS.md |
| 4 | 1 | Mode SELECTIVE EXPANSION | Mechanical | override | Autoplan rule | HOLD |
| 5 | 1 | Recommend Approach B (re-sequence) | User Challenge | P1/P5 | Reachable scope only | A (as written), C (UDS) |
| 6 | 1 | Accept delights: link expiry printout; status trust_source | Mechanical | P2 | Same files, <1 d | — |
| 7 | 1 | Defer doctor gate, UDS, keychain | Mechanical | P3 | Outside blast radius | Add now |
| 8 | 1 | Fix route table to GET+POST+HEAD+job regex | Mechanical | P1 | Fail-closed table breaks live routes | GET-only |
| 9 | 1 | Profile passed explicitly to server start | Taste (T4) | P5 | Library never reads env | Default DEVELOPMENT |
| 10 | 1 | Cut H7 | User Challenge | P4/P5 | Out of model; browser branch covers it | Build |
| 11 | 1 | Defer/cut H4 | User Challenge | P3 | Unbuildable restore; no backups ship | Build |
| 12 | 1 | Reader page states (replaced/ended) added to H1 | Mechanical | P1 | Unhandled edge case | Unspecified |
| 13 | 1 | One passphrase helper; separate backup env var | Mechanical | P4 | DRY; secret separation | Shared var |
| 14 | 1 | `rekey` keeps `.pre-rekey` copy | Mechanical | P1 | Rollback depended on cut H4 | Rely on H4 |
| 15 | 3 | Generated `_ROUTE_KINDS` + meta-test | Mechanical | P5 | S10 | Hand list |
| 16 | 3 | One-lock `authorize_session`; `end_session` never resurrects | Mechanical | P1 | Race (codex 3, Claude 12) | Separate calls |
| 17 | 3 | Rate-limit trip returns 429, keeps slot | Mechanical | P1 | S13: DoS not closed otherwise | Burn on trip |
| 18 | 3 | End session only on GRANT_REVOKED/GRANT_NOT_CURRENT | Mechanical | P5 | Enum has no expired/superseded | "revoked, expired, superseded" |
| 19 | 3 | Criterion 5 code → `permission_denied` | Mechanical | P5 | S7 | TBX-READER-DENIED |
| 20 | 3 | Regression tests R1–R4 mandatory | Mechanical | iron rule | S12, S11, S10, S9 | — |
| 21 | 3 | H6 catch at dispatch; invert `_guarded` polarity | Mechanical | P1/P5 | handle_error has no route | handle_error |
| 22 | 3 | Cut diagnostics ring/route; keep counter | Taste (T3) | P5 | Flushable; E9 owns diagnostics | Build ring |
| 23 | 3 | Keep two-step launch + digest binding | Taste (T5) | P3 | Avoid mid-PR page churn | Single exchange |
| 24 | 3 | H2: status half now, catalog half deferred | Taste (T1) | P1/P3 | S3 breaks catalogs | All now / none |
| 25 | 3 | H5: validator with key grammar + R2 | Taste (T2) | P4 | Reuse `validate_longitudinal_public` | Value regex on keys; cut |
| 26 | 3 | Pin scrypt n/r/p on open (if H4 built) | Mechanical | P1 | CPU DoS | Trust header |
| 27 | 3 | CI as T0 | Mechanical | P1 | Nothing enforced without it | Later |
| 28 | 3.5 | Persona = provider operator + founder installer | Mechanical | P6 | Docs evidence | — |
| 29 | 3.5 | TTHW target 3–5 min via runbook | Mechanical | P5 | Competitive tier | Champion |
| 30 | 3.5 | `reader` rejects `--profile`; env-var message names value | Mechanical | P5 | S9 + env trap | Flag-first form |
| 31 | 3.5 | Rename `rekey --encrypt` → `authority set-passphrase` | Mechanical | P5 | Clashes with `rotate` | Keep name |
| 32 | 3.5 | Every refusal = problem+cause+fix+exit 2/3/5 | Mechanical | P1 | Error quality rule | "non-zero" |
| 33 | 3.5 | `status` exit stays 0 with distinct trust_source | Taste (T6) | P5 | Status reports state | Non-zero exit |
| 34 | 3.5 | OPERATOR-GUIDE runbook + README pointer | Mechanical | P1 | No reader docs | — |

## GSTACK REVIEW REPORT

| Review | Trigger | Why | Runs | Status | Findings |
|--------|---------|-----|------|--------|----------|
| CEO Review | `/plan-ceo-review` (via autoplan) | Scope & strategy | 1 | issues_open | 6 proposals, 2 accepted, 4 deferred; 2 critical gaps (pre-existing) |
| Codex Review | `codex exec` (3 phases) | Independent 2nd opinion | 3 | issues_open | CEO 8, Eng 8 (5 P0), DX 8 (3 P0) |
| Eng Review | `/plan-eng-review` (via autoplan) | Architecture & tests (required) | 1 | issues_open | 13 factual/scope errors, 4 regressions, 3 critical gaps |
| Design Review | `/plan-design-review` | UI/UX gaps | 0 | skipped | no UI scope |
| DX Review | `/plan-devex-review` (via autoplan) | Developer experience gaps | 1 | issues_open | score 3/10 → 6.5/10, TTHW ~10 min → 3–5 min |

- **CODEX:** agreed with Claude on every consensus dimension (CEO 6/6, Eng 6/6, DX 6/6). Disagreed on H2, H5, H6 scope and on the `status` exit code (taste T1–T3, T6).
- **CROSS-MODEL:** both models independently found the unreachable explorer gap, the incomplete route table, the `--profile` dispatch break, the nonexistent `status` flags, and that H7 is out of model.
- **VERDICT:** NOT CLEARED. Eng review has open premise and User Challenge gates. Re-sequence per Challenge 1 before building.

**UNRESOLVED DECISIONS:**
- Gate 1: premises PR1, PR3, PR4, PR6
- Challenge 1: re-sequence (Approach B)
- Challenge 2: cut H7
- Challenge 3: defer H4
- Challenge 4: defer H3
