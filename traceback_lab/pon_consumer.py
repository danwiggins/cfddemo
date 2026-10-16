"""Bind one selected panel to copy-number's per-run execution contract.

The cohort layer chooses and verifies a PoN. This module only translates that
selection to a Lab CN parameter identity; the worker's execution path must
consume the same selected bytes before an attempt can be published.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from typing import Protocol

from .adapters import ichor
from .registry import PlanItem


class SelectedPon(Protocol):
    pon_policy: str
    pon_sha256: str


def bind_copy_number(items: Sequence[PlanItem], selection: SelectedPon) -> list[PlanItem]:
    """Use the selected PoN digest in copy-number's D19 execution identity."""

    if selection.pon_policy != "lab_loo_v1":
        raise ValueError("a selected PoN must use lab_loo_v1")
    parameters = replace(
        ichor.default_parameters(), pon_policy="lab_loo_v1", pon_sha256=selection.pon_sha256
    ).contract()
    return [
        PlanItem(replace(item.algorithm, parameters=parameters), item.upstream_only)
        if item.algorithm.id == "copy-number" else item
        for item in items
    ]
