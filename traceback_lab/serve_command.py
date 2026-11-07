"""``traceback lab serve [--open] [--port N] [--root R]``: the loopback Lab page (D12, D36).

Prints the resolved ROOT, then a one-use ``http://127.0.0.1:<port>/#bootstrap=…``
link.  On a terminal, Enter prints a fresh link (the signed ``serve``
behaviour); Ctrl-C or SIGTERM stops the server.  The server never takes the
writer lock, so ``lab run`` can run while the page is open.

The port is ephemeral unless ``--port N`` (1024-65535) names one; either way
the server binds ``127.0.0.1`` only.  A port that cannot be bound is refused
with ``TBX-LAB-026`` before anything is printed, so ``--json`` stays one object.
"""

from __future__ import annotations

import argparse
import errno
import signal
import sys
import threading
import webbrowser
from collections.abc import Callable
from typing import Any

from .problems import EXIT_OK, LabProblem
from .store import LabStore
from .web.server import RunningLabServer

PORT_MIN = 1024
PORT_MAX = 65535


def port_number(text: str) -> int:
    """``--port`` value: an integer in 1024-65535 (argparse reports anything else)."""

    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected an integer port in {PORT_MIN}-{PORT_MAX}, got {text!r}") from None
    if not PORT_MIN <= value <= PORT_MAX:
        raise argparse.ArgumentTypeError(f"port must be in {PORT_MIN}-{PORT_MAX}, got {value}")
    return value


def add_serve_parser(commands: Any, common: Callable[[argparse.ArgumentParser], None]) -> None:
    serve = commands.add_parser("serve", help="open the local Lab page (loopback only)")
    serve.add_argument("--open", action="store_true", dest="open_browser", help="open the link in the default browser")
    serve.add_argument(
        "--port",
        type=port_number,
        default=0,
        metavar="N",
        help=f"bind 127.0.0.1:N ({PORT_MIN}-{PORT_MAX}) instead of an ephemeral loopback port",
    )
    common(serve)
    serve.set_defaults(handler=serve_command, long_running=True)


def serve_command(args: argparse.Namespace, store: LabStore, *, out: Any = None, stdin: Any = None, stop: threading.Event | None = None, ready: Callable[[RunningLabServer], None] | None = None) -> int:
    out = out or sys.stdout
    stdin = stdin or sys.stdin
    stop = stop or threading.Event()
    store.check_version()
    server = _bind(store, int(getattr(args, "port", 0) or 0))
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
        with server:
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


def _bind(store: LabStore, port: int) -> RunningLabServer:
    """Start the loopback server; a port that cannot be bound is ``TBX-LAB-026``."""

    try:
        return RunningLabServer(store, port=port) if port else RunningLabServer(store)
    except OSError as exc:
        reason = errno.errorcode.get(exc.errno or 0, "OSError")
        if port:
            cause = f"127.0.0.1:{port} cannot be bound ({reason}); another process may be listening on it"
            fix = "pass a different --port, stop the process using that port, or omit --port for an ephemeral one"
        else:
            cause = f"no ephemeral loopback port could be bound ({reason})"
            fix = "check that the loopback interface is up and retry"
        raise LabProblem("TBX-LAB-026", cause=cause, fix=fix, data={"port": port or None, "errno": reason}) from None


__all__ = ["PORT_MAX", "PORT_MIN", "add_serve_parser", "port_number", "serve_command"]
