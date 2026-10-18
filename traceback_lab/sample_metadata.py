"""Versioned operator assertions kept separate from measured Lab results."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from .problems import LabProblem

SCHEMA = "traceback.lab.sample-metadata.v1"


def _utc_second(value: Any) -> bool:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:Z|\+00:00)", value):
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() == UTC.utcoffset(parsed)


def sex_assertion(record: dict[str, Any]) -> dict[str, str] | None:
    """Read a local assertion, refusing malformed metadata instead of ignoring it."""

    value = record.get("operator_metadata")
    if value is None:
        return None
    if (
        not isinstance(value, dict)
        or set(value) != {"schema", "sex", "recorded_utc"}
        or value.get("schema") != SCHEMA
        or value.get("sex") not in ("M", "F")
        or not _utc_second(value.get("recorded_utc"))
    ):
        raise LabProblem(
            "TBX-LAB-023",
            cause="stored sample sex metadata is malformed",
            fix="Restore the local sample record from a known-good backup before setting --sex again; reruns without --sex may continue",
        )
    return value


def with_sex_assertion(record: dict[str, Any], requested: str | None) -> dict[str, Any]:
    """Add one assertion without changing a prior one or any measurement key."""

    if requested is None:
        return record
    existing = sex_assertion(record)
    if requested not in ("M", "F"):
        raise ValueError("sex assertion must be M or F")
    if existing is not None:
        if existing["sex"] != requested:
            raise LabProblem(
                "TBX-LAB-023",
                cause=f"this sample already records --sex {existing['sex']}; --sex {requested} conflicts",
                fix=f"Rerun with --sex {existing['sex']} or omit --sex; correct the local assertion only after review",
                data={"recorded_sex": existing["sex"], "requested_sex": requested},
            )
        return record
    return {
        **record,
        "operator_metadata": {
            "schema": SCHEMA,
            "sex": requested,
            "recorded_utc": datetime.now(UTC).isoformat(timespec="seconds"),
        },
    }


def sex_concordance(declared: str | None, observed: str | None) -> dict[str, str | None]:
    """Classify a definite call at read time; no assertion or call means no verdict."""

    if declared not in ("M", "F"):
        return {"state": "no_verdict", "reason": "no_assertion", "declared": None, "observed": observed}
    if observed is None:
        return {"state": "no_verdict", "reason": "no_measurement", "declared": declared, "observed": None}
    if observed == "indeterminate":
        return {"state": "no_verdict", "reason": "indeterminate_call", "declared": declared, "observed": observed}
    if observed not in ("male", "female"):
        return {"state": "no_verdict", "reason": "invalid_call", "declared": declared, "observed": observed}
    expected = "male" if declared == "M" else "female"
    return {
        "state": "concordant" if observed == expected else "mismatch",
        "reason": None,
        "declared": declared,
        "observed": observed,
    }


__all__ = ["SCHEMA", "sex_assertion", "sex_concordance", "with_sex_assertion"]
