# Protected reader-authorization registry and B01 session binding

Status: E12 prerequisite, synthetic/local only. It does not authorize real
provider operation, clinical use, export, or release. No E12 route exists yet;
this change provides the registry and the boundary those routes must call.

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
instances.

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
   unrevoked, verify against an active key in the current trust, be inside its
   validity window, be at exactly the bound registry head, and include the
   requested cohort registry and measurement. The caller builds its result
   inside the block. On exit the session and the grant are re-resolved before
   the fence is released; any difference denies the return.

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

A failed journal append truncates any torn suffix. At most one uncommitted
object is tolerated and it is removed by the next mutation. A failed create or
restore, including a failed final reopen, removes the target it created.

Bounds: 1,000 grants, 64 trust revisions, 16 keys, 16 cohort and 16
measurement scopes per grant, 16 KiB per object, 2 MiB of journal, 64 MiB of
backup. Contracts are captured as exact bounded canonical bytes before any
authority read, so subclasses, private Pydantic state and oversized graphs
fail first.

Time comes from the package-owned `AuthorityTimeSource`. A check whose time is
earlier than the newest record time is denied (`clock_rollback`), which
bounds how far a rolled-back clock can resurrect an expired grant.

## Known gaps

- Any registry mutation (another grant, a revocation, a rotation) moves the
  head, so every bound session must re-bootstrap. This is the strict reading
  of "no longer at the expected head".
- The rollback fence is per process; a fresh process trusts the retained head
  it is given. Crash recovery beyond a caught failure matches D03/D05.
- There is no HTTP route for the launch exchange yet, and the launch
  credential's delivery to the browser is not defined.
- The composite E12 fence coordinator does not exist; this registry provides
  the shared fence and the in-fence reads it will compose.

## Decisions this change does not make

- Who the external provider authority is, how its trust document is
  provisioned, and who may authorize a trust rotation.
- How real grants are issued and delivered, and who may revoke them.
- How the launcher chooses the grant selector for a launch.
- Whether head equality should be relaxed to "same grant, not revoked, head
  extends the bound head".

All tests use the checked-in synthetic authority or keys generated inside the
test.
