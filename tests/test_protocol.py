"""Protocol rendering tests."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from traceback_runner.contracts import CompatibilityItem
from traceback_runner.protocol import render_protocol, synthetic_protocol_manifest


def test_unapproved_wet_lab_instruction_is_never_rendered() -> None:
    manifest = synthetic_protocol_manifest()
    rendered = render_protocol(manifest)
    wet_lab = next(row for row in rendered if row["category"] == "wet-lab")

    assert wet_lab["approval_state"] == "unapproved_synthetic"
    assert wet_lab["content"] == "Instruction withheld pending scientific approval"


def test_unapproved_wet_lab_row_must_be_withheld() -> None:
    with pytest.raises(ValidationError, match="must be withheld"):
        CompatibilityItem(
            category="wet-lab",
            item_id="unsafe-row",
            display_name="Unsafe incomplete instruction",
            description="Perform an exact wet-lab action",
            status="required",
            instruction_kind="wet_lab_instruction",
            rendering="display",
        )


def test_synthetic_manifest_states_nonqualification() -> None:
    text = " ".join(
        row["content"] for row in render_protocol(synthetic_protocol_manifest())
    )

    assert "Synthetic development workflow only" in text
    assert "qualification is implied" in text
