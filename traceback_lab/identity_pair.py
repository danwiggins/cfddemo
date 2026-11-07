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
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from traceback_runner.serialization import canonical_json_bytes

from .assets import AssetError, _check_contained
from .algorithms.identity import IDENTITY, MAX_SKETCH_BYTES, MIN_ELIGIBLE, _somalier_env, somalier_tool
from .file_access import open_regular_no_follow
from .identity import SHA256_PATTERN, canonical_sha256, resolve_prefix
from .identity_sites import IdentitySitesError, inspect_sites
from .ingest import id12_for, resolve_reference, source_ids
from .pipeline import Stage, run_pipeline
from .registry import execution_identity
from .store import LabStore, new_attempt_id, write_atomic
from . import views

PAIR_SCHEMA = "traceback.lab.identity-pair.v1"
MAX_PAIRS_SHOWN = 64
MAX_PAIR_DIRECTORIES_SCANNED = 4096
MAX_PAIR_PEERS_VERIFIED = 4  # each selected sketch can be up to 16 MiB
_PAIR_HEADER = {"sample_a", "sample_b", "relatedness", "ibs0", "ibs2", "hom_concordance", "n"}
SOMALIER_COMPARE_TIMEOUT_S = 600


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
    if identity_result.depth is None or identity_result.depth.eligible_reads < MIN_ELIGIBLE:
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
            "relatedness": float(row["relatedness"]), "concordance": float(row["hom_concordance"]),
            "ibs0": int(row["ibs0"]), "ibs2": int(row["ibs2"]), "n": int(row["n"]),
        }
    except (TypeError, ValueError) as exc:
        raise IdentityPairError("Somalier pair metrics are malformed") from exc
    if not all(math.isfinite(value) for value in values.values()) or any(values[key] < 0 for key in ("ibs0", "ibs2", "n")):
        raise IdentityPairError("Somalier pair metrics are invalid")
    if values["n"] == 0:
        raise IdentityPairError("Somalier found no shared callable sites; the pair is unavailable")
    return values


def _selected_pair(store: LabStore, pair_id: str) -> dict[str, Any] | None:
    directory = store.lab / "identity-pairs" / pair_id
    if not SHA256_PATTERN.fullmatch(pair_id):
        return None
    try:
        with os.fdopen(open_regular_no_follow(directory / "selected.json"), "rb") as stream:
            raw = stream.read(4097)
        if len(raw) > 4096:
            return None
        selected = json.loads(raw)
        attempt = selected["attempt_id"]
        if not isinstance(attempt, str) or not re.fullmatch(r"a-[0-9A-Za-z-]{8,80}", attempt):
            return None
        with os.fdopen(open_regular_no_follow(directory / "attempts" / attempt / "summary.json"), "rb") as stream:
            raw = stream.read(8193)
        if len(raw) > 8192:
            return None
        summary = json.loads(raw)
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if (not isinstance(summary, dict) or summary.get("schema") != PAIR_SCHEMA
            or summary.get("pair_id") != pair_id or summary.get("attempt_id") != attempt
            or not isinstance(summary.get("sites_sha256"), str)
            or not SHA256_PATTERN.fullmatch(summary["sites_sha256"])
            or not isinstance(summary.get("execution_id"), str)
            or not SHA256_PATTERN.fullmatch(summary["execution_id"])):
        return None
    participants = summary.get("participants")
    if not isinstance(participants, list) or len(participants) != 2:
        return None
    required = {"source_id", "input_id", "attempt_id", "parameters_sha256", "sketch_sha256"}
    for participant in participants:
        if (not isinstance(participant, dict) or set(participant) != required
                or any(not isinstance(participant[key], str) for key in required)
                or any(not SHA256_PATTERN.fullmatch(participant[key]) for key in
                       ("source_id", "input_id", "parameters_sha256", "sketch_sha256"))
                or not re.fullmatch(r"a-[0-9A-Za-z-]{8,80}", participant["attempt_id"])):
            return None
    if (participants[0]["source_id"] == participants[1]["source_id"]
            or participants[0]["input_id"] == participants[1]["input_id"]
            or participants[0]["parameters_sha256"] != participants[1]["parameters_sha256"]):
        return None
    metrics = summary.get("metrics")
    if not isinstance(metrics, dict) or set(metrics) != {"relatedness", "concordance", "ibs0", "ibs2", "n"}:
        return None
    if (any(not isinstance(metrics[key], float) or not math.isfinite(metrics[key])
            for key in ("relatedness", "concordance"))
            or any(isinstance(metrics[key], bool) or not isinstance(metrics[key], int)
                   or metrics[key] < 0 for key in ("ibs0", "ibs2", "n"))
            or metrics["n"] == 0):
        return None
    return summary


def _unreadable_pair_notice(store: LabStore, pair_id: str, source_id: str) -> dict[str, Any] | None:
    """Name a relevant invalid selection without trusting any stored metrics."""

    directory = store.lab / "identity-pairs" / pair_id
    try:
        with os.fdopen(open_regular_no_follow(directory / "selected.json"), "rb") as stream:
            selected_raw = stream.read(4097)
        if len(selected_raw) > 4096:
            return None
        attempt = json.loads(selected_raw).get("attempt_id")
        if not isinstance(attempt, str) or not re.fullmatch(r"a-[0-9A-Za-z-]{8,80}", attempt):
            return None
        with os.fdopen(open_regular_no_follow(directory / "attempts" / attempt / "summary.json"), "rb") as stream:
            summary_raw = stream.read(8193)
        if len(summary_raw) > 8192:
            return None
        summary = json.loads(summary_raw)
    except (OSError, ValueError, AttributeError):
        return None
    if not isinstance(summary, dict) or summary.get("pair_id") != pair_id:
        return None
    participants = summary.get("participants")
    if not isinstance(participants, list) or len(participants) != 2:
        return None
    ids = [row.get("source_id") if isinstance(row, dict) else None for row in participants]
    if source_id not in ids:
        return None
    other = next((item for item in ids if item != source_id and isinstance(item, str)
                  and SHA256_PATTERN.fullmatch(item)), None)
    if other is None or pair_id != canonical_sha256({"sources": sorted((source_id, other)),
                                                      "kind": "somalier-pair-v1"}):
        return None
    return {"pair_id": pair_id, "peer_id12": other[:12],
            "state": "unavailable", "reason": "Stored comparison is unreadable; rerun lab identity compare for these samples"}


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
    try:
        _check_contained(store, work_root)
    except AssetError as exc:
        raise IdentityPairError("temporary comparison directory is unsafe") from exc
    work_root.mkdir(parents=True, exist_ok=True)
    try:
        _check_contained(store, work_root)
    except AssetError as exc:
        raise IdentityPairError("temporary comparison directory is unsafe") from exc
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
            outcome = run_pipeline(
                [Stage("somalier-relate", [
                    str(Path(tool[0]).absolute()), "relate", "--sites", str(sites.file),
                    "--min-depth", "7", "--min-ab", "0.3", "--output-prefix", str(prefix),
                    str(paths[0]), str(paths[1]),
                ])],
                env=env, cwd=work, timeout_s=SOMALIER_COMPARE_TIMEOUT_S,
            )
        except OSError as exc:
            raise IdentityPairError("Somalier pair comparison could not complete") from exc
        if outcome.timed_out or outcome.interrupted:
            raise IdentityPairError("Somalier pair comparison could not complete")
        if not outcome.ok:
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
    def status_notice(reason: str) -> dict[str, Any]:
        return {"pair_id": "0" * 64, "peer_id12": "unavailable", "state": "unavailable", "reason": reason}

    try:
        sites = inspect_sites(store)
    except IdentitySitesError:
        return [status_notice("Registered Somalier sites are unavailable; restore the pinned sites to view comparisons")]
    anchor_identity: dict[str, str] | None = None
    anchor_checked = False
    peer_identities: dict[str, dict[str, str] | None] = {}
    peer_failure_noted = False

    class PeerBudgetReached(Exception):
        pass

    def current_identity(participant_source: str) -> dict[str, str] | None:
        nonlocal anchor_identity, anchor_checked
        if participant_source == source_id:
            if not anchor_checked:
                anchor_checked = True
                try:
                    anchor_identity = _selected_sketch(store, source_id, sites_sha256=sites.sha256).identity()
                except IdentityPairError:
                    anchor_identity = None
            return anchor_identity
        if participant_source not in peer_identities:
            if len(peer_identities) >= MAX_PAIR_PEERS_VERIFIED:
                raise PeerBudgetReached
            try:
                peer_identities[participant_source] = _selected_sketch(
                    store, participant_source, sites_sha256=sites.sha256,
                ).identity()
            except IdentityPairError:
                peer_identities[participant_source] = None
        return peer_identities[participant_source]
    try:
        with os.scandir(root) as entries:
            for scanned, entry in enumerate(entries, start=1):
                if scanned > MAX_PAIR_DIRECTORIES_SCANNED:
                    rows.append(status_notice("Comparison list was truncated after scanning its directory limit"))
                    break
                if not re.fullmatch(r"[0-9a-f]{64}", entry.name) or not entry.is_dir(follow_symlinks=False):
                    continue
                summary = _selected_pair(store, entry.name)
                if summary is None:
                    unreadable_notice = _unreadable_pair_notice(store, entry.name, source_id)
                    if unreadable_notice is not None:
                        rows.append(unreadable_notice)
                        if len(rows) == MAX_PAIRS_SHOWN - 1:
                            rows.append(status_notice("Comparison list is limited; additional pairs may exist"))
                            break
                    continue
                if not isinstance(summary.get("participants"), list):
                    continue
                participants = summary["participants"]
                if (len(participants) != 2 or not all(isinstance(row, dict) for row in participants)
                        or source_id not in [row.get("source_id") for row in participants]):
                    continue
                try:
                    anchor = current_identity(source_id)
                    if anchor is None:
                        rows.append(status_notice("Current fingerprint is unavailable; rerun the identity algorithm before viewing comparisons"))
                        break
                    current = [current_identity(row["source_id"]) for row in participants]
                except PeerBudgetReached:
                    rows.append(status_notice("Comparison list was truncated at its fingerprint verification budget"))
                    break
                except (IdentityPairError, KeyError, TypeError):
                    continue
                if any(identity is None for identity in current):
                    if not peer_failure_noted:
                        rows.append(status_notice("A compared fingerprint is unavailable; rerun identity for the affected samples"))
                        peer_failure_noted = True
                    continue
                if current != participants or sites.sha256 != summary.get("sites_sha256"):
                    continue
                other = next(row["source_id"] for row in participants if row["source_id"] != source_id)
                rows.append({"peer_id12": id12_for(store, other), "metrics": summary.get("metrics"),
                             "pair_id": summary["pair_id"], "interpretation": summary.get("interpretation")})
                if len(rows) == MAX_PAIRS_SHOWN - 1:
                    rows.append(status_notice("Comparison list is limited; additional pairs may exist"))
                    break
    except OSError:
        return [status_notice("Comparison list is unavailable; restore its storage and retry")]
    return sorted(rows, key=lambda row: row["pair_id"])
