"""Renders the dashboard under CPython so the styling can be reviewed in a
browser without flashing the board.

    python3 tests/render_dashboard.py && open tests/out/dashboard.html

Builds a plausible mix of healthy, degrading and failed services, exercises the
real Dashboard, chart and Ring code, and writes both the HTML page and the JSON
API response to tests/out/.
"""

import asyncio
import math
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tests.micropython_shim  # noqa: F401  (installs the stubs)

import network  # noqa: E402
import httpc
import web  # noqa: E402
from monitors import Monitor  # noqa: E402
from utils import FAILED, Uptime  # noqa: E402

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out")


class Collector:
    """Stands in for an asyncio StreamWriter."""

    def __init__(self):
        self.parts = []

    def write(self, text):
        self.parts.append(text)

    async def drain(self):
        pass

    def text(self):
        return "".join(self.parts)


def make_monitor(name, kind, endpoint, shape, frequency=180, failures=0, online=True):
    monitor = Monitor({
        "name": name,
        "type": kind,
        "endpoint": endpoint,
        "update_frequency": frequency,
        "history_length": 60,
    }, [])

    for index in range(60):
        monitor.history.append(shape(index))

    monitor.checked_once = True
    monitor.total_checks = 412
    monitor.total_failures = sum(1 for sample in monitor.history if sample == FAILED)
    monitor.failures = failures
    monitor.online = online
    monitor.alerts_sent = 0 if online else 2
    monitor.last_change -= 0 if online else 900000

    return monitor


def build_monitors():
    random.seed(7)

    def steady(index):
        return int(11 + 4 * math.sin(index / 5) + random.random() * 3)

    def spiky(index):
        if index in (28, 29, 30):
            return int(280 + random.random() * 60)
        return int(45 + 12 * math.sin(index / 3) + random.random() * 10)

    def degrading(index):
        if index > 52:
            return FAILED
        return int(60 + index * 2.2 + random.random() * 8)

    def flapping(index):
        if index % 11 in (0, 1):
            return FAILED
        return int(120 + 30 * math.sin(index / 4) + random.random() * 15)

    return [
        make_monitor("gateway", "icmp", "192.168.1.1", steady, frequency=60),
        make_monitor("homepage", "http", "http://example.internal/health", spiky),
        make_monitor("mailserver", "tcp", "mail.internal:25", degrading,
                     failures=3, online=False),
        make_monitor("resolver", "dns", "9.9.9.9", flapping, failures=1),
    ]


async def render():
    os.makedirs(OUT, exist_ok=True)

    monitors = build_monitors()
    uptime = Uptime()
    uptime.seconds = 4 * 86400 + 2 * 3600 + 11 * 60 + 7

    dashboard = web.Dashboard(monitors, network.WLAN(network.STA_IF), uptime,
                              webrepl_enabled=True)
    dashboard.last_sync = "2026-09-08 14:22:07"
    dashboard.requests = 137

    # Preview the state a board actually boots into: TLS working, but with no
    # CA file configured, so the dashboard is saying the connection is
    # encrypted and unauthenticated.
    httpc.tls_state.unverified = True
    httpc.tls_state.handshakes = 12

    page = Collector()
    await dashboard.send_page(page)
    html = page.text()
    with open(os.path.join(OUT, "dashboard.html"), "w") as handle:
        handle.write(html)

    api = Collector()
    await dashboard.send_json(api)
    payload = api.text()
    with open(os.path.join(OUT, "api.json"), "w") as handle:
        handle.write(payload)

    import json
    parsed = json.loads(payload)  # proves the streamed JSON is well formed

    print("dashboard.html  {} bytes".format(len(html)))
    print("api.json        {} bytes, {} services".format(
        len(payload), len(parsed["services"])))
    print()
    for monitor in monitors:
        current, minimum, average, maximum, p95, up = monitor.history.stats()
        print("  {:<12} {:<5} now={:<6} min={:<6} avg={:<6} p95={:<6} max={:<6} up={:.1f}%".format(
            monitor.name, monitor.state(), current, minimum, average, p95, maximum, up))
    print()
    print("Open: tests/out/dashboard.html")


if __name__ == "__main__":
    asyncio.run(render())
