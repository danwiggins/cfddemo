# Synthetic runner durability and recovery

Status: first implementation wave; synthetic inputs and in-process stages only.

This runner proves local orchestration behavior. It does **not** provide OCI
isolation, zero-egress enforcement, secret stripping, production signing,
qualified hardware execution, real genomic processing, or scientific/protocol
qualification. Real-data execution remains disabled.

## Public interface

`traceback_runner.runner.Runner` owns a private root containing SQLite state,
sealed input copies, private attempts, published artifacts, and quarantine.

- `submit(request, source_root, relative_files, idempotency_key=None)` captures
  runner-owned bytes and returns one durable `JobRecord`. The captured tree
  digest must equal `JobRequest.input_tree_sha256_local`; the caller's digest is
  never trusted without this comparison.
- `status(job_id)` returns the durable state without source paths.
- `snapshot(job_id)` returns a relative-only local integrity view.
- `outputs(job_id, stage, stage_definition_sha256=None)` verifies the receipt
  and returns role-to-`Path` mappings for local programmatic use. Those paths
  must not be serialized into exports, logs, or UI fixtures.
- `execute(job_id, stages, worker_id=...)` runs explicitly enabled synthetic
  callbacks. `resume`, `retry`, and `request_pause` operate at safe stage
  boundaries.
- `recover(job_id)` verifies published bytes, adopts exactly the current fenced
  attempt, and quarantines stale, private, or corrupt orphans.
- `backup(destination)` uses SQLite's online backup API rather than copying the
  database and WAL files independently.

Each `StageSpec` requires a name, explicit implementation version, canonical
parameters, and callback. Its definition digest changes when the name, version,
or parameters change. A `StageResult` names relative outputs, export-safe scalar
metadata, and postconditions. A `StageContext` exposes the sealed input, prior
verified stage directories, a private attempt directory, and a fenced heartbeat.

`request_pause` is valid only while a job is `RUNNING` and takes effect after
the current callback publishes or fails. A queued job has no active work to
pause; an operator should leave it queued rather than issue a pause request.

## SQLite and fencing

The database enables WAL, foreign keys, a busy timeout, and `synchronous=FULL`.
Submission uses `BEGIN IMMEDIATE` with unique request and idempotency keys, so
concurrent identical submissions cannot create multiple jobs. Reusing one
idempotency key for different canonical request bytes fails closed.

Every lease acquisition increments a job-local integer fence inside the same
write transaction that creates its stage attempt. Heartbeats and commits require
the current token, owner, stage, and an unexpired lease. A worker whose lease
expired cannot commit after another worker acquires a higher token.

The schema is explicitly version 1. An unknown, missing, or future schema is
refused. This wave does not pretend to provide migrations. Before a later
expand/contract migration, operators must stop writes, create and verify an
online backup, and retain the previous compatible runner. Active-job rollback
policy remains a release gate.

## Publication protocol

1. A fenced worker writes only inside a new private attempt directory.
2. It hashes every declared regular-file output.
3. It writes canonical `receipt.json` with job, attempt, fence, workflow digest,
   stage-definition digest, ordered input digests, output digests, parameters
   through the stage digest, and passed postconditions.
4. Output and receipt files are made read-only and the directory is atomically
   renamed to its unique publication identity. It never replaces an existing
   publication.
5. The worker commits the receipt digest and relative locator in SQLite only if
   its lease is still current and unexpired.

The receipt contains no creation timestamp, so identical content and identities
serialize deterministically. It is local evidence, not a signed export record.

## Restart reconciliation

A crash before publication leaves a private orphan. Recovery quarantines it;
after lease expiry, a new fenced attempt may retry. A crash after publication
but before the database commit leaves a verifiable orphan. Recovery adopts it
only when its token is still the database's current token and the matching
attempt exists. A crash after the database commit re-verifies and reuses the
same receipt. Adoption is idempotent.

Malformed receipts, changed outputs, missing files, unsafe output locators, and
stale-token publications are never adopted. They move under quarantine and
raise a recovery error. Quarantine changes directory permissions only so the
local filesystem permits the move; it does not rewrite evidence bytes.

Read-only mode bits are defense in depth, not a security boundary. A process
with the same user privileges can change them. Production immutability still
requires the specified execution isolation, mount policy, custody controls,
and qualification evidence.

## Remaining gates

- Rootless OCI execution with verified zero egress, bounded resources, and no
  host socket or secret inheritance.
- Durable cancellation and structured event/log retention.
- Disk-pressure, timeout, and OOM enforcement.
- Signed workflow resolution and signed result bundles.
- Reviewed expand/contract migrations and active-job rollback exercises.
- Real modBAM/POD5, Dorado, hg38, workstation, scientific, and protocol
  qualification.
