"""``traceback lab serve [--open] [--root R]``: the loopback Lab page (D12, D36).

Prints the resolved ROOT, then a one-use ``http://127.0.0.1:<port>/#bootstrap=…``
link.  On a terminal, Enter prints a fresh link (the signed ``serve``
behaviour); Ctrl-C or SIGTERM stops the server.  The server never takes the
writer lock, so ``lab run`` can run while the page is open.
"""

from __future__ import annotations

import argparse
import signal
import sys
import threading
import webbrowser
from collections.abc import Callable
from typing import Any

from .problems import EXIT_OK
from .store import LabStore
from .web.server import RunningLabServer


def add_serve_parser(commands: Any, common: Callable[[argparse.ArgumentParser], None]) -> None:
    serve = commands.add_parser("serve", help="open the local Lab page (loopback only)")
    serve.add_argument("--open", action="store_true", dest="open_browser", help="open the link in the default browser")
    common(serve)
    serve.set_defaults(handler=serve_command, long_running=True)


def serve_command(args: argparse.Namespace, store: LabStore, *, out: Any = None, stdin: Any = None, stop: threading.Event | None = None, ready: Callable[[RunningLabServer], None] | None = None) -> int:
    out = out or sys.stdout
    stdin = stdin or sys.stdin
    stop = stop or threading.Event()
    store.check_version()
    print(f"ROOT  {store.root}", file=out, flush=True)
    if not store.run_ids():
        print(
            f"ROOT {store.root} has no Lab runs. Pass the same --root (or TRACEBACK_ROOT) you used for lab run.",
            file=out,
            flush=True,
        )
    previous = {}
    if threading.current_thread() is threading.main_thread():
        for sig in (signal.SIGINT, signal.SIGTERM):
            previous[sig] = signal.signal(sig, lambda *_: stop.set())
    try:
        with RunningLabServer(store) as server:
            link = server.issue_link()
            print(link, file=out, flush=True)
            print("Open this one-use link in a browser on this machine within 60 seconds; do not share it.", file=out)
            interactive = False
            try:
                interactive = bool(stdin.isatty())
            except (AttributeError, OSError, ValueError):
                pass
            print(
                "Press Enter for a fresh link; Ctrl-C stops the server." if interactive else "Ctrl-C or SIGTERM stops the server.",
                file=out,
                flush=True,
            )
            if args.open_browser:
                webbrowser.open(link)

            def read_terminal() -> None:
                while not stop.is_set():
                    try:
                        line = stdin.readline()
                    except (OSError, ValueError):
                        line = ""
                    if not line:
                        stop.set()
                        return
                    print(server.issue_link(), file=out, flush=True)

            if interactive:
                threading.Thread(target=read_terminal, name="lab-serve-stdin", daemon=True).start()
            if ready is not None:
                ready(server)
            while not stop.wait(0.2):
                pass
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    print("Stopped.", file=out, flush=True)
    return EXIT_OK


__all__ = ["add_serve_parser", "serve_command"]
