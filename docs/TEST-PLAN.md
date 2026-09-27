# Traceback Release-One Test Plan

Status: `/autoplan` review artifact

## Test architecture

```text
strict contracts and state transitions
  → synthetic modBAM vertical slice
  → fault-injected resumable runner
  → real POD5 qualification on one workstation
  → signed-record privacy and integrity boundary
  → five-operator usability study
  → paid provider pilot
```

## Required suites

| Suite | Required proof |
|---|---|
| Contracts | Unknown fields, non-finite values, invalid transitions, schema versions, canonical bytes |
| Input | Wrong build, corrupt/truncated BAM, missing index, changing POD5, duplicate delivery, unsafe paths |
| Runner | Kill/restart every stage, stale lease recovery, concurrent submit, disk exhaustion, timeout, OOM |
| Fencing | A stale worker cannot publish or commit after a newer lease token exists |
| Snapshot | Mutation during capture blocks; stages read only runner-owned sealed bytes |
| Isolation | Zero egress, no host socket/secret inheritance, no write outside attempt mount |
| Scientific | Complete aligned reference-span scan, filters, exclusions, denominator reconciliation, golden JSON |
| Artifact | Receipt and digest validation, atomic adoption after crash, immutable supersession |
| Signature | Valid, tampered, wrong key, revoked key, missing key, offline verification |
| Upgrade | Backup, expand/contract migration, active-job behavior, previous-runner rollback |
| Privacy | Seed sequence, read IDs, paths, labels, secrets, and raw hashes into every upstream field and prove exclusion |
| Claims | No clinical conclusion, reassurance, “clean,” “normal,” or cancer-signal language in production records |
| UX | Every empty/loading/stale/queued/paused/recovery/failure/success state has a fixture |
| Qualification | Real POD5, pinned Dorado and hg38, one workstation, repeatability and resource envelope |

## Release gates

- Existing offline test suite remains green.
- Identical validated analysis-ready input produces byte-identical measurement
  JSON and chart data.
- A capped, prefix-only, interrupted, or zero-eligible scan cannot publish.
- Missing modification tags do not block the fragment record.
- Every failed stage resumes from the last verified receipt without duplicating
  a record.
- Crashes at every receipt/publication/database boundary produce exactly one
  adopted artifact or a quarantined orphan.
- No seeded forbidden value appears in result bundles, logs, support bundles,
  or UI fixtures.
- Five of five representative operators identify job, state, owner, and next
  action within ten seconds.
- Four of five recover from low disk and interruption without coaching.
- Five of five save and verify a local record without believing data uploaded.
- Nineteen of twenty consecutive eligible pilot runs complete without
  engineering intervention.
- Five of five operators pass doctor, run the synthetic demo, and verify its
  record within five minutes after assisted installation.
- A new workflow developer generates a fixture and passes the signed synthetic
  modBAM conformance path within five minutes.

Synthetic metadata fixtures prove orchestration only. They do not qualify
Dorado, chemistry, the scientific measurement, or hardware.
