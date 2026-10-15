"""``lab run --example`` runtime guards (S6d budget).

Two costs grew the example past the CI budget after L1.3: every algorithm
worker imported the signed CLI (and its web server) through
``registry -> problems``, about 1 s per step over ~45 steps, and the
``meth-calls`` decoder walked every base and every MM call of every record in
Python.  These tests pin both fixes: the worker's import closure stays free of
the signed CLI, and the CpG-proportional decoder matches the per-base
reference implementation it replaced, record by record.
"""

from __future__ import annotations

import array
import random
import subprocess
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pysam
import pytest

from traceback_lab import meth_calls
from traceback_lab.meth_calls import EXPLICIT_H, EXPLICIT_M, HAS_H, HAS_M, MethCalls, write_meth_calls
from traceback_lab.modbase import mm_skip_modes, mm_tag, mn_ok

from .bam_fixtures import Read, write_bam

HEAVY = ("traceback_runner.cli", "traceback_runner.web")


def test_worker_and_example_algorithms_do_not_import_the_signed_cli() -> None:
    script = (
        "import sys, traceback_lab.worker\n"
        "from traceback_lab.registry import load_registry\n"
        "load_registry()\n"
        f"print(sorted(m for m in sys.modules if m.startswith({HEAVY!r})))\n"
    )
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, check=True)
    assert result.stdout.strip() == "[]", result.stdout


def test_unknown_only_id_still_raises_the_lab_problem() -> None:
    from traceback_lab.problems import LabProblem
    from traceback_lab.registry import load_registry, plan

    with pytest.raises(LabProblem) as raised:
        plan(load_registry(), only=["bam-qcc"])
    assert raised.value.code == "TBX-LAB-001"


# ------------------------------------------------- decoder equivalence
class _Invalid(ValueError):
    pass


def _reference_record_sites(record: Any) -> tuple[list[tuple[int, int, int, int, int]], int]:
    """The per-base decoder as shipped in L1.3 (7e79f60), kept as the oracle."""

    mm = mm_tag(record)
    if mm is None:
        return [], 0
    if not mn_ok(record):
        raise _Invalid("MN mismatch")
    sequence = record.query_sequence or ""
    try:
        parsed = record.modified_bases
    except (ValueError, KeyError, IndexError) as exc:
        raise _Invalid(str(exc)) from exc
    if parsed is None:
        raise _Invalid("MM/ML do not parse")
    reverse = record.is_reverse
    sites: dict[int, dict[str, int]] = {}
    explicit: set[tuple[int, str]] = set()
    for (base, _strand, code), calls in parsed.items():
        if base not in ("C", "G") or code not in ("m", "h"):
            continue
        for position, quality in calls:
            if quality < 0:
                raise _Invalid("MM call without ML")
            on_c = position + 1 < len(sequence) and sequence[position] == "C" and sequence[position + 1] == "G"
            on_g = 0 < position < len(sequence) and sequence[position - 1] == "C" and sequence[position] == "G"
            if on_c or on_g:
                sites.setdefault(position, {})[code] = int(quality)
                explicit.add((position, code))
    complement = {"C": "G", "G": "C"}
    for code, mode in mm_skip_modes(mm).items():
        base, letter = code[0], code[-1]
        if mode != "." or base not in complement or letter not in ("m", "h"):
            continue
        target = complement[base] if reverse else base
        for index, stored in enumerate(sequence):
            if stored != target:
                continue
            on_c = stored == "C" and index + 1 < len(sequence) and sequence[index + 1] == "G"
            on_g = stored == "G" and index > 0 and sequence[index - 1] == "C"
            if on_c or on_g:
                sites.setdefault(index, {}).setdefault(letter, 0)
    if not sites:
        return [], 0
    pairs = dict(record.get_aligned_pairs(matches_only=True))
    out: list[tuple[int, int, int, int, int]] = []
    dropped = 0
    for position in sorted(sites):
        on_g = sequence[position] == "G"
        c_pos = position - 1 if on_g else position
        ref_c, ref_g = pairs.get(c_pos), pairs.get(c_pos + 1)
        if ref_c is None or ref_g is None or ref_g != ref_c + 1:
            dropped += 1
            continue
        calls = sites[position]
        channels = (
            (HAS_M if "m" in calls else 0)
            | (HAS_H if "h" in calls else 0)
            | (EXPLICIT_M if (position, "m") in explicit else 0)
            | (EXPLICIT_H if (position, "h") in explicit else 0)
        )
        out.append((ref_c, 1 if on_g else 0, calls.get("m", 0), calls.get("h", 0), channels))
    return out, dropped


def _outcome(decode: Any, record: Any) -> object:
    try:
        sites, dropped = decode(record)
    except (meth_calls._InvalidRecord, _Invalid):
        return "invalid"
    return [tuple(int(v) for v in site) for site in sites], int(dropped)


_REVCOMP = str.maketrans("ACGT", "TGCA")


def _random_cigar(rng: random.Random, length: int) -> list[tuple[int, int]]:
    """A CIGAR consuming ``length`` query bases: clips, matches, indels, skips."""

    cigar: list[tuple[int, int]] = []
    if rng.random() < 0.2:
        cigar.append((5, rng.randint(1, 5)))
    remaining = length
    if remaining > 2 and rng.random() < 0.3:
        clip = rng.randint(1, min(4, remaining - 1))
        cigar.append((4, clip))
        remaining -= clip
    tail = 0
    if remaining > 2 and rng.random() < 0.3:
        tail = rng.randint(1, min(4, remaining - 1))
        remaining -= tail
    while remaining:
        take = rng.randint(1, remaining)
        cigar.append((rng.choice((0, 0, 0, 7, 8)), take))
        remaining -= take
        if remaining and rng.random() < 0.5:
            op = rng.choice((1, 2, 3))
            if op == 1:
                size = rng.randint(1, remaining)
                cigar.append((1, size))
                remaining -= size
            else:
                cigar.append((op, rng.randint(1, 3)))
    if tail:
        cigar.append((4, tail))
    if rng.random() < 0.2:
        cigar.append((5, rng.randint(1, 5)))
    return cigar


def _random_mm(rng: random.Random, original: str) -> tuple[str, list[int]]:
    parts: list[str] = []
    ml: list[int] = []
    for base, strand, mods in rng.sample([("C", "+", "m"), ("C", "+", "h"), ("C", "+", "mh"), ("G", "-", "m"), ("A", "+", "a")], k=rng.randint(1, 3)):
        mode = rng.choice(("", "?", "."))
        count = original.count(base)
        listed = sorted(rng.sample(range(count), k=rng.randint(0, count))) if count else []
        if rng.random() < 0.05 and count:
            listed.append(count + 3)  # past the last base: pysam refuses or drops it
        deltas, previous = [], -1
        for index in listed:
            deltas.append(index - previous - 1)
            previous = index
        parts.append(f"{base}{strand}{mods}{mode}" + "".join(f",{d}" for d in deltas) + ";")
        ml.extend(rng.randint(0, 255) for _ in range(len(listed) * len(mods)))
    if ml and rng.random() < 0.05:
        ml.pop()  # an ML shorter than MM
    return "".join(parts), ml


def _random_reads(seed: int, count: int) -> list[Read]:
    rng = random.Random(seed)
    reads = []
    for index in range(count):
        length = rng.randint(1, 40)
        sequence = "".join(rng.choices("ACGT", weights=(2, 4, 4, 2), k=length))
        flag = rng.choice((0, 16))
        original = sequence.translate(_REVCOMP)[::-1] if flag & 16 else sequence
        mm, ml = _random_mm(rng, original)
        mn = None if rng.random() > 0.05 else length + 1
        reads.append(
            Read(f"r{index}", "chr1", rng.randint(0, 5_000), sequence, flag=flag, cigar=_random_cigar(rng, length), mm=mm, ml=ml, mn=mn)
        )
    return reads


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_decoder_matches_the_per_base_reference(tmp_path: Path, seed: int) -> None:
    bam = write_bam(tmp_path / "fuzz.bam", [("chr1", 10_000)], _random_reads(seed, 600))
    compared = invalid = sites = 0
    with pysam.AlignmentFile(str(bam)) as stream:
        for record in stream.fetch(until_eof=True):
            expected = _outcome(_reference_record_sites, record)
            assert _outcome(meth_calls._record_sites, record) == expected, record.to_string()
            compared += 1
            if expected == "invalid":
                invalid += 1
            else:
                sites += len(expected[0])  # type: ignore[index]
    # the fuzz reaches every branch: valid sites, drops and invalid records
    assert compared == 600 and invalid > 0 and sites > 0


def test_decoder_matches_the_reference_on_the_example_generator(tmp_path: Path) -> None:
    from traceback_lab.synthetic import _mm_ml

    rng = random.Random(5)
    reads = []
    for index in range(300):
        sequence = "".join(rng.choices("ACGT", weights=(29.5, 20.5, 20.5, 29.5), k=rng.randint(60, 200)))
        flag = rng.choice((0, 16))
        original = sequence.translate(_REVCOMP)[::-1] if flag & 16 else sequence
        mm, ml = _mm_ml(original, rng, 0.7)
        reads.append(Read(f"e{index}", "chr1", rng.randint(0, 5_000), sequence, flag=flag, mm=mm, ml=ml))
    bam = write_bam(tmp_path / "example.bam", [("chr1", 10_000)], reads)
    with pysam.AlignmentFile(str(bam)) as stream:
        for record in stream.fetch(until_eof=True):
            assert _outcome(meth_calls._record_sites, record) == _outcome(_reference_record_sites, record)


def test_batched_rows_split_exactly_at_chunk_calls(tmp_path: Path) -> None:
    reads = [Read(f"c{i:03d}", "chr1", 10 * i, "ACGACGACG", mm="C+m?,0,0,0;", ml=[i % 256, 7, 250]) for i in range(50)]
    bam = write_bam(tmp_path / "chunks.bam", [("chr1", 10_000)], reads)

    def store(name: str, chunk_calls: int, pending: int) -> MethCalls:
        original = meth_calls.PENDING_CALLS
        meth_calls.PENDING_CALLS = pending
        try:
            write_meth_calls(bam, tmp_path / name, eligible=lambda r: True, contig_index={"chr1": 0}, chunk_calls=chunk_calls)
        finally:
            meth_calls.PENDING_CALLS = original
        return MethCalls.open(tmp_path / name)

    whole = store("whole", 1 << 20, 1 << 16)
    split = store("split", 7, 5)
    assert whole.meta["chunks"] == [150]
    assert split.meta["chunks"] == [7] * 21 + [3]
    assert np.array_equal(np.concatenate(list(whole.chunks())), np.concatenate(list(split.chunks())))
    rows = np.concatenate(list(whole.chunks()))
    assert np.array_equal(rows["p_canonical"], np.maximum(0, 255 - rows["p_m"].astype(int) - rows["p_h"].astype(int)))
    assert array.array("I", rows["read_row"][:3]) == array.array("I", [0, 0, 0])


def test_decoder_refuses_mm_without_ml_like_the_reference() -> None:
    header = pysam.AlignmentHeader.from_dict({"SQ": [{"SN": "chr1", "LN": 1_000}]})
    record = pysam.AlignedSegment(header)
    record.query_name = "noml"
    record.query_sequence = "ACGACGTT"
    record.reference_id, record.reference_start, record.cigartuples = 0, 10, [(0, 8)]
    record.set_tag("MM", "C+m?,0,0;", value_type="Z")  # no ML tag: pysam reports -1
    assert _outcome(_reference_record_sites, record) == "invalid"
    assert _outcome(meth_calls._record_sites, record) == "invalid"
