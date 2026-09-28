from __future__ import annotations

import gzip
import hashlib
from pathlib import Path

import pytest

from evidence_inspector.cell_origin_inputs import CellOriginInputError
from evidence_inspector.cell_origin_models import CpgCallState, ModkitSourceSchema
from evidence_inspector.modkit_adapter import (
    AlignmentExclusionLedger,
    MODKIT_EXECUTABLE_SHA256,
    MODKIT_FULL_HEADER,
    MODKIT_LICENSE_SHA256,
    ModkitExecutionManifest,
    load_modkit_extract_full_064,
)
from scripts.adapt_modkit_064 import main as adapter_main


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _reference(tmp_path: Path) -> tuple[Path, Path]:
    fasta = tmp_path / "synthetic.fa"
    fai = tmp_path / "synthetic.fa.fai"
    fasta.write_bytes(b">chr1\nACGTCGAA\n")
    fai.write_text("chr1\t8\t6\t8\t9\n", encoding="utf-8")
    return fasta, fai


def _row(
    *,
    read_id: str,
    forward_position: int,
    ref_position: int,
    probability: float,
    code: str,
    inferred: bool = False,
) -> str:
    values = {
        "read_id": read_id,
        "forward_read_position": str(forward_position),
        "ref_position": str(ref_position),
        "chrom": "chr1",
        "mod_strand": "+",
        "ref_strand": "+",
        "ref_mod_strand": "+",
        "fw_soft_clipped_start": "0",
        "fw_soft_clipped_end": "0",
        "alignment_start": "0",
        "alignment_end": "8",
        "read_length": "8",
        "mod_qual": str(probability),
        "mod_code": code,
        "base_qual": "30",
        "ref_kmer": "ACGTC",
        "query_kmer": "ACGTC",
        "canonical_base": "C",
        "modified_primary_base": "C",
        "inferred": str(inferred).lower(),
        "flag": "0",
    }
    return "\t".join(values[column] for column in MODKIT_FULL_HEADER)


def _source(tmp_path: Path, rows: list[str]) -> Path:
    path = tmp_path / "full.tsv.bgz"
    content = "\t".join(MODKIT_FULL_HEADER) + "\n" + "\n".join(rows) + "\n"
    with gzip.GzipFile(filename=str(path), mode="wb", mtime=0) as handle:
        handle.write(content.encode())
    return path


def _manifest(
    source: Path,
    fasta: Path,
    fai: Path,
    *,
    inferred_policy: str = "include_as_canonical",
) -> ModkitExecutionManifest:
    return ModkitExecutionManifest(
        source_complete=True,
        source_selection="invented two-observation synthetic fixture",
        modkit_version="0.6.4",
        modkit_executable_sha256=MODKIT_EXECUTABLE_SHA256,
        modkit_license_sha256=MODKIT_LICENSE_SHA256,
        container_image_digest="sha256:" + "1" * 64,
        argv=(
            "modkit",
            "extract",
            "full",
            "synthetic.bam",
            "full.tsv.bgz",
            "--bgzf",
            "--reference",
            "synthetic.fa",
            "--cpg",
            "--mapped-only",
            "--ignore-index",
        ),
        input_bam_sha256="2" * 64,
        raw_output_sha256=_digest(source),
        reference_fasta_sha256=_digest(fasta),
        reference_fai_sha256=_digest(fai),
        minimum_mapq=0,
        alignment_policy="primary-mapped-pass-qc-nonduplicate-mapq.v1",
        alignment_ledger=AlignmentExclusionLedger(
            input_records=1,
            excluded_unmapped=0,
            excluded_secondary=0,
            excluded_supplementary=0,
            excluded_qc_fail=0,
            excluded_duplicate=0,
            excluded_below_mapq=0,
            accepted_primary_records=1,
        ),
        inferred_policy=inferred_policy,
    )


def _load(
    source: Path,
    fasta: Path,
    fai: Path,
    manifest: ModkitExecutionManifest,
):
    return load_modkit_extract_full_064(
        source,
        manifest=manifest,
        fasta_path=fasta,
        fai_path=fai,
        fragment_hash_salt=b"private-test-salt",
        probability_threshold=0.6,
        source_model_id="invented.synthetic.cmh",
        source_model_version="1",
        reference_id="invented-reference",
    )


def test_native_adapter_combines_m_h_and_declares_inferred_policy(
    tmp_path: Path,
) -> None:
    fasta, fai = _reference(tmp_path)
    source = _source(
        tmp_path,
        [
            _row(
                read_id="invented-read",
                forward_position=1,
                ref_position=1,
                probability=0.4,
                code="m",
            ),
            _row(
                read_id="invented-read",
                forward_position=1,
                ref_position=1,
                probability=0.3,
                code="h",
            ),
            _row(
                read_id="invented-read",
                forward_position=4,
                ref_position=4,
                probability=0.0,
                code="m",
                inferred=True,
            ),
            _row(
                read_id="invented-read",
                forward_position=4,
                ref_position=4,
                probability=0.0,
                code="h",
                inferred=True,
            ),
        ],
    )

    included = _load(source, fasta, fai, _manifest(source, fasta, fai))
    excluded = _load(
        source,
        fasta,
        fai,
        _manifest(source, fasta, fai, inferred_policy="exclude"),
    )

    assert included.ingestion.provenance.source_schema_id == (
        ModkitSourceSchema.MODKIT_EXTRACT_FULL_064
    )
    assert [call.state for call in included.ingestion.calls] == [
        CpgCallState.METHYLATED,
        CpgCallState.UNMETHYLATED,
    ]
    assert included.ingestion.calls[0].selected_state_probability == pytest.approx(0.7)
    assert included.native_ledger.inferred_observations == 1
    assert excluded.native_ledger.excluded_inferred_observations == 1
    assert len(excluded.ingestion.calls) == 1
    assert "invented-read" not in included.model_dump_json()


def test_native_adapter_rejects_single_modification_input(tmp_path: Path) -> None:
    fasta, fai = _reference(tmp_path)
    source = _source(
        tmp_path,
        [
            _row(
                read_id="invented-read",
                forward_position=1,
                ref_position=1,
                probability=0.8,
                code="m",
            )
        ],
    )

    with pytest.raises(CellOriginInputError, match=r"complete paired m\+h"):
        _load(source, fasta, fai, _manifest(source, fasta, fai))


def test_native_adapter_binds_actual_fasta_and_fai_bytes(tmp_path: Path) -> None:
    fasta, fai = _reference(tmp_path)
    source = _source(
        tmp_path,
        [
            _row(
                read_id="invented-read",
                forward_position=1,
                ref_position=1,
                probability=0.4,
                code="m",
            ),
            _row(
                read_id="invented-read",
                forward_position=1,
                ref_position=1,
                probability=0.3,
                code="h",
            ),
        ],
    )
    manifest = _manifest(source, fasta, fai)
    fasta.write_bytes(b">chr1\nATGTCGAA\n")

    with pytest.raises(CellOriginInputError, match="FASTA digest"):
        _load(source, fasta, fai, manifest)


def test_execution_manifest_requires_alignment_exclusion_reconciliation() -> None:
    with pytest.raises(ValueError, match="alignment exclusion ledger"):
        AlignmentExclusionLedger(
            input_records=2,
            excluded_unmapped=0,
            excluded_secondary=0,
            excluded_supplementary=0,
            excluded_qc_fail=0,
            excluded_duplicate=0,
            excluded_below_mapq=0,
            accepted_primary_records=1,
        )


def test_development_entry_point_requires_private_hash_salt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("TRACEBACK_FRAGMENT_HASH_SALT", raising=False)
    assert adapter_main(
        [
            "--methylation-input-mode",
            "modkit-0.6.4-full-cmh-v2",
            "--extract-full",
            str(tmp_path / "input.bgz"),
            "--execution-manifest",
            str(tmp_path / "manifest.json"),
            "--reference-fasta",
            str(tmp_path / "reference.fa"),
            "--reference-fai",
            str(tmp_path / "reference.fa.fai"),
            "--reference-id",
            "invented-reference",
            "--source-model-id",
            "invented-model",
            "--source-model-version",
            "1",
            "--combined-call-threshold",
            "0.7",
            "--output",
            str(tmp_path / "output.json"),
        ]
    ) == 2
