"""Streaming status dashboard.

Everything is written to the socket in small pieces. The page is never
assembled into a single string. The ESP32 has far more headroom than the
ESP8266 this was written for, but streaming costs nothing and keeps the page
size independent of how many services are configured.
"""

import asyncio
import gc
import json
import os
from machine import reset_cause

import chart
import httpc
import log
from utils import (FAILED, cpu_mhz, escape_html, format_bytes, format_duration,
                   format_latency, rtc_timestamp, set_freq)

VERSION = "2.0.0"
REFRESH_SECONDS = 30
CHART_HEIGHT = 8

def _reset_cause_labels():
    """Reads the constants from machine rather than hardcoding them, because
    the integer values differ between ports."""
    import machine

    labels = {}
    for name, label in (("PWRON_RESET", "POWER ON"), ("HARD_RESET", "HARD RESET"),
                        ("WDT_RESET", "WATCHDOG"), ("DEEPSLEEP_RESET", "DEEP SLEEP"),
                        ("SOFT_RESET", "SOFT RESET"), ("BROWNOUT_RESET", "BROWNOUT")):
        value = getattr(machine, name, None)
        if value is not None:
            labels[value] = label
    return labels


RESET_CAUSES = _reset_cause_labels()


def board_name():
    from utils import board

    return board.hostname or "minutePing"

BADGES = {"ok": "[ OK ]", "warn": "[WARN]", "fail": "[FAIL]"}

STYLE = """
:root{
 --bg:#070b07;--panel:#0c120c;--edge:#1b2a1b;--edge2:#263a26;
 --dim:#5c7a5c;--txt:#9fc79f;--hi:#d6ffd6;
 --ok:#4ade80;--warn:#fbbf24;--fail:#f87171;
}
*{box-sizing:border-box}
body{margin:0;padding:18px 14px 40px;background:var(--bg);color:var(--txt);
 font:13px/1.5 ui-monospace,SFMono-Regular,Menlo,Consolas,"DejaVu Sans Mono",monospace;
 -webkit-font-smoothing:antialiased}
body::before{content:"";position:fixed;inset:0;pointer-events:none;z-index:9;
 background:repeating-linear-gradient(180deg,rgba(0,0,0,.22) 0 1px,transparent 1px 3px);
 opacity:.5}
.wrap{max-width:960px;margin:0 auto;position:relative;z-index:1}
h1{margin:0;font-size:20px;letter-spacing:.32em;color:var(--hi);
 text-shadow:0 0 12px rgba(74,222,128,.45)}
.sub{color:var(--dim);letter-spacing:.18em;font-size:11px;margin-top:4px}
.rule{height:1px;background:linear-gradient(90deg,var(--edge2),transparent);margin:14px 0 18px}
.panel{border:1px solid var(--edge);background:var(--panel);margin-bottom:16px}
.ph{padding:7px 12px;border-bottom:1px solid var(--edge);color:var(--hi);
 font-size:11px;letter-spacing:.22em;background:rgba(255,255,255,.02);
 display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap}
.ph .r{color:var(--dim);letter-spacing:.12em}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:1px;
 background:var(--edge)}
.cell{background:var(--panel);padding:9px 12px;min-width:0}
.k{display:block;color:var(--dim);font-size:10px;letter-spacing:.16em}
.v{display:block;color:var(--hi);font-size:14px;margin-top:2px;
 overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.svc{border-top:1px solid var(--edge)}
.svc:first-child{border-top:0}
.hd{display:flex;align-items:baseline;gap:12px;padding:10px 12px;flex-wrap:wrap}
.bdg{font-weight:700;letter-spacing:.06em;white-space:pre}
.nm{color:var(--hi);letter-spacing:.14em;font-size:14px}
.ep{color:var(--dim);font-size:11px;letter-spacing:.06em;
 overflow:hidden;text-overflow:ellipsis}
.ok .bdg,.ok .nm{color:var(--ok)}
.warn .bdg,.warn .nm{color:var(--warn)}
.fail .bdg,.fail .nm{color:var(--fail)}
.mx{display:grid;grid-template-columns:repeat(auto-fit,minmax(88px,1fr));gap:1px;
 background:var(--edge);border-top:1px solid var(--edge)}
.mx div{background:var(--panel);padding:7px 12px}
.mx .v{font-size:13px}
.bar{height:3px;background:var(--edge2);margin:0}
.bar i{display:block;height:3px;background:var(--ok)}
.warn .bar i{background:var(--warn)}
.fail .bar i{background:var(--fail)}
pre.ch{margin:0;padding:12px;overflow-x:auto;color:var(--ok);font-size:12px;
 line-height:1.15;border-top:1px solid var(--edge);
 text-shadow:0 0 8px rgba(74,222,128,.3)}
.fail pre.ch{color:var(--fail);text-shadow:0 0 8px rgba(248,113,113,.3)}
.warn pre.ch{color:var(--warn);text-shadow:0 0 8px rgba(251,191,36,.3)}
.empty{padding:14px 12px;color:var(--dim)}
footer{color:var(--dim);font-size:11px;letter-spacing:.1em;
 display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap}
a{color:var(--ok);text-decoration:none;border-bottom:1px dotted var(--edge2)}
a:hover{color:var(--hi)}
"""


class Dashboard:
    def __init__(self, monitors, station, uptime, webrepl_enabled=False,
                 idle_mhz=80, busy_mhz=160):
        self.monitors = monitors
        self.station = station
        self.uptime = uptime
        self.webrepl_enabled = webrepl_enabled
        self.idle_mhz = idle_mhz
        self.busy_mhz = busy_mhz
        self.last_sync = None
        self.requests = 0

    # -- system readings -------------------------------------------------

    def address(self):
        try:
            return self.station.ifconfig()[0]
        except (OSError, AttributeError):
            return "0.0.0.0"

    def ssid(self):
        # The ESP32 accepts both spellings, but reading either is undocumented,
        # so fall back rather than assume.
        for key in ("ssid", "essid"):
            try:
                value = self.station.config(key)
                if value:
                    return value
            except (OSError, ValueError, AttributeError, KeyError):
                pass
        return "?"

    def rssi(self):
        try:
            return "{} dBm".format(self.station.status("rssi"))
        except (OSError, ValueError, AttributeError, TypeError):
            return "n/a"

    def online_count(self):
        return sum(1 for monitor in self.monitors if monitor.online)

    def totals(self):
        checks = sum(monitor.total_checks for monitor in self.monitors)
        alerts = sum(monitor.alerts_sent for monitor in self.monitors)
        return checks, alerts

    def system_cells(self):
        """The (label, value) pairs of the SYSTEM panel, shared by the web
        page and the terminal view so the two can never disagree."""
        checks, alerts = self.totals()
        online = self.online_count()

        gc.collect()  # report the honest post-collection figure
        free = gc.mem_free()
        used = gc.mem_alloc()

        return (
            ("UPTIME", format_duration(self.uptime.seconds)),
            ("FREE HEAP", "{} / {}".format(format_bytes(free), format_bytes(free + used))),
            ("IP ADDRESS", self.address()),
            ("NETWORK", self.ssid()),
            ("SIGNAL", self.rssi()),
            ("CPU", "{} MHz".format(cpu_mhz())),
            ("LAST RESET", RESET_CAUSES.get(reset_cause(), "UNKNOWN")),
            ("NTP SYNC", self.last_sync or "never"),
            ("TLS", httpc.tls_state.summary()),
            ("SERVICES UP", "{} / {}".format(online, len(self.monitors))),
            ("CHECKS RUN", str(checks)),
            ("ALERTS SENT", str(alerts)),
            ("PAGE VIEWS", str(self.requests)),
        )

    def boot_report(self, web_enabled=True, telnet_port=None):
        """Multi-line plain text for the startup notification: what a person
        wants to know when the board comes up (and why it restarted)."""
        cells = dict(self.system_cells())
        lines = [
            "minutePing {} is up on {}.".format(VERSION, board_name()),
            "Reset cause: {}".format(cells["LAST RESET"]),
            "Network: {} ({}), signal {}".format(
                cells["NETWORK"], cells["IP ADDRESS"], cells["SIGNAL"]),
            "Clock: {} UTC, NTP sync {}".format(rtc_timestamp(), cells["NTP SYNC"]),
            "Free heap: {} (free / total), CPU {}".format(cells["FREE HEAP"], cells["CPU"]),
            "TLS: {}".format(cells["TLS"]),
            "Monitoring {} services: {}".format(
                len(self.monitors), ", ".join(m.name for m in self.monitors)),
            "Firmware: MicroPython {}".format(os.uname().release),
        ]
        address = cells["IP ADDRESS"]
        views = []
        if web_enabled:
            views.append("http://{}/".format(address))
        if telnet_port:
            views.append("telnet {} {}".format(address, telnet_port))
        if views:
            lines.append("View: " + " or ".join(views))
        return "\n".join(lines)

    # -- rendering -------------------------------------------------------

    async def send_page(self, writer):
        write = writer.write
        online = self.online_count()

        write("<!DOCTYPE html><html><head><meta charset=\"utf-8\">"
              "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
              "<meta http-equiv=\"refresh\" content=\"{}\">"
              "<title>minutePing // {}</title><style>".format(
                  REFRESH_SECONDS, escape_html(self.address())))
        await writer.drain()

        write(STYLE)
        write("</style></head><body><div class=\"wrap\">")
        write("<h1>MINUTEPING</h1><div class=\"sub\">SERVICE MONITOR &middot; V{}"
              " &middot; {} UTC</div><div class=\"rule\"></div>".format(
                  VERSION, rtc_timestamp()))
        await writer.drain()

        write("<div class=\"panel\"><div class=\"ph\"><span>SYSTEM</span>"
              "<span class=\"r\">{}</span></div><div class=\"grid\">".format(
                  escape_html(os.uname().release)))

        for key, value in self.system_cells():
            write("<div class=\"cell\"><span class=\"k\">{}</span>"
                  "<span class=\"v\">{}</span></div>".format(key, escape_html(value)))
            await writer.drain()

        write("</div></div>")

        write("<div class=\"panel\"><div class=\"ph\"><span>SERVICES</span>"
              "<span class=\"r\">{} OF {} ONLINE</span></div>".format(
                  online, len(self.monitors)))
        await writer.drain()

        if not self.monitors:
            write("<div class=\"empty\">No services configured.</div>")

        for monitor in self.monitors:
            await self.send_service(writer, monitor)

        write("</div>")

        write("<footer><span>REFRESH {}S</span>".format(REFRESH_SECONDS))
        if self.webrepl_enabled:
            write("<span><a href=\"http://micropython.org/webrepl/#{}:8266/\">"
                  "ADMIN CONSOLE</a></span>".format(escape_html(self.address())))
        write("<span><a href=\"/api\">JSON API</a></span></footer>")
        write("</div></body></html>")
        await writer.drain()

    async def send_service(self, writer, monitor):
        write = writer.write
        state = monitor.state()
        current, minimum, average, maximum, p95, uptime = monitor.history.stats()

        write("<div class=\"svc {}\"><div class=\"hd\">"
              "<span class=\"bdg\">{}</span><span class=\"nm\">{}</span>"
              "<span class=\"ep\">{} &middot; {}</span></div>".format(
                  state, BADGES[state], escape_html(monitor.name.upper()),
                  monitor.type.upper(), escape_html(monitor.endpoint)))

        write("<div class=\"bar\"><i style=\"width:{:.1f}%\"></i></div>".format(uptime))
        await writer.drain()

        if not monitor.checked_once:
            write("<div class=\"empty\">Awaiting first check&hellip;</div></div>")
            await writer.drain()
            return

        write("<div class=\"mx\">")
        for key, value in (
            ("NOW", format_latency(current)),
            ("MIN", format_latency(minimum)),
            ("AVG", format_latency(average)),
            ("P95", format_latency(p95)),
            ("MAX", format_latency(maximum)),
            ("UPTIME", "{:.1f}%".format(uptime)),
            ("FAILS", "{} / {}".format(monitor.failures, monitor.max_consecutive_failures)),
            ("SINCE", format_duration(monitor.seconds_since_change())),
            ("NEXT", "{}s".format(monitor.seconds_until_next_check())),
            ("SAMPLES", "{} / {}".format(len(monitor.history), monitor.history.length)),
        ):
            write("<div><span class=\"k\">{}</span><span class=\"v\">{}</span></div>".format(
                key, value))
        write("</div>")
        await writer.drain()

        write("<pre class=\"ch\">")
        for row in chart.plot(monitor.history, CHART_HEIGHT):
            write(escape_html(row))
            write("\n")
            await writer.drain()
        write("      ms over last {} checks ({} min)</pre></div>".format(
            len(monitor.history),
            len(monitor.history) * monitor.update_frequency // 60))
        await writer.drain()

    async def send_json(self, writer):
        """Streams the same data as the page, one service object at a time."""
        checks, alerts = self.totals()
        gc.collect()

        writer.write("{\"system\":")
        writer.write(json.dumps({
            "version": VERSION,
            "release": os.uname().release,
            "uptime_seconds": self.uptime.seconds,
            "free_heap": gc.mem_free(),
            "used_heap": gc.mem_alloc(),
            "ip": self.address(),
            "ssid": self.ssid(),
            "rssi": self.rssi(),
            "cpu_mhz": cpu_mhz(),
            "reset_cause": RESET_CAUSES.get(reset_cause(), "UNKNOWN"),
            "time": rtc_timestamp(),
            "last_ntp_sync": self.last_sync,
            "tls": httpc.tls_state.summary(),
            "tls_verify_failures": httpc.tls_state.verify_failures,
            "tls_last_error": httpc.tls_state.last_error,
            "services_online": self.online_count(),
            "services_total": len(self.monitors),
            "checks_run": checks,
            "alerts_sent": alerts,
        }))
        writer.write(",\"services\":[")
        await writer.drain()

        for index, monitor in enumerate(self.monitors):
            current, minimum, average, maximum, p95, uptime = monitor.history.stats()

            if index:
                writer.write(",")

            writer.write(json.dumps({
                "name": monitor.name,
                "type": monitor.type,
                "endpoint": monitor.endpoint,
                "state": monitor.state(),
                "online": monitor.online,
                "latency_ms": None if current == FAILED else current,
                "min_ms": None if minimum == FAILED else minimum,
                "avg_ms": None if average == FAILED else average,
                "p95_ms": None if p95 == FAILED else p95,
                "max_ms": None if maximum == FAILED else maximum,
                "uptime_percent": round(uptime, 1),
                "failures": monitor.failures,
                "max_consecutive_failures": monitor.max_consecutive_failures,
                "update_frequency": monitor.update_frequency,
                "seconds_since_change": monitor.seconds_since_change(),
                "seconds_until_next_check": monitor.seconds_until_next_check(),
                "total_checks": monitor.total_checks,
                "total_failures": monitor.total_failures,
                "samples": len(monitor.history),
            }))
            await writer.drain()

        writer.write("]}")
        await writer.drain()

    async def handle(self, reader, writer):
        """Serves one request at the busy clock, then drops back to idle."""
        self.requests += 1
        set_freq(self.busy_mhz)

        try:
            request_line = await asyncio.wait_for(reader.readline(), 5)
            if not request_line:
                return

            parts = request_line.split(b" ")
            if len(parts) < 2:
                writer.write("HTTP/1.0 400 Bad Request\r\n\r\n")
                await writer.drain()
                return

            method, path = parts[0], parts[1]

            # Drain the headers so the client sees a clean response.
            while True:
                line = await asyncio.wait_for(reader.readline(), 5)
                if not line or line == b"\r\n":
                    break

            if method != b"GET":
                writer.write("HTTP/1.0 405 Method Not Allowed\r\n\r\n")
                await writer.drain()
                return

            if path == b"/api":
                writer.write("HTTP/1.0 200 OK\r\nContent-Type: application/json\r\n"
                             "Connection: close\r\n\r\n")
                await writer.drain()
                await self.send_json(writer)
            elif path == b"/" or path.startswith(b"/?"):
                writer.write("HTTP/1.0 200 OK\r\nContent-Type: text/html; charset=utf-8\r\n"
                             "Connection: close\r\n\r\n")
                await writer.drain()
                await self.send_page(writer)
            else:
                writer.write("HTTP/1.0 404 Not Found\r\n\r\n")
                await writer.drain()

        except (OSError, asyncio.TimeoutError) as error:
            log.warning("web", "request failed: {}".format(error))
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except OSError:
                pass
            set_freq(self.idle_mhz)
            gc.collect()
