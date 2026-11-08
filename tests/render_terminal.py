"""Renders the terminal view under CPython.

    python3 tests/render_terminal.py            # prints the frame
    python3 tests/render_terminal.py --compact  # six services, compact mode

Writes the raw ANSI to tests/out/terminal.ans too, so `cat` shows it in colour.
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tests.micropython_shim  # noqa: F401

import network  # noqa: E402
import tui  # noqa: E402
import web  # noqa: E402
from tests.render_dashboard import OUT, build_monitors  # noqa: E402
from utils import Uptime  # noqa: E402


def build_terminal(compact=False):
    monitors = build_monitors()
    if compact:
        monitors = monitors + build_monitors()[:2]
        for index, monitor in enumerate(monitors):
            monitor.name = "{}-{}".format(monitor.name, index)
    uptime = Uptime()
    uptime.seconds = 4 * 86400 + 2 * 3600 + 11 * 60 + 7
    dashboard = web.Dashboard(monitors, network.WLAN(network.STA_IF), uptime)
    dashboard.last_sync = "2026-09-08 14:22:07"
    return tui.Terminal(dashboard), tui.Client()


def main():
    terminal, client = build_terminal("--compact" in sys.argv)
    frame = list(terminal.frame(client))
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, "terminal.ans"), "w") as handle:
        handle.write("\n".join(frame) + "\n")
    print("\n".join(frame))


if __name__ == "__main__":
    main()
