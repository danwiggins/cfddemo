"""Shared synthetic fixtures for the Lab tests: a seeded reference and uBAM chunks."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from traceback_lab.synthetic import write_chunks, write_reference
from traceback_runner.references import register_reference


@pytest.fixture(scope="session")
def synthetic_world(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    base = tmp_path_factory.mktemp("lab-world")
    reference = write_reference(base / "ref.fa", length=30_000)
    write_chunks(base / "chunks", reference, chunks=3, reads_per_chunk=600)
    return {"base": base, "fasta": reference.fasta, "chunks": base / "chunks"}


@pytest.fixture
def lab_world(tmp_path: Path, synthetic_world: dict[str, Path]) -> dict[str, Path]:
    """A fresh ROOT with the synthetic reference registered and a private chunk copy."""

    root = tmp_path / "root"
    register_reference(root, synthetic_world["fasta"], "synth")
    chunks = tmp_path / "chunks"
    shutil.copytree(synthetic_world["chunks"], chunks)
    return {"root": root, "chunks": chunks, "fasta": synthetic_world["fasta"], "tmp": tmp_path}


@pytest.fixture(scope="session")
def shared_mmi(tmp_path_factory: pytest.TempPathFactory, synthetic_world: dict[str, Path]) -> Path:
    """One minimap2 index for the session (passed with --mmi to save time)."""

    import subprocess

    path = tmp_path_factory.mktemp("mmi") / "ref.mmi"
    minimap2 = shutil.which("minimap2")
    assert minimap2, "minimap2 is required for the Lab integration tier"
    subprocess.run(
        [minimap2, "-x", "map-ont", "-d", str(path), str(synthetic_world["fasta"])],
        check=True,
        capture_output=True,
    )
    return path
