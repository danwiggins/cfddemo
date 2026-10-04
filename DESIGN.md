# Traceback local operator design

Status: implementation design for the first synthetic development wave.

## Boundary

The CLI is a separate entry point over `traceback_runner`; it does not modify
the public Streamlit demo. The only end-to-end execution enabled in this wave
is the runtime-generated synthetic path. Real input may be inspected by
bounded preflight code, but processing remains disabled.

```text
CLI parsing and stable output
        |
operator state / protocol rendering
        |
durable Runner and sealed stage receipts
        |
synthetic BAM -> preflight -> complete scan -> signed bundle
        |
independent development trust store -> offline verification
```

There is no hosted portal, account, upload, remote inbox, or automatic
workflow update.

## Command contract

`traceback_runner.cli.main(argv)` returns an integer stable exit code. Human
and JSON renderers consume the same structured command result so status and
failure semantics cannot drift. JSON contains no local absolute path or raw
input identifier. Commands that need a local path accept it as input but use a
safe token in diagnostics.

The stable exit codes are 0 success, 2 usage, 3 blocked/unsupported, 4 not
found, 5 invalid or untrusted bundle, 6 retryable runner failure, and 7
unexpected internal failure.

## Completion rule

Job progress and record validity are separate state dimensions. `COMPLETE`
alone renders `Record created; verification required`. The phrase `Signed
local record ready` is available only when all of the following are true:

1. the runner completed every declared stage;
2. the measurement records a complete, uncapped, uninterrupted scan with at
   least one eligible alignment;
3. aggregate and claims validation passed;
4. the signed bundle was built from allowlisted data; and
5. offline verification succeeded against a trust store configured outside
   the candidate bundle.

Development trust is always labeled. It cannot be represented as production
or pilot qualification.

## Operator state

The presentation model groups jobs into needs attention, processing, queued,
completed records, and inactive. It never groups or sorts by biological value.
After three missed 15-second observations, the view reports `Runner status is
stale`, separately from job failure, and directs the operator to refresh before
another action.

Blockers contain a stable code, problem, likely cause, exact fix, owner,
retryability, and documentation path. The UI-state fixture covers empty,
queued, processing, stale, paused, retryable failure, complete-unverified, and
complete-verified states. The fixture is a future UI contract, not evidence of
an operator usability study.

## Protocol fail-closed rule

Approved wet-lab rows require instruction text, owner, versioned source,
protocol version, and last-reviewed date. Unapproved wet-lab rows never expose
their instruction field; rendering replaces it with an approval placeholder.
Non-wet-lab scope and local-retention guidance may render independently.

## Privacy

Support and JSON diagnostics use an allowlist-shaped payload and recursive
redaction. Forbidden keys include local paths, filenames, sample labels, read
identifiers, sequence, secrets, tokens, and raw SHA-256 values. Bundle export
and signature enforcement remain in the signing layer. No command uploads or
deletes data implicitly.

Operator record labels (`ROOT/labels`, unsigned notes) are the one exception
to "no sample labels": they appear in human CLI output and may appear in the
operator's own loopback browser session, but never in any CLI `--json`
output, logs, bundles, exports or support bundles. Their grammar rejects
paths, identifiers and control characters; operators are told not to put
donor names in them.

## Explicit limitations

- Synthetic in-process callbacks do not qualify rootless container isolation.
- A tiny generated BAM does not qualify scientific thresholds or reference
  assets.
- Development Ed25519 keys do not satisfy pilot key-custody requirements.
- The protocol manifest contains no approved wet-lab procedure.
- UI-state fixtures do not satisfy the five-operator E7 acceptance test.
- Real MinKNOW/POD5 execution and the complete E7 surface remain future work.
