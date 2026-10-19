"""Synthetic browser checks for Lab keyboard and responsive acceptance."""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

import pytest

from tests.lab.test_acceptance_synthetic_routes import (
    ID12,
    _run_marker,
    _sample,
    _server,
)
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
@pytest.mark.parametrize("size", ["1024,768", "768,1024", "360,740"])
def test_landing_responsive_clia_and_single_column_geometry(size, tmp_path) -> None:
    """AC6: the 1024px cap ends below 1024px; the landing band follows DOM order."""

    observed = {}
    with _server(tmp_path, _sample()) as server:
        profile = tmp_path / "browser-profile"

        def inspect(_command, evaluate) -> None:
            observed.update(evaluate("""(() => {
              const panel = document.querySelector('#main .clia-panel');
              const band = document.querySelector('#main .landing-band');
              const sections = [...band.children].map((node) => {
                const rect = node.getBoundingClientRect();
                return { top: rect.top, left: rect.left, right: rect.right };
              });
              const title = document.querySelector('.lab-title').getBoundingClientRect();
              const switcher = document.querySelector('.switcher').getBoundingClientRect();
              return {
                viewportWidth: window.innerWidth,
                panelHeight: panel.getBoundingClientRect().height,
                panelScrollHeight: panel.scrollHeight,
                panelClientHeight: panel.clientHeight,
                panelMaxHeight: getComputedStyle(panel).maxHeight,
                panelOverflowY: getComputedStyle(panel).overflowY,
                cliaColumns: getComputedStyle(panel.querySelector('.clia-short')).columnCount,
                bandColumns: getComputedStyle(band).gridTemplateColumns,
                sections,
                switcherBelowTitle: switcher.top >= title.bottom - 1
              };
            })()"""))

        _dom_cdp(
            [_chrome(), "--headless=new", "--disable-gpu", "--no-first-run",
             f"--user-data-dir={profile}", f"--window-size={size}",
             server.issue_link() + "&route=/"],
            profile,
            ok=lambda dom: 'data-ready="1"' in dom and
            re.search(r'<div class="clia-short">(?:(?!</div>).)*FLAG: LEGAL — verify', dom, re.DOTALL) is not None,
            interact=inspect,
            viewport=tuple(map(int, size.split(","))),
        )

    assert observed["panelScrollHeight"] <= observed["panelClientHeight"] + 1, (size, observed)
    assert observed["viewportWidth"] == int(size.split(",")[0]), (size, observed)
    assert observed["panelOverflowY"] == "visible", (size, observed)
    assert observed["cliaColumns"] == "1", (size, observed)
    assert len(observed["bandColumns"].split()) == 1, (size, observed)
    defended, curve, controls = observed["sections"]
    assert defended["top"] < curve["top"] < controls["top"], (size, observed)
    assert max(section["left"] for section in observed["sections"]) - min(
        section["left"] for section in observed["sections"]) <= 1, (size, observed)
    if size == "1024,768":
        assert observed["panelHeight"] <= 320, observed
    else:
        assert observed["panelMaxHeight"] == "none", (size, observed)
    if size == "360,740":
        assert observed["switcherBelowTitle"], observed


@needs_chrome
@pytest.mark.parametrize("size,columns", [
    ("1280,800", 4), ("1024,768", 2), ("768,1024", 2), ("360,740", 1),
])
def test_stability_columns_and_lens_wrap_at_exact_width(size, columns, tmp_path) -> None:
    observed = {}
    width, height = map(int, size.split(","))
    with _server(tmp_path, _sample()) as server:
        profile = tmp_path / "browser-profile"

        def inspect(_command, evaluate) -> None:
            observed.update(evaluate("""(() => {
              const grid = document.querySelector('#main .multiples');
              const nav = document.querySelector('#lenses');
              return {
                width: innerWidth,
                panels: grid.children.length,
                columns: getComputedStyle(grid).gridTemplateColumns.split(' ').length,
                navLines: new Set([...nav.querySelectorAll('a')]
                  .map((link) => Math.round(link.getBoundingClientRect().top))).size
              };
            })()"""))

        _dom_cdp(
            [_chrome(), "--headless=new", "--disable-gpu", "--no-first-run",
             f"--user-data-dir={profile}", f"--window-size={size}",
             server.issue_link() + f"&route=/sample/{ID12}/stability"],
            profile, ok=lambda dom: 'data-ready="1"' in dom and 'class="multiples"' in dom,
            interact=inspect, viewport=(width, height),
        )
    assert observed["width"] == width and observed["panels"] >= 4, observed
    assert observed["columns"] == columns, (size, observed)
    if width == 768:
        assert observed["navLines"] == 2, observed


@needs_chrome
def test_mobile_lens_deep_link_and_route_changes_reveal_current_tab(tmp_path) -> None:
    observed = {}
    with _server(tmp_path, _sample()) as server:
        profile = tmp_path / "browser-profile"

        def inspect(_command, evaluate) -> None:
            snapshot = """(() => {
              const nav = document.querySelector('#lenses');
              const active = nav.querySelector('[aria-current="page"]');
              const box = nav.getBoundingClientRect();
              const tab = active.getBoundingClientRect();
              return {
                width: innerWidth, current: active.textContent.trim(),
                scrollLeft: nav.scrollLeft, scrollWidth: nav.scrollWidth,
                clientWidth: nav.clientWidth, visible: tab.left >= box.left && tab.right <= box.right,
                hitCurrent: active.contains(document.elementFromPoint(
                  (tab.left + tab.right) / 2, (tab.top + tab.bottom) / 2)),
                leftFade: nav.classList.contains('more-left'),
                rightFade: nav.classList.contains('more-right'),
                mask: getComputedStyle(nav).maskImage,
                pageOverflow: document.documentElement.scrollWidth > document.documentElement.clientWidth + 1
              };
            })()"""

            def route(lens: str) -> dict:
                evaluate(f"location.hash = '#/sample/{ID12}/{lens}'")
                for _ in range(100):
                    if evaluate(f"document.querySelector('#lenses [aria-current=page]')?.getAttribute('href')?.endsWith('/{lens}')"):
                        break
                    time.sleep(0.05)
                return evaluate(snapshot)

            observed["methods"] = evaluate(snapshot)
            evaluate("document.querySelector('#lenses').scrollLeft = 0")
            time.sleep(0.05)
            observed["start"] = evaluate(snapshot)
            observed["findings"] = route("findings")
            observed["methods_again"] = route("methods")

        _dom_cdp(
            [_chrome(), "--headless=new", "--disable-gpu", "--no-first-run",
             f"--user-data-dir={profile}", "--window-size=360,740",
             server.issue_link() + f"&route=/sample/{ID12}/methods"],
            profile, ok=lambda dom: 'data-ready="1"' in dom and 'aria-current="page">Methods' in dom,
            interact=inspect, viewport=(360, 740),
        )

    initial = observed["methods"]
    assert initial["width"] == 360 and initial["scrollWidth"] > initial["clientWidth"], initial
    assert initial["current"] == "Methods" and initial["visible"] and initial["hitCurrent"], initial
    assert initial["scrollLeft"] > 0, initial
    assert initial["leftFade"] and not initial["rightFade"] and initial["mask"] != "none", initial
    start = observed["start"]
    assert not start["leftFade"] and start["rightFade"] and start["mask"] != "none", start
    findings = observed["findings"]
    assert findings["current"].startswith("Findings") and findings["visible"] and findings["hitCurrent"], findings
    assert not findings["leftFade"] and findings["rightFade"], findings
    again = observed["methods_again"]
    assert again["current"] == "Methods" and again["visible"] and again["hitCurrent"] and again["leftFade"], again
    assert all(not state["pageOverflow"] for state in observed.values()), observed


@needs_chrome
def test_mobile_nav_uses_final_width_after_lens_content_renders(tmp_path) -> None:
    """A classic page scrollbar may narrow the nav after its links are added."""

    observed = {}
    with _server(tmp_path, _sample()) as server:
        kind, source = server.app.assets["/assets/lab-views.js"]
        server.app.assets["/assets/lab-views.js"] = (kind, source + b"""
          const originalMethods = window.TracebackLabViews.methods;
          window.TracebackLabViews.methods = (sample) => {
            const nav = document.getElementById('lenses');
            nav.style.width = 'calc(100% - 47px)';
            nav.dispatchEvent(new Event('scroll'));
            return originalMethods(sample);
          };
        """)
        profile = tmp_path / "browser-profile"

        def inspect(_command, evaluate) -> None:
            observed.update(evaluate("""(() => {
              const nav = document.querySelector('#lenses');
              const tab = nav.querySelector('[aria-current="page"]').getBoundingClientRect();
              const box = nav.getBoundingClientRect();
              return {
                width: innerWidth, navWidth: nav.clientWidth,
                scroll: nav.scrollLeft, maxScroll: nav.scrollWidth - nav.clientWidth,
                visible: tab.left >= box.left && tab.right <= box.right,
                leftFade: nav.classList.contains('more-left'),
                rightFade: nav.classList.contains('more-right')
              };
            })()"""))

        _dom_cdp(
            [_chrome(), "--headless=new", "--disable-gpu", "--no-first-run",
             f"--user-data-dir={profile}", "--window-size=360,740",
             server.issue_link() + f"&route=/sample/{ID12}/methods"],
            profile, ok=lambda dom: 'data-ready="1"' in dom and 'aria-current="page">Methods' in dom,
            interact=inspect, viewport=(360, 740),
        )

    assert observed["width"] == 360 and observed["navWidth"] < 328, observed
    assert observed["visible"] and observed["leftFade"] and not observed["rightFade"], observed
    assert observed["scroll"] >= observed["maxScroll"] - 1, observed


@needs_chrome
@pytest.mark.parametrize("width", [375, 768, 1440])
def test_lens_navigation_labels_controls_and_history(width, tmp_path) -> None:
    """P03: full labels, discoverable mobile scrolling and retained route focus."""

    with _server(tmp_path, _sample()) as server:
        profile = tmp_path / "browser-profile"

        def snapshot(evaluate) -> dict:
            return evaluate("""(() => {
              const nav = document.getElementById('lenses');
              const controls = [...document.querySelectorAll('.lens-scroll-control')];
              const links = [...nav.querySelectorAll('a')];
              const active = nav.querySelector('[aria-current="page"]');
              const frame = nav.getBoundingClientRect();
              const tab = active.getBoundingClientRect();
              return {
                pageOverflow: document.documentElement.scrollWidth > document.documentElement.clientWidth + 1,
                navOverflow: nav.scrollWidth > nav.clientWidth + 1,
                scrollLeft: nav.scrollLeft,
                activeVisible: tab.left >= frame.left - 1 && tab.right <= frame.right + 1,
                labels: links.map((link) => ({nowrap: getComputedStyle(link).whiteSpace === 'nowrap',
                  shrink: getComputedStyle(link).flexShrink, height: link.getBoundingClientRect().height})),
                controls: controls.map((control) => ({name: control.getAttribute('aria-label'),
                  target: control.getAttribute('aria-controls'), disabled: control.disabled,
                  visible: getComputedStyle(control).display !== 'none',
                  width: control.getBoundingClientRect().width,
                  height: control.getBoundingClientRect().height}))
              };
            })()""")

        def inspect(command, evaluate) -> None:
            command("Page.bringToFront")
            initial = snapshot(evaluate)
            assert not initial["pageOverflow"] and initial["activeVisible"], initial
            assert len(initial["labels"]) == 9, initial
            assert all(row["nowrap"] and row["shrink"] == "0" for row in initial["labels"]), initial
            assert [row["name"] for row in initial["controls"]] == ["Previous views", "More views"]
            assert all(row["target"] == "lenses" for row in initial["controls"]), initial
            if width == 375:
                assert initial["navOverflow"] and initial["controls"][0]["visible"], initial
                assert all(row["width"] >= 43.5 and row["height"] >= 43.5 for row in initial["controls"]), initial
                assert not initial["controls"][0]["disabled"] and initial["controls"][1]["disabled"], initial
                evaluate("document.querySelector('.lens-scroll-prev').focus()")
                _key(command, "Enter", "Enter", 13)
                moved = snapshot(evaluate)
                assert moved["scrollLeft"] < initial["scrollLeft"] and not moved["controls"][1]["disabled"], moved
                evaluate("document.querySelector('.lens-scroll-next').focus()")
                _key(command, "Enter", "Enter", 13)
                at_end = snapshot(evaluate)
                assert at_end["scrollLeft"] > moved["scrollLeft"] and at_end["controls"][1]["disabled"], at_end
                assert evaluate("document.activeElement === document.querySelector('.lens-scroll-next')")
                _key(command, "Enter", "Enter", 13)
                assert snapshot(evaluate)["scrollLeft"] == at_end["scrollLeft"]
                evaluate("document.querySelector('.lens-scroll-prev').click()")
                evaluate("document.querySelector('#lenses [aria-current=page]').focus()")
                assert snapshot(evaluate)["activeVisible"]
                evaluate("document.querySelector('.lens-scroll-prev').focus()")
                for _ in range(10):
                    if snapshot(evaluate)["controls"][0]["disabled"]:
                        break
                    _key(command, "Enter", "Enter", 13)
                else:
                    pytest.fail("Previous views did not reach the first edge")
                at_start = snapshot(evaluate)
                assert evaluate("document.activeElement === document.querySelector('.lens-scroll-prev')")
                _key(command, "Enter", "Enter", 13)
                assert snapshot(evaluate)["scrollLeft"] == at_start["scrollLeft"]
                assert evaluate(f"location.hash === '#/sample/{ID12}/methods'")
                assert evaluate("document.querySelector('#main h1')?.textContent === 'Methods'")
            else:
                assert all(not row["visible"] for row in initial["controls"]), initial

            evaluate(f"location.hash = '#/sample/{ID12}/findings'")
            for _ in range(40):
                if evaluate("document.querySelector('#main h1')?.textContent === 'Findings'"):
                    break
                time.sleep(0.05)
            else:
                pytest.fail("Findings route did not render")
            evaluate("history.back()")
            for _ in range(40):
                if evaluate("document.querySelector('#main h1')?.textContent === 'Methods'"):
                    break
                time.sleep(0.05)
            else:
                pytest.fail("browser Back did not restore Methods")
            assert evaluate("document.activeElement === document.querySelector('#main h1')")
            assert snapshot(evaluate)["activeVisible"]
            if width == 375:
                evaluate("location.hash = '#/'")
                for _ in range(40):
                    if evaluate("document.querySelector('#main h1')?.textContent.startsWith('Traceback Lab')"):
                        break
                    time.sleep(0.05)
                else:
                    pytest.fail("landing route did not render")
                assert evaluate("document.querySelector('.lens-navigation').hidden")
                evaluate(f"location.hash = '#/sample/{ID12}/methods'")
                for _ in range(40):
                    if evaluate("document.querySelector('#main h1')?.textContent === 'Methods'"):
                        break
                    time.sleep(0.05)
                else:
                    pytest.fail("landing-to-lens route did not render")
                assert snapshot(evaluate)["activeVisible"]

        _dom_cdp(
            [_chrome(), "--headless=new", "--disable-gpu", "--no-first-run",
             f"--user-data-dir={profile}", f"--window-size={width},800",
             server.issue_link() + f"&route=/sample/{ID12}/methods"],
            profile, ok=lambda dom: 'data-ready="1"' in dom and 'aria-current="page">Methods' in dom,
            interact=inspect, viewport=(width, 800),
        )


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
              if (document.body.dataset.cspViolations !== '0')
                failures.push(`${route}: CSP violations ${document.body.dataset.cspViolations} (${document.body.dataset.cspLast || 'unknown'})`);
              if (document.documentElement.scrollWidth > document.documentElement.clientWidth + 1)
                failures.push(`${route}: page scroll ${document.documentElement.scrollWidth}/${document.documentElement.clientWidth}`);
              for (const table of document.querySelectorAll('#main table')) {
                const header = [...table.querySelectorAll('thead th')].map((cell) => cell.textContent.trim()).join('|');
                if (header !== 'Algorithm|Status|Duration|Run log|Detail') continue;
                for (const row of table.querySelectorAll('tbody tr'))
                  if (row.cells.length !== 5)
                    failures.push(`${route}: Run row has ${row.cells.length} cells under 5 headers`);
              }
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
@pytest.mark.parametrize("size", ["1280,800", "1024,768", "768,1024", "360,740"])
def test_wide_methods_table_stays_inside_its_wrapper(size, tmp_path) -> None:
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
        dom = _dom(server, tmp_path / "browser", size=size,
                   route=f"/sample/{ID12}/methods", want=lambda d: 'data-table-scroll' in d)
    if size == "360,740":
        assert 'data-table-scroll="true"' in dom
    assert 'data-table-overflow="auto"' in dom
    assert 'data-page-overflow="false"' in dom


@needs_chrome
@pytest.mark.parametrize("size", ["1280,800", "1024,768", "768,1024", "360,740"])
def test_revealed_controls_and_each_signals_tab_keep_target_and_page_bounds(size, tmp_path) -> None:
    """Audit the rendered states omitted by the default-route geometry pass."""

    routes = ("home", "findings", "trust-qc", "stability", "signals", "compare", "body-map",
              "agreement", "evidence", "methods")
    headings = {"home": f"Traceback Lab — {ID12}", "findings": "Findings",
                "trust-qc": "Trust / QC", "stability": "Stability",
                "signals": "Signals", "compare": "Compare", "body-map": "Body map",
                "agreement": "Agreement", "evidence": "Evidence", "methods": "Methods"}
    with _server(tmp_path, _sample()) as server:
        profile = tmp_path / "browser-profile"

        def interact(command, evaluate) -> None:
            command("Page.bringToFront")
            states = []

            def audit(label: str) -> None:
                result = evaluate("""(() => {
                  const visible = (el) => { const style = getComputedStyle(el);
                    return el.getClientRects().length > 0 && style.display !== 'none'
                      && style.visibility !== 'hidden'; };
                  return {
                    width: document.documentElement.clientWidth,
                    scrollWidth: document.documentElement.scrollWidth,
                    small: [...document.querySelectorAll('a[href], button, select, summary, input, [tabindex], .table-wrap')]
                      .filter((el) => (el.tabIndex >= 0 ||
                        (el.matches('.table-wrap') && el.scrollWidth > el.clientWidth + 1)) && visible(el)).flatMap((el) => {
                        const rect = el.getBoundingClientRect();
                        return rect.width < 43.5 || rect.height < 43.5
                          ? [`${el.tagName.toLowerCase()} ${el.textContent.trim().slice(0, 28)} ${Math.round(rect.width)}x${Math.round(rect.height)}`]
                          : []; }),
                  };
                })()""")
                assert result["scrollWidth"] <= result["width"] + 1, (size, label, result)
                assert not result["small"], (size, label, result["small"])
                states.append(label)

            def reveal_all() -> None:
                evaluate("""(() => { for (const button of document.querySelectorAll('#main button[aria-controls]')) {
                    const target = document.getElementById(button.getAttribute('aria-controls'));
                    if (target?.hidden && button.getClientRects().length) button.click();
                  }
                  for (const details of document.querySelectorAll('#main details'))
                  details.open = true; })()""")
                time.sleep(0.05)

            for route in routes:
                path = "/" if route == "home" else f"/sample/{ID12}/{route}"
                evaluate(f"location.hash = '#{path}'")
                for _ in range(60):
                    if evaluate(f"document.querySelector('#main h1')?.textContent.trim() === {json.dumps(headings[route])}"):
                        break
                    time.sleep(0.05)
                else:
                    pytest.fail(f"{size}: {route} did not render")
                if route == "home":
                    for _ in range(60):
                        if evaluate("document.querySelector('#clia-full')?.children.length > 0"):
                            break
                        time.sleep(0.05)
                    else:
                        pytest.fail(f"{size}: CLIA copy did not load")
                time.sleep(0.05)
                if route == "signals":
                    for label in ("Fragmentomics", "Copy number", "Methylation"):
                        selected = evaluate(f"""(() => {{ const tab = [...document.querySelectorAll('#main .subtab')]
                          .find((item) => item.textContent === {json.dumps(label)});
                          tab?.click(); return tab?.getAttribute('aria-pressed'); }})()""")
                        assert selected == "true", (size, label)
                        audit(f"signals/{label}")
                        reveal_all()
                        audit(f"signals/{label}/revealed")
                else:
                    reveal_all()
                    audit(f"{route}/revealed")

            evaluate("document.getElementById('key-button').focus()")
            _key(command, "Enter", "Enter", 13)
            assert evaluate("document.getElementById('key-dialog').open")
            audit("key-dialog/open")
            _key(command, "Escape", "Escape", 27)
            assert len(states) == len(routes) - 1 + 3 * 2 + 1, states

        _dom_cdp(
            [_chrome(), "--headless=new", "--disable-gpu", "--no-first-run",
             f"--user-data-dir={profile}", f"--window-size={size}",
             server.issue_link() + f"&route=/sample/{ID12}/findings"],
            profile, ok=lambda d: 'data-ready="1"' in d and 'class="finding-card"' in d,
            interact=interact, timeout=180,
            viewport=tuple(map(int, size.split(","))),
        )


def _key(command, key: str, code: str, virtual: int) -> None:
    args = {"key": key, "code": code, "windowsVirtualKeyCode": virtual, "nativeVirtualKeyCode": virtual}
    if virtual in (13, 32):
        args["text"] = "\r" if virtual == 13 else " "
    command("Input.dispatchKeyEvent", {"type": "keyDown" if virtual in (13, 32) else "rawKeyDown", **args})
    command("Input.dispatchKeyEvent", {"type": "keyUp", **args})


def _walk_live_tab_order(command, evaluate, selector: str, label: str) -> list[int]:
    """Prove each Tab reaches the next control enabled at that keypress."""

    expression = f"""(() => {{
      const all = [...document.querySelectorAll({json.dumps(selector)})];
      const current = all.indexOf(document.activeElement);
      const eligible = (el) =>
        (el.tabIndex >= 0 || (el.matches('.table-wrap') && el.scrollWidth > el.clientWidth + 1))
        && !el.disabled && el.getClientRects().length
        && getComputedStyle(el).visibility !== 'hidden';
      const next = all.findIndex((el, index) => index > current && eligible(el));
      const active = document.activeElement;
      return {{current, next, count: all.length,
        html: active?.outerHTML.slice(0, 180),
        visible: active?.matches(':focus-visible'),
        outline: active ? getComputedStyle(active).outlineWidth : '0px'}};
    }})()"""
    visited = []
    maximum = evaluate(expression)["count"] + 10
    for _ in range(maximum):
        before = evaluate(expression)
        if before["next"] < 0:
            return visited
        _key(command, "Tab", "Tab", 9)
        after = evaluate(expression)
        assert after["current"] == before["next"] and after["visible"], (label, before, after)
        assert float(after["outline"].removesuffix("px")) >= 3, (label, after)
        visited.append(after["current"])
    pytest.fail(f"{label}: Tab order did not reach its final control")


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
            visited = _walk_live_tab_order(command, evaluate,
                "a[href], button, select, summary, input", "findings")
            assert len(visited) >= 7

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
            time.sleep(0.1)  # Let the dialog's close event finish restoring focus.
            assert evaluate("document.activeElement?.id") == "key-button"
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
            interact=interact, timeout=180,
        )


@needs_chrome
def test_live_status_changes_keep_focus_and_open_disclosure(tmp_path) -> None:
    sample = _sample()
    phase = {"state": "idle"}
    with _server(tmp_path, sample) as server:
        kind, source = server.app.assets["/assets/lab.js"]
        server.app.assets["/assets/lab.js"] = (
            kind, source.replace(b"const POLL_MS = 15000;", b"const POLL_MS = 500;"),
        )

        def status():
            if phase["state"] == "disconnected":
                raise OSError("synthetic disconnect")
            row = {"generation": 2 if phase["state"] in ("stale", "active-after-stale") else 1,
                   "active": None, "interrupted": None, "library": sample["library_health"]}
            if phase["state"] in ("active", "active-after-stale"):
                row["active"] = {**_run_marker(), "started_utc": "2026-10-16T00:00:00Z"}
            elif phase["state"] == "interrupted":
                row["interrupted"] = _run_marker()
            return row

        server.app.api.status = status

        def interact(command, evaluate) -> None:
            evaluate("""(() => {
              window.pinnedMain = document.getElementById('main');
              window.pinnedSummary = document.querySelector('.finding-group summary');
              window.pinnedSummary.closest('details').open = true;
              window.pinnedSummary.focus();
            })()""")

            def expect_status(fragment):
                for _ in range(30):
                    if evaluate(f"document.getElementById('status').textContent.includes({json.dumps(fragment)})"):
                        break
                    time.sleep(0.2)
                else:
                    pytest.fail(f"status did not update to {fragment!r}")
                assert evaluate("document.getElementById('main') === window.pinnedMain")
                assert evaluate("document.activeElement === window.pinnedSummary")
                assert evaluate("window.pinnedSummary.closest('details').open")

            phase["state"] = "active"
            expect_status("A Lab run is in progress")
            phase["state"] = "interrupted"
            expect_status("stopped during bam-qc")
            phase["state"] = "stale"
            expect_status("Results changed on disk")
            evaluate("""(() => {
              window.pinnedReload = document.querySelector('#status button.reload');
              window.pinnedReload.focus();
            })()""")
            phase["state"] = "active-after-stale"
            for _ in range(30):
                if evaluate("document.getElementById('status').textContent.includes('A Lab run is in progress')"):
                    break
                time.sleep(0.2)
            else:
                pytest.fail("active run after stale generation did not appear")
            assert evaluate("document.querySelector('#status button.reload') === window.pinnedReload")
            assert evaluate("document.activeElement === window.pinnedReload")
            evaluate("window.pinnedSummary.focus()")
            phase["state"] = "disconnected"
            expect_status("Lab server not responding")

            phase["state"] = "idle"
            for _ in range(30):
                if evaluate("document.body.dataset.disconnected === '0'"):
                    break
                time.sleep(0.2)
            else:
                pytest.fail("Lab page did not reconnect")
            evaluate(f"location.hash = '#/sample/{ID12}/trust-qc'")
            for _ in range(30):
                if evaluate("document.querySelector('#main h1')?.textContent === 'Trust / QC'"):
                    break
                time.sleep(0.2)
            else:
                pytest.fail("Trust / QC did not render")
            assert evaluate("""getComputedStyle(document.querySelector('[data-run-algorithm="bam-qc"] [data-run-note]')).display === 'none'""")
            evaluate("""(() => {
              window.pinnedRunButton = document.querySelector('button[aria-label="Run log line for bam-qc"]');
              window.pinnedRunButton.focus();
            })()""")
            phase["state"] = "active"
            for _ in range(30):
                if evaluate("""document.querySelector('[data-run-algorithm="bam-qc"] [data-run-note]')?.textContent === 'latest run in progress'"""):
                    break
                time.sleep(0.2)
            else:
                pytest.fail("Trust / QC run note did not update")
            assert evaluate("""getComputedStyle(document.querySelector('[data-run-algorithm="bam-qc"] [data-run-note]')).display !== 'none'""")
            assert evaluate("document.activeElement === window.pinnedRunButton")
            phase["state"] = "interrupted"
            for _ in range(30):
                if evaluate("""document.querySelector('[data-run-algorithm="bam-qc"] [data-run-note]')?.textContent === 'latest run interrupted'"""):
                    break
                time.sleep(0.2)
            else:
                pytest.fail("Trust / QC interrupted note did not update")
            assert evaluate("document.activeElement === window.pinnedRunButton")

            phase["state"] = "idle"
            for _ in range(30):
                if evaluate("""getComputedStyle(document.querySelector('[data-run-algorithm="bam-qc"] [data-run-note]')).display === 'none'"""):
                    break
                time.sleep(0.2)
            else:
                pytest.fail("Trust / QC run note stayed visible after the run ended")
            evaluate(f"location.hash = '#/sample/{ID12}/signals'")
            for _ in range(30):
                if evaluate("document.querySelector('#main h1')?.textContent === 'Signals'"):
                    break
                time.sleep(0.2)
            else:
                pytest.fail("Signals did not render")
            evaluate("""(() => {
              window.pinnedSubtab = [...document.querySelectorAll('.subtab')]
                .find((button) => button.textContent === 'Copy number');
              window.pinnedSubtab.click();
              window.pinnedSubtab.focus();
            })()""")
            assert evaluate("window.pinnedSubtab.getAttribute('aria-pressed') === 'true'")
            phase["state"] = "active"
            for _ in range(30):
                if evaluate("document.getElementById('status').textContent.includes('A Lab run is in progress')"):
                    break
                time.sleep(0.2)
            else:
                pytest.fail("Signals status did not update")
            assert evaluate("document.activeElement === window.pinnedSubtab")
            assert evaluate("window.pinnedSubtab.getAttribute('aria-pressed') === 'true'")

        profile = tmp_path / "browser-profile"
        _dom_cdp(
            [_chrome(), "--headless=new", "--disable-gpu", "--no-first-run",
             f"--user-data-dir={profile}", server.issue_link() + f"&route=/sample/{ID12}/findings"],
            profile, ok=lambda d: 'data-ready="1"' in d and 'class="finding-card"' in d,
            interact=interact, timeout=180,
        )


@needs_chrome
def test_key_dialog_restores_invoker_after_unfocused_click(tmp_path) -> None:
    """A click that does not focus its button still returns focus to Key on Escape."""

    with _server(tmp_path, _sample()) as server:
        def inspect(command, evaluate) -> None:
            opened = evaluate("""(() => {
              const title = document.querySelector('main h1');
              const button = document.getElementById('key-button');
              const dialog = document.getElementById('key-dialog');
              title.focus();
              const titleFocused = document.activeElement === title;
              window.keyDialogCloseObserved = false;
              dialog.addEventListener('close', () => { window.keyDialogCloseObserved = true; }, {once: true});
              button.click();
              return {titleFocused, open: dialog.open, focusInside: dialog.contains(document.activeElement)};
            })()""")
            assert opened == {"titleFocused": True, "open": True, "focusInside": True}
            _key(command, "Escape", "Escape", 27)
            for _ in range(20):
                if evaluate("window.keyDialogCloseObserved === true"):
                    break
                time.sleep(0.05)
            else:
                pytest.fail("Key dialog close event did not fire")
            assert evaluate("document.activeElement === document.getElementById('key-button')")

        profile = tmp_path / "browser-profile"
        _dom_cdp(
            [_chrome(), "--headless=new", "--disable-gpu", "--no-first-run",
             f"--user-data-dir={profile}", server.issue_link() + f"&route=/sample/{ID12}/findings"],
            profile, ok=lambda d: 'data-ready="1"' in d and 'class="finding-card"' in d,
            interact=inspect, timeout=90,
        )


@needs_chrome
def test_key_dialog_close_does_not_steal_new_focus(tmp_path) -> None:
    """A queued close event must not undo focus travel after the dialog closes."""

    with _server(tmp_path, _sample()) as server:
        def inspect(_command, evaluate) -> None:
            evaluate("""(() => {
              const button = document.getElementById('key-button');
              const dialog = document.getElementById('key-dialog');
              const select = document.getElementById('sample-switcher');
              window.closeFocusOrder = [];
              document.addEventListener('focusin', (event) =>
                window.closeFocusOrder.push(event.target.id), {capture: true});
              button.focus();
              dialog.showModal();
              dialog.addEventListener('close', () => { window.keyDialogCloseObserved = true; }, {once: true});
              dialog.close();
              select.focus();
              window.focusMovedBeforeCloseEvent = document.activeElement === select;
            })()""")
            assert evaluate("window.focusMovedBeforeCloseEvent")
            for _ in range(20):
                if evaluate("window.keyDialogCloseObserved === true"):
                    break
                time.sleep(0.05)
            else:
                pytest.fail("Key dialog close event did not fire")
            assert evaluate("document.activeElement === document.getElementById('sample-switcher')"), evaluate(
                "window.closeFocusOrder"
            )

        profile = tmp_path / "browser-profile"
        _dom_cdp(
            [_chrome(), "--headless=new", "--disable-gpu", "--no-first-run",
             f"--user-data-dir={profile}", server.issue_link() + f"&route=/sample/{ID12}/trust-qc"],
            profile, ok=lambda d: 'data-ready="1"' in d and "Run log line for bam-qc" in d,
            interact=inspect, timeout=90,
        )


@needs_chrome
def test_disconnected_page_blocks_fetch_controls_until_reconnected(tmp_path) -> None:
    """AC21: a stalled server must not leave the run-log fetch button live."""

    phase = {"down": False}
    with _server(tmp_path, _sample()) as server:
        kind, source = server.app.assets["/assets/lab.js"]
        server.app.assets["/assets/lab.js"] = (
            kind, source.replace(b"const POLL_MS = 15000;", b"const POLL_MS = 500;"),
        )
        original_status = server.app.api.status

        def status():
            if phase["down"]:
                raise OSError("synthetic disconnect")
            return original_status()

        server.app.api.status = status

        def inspect(command, evaluate) -> None:
            evaluate("""(() => {
              const original = window.fetch;
              window.runLogFetches = 0;
              window.fetch = (...args) => {
                if (String(args[0]).includes('/run-log/')) window.runLogFetches += 1;
                return original.apply(window, args);
              };
            })()""")
            button = "document.querySelector('button[aria-label=\"Run log line for bam-qc\"]')"
            assert evaluate(f"!!{button} && !{button}.disabled")
            evaluate(f"{button}.focus()")
            assert evaluate(f"document.activeElement === {button}")

            phase["down"] = True
            for _ in range(40):
                if evaluate("document.body.dataset.disconnected === '1'"):
                    break
                time.sleep(0.2)
            else:
                pytest.fail("three missed polls did not mark the page disconnected")
            assert evaluate(f"document.activeElement === {button}")
            assert evaluate(f"{button}.getAttribute('aria-disabled') === 'true'")
            assert evaluate("""(() => {
              const other = [...document.querySelectorAll('button[data-requires-server]')]
                .filter((control) => control !== document.activeElement);
              return other.length > 0 && other.every((control) => control.disabled);
            })()""")
            assert evaluate("document.getElementById('sample-switcher').disabled")
            assert evaluate("!document.getElementById('key-button').disabled")
            evaluate(f"{button}.click()")
            assert evaluate("window.runLogFetches === 0")

            phase["down"] = False
            for _ in range(40):
                if evaluate("document.body.dataset.disconnected === '0'"):
                    break
                time.sleep(0.2)
            else:
                pytest.fail("the page did not reconnect")
            assert evaluate(f"!{button}.disabled && !document.getElementById('sample-switcher').disabled")
            assert evaluate("""[...document.querySelectorAll('button[data-requires-server]')]
              .every((control) => !control.disabled && !control.hasAttribute('aria-disabled'))""")
            evaluate(f"{button}.click()")
            assert evaluate("window.runLogFetches === 1")

            evaluate("document.getElementById('sample-switcher').focus()")
            phase["down"] = True
            for _ in range(40):
                if evaluate("document.body.dataset.disconnected === '1'"):
                    break
                time.sleep(0.2)
            else:
                pytest.fail("the page did not disconnect again")
            assert evaluate("document.activeElement === document.getElementById('sample-switcher')")
            evaluate("""(() => {
              const select = document.getElementById('sample-switcher');
              window.selectedBeforeDisconnect = select.value;
              window.hashBeforeDisconnect = location.hash;
              select.add(new Option('Synthetic second sample', 'ffffffffffff'));
            })()""")
            _key(command, "ArrowDown", "ArrowDown", 40)
            assert evaluate("document.getElementById('sample-switcher').value === window.selectedBeforeDisconnect")
            assert evaluate("location.hash === window.hashBeforeDisconnect")
            _key(command, "Tab", "Tab", 9)
            assert evaluate("document.activeElement === document.getElementById('key-button')")
            _key(command, "Enter", "Enter", 13)
            assert evaluate("document.getElementById('key-dialog').open")
            _key(command, "Escape", "Escape", 27)
            assert evaluate("!document.getElementById('key-dialog').open")
            shift_tab = {"key": "Tab", "code": "Tab", "windowsVirtualKeyCode": 9,
                         "nativeVirtualKeyCode": 9, "modifiers": 8}
            command("Input.dispatchKeyEvent", {"type": "rawKeyDown", **shift_tab})
            command("Input.dispatchKeyEvent", {"type": "keyUp", **shift_tab})
            assert evaluate("document.activeElement !== document.getElementById('key-button')")

        profile = tmp_path / "browser-profile"
        _dom_cdp(
            [_chrome(), "--headless=new", "--disable-gpu", "--no-first-run",
             f"--user-data-dir={profile}", server.issue_link() + f"&route=/sample/{ID12}/trust-qc"],
            profile, ok=lambda d: 'data-ready="1"' in d and "Run log line for bam-qc" in d,
            interact=inspect, timeout=90,
        )


@needs_chrome
def test_disconnected_navigation_keeps_url_and_view_in_sync(tmp_path) -> None:
    """AC21: Enter on a focused lens link cannot queue a hidden route change."""

    phase = {"down": False}
    with _server(tmp_path, _sample()) as server:
        kind, source = server.app.assets["/assets/lab.js"]
        server.app.assets["/assets/lab.js"] = (
            kind, source.replace(b"const POLL_MS = 15000;", b"const POLL_MS = 500;"),
        )
        original_status = server.app.api.status

        def status():
            if phase["down"]:
                raise OSError("synthetic disconnect")
            return original_status()

        server.app.api.status = status

        def inspect(command, evaluate) -> None:
            command("Page.bringToFront")
            evaluate("""(() => {
              window.trustHash = location.hash;
              window.stabilityLink = document.querySelector('#lenses a[href$="/stability"]');
              window.stabilityLink.focus();
            })()""")
            assert evaluate("document.activeElement === window.stabilityLink")
            phase["down"] = True
            for _ in range(40):
                if evaluate("document.body.dataset.disconnected === '1'"):
                    break
                time.sleep(0.2)
            else:
                pytest.fail("three missed polls did not mark the page disconnected")
            assert evaluate("window.stabilityLink.getAttribute('aria-disabled') === 'true'")
            _key(command, "Enter", "Enter", 13)
            assert evaluate("location.hash === window.trustHash")
            assert evaluate("document.querySelector('#main h1')?.textContent === 'Trust / QC'")

            phase["down"] = False
            for _ in range(40):
                if evaluate("document.body.dataset.disconnected === '0'"):
                    break
                time.sleep(0.2)
            else:
                pytest.fail("the page did not reconnect")
            _key(command, "Enter", "Enter", 13)
            for _ in range(40):
                if evaluate("document.querySelector('#main h1')?.textContent === 'Stability'"):
                    break
                time.sleep(0.2)
            else:
                pytest.fail("a re-enabled lens link did not navigate")

            phase["down"] = True
            for _ in range(40):
                if evaluate("document.body.dataset.disconnected === '1'"):
                    break
                time.sleep(0.2)
            else:
                pytest.fail("the page did not disconnect again")
            evaluate("""(() => {
              window.backHashes = [];
              window.backAfterHash = null;
              window.addEventListener('popstate', () => {
                window.backHashes.push(location.hash);
                setTimeout(() => { window.backAfterHash = location.hash; }, 0);
              }, {once: true});
              history.back();
            })()""")
            for _ in range(20):
                if evaluate("window.backAfterHash !== null"):
                    break
                time.sleep(0.05)
            assert evaluate("window.backHashes.length === 1")
            assert evaluate("window.backHashes[0].endsWith('/trust-qc')")
            assert evaluate("window.backAfterHash.endsWith('/stability')")
            assert evaluate("location.hash.endsWith('/stability')")
            assert evaluate("document.querySelector('#main h1')?.textContent === 'Stability'")

        profile = tmp_path / "browser-profile"
        _dom_cdp(
            [_chrome(), "--headless=new", "--disable-gpu", "--no-first-run",
             f"--user-data-dir={profile}", server.issue_link() + f"&route=/sample/{ID12}/trust-qc"],
            profile, ok=lambda d: 'data-ready="1"' in d and "Run log line for bam-qc" in d,
            interact=inspect, timeout=90,
        )


@needs_chrome
def test_pending_navigation_is_cancelled_when_status_disconnects(tmp_path) -> None:
    """A held sample response cannot move the URL ahead of the visible route."""

    phase = {"down": False}
    with _server(tmp_path, _sample()) as server:
        kind, source = server.app.assets["/assets/lab.js"]
        server.app.assets["/assets/lab.js"] = (
            kind, source.replace(b"const POLL_MS = 15000;", b"const POLL_MS = 500;"),
        )
        original_status = server.app.api.status

        def status():
            if phase["down"]:
                raise OSError("synthetic disconnect")
            return original_status()

        server.app.api.status = status

        def inspect(_command, evaluate) -> None:
            evaluate("""(() => {
              window.trustHash = location.hash;
              const original = window.fetch;
              window.fetch = (...args) => String(args[0]) === '/api/samples/aaaaaaaaaaaa'
                ? new Promise((resolve) => { window.releaseUnknownSample = () => resolve(new Response(
                    JSON.stringify({found: false, candidates: []}), {status: 404,
                    headers: {'Content-Type': 'application/json'}})); })
                : original.apply(window, args);
              location.hash = '#/sample/aaaaaaaaaaaa/findings';
            })()""")
            for _ in range(20):
                if evaluate("typeof window.releaseUnknownSample === 'function'"):
                    break
                time.sleep(0.05)
            else:
                pytest.fail("unknown sample request was not held")
            phase["down"] = True
            for _ in range(40):
                if evaluate("document.body.dataset.disconnected === '1'"):
                    break
                time.sleep(0.2)
            else:
                pytest.fail("the page did not disconnect")
            assert evaluate("location.hash === window.trustHash")
            assert evaluate("document.querySelector('#main h1')?.textContent === 'Trust / QC'")
            evaluate("window.releaseUnknownSample()")
            time.sleep(0.1)
            assert evaluate("location.hash === window.trustHash")
            assert evaluate("document.querySelector('#main h1')?.textContent === 'Trust / QC'")
            assert evaluate("document.querySelector('.lab-banner a')?.getAttribute('aria-disabled') === 'true'")
            assert evaluate("document.querySelector('.lab-title')?.getAttribute('aria-disabled') === 'true'")
            phase["down"] = False
            for _ in range(40):
                if evaluate("document.body.dataset.disconnected === '0'"):
                    break
                time.sleep(0.2)
            else:
                pytest.fail("the page did not reconnect")
            assert evaluate("location.hash === window.trustHash")
            assert evaluate("document.querySelector('#main h1')?.textContent === 'Trust / QC'")
            assert evaluate("!document.querySelector('.lab-title')?.hasAttribute('aria-disabled')")

        profile = tmp_path / "browser-profile"
        _dom_cdp(
            [_chrome(), "--headless=new", "--disable-gpu", "--no-first-run",
             f"--user-data-dir={profile}", server.issue_link() + f"&route=/sample/{ID12}/trust-qc"],
            profile, ok=lambda d: 'data-ready="1"' in d and "Run log line for bam-qc" in d,
            interact=inspect, timeout=90,
        )


@needs_chrome
def test_hung_status_observations_count_as_missed_polls(tmp_path) -> None:
    """Three unanswered polls disconnect the page within the polling budget."""

    with _server(tmp_path, _sample()) as server:
        kind, source = server.app.assets["/assets/lab.js"]
        server.app.assets["/assets/lab.js"] = (
            kind, source.replace(b"const POLL_MS = 15000;", b"const POLL_MS = 500;"),
        )

        def inspect(_command, evaluate) -> None:
            evaluate("""(() => {
              const original = window.fetch;
              window.hangStatus = true;
              window.fetch = (...args) => window.hangStatus && String(args[0]) === '/api/status'
                ? new Promise(() => {}) : original.apply(window, args);
            })()""")
            for _ in range(40):
                if evaluate("document.body.dataset.disconnected === '1'"):
                    break
                time.sleep(0.2)
            else:
                pytest.fail("three unanswered status polls did not disconnect the page")
            assert evaluate("document.getElementById('status').textContent.includes('Lab server not responding')")
            evaluate("window.hangStatus = false")
            for _ in range(40):
                if evaluate("document.body.dataset.disconnected === '0'"):
                    break
                time.sleep(0.2)
            else:
                pytest.fail("the page did not reconnect after status responses resumed")

        profile = tmp_path / "browser-profile"
        _dom_cdp(
            [_chrome(), "--headless=new", "--disable-gpu", "--no-first-run",
             f"--user-data-dir={profile}", server.issue_link() + f"&route=/sample/{ID12}/trust-qc"],
            profile, ok=lambda d: 'data-ready="1"' in d and "Run log line for bam-qc" in d,
            interact=inspect, timeout=90,
        )


@needs_chrome
def test_late_compare_picker_is_disabled_until_status_recovers(tmp_path) -> None:
    """A cohort response arriving after disconnection cannot add a live picker."""

    phase = {"down": False}
    with _server(tmp_path, _sample()) as server:
        kind, source = server.app.assets["/assets/lab.js"]
        server.app.assets["/assets/lab.js"] = (
            kind, source.replace(b"const POLL_MS = 15000;", b"const POLL_MS = 500;"),
        )
        original_status = server.app.api.status

        def status():
            if phase["down"]:
                raise OSError("synthetic disconnect")
            return original_status()

        server.app.api.status = status

        def inspect(_command, evaluate) -> None:
            evaluate(f"""(() => {{
              const original = window.fetch;
              window.fetch = (...args) => String(args[0]) === '/api/cohorts'
                ? new Promise((resolve) => {{ window.releaseCohorts = () => resolve(new Response(
                    JSON.stringify({{cohorts: [{{cohort_id: 'synthetic', cohort_version: 'synthetic:1', member_n: 10}}], unavailable: []}}),
                    {{status: 200, headers: {{'Content-Type': 'application/json'}}}})); }})
                : original.apply(window, args);
              location.hash = '#/sample/{ID12}/compare';
            }})()""")
            for _ in range(20):
                if evaluate("typeof window.releaseCohorts === 'function'"):
                    break
                time.sleep(0.05)
            else:
                pytest.fail("cohort listing request was not held")
            phase["down"] = True
            for _ in range(40):
                if evaluate("document.body.dataset.disconnected === '1'"):
                    break
                time.sleep(0.2)
            else:
                pytest.fail("the page did not disconnect")
            evaluate("window.releaseCohorts()")
            for _ in range(20):
                if evaluate("!!document.querySelector('select[aria-label=\"Cohort generation\"]')"):
                    break
                time.sleep(0.05)
            else:
                pytest.fail("late cohort picker did not render")
            picker = "document.querySelector('select[aria-label=\"Cohort generation\"]')"
            assert evaluate(f"{picker}.disabled && {picker}.getAttribute('aria-disabled') === 'true'")
            phase["down"] = False
            for _ in range(40):
                if evaluate("document.body.dataset.disconnected === '0'"):
                    break
                time.sleep(0.2)
            else:
                pytest.fail("the page did not reconnect")
            assert evaluate(f"!{picker}.disabled && !{picker}.hasAttribute('aria-disabled')")

        profile = tmp_path / "browser-profile"
        _dom_cdp(
            [_chrome(), "--headless=new", "--disable-gpu", "--no-first-run",
             f"--user-data-dir={profile}", server.issue_link() + f"&route=/sample/{ID12}/trust-qc"],
            profile, ok=lambda d: 'data-ready="1"' in d and "Run log line for bam-qc" in d,
            interact=inspect, timeout=90,
        )


@needs_chrome
def test_keyboard_traversal_and_revealed_controls_on_every_sample_route(tmp_path) -> None:
    routes = {
        "home": f"Traceback Lab — {ID12}",
        "findings": "Findings", "trust-qc": "Trust / QC", "stability": "Stability",
        "signals": "Signals", "compare": "Compare", "body-map": "Body map",
        "agreement": "Agreement", "evidence": "Evidence", "methods": "Methods",
    }
    with _server(tmp_path, _sample()) as server:
        profile = Path(tmp_path / "browser-profile")

        def interact(command, evaluate) -> None:
            command("Page.bringToFront")
            for route, heading in routes.items():
                path = "/" if route == "home" else f"/sample/{ID12}/{route}"
                evaluate(f"location.hash = '#{path}'")
                for _ in range(60):
                    current = evaluate("document.querySelector('#main h1')?.textContent.trim()")
                    if current == heading and evaluate("document.activeElement === document.querySelector('#main h1')"):
                        break
                    time.sleep(0.05)
                assert current == heading, route
                assert evaluate("document.activeElement === document.querySelector('#main h1')"), route
                time.sleep(0.1)
                evaluate("document.querySelector('.skip-link').focus()")
                visited = _walk_live_tab_order(command, evaluate,
                    "a[href], button, select, summary, input, [tabindex]", route)
                assert visited, route
                disclosure = evaluate("""(() => {
                  const details = [...document.querySelectorAll('#main details')]
                    .find((item) => item.querySelector('summary') && !item.open);
                  if (!details) return null;
                  details.querySelector('summary').focus();
                  return true;
                })()""")
                if disclosure:
                    _key(command, "Enter", "Enter", 13)
                    revealed = evaluate("""(() => {
                      const details = [...document.querySelectorAll('#main details')]
                        .find((item) => item.open && item.contains(document.activeElement));
                      return details && {open: details.open,
                        hasControl: !!details.querySelector('a[href], button, select, input')};
                    })()""")
                    assert revealed and revealed["open"], route
                    if revealed["hasControl"]:
                        _key(command, "Tab", "Tab", 9)
                        assert evaluate("document.activeElement.closest('details')?.open === true"), route

        _dom_cdp(
            [_chrome(), "--headless=new", "--disable-gpu", "--no-first-run",
             f"--user-data-dir={profile}", server.issue_link() + f"&route=/sample/{ID12}/findings"],
            profile, ok=lambda d: 'data-ready="1"' in d and 'class="finding-card"' in d,
            interact=interact, timeout=240,
        )


@needs_chrome
def test_keyboard_walk_reaches_every_revealed_control_and_signals_subtab(tmp_path) -> None:
    """Open every disclosure by key, then walk its controls in document order."""

    routes = {"home": f"Traceback Lab — {ID12}", "findings": "Findings",
              "trust-qc": "Trust / QC", "stability": "Stability",
              "signals": "Signals", "compare": "Compare", "body-map": "Body map",
              "agreement": "Agreement", "evidence": "Evidence", "methods": "Methods"}
    with _server(tmp_path, _sample()) as server:
        profile = tmp_path / "browser-profile"

        def interact(command, evaluate) -> None:
            command("Page.bringToFront")
            walked = []

            def reveal_all(label: str) -> None:
                controls = evaluate("""(() => [...document.querySelectorAll('#main button[aria-controls]')]
                  .flatMap((button, index) => { const target = document.getElementById(button.getAttribute('aria-controls'));
                    return target?.hidden && button.getClientRects().length ? [index] : []; }))()""")
                for index in controls:
                    button = f"document.querySelectorAll('#main button[aria-controls]')[{index}]"
                    evaluate(f"{button}.focus()")
                    _key(command, "Enter", "Enter", 13)
                    assert evaluate(f"!document.getElementById({button}.getAttribute('aria-controls')).hidden"), (label, index)
                count = evaluate("document.querySelectorAll('#main details > summary').length")
                for index in range(count):
                    state = evaluate(f"""(() => {{ const summary = document.querySelectorAll('#main details > summary')[{index}];
                      if (!summary.getClientRects().length) return {{hidden: true}};
                      summary.focus(); return {{open: summary.parentElement.open}}; }})()""")
                    assert not state.get("hidden"), (label, index)
                    if not state["open"]:
                        _key(command, "Enter", "Enter", 13)
                        assert evaluate(f"document.querySelectorAll('#main details > summary')[{index}].parentElement.open"), (label, index)

            def walk(label: str) -> None:
                evaluate("document.querySelector('.skip-link').focus()")
                visited = _walk_live_tab_order(command, evaluate,
                    "a[href], button, select, summary, input, [tabindex], .table-wrap", label)
                assert visited, label
                walked.append((label, len(visited) + 1))

            for route, heading in routes.items():
                path = "/" if route == "home" else f"/sample/{ID12}/{route}"
                evaluate(f"location.hash = '#{path}'")
                for _ in range(60):
                    if evaluate(f"document.querySelector('#main h1')?.textContent.trim() === {json.dumps(heading)}"):
                        break
                    time.sleep(0.05)
                else:
                    pytest.fail(f"{route} did not render")
                if route == "home":
                    for _ in range(60):
                        if evaluate("document.querySelector('#clia-full')?.children.length > 0"):
                            break
                        time.sleep(0.05)
                    else:
                        pytest.fail("CLIA copy did not load")
                time.sleep(0.05)
                if route == "signals":
                    for label in ("Fragmentomics", "Copy number", "Methylation"):
                        tab = f"[...document.querySelectorAll('#main .subtab')].find((el) => el.textContent === {json.dumps(label)})"
                        evaluate(f"{tab}.focus()")
                        _key(command, "Enter", "Enter", 13)
                        assert evaluate(f"{tab}.getAttribute('aria-pressed') === 'true'"), label
                        reveal_all(label)
                        walk(f"signals/{label}/revealed")
                else:
                    reveal_all(route)
                    walk(f"{route}/revealed")

            evaluate("document.getElementById('key-button').focus()")
            _key(command, "Enter", "Enter", 13)
            assert evaluate("document.getElementById('key-dialog').open")
            expected_dialog = evaluate("""(() => [...document.querySelectorAll('#key-dialog a[href], #key-dialog button, #key-dialog input')]
              .filter((el) => !el.disabled && el.getClientRects().length).map((el) => el.id || el.textContent.trim()))()""")
            visited = set()
            for _ in range(len(expected_dialog) + 2):
                state = evaluate("""(() => { const active = document.activeElement;
                  return {inside: !!active.closest('#key-dialog'), id: active.id || active.textContent.trim(),
                    visible: active.matches(':focus-visible'), outline: getComputedStyle(active).outlineWidth}; })()""")
                if not state["inside"]:
                    _key(command, "Tab", "Tab", 9)
                    continue
                assert state["visible"], state
                assert float(state["outline"].removesuffix("px")) >= 3, state
                visited.add(state["id"])
                if set(expected_dialog) <= visited:
                    break
                _key(command, "Tab", "Tab", 9)
            assert set(expected_dialog) <= visited, (expected_dialog, visited)
            _key(command, "Escape", "Escape", 27)
            assert len(walked) == len(routes) - 1 + 3, walked

        _dom_cdp(
            [_chrome(), "--headless=new", "--disable-gpu", "--no-first-run",
             f"--user-data-dir={profile}", server.issue_link() + f"&route=/sample/{ID12}/findings"],
            profile, ok=lambda d: 'data-ready="1"' in d and 'class="finding-card"' in d,
            interact=interact, timeout=240,
        )
