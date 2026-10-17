"""AC23 contrast checks for the Lab's inherited and local design tokens."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.web.longitudinal_env import _contrast


BASE = Path("traceback_runner/web/static/styles.css").read_text()
LAB = Path("traceback_lab/web/static/lab.css").read_text()


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
    assert _contrast("#573b00", "#fff1c7") >= 4.5
