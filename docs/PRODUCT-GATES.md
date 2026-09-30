# Product gate evidence foundation

Status: E14 synthetic/local prerequisite. The product capability remains
disabled.

Run the offline harness from an installed checkout with:

```text
python -m traceback_runner.product_gates \
  tests/fixtures/product_gates/screenshot_manifest.json \
  gate_run_YYYYMMDD
```

The command writes one canonical JSON report to stdout. It does not write a
result artifact, open a socket, invoke AWS, or ingest biological data.

## What the harness measures

- p95 filter/sort time across 10,000 synthetic catalog-shaped records;
- initial 100-row projection serialization across the same fixture;
- Python peak allocation while generating and filtering the 100,000-record
  stress fixture;
- process-level socket denial during the complete run;
- absence of seeded private identifier, local path, and sequence sentinels;
- canonical synthetic screenshot metadata for desktop, mobile, keyboard
  order, screen-reader names, non-color state text, and 200% zoom; and
- exact run, timestamp, Python, operating system, machine, and processor
  evidence.

Local performance results use `observed_local_unapproved`. They can show that a
target was met on the current machine, but they cannot satisfy the approved-host
gate. Screenshot and accessibility entries remain `fixture_only`: metadata is
not a browser capture or manual assistive-technology audit.

## Gates that remain external

The report always includes all nine gate identities. The following remain
unpassed until evidence is supplied by the named work:

- approved-host filter p95 and initial browser render evidence;
- reviewed browser screenshots at required viewports and 200% zoom;
- keyboard-only and screen-reader audit evidence; and
- the five-provider task study.

An external gate cannot be marked `observed_pass` without an evidence
reference. `capability_enabled` is derived from all gate statuses and is false
for the foundation report. Synthetic fixtures, a fast developer laptop, or a
green test suite cannot upgrade that state.

## Scope limits

The memory figure covers Python allocations in this harness, not total process
RSS, SQLite cache behavior, or browser memory. The network guard covers socket
connections from this Python process, not operating-system sandbox evidence.
Those narrower results are retained because they are reproducible prerequisites,
not substituted for approved-host and installed-system validation.
