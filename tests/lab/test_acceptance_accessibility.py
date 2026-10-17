"""Synthetic browser checks for Lab keyboard and responsive acceptance."""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

import pytest

from tests.lab.test_acceptance_synthetic_routes import ID12, _sample, _server
from tests.lab.test_lab_server import _chrome, _dom_cdp
from tests.lab.test_landing import _dom, needs_chrome


@needs_chrome
def test_large_viewport_banner_stays_within_landing_budget(tmp_path) -> None:
    with _server(tmp_path, _sample()) as server:
        dom = _dom(server, tmp_path / "browser", size="1280,800", route="/",
                   want=lambda d: 'data-measured="1"' in d)
    height = int(re.search(r'data-banner-height="(\d+)"', dom).group(1))
    assert height <= 40


@needs_chrome
@pytest.mark.parametrize("size", ["1280,800", "1024,768", "768,1024", "360,740"])
def test_all_routes_have_no_page_overflow_and_44px_targets(size, tmp_path) -> None:
    routes = [
        "/", *(f"/sample/{ID12}/{lens}" for lens in (
            "findings", "trust-qc", "stability", "signals", "compare",
            "body-map", "agreement", "evidence", "methods",
        )),
    ]
    headings = {
        "/": f"Traceback Lab — {ID12}",
        **{f"/sample/{ID12}/{lens}": title for lens, title in (
            ("findings", "Findings"), ("trust-qc", "Trust / QC"),
            ("stability", "Stability"), ("signals", "Signals"),
            ("compare", "Compare"), ("body-map", "Body map"),
            ("agreement", "Agreement"), ("evidence", "Evidence"),
            ("methods", "Methods"),
        )},
    }
    assert set(headings) == set(routes)
    with _server(tmp_path, _sample()) as server:
        kind, source = server.app.assets["/assets/lab.js"]
        probe = b"""
        window.addEventListener('load', () => {
          const routes = __ROUTES__;
          const headings = __HEADINGS__;
          const visible = (el) => {
            const style = getComputedStyle(el);
            return style.display !== 'none' && style.visibility !== 'hidden' && el.getClientRects().length > 0;
          };
          const audit = async () => {
            while (document.body.dataset.ready !== '1') await new Promise((r) => setTimeout(r, 20));
            const failures = [];
            const audited = [];
            for (const route of routes) {
              location.hash = `#${route}`;
              const deadline = performance.now() + 5000;
              while (document.querySelector('#main h1')?.textContent.trim() !== headings[route]
                     && performance.now() < deadline)
                await new Promise((r) => setTimeout(r, 20));
              const rendered = document.querySelector('#main h1')?.textContent.trim();
              if (rendered !== headings[route]) {
                failures.push(`${route}: render timeout; expected ${headings[route]}, saw ${rendered}`);
                break;
              }
              await new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)));
              audited.push(route);
              if (document.documentElement.scrollWidth > document.documentElement.clientWidth + 1)
                failures.push(`${route}: page scroll ${document.documentElement.scrollWidth}/${document.documentElement.clientWidth}`);
              for (const el of document.querySelectorAll('a[href], button, select, summary, input')) {
                if (!visible(el)) continue;
                const rect = el.getBoundingClientRect();
                if (rect.width < 43.5 || rect.height < 43.5)
                  failures.push(`${route}: ${el.tagName.toLowerCase()} ${el.textContent.trim().slice(0, 35)} ${Math.round(rect.width)}x${Math.round(rect.height)}`);
              }
            }
            document.body.dataset.auditFailures = failures.slice(0, 30).join(' | ');
            document.body.dataset.auditCount = String(failures.length);
            document.body.dataset.auditedCount = String(audited.length);
            document.body.dataset.auditedUniqueCount = String(new Set(audited).size);
            document.body.dataset.auditComplete = '1';
          };
          audit();
        });
        """.replace(b"__ROUTES__", json.dumps(routes).encode()).replace(
            b"__HEADINGS__", json.dumps(headings).encode()
        )
        server.app.assets["/assets/lab.js"] = (kind, source + probe)
        dom = _dom(server, tmp_path / "browser", size=size, route="/",
                   want=lambda d: 'data-audit-complete="1"' in d)
    body = re.search(r"<body([^>]*)>", dom).group(1)
    count = int(re.search(r'data-audit-count="(\d+)"', body).group(1))
    detail = re.search(r'data-audit-failures="([^"]*)"', body).group(1)
    audited_count = int(re.search(r'data-audited-count="(\d+)"', body).group(1))
    unique_count = int(re.search(r'data-audited-unique-count="(\d+)"', body).group(1))
    assert (audited_count, unique_count) == (len(routes), len(routes)), f"{size}: {detail}"
    assert count == 0, f"{size}: {detail} ({count} total)"


@needs_chrome
def test_wide_methods_table_scrolls_inside_its_wrapper_at_360px(tmp_path) -> None:
    with _server(tmp_path, _sample()) as server:
        kind, source = server.app.assets["/assets/lab.js"]
        server.app.assets["/assets/lab.js"] = (kind, source + b"""
          window.addEventListener('load', () => {
            const timer = setInterval(() => {
              const wrap = document.querySelector('#main .table-wrap');
              if (!wrap || !wrap.querySelector('table')) return;
              document.body.dataset.tableScroll = String(wrap.scrollWidth > wrap.clientWidth + 1);
              document.body.dataset.tableOverflow = getComputedStyle(wrap).overflowX;
              document.body.dataset.pageOverflow = String(document.documentElement.scrollWidth > document.documentElement.clientWidth + 1);
              clearInterval(timer);
            }, 20);
          });
        """)
        dom = _dom(server, tmp_path / "browser", size="360,740",
                   route=f"/sample/{ID12}/methods", want=lambda d: 'data-table-scroll' in d)
    assert 'data-table-scroll="true"' in dom
    assert 'data-table-overflow="auto"' in dom
    assert 'data-page-overflow="false"' in dom


def _key(command, key: str, code: str, virtual: int) -> None:
    args = {"key": key, "code": code, "windowsVirtualKeyCode": virtual, "nativeVirtualKeyCode": virtual}
    if virtual in (13, 32):
        args["text"] = "\r" if virtual == 13 else " "
    command("Input.dispatchKeyEvent", {"type": "keyDown" if virtual in (13, 32) else "rawKeyDown", **args})
    command("Input.dispatchKeyEvent", {"type": "keyUp", **args})


@needs_chrome
def test_keyboard_focus_order_disclosures_dialog_and_route_focus(tmp_path) -> None:
    sample = _sample()
    sample["algorithms"] = sample["algorithms"][:1]
    sample["algorithms"][0]["metrics"] = sample["algorithms"][0]["metrics"][:1]
    with _server(tmp_path, sample) as server:
        profile = Path(tmp_path / "browser-profile")

        def interact(command, evaluate) -> None:
            command("Page.bringToFront")
            assert evaluate("document.activeElement?.tagName") == "H1"
            evaluate("document.querySelector('.skip-link').focus()")
            expected = evaluate("""(() => [...document.querySelectorAll('a[href], button, select, summary, input')]
              .flatMap((el, index) => el.tabIndex >= 0 && !el.disabled && el.getClientRects().length
                && getComputedStyle(el).visibility !== 'hidden' ? [index] : []))()""")
            assert len(expected) >= 8
            indices = []
            for next_index in expected[1:]:
                _key(command, "Tab", "Tab", 9)
                state = evaluate("""(() => {
                  const active = document.activeElement;
                  const order = [...document.querySelectorAll('a[href], button, select, summary, input')];
                  return {index: order.indexOf(active), tag: active.tagName, id: active.id,
                    focusVisible: active.matches(':focus-visible'), outline: getComputedStyle(active).outlineWidth};
                })()""")
                assert state["index"] == next_index and state["focusVisible"], f"focus={state['tag']}#{state['id']} {state}"
                assert float(state["outline"].removesuffix("px")) >= 3
                indices.append(state["index"])
            assert indices == expected[1:]

            evaluate("document.querySelector('.finding-group summary').focus()")
            assert evaluate("document.activeElement?.tagName") == "SUMMARY"
            _key(command, "Enter", "Enter", 13)
            assert evaluate("document.querySelector('.finding-group').open") is True
            _key(command, " ", "Space", 32)
            assert evaluate("document.querySelector('.finding-group').open") is False

            evaluate("document.getElementById('key-button').focus()")
            _key(command, "Enter", "Enter", 13)
            assert evaluate("document.getElementById('key-dialog').open") is True
            _key(command, "Escape", "Escape", 27)
            assert evaluate("document.getElementById('key-dialog').open") is False
            assert evaluate("document.activeElement?.id") == "key-button"

            time.sleep(0.1)  # Let the dialog's close event finish restoring focus.
            evaluate(f"location.hash = '#/sample/{ID12}/stability'")
            for _ in range(20):
                if evaluate("document.activeElement?.tagName === 'H1' && document.activeElement?.textContent === 'Stability'"):
                    break
                time.sleep(0.1)
            assert evaluate("document.activeElement?.tagName") == "H1"
            assert evaluate("document.activeElement?.textContent") == "Stability"

        _dom_cdp(
            [_chrome(), "--headless=new", "--disable-gpu", "--no-first-run",
             f"--user-data-dir={profile}", server.issue_link() + f"&route=/sample/{ID12}/findings"],
            profile, ok=lambda d: 'data-ready="1"' in d and 'class="finding-card"' in d,
            interact=interact,
        )
