# Protocol & Setup

Status: documentation contract and sourced device baseline.
Exact wet-lab instructions are not yet approved for operator use.

## What this page will do

This page will guide a trained operator through:

```text
provider-performed blood draw
  → plasma processing
  → cfDNA extraction
  → native-DNA library preparation
  → MinION sequencing
  → local POD5 processing
  → technical QC
  → research record
```

It is not a phlebotomy tutorial and does not authorize an untrained person to
collect blood. The publishable protocol must be reviewed and versioned by the
scientific owner.

## Protocol approval table

| Stage | Required published detail | Current state |
|---|---|---|
| Blood collection | Operator qualification, tube, volume, labeling, mixing, handling, time and temperature limits | Unapproved |
| Plasma processing | Centrifugation equipment, force/time/temperature, transfers, contamination and rejection rules | Unapproved |
| cfDNA extraction | Kit, input/output volumes, controls, storage, concentration and QC | Unapproved |
| Library preparation | Native/PCR-free kit, input, cleanup, controls, flow-cell compatibility | Unapproved |
| Sequencing | MinION/flow cell/kit, MinKNOW configuration, run checks and stop criteria | Device baseline sourced; workflow unapproved |
| Processing | POD5 retention, Dorado models, hg38, samtools, modkit and Traceback release | Specification drafted |

Traceback must fail closed: an unapproved row can be shown as “pending protocol
qualification,” but cannot render imperative instructions.

## Materials structure

The final bill of materials must give each item:

- required, recommended, or optional status;
- manufacturer part number or generic performance specification;
- compatible protocol and workflow versions;
- quantity per sample and per batch;
- storage and shelf-life constraints;
- allowed substitute or “no substitution” rule;
- estimated cost range;
- supplier link;
- safety or training requirement; and
- last-reviewed date and owner.

Material groups:

1. blood collection and labeling;
2. transport and temperature control;
3. plasma separation and transfer;
4. cfDNA extraction and cleanup;
5. concentration and quality control;
6. native-DNA library preparation;
7. MinION device, flow cell, sequencing kit, and controls;
8. pipettes, filtered tips, tubes, racks, magnets, and cold blocks;
9. centrifugation and cold storage;
10. sequencing host, local SSD, backup, and network; and
11. PPE, sharps, biohazard, spill, and waste handling.

## MinION Mk1D baseline

Oxford Nanopore's current device documentation describes:

| Item | Baseline |
|---|---|
| Device | MinION Mk1D, `MIN-101D` |
| Host connection | USB Type-C; USB-A adapters are not supported |
| Power | Maximum rated power 7.5 W |
| Size and weight | 55 × 13 × 125 mm; 130 g |
| Sequencing environment | 10–35°C ambient |
| Control software | MinKNOW |
| Raw format | POD5 |
| Basecaller | Dorado, integrated with MinKNOW or run after acquisition |
| Research status | Oxford Nanopore labels the device research-use only |

The supported production workflow must pin the exact flow cell, sequencing kit,
MinKNOW version, Dorado release, canonical model, modified-base model, hg38
assets, samtools, modkit, and Traceback release.

## Processing computer

The vendor table below is acquisition context, not Traceback processing
qualification. Release one will publish exactly one measured processing-host
profile after real POD5 qualification.

| Component | Minimum class | Recommended class |
|---|---|---|
| Purpose | MinKNOW acquisition baseline from current vendor guidance | Vendor-recommended acquisition headroom |
| Memory | 16 GB; supported Apple systems: 24 GB unified memory | 32 GB |
| Storage | 1 TB SSD | 2 TB SSD |
| GPU | Current supported entry accelerator; ONT currently lists RTX 5060 Laptop GPU+ | Current supported high-end accelerator; ONT currently lists RTX 5090 Laptop GPU |
| Apple | M4 Pro+ with 24 GB+ unified memory | Workflow-specific benchmark required |
| OS | Windows 10/11, Ubuntu 22.04/24.04 LTS, or supported macOS for minimum profile | Windows 10/11 or Ubuntu 22.04/24.04 LTS in ONT's recommended table |
| CPU | Modern AVX2-capable supported processor, at least six cores | At least 12 supported cores |
| Connection | USB-C data and sufficient power | Direct USB-C |
| Network | Outbound TCP 80/443 for supported updates, account, and telemetry workflows | Stable wired or high-quality connection |

Traceback will publish two independent checks:

- **Acquisition ready:** can MinKNOW collect the run without interruption?
- **Processing ready:** can the pinned post-run workflow finish within its
  published time and peak-storage envelope?

The first `Processing ready` profile is Linux x86-64 with one qualified NVIDIA
GPU, exact driver/runtime versions, measured peak RAM, measured peak disk, and
measured completion time. macOS, Windows, ARM, CPU-only, real-time basecalling,
and adaptive sampling are not release-one processing promises.

A machine can pass acquisition and fail local processing. The operator must
then move the complete run package to another qualified local processing host
without losing POD5 or metadata.

## Storage planning

Oxford Nanopore's Mk1D guide gives these examples when POD5, compressed FASTQ,
and unaligned modified BAM are all retained:

| Flow-cell output | POD5 | FASTQ.gz | Unaligned modified BAM |
|---:|---:|---:|---:|
| 10 Gbases | 70 GB | 6.5 GB | 6 GB |
| 15 Gbases | 105 GB | 9.75 GB | 9 GB |
| 30 Gbases | 210 GB | 19.5 GB | 18 GB |
| 50 Gbases | 350 GB | 35 GB | 30 GB |

Those examples assume a 23 kb read N50. cfDNA has much shorter fragments, so
Traceback must measure actual run behavior and apply workflow-specific
headroom. Preflight reserves space for input, temporary basecalling/alignment,
sorted output, checkpoints, final artifacts, and 20% free-space safety margin.

## Required run outputs

| Output | Why it matters |
|---|---|
| POD5 | Original signal and only reliable route to future re-basecalling |
| Sample sheet | Sample, device, flow-cell, kit, barcode and reference metadata |
| Final summary/report | Run completion and technical acquisition evidence |
| Sequencing summary | Per-read length, quality, duration, channel and optional alignment data |
| Output hash file | Integrity evidence when emitted |
| BAM/modBAM | Basecalled reads and modified-base probabilities when configured |
| BAI/CSI | Coordinate index for aligned analysis |

FASTQ alone does not retain the modified-base probability tags required for the
cell-origin workflow. Legacy FAST5 is outside the first release because current
Dorado documentation has ended FAST5 support.

## MinKNOW configuration checklist

The final page will validate rather than merely describe:

- [ ] pseudonymous sample and experiment identifiers;
- [ ] registered flow cell and kit;
- [ ] correct native-DNA protocol;
- [ ] POD5 retention enabled;
- [ ] sufficient local storage;
- [ ] supported basecalling mode;
- [ ] compatible modified-base model;
- [ ] required BAM and summary outputs;
- [ ] registered reference where live alignment is used;
- [ ] controls and sample-sheet fields;
- [ ] output directory on approved local storage;
- [ ] update/restart/antivirus risks controlled during the run; and
- [ ] post-run retention and transfer owner assigned.

## File custody

- POD5, FASTQ, BAM/modBAM, sequencing summaries, and read IDs stay in the
  provider-controlled local environment by default.
- The hosted product receives only signed aggregate results and allowed
  provenance.
- Do not place personal information in MinKNOW free-text telemetry fields.
- Never delete POD5 automatically.
- A transfer is complete only after manifest, size, format, and available hash
  checks pass.

## Sources

- [MinION Mk1D device and IT specifications](https://nanoporetech.com/document/requirements/minion-mk1d-device-and-it-specifications)
- [MinION product page](https://nanoporetech.com/products/sequence/minion)
- [MinKNOW output structure](https://software-docs.nanoporetech.com/output-specifications/latest/minknow/output_structure/)
- [POD5 output specification](https://software-docs.nanoporetech.com/output-specifications/latest/read_formats/pod5/)
- [BAM output specification](https://software-docs.nanoporetech.com/output-specifications/latest/read_formats/bam/)
- [Sequencing summary specification](https://software-docs.nanoporetech.com/output-specifications/latest/protocol_formats/sequencing_summary/)
- [Sample sheet specification](https://software-docs.nanoporetech.com/output-specifications/latest/protocol_formats/sample_sheet/)
- [Dorado basecalling](https://software-docs.nanoporetech.com/dorado/latest/basecaller/basecall_overview/)
- [Dorado modified-base calling](https://software-docs.nanoporetech.com/dorado/latest/basecaller/mods/)
