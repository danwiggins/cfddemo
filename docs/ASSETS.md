# Assets

Most Lab algorithms need a local file the repository does not ship: a
reference, an atlas, an annotation, a site list, a model or a calibration.
The operator obtains each one through an approved local process, checks its
terms, and registers it under the Lab ROOT. Registration copies or pins the
exact bytes and records their SHA-256; every run re-checks them, and a
changed or missing file makes the method skip with `missing_asset` (or
`invalid_asset`) instead of producing a value. Nothing is downloaded by a
Lab run. Most registrations are write-once per ROOT: to use different bytes,
register under a new ROOT.

Research use only. Registering an asset does not grant rights to its data or
validate a method.

## Lab assets

Placeholders: `R` is the Lab ROOT, `<...>` a local file you supply.

| Asset | Used by | Register | Inspect |
|---|---|---|---|
| hg38 FASTA (UCSC `chr` names; register as id `hg38`) | every method | `traceback reference register --fasta <hg38.fa> --id hg38 --root R` | `traceback lab doctor --root R` |
| ichorCNA toolchain and its GC/mappability wigs and centromere table | `copy-number`, `instability`, `arm-summary`, cohort panels | `traceback toolchain install ichor --yes`, then `traceback method-asset register --from-toolchain copy-number --root R` | `traceback doctor --deep --root R` |
| Loyfer U250 atlas directory (three files) | `cell-origin` | `traceback method-asset register --from-dir <loyfer-dir> --root R` | `traceback lab doctor --root R` |
| Zhou 2018 common-PMD BED.gz | `pmd-score` | `traceback lab asset add pmd --file <pmd.bed.gz> --source-label <label> --terms unknown --root R` | `traceback lab asset inspect pmd --root R` |
| GENCODE v46 basic CHR GTF (pinned SHA-256 `d620d548ad23dad6c2d67486b7679d12f00f02f66dd5787183d8e6678dceb9b4`) | `tss-profile` | `traceback lab asset add tss-annotation --file <gencode.v46.basic.annotation.gtf.gz> --source-label "GENCODE v46 basic CHR GTF" --terms unknown --root R` | `traceback lab asset inspect tss-annotation --root R` |
| Weidner three-CpG hg38 map (in this repo: `docs/assets/weidner-3-grch38-map.json`) | `epi-clock` | `traceback lab asset register-clock-map --file docs/assets/weidner-3-grch38-map.json --sha256 <sha256 from epi-clock.md> --root R` | `traceback lab asset inspect-clock-map --root R` |
| Somalier hg38 sites VCF (gzip) and its independently obtained SHA-256 | `identity` | `traceback lab identity sites add --file <sites.vcf.gz> --sha256 <sha256> --source-label <label> --root R` | `traceback lab identity sites inspect --root R` |
| Griffin checkout, Griffin hg38 reference assets and TF-site lists | `griffin` | `traceback lab asset griffin-register --upstream-root <griffin-checkout> --ref-assets <griffin-ref-dir> --site-list CTCF=<sites> --primary-site CTCF --source-label <label> --terms unknown --root R` | `traceback lab asset griffin-inspect --root R` |
| Viral FASTA (EBV, HPV, HBV) and a `contig`/`virus`/`accession` TSV | `viral-reads` | `traceback lab asset add viral-fasta --file <viral.fasta> --targets <targets.tsv> --source-label <label> --terms unknown --root R` | `traceback lab asset inspect viral-fasta --root R` |
| hg38 open-chromatin centers (BED3) and preferred U/D end sites (BED4) | `aligned-end-structure` | `traceback lab asset end-catalogues-register --centers <centers.bed> --preferred <ends.bed> --source-label <label> --centers-source <accession> --preferred-source <accession> --terms unknown --root R` | `traceback lab asset end-catalogues-inspect --root R` |
| Methylation marker panel JSON | `meth-tf` | `traceback lab asset add meth-tf-reference --file <panel.json> --source-label <label> --terms unknown --root R` | `traceback lab asset inspect meth-tf-reference --root R` |
| MethylBERT fine-tuned bundle | `methylbert` | `traceback lab asset add methylbert-bundle --file <bundle.tar> --source-label <label> --terms unknown --root R` ([contract](METHYLBERT-BUNDLE.md)) | `traceback lab asset inspect methylbert-bundle --sha256 <sha256> --root R` |
| Kraken2/Bracken database directory | `microbial` (not supported for now) | `traceback lab microbial db add --directory <db-dir> --read-length <n> --source-label <label> --root R` | `traceback lab microbial db inspect --root R` |
| Input-scoped prenatal NIPTeR control | `fetal-fraction` | `traceback lab fetal register --file <contract> --sha256 <sha256> --root R` ([details](fetal-nipter.md)) | `traceback lab fetal inspect <input-id> --root R` |
| Input-scoped male-fetus chrY calibration | `fetal-fraction-chry` | `traceback lab fetal chry-register --file <contract> --sha256 <sha256> --root R` ([details](fetal-chry.md)) | `traceback lab fetal chry-inspect <input-id> --root R` |
| Input-scoped transplant chrY calibration | `dd-cfdna-chry` | `traceback lab donor register --file <contract> --sha256 <sha256> --root R` ([details](dd-cfdna-chry.md)) | `traceback lab donor inspect <input-id> --root R` |

Tools that are not assets but are also needed on real data: samtools and
minimap2 (all methods); FinaleToolkit 1.1.0 for `finaletoolkit-xcheck`
(`PATH` or `TRACEBACK_FINALETOOLKIT_BIN`); Somalier for `identity`;
`pyBigWig` for `griffin` (installed by the default `uv sync`). The Lab guide's
[Running on real data](LAB-GUIDE.md#running-on-real-data) puts these together
with the run flags.

`--terms` records the declared data terms (`unknown`, `restricted` or
`known`; `known` also needs `--license-id`). `--source-label` is a short
public description, never a path. Registrations live under `ROOT/lab/assets/`
(the signed `reference` and `method-asset` registrations under `ROOT/`).

Input-scoped calibrations (prenatal, transplant) bind to one stored input and
its reference: registration reads that input's manifest and refuses a
mismatched input ID, source ID or reference SHA-256. These checks protect
provenance; they do not verify the calibration study, model quality, consent
or rights.

## Synthetic release asset registry (signed path)

`traceback assets install` and `traceback assets verify` manage a separate
registry of **synthetic release assets** for the signed path. Installing one
does not satisfy a Lab method's asset check. The rest of this page documents
that registry. It installs only assets named by independently verified
development release evidence. It does not download anything, grant a license,
qualify a workstation or scientific method, authorize real input, or authorize
execution. Installed state, byte integrity, release authority, and lifecycle
currentness are separate results.

### Package format

`traceback.synthetic-asset-package.v1` is deliberately narrower than an archive:

1. the fixed bytes `TRACEBACK-ASSET\0V1\n`;
2. one unsigned, big-endian 32-bit canonical-JSON header length, capped at 256 KiB;
3. one canonical JSON header containing an `identity` encoding, the complete
   frozen `AssetReference`, and its domain-separated reference digest;
4. exactly one uncompressed payload whose length and SHA-256 are declared by
   `AssetContentIdentity`.

There are no entry names, directories, links, devices, compression methods, or
multiple members. Unknown header fields, trailing bytes, partial payloads,
non-regular package files, and symlink package paths are rejected. Payloads are
streamed and have a 2 TiB format ceiling; actual installation is additionally
bounded by the target filesystem capacity and its free-space floor.

`build_synthetic_asset_package` exists to create tiny offline test fixtures. It
accepts caller-supplied bytes and never resolves a source or performs network I/O.

### Install transaction

`AssetRegistry` receives the registry root itself (the CLI maps its product root
to `<root>/assets`). An install:

1. parses the package and invokes `verify_release_asset_authorization` with the
   signed envelope, external public-key trust, external role policy, independent
   fresh authority head, exact expected release binding/package digest, selected
   asset ID/version, reference digest, and an injected aware-UTC time;
2. blocks unknown, invalid, expired, wrong-version, or revoked authority before
   creating a staging directory;
3. takes a process-shared POSIX lock, rejects conflicting duplicate identifiers,
   and rechecks capacity;
4. requires predicted available bytes after peak staging and registration cost
   to remain at or above 20% of total filesystem bytes;
5. copies into a private registry-local staging directory while hashing, fsyncs
   the payload and directory, and atomically publishes the content-addressed
   object;
6. fsyncs and atomically publishes the canonical identifier/version registration.

The registration rename is the commit point. A crash before it can leave only an
unregistered content-addressed object, which readers ignore. A later installer
verifies and adopts matching orphan bytes. A crash after it is an idempotent
success on retry. The lock serializes cooperating installers so they cannot
double-spend the same observed free capacity or overwrite an identifier with a
different reference.

The 20% check is admission control, not a filesystem quota: unrelated writers
outside the registry lock can still consume space after the check.

### Verification semantics

`verify` rehashes the installed object and re-runs current release authorization.
Its result reports independently:

- `installed` and `integrity_status` (`absent`, `valid`, or `invalid`);
- release `authority_status` and its failure code;
- current asset `lifecycle_status` (`active`, `revoked`, or `unknown`);
- whether the historical registration reference equals the current reference.

An old object may therefore remain byte-valid while current authority is unknown
or revoked. Historical bytes are retained for audit, but both
`execution_authorized` and `qualification_established` remain false.
