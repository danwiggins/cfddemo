"""Explicit Somalier comparisons bound to two current, immutable sketches."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import os
import re
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from traceback_runner.serialization import canonical_json_bytes

from .algorithms.identity import IDENTITY, MAX_SKETCH_BYTES, _somalier_env, somalier_tool
from .file_access import open_regular_no_follow
from .identity import SHA256_PATTERN, canonical_sha256, resolve_prefix
from .identity_sites import IdentitySitesError, inspect_sites
from .ingest import id12_for, resolve_reference, source_ids
from .registry import execution_identity
from .store import LabStore, new_attempt_id, write_atomic
from . import views

PAIR_SCHEMA = "traceback.lab.identity-pair.v1"
MAX_PAIRS_SHOWN = 64
_PAIR_HEADER = {"sample_a", "sample_b", "relatedness", "ibs0", "ibs2", "concordance", "n"}


class IdentityPairError(ValueError):
    """A requested pair cannot safely be compared."""


@dataclass(frozen=True)
class SelectedSketch:
    source_id: str
    input_id: str
    attempt_id: str
    parameters_sha256: str
    sketch_sha256: str
    data: bytes

    def identity(self) -> dict[str, str]:
        return {
            "source_id": self.source_id, "input_id": self.input_id,
            "attempt_id": self.attempt_id, "parameters_sha256": self.parameters_sha256,
            "sketch_sha256": self.sketch_sha256,
        }


def _read_sketch(path: Path) -> bytes:
    try:
        descriptor = open_regular_no_follow(path)
    except OSError as exc:
        raise IdentityPairError("selected fingerprint is missing or unsafe") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or not 0 < before.st_size <= MAX_SKETCH_BYTES:
            raise IdentityPairError("selected fingerprint is unsafe or outside the size limit")
        data = os.read(descriptor, MAX_SKETCH_BYTES + 1)
        after = os.fstat(descriptor)
        if len(data) != before.st_size or (before.st_dev, before.st_ino, before.st_mtime_ns) != (
            after.st_dev, after.st_ino, after.st_mtime_ns
        ):
            raise IdentityPairError("selected fingerprint changed while being read")
        return data
    finally:
        os.close(descriptor)


def _selected_sketch(store: LabStore, source_id: str, *, sites_sha256: str) -> SelectedSketch:
    sample = views.load_sample(store, source_id)
    if sample is None:
        raise IdentityPairError("sample has no current full input")
    identity_result = sample.full.results.get("identity")
    summary = sample.full.results.get("read-summary")
    if identity_result is None or summary is None or identity_result.status != "ok":
        raise IdentityPairError("run the identity algorithm for both current full inputs first")
    if identity_result.provenance.source_id != source_id or identity_result.provenance.input_id != sample.full.input_id:
        raise IdentityPairError("fingerprint does not belong to the current source and input")
    if identity_result.depth is None or identity_result.depth.eligible_reads < 10_000_000:
        raise IdentityPairError("fingerprint has fewer than 10 million eligible reads")
    try:
        reference = resolve_reference(store.root, identity_result.provenance.reference_id, example=True)
    except Exception as exc:  # noqa: BLE001 - changed registration means no compatible pair
        raise IdentityPairError("fingerprint reference is no longer registered") from exc
    expected = execution_identity(
        IDENTITY, reference_sha256=reference.sha256, asset_sha256s=[sites_sha256],
        upstream=[{"algorithm_id": "read-summary", "parameters_sha256": summary.provenance.parameters_sha256}],
    )
    parameters_sha256 = identity_result.provenance.parameters_sha256
    if parameters_sha256 != expected:
        raise IdentityPairError("fingerprint uses an older sites, tool, reference, or upstream identity; rerun it")
    attempt = store.selected_attempt(sample.full.input_id, "identity", parameters_sha256)
    artifacts = store.selected_artifacts(sample.full.input_id, "identity", parameters_sha256)
    if attempt is None or artifacts is None:
        raise IdentityPairError("selected fingerprint attempt is incomplete")
    data = _read_sketch(artifacts / "fingerprint.somalier")
    return SelectedSketch(source_id, sample.full.input_id, attempt.name, parameters_sha256,
                          hashlib.sha256(data).hexdigest(), data)


def parse_pair_tsv(raw: bytes, input_a: str, input_b: str) -> dict[str, float | int]:
    """Allowlist one pair from Somalier; never publish its HTML or sample TSV."""

    if len(raw) > 64 * 1024:
        raise IdentityPairError("Somalier pair table is too large")
    try:
        reader = csv.DictReader(io.StringIO(raw.decode("utf-8")), delimiter="\t")
        if reader.fieldnames and reader.fieldnames[0].startswith("#"):
            reader.fieldnames[0] = reader.fieldnames[0][1:]
        if not reader.fieldnames or len(reader.fieldnames) != len(set(reader.fieldnames)):
            raise IdentityPairError("Somalier pair table has an invalid header")
        rows = list(reader)
    except (UnicodeError, csv.Error) as exc:
        raise IdentityPairError("Somalier pair table is unreadable") from exc
    if len(rows) != 1 or None in rows[0] or not _PAIR_HEADER.issubset(rows[0]):
        raise IdentityPairError("Somalier did not produce exactly one complete pair")
    row = rows[0]
    if {row["sample_a"], row["sample_b"]} != {input_a, input_b}:
        raise IdentityPairError("Somalier pair names do not match the selected inputs")
    try:
        values: dict[str, float | int] = {
            "relatedness": float(row["relatedness"]), "concordance": float(row["concordance"]),
            "ibs0": int(row["ibs0"]), "ibs2": int(row["ibs2"]), "n": int(row["n"]),
        }
    except (TypeError, ValueError) as exc:
        raise IdentityPairError("Somalier pair metrics are malformed") from exc
    if not all(math.isfinite(value) for value in values.values()) or any(values[key] < 0 for key in ("ibs0", "ibs2", "n")):
        raise IdentityPairError("Somalier pair metrics are invalid")
    return values


def _selected_pair(store: LabStore, pair_id: str) -> dict[str, Any] | None:
    directory = store.lab / "identity-pairs" / pair_id
    if not SHA256_PATTERN.fullmatch(pair_id):
        return None
    try:
        with os.fdopen(open_regular_no_follow(directory / "selected.json"), "rb") as stream:
            selected = json.loads(stream.read(4097))
        attempt = selected["attempt_id"]
        if not isinstance(attempt, str) or not re.fullmatch(r"a-[0-9A-Za-z-]{8,80}", attempt):
            return None
        with os.fdopen(open_regular_no_follow(directory / "attempts" / attempt / "summary.json"), "rb") as stream:
            summary = json.loads(stream.read(8193))
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if (isinstance(summary, dict) and summary.get("schema") == PAIR_SCHEMA
            and summary.get("pair_id") == pair_id and summary.get("attempt_id") == attempt):
        return summary
    return None


def compare_pair(store: LabStore, sample_a: str, sample_b: str, *, force: bool = False) -> dict[str, Any]:
    """Compare exactly two chosen current samples. Caller holds the writer lock."""

    ids = source_ids(store)
    matches = [resolve_prefix(text, ids) for text in (sample_a, sample_b)]
    if any(match.state != "found" for match in matches):
        raise IdentityPairError("each comparison input must be an unambiguous stored sample ID")
    sources = sorted([matches[0].value, matches[1].value])
    if sources[0] == sources[1]:
        raise IdentityPairError("choose two different stored samples")
    try:
        sites = inspect_sites(store)
    except IdentitySitesError as exc:
        raise IdentityPairError("registered Somalier sites are unavailable") from exc
    tool = somalier_tool()
    if tool is None:
        raise IdentityPairError("Somalier is unavailable")
    sketches = [_selected_sketch(store, source, sites_sha256=sites.sha256) for source in sources]
    if sketches[0].parameters_sha256 != sketches[1].parameters_sha256:
        raise IdentityPairError("fingerprints have incompatible measurement identities")
    pair_id = canonical_sha256({"sources": sources, "kind": "somalier-pair-v1"})
    execution_id = canonical_sha256({
        "participants": [sketch.identity() for sketch in sketches],
        "sites_sha256": sites.sha256, "somalier_version": tool[1],
        "options": {"min_depth": 7, "min_ab": 0.3, "report_all_pairs": True},
    })
    if not force:
        cached = _selected_pair(store, pair_id)
        if cached is not None and cached.get("execution_id") == execution_id:
            return {**cached, "cached": True}
    work_root = store.lab / "tmp"
    work_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="identity-pair-", dir=work_root) as temp:
        work = Path(temp)
        paths = []
        for sketch in sketches:
            path = work / f"{sketch.input_id}.somalier"
            path.write_bytes(sketch.data)
            paths.append(path)
        prefix = work / "pair"
        env = _somalier_env(sketches[0].input_id)
        env["SOMALIER_REPORT_ALL_PAIRS"] = "1"
        try:
            process = subprocess.run(
                [tool[0], "relate", "--sites", str(sites.file), "--output-prefix", str(prefix),
                 str(paths[0]), str(paths[1])], env=env,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=600, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise IdentityPairError("Somalier pair comparison could not complete") from exc
        if process.returncode != 0:
            raise IdentityPairError("Somalier pair comparison failed")
        table = prefix.with_name(prefix.name + ".pairs.tsv")
        try:
            with os.fdopen(open_regular_no_follow(table), "rb") as stream:
                metrics = parse_pair_tsv(stream.read(64 * 1024 + 1), sketches[0].input_id, sketches[1].input_id)
        except OSError as exc:
            raise IdentityPairError("Somalier pair table is missing or unsafe") from exc
    attempt_id = new_attempt_id()
    summary = {
        "schema": PAIR_SCHEMA, "pair_id": pair_id, "attempt_id": attempt_id,
        "execution_id": execution_id,
        "participants": [sketch.identity() for sketch in sketches],
        "sites_sha256": sites.sha256, "somalier_version": tool[1],
        "metrics": metrics, "interpretation": "exploratory; no ONT duplicate or kinship cutoff",
    }
    parent = store.lab / "identity-pairs" / pair_id
    store.publish_directory(parent / "attempts", attempt_id,
                            lambda staging: (staging / "summary.json").write_bytes(canonical_json_bytes(summary)))
    write_atomic(parent / "selected.json", canonical_json_bytes({"attempt_id": attempt_id}))
    store.bump_generation()
    return {**summary, "cached": False}


def pairs_for_sample(store: LabStore, source_id: str) -> list[dict[str, Any]]:
    """Bounded read-only display, omitting comparisons to changed selections."""

    root = store.lab / "identity-pairs"
    if not root.is_dir() or root.is_symlink():
        return []
    rows: list[dict[str, Any]] = []
    try:
        with os.scandir(root) as scan:
            entries = sorted((entry for entry in scan if re.fullmatch(r"[0-9a-f]{64}", entry.name)),
                             key=lambda entry: entry.name)[:MAX_PAIRS_SHOWN]
    except OSError:
        return []
    for entry in entries:
        if not entry.is_dir(follow_symlinks=False):
            continue
        summary = _selected_pair(store, entry.name)
        if summary is None or not isinstance(summary.get("participants"), list):
            continue
        participants = summary["participants"]
        if len(participants) != 2 or source_id not in [row.get("source_id") for row in participants if isinstance(row, dict)]:
            continue
        try:
            sites = inspect_sites(store)
            current = [_selected_sketch(store, row["source_id"], sites_sha256=sites.sha256) for row in participants]
        except (IdentitySitesError, IdentityPairError, KeyError, TypeError):
            continue
        if [sketch.identity() for sketch in current] != participants or sites.sha256 != summary.get("sites_sha256"):
            continue
        other = next(row["source_id"] for row in participants if row["source_id"] != source_id)
        rows.append({"peer_id12": id12_for(store, other), "metrics": summary.get("metrics"),
                     "pair_id": summary["pair_id"], "interpretation": summary.get("interpretation")})
    return rows
