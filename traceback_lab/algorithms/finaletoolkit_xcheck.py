"""Paired-end FinaleToolkit 1.1.0 end-motif and DELFI ratio cross-check.

The independent comparator is deliberately paired-end. It does not relabel
the Lab's single-end ONT end-motifs or DELFI-style track as equivalent.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import re
import shlex
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pysam

from ..platforms import for_input
from ..registry import AlgorithmOutput, InputContext, Ready, Requirements, Skip, ToolPin
from ._base import Base, metric
from .end_motifs import MOTIFS, motif_diversity
from .short_read_fragment_size import _declared_illumina

VERSION = "1.1.0"
BIN = 100_000
MAPQ = 20
MOTIF_MIN = 4
_AUTOSOME = re.compile(r"^(?:chr)?(?:[1-9]|1[0-9]|2[0-2])$")
_MAX_OUTPUT = 8_000_000
_PIN_SCRIPT = """
import hashlib, importlib.metadata as md, json, sys
from pathlib import Path
import finaletoolkit
root = Path(finaletoolkit.__file__).parent
digest = hashlib.sha256()
for path in sorted(root.rglob('*')):
    if path.is_file() and '__pycache__' not in path.parts and path.suffix != '.pyc':
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(path.read_bytes())
print(json.dumps({'version': md.version('finaletoolkit'), 'code_sha256': digest.hexdigest(),
    'python_version': sys.version, 'python_sha256':
       hashlib.sha256(Path(sys.executable).resolve().read_bytes()).hexdigest(),
    'dependencies': {name: md.version(name) for name in
      ('numpy', 'pysam', 'pandas', 'scipy', 'statsmodels', 'loess')}}))
"""


def _command_base(launcher: Path, interpreter: Path, *, overridden: bool) -> list[str]:
    """Invoke the package under the interpreter whose runtime we attest."""
    if overridden:
        return [str(interpreter), "-m", "finaletoolkit.cli.main_cli"]
    return [str(launcher)]


def _tool() -> tuple[str, str] | None:
    selected = os.environ.get("TRACEBACK_FINALETOOLKIT_BIN") or shutil.which("finaletoolkit")
    if not selected:
        return None
    path = Path(selected).resolve()
    if not path.is_file():
        return None
    try:
        with path.open("rb") as stream:
            launcher_prefix = stream.read(2)
        if launcher_prefix not in {b"#!", b"MZ"}:
            return None
        override = os.environ.get("TRACEBACK_FINALETOOLKIT_PYTHON")
        if override:
            # Preserve the venv executable path; resolve() follows its Python
            # symlink to the base interpreter and loses the installed package.
            interpreter = Path(override).absolute()
        else:
            if launcher_prefix != b"#!":
                return None
            with path.open("rb") as stream:
                first_line = stream.readline(512).decode("utf-8")
            if not first_line.startswith("#!"):
                return None
            parts = shlex.split(first_line[2:])
            if not parts:
                return None
            if Path(parts[0]).name == "env":
                name = parts[2] if len(parts) > 2 and parts[1] == "-S" else parts[1]
                resolved = shutil.which(name)
                if not resolved:
                    return None
                interpreter = Path(resolved)
            else:
                interpreter = Path(parts[0])
        if not interpreter.is_file():
            return None
        cli = subprocess.run([*_command_base(path, interpreter, overridden=bool(override)), "--version"],
                             capture_output=True, text=True,
                             timeout=20, check=False)
        if cli.returncode or not re.search(r"\b1\.1\.0\b", cli.stdout + cli.stderr):
            return None
        answer = subprocess.run([str(interpreter), "-c", _PIN_SCRIPT], capture_output=True,
                                text=True, timeout=20, check=False)
    except (OSError, UnicodeError, ValueError, IndexError, subprocess.TimeoutExpired):
        return None
    if answer.returncode:
        return None
    try:
        identity = json.loads(answer.stdout)
        if identity["version"] != VERSION or not re.fullmatch(r"[0-9a-f]{64}", identity["code_sha256"]):
            return None
        identity["cli_version"] = (cli.stdout + cli.stderr).strip()
        identity["launcher_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        pin = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    except (KeyError, ValueError, TypeError):
        return None
    return str(path), pin


def _inputs(ctx: InputContext) -> tuple[Path, Path, list[tuple[str, int]]]:
    bam_path = ctx.input_dir / "aligned.bam"
    fasta = Path(ctx.extras["reference_fasta"])
    manifest = json.loads((ctx.input_dir / "manifest.json").read_bytes())
    if manifest.get("kind") != "prealigned" or for_input(manifest, bam_path) != "illumina":
        raise ValueError("FinaleToolkit needs a verified prealigned Illumina BAM")
    with pysam.AlignmentFile(str(bam_path), "rb") as bam:
        if not _declared_illumina(bam) or not bam.has_index():
            raise ValueError("FinaleToolkit needs an indexed BAM with unanimous Illumina read groups")
        contigs = list(zip(bam.references, bam.lengths))
    if not any(_AUTOSOME.fullmatch(name) for name, _ in contigs):
        raise ValueError("FinaleToolkit needs hg38 autosomes in the BAM")
    with pysam.FastaFile(str(fasta)) as reference:
        lengths = dict(zip(reference.references, reference.lengths))
        if lengths != dict(contigs):
            raise ValueError("BAM and registered reference contigs/lengths differ; upstream motifs scan every reference contig")
    return bam_path, fasta, contigs


def _pearson(left: list[float], right: list[float]) -> float | None:
    if len(left) < 2 or len(left) != len(right):
        return None
    a, b = np.asarray(left, dtype=float), np.asarray(right, dtype=float)
    if not np.all(np.isfinite(a)) or not np.all(np.isfinite(b)):
        return None
    a -= a.mean()
    b -= b.mean()
    denominator = float(np.linalg.norm(a) * np.linalg.norm(b))
    if not denominator:
        return None
    # Dot and norm use different rounding paths; exact agreement can yield
    # 1.0000000000000002, which the artifact reader correctly rejects.
    return max(-1.0, min(1.0, float(np.dot(a, b) / denominator)))


def _reference_counts(bam_path: Path, fasta: Path, contigs: list[tuple[str, int]]) -> tuple[dict[str, int], dict[tuple[str, int], tuple[int, int]], int]:
    """Count first mates with the pinned upstream fragment and MAPQ filters."""
    motifs = dict.fromkeys(MOTIFS, 0)
    counts: dict[tuple[str, int], list[int]] = {}
    pairs = 0
    with pysam.AlignmentFile(str(bam_path), "rb") as bam, pysam.FastaFile(str(fasta)) as reference:
        for name, length in contigs:
            for read in bam.fetch(name):
                if (not read.is_read1 or not read.is_paired or not read.is_proper_pair
                    or read.is_unmapped or read.mate_is_unmapped or read.is_secondary
                    or read.is_supplementary or read.is_duplicate or read.is_qcfail
                    or read.mapping_quality < MAPQ):
                    continue
                tlen = read.template_length
                if read.reference_end is None:
                    continue
                if tlen > 0:
                    start, stop = read.reference_start, read.reference_start + tlen
                elif tlen < 0:
                    start, stop = read.reference_end + tlen, read.reference_end
                else:
                    continue
                if start < 0 or stop > length or start >= stop:
                    continue
                pairs += 1
                # v1.1.0 passes --min-length to its region worker but does not
                # apply that filter there. Match the actual pinned output.
                if start + 4 <= length:
                    left = reference.fetch(name, start, start + 4).upper()
                    if left in motifs:
                        motifs[left] += 1
                if stop >= 4:
                    right = reference.fetch(name, stop - 4, stop).upper()
                    if set(right) <= set("ACGT") and len(right) == 4:
                        reverse = right.translate(str.maketrans("ACGT", "TGCA"))[::-1]
                        motifs[reverse] += 1
                span = stop - start
                if _AUTOSOME.fullmatch(name) and 100 <= span <= 220:
                    key = (name, ((start + stop) // 2 // BIN) * BIN)
                    # FinaleToolkit queries indexed read1 alignments per bin;
                    # a fragment whose midpoint is in a bin but whose read1
                    # alignment does not overlap it is absent upstream.
                    if not (read.reference_start < key[1] + BIN and read.reference_end > key[1]):
                        continue
                    row = counts.setdefault(key, [0, 0])
                    row[0 if span <= 150 else 1] += 1
    return motifs, {key: (row[0], row[1]) for key, row in counts.items()}, pairs


def _motifs(path: Path) -> dict[str, float]:
    if path.stat().st_size > _MAX_OUTPUT:
        raise ValueError("FinaleToolkit motif output exceeds the size limit")
    rows: dict[str, float] = {}
    with path.open(newline="") as stream:
        for motif, value in csv.reader(stream, delimiter="\t"):
            number = float(value)
            if motif not in MOTIFS or motif in rows or not math.isfinite(number) or number < 0:
                raise ValueError("FinaleToolkit motif output is invalid")
            rows[motif] = number
    if set(rows) != set(MOTIFS) or not 0.999 <= sum(rows.values()) <= 1.001:
        raise ValueError("FinaleToolkit motif output is incomplete")
    return rows


def _delfi(path: Path, expected: set[tuple[str, int]]) -> dict[tuple[str, int], tuple[int, int]]:
    if path.stat().st_size > _MAX_OUTPUT:
        raise ValueError("FinaleToolkit DELFI output exceeds the size limit")
    rows: dict[tuple[str, int], tuple[int, int]] = {}
    with path.open(newline="") as stream:
        reader = csv.DictReader(stream, delimiter="\t")
        if not reader.fieldnames or not {"#contig", "start", "short", "long"} <= set(reader.fieldnames):
            raise ValueError("FinaleToolkit DELFI columns changed")
        for row in reader:
            key = row["#contig"], int(row["start"])
            if key not in expected or key in rows:
                raise ValueError("FinaleToolkit DELFI bins changed")
            short, long = float(row["short"]), float(row["long"])
            if (not math.isfinite(short) or not math.isfinite(long) or short < 0 or long < 0
                or not short.is_integer() or not long.is_integer()):
                raise ValueError("FinaleToolkit DELFI counts are invalid")
            rows[key] = int(short), int(long)
    if set(rows) != expected:
        raise ValueError("FinaleToolkit DELFI output is incomplete")
    return rows


@dataclass
class FinaleToolkitXcheck(Base):
    version: str = "1"
    family: str = "fragmentomics"
    requires: Requirements = field(default_factory=lambda: Requirements(
        min_eligible_reads=1_000_000, platforms=frozenset({"illumina"})))
    consumes: str = "full_bam"
    cost: str = "slow"
    upstream: tuple[str, ...] = ()
    parameters: dict[str, Any] = field(default_factory=lambda: {
        "upstream_version": VERSION, "min_mapq": MAPQ, "end_motif_k": 4,
        "end_motif_min_length": MOTIF_MIN, "delfi_bin_bp": BIN,
        "delfi_short_bp": [100, 150], "delfi_long_bp": [151, 220],
        "delfi_gc_correct": False, "delfi_merge_bins": False,
        "delfi_remove_hg19_nocov": False, "input": "full paired-end Illumina BAM",
    })

    @property  # type: ignore[override]
    def tools(self) -> tuple[ToolPin, ...]:
        tool = _tool()
        return (ToolPin("finaletoolkit", f"{VERSION}+{tool[1]}" if tool else "missing"),)

    @tools.setter
    def tools(self, _value: Any) -> None:
        pass

    def preflight(self, ctx: InputContext) -> Ready | Skip:
        if ctx.platform != "illumina":
            return Skip("platform", "FinaleToolkit 1.1.0 requires a paired-end Illumina input; the ONT spike yielded no usable fragments")
        if ctx.derived:
            return Skip("derived_not_supported", "FinaleToolkit cross-check uses the full paired-end input only")
        if ctx.example or ctx.reference_id.lower() not in {"hg38", "grch38"}:
            return Skip("reference", "FinaleToolkit cross-check needs a genuine registered hg38 input")
        decision = super().preflight(ctx)
        if isinstance(decision, Skip):
            return decision
        if ctx.eligible_reads is None:
            return Skip("depth", "FinaleToolkit needs a measured eligible-read count for its 1M floor")
        if _tool() is None:
            return Skip("missing_toolchain", "Install FinaleToolkit 1.1.0 or set TRACEBACK_FINALETOOLKIT_BIN")
        if not ctx.extras.get("reference_fasta"):
            return Skip("reference", "registered hg38 FASTA is unavailable")
        try:
            _inputs(ctx)
        except (OSError, ValueError, KeyError):
            # Input/library exceptions can include the operator's local path,
            # which must not enter a persisted skip or the Lab UI.
            return Skip("input_kind", "FinaleToolkit needs a readable indexed prealigned Illumina BAM and matching registered FASTA")
        return Ready()

    def run(self, ctx: InputContext, out: Path) -> AlgorithmOutput:
        try:
            return self._run(ctx, out)
        except subprocess.TimeoutExpired:
            raise RuntimeError("FinaleToolkit execution timed out") from None
        except (OSError, subprocess.SubprocessError):
            # Workers scrub stderr, but direct callers must not receive local
            # sample paths in an exception either.
            raise RuntimeError("FinaleToolkit input or output became unreadable") from None

    def _run(self, ctx: InputContext, out: Path) -> AlgorithmOutput:
        tool = _tool()
        if tool is None or ctx.extras.get("planned_tool_pins") != [pin.identity() for pin in self.tools]:
            raise RuntimeError("FinaleToolkit tool changed since execution planning")
        bam_path, fasta, contigs = _inputs(ctx)
        work = out.parent / "finaletoolkit-work"
        work.mkdir()
        sizes, bins = work / "autosomes.chrom.sizes", work / "autosomes.100kb.bed"
        expected: set[tuple[str, int]] = set()
        with sizes.open("w") as size_stream, bins.open("w") as bin_stream:
            for name, length in contigs:
                if not _AUTOSOME.fullmatch(name):
                    continue
                size_stream.write(f"{name}\t{length}\n")
                for start in range(0, length, BIN):
                    bin_stream.write(f"{name}\t{start}\t{min(start + BIN, length)}\n")
                    expected.add((name, start))
        motif_path, delfi_path = work / "motifs.tsv", work / "delfi.tsv"
        override = os.environ.get("TRACEBACK_FINALETOOLKIT_PYTHON")
        command_base = _command_base(Path(tool[0]), Path(override).absolute() if override else Path(tool[0]),
                                     overridden=bool(override))
        commands = [
            [*command_base, "end-motifs", str(bam_path), str(fasta), "-k", "4", "--min-length", "4",
             "-q", str(MAPQ), "-t", "1", "-o", str(motif_path)],
            [*command_base, "delfi", str(bam_path), str(sizes), str(fasta), str(bins),
             "--no-gc-correct", "--no-merge-bins", "--no-remove-nocov", "-q", str(MAPQ),
             "-t", "1", "-o", str(delfi_path)],
        ]
        for name, command in zip(("end-motifs", "delfi"), commands):
            completed = subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                       timeout=7200, check=False)
            if completed.returncode:
                raise RuntimeError(f"FinaleToolkit {name} failed (exit {completed.returncode})")
        if _tool() != tool:
            raise RuntimeError("FinaleToolkit tool changed during execution")
        upstream_motifs = _motifs(motif_path)
        upstream_delfi = _delfi(delfi_path, expected)
        reference_motifs, reference_delfi, pairs = _reference_counts(bam_path, fasta, contigs)
        total = sum(reference_motifs.values())
        if total <= 0:
            raise ValueError("paired-end input yielded no comparable 4-mer ends")
        reference_frequencies = {k: reference_motifs[k] / total for k in MOTIFS}
        motif_r = _pearson([upstream_motifs[k] for k in MOTIFS],
                           [reference_frequencies[k] for k in MOTIFS])
        motif_max_abs_difference = max(abs(upstream_motifs[k] - reference_frequencies[k]) for k in MOTIFS)
        delfi_keys = [key for key in sorted(expected) if upstream_delfi[key][1] and reference_delfi.get(key, (0, 0))[1]]
        delfi_r = _pearson([upstream_delfi[key][0] / upstream_delfi[key][1] for key in delfi_keys],
                           [reference_delfi[key][0] / reference_delfi[key][1] for key in delfi_keys])
        comparable = motif_r is not None and delfi_r is not None
        counts_equal = all(upstream_delfi[key] == reference_delfi.get(key, (0, 0)) for key in expected)
        track_lines = ["contig\tstart\tshort\tlong\tratio\n"]
        for name, start in sorted(expected):
            short, long = reference_delfi.get((name, start), (0, 0))
            track_lines.append(f"{name}\t{start}\t{short}\t{long}\t{short / long if long else ''}\n")
        reference_track = "".join(track_lines)
        panel = {
            "schema": "traceback.lab.finaletoolkit-xcheck.v1", "version": VERSION,
            "runtime_pin_sha256": tool[1],
            "input": "verified paired-end Illumina hg38 full BAM", "mapq": MAPQ,
            "motif": {"k": 4, "min_length": MOTIF_MIN, "r": motif_r, "threshold": 0.95,
                      "max_abs_frequency_difference": motif_max_abs_difference,
                      "length_filter_effective": False,
                      "upstream_mds": motif_diversity(np.asarray([upstream_motifs[k] for k in MOTIFS])),
                      "reference_mds": motif_diversity(np.asarray([reference_motifs[k] for k in MOTIFS])),
                      "upstream": upstream_motifs,
                      "reference": reference_frequencies},
            "delfi": {"bin_bp": BIN, "gc_correct": False, "merged": False,
                      "remove_hg19_nocov": False, "r": delfi_r, "threshold": 0.90,
                      "comparable_bins": len(delfi_keys),
                      "upstream_counts_equal": counts_equal},
            "pairs": pairs, "motif_ends": total,
            "verdict": ("concordant" if motif_r >= 0.95 and delfi_r >= 0.90 and counts_equal
                                          else "discordant") if comparable else "insufficient_variation",
            "note": "Uncorrected 100-kb paired-end method parity; not the ONT Lab ratio track, original GC-corrected DELFI classifier, or clinical validation.",
        }
        return AlgorithmOutput(
            metrics=[metric("motif_pearson_r", motif_r, unit="correlation", n=256),
                     metric("delfi_ratio_pearson_r", delfi_r, unit="correlation", n=len(delfi_keys))],
            panels=[{"panel_id": "finaletoolkit-xcheck", "kind": "density",
                     "data_ref": "artifacts/finaletoolkit_xcheck.json"}],
            artifacts={"finaletoolkit_xcheck.json": json.dumps(panel, separators=(",", ":")).encode(),
                       "finaletoolkit_motifs.tsv": motif_path.read_bytes(),
                       "finaletoolkit_delfi.tsv": delfi_path.read_bytes(),
                       "reference_delfi.tsv": reference_track.encode("ascii")},
        )


FINALETOOLKIT_XCHECK = FinaleToolkitXcheck("finaletoolkit-xcheck")
