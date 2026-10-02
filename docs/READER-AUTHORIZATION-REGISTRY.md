# Protected reader-authorization registry and B01 session binding

Status: E12 prerequisite, local only. It does not authorize clinical use,
export, or release. No E12 read route exists yet; this provides the registry,
the local operator authority, the launch exchange route, and the boundary
those routes must call.

`evidence_inspector/reader_authorization_registry.py` holds the
`ReaderAuthorizationRegistry`. `traceback_runner/web/reader_session.py` binds a
B01 browser session to one grant and exposes the E12 boundary function.

## Threat model

- The process and OS-user boundary is the trust boundary. In-process code
  mutation and same-user filesystem races are out of scope, as in the D03, D05
  and E06 registries. Seals detect accidental or naive replacement of the
  registry's methods, pinned callables, result constructors and instance
  authority state. There is no whole-module namespace seal.
- The checked-in synthetic provider authority
  (`evidence_inspector/reader_authorization_synthetic.py`) is a public fixture.
  Its private seeds are in the repository, so anyone can sign with it. It is
  accepted only in the `synthetic` profile and never authorizes anything else.

## What a grant is

A `SignedReaderGrant` is an Ed25519 signature by an external provider
authority over `traceback-reader-grant-v1\0` plus the canonical bytes of a
`ReaderGrantPayload`. This is the provider-linkage approval pattern (domain
separator, canonical contract bytes, raw Ed25519, public keys in a trust
document). The payload binds:

- profile, registry ID and epoch, so a grant cannot be replayed into another
  registry or profile;
- an opaque grant selector (`reader_grant_` + 32 hex), unique per registry;
- the provider authority ID and key version;
- the role, which is the literal `longitudinal_reader`. There is no other role
  and no wildcard;
- 1 to 16 exact D05 cohort-registry IDs and 1 to 16 exact D02 measurement
  scopes (family, quantity, unit), uniquely sorted. Patterns reject `*` and
  any non-exact identifier;
- whole-second UTC issue and expiry times, at most 90 days apart.

A grant is immutable. Revocation is a separate append-only record naming the
selector and grant digest. Revocation is unsigned because it only removes
authority.

## Provider trust and profiles

`ReaderProviderTrust` names one authority ID and up to 16 versioned public
keys with `active` or `revoked` status. It is chained by revision and
predecessor digest. The registry's first record is the configured trust
(revision 1). `rotate_trust` appends the next revision under an independently
supplied digest pin. A rotation must keep every key version and public key,
may only add higher versions, and can revoke but never reactivate a key.
Resolution always checks the grant's signature against the current trust, so
revoking a key in a rotation denies every grant it signed.

Profiles:

- `synthetic`: the trust may name only the checked-in synthetic authority ID
  and its checked-in public keys.
- `provider`: the trust may not name the synthetic authority ID or any
  checked-in synthetic key, so a synthetic signature never verifies.

The profile is bound into the metadata, the trust, and every grant. One
process may open registries of one profile only; opening the other profile
fails.

## Startup

`ReaderAuthorizationRegistry.create` makes a new registry whose first journal
record is the configured trust. Opening an existing registry requires, with no
defaults:

- the profile;
- the configured trust document and its independently retained digest, which
  must equal the registry's current trust;
- the independently retained registry ID, epoch and expected head.

A missing root never bootstraps a new registry. A missing registry, a wrong
pin, or no registry passed to `ReaderSessionBinder` (`registry=None`) disables
E12: every reader check is denied.

## The fence

The registry lock file is one cross-process fence:

- `add_grant`, `revoke_grant` and `rotate_trust` hold it exclusively;
- `authority_read_fence()` holds it shared. `bind_grant_in_fence` and
  `authorize_reader_in_fence` refuse to run unless the current thread holds
  it.

So no grant add, revocation or key rotation from any process can land while a
reader check and the work it protects are in progress. Opening a registry also
takes the exclusive lock briefly. The fence is not reentrant, and a mutation
attempted by the thread that holds it fails instead of silently converting the
shared lock. Within a process, a module lock serializes all registry
instances. `flock` locks belong to the open file description, which `fork()`
shares, so an instance refuses every locked operation in a process other than
the one that opened it.

The fence covers construction of the protected result, not its transmission:
an E12 handler must build the immutable response object inside the
`reader_authorization` block. The fence is released after final revalidation
and that construction, matching the E12 coordinator contract ("releases after
return-value construction"). Bytes already built may be sent after a
revocation that lands later; they were authorized as of the fence.

In the E12 global order (D01, D04, D05, reader authorization, D06, ...) this
fence is acquired after D05. The registry calls no other store while holding
it.

## Session binding (B01)

`BootstrapBroker` session records gain one optional field,
`reader_binding: ReaderSessionBinding | None`, which holds only the grant
commitment (the grant's canonical SHA-256) and the registry head at binding
time. Existing routes never read it, so a bare B01 session behaves exactly as
before for every non-E12 route.

1. The server-side launcher calls
   `ReaderSessionBinder.issue_launch_credential(grant_selector)`. This mints a
   separate opaque one-use credential (at least 256 bits, kept in memory as a
   hash, 60 s TTL, at most 16 pending, bound to the B01 authority). The
   browser never sees or supplies the selector.
2. The browser, already holding a B01 session, presents the credential in a
   POST that passes B01 Host, Origin, session and CSRF checks.
   `exchange_launch_credential` consumes the credential before verifying it,
   then, under the registry fence, resolves the selector to a current grant
   (present, unrevoked, signed by an active trusted key, inside its validity
   window) and stores the binding on the session while the fence is still
   held. A session binds once. Exchange attempts are throttled.
3. Every E12 read or save calls
   `with binder.reader_authorization(request, cohort_registry_id=...,
   measurement_scope=...) as authorization:`. B01 transport checks run first
   and keep their own errors. Under the fence, the bound grant must exist, be
   unrevoked, verify against a key that is active in the current trust, be
   inside its validity window, and include the requested cohort registry and
   measurement. The bound registry head must still be in the committed journal
   chain, so a replaced, restored or rolled-back registry denies. Only changes
   to the session's own grant or signing key end it: its revocation, its
   expiry, a request outside its scope, its key being revoked or rotated out,
   or a trust that no longer names its authority and key. Other grants,
   revocations and trust revisions that leave its key active do not. The
   caller builds its result inside the block. On exit the session and the
   grant are re-resolved before the fence is released; any difference denies
   the return.

Every failure is `ReaderAuthorizationDenied` with `code` and text
`permission_denied`. It carries a closed `reason` for tests and diagnostics
and no reader, selector, authority, registry, scope or session identifier.
Registry integrity or storage failures at this boundary also become
`permission_denied` (`registry_unavailable`).

The binder accepts no role, principal, grant object, scope list, signature or
approval digest from the browser. The requested cohort and measurement come
from the E12 request and are only checked against the grant.

## Storage and bounds

Storage follows the D03 decision registry: a private `0700` root, `0600`
owner-only files, descriptor-relative no-overwrite publication with fsync and
hard-link adoption, a journal chain from a genesis digest over immutable
metadata, a process-wide monotonic head fence against rollback, inode-bound
control files, and a process-private instance seal. Every load replays the
whole journal through the same record rules used for appends (genesis trust
first, trust rotation rules, grants bound to this registry and recorded while
current under an active key, unique selectors, one revocation per registered
grant, non-decreasing record times).

A failed journal append truncates any torn suffix back to the last committed
entry, so the chain stays readable and the mutation can be retried. At most
one uncommitted object is tolerated and it is removed by the next mutation. A
failed create or restore, including a failed final reopen, removes the target
it created.
Crash recovery follows the shared storage behaviour in
`docs/REGISTRY-STORAGE.md`. Creation and restore are staged in a hidden
sibling and published with one rename. A torn journal tail fails closed on
reopen until an operator runs `recover_torn_journal_tail` with the retained
identity and head. Owned `.tmp-<32 hex>` names are swept under the
exclusive lock (the D05 rule). A failed append truncates on any exception.

Bounds: 1,000 grants, 64 trust revisions, 16 keys, 16 cohort and 16
measurement scopes per grant, 16 KiB per object, 2 MiB of journal, 64 MiB of
backup. Contracts are captured as exact bounded canonical bytes before any
authority read, so subclasses, private Pydantic state and oversized graphs
fail first.

Time comes from the package-owned `AuthorityTimeSource`. A check whose time is
earlier than the newest record time is denied (`clock_rollback`), which
bounds how far a rolled-back clock can resurrect an expired grant.

## Local operator authority (decided 2026-10-01)

The provider authority is a local operator authority, managed by
`traceback reader` (`traceback_runner/reader_cli.py`):

- It is a configuration of the `provider` profile, not a new profile. That
  profile already refuses the synthetic authority ID and every synthetic key,
  and the registry cannot see where a private key is kept, so an `operator`
  profile would add an enum value and no check.
- `traceback reader authority init` generates one Ed25519 key (version 1), a
  random `reader_authority_` ID and trust revision 1, and creates the
  registry. The key is an unencrypted PKCS#8 PEM file, mode `0600`, in a
  private `0700` authority directory that may not overlap the registry root.
  It is never printed or logged. **This is local-file custody, not production
  custody.**
- The same directory holds the operator's retained pins (`authority.json`:
  registry ID, epoch, head, trust and trust digest). Every command opens the
  registry with them and re-pins the head after each mutation, under an
  operator lock file.
- `authority rotate` appends a trust revision adding key version N+1, then a
  revision revoking every previously active key, and deletes the old key
  file. Grants signed by the old key stop authorizing and must be reissued.
  If a rotation stopped after adding its key (run `authority recover`
  first if the pins lag), the next `rotate` only finishes it: it revokes all
  but the newest active key and deletes every revoked key file.
  Revoked keys stay in the trust, so the 16-key bound allows 15 rotations
  per registry; after that a new authority and registry are needed.
- `authority recover` re-pins after a crash between a registry append and the
  pins write. It accepts the journal tail only when the retained head is in
  the journal and the registry then opens and replays at that tail.
- `grant issue --cohort ID --measurement FAMILY:QUANTITY:UNIT
  --expires-in-days N` signs and registers one grant (N is 1 to 90) and prints
  its selector. `grant revoke SELECTOR` revokes it. `grant list` prints
  selectors and states (`active`, `revoked`, `expired`, `not_yet_valid`,
  `untrusted_key`) only.
- `launch --grant SELECTOR` checks the grant is active, starts the loopback
  server with the registry, and prints a one-use launch link to the terminal;
  Enter prints a fresh one.

### Launch flow

The link is `http://127.0.0.1:PORT/#bootstrap=CODE&reader_launch=CREDENTIAL`.
Both secrets are in the fragment, which the browser never sends to the server
or in a Referer (responses also set `Referrer-Policy: no-referrer`, and the
server writes no access log). The packaged page clears the fragment, exchanges
the bootstrap for a session cookie and CSRF token, then POSTs
`{"launch": CREDENTIAL}` to `/api/v1/session/reader-launch` with the cookie,
`Origin` and `X-Traceback-CSRF`. The route runs the B01 mutation checks before
reading the body and calls `ReaderSessionBinder.exchange_launch_credential`,
which repeats them. A GET page that auto-submits a form was not needed: the
page script already holds the CSRF token in memory after the bootstrap, and a
plain form could not carry the CSRF header. Both secrets live 60 seconds and
are single-use.

## Known gaps
- The rollback fence is per process; a fresh process trusts the retained head
  it is given.
- A restore is a replacement, not a replica. The fence is the lock file inside
  one root, so running the original and a restored copy at the same time
  splits it: a revocation in one is invisible to the other. Operators must
  retire the original before opening a restored copy.
- No E12 read route consumes a bound session yet.
- Launch credentials live in the `launch` process's memory, so a link can be
  issued only by the process serving it.
- The composite E12 fence coordinator does not exist; this registry provides
  the shared fence and the in-fence reads it will compose.

## Decisions still open

- An external provider authority and production key custody.

Tests use the checked-in synthetic authority or keys generated inside the
test.
