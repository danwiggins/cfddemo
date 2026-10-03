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
result artifact, invoke AWS, contact a non-loopback address, or ingest
biological data. It starts the packaged service on one random literal-loopback
port so the service path is part of the local evidence.

## What the harness measures

- p95 filter/sort time across 10,000 synthetic catalog-shaped records;
- initial 100-row projection serialization across the same fixture;
- Python peak allocation while generating and filtering the 100,000-record
  stress fixture;
- process-level socket denial during the complete run;
- byte-identical repeated responses from the authenticated packaged catalog
  route, with release explorer/export disabled and each catalog row's E12
  (longitudinal) state explicitly unavailable;
- rejection of seeded private identifier, local path, and sequence sentinels at
  model boundaries and the authenticated HTTP query boundary without echoing
  them in the response;
- canonical synthetic screenshot metadata for desktop, mobile, keyboard
  order, screen-reader names, non-color state text, and 200% zoom; and
- exact run, timestamp, Python, operating system, machine, and processor
  evidence.

The three scale/performance entries remain `fixture_only`. The catalog population
uses private SQL rather than 10,000 verified E04 imports, the render measurement
ends at Python serialization rather than browser DOM readiness, and `tracemalloc`
does not measure process RSS, SQLite/native allocations, or browser memory.
The packaged-service result is an `observed_pass` local prerequisite. It is not
a browser render or approved-host measurement. Screenshot and accessibility
metadata is retained as a fixture, while the actual keyboard, screen-reader,
200 percent zoom and reviewed-capture requirements are each recorded as
`unmet_no_observed_evidence`.

## Gates that remain external

The report always includes all ten gate identities plus six separate external
requirement states. The following remain
unpassed until evidence is supplied by the named work:

- approved-host filter p95 and initial browser render evidence;
- reviewed browser screenshots at required viewports and 200% zoom;
- keyboard-only and screen-reader audit evidence; and
- the five-provider task study.

The provider-study artifact requires exactly five participants and the frozen
25-outcome task matrix. It enforces the specified five-of-five identification,
four-of-five uncoached recovery, five-of-five no-upload-belief, and five-minute
doctor/demo/verify thresholds. This schema does not create study evidence.

The report's E12 field still reads `unavailable_not_implemented`. That literal
is part of the digested report contract and is not changed here. E12 code does
exist: the D08 read model (#83) and the reader-authorized browser routes, view,
Save and Reopen (#93, `docs/LONGITUDINAL-BROWSER.md`), tested in-process on
synthetic stores. This harness does not exercise them, the E14 evidence does
not cover them, and no production composition root wires them (`traceback
reader launch` starts the service without an explorer). The report binds that
missing E12 evidence and all six unmet external requirements into a
release-control record whose explorer, export and capability fields are fixed
false.

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
