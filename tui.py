"""Live terminal view of the dashboard, served over a plain TCP socket.

    telnet <board-ip> 2323        (or: nc <board-ip> 2323)

Shows the same panels and latency charts as the web page, redrawn in place
with ANSI escapes. It is read-only and unauthenticated, exactly like the web
dashboard, so keep it on a trusted network.

Like web.py, every line is written to the socket as it is built; no frame is
ever assembled into one string.
"""

import asyncio
import gc
import os

import chart
import log
from utils import (format_duration, format_latency, rtc_timestamp, set_freq)

WIDTH = 72              # total columns, so it fits any terminal
INNER = WIDTH - 4       # between "| " and " |"
MAX_CLIENTS = 2
COMPACT_ABOVE = 3       # more services than this and only one gets its chart
BAR_WIDTH = 60

RESET = "\x1b[0m"
BORDER = "\x1b[2;32m"
DIM = "\x1b[2;32m"
TEXT = "\x1b[32m"
HIGH = "\x1b[1;92m"
STATE_COLOR = {"ok": "\x1b[92m", "warn": "\x1b[93m", "fail": "\x1b[91m"}
BADGES = {"ok": "[ OK ]", "warn": "[WARN]", "fail": "[FAIL]"}
FAIL = "\x1b[91m"

HIDE_CURSOR = "\x1b[?25l"
SHOW_CURSOR = "\x1b[?25h"
HOME = "\x1b[H"
CLEAR = "\x1b[2J"
CLEAR_LINE = "\x1b[K"
CLEAR_BELOW = "\x1b[J"

# IAC WILL ECHO, IAC WILL SUPPRESS-GO-AHEAD, IAC DO SUPPRESS-GO-AHEAD:
# puts a telnet client into character-at-a-time mode with no local echo, so
# keys arrive immediately. A plain `nc` shows these bytes briefly, then the
# first screen clear wipes them.
NEGOTIATE = b"\xff\xfb\x01\xff\xfb\x03\xff\xfd\x03"

# Telnet commands that mean "the user pressed Ctrl-C / quit".
IAC_QUIT = (243, 244, 236)   # BRK, IP, EOF

# _feed() parser states
NORMAL, GOT_IAC, SKIP_ONE, GOT_ESC, GOT_CSI, IN_SUB, SUB_IAC = range(7)


# -- line building -------------------------------------------------------

def row(*segments):
    """One boxed line from (color, text) pieces, padded to the box width.

    Padding is computed from the plain text, so colors never throw the right
    border out of line.
    """
    body = ""
    used = 0
    for color, text in segments:
        room = INNER - used
        if room <= 0:
            break
        if len(text) > room:
            text = text[:room]
        body += color + text + RESET
        used += len(text)
    return "{}│ {}{}{}{} │{}".format(BORDER, RESET, body, " " * (INNER - used),
                                     BORDER, RESET)


def border(title, right=""):
    left = "┌─ {} ".format(title)
    tail = " {} ─┐".format(right) if right else " ─┐"
    fill = WIDTH - len(left) - len(tail)
    return (BORDER + "┌─ " + HIGH + title + BORDER + " " + "─" * fill
            + (DIM + " " + right + BORDER if right else "") + " ─┐" + RESET)


def bottom():
    return BORDER + "└" + "─" * (WIDTH - 2) + "┘" + RESET


def bar(percent):
    filled = int(BAR_WIDTH * percent / 100 + 0.5)
    filled = max(0, min(BAR_WIDTH, filled))
    return "█" * filled + "░" * (BAR_WIDTH - filled)


def pad(text, width):
    """str.ljust, which MicroPython's str lacks."""
    return text + " " * (width - len(text))


def cell(key, value):
    """A SYSTEM-panel cell: 34 columns, two per line."""
    return ((DIM, pad(key[:11], 12)), (HIGH, pad(str(value)[:21], 22)))


def pairs(items):
    """Label/value pairs separated by two spaces, as a segment list."""
    segments = []
    for index, (key, value) in enumerate(items):
        if index:
            segments.append((TEXT, "  "))
        segments.append((DIM, key + " "))
        segments.append((HIGH, value))
    return segments


def chart_rows(history, color):
    """chart.plot rows trimmed to fit, keeping the newest samples."""
    width = len(history)
    keep = INNER - 6
    for line in chart.plot(history, 8):
        line = pad(line, 6 + width)
        if width > keep:
            line = line[:6] + line[-keep:]
        # Failures stand out on the baseline in red.
        segments = []
        for index, part in enumerate(line.split("╳")):
            if index:
                segments.append((FAIL, "╳"))
            if part:
                segments.append((color, part))
        yield row(*segments)


# -- per-connection state ------------------------------------------------

class Client:
    def __init__(self):
        self.done = False
        self.dirty = False
        self.selected = 0


# -- the view ------------------------------------------------------------

class Terminal:
    def __init__(self, dashboard, refresh_seconds=5):
        self.dashboard = dashboard
        self.refresh_seconds = refresh_seconds
        self.clients = 0

    # Frame ------------------------------------------------------------

    def compact(self):
        return len(self.dashboard.monitors) > COMPACT_ABOVE

    def frame(self, client):
        """Yields the screen one finished line at a time."""
        dashboard = self.dashboard
        monitors = dashboard.monitors
        online = dashboard.online_count()
        compact = self.compact()

        yield " " + HIGH + "MINUTEPING" + RESET
        yield " {}SERVICE MONITOR · V{} · {} UTC{}".format(
            DIM, _version(), rtc_timestamp(), RESET)
        yield " " + BORDER + "═" * (WIDTH - 2) + RESET
        yield ""

        yield border("SYSTEM", os.uname().release)
        cells = dashboard.system_cells()
        for index in range(0, len(cells), 2):
            segments = cell(*cells[index])
            if index + 1 < len(cells):
                segments += cell(*cells[index + 1])
            yield row(*segments)
        yield bottom()
        yield ""

        yield border("SERVICES", "{} OF {} ONLINE".format(online, len(monitors)))
        if not monitors:
            yield row((DIM, "No services configured."))

        for index, monitor in enumerate(monitors):
            selected = index == client.selected
            if compact and not selected:
                yield self.summary_line(monitor)
                continue
            yield row()
            for line in self.service_lines(monitor, compact and selected):
                yield line
        yield row()
        yield bottom()
        yield ""

        footer = " REFRESH {}S · q QUIT".format(self.refresh_seconds)
        if compact:
            footer += " · ↑↓ SELECT"
        yield DIM + footer + " · " + rtc_timestamp() + RESET

    def summary_line(self, monitor):
        state = monitor.state()
        current, _, _, _, _, uptime = monitor.history.stats()
        return row(
            (STATE_COLOR[state], BADGES[state]), (TEXT, " "),
            (HIGH, pad(monitor.name.upper()[:18], 18)),
            (DIM, pad(monitor.type.upper(), 6)),
            (DIM, "NOW "), (HIGH, pad(format_latency(current), 8)),
            (DIM, "UP "), (HIGH, "{:.1f}%".format(uptime)))

    def service_lines(self, monitor, marked):
        state = monitor.state()
        color = STATE_COLOR[state]
        current, minimum, average, maximum, p95, uptime = monitor.history.stats()

        name = ("▸ " if marked else "") + monitor.name.upper()
        endpoint = "{} · {}".format(monitor.type.upper(), monitor.endpoint)
        room = INNER - len(BADGES[state]) - 1 - len(name) - 2
        endpoint = endpoint[:max(room, 0)]
        gap = INNER - len(BADGES[state]) - 1 - len(name) - len(endpoint)
        yield row((color, BADGES[state]), (TEXT, " "), (HIGH, name),
                  (TEXT, " " * gap), (DIM, endpoint))

        yield row((color, bar(uptime)), (TEXT, "  "), (HIGH, "{:5.1f}%".format(uptime)))

        if not monitor.checked_once:
            yield row((DIM, "Awaiting first check..."))
            return

        yield row(*pairs((
            ("NOW", format_latency(current)),
            ("MIN", format_latency(minimum)),
            ("AVG", format_latency(average)),
            ("P95", format_latency(p95)),
            ("MAX", format_latency(maximum)))))
        yield row(*pairs((
            ("UPTIME", "{:.1f}%".format(uptime)),
            ("FAILS", "{}/{}".format(monitor.failures, monitor.max_consecutive_failures)),
            ("SINCE", format_duration(monitor.seconds_since_change())),
            ("NEXT", "{}s".format(monitor.seconds_until_next_check())),
            ("SAMPLES", "{}/{}".format(len(monitor.history), monitor.history.length)))))

        for line in chart_rows(monitor.history, color):
            yield line
        yield row((DIM, "      ms over last {} checks ({} min)".format(
            len(monitor.history), len(monitor.history) * monitor.update_frequency // 60)))

    async def draw(self, writer, client):
        gc.collect()
        writer.write(HOME)
        count = 0
        for line in self.frame(client):
            writer.write(line + CLEAR_LINE + "\r\n")
            count += 1
            if count % 4 == 0:
                await writer.drain()
        writer.write(CLEAR_BELOW)
        await writer.drain()

    # Input ------------------------------------------------------------

    def feed(self, client, state, byte):
        """Advances the key parser by one byte and returns the new state.

        Skips telnet option negotiation, understands the arrow keys, and
        flags the client done on q, Ctrl-C or Ctrl-D.
        """
        if state == NORMAL:
            if byte == 255:
                return GOT_IAC
            if byte == 27:
                return GOT_ESC
            if byte in (3, 4, 81, 113):
                client.done = True
            elif byte in (106, 110):        # j, n
                self.move(client, 1)
            elif byte in (107, 112):        # k, p
                self.move(client, -1)
            elif byte == 114:               # r
                client.dirty = True
            return NORMAL

        if state == GOT_IAC:
            if byte in IAC_QUIT:
                client.done = True
                return NORMAL
            if byte == 250:
                return IN_SUB
            if 251 <= byte <= 254:
                return SKIP_ONE
            return NORMAL

        if state == SKIP_ONE:
            return NORMAL

        if state == IN_SUB:
            return SUB_IAC if byte == 255 else IN_SUB

        if state == SUB_IAC:
            return NORMAL if byte == 240 else IN_SUB

        if state == GOT_ESC:
            return GOT_CSI if byte in (91, 79) else NORMAL

        # GOT_CSI
        if byte == 65:
            self.move(client, -1)
        elif byte == 66:
            self.move(client, 1)
        return NORMAL

    def move(self, client, step):
        count = len(self.dashboard.monitors)
        if count:
            client.selected = (client.selected + step) % count
            client.dirty = True

    async def read_keys(self, reader, client):
        state = NORMAL
        try:
            while not client.done:
                data = await reader.read(32)
                if not data:
                    break
                for byte in data:
                    state = self.feed(client, state, byte)
        except OSError:
            pass
        client.done = True

    # Connection -------------------------------------------------------

    async def handle(self, reader, writer):
        if self.clients >= MAX_CLIENTS:
            try:
                writer.write("minutePing: too many viewers, try again shortly.\r\n")
                await writer.drain()
                writer.close()
                await writer.wait_closed()
            except OSError:
                pass
            return

        self.clients += 1
        set_freq(self.dashboard.busy_mhz)
        client = Client()
        keys = asyncio.create_task(self.read_keys(reader, client))

        try:
            writer.write(NEGOTIATE)
            writer.write(HIDE_CURSOR + CLEAR)
            await writer.drain()

            while not client.done:
                await self.draw(writer, client)
                waited = 0
                while (waited < self.refresh_seconds * 1000
                       and not client.done and not client.dirty):
                    await asyncio.sleep_ms(100)
                    waited += 100
                client.dirty = False
        except OSError as error:
            log.debug("tui", "client dropped: {}".format(error))
        finally:
            client.done = True
            keys.cancel()
            try:
                writer.write(SHOW_CURSOR + RESET + "\r\n")
                await writer.drain()
                writer.close()
                await writer.wait_closed()
            except OSError:
                pass
            self.clients -= 1
            if not self.clients:
                set_freq(self.dashboard.idle_mhz)
            gc.collect()


def _version():
    import web

    return web.VERSION
