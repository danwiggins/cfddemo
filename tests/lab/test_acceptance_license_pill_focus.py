"""AC7: a rendered license pill reaches its full callout with real pointer input."""

from __future__ import annotations

import json

from tests.lab.test_acceptance_synthetic_routes import ID12, _sample, _server
from tests.lab.test_lab_server import _chrome, _dom_cdp
from tests.lab.test_landing import needs_chrome


@needs_chrome
def test_finding_license_pills_focus_their_full_callouts(tmp_path) -> None:
    checked: list[str] = []
    with _server(tmp_path, _sample()) as server:
        profile = tmp_path / "browser-profile"

        def inspect(command, evaluate) -> None:
            targets = evaluate("""(() => [...new Set([...document.querySelectorAll('#main .license-pill')]
              .filter((pill) => pill.getClientRects().length)
              .map((pill) => pill.getAttribute('aria-describedby')))])()""")
            assert targets, "the registry-complete Findings fixture must render linked pills"
            for target_id in targets:
                point = evaluate("""(() => {
                  const pill = [...document.querySelectorAll('#main .license-pill')]
                    .find((node) => node.getAttribute('aria-describedby') === __TARGET__
                      && node.getClientRects().length);
                  pill.scrollIntoView({block: 'center'});
                  const box = pill.getBoundingClientRect();
                  return {x: box.left + box.width / 2, y: box.top + box.height / 2};
                })()""".replace("__TARGET__", json.dumps(target_id)))
                for kind in ("mousePressed", "mouseReleased"):
                    command("Input.dispatchMouseEvent", {
                        "type": kind, "x": point["x"], "y": point["y"],
                        "button": "left", "clickCount": 1,
                    })
                state = evaluate("""(() => {
                  const target = document.getElementById(__TARGET__);
                  return {focused: document.activeElement === target,
                    visible: !!target && target.getClientRects().length > 0,
                    tabindex: target?.getAttribute('tabindex')};
                })()""".replace("__TARGET__", json.dumps(target_id)))
                assert state == {"focused": True, "visible": True, "tabindex": "-1"}, (target_id, state)
                checked.append(target_id)

        _dom_cdp(
            [_chrome(), "--headless=new", "--disable-gpu", "--no-first-run",
             f"--user-data-dir={profile}", server.issue_link() + f"&route=/sample/{ID12}/findings"],
            profile, ok=lambda page: 'data-ready="1"' in page and 'class="finding-card"' in page,
            interact=inspect,
        )
    assert len(checked) == len(set(checked))
