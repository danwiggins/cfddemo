"""Missing AC21 state/surface pairs from the approved front-end matrix."""

from __future__ import annotations

from pathlib import Path

from tests.lab.test_acceptance_synthetic_routes import ID12, _sample, _server
from tests.lab.test_lab_server import _chrome, _dom_cdp
from tests.lab.test_landing import _dom, needs_chrome


@needs_chrome
def test_partial_input_trust_qc_names_excluded_chunk_indexes(tmp_path) -> None:
    sample = _sample()
    sample["excluded_chunks"] = [2, 5]
    with _server(tmp_path, sample) as server:
        dom = _dom(server, tmp_path / "browser", route=f"/sample/{ID12}/trust-qc",
                   want=lambda page: "Provisional: this input excludes 2 truncated chunks" in page)
    assert "Excluded chunk indexes: 2, 5" in dom


@needs_chrome
def test_every_finding_state_has_text_outside_its_hidden_glyph(tmp_path) -> None:
    labels = {
        "above_typical": "above typical",
        "markedly_above": "markedly above",
        "below_typical": "below typical",
        "markedly_below": "markedly below",
        "not_interpretable": "not interpretable",
        "no_reference": "no reference",
        "in_range": "in range",
    }
    sample = _sample()
    metrics = [metric for algorithm in sample["algorithms"] for metric in algorithm["metrics"]]
    assert len(metrics) >= len(labels)
    for metric, state in zip(metrics, labels):
        metric["state"] = state
    observed = []
    with _server(tmp_path, sample) as server:
        profile = Path(tmp_path / "browser-profile")

        def inspect(_command, evaluate) -> None:
            observed.extend(evaluate("""(() => [...document.querySelectorAll('#main .state-chip')].map((chip) => {
              const textOnly = chip.cloneNode(true);
              textOnly.querySelectorAll('[aria-hidden="true"]').forEach((node) => node.remove());
              return textOnly.textContent.trim();
            }))()"""))

        dom = _dom_cdp(
            [_chrome(), "--headless=new", "--disable-gpu", "--no-first-run",
             f"--user-data-dir={profile}", server.issue_link() + f"&route=/sample/{ID12}/findings"],
            profile, ok=lambda dom: 'data-ready="1"' in dom and 'class="finding-card"' in dom,
            interact=inspect,
        )
    assert set(list(labels.values())[:4]) <= set(observed)
    for collapsed in ("Not interpretable (1)", "No reference (1)", "In range (1)"):
        assert collapsed in dom
