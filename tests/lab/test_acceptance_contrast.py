"""AC23 contrast checks for the Lab's inherited and local design tokens."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.lab.test_acceptance_synthetic_routes import ID12, _sample, _server
from tests.lab.test_lab_server import _chrome, _dom_cdp
from tests.lab.test_landing import needs_chrome
from tests.web.longitudinal_env import _contrast

ROOT = Path(__file__).resolve().parents[2]
BASE = (ROOT / "traceback_runner/web/static/styles.css").read_text(encoding="utf-8")
LAB = (ROOT / "traceback_lab/web/static/lab.css").read_text(encoding="utf-8")


def _token(name: str, css: str = BASE) -> str:
    match = re.search(rf"{re.escape(name)}:\s*(#[0-9a-f]{{6}})", css)
    assert match is not None, name
    return match.group(1)


@pytest.mark.parametrize("foreground,background", [
    ("--ink", "#ffffff"),
    ("--ink", "#f3f1ec"),
    ("--muted", "#ffffff"),
    ("--ink", "--flag-bg"),
])
def test_lab_text_token_pairs_meet_4_5_to_1(foreground, background) -> None:
    fg = _token(foreground)
    bg = _token(background, LAB) if background.startswith("--") else background
    assert _contrast(fg, bg) >= 4.5, (foreground, background)


@pytest.mark.parametrize("token", (
    "--chart-a", "--chart-b", "--chart-hatch", "--neutral", "--focus", "--license",
))
def test_lab_nontext_mark_tokens_meet_3_to_1(token) -> None:
    color = _token(token, LAB if token == "--license" else BASE)
    assert _contrast(color, "#ffffff") >= 3, token


def test_lab_badge_text_pairs_meet_4_5_to_1() -> None:
    assert _contrast("#ffffff", _token("--ink")) >= 4.5
    unverified = re.search(r"\.unverified\s*\{([^}]+)\}", LAB)
    assert unverified is not None
    color = re.search(r"\bcolor:\s*(#[0-9a-f]{6})", unverified.group(1))
    background = re.search(r"\bbackground:\s*(#[0-9a-f]{6})", unverified.group(1))
    assert color is not None and background is not None
    assert _contrast(color.group(1), background.group(1)) >= 4.5


def _rgb_hex(value: str) -> str:
    match = re.fullmatch(
        r"rgba?\(\s*(\d{1,3}),\s*(\d{1,3}),\s*(\d{1,3})(?:,\s*([0-9.]+))?\s*\)", value,
    )
    assert match is not None and (match.group(4) is None or float(match.group(4)) == 1.0), value
    channels = [int(match.group(index)) for index in (1, 2, 3)]
    assert all(0 <= channel <= 255 for channel in channels), value
    return "#" + "".join(f"{channel:02x}" for channel in channels)


@needs_chrome
def test_rendered_lab_badge_text_has_computed_4_5_to_1_contrast(tmp_path) -> None:
    rendered = []
    translucent_probe = []
    with _server(tmp_path, _sample()) as server:
        profile = tmp_path / "browser-profile"

        def inspect(_command, evaluate) -> None:
            expression = """(() => [...document.querySelectorAll('#main .flag-pill, #main .state-chip, #main .tier, #main .unverified')].map((node) => {
              const style = getComputedStyle(node);
              const alpha = (color) => {
                if (/^rgb\\(/.test(color)) return 1;
                const match = color.match(/^rgba\\([^)]*,\\s*([0-9.]+)\\)$/);
                return match ? Number(match[1]) : NaN;
              };
              let ancestor = node;
              let background = 'transparent';
              let partial = false;
              let unknown = false;
              while (ancestor) {
                const layer = getComputedStyle(ancestor).backgroundColor;
                const opacity = alpha(layer);
                if (!Number.isFinite(opacity)) unknown = true;
                if (opacity > 0 && opacity < 1) partial = true;
                if (opacity === 1) { background = layer; break; }
                ancestor = ancestor.parentElement;
              }
              return {kind: node.className, foreground: style.color, background,
                opaque: alpha(background) === 1, partial, unknown};
            }))()"""
            rendered.extend(evaluate(expression))
            evaluate("""(() => {
              const chip = document.querySelector('#main .state-chip');
              chip.style.backgroundColor = 'rgba(0, 0, 0, 0.5)';
              chip.style.color = 'rgb(118, 118, 118)';
            })()""")
            translucent_probe.extend(evaluate(expression))

        _dom_cdp(
            [_chrome(), "--headless=new", "--disable-gpu", "--no-first-run",
             f"--user-data-dir={profile}", server.issue_link() + f"&route=/sample/{ID12}/findings"],
            profile, ok=lambda dom: 'data-ready="1"' in dom and 'class="finding-card"' in dom,
            interact=inspect,
        )
    kinds = {item["kind"] for item in rendered}
    assert any("flag-pill" in kind for kind in kinds)
    assert any("state-chip" in kind for kind in kinds)
    assert any("tier" in kind for kind in kinds)
    assert _contrast("#767676", "#ffffff") >= 4.5
    assert any(item["partial"] and item["foreground"] == "rgb(118, 118, 118)"
               for item in translucent_probe if "state-chip" in item["kind"])
    for item in rendered:
        assert item["opaque"] and not item["partial"] and not item["unknown"], item
        assert _contrast(_rgb_hex(item["foreground"]), _rgb_hex(item["background"])) >= 4.5, item


@needs_chrome
def test_reduced_motion_disables_rendered_lab_transition_and_animation(tmp_path) -> None:
    observed = {}
    with _server(tmp_path, _sample()) as server:
        profile = tmp_path / "browser-profile"

        def inspect(command, evaluate) -> None:
            command("Emulation.setEmulatedMedia", {"features": [
                {"name": "prefers-reduced-motion", "value": "no-preference"},
            ]})
            evaluate("""(() => {
              const chip = document.querySelector('#main .state-chip');
              chip.style.transition = 'opacity 1s linear';
              chip.style.animation = 'pulse 1s infinite';
            })()""")
            observed["ordinary"] = evaluate("""(() => {
              const style = getComputedStyle(document.querySelector('#main .state-chip'));
              return {transition: style.transitionDuration, animation: style.animationName};
            })()""")
            command("Emulation.setEmulatedMedia", {"features": [
                {"name": "prefers-reduced-motion", "value": "reduce"},
            ]})
            observed["reduced"] = evaluate("""(() => {
              const style = getComputedStyle(document.querySelector('#main .state-chip'));
              return {transition: style.transitionDuration, animation: style.animationName};
            })()""")

        _dom_cdp(
            [_chrome(), "--headless=new", "--disable-gpu", "--no-first-run",
             f"--user-data-dir={profile}", server.issue_link() + f"&route=/sample/{ID12}/findings"],
            profile, ok=lambda dom: 'data-ready="1"' in dom and 'class="finding-card"' in dom,
            interact=inspect,
        )
    assert observed["ordinary"]["transition"] == "1s"
    assert observed["reduced"] == {"transition": "0s", "animation": "none"}
