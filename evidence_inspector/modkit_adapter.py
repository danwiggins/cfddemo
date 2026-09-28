"""Pinned, fail-closed adapter for native ``modkit extract full`` 0.6.4.

The adapter is deliberately narrower than Modkit.  It accepts only complete
paired C+m and C+h probability rows produced by the digest-pinned 0.6.4
release, hashes read names before disk-backed grouping, and validates CpG
context against the exact FASTA and FAI bytes named by the execution receipt.
"""

from __future__ import annotations

import csv
import gzip
import hashlib
import io
import json
import math
import os
import sqlite3
import tempfile
from pathlib import Path
from typing import Literal

from pydantic import Field, ValidationError, model_validator

from evidence_inspector.cell_origin_inputs import CellOriginInputError
from evidence_inspector.cell_origin_models import (
    CpgCallState,
    ModProbabilityPolicy,
    ModkitCpgCallV2,
    ModkitIngestionLedgerV2,
    ModkitInputProvenanceV2,
    ModkitInputResultV2,
    ModkitSourceSchema,
    Sha256,
    Strand,
    StrictModel,
)

MODKIT_VERSION = "0.6.4"
MODKIT_EXECUTABLE_SHA256 = (
    "e365089a0d234dc4145f7af57f4f042b9d6714e4e50d15b82a4f2f3328492955"
)
MODKIT_LICENSE_SHA256 = (
    "39cc712a23eead54302ce722e2d0cb6eb73d94ad42f407e7153829a4d5154884"
)
# Captured from the digest-pinned release executable using ``extract full
# --cpg``. In v0.6.4, ``--cpg`` filters through an implicit CpG motif but does
# not set the explicit ``motif`` argument that enables the optional ``motifs``
# output column. An explicit ``--motif`` command is a different schema.
MODKIT_FULL_HEADER = (
    "read_id",
    "forward_read_position",
    "ref_position",
    "chrom",
    "mod_strand",
    "ref_strand",
    "ref_mod_strand",
    "fw_soft_clipped_start",
    "fw_soft_clipped_end",
    "alignment_start",
    "alignment_end",
    "read_length",
    "mod_qual",
    "mod_code",
    "base_qual",
    "ref_kmer",
    "query_kmer",
    "canonical_base",
    "modified_primary_base",
    "inferred",
    "flag",
)


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class AlignmentExclusionLedger(StrictModel):
    """Mutually exclusive BAM-record accounting before Modkit extraction."""

    input_records: int = Field(ge=0)
    excluded_unmapped: int = Field(ge=0)
    excluded_secondary: int = Field(ge=0)
    excluded_supplementary: int = Field(ge=0)
    excluded_qc_fail: int = Field(ge=0)
    excluded_duplicate: int = Field(ge=0)
    excluded_below_mapq: int = Field(ge=0)
    accepted_primary_records: int = Field(ge=0)

    @model_validator(mode="after")
    def reconcile(self) -> AlignmentExclusionLedger:
        terminal = (
            self.excluded_unmapped
            + self.excluded_secondary
            + self.excluded_supplementary
            + self.excluded_qc_fail
            + self.excluded_duplicate
            + self.excluded_below_mapq
            + self.accepted_primary_records
        )
        if terminal != self.input_records:
            raise ValueError("alignment exclusion ledger must reconcile")
        return self


class AlignmentPrefilterReceipt(StrictModel):
    """Digest-bound, structurally validated samtools prefilter execution."""

    schema_version: Literal["traceback.alignment-prefilter.v1"] = (
        "traceback.alignment-prefilter.v1"
    )
    samtools_version: str = Field(min_length=1, max_length=64)
    samtools_executable_sha256: Sha256
    executable_arg: str = Field(min_length=1, max_length=1024)
    source_bam_arg: str = Field(min_length=1, max_length=1024)
    output_bam_arg: str = Field(min_length=1, max_length=1024)
    source_bam_sha256: Sha256
    output_bam_sha256: Sha256
    minimum_mapq: int = Field(ge=0, le=255)
    flag_exclusion_mask: Literal[3844] = 3844
    argv: tuple[str, ...] = Field(min_length=10)
    alignment_ledger: AlignmentExclusionLedger

    @model_validator(mode="after")
    def validate_command(self) -> AlignmentPrefilterReceipt:
        expected = (
            self.executable_arg,
            "view",
            "-b",
            "-F",
            str(self.flag_exclusion_mask),
            "-q",
            str(self.minimum_mapq),
            "-o",
            self.output_bam_arg,
            self.source_bam_arg,
        )
        if self.argv != expected:
            raise ValueError(
                "alignment prefilter argv does not match its bound command fields"
            )
        return self


class ModkitExecutionManifest(StrictModel):
    """Digested execution record required by the native adapter."""

    schema_version: Literal["traceback.modkit-execution.v1"] = (
        "traceback.modkit-execution.v1"
    )
    source_complete: Literal[True]
    source_selection: str = Field(min_length=1, max_length=256)
    modkit_version: Literal["0.6.4"]
    modkit_executable_sha256: Literal[MODKIT_EXECUTABLE_SHA256]
    modkit_license_sha256: Literal[MODKIT_LICENSE_SHA256]
    container_image_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    executable_arg: str = Field(min_length=1, max_length=1024)
    input_bam_arg: str = Field(min_length=1, max_length=1024)
    output_arg: str = Field(min_length=1, max_length=1024)
    reference_arg: str = Field(min_length=1, max_length=1024)
    log_arg: str = Field(min_length=1, max_length=1024)
    argv: tuple[str, ...] = Field(min_length=21)
    input_bam_sha256: Sha256
    raw_output_sha256: Sha256
    reference_fasta_sha256: Sha256
    reference_fai_sha256: Sha256
    transform_id: Literal["traceback.modkit-full-cmh.v1"] = (
        "traceback.modkit-full-cmh.v1"
    )
    alignment_prefilter: AlignmentPrefilterReceipt
    alignment_policy: Literal["primary-mapped-pass-qc-nonduplicate-mapq.v1"]
    inferred_policy: Literal["include_as_canonical", "exclude"]

    @model_validator(mode="after")
    def validate_argv(self) -> ModkitExecutionManifest:
        expected = (
            self.executable_arg,
            "extract",
            "full",
            self.input_bam_arg,
            self.output_arg,
            "--bgzf",
            "--reference",
            self.reference_arg,
            "--cpg",
            "--mapped-only",
            "--ignore-index",
            "--threads",
            "1",
            "--io-threads",
            "1",
            "--out-threads",
            "1",
            "--suppress-progress",
            "--log-filepath",
            self.log_arg,
            "--force",
        )
        if self.argv != expected:
            raise ValueError(
                "Modkit argv does not match its bound extraction command fields"
            )
        if self.input_bam_sha256 != self.alignment_prefilter.output_bam_sha256:
            raise ValueError("Modkit input digest does not match prefilter output")
        if self.input_bam_arg != self.alignment_prefilter.output_bam_arg:
            raise ValueError("Modkit input argument does not match prefilter output")
        return self


class ModkitAdapterLedger(StrictModel):
    """Native rows and grouped observations accounted before v2 ingestion."""

    raw_rows: int = Field(ge=0)
    excluded_non_c_rows: int = Field(ge=0)
    c_probability_rows: int = Field(ge=0)
    grouped_c_observations: int = Field(ge=0)
    inferred_observations: int = Field(ge=0)
    excluded_inferred_observations: int = Field(ge=0)
    explicit_observations: int = Field(ge=0)

    @model_validator(mode="after")
    def reconcile(self) -> ModkitAdapterLedger:
        if self.raw_rows != self.excluded_non_c_rows + self.c_probability_rows:
            raise ValueError("native Modkit rows must reconcile")
        if self.grouped_c_observations != (
            self.inferred_observations + self.explicit_observations
        ):
            raise ValueError("grouped C observations must reconcile")
        if self.excluded_inferred_observations > self.inferred_observations:
            raise ValueError("excluded inferred observations exceed inferred input")
        return self


class ModkitAdapterResult(StrictModel):
    """Research record returned by the pinned native adapter."""

    schema_version: Literal["traceback.modkit-adapter-result.v1"] = (
        "traceback.modkit-adapter-result.v1"
    )
    execution: ModkitExecutionManifest
    native_ledger: ModkitAdapterLedger
    normalized_output_sha256: Sha256
    ingestion: ModkitInputResultV2


class BoundFastaProvider:
    """Indexed FASTA provider bound to the exact FASTA and FAI bytes."""

    def __init__(
        self,
        fasta_path: Path,
        fai_path: Path,
        *,
        fasta_sha256: str,
        fai_sha256: str,
    ) -> None:
        self._fasta_path = fasta_path
        if _sha256_path(fasta_path) != fasta_sha256:
            raise CellOriginInputError("reference FASTA digest does not match receipt")
        if _sha256_path(fai_path) != fai_sha256:
            raise CellOriginInputError("reference FAI digest does not match receipt")
        self._index: dict[str, tuple[int, int, int, int]] = {}
        try:
            for line in fai_path.read_text(encoding="utf-8").splitlines():
                fields = line.split("\t")
                if len(fields) < 5 or fields[0] in self._index:
                    raise ValueError
                length, offset, line_bases, line_width = map(int, fields[1:5])
                self._index[fields[0]] = (
                    length,
                    offset,
                    line_bases,
                    line_width,
                )
        except (OSError, UnicodeError, ValueError) as exc:
            raise CellOriginInputError("reference FAI is malformed") from exc

    def __call__(self, contig: str, start0: int, end0: int) -> str:
        if contig not in self._index or start0 < 0 or end0 < start0:
            raise CellOriginInputError("reference interval is unavailable")
        length, offset, line_bases, line_width = self._index[contig]
        if end0 > length or line_bases <= 0 or line_width < line_bases:
            raise CellOriginInputError("reference interval is unavailable")
        chunks: list[bytes] = []
        position = start0
        with self._fasta_path.open("rb") as handle:
            while position < end0:
                line_offset = position % line_bases
                take = min(end0 - position, line_bases - line_offset)
                byte_offset = (
                    offset
                    + (position // line_bases) * line_width
                    + line_offset
                )
                handle.seek(byte_offset)
                chunk = handle.read(take)
                if len(chunk) != take:
                    raise CellOriginInputError("reference FASTA is truncated")
                chunks.append(chunk)
                position += take
        try:
            return b"".join(chunks).decode("ascii")
        except UnicodeDecodeError as exc:
            raise CellOriginInputError("reference FASTA sequence is not ASCII") from exc


def _open_bgzf_text(path: Path) -> io.TextIOWrapper:
    try:
        return io.TextIOWrapper(gzip.open(path, "rb"), encoding="utf-8", newline="")
    except OSError as exc:
        raise CellOriginInputError("Modkit output is not readable BGZF/gzip") from exc


def _required(row: dict[str, str], field: str, row_number: int) -> str:
    value = row.get(field)
    if value is None or not value or value != value.strip():
        raise CellOriginInputError(f"invalid {field} at row {row_number}")
    return value


def _integer(value: str, field: str, row_number: int) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise CellOriginInputError(f"invalid {field} at row {row_number}") from exc
    return parsed


def _probability(value: str, row_number: int) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise CellOriginInputError(f"invalid mod_qual at row {row_number}") from exc
    if not math.isfinite(parsed) or not 0.0 <= parsed <= 1.0:
        raise CellOriginInputError(f"invalid mod_qual at row {row_number}")
    return parsed


def _fragment_digest(read_id: str, salt: bytes) -> str:
    return hashlib.sha256(salt + b"\0" + read_id.encode("utf-8")).hexdigest()


def _canonical_position(position0: int, strand: Strand) -> int:
    if strand == Strand.MINUS:
        if position0 == 0:
            raise CellOriginInputError("minus-strand CpG coordinate underflows")
        return position0 - 1
    return position0


def _normalized_digest(
    calls: tuple[ModkitCpgCallV2, ...], ledger: ModkitIngestionLedgerV2
) -> str:
    payload = {
        "calls": [call.model_dump(mode="json") for call in calls],
        "ledger": ledger.model_dump(mode="json"),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def load_modkit_extract_full_064(
    source_path: Path,
    *,
    manifest: ModkitExecutionManifest,
    fasta_path: Path,
    fai_path: Path,
    fragment_hash_salt: bytes,
    probability_threshold: float,
    source_model_id: str,
    source_model_version: str,
    reference_id: str,
    max_rows: int = 1_000_000,
) -> ModkitAdapterResult:
    """Load one digest-bound Modkit 0.6.4 full extract.

    A temporary SQLite database provides bounded-memory grouping.  It contains
    only salted read digests, never source read identifiers.
    """

    if not isinstance(fragment_hash_salt, bytes) or not fragment_hash_salt:
        raise CellOriginInputError("fragment_hash_salt must be nonempty bytes")
    if isinstance(probability_threshold, bool) or not isinstance(
        probability_threshold, (int, float)
    ):
        raise CellOriginInputError("probability threshold must be numeric")
    threshold = float(probability_threshold)
    if not math.isfinite(threshold) or not 0.0 <= threshold <= 1.0:
        raise CellOriginInputError("probability threshold must be within [0, 1]")
    if _sha256_path(source_path) != manifest.raw_output_sha256:
        raise CellOriginInputError("Modkit output digest does not match receipt")
    provider = BoundFastaProvider(
        fasta_path,
        fai_path,
        fasta_sha256=manifest.reference_fasta_sha256,
        fai_sha256=manifest.reference_fai_sha256,
    )

    temp = tempfile.NamedTemporaryFile(
        prefix="traceback-modkit-",
        suffix=".sqlite3",
        delete=False,
    )
    temp_path = Path(temp.name)
    temp.close()
    raw_rows = 0
    non_c_rows = 0
    c_rows = 0
    try:
        database = sqlite3.connect(temp_path)
        database.execute(
            """CREATE TABLE observations (
                fragment_digest TEXT NOT NULL,
                forward_position INTEGER NOT NULL,
                invariant_json TEXT NOT NULL,
                m_probability REAL,
                h_probability REAL,
                PRIMARY KEY (fragment_digest, forward_position)
            )"""
        )
        try:
            with _open_bgzf_text(source_path) as handle:
                reader = csv.DictReader(handle, delimiter="\t")
                if tuple(reader.fieldnames or ()) != MODKIT_FULL_HEADER:
                    raise CellOriginInputError(
                        "Modkit extract full header does not match 0.6.4"
                    )
                for row_number, row in enumerate(reader, start=1):
                    raw_rows += 1
                    if raw_rows > max_rows:
                        raise CellOriginInputError("Modkit output exceeds row limit")
                    if None in row:
                        raise CellOriginInputError(
                            f"unexpected extra TSV fields at row {row_number}"
                        )
                    primary = _required(row, "modified_primary_base", row_number)
                    if primary != "C":
                        non_c_rows += 1
                        continue
                    c_rows += 1
                    canonical_base = _required(
                        row, "canonical_base", row_number
                    )
                    if canonical_base != "C":
                        raise CellOriginInputError(
                            f"C observation has non-C canonical base at row {row_number}"
                        )
                    flag = _integer(
                        _required(row, "flag", row_number),
                        "flag",
                        row_number,
                    )
                    if flag not in {0, 16}:
                        raise CellOriginInputError(
                            f"non-primary or filtered alignment at row {row_number}"
                        )
                    position0 = _integer(
                        _required(row, "ref_position", row_number),
                        "ref_position",
                        row_number,
                    )
                    if position0 < 0:
                        raise CellOriginInputError(
                            f"unmapped C observation at row {row_number}"
                        )
                    inferred_text = _required(row, "inferred", row_number)
                    if inferred_text not in {"true", "false"}:
                        raise CellOriginInputError(
                            f"invalid inferred at row {row_number}"
                        )
                    code = _required(row, "mod_code", row_number)
                    if code not in {"m", "h"}:
                        raise CellOriginInputError(
                            f"unsupported C modification code at row {row_number}"
                        )
                    strand_text = _required(row, "mod_strand", row_number)
                    ref_strand_text = _required(
                        row, "ref_strand", row_number
                    )
                    ref_mod_text = _required(row, "ref_mod_strand", row_number)
                    if any(
                        value not in {"+", "-"}
                        for value in (
                            strand_text,
                            ref_strand_text,
                            ref_mod_text,
                        )
                    ):
                        raise CellOriginInputError(
                            f"invalid strand at row {row_number}"
                        )
                    alignment_values = {
                        field: _integer(
                            _required(row, field, row_number),
                            field,
                            row_number,
                        )
                        for field in (
                            "fw_soft_clipped_start",
                            "fw_soft_clipped_end",
                            "alignment_start",
                            "alignment_end",
                            "read_length",
                        )
                    }
                    if (
                        any(value < 0 for value in alignment_values.values())
                        or alignment_values["read_length"] == 0
                        or alignment_values["alignment_end"]
                        < alignment_values["alignment_start"]
                    ):
                        raise CellOriginInputError(
                            f"invalid alignment identity at row {row_number}"
                        )
                    fragment = _fragment_digest(
                        _required(row, "read_id", row_number), fragment_hash_salt
                    )
                    forward_position = _integer(
                        _required(row, "forward_read_position", row_number),
                        "forward_read_position",
                        row_number,
                    )
                    invariant = json.dumps(
                        {
                            "chrom": _required(row, "chrom", row_number),
                            "position0": position0,
                            "mod_strand": strand_text,
                            "ref_strand": ref_strand_text,
                            "ref_mod_strand": ref_mod_text,
                            "inferred": inferred_text == "true",
                            "flag": flag,
                            **alignment_values,
                            "base_qual": _required(row, "base_qual", row_number),
                            "ref_kmer": _required(row, "ref_kmer", row_number),
                            "query_kmer": _required(
                                row, "query_kmer", row_number
                            ),
                            "canonical_base": canonical_base,
                            "modified_primary_base": primary,
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    existing = database.execute(
                        "SELECT invariant_json, m_probability, h_probability "
                        "FROM observations WHERE fragment_digest=? AND forward_position=?",
                        (fragment, forward_position),
                    ).fetchone()
                    probability = _probability(
                        _required(row, "mod_qual", row_number), row_number
                    )
                    if existing is None:
                        database.execute(
                            "INSERT INTO observations VALUES (?, ?, ?, ?, ?)",
                            (
                                fragment,
                                forward_position,
                                invariant,
                                probability if code == "m" else None,
                                probability if code == "h" else None,
                            ),
                        )
                    else:
                        old_invariant, old_m, old_h = existing
                        if old_invariant != invariant:
                            raise CellOriginInputError(
                                f"inconsistent paired probability rows at row {row_number}"
                            )
                        if (code == "m" and old_m is not None) or (
                            code == "h" and old_h is not None
                        ):
                            raise CellOriginInputError(
                                f"duplicate modification probability at row {row_number}"
                            )
                        database.execute(
                            "UPDATE observations SET "
                            + ("m_probability=?" if code == "m" else "h_probability=?")
                            + " WHERE fragment_digest=? AND forward_position=?",
                            (probability, fragment, forward_position),
                        )
            database.commit()

            grouped = database.execute(
                "SELECT COUNT(*) FROM observations"
            ).fetchone()[0]
            missing = database.execute(
                "SELECT COUNT(*) FROM observations "
                "WHERE m_probability IS NULL OR h_probability IS NULL"
            ).fetchone()[0]
            if missing:
                raise CellOriginInputError(
                    "Modkit input is not complete paired m+h probability data"
                )

            calls: list[ModkitCpgCallV2] = []
            inferred_count = 0
            excluded_inferred = 0
            tie_count = 0
            low_count = 0
            explicit_count = 0
            plus_count = 0
            unmethylated_count = 0
            for fragment, _, invariant_json, p_m, p_h in database.execute(
                "SELECT fragment_digest, forward_position, invariant_json, "
                "m_probability, h_probability FROM observations "
                "ORDER BY fragment_digest, forward_position"
            ):
                invariant = json.loads(invariant_json)
                inferred = invariant["inferred"]
                if inferred:
                    inferred_count += 1
                    if p_m != 0.0 or p_h != 0.0:
                        raise CellOriginInputError(
                            "inferred canonical observation has nonzero "
                            "modification probability"
                        )
                    if manifest.inferred_policy == "exclude":
                        excluded_inferred += 1
                        continue
                else:
                    explicit_count += 1
                p_c = 1.0 - p_m - p_h
                if p_c < -1e-6 or p_c > 1.0 + 1e-6:
                    raise CellOriginInputError(
                        "C/m/h probabilities do not sum within tolerance"
                    )
                p_c = min(1.0, max(0.0, p_c))
                combined = p_m + p_h
                difference = combined - p_c
                if difference == 0.0:
                    tie_count += 1
                    continue
                selected = max(p_c, combined)
                if selected <= 0.5 or selected < threshold:
                    low_count += 1
                    continue
                ref_mod_strand = Strand(invariant["ref_mod_strand"])
                canonical_position = _canonical_position(
                    invariant["position0"], ref_mod_strand
                )
                if provider(
                    invariant["chrom"], canonical_position, canonical_position + 2
                ).upper() != "CG":
                    raise CellOriginInputError("reference context is not a CpG dyad")
                state = (
                    CpgCallState.METHYLATED
                    if difference > 0.0
                    else CpgCallState.UNMETHYLATED
                )
                call = ModkitCpgCallV2(
                    fragment_digest=fragment,
                    chromosome=invariant["chrom"],
                    original_position0=invariant["position0"],
                    canonical_cpg_position0=canonical_position,
                    modification_strand=Strand(invariant["mod_strand"]),
                    reference_mod_strand=ref_mod_strand,
                    selected_state_probability=selected,
                    state=state,
                    policy=ModProbabilityPolicy.PRECALL_COMBINED_M_H,
                )
                calls.append(call)
                plus_count += ref_mod_strand == Strand.PLUS
                unmethylated_count += state == CpgCallState.UNMETHYLATED
        finally:
            database.close()
    except (OSError, EOFError, UnicodeError, csv.Error):
        raise CellOriginInputError(
            "Modkit output decompression or text decoding failed"
        ) from None
    except (sqlite3.Error, ValidationError) as exc:
        raise CellOriginInputError("native Modkit ingestion failed validation") from exc
    finally:
        os.unlink(temp_path)

    candidate_count = grouped - excluded_inferred
    ledger = ModkitIngestionLedgerV2(
        policy=ModProbabilityPolicy.PRECALL_COMBINED_M_H,
        total_rows=candidate_count,
        source_failed_rows=0,
        source_passed_rows=candidate_count,
        excluded_non_c_rows=0,
        candidate_c_rows=candidate_count,
        hard_call_c_rows=0,
        hard_call_m_rows=0,
        hard_call_h_rows=0,
        probability_input_rows=candidate_count,
        excluded_probability_tie_rows=tie_count,
        excluded_low_confidence_rows=low_count,
        eligible_call_rows=len(calls),
        unmethylated_call_rows=unmethylated_count,
        methylated_call_rows=len(calls) - unmethylated_count,
        reference_plus_call_rows=plus_count,
        reference_minus_call_rows=len(calls) - plus_count,
    )
    provenance = ModkitInputProvenanceV2(
        source_schema_id=ModkitSourceSchema.MODKIT_EXTRACT_FULL_064,
        source_schema_version=MODKIT_VERSION,
        source_tool_id="nanoporetech.modkit",
        source_tool_version=MODKIT_VERSION,
        source_model_id=source_model_id,
        source_model_version=source_model_version,
        policy=ModProbabilityPolicy.PRECALL_COMBINED_M_H,
        probability_threshold=threshold,
        probability_threshold_source="adapter_explicit",
        reference_id=reference_id,
        reference_sha256=manifest.reference_fasta_sha256,
        reference_context_provider_id="traceback.bound-fasta-fai.v1",
    )
    ingestion = ModkitInputResultV2(
        provenance=provenance,
        ledger=ledger,
        calls=tuple(calls),
    )
    native_ledger = ModkitAdapterLedger(
        raw_rows=raw_rows,
        excluded_non_c_rows=non_c_rows,
        c_probability_rows=c_rows,
        grouped_c_observations=grouped,
        inferred_observations=inferred_count,
        excluded_inferred_observations=excluded_inferred,
        explicit_observations=explicit_count,
    )
    return ModkitAdapterResult(
        execution=manifest,
        native_ledger=native_ledger,
        normalized_output_sha256=_normalized_digest(ingestion.calls, ledger),
        ingestion=ingestion,
    )


__all__ = [
    "AlignmentExclusionLedger",
    "AlignmentPrefilterReceipt",
    "BoundFastaProvider",
    "MODKIT_EXECUTABLE_SHA256",
    "MODKIT_FULL_HEADER",
    "MODKIT_LICENSE_SHA256",
    "ModkitAdapterLedger",
    "ModkitAdapterResult",
    "ModkitExecutionManifest",
    "load_modkit_extract_full_064",
]
