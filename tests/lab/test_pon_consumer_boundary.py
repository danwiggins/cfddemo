"""PoN bytes and parameter invariants before cohort binding is wired."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from traceback_lab.adapters.ichor import CopyNumberError, LabCopyNumberParameters, stage_verified_pon


def test_pon_contract_requires_policy_and_digest_together() -> None:
    digest = "a" * 64
    assert LabCopyNumberParameters(signed=object()).pon_policy == "none"
    assert LabCopyNumberParameters(signed=object(), pon_policy="lab_loo_v1", pon_sha256=digest).pon_sha256 == digest
    with pytest.raises(ValueError, match="no-PoN"):
        LabCopyNumberParameters(signed=object(), pon_sha256=digest)
    for bad in (None, "A" * 64, "short"):
        with pytest.raises(ValueError, match="requires a lowercase"):
            LabCopyNumberParameters(signed=object(), pon_policy="lab_loo_v1", pon_sha256=bad)


def test_pon_staging_verifies_exact_bytes(tmp_path: Path) -> None:
    source = tmp_path / "selected.rds"
    source.write_bytes(b"synthetic-pon-v1")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    destination = tmp_path / "scratch" / "pon.rds"
    destination.parent.mkdir()
    stage_verified_pon(source, destination, digest)
    assert destination.read_bytes() == source.read_bytes()

    source.write_bytes(b"mutated-after-selection")
    with pytest.raises(CopyNumberError, match="digest verification"):
        stage_verified_pon(source, destination, digest)


def test_pon_staging_refuses_missing_and_linked_source(tmp_path: Path) -> None:
    destination = tmp_path / "staged.rds"
    with pytest.raises(CopyNumberError, match="not a regular file"):
        stage_verified_pon(tmp_path / "absent.rds", destination, "a" * 64)
    source = tmp_path / "source.rds"
    source.write_bytes(b"synthetic")
    link = tmp_path / "link.rds"
    link.symlink_to(source)
    with pytest.raises(CopyNumberError, match="not a regular file"):
        stage_verified_pon(link, destination, hashlib.sha256(source.read_bytes()).hexdigest())
