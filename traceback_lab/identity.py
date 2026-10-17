"""D31 identities: what a sample, an input and a result are called.

Every identity is the sha256 of canonical JSON (sorted keys, no whitespace,
UTF-8), so it is stable across machines and Python versions.

- ``source_id`` names the *sample*: the ordered chunk-set digest (or the file
  digest of one already-aligned BAM).
- ``input_id`` names the aligned input every algorithm consumes.  The aligner
  identity is part of it, so a new minimap2 or samtools version can never reuse
  a result computed on an older alignment.
- ``derived_input_id`` names a row-mask view of an input (downsample, split
  half, synthetic CNA spike).
- ``parameters_sha256`` is the execution identity of one result (D19).
- ``cohort_compat_key`` answers "may these two results be compared?" (D8, D17).
- ``id12`` is the shortest unique prefix of a ``source_id``, at least 12 hex.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from traceback_runner.serialization import canonical_json_bytes

SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
ID12_PATTERN = re.compile(r"^[0-9a-f]{12,64}$")
ALGORITHM_ID_PATTERN = re.compile(r"^[a-z0-9-]{1,40}$")
METRIC_ID_PATTERN = re.compile(r"^[a-z0-9_]{1,64}$")
ID12_MIN = 12

Transform = Literal["downsample", "split", "cna"]
TRANSFORMS: tuple[str, ...] = ("downsample", "split", "cna")
PRE_ALIGNED = "pre-aligned"


def canonical_sha256(value: Any) -> str:
    """sha256 hex of the canonical JSON of ``value``."""

    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _sha(value: str, name: str) -> str:
    if not isinstance(value, str) or not SHA256_PATTERN.fullmatch(value):
        raise ValueError(f"{name} must be a lowercase sha256 hex digest")
    return value


def source_id_for_chunks(chunk_sha256s: Sequence[str], excluded: Iterable[int] = ()) -> str:
    """The sample identity of an ordered chunk set (D9 order) and its exclusions."""

    chunks = [_sha(digest, "chunk sha256") for digest in chunk_sha256s]
    if not chunks:
        raise ValueError("a source needs at least one chunk")
    skipped = sorted({int(index) for index in excluded})
    if any(index < 0 for index in skipped):
        raise ValueError("excluded chunk indexes are non-negative")
    return canonical_sha256({"chunks": chunks, "excluded": skipped})


def source_id_for_aligned_bam(file_sha256: str) -> str:
    """A single already-aligned BAM is its own sample identity."""

    return _sha(file_sha256, "file sha256")


def aligned_input_id(
    *,
    source_id: str,
    reference_sha256: str,
    mmi_sha256: str,
    minimap2_version: str,
    samtools_version: str,
    aligner_argv: Sequence[str],
) -> str:
    """The identity of an input the Lab aligned itself."""

    if not minimap2_version or not samtools_version:
        raise ValueError("aligner tool versions are required")
    return canonical_sha256(
        {
            "source_id": _sha(source_id, "source_id"),
            "reference_sha256": _sha(reference_sha256, "reference_sha256"),
            "mmi_sha256": _sha(mmi_sha256, "mmi_sha256"),
            "minimap2_version": minimap2_version,
            "samtools_version": samtools_version,
            "aligner_argv": [str(part) for part in aligner_argv],
        }
    )


def prealigned_input_id(*, source_id: str, reference_sha256: str, platform: str = "unknown") -> str:
    """The identity of an input that arrived already aligned."""

    if platform not in {"ont", "illumina", "unknown"}:
        raise ValueError("prealigned platform must be ont, illumina or unknown")
    identity = {
        "source_id": _sha(source_id, "source_id"),
        "reference_sha256": _sha(reference_sha256, "reference_sha256"),
        "aligner": PRE_ALIGNED,
    }
    if platform != "unknown":
        identity["platform"] = platform
    return canonical_sha256(identity)


def validate_fraction(value: float) -> float:
    """A downsample fraction: a finite float strictly between 0 and 1."""

    fraction = float(value)
    if not (0.0 < fraction < 1.0):
        raise ValueError("a fraction must satisfy 0 < f < 1")
    return fraction


def derived_input_id(
    *,
    parent: str,
    transform: str,
    parameters: Mapping[str, Any],
    salt: str,
) -> str:
    """The identity of a row-mask view of ``parent`` (D31, D33)."""

    if transform not in TRANSFORMS:
        raise ValueError(f"unknown transform {transform!r}")
    if not salt:
        raise ValueError("a derived input needs its predicate salt")
    return canonical_sha256(
        {
            "parent": _sha(parent, "parent input_id"),
            "transform": transform,
            "parameters": dict(parameters),
            "salt": salt,
        }
    )


def derived_label(source_id12: str, transform: str, parameters: Mapping[str, Any]) -> str:
    """Human label ``<id12>@<frac>``, ``<id12>@split-A`` or ``<id12>@cna-<tf>``."""

    if transform == "downsample":
        return f"{source_id12}@{validate_fraction(parameters['fraction'])!r}"
    if transform == "split":
        half = parameters["half"]
        if half not in {"A", "B"}:
            raise ValueError("a split half is A or B")
        return f"{source_id12}@split-{half}"
    if transform == "cna":
        return f"{source_id12}@cna-{validate_fraction(parameters['tf'])!r}"
    raise ValueError(f"unknown transform {transform!r}")


def _tools(tools: Iterable[Mapping[str, str]]) -> list[dict[str, str]]:
    pinned = [{"name": str(tool["name"]), "version": str(tool["version"])} for tool in tools]
    return sorted(pinned, key=lambda tool: (tool["name"], tool["version"]))


def parameters_sha256(
    *,
    algorithm_id: str,
    algorithm_version: str,
    parameters: Mapping[str, Any],
    tools: Iterable[Mapping[str, str]],
    reference_sha256: str,
    asset_sha256s: Iterable[str] = (),
    upstream: Iterable[Mapping[str, str]] = (),
    platform: str | None = None,
) -> str:
    """The execution identity of one result (D19).

    Interpretation (tier, footnote, reference ranges) is deliberately absent:
    editing the library never re-keys a result (D3).
    """

    if not ALGORITHM_ID_PATTERN.fullmatch(algorithm_id):
        raise ValueError("algorithm id must match ^[a-z0-9-]{1,40}$")
    if platform is not None and platform not in {"ont", "illumina", "unknown"}:
        raise ValueError("unknown Lab platform")
    identity = {
        "algorithm_id": algorithm_id,
        "algorithm_version": str(algorithm_version),
        "parameters": dict(parameters),
        "tools": _tools(tools),
        "reference_sha256": _sha(reference_sha256, "reference_sha256"),
        "asset_sha256s": sorted(_sha(item, "asset sha256") for item in asset_sha256s),
        "upstream": sorted(
            (
                {
                    "algorithm_id": str(item["algorithm_id"]),
                    "parameters_sha256": _sha(
                        str(item["parameters_sha256"]), "upstream parameters_sha256"
                    ),
                }
                for item in upstream
            ),
            key=lambda item: (item["algorithm_id"], item["parameters_sha256"]),
            ),
    }
    if platform is not None:
        identity["platform"] = platform
    return canonical_sha256(identity)


def cohort_compat_key(
    *,
    algorithm_id: str,
    algorithm_version: str,
    parameters: Mapping[str, Any],
    tools: Iterable[Mapping[str, str]],
    reference_sha256: str,
    aligner_identity: Mapping[str, Any],
    pon_policy: str = "none",
    per_sample_asset_keys: Iterable[str] = (),
    platform: str | None = None,
) -> str:
    """May two results be compared?  Per-sample assets (a leave-one-out PoN)
    are removed from ``parameters`` first, so LOO members stay comparable."""

    drop = set(per_sample_asset_keys)
    shared = {key: value for key, value in parameters.items() if key not in drop}
    if platform is not None and platform not in {"ont", "illumina", "unknown"}:
        raise ValueError("unknown Lab platform")
    identity = {
        "algorithm_id": algorithm_id,
        "algorithm_version": str(algorithm_version),
        "parameters": shared,
        "tools": _tools(tools),
        "reference_sha256": _sha(reference_sha256, "reference_sha256"),
        "aligner": dict(aligner_identity),
        "pon_policy": pon_policy,
    }
    if platform is not None:
        identity["platform"] = platform
    return canonical_sha256(identity)


def shortest_unique_prefixes(ids: Iterable[str], minimum: int = ID12_MIN) -> dict[str, str]:
    """Map each id to its shortest prefix (>= ``minimum`` hex) unique among ``ids``.

    Git style: two ids sharing 12 characters both get longer prefixes.
    """

    unique = sorted(set(ids))
    for value in unique:
        if not re.fullmatch(r"[0-9a-f]+", value) or len(value) < minimum:
            raise ValueError("ids must be lowercase hex at least as long as the minimum")
    prefixes: dict[str, str] = {}
    for position, value in enumerate(unique):
        shared = 0
        for neighbour in (
            unique[position - 1] if position > 0 else None,
            unique[position + 1] if position + 1 < len(unique) else None,
        ):
            if neighbour is None:
                continue
            common = 0
            for left, right in zip(value, neighbour):
                if left != right:
                    break
                common += 1
            shared = max(shared, common)
        prefixes[value] = value[: min(len(value), max(minimum, shared + 1))]
    return prefixes


@dataclass(frozen=True)
class PrefixMatch:
    """Resolution of a user-supplied ``id12`` prefix."""

    state: Literal["found", "ambiguous", "missing", "invalid"]
    value: str | None = None
    candidates: tuple[str, ...] = ()


def resolve_prefix(text: str, ids: Iterable[str]) -> PrefixMatch:
    """Resolve a 12–64 hex prefix against known ids; never guesses."""

    if not isinstance(text, str) or not ID12_PATTERN.fullmatch(text):
        return PrefixMatch("invalid")
    matches = tuple(sorted(value for value in set(ids) if value.startswith(text)))
    if not matches:
        return PrefixMatch("missing")
    if len(matches) > 1:
        return PrefixMatch("ambiguous", candidates=matches)
    return PrefixMatch("found", value=matches[0], candidates=matches)


__all__ = [
    "ALGORITHM_ID_PATTERN",
    "ID12_MIN",
    "ID12_PATTERN",
    "METRIC_ID_PATTERN",
    "PRE_ALIGNED",
    "PrefixMatch",
    "SHA256_PATTERN",
    "TRANSFORMS",
    "aligned_input_id",
    "canonical_sha256",
    "cohort_compat_key",
    "derived_input_id",
    "derived_label",
    "parameters_sha256",
    "prealigned_input_id",
    "resolve_prefix",
    "shortest_unique_prefixes",
    "source_id_for_aligned_bam",
    "source_id_for_chunks",
    "validate_fraction",
]
