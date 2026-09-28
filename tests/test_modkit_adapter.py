from __future__ import annotations

import gzip
import hashlib
from pathlib import Path

import pytest

from evidence_inspector.cell_origin_inputs import CellOriginInputError
from evidence_inspector.cell_origin_models import CpgCallState, ModkitSourceSchema
from evidence_inspector.modkit_adapter import (
    AlignmentExclusionLedger,
    AlignmentPrefilterReceipt,
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
    **overrides: str,
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
    values.update(overrides)
    return "\t".join(values[column] for column in MODKIT_FULL_HEADER)


def _source(tmp_path: Path, rows: list[str]) -> Path:
    path = tmp_path / "full.tsv.bgz"
    content = "\t".join(MODKIT_FULL_HEADER) + "\n" + "\n".join(rows) + "\n"
    with gzip.GzipFile(filename=str(path), mode="wb", mtime=0) as handle:
        handle.write(content.encode())
    return path


def _paired_rows(**h_overrides: str) -> list[str]:
    return [
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
            **h_overrides,
        ),
    ]


def _manifest(
    source: Path,
    fasta: Path,
    fai: Path,
    *,
    inferred_policy: str = "include_as_canonical",
) -> ModkitExecutionManifest:
    prefilter = AlignmentPrefilterReceipt(
        samtools_version="1.20",
        samtools_executable_sha256="3" * 64,
        executable_arg="samtools",
        source_bam_arg="source.bam",
        output_bam_arg="synthetic.filtered.bam",
        source_bam_sha256="4" * 64,
        output_bam_sha256="2" * 64,
        minimum_mapq=20,
        argv=(
            "samtools",
            "view",
            "-b",
            "-F",
            "3844",
            "-q",
            "20",
            "-o",
            "synthetic.filtered.bam",
            "source.bam",
        ),
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
    )
    return ModkitExecutionManifest(
        source_complete=True,
        source_selection="invented two-observation synthetic fixture",
        modkit_version="0.6.4",
        modkit_executable_sha256=MODKIT_EXECUTABLE_SHA256,
        modkit_license_sha256=MODKIT_LICENSE_SHA256,
        container_image_digest="sha256:" + "1" * 64,
        executable_arg="modkit",
        input_bam_arg="synthetic.filtered.bam",
        output_arg="full.tsv.bgz",
        reference_arg="synthetic.fa",
        log_arg="full.log",
        argv=(
            "modkit",
            "extract",
            "full",
            "synthetic.filtered.bam",
            "full.tsv.bgz",
            "--bgzf",
            "--reference",
            "synthetic.fa",
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
            "full.log",
            "--force",
        ),
        input_bam_sha256="2" * 64,
        raw_output_sha256=_digest(source),
        reference_fasta_sha256=_digest(fasta),
        reference_fai_sha256=_digest(fai),
        alignment_prefilter=prefilter,
        alignment_policy="primary-mapped-pass-qc-nonduplicate-mapq.v1",
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


@pytest.mark.parametrize(
    "mutation",
    (
        "reordered",
        "input_value",
        "output_value",
        "reference_value",
        "duplicate_flag",
        "forbidden_flag",
        "missing_threads",
    ),
)
def test_execution_manifest_rejects_noncanonical_modkit_argv(
    tmp_path: Path, mutation: str
) -> None:
    fasta, fai = _reference(tmp_path)
    source = _source(tmp_path, _paired_rows())
    payload = _manifest(source, fasta, fai).model_dump()
    argv = list(payload["argv"])
    if mutation == "reordered":
        left = argv.index("--cpg")
        right = argv.index("--mapped-only")
        argv[left], argv[right] = argv[right], argv[left]
    elif mutation == "input_value":
        argv[3] = "other.bam"
    elif mutation == "output_value":
        argv[4] = "other.bgz"
    elif mutation == "reference_value":
        argv[argv.index("--reference") + 1] = "other.fa"
    elif mutation == "duplicate_flag":
        argv.append("--cpg")
    elif mutation == "forbidden_flag":
        argv.append("--allow-non-primary")
    else:
        position = argv.index("--threads")
        del argv[position : position + 2]
    payload["argv"] = tuple(argv)

    with pytest.raises(ValueError, match="bound extraction command|at least 21"):
        ModkitExecutionManifest.model_validate(payload)


def test_prefilter_receipt_rejects_minimum_mapq_mismatch(
    tmp_path: Path,
) -> None:
    fasta, fai = _reference(tmp_path)
    source = _source(tmp_path, _paired_rows())
    payload = _manifest(source, fasta, fai).model_dump()
    payload["alignment_prefilter"]["minimum_mapq"] = 30

    with pytest.raises(ValueError, match="prefilter argv"):
        ModkitExecutionManifest.model_validate(payload)


@pytest.mark.parametrize("mismatch", ("argument", "digest"))
def test_execution_manifest_binds_prefilter_output(
    tmp_path: Path, mismatch: str
) -> None:
    fasta, fai = _reference(tmp_path)
    source = _source(tmp_path, _paired_rows())
    payload = _manifest(source, fasta, fai).model_dump()
    if mismatch == "argument":
        payload["input_bam_arg"] = "different.filtered.bam"
        argv = list(payload["argv"])
        argv[3] = "different.filtered.bam"
        payload["argv"] = tuple(argv)
    else:
        payload["input_bam_sha256"] = "5" * 64

    with pytest.raises(ValueError, match="does not match prefilter output"):
        ModkitExecutionManifest.model_validate(payload)


def test_native_adapter_rejects_non_c_canonical_base(tmp_path: Path) -> None:
    fasta, fai = _reference(tmp_path)
    source = _source(
        tmp_path,
        _paired_rows(canonical_base="A"),
    )

    with pytest.raises(CellOriginInputError, match="non-C canonical base"):
        _load(source, fasta, fai, _manifest(source, fasta, fai))


def test_native_adapter_rejects_pair_invariant_mismatch(tmp_path: Path) -> None:
    fasta, fai = _reference(tmp_path)
    source = _source(tmp_path, _paired_rows(ref_strand="-"))

    with pytest.raises(CellOriginInputError, match="inconsistent paired"):
        _load(source, fasta, fai, _manifest(source, fasta, fai))


def test_official_modkit_064_cpg_header_golden() -> None:
    """Golden captured from the digest-pinned release executable with --cpg.

    In 0.6.4, `--cpg` filters by the implicit CpG motif but does not populate
    the `motif` argument used by the writer's `with_motifs` switch. Therefore
    the executable emits 21 fields; `motifs` is conditional on explicit
    `--motif`, not this pinned command.
    """

    path = Path("tests/fixtures/modkit-0.6.4-extract-full-cpg.header.tsv")
    assert _digest(path) == (
        "f8c35e1b8463105627bb82e8d3db07924b532f7a877f14e3630f4f68ca9c9967"
    )
    assert tuple(path.read_text(encoding="utf-8").rstrip("\n").split("\t")) == (
        MODKIT_FULL_HEADER
    )
    assert len(MODKIT_FULL_HEADER) == 21
    assert "motifs" not in MODKIT_FULL_HEADER


@pytest.mark.parametrize("corruption", ("not_gzip", "truncated", "invalid_utf8"))
def test_native_adapter_sanitizes_compressed_text_failures(
    tmp_path: Path, corruption: str
) -> None:
    fasta, fai = _reference(tmp_path)
    source = _source(tmp_path, _paired_rows())
    if corruption == "not_gzip":
        source.write_bytes(b"not a gzip stream")
    elif corruption == "truncated":
        source.write_bytes(source.read_bytes()[:-8])
    else:
        with gzip.GzipFile(filename=str(source), mode="wb", mtime=0) as handle:
            handle.write("\t".join(MODKIT_FULL_HEADER).encode() + b"\n\xff\n")

    with pytest.raises(
        CellOriginInputError,
        match="decompression or text decoding",
    ):
        _load(source, fasta, fai, _manifest(source, fasta, fai))


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
