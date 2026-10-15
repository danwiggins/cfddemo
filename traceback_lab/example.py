"""``lab run --example``: a seeded synthetic walkthrough generated at runtime (D36).

Nothing is stored in the repo.  A ~6 Mb reference with ``chr1``…``chr22``,
``chrX``, ``chrY`` and ``chrM`` is registered under ``lab-example`` in ROOT,
and three unaligned chunks (~200k reads) carry a planted size mode near
167 bp, MM/ML 5mC calls and chrY reads.  The run goes through the real
ingest, alignment, read summary, fast algorithms, downsample and split
halves.  Copy-number needs hg38-scale assets, so it is
``skipped(example_reference)`` and the SYNTHETIC control says why.
"""

from __future__ import annotations

import json
from pathlib import Path

from traceback_runner.references import list_reference_ids, load_reference, register_reference

from .ingest import EXAMPLE_REFERENCE_ID
from .problems import LabProblem
from .synthetic import write_chunks, write_reference

SEED = 20261015
CONTIG_LENGTH = 240_000
READS_PER_CHUNK = 66_000
CHUNKS = 3
STEM = "EXAMPLE0001_pass_synthetic"
MARKER = "traceback.lab.example.v1"


def example_dir(root: Path) -> Path:
    return root / "lab" / "example"


def prepare(root: Path, *, reads_per_chunk: int = READS_PER_CHUNK) -> tuple[Path, str]:
    """Write (once) the example reference and chunks under ROOT; return (chunk dir, reference id)."""

    base = example_dir(root)
    chunks = base / "chunks"
    marker = base / "example.json"
    expected = {"schema": MARKER, "seed": SEED, "contig_length": CONTIG_LENGTH, "reads_per_chunk": reads_per_chunk, "chunks": CHUNKS}
    current = None
    try:
        current = json.loads(marker.read_text())
    except (FileNotFoundError, ValueError):
        pass
    fasta = base / "reference.fa"
    if current != expected or not fasta.is_file() or len(list(chunks.glob("*.bam"))) != CHUNKS:
        base.mkdir(parents=True, exist_ok=True)
        reference = write_reference(fasta, seed=SEED, length=CONTIG_LENGTH)
        for old in chunks.glob("*.bam"):
            old.unlink()
        write_chunks(
            chunks,
            reference,
            chunks=CHUNKS,
            reads_per_chunk=reads_per_chunk,
            seed=SEED + 1,
            stem=STEM,
            chry_weight=1.0,
        )
        marker.write_text(json.dumps(expected, sort_keys=True))
    if EXAMPLE_REFERENCE_ID not in list_reference_ids(root):
        register_reference(root, fasta, EXAMPLE_REFERENCE_ID)
    registered = load_reference(root, EXAMPLE_REFERENCE_ID).registered.asset_sha256
    if registered != _sha256(fasta):
        raise LabProblem(
            "TBX-LAB-004",
            cause=f"another reference is registered as {EXAMPLE_REFERENCE_ID!r} under this ROOT",
            fix="Run the example with a different --root; never register a real reference as lab-example",
            data={"registered": [EXAMPLE_REFERENCE_ID]},
        )
    return chunks, EXAMPLE_REFERENCE_ID


def prepared(root: Path) -> bool:
    """True when the example chunks already exist (a dry run never writes)."""

    return (example_dir(root) / "example.json").is_file() and len(list((example_dir(root) / "chunks").glob("*.bam"))) == CHUNKS


def _sha256(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1 << 20):
            digest.update(block)
    return digest.hexdigest()


__all__ = ["CHUNKS", "READS_PER_CHUNK", "SEED", "example_dir", "prepare", "prepared"]
