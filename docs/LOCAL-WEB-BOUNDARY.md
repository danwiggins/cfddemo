# Local operator web boundary

Status: B01 contract foundation; synthetic/local only. This does not start a
production server or authorize real provider operation.

`traceback_runner.web` freezes the security and projection interface that any
later packaged browser implementation must preserve. The preserved Streamlit
demo is not this service.

## Threat boundary

- The bind host is a literal IPv4 or IPv6 loopback address. Hostnames,
  wildcard addresses, LAN addresses, and public addresses fail validation.
- Allowed `Host` and `Origin` values are exact, port-bound values. CORS and
  outbound network are disabled by contract.
- The launcher creates a short-lived one-use bootstrap code. It belongs only
  in the URL fragment, is exchanged by an exact same-origin POST, and is
  invalidated after the first exchange attempt.
- The exchange returns an opaque session for an `HttpOnly`,
  `SameSite=Strict` cookie plus a separate CSRF token. Every read requires the
  session. Every mutation additionally requires exact Host, Origin, and CSRF
  validation.
- Bootstrap and session secrets exist only in process memory, are represented
  by hashes after issuance, expire, and rotate when the process restarts.
- Authorization happens before an opaque object lookup. An unauthenticated
  guessed ID receives the same safe denial as any other missing session and
  reveals no object state.

The current contract uses plain HTTP on a literal loopback origin, so the
cookie is intentionally not marked `Secure`; browsers do not treat arbitrary
loopback HTTP as a secure transport. The service must never bind beyond
loopback to compensate for that constraint.

## Frozen response contracts

`ProblemDetail` carries a registered safe code, bounded problem/cause/fix,
bundled documentation path, owner, retryability, correlation ID, and explicit
preserved/repeated work. `JobProjection` carries an opaque job ID, technical
state, safe stage text, update time, revision, stale state, owner, next action,
optional problem, and revision-bound actions. Unknown fields are rejected and
stale projections cannot enable mutations.

These objects contain no source path, filename, raw sequence, read identifier,
person identifier, unrestricted tool output, or external URL. Later HTTP and
browser adapters should be thin translations over these frozen objects rather
than separate state models.

## Not yet claimed

This foundation does not choose React versus server-rendered HTML, package a
browser runtime, expose a remote API, implement multi-user authorization, or
prove provider usability. Those require their named B-epic evidence and remain
behind the disabled product gate.
