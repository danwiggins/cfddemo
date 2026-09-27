"""Fail-closed rendering for the shared compatibility manifest."""

from __future__ import annotations

from datetime import date

from .contracts import CompatibilityItem, CompatibilityManifest


def render_protocol(manifest: CompatibilityManifest) -> list[dict[str, str]]:
    """Render shared manifest items without exposing withheld instructions."""

    rendered: list[dict[str, str]] = []
    for item in manifest.items:
        withheld = (
            item.instruction_kind == "wet_lab_instruction"
            or item.rendering == "withhold"
        )
        rendered.append(
            {
                "item_id": item.item_id,
                "category": item.category,
                "display_name": item.display_name,
                "status": item.status,
                "instruction_kind": item.instruction_kind,
                "approval_state": item.approval_state.value,
                "content": (
                    "Instruction withheld pending scientific approval"
                    if withheld
                    else item.description
                ),
            }
        )
    return rendered


def synthetic_protocol_manifest() -> CompatibilityManifest:
    """Return documentation-safe content bound to the synthetic release."""

    return CompatibilityManifest(
        workflow_release_id="synthetic-development-v1",
        items=(
            CompatibilityItem(
                category="scope",
                item_id="research-use",
                display_name="Scope",
                description=(
                    "Synthetic development workflow only; no hardware, scientific, "
                    "clinical, or protocol qualification is implied"
                ),
                status="required",
            ),
            CompatibilityItem(
                category="retention",
                item_id="local-boundary",
                display_name="Local data boundary",
                description="Keep raw genomic files in the configured local data root",
                status="required",
            ),
            CompatibilityItem(
                category="wet-lab",
                item_id="collection-sop",
                display_name="Collection and preparation SOP",
                description="Unapproved wet-lab instruction text must not render",
                status="required",
                instruction_kind="wet_lab_instruction",
                rendering="withhold",
                protocol_version="synthetic-protocol-v0",
                owner="Scientific owner pending approval",
                source="Protocol source pending approval",
                source_version="unapproved-v0",
                last_reviewed=date(2026, 9, 26),
            ),
        ),
    )


__all__ = ["render_protocol", "synthetic_protocol_manifest"]
