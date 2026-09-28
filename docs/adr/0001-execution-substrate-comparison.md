# ADR 0001: provisional execution-substrate direction

- Status: provisional; no executor selected or qualified
- Date: 2026-09-27
- Scope: A02 synthetic comparison only

> Historical evidence note: host inventory and runtime availability in this ADR
> were observed on 2026-09-27. They are not a live inventory. Later development
> experiments, including CPU-only native tool smokes on another local runtime,
> do not retroactively exercise or qualify any A02 candidate.

## Decision

Carry direct rootless Podman execution forward as the provisional candidate for
the A04 Linux experiment. Do not freeze a production executor contract or claim
that Podman, EPI2ME, Nextflow, Docker, GPU execution, or zero egress is qualified.

The vendor-workflow/verifier wrapper and pinned Nextflow adapter remain viable
comparators. The decision must be revisited after all three run the same trace on
the intended Linux/NVIDIA workstation. Traceback's job store, sealed snapshots,
receipt validation, fencing, and publication remain authoritative regardless of
which substrate launches a process. A vendor report, Nextflow cache hit, or
container exit code cannot publish a record by itself.

## Evidence vocabulary

- **Observed**: directly measured on the current host or by the checked-in
  synthetic fixture harness.
- **Documented**: stated by the vendor's current primary documentation but not
  reproduced here.
- **Untested**: required by Traceback but neither observed nor established by
  the cited documentation in the needed environment.

These labels describe comparison claims. They are not A01 qualification
`EvidenceStatus`, approval, release authorization, or permission to process real
data.

## Current-host inventory: observed

The comparison host is macOS on ARM64. The following was observed without
installing software or downloading images, workflows, models, or references:

| Capability | Observation |
|---|---|
| Podman | unavailable |
| Nextflow | unavailable |
| EPI2ME automation entry point | unavailable |
| usable Java runtime | unavailable |
| NVIDIA runtime/GPU | unavailable |
| Docker | client 29.4.0 present; daemon unavailable |
| qualified Linux host | unavailable |

Therefore no actual candidate executor ran. The fixture harness results are
software-contract observations only and do not count as executor isolation,
performance, GPU, workflow, vendor, or scientific qualification.

## Common conformance fixture

`python -m tests.executor.conformance --json` runs three identical synthetic
Traceback custody/recovery scenarios for each candidate identity:

1. complete and adopt one canonical fixture output;
2. reject a changed sealed input before dispatch; and
3. recover after a process boundary following publication without rerunning the
   stage.

`ExecutorRequestFixture` is deliberately isolated under `tests/`. It fixes
synthetic-only input, `network="none"`, argv, opaque mounts, bounded resources,
and `real_data_authorized=False`. It is not a production runner API and cannot
authorize or launch real data. The callbacks run in the existing in-process
synthetic runner; no candidate runtime is invoked. Every trace records
`executor_exercised=false` and `qualified=false`.

## Primary-source findings: documented

### Vendor workflow plus verifier wrapper

Oxford Nanopore documents EPI2ME Desktop as using Nextflow and Docker. Initial
workflow installation expects network access; later workflow runs can generally
run offline after dependencies and references are prepared. The vendor also
warns that not all workflows support Apple ARM natively and that GPU workflow
support on macOS is unavailable. EPI2ME provides a convenient operator surface,
but its workflow state and reports do not establish Traceback snapshot custody,
fenced adoption, export privacy, or independent verification.

Sources, accessed 2026-09-27:

- https://epi2me.nanoporetech.com/epi2me-docs/installation/
- https://epi2me.nanoporetech.com/epi2me-docs/help/faq/

### Direct rootless OCI

Podman documents rootless user namespaces and direct controls relevant to the
future boundary: `--network=none`, `--read-only`, read-only bind mounts,
`--pull=never`, user-namespace modes, memory limits, and ulimits. Those controls
are individually inspectable and introduce no second workflow cache. Their
presence in documentation is not proof they work together on the intended host,
and rootless containers do not protect against a compromised host administrator,
kernel, or GPU driver.

Sources, accessed 2026-09-27:

- https://docs.podman.io/en/stable/markdown/podman.1.html
- https://docs.podman.io/en/latest/markdown/podman-run.1.html

### Pinned Nextflow adapter

Nextflow documents container support, including Podman, and an offline mode.
`NXF_OFFLINE=true` prevents automatic repository updates and plugin downloads;
plugins must already exist and be explicitly versioned. Resume requires both the
task cache and work directory. Task hashes bind process inputs, script, and
container metadata, but standard file identity uses path, modification time, and
size. That is useful orchestration infrastructure, not a substitute for
Traceback's byte digests, sealed inputs, postconditions, receipt graph, or lease
fence. Nextflow's cache must therefore remain advisory behind Traceback adoption.

Sources, accessed 2026-09-27:

- https://docs.seqera.io/nextflow/cache-and-resume
- https://docs.seqera.io/nextflow/container
- https://docs.seqera.io/nextflow/reference/env-vars#nxf-offline

## Comparison

| Criterion | Vendor wrapper | Direct rootless Podman | Pinned Nextflow adapter |
|---|---|---|---|
| Current-host runtime | unavailable | unavailable | unavailable |
| Operator workflow | strongest documented surface | Traceback must own | Traceback must own or wrap |
| Isolation controls | delegated through Docker/workflow configuration | directly expressed per invocation | delegated through Nextflow plus runtime config |
| Resume state | vendor/Nextflow state plus Traceback reconciliation | Traceback receipts only | Nextflow cache plus Traceback reconciliation |
| Offline preparation | documented after initial setup | image must be preloaded and pull disabled | pipeline/plugins/images must be preloaded and pinned |
| Additional authority | must not become publication authority | none beyond OCI runtime | cache must not become publication authority |
| Maintenance surface | vendor application, workflow, Nextflow, Docker | runtime plus narrow adapter | Java, Nextflow, plugins, runtime, adapter |
| Measured isolation/performance | untested | untested | untested |

Direct Podman is provisionally preferred because the future A04 experiment can
inspect one argv boundary and one runtime policy without reconciling a second
scheduler. This is a reversibility and evidence decision, not a claim that custom
execution is inherently safer or cheaper.

## Required next experiment

On the proposed exact Linux x86-64/NVIDIA workstation, preload one tiny pinned
synthetic image and the pinned vendor/Nextflow fixture without network access.
For each candidate:

1. run the identical canonical fixture request with no shell interpolation;
2. prove input mounts cannot be written and only the attempt mount changes;
3. attempt network access, host-secret reads, mount escape, extra device access,
   and writes outside the attempt mount;
4. enforce timeout, process-group termination, CPU/memory/disk bounds, and the
   declared GPU device set;
5. kill before and after publication, restart offline, and verify one Traceback
   adoption or explicit quarantine;
6. change input, workflow, image, parameters, and postconditions independently
   and prove all dependent receipts invalidate; and
7. record installation size, launch overhead, peak resources, logs requiring
   redaction, operational steps, and exact versions.

The EPI2ME route additionally needs a supported non-interactive pinned workflow
entry point and a verifier handoff. The Nextflow route additionally needs pinned
Java/Nextflow/plugin versions, local cache/work retention, and proof that
`NXF_OFFLINE` performs no downloads. If a candidate cannot express or expose the
required controls, it fails rather than receiving simulated evidence.

## Consequences and remaining gaps

- A04 owns any production executor request and enforcement code after measured
  comparison. This ADR does not freeze that interface.
- No real-input CLI path is enabled and no qualification decision changes.
- No OCI image, workflow, model, reference, or large runtime was installed.
- Linux namespace, cgroup, seccomp/capability, filesystem, GPU, offline, timeout,
  and process-tree behavior remain untested.
- Vendor licensing, support terms, exact workflow suitability, and stable
  automation interfaces remain unverified.
- Candidate choice remains provisional until the named experiment produces
  reviewable evidence.
