"""``fragment-size``: the 1-bp aligned-span histogram and its summary metrics.

Fragment length is the aligned reference span (``reference_end -
reference_start``), the same definition as the signed ``measurement.py``,
provisional until validation V1 confirms it for ONT (D21).  Formulas follow
Appendix A of the spec.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from ..read_summary import span_counts
from ..registry import AlgorithmOutput, InputContext
from ._base import Base, load_summary, metric, ratio

HIST_MAX = 1000


def periodicity_power(counts: np.ndarray) -> float | None:
    """Share of FFT power in the 10-11 bp period band, spans 50-150 bp.

    ``counts[s]`` is the number of molecules with span ``s``.  The 101 counts
    are divided by their total, mean-subtracted and Hann-windowed.
    """

    window = counts[50:151].astype(float)
    total = window.sum()
    if total <= 0:
        return None
    signal = window / total
    signal = (signal - signal.mean()) * np.hanning(signal.size)
    power = np.abs(np.fft.rfft(signal)) ** 2
    frequencies = np.fft.rfftfreq(signal.size, d=1.0)
    nonzero = frequencies > 0
    band = nonzero & (frequencies >= 1 / 11) & (frequencies <= 1 / 10)
    denominator = power[nonzero].sum()
    return float(power[band].sum() / denominator) if denominator > 0 else None


def summarize(counts: dict[int, int]) -> dict[str, Any]:
    hist = np.zeros(HIST_MAX + 1, dtype=np.int64)
    for span, count in counts.items():
        if 1 <= span <= HIST_MAX:
            hist[span] += count
    n = int(sum(counts.values()))

    def argmax(low: int, high: int) -> int | None:
        window = hist[low : high + 1]
        return int(low + int(window.argmax())) if window.sum() > 0 else None

    short = int(hist[50:150].sum())
    base = int(hist[50 : HIST_MAX + 1].sum())
    return {
        "n": n,
        "hist": hist,
        "mode_bp": argmax(100, 250),
        "peak_mono_bp": argmax(100, 250),
        "peak_di_bp": argmax(250, 450),
        "short_fraction_lt150": ratio(short, base),
        "periodicity_10bp_power": periodicity_power(hist),
    }


@dataclass
class FragmentSize(Base):
    family: str = "fragmentomics"

    def run(self, ctx: InputContext, out: Path) -> AlgorithmOutput:
        summary, mask = load_summary(ctx)
        stats = summarize(span_counts(summary, mask))
        n = stats["n"]
        hist = stats["hist"]
        panel = {"span_bp": list(range(1, HIST_MAX + 1)), "count": [int(v) for v in hist[1:]]}
        return AlgorithmOutput(
            metrics=[
                metric("mode_bp", stats["mode_bp"], unit="bp", n=n),
                metric("short_fraction_lt150", stats["short_fraction_lt150"], unit="fraction", n=n),
                metric("peak_mono_bp", stats["peak_mono_bp"], unit="bp", n=n),
                metric("peak_di_bp", stats["peak_di_bp"], unit="bp", n=n),
                metric("periodicity_10bp_power", stats["periodicity_10bp_power"], unit="fraction", n=n),
            ],
            panels=[{"panel_id": "size-density", "kind": "density", "data_ref": "artifacts/size_hist.json"}],
            artifacts={"size_hist.json": json.dumps(panel, separators=(",", ":")).encode()},
        )


FRAGMENT_SIZE = FragmentSize("fragment-size")
