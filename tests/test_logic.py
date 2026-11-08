"""Logic tests for the parts that are pure computation or state.

    python3 tests/test_logic.py

The probes themselves need MicroPython sockets and a board, so they are not
covered here; what is covered is the alert state machine, the history buffer,
the chart, and URL/status parsing.
"""

import asyncio
import errno
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tests.micropython_shim  # noqa: F401

import chart  # noqa: E402
import httpc  # noqa: E402
import monitors  # noqa: E402
from monitors import Monitor  # noqa: E402
from utils import FAILED, Ring, format_duration  # noqa: E402

failures = []

# check_ca_file only looks for the PEM header, so this need not be a real key.
FAKE_CERT = ("-----BEGIN CERTIFICATE-----\n"
             "MIIBkTCB+wIJAP8AAAAAAAABMA0GCSqGSIb3DQEBCwUAMBExDzANBgNVBAMMBnRl\n"
             "-----END CERTIFICATE-----\n")


def check(label, actual, expected):
    if actual == expected:
        print("  ok   {}".format(label))
    else:
        print("  FAIL {}: expected {!r}, got {!r}".format(label, expected, actual))
        failures.append(label)


class RecordingNotifier:
    """Records alerts, and can be told to fail delivery."""

    def __init__(self, name="rec", deliver=True):
        self.name = name
        self.deliver = deliver
        self.sent = []

    async def post(self, subject, body, monitor, status):
        self.sent.append(status)
        return self.deliver


def make_monitor(notifiers, threshold=3):
    return Monitor({
        "name": "svc",
        "type": "icmp",
        "endpoint": "10.0.0.1",
        "max_consecutive_failures": threshold,
        "history_length": 8,
    }, notifiers)


def test_ring():
    print("Ring")
    ring = Ring(4)
    check("empty length", len(ring), 0)
    check("empty latest", ring.latest(), FAILED)
    check("empty stats", ring.stats(), (FAILED, FAILED, FAILED, FAILED, FAILED, 0.0))

    for value in (10, 20, 30):
        ring.append(value)
    check("partial order", list(ring), [10, 20, 30])

    ring.append(40)
    ring.append(50)
    check("wraps and drops oldest", list(ring), [20, 30, 40, 50])
    check("length caps at size", len(ring), 4)
    check("latest", ring.latest(), 50)

    mixed = Ring(4)
    for value in (100, FAILED, 200, FAILED):
        mixed.append(value)
    current, minimum, average, maximum, p95, uptime = mixed.stats()
    check("ignores failures in min", minimum, 100)
    check("ignores failures in max", maximum, 200)
    check("ignores failures in avg", average, 150)
    check("uptime percent", uptime, 50.0)
    check("current is the failure", current, FAILED)

    clamped = Ring(2)
    clamped.append(99999)
    check("clamps to int16", clamped.latest(), 32000)


def test_chart():
    print("chart")
    check("nice_max rounds up", [chart.nice_max(v) for v in (0, 3, 45, 120, 999)],
          [10, 5, 50, 200, 1000])

    ring = Ring(6)
    for value in (10, 20, 30, 20, 10, 20):
        ring.append(value)
    rows = list(chart.plot(ring, height=5))
    check("row count matches height", len(rows), 5)
    check("baseline uses the zero cross", rows[-1].startswith("    0┼"), True)

    failed = Ring(3)
    for _ in range(3):
        failed.append(FAILED)
    marked = list(chart.plot(failed, height=3))
    check("failures marked on baseline", marked[-1].count("╳"), 3)

    check("empty ring still renders an axis", list(chart.plot(Ring(4))), ["    0┼"])


def test_url_parsing():
    print("httpc")
    check("plain http", httpc.parse_url("http://example.com/health"),
          (False, "example.com", 80, "/health"))
    check("https default port", httpc.parse_url("https://example.com"),
          (True, "example.com", 443, "/"))
    check("explicit port", httpc.parse_url("http://example.com:8080/a/b"),
          (False, "example.com", 8080, "/a/b"))
    check("status line", httpc.parse_status("HTTP/1.1 404 Not Found"), 404)
    check("malformed status line", httpc.parse_status("garbage"), None)

    try:
        httpc.parse_url("example.com")
        check("rejects a schemeless url", "no error", "ValueError")
    except ValueError:
        check("rejects a schemeless url", "ValueError", "ValueError")


def test_alert_template():
    print("alert templates")
    monitors.set_alert_template(None)
    monitor = make_monitor([])
    monitor.failures = 2
    check("default mentions the service",
          "svc" in monitors.format_alert(monitor, "offline"), True)

    monitors.set_alert_template("{name}/{status}/{failures}")
    check("custom template", monitors.format_alert(monitor, "offline"), "svc/offline/2")

    monitors.set_alert_template("{nonsense}")
    check("a broken template still produces an alert",
          "svc" in monitors.format_alert(monitor, "offline"), True)
    monitors.set_alert_template(None)


async def test_alert_latch():
    print("alert state machine")
    notifier = RecordingNotifier()
    monitor = make_monitor([notifier], threshold=3)

    await monitor.record_failure()
    check("no alert below threshold", notifier.sent, [])
    check("state is warn", monitor.state(), "warn")

    await monitor.record_failure()
    await monitor.record_failure()
    check("alerts at the threshold", notifier.sent, ["offline"])
    check("state is fail", monitor.state(), "fail")

    await monitor.record_failure()
    await monitor.record_failure()
    check("does not alert repeatedly", notifier.sent, ["offline"])

    await monitor.record_success(12)
    check("alerts on recovery", notifier.sent, ["offline", "online"])
    check("state is ok", monitor.state(), "ok")

    await monitor.record_success(12)
    check("no repeat recovery alert", notifier.sent, ["offline", "online"])


async def test_delivery_retry():
    print("delivery retry")
    notifier = RecordingNotifier(deliver=False)
    monitor = make_monitor([notifier], threshold=1)

    await monitor.record_failure()
    check("attempted once", len(notifier.sent), 1)
    check("latch stays clear after a failed send", monitor.notified, [False])

    await monitor.record_failure()
    check("retries on the next check", len(notifier.sent), 2)

    notifier.deliver = True
    await monitor.record_failure()
    check("latch sets once delivered", monitor.notified, [True])

    await monitor.record_failure()
    check("stops once delivered", len(notifier.sent), 3)
    check("counts only delivered alerts", monitor.alerts_sent, 1)


async def test_notifier_exception():
    print("notifier errors")

    class Exploding:
        name = "boom"

        async def post(self, subject, body, monitor, status):
            raise OSError("smtp is down")

    monitor = make_monitor([Exploding()], threshold=1)
    await monitor.record_failure()
    check("a raising notifier does not propagate", monitor.notified, [False])


def test_config():
    print("config validation")
    import config as configuration

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    # config.example.json points tls.ca_file at ca.pem, and the loader checks
    # the file is really there. On the board it sits next to config.json; here
    # it has to be conjured up next to the example for the duration.
    ca_path = os.path.join(root, "ca.pem")
    ca_existed = os.path.exists(ca_path)
    if not ca_existed:
        with open(ca_path, "w") as handle:
            handle.write(FAKE_CERT)

    try:
        settings = configuration.load(os.path.join(root, "config.example.json"))
    finally:
        if not ca_existed:
            os.unlink(ca_path)

    check("example config loads", len(settings["monitors"]), 5)
    check("example config notifiers", len(settings["notifiers"]), 3)
    check("webrepl password read", settings["webrepl_password"], "pingpong")
    check("service without a notifier list gets all",
          len(settings["monitors"][0].notifiers), 3)
    check("service with a notifier list gets only those",
          [n.name for n in settings["monitors"][3].notifiers], ["ops-mail"])
    check("watchdog timeout read", settings["watchdog_ms"], 30000)
    check("led pin read", settings["led_pin"], 8)
    check("cpu frequencies read", (settings["idle_mhz"], settings["busy_mhz"]), (80, 160))
    check("global ca file applies to an https notifier",
          settings["notifiers"][1].ca_file, "ca.pem")
    check("an explicit null ca_file opts a service out",
          settings["monitors"][2].ca_file, None)

    def expect_error(label, document, fragment):
        handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        handle.write(json.dumps(document) if not isinstance(document, str) else document)
        handle.close()
        try:
            configuration.load(handle.name)
            check(label, "no error", "ConfigError containing {!r}".format(fragment))
        except configuration.ConfigError as error:
            check(label, fragment in str(error), True)
        finally:
            os.unlink(handle.name)

    base = {"network": {"ssid": "a", "password": "b"}, "notifiers": [], "services": []}

    expect_error("rejects invalid json", "{not json", "not valid JSON")
    expect_error("names a missing network key",
                 {"network": {"ssid": "a"}}, "'password'")
    expect_error("rejects an empty service list", base, "no services")
    expect_error("names an unknown service type",
                 dict(base, services=[{"name": "s", "type": "gopher", "endpoint": "x"}]),
                 "unknown type 'gopher'")
    expect_error("requires a URL for http services",
                 dict(base, services=[{"name": "s", "type": "http", "endpoint": "host"}]),
                 "full URL")
    expect_error("catches an unknown notifier reference",
                 dict(base, services=[{"name": "s", "type": "icmp", "endpoint": "x",
                                       "notifiers": ["ghost"]}]),
                 "unknown notifier 'ghost'")
    expect_error("catches duplicate service names",
                 dict(base, services=[{"name": "s", "type": "icmp", "endpoint": "x"},
                                      {"name": "s", "type": "icmp", "endpoint": "y"}]),
                 "duplicate service name")
    expect_error("validates the webrepl password length",
                 dict(base, webrepl={"password": "no"},
                      services=[{"name": "s", "type": "icmp", "endpoint": "x"}]),
                 "4 and 9 characters")
    expect_error("rejects a non-integer interval",
                 dict(base, services=[{"name": "s", "type": "icmp", "endpoint": "x",
                                       "update_frequency": "often"}]),
                 "must be an integer")

    icmp = [{"name": "s", "type": "icmp", "endpoint": "x"}]
    expect_error("names a missing CA file",
                 dict(base, tls={"ca_file": "nope.pem"}, services=icmp),
                 "is not on the board")
    expect_error("rejects a CPU frequency the chip cannot take",
                 dict(base, cpu={"busy_mhz": 133}, services=icmp),
                 "must be one of")
    expect_error("rejects a CPU frequency that would take Wi-Fi down",
                 dict(base, cpu={"idle_mhz": 40}, services=icmp),
                 "at least 80 MHz")
    expect_error("rejects an unknown email security mode",
                 dict(base, services=icmp,
                      notifiers=[{"name": "m", "type": "email", "security": "ssl",
                                  "recipient_addresses": ["a@b.c"],
                                  "smtp_server": "s", "username": "u",
                                  "password": "p"}]),
                 "must be one of")
    expect_error("requires a pin when led is present",
                 dict(base, led={"active_low": True}, services=icmp),
                 "'pin' is required")
    expect_error("rejects a too-short watchdog timeout",
                 dict(base, watchdog={"timeout_ms": 100}, services=icmp),
                 "integer >= 1000")

    with_ca = dict(base, services=icmp, watchdog=False)
    handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
    handle.write(json.dumps(with_ca))
    handle.close()
    try:
        settings = configuration.load(handle.name)
        check("watchdog false disables it", settings["watchdog_ms"], None)
        check("no led by default", settings["led_pin"], None)
        check("no ca file by default", settings["ca_file"], None)
    finally:
        os.unlink(handle.name)


def test_icmp_parsing():
    """The packet handling is pure, even though the socket work is not."""
    print("icmp")
    import struct

    def ip_packet(header_length=20, icmp_type=monitors.ICMP_ECHO_REPLY,
                  identifier=0x1234, sequence=2):
        header = bytearray(header_length)
        header[0] = 0x40 | (header_length // 4)  # version 4, IHL in words
        icmp = struct.pack("!BBHHH", icmp_type, 0, 0, identifier, sequence)
        return bytes(header) + icmp + monitors.ICMP_PAYLOAD

    check("reads a standard 20-byte header",
          monitors.parse_echo_reply(ip_packet()), (0, 0x1234, 2))
    # The old fixed-offset parse read the wrong bytes here and reported the
    # service down; the length now comes from the packet.
    check("reads a header carrying IP options",
          monitors.parse_echo_reply(ip_packet(header_length=24)), (0, 0x1234, 2))
    check("rejects a runt packet", monitors.parse_echo_reply(b"\x45" * 8), None)
    check("rejects a packet cut short of the icmp header",
          monitors.parse_echo_reply(ip_packet()[:24]), None)
    check("rejects a nonsense header length",
          monitors.parse_echo_reply(b"\x41" + bytes(40)), None)

    check("one echo per check by default", make_monitor([]).icmp_attempts, 1)

    request = monitors.echo_request(0xBEEF, 1)
    check("request is 32 bytes", len(request), 32)
    check("request is an echo request", request[0], monitors.ICMP_ECHO_REQUEST)
    # A correct checksum over a packet that already contains it sums to zero.
    check("request checksum verifies", monitors.checksum(request), 0)
    check("request carries the id and sequence",
          struct.unpack("!HH", request[4:8]), (0xBEEF, 1))


async def test_icmp_send_retries():
    """A send that never left the board is retried; a lost reply is not."""
    print("icmp send")

    class FakeSocket:
        """Fails the first `failures` sends, then accepts everything."""

        def __init__(self, failures=0, returns_none=False):
            self.remaining = failures
            self.returns_none = returns_none
            self.sends = 0
            self.sent = []

        def send(self, data):
            self.sends += 1
            if self.remaining:
                self.remaining -= 1
                if self.returns_none:
                    return None
                raise OSError(errno.ENOBUFS)
            self.sent.append(bytes(data))
            return len(data)

    sock = FakeSocket()
    check("a clean send goes out once",
          (await monitors.send_echo_request(sock, 0x1234, 0, 5), sock.sends),
          (True, 1))
    check("the request on the wire is the echo request",
          sock.sent[0], monitors.echo_request(0x1234, 0))

    sock = FakeSocket(failures=monitors.ICMP_SEND_RETRIES)
    check("a transient send error is retried rather than recorded",
          (await monitors.send_echo_request(sock, 0x1234, 0, 5), sock.sends),
          (True, monitors.ICMP_SEND_RETRIES + 1))

    # A non-blocking socket signals "no room" by returning None as well as by
    # raising, and both mean the packet never reached the network.
    sock = FakeSocket(failures=1, returns_none=True)
    check("a None return counts as a failed send, not a sent packet",
          (await monitors.send_echo_request(sock, 0x1234, 0, 5), sock.sends),
          (True, 2))

    sock = FakeSocket(failures=99)
    check("a persistent send error gives up after the retries",
          (await monitors.send_echo_request(sock, 0x1234, 0, 5), sock.sends),
          (False, monitors.ICMP_SEND_RETRIES + 1))

    # The retry budget must not outlive the check's own timeout.
    sock = FakeSocket(failures=99)
    check("retries stop at the probe timeout",
          await monitors.send_echo_request(sock, 0x1234, 0, 0), False)
    check("a zero timeout still tries once", sock.sends, 1)


def test_tls_state():
    """A rejected chain has to be counted and shown, however the port reports it."""
    print("tls state")

    def fresh():
        state = httpc.TlsState()
        httpc.tls_state = state
        return state

    # MicroPython 1.29 on the ESP32-C3 raises ValueError with mbedTLS's text;
    # other ports surface the numeric code. Both are the same event.
    state = fresh()
    message = httpc.describe_tls_error(
        ValueError("\nThe certificate is not correctly signed by the trusted CA\n"))
    check("a string-form rejection is counted", state.verify_failures, 1)
    check("a string-form rejection is explained",
          "certificate verification failed" in message, True)

    state = fresh()
    httpc.describe_tls_error(OSError(httpc.CERT_VERIFY_FAILED))
    check("the numeric form is still counted", state.verify_failures, 1)

    state = fresh()
    httpc.describe_tls_error(OSError(104, "ECONNRESET"))
    check("an ordinary socket error is not a verify failure",
          state.verify_failures, 0)

    # The dashboard's TLS line is Phase 0's pass criterion, so "verified" must
    # mean a chain verified -- not merely that verification was switched on.
    state = fresh()
    check("unused before any request", state.summary(), "unused")
    state.verified = True
    state.verify_failures = 1
    check("failing verification is not reported as verified",
          state.summary(), "VERIFY FAILING")
    state.handshakes = 1
    check("a successful handshake reads as verified",
          state.summary(), "verified")
    state = fresh()
    state.unverified = True
    check("verification off is shouted about", state.summary(), "UNVERIFIED")


def test_settings_contract():
    """main.py cannot be imported (boot() runs on import), so check statically.

    Every settings["key"] main.py reads has to be one config.load() actually
    returns. Getting this wrong is a boot-time KeyError on the board and a
    silent pass everywhere else, which is the worst way to find out.
    """
    print("settings contract")
    import ast

    import config as configuration

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "main.py")) as handle:
        tree = ast.parse(handle.read())

    read = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Subscript)
                and isinstance(node.value, ast.Name)
                and node.value.id == "settings"
                and isinstance(node.slice, ast.Constant)):
            read.add(node.slice.value)

    handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
    handle.write(json.dumps({
        "network": {"ssid": "a", "password": "b"},
        "services": [{"name": "s", "type": "icmp", "endpoint": "x"}],
    }))
    handle.close()
    try:
        provided = set(configuration.load(handle.name))
    finally:
        os.unlink(handle.name)

    check("main.py reads a plausible number of settings", len(read) > 8, True)
    check("every setting main.py reads is provided", sorted(read - provided), [])


def strip_ansi(text):
    import re

    return re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", text)


def test_terminal():
    print("terminal view")
    import tests.render_terminal as render
    import tui

    for compact in (False, True):
        terminal, client = render.build_terminal(compact)
        frame = [strip_ansi(line) for line in terminal.frame(client)]
        boxed = [line for line in frame if line and line[0] in "┌│└"]
        check("every boxed line is {} columns ({})".format(
            tui.WIDTH, "compact" if compact else "full"),
            sorted({len(line) for line in boxed}), [tui.WIDTH])

    terminal, client = render.build_terminal(False)
    names = [monitor.name.upper() for monitor in terminal.dashboard.monitors]
    text = "\n".join(strip_ansi(line) for line in terminal.frame(client))
    check("full mode shows every service", all(name in text for name in names), True)

    terminal, client = render.build_terminal(True)
    text = "\n".join(strip_ansi(line) for line in terminal.frame(client))
    check("compact mode charts only the selected service", text.count("ms over last"), 1)
    check("compact mode marks the selection", "▸ GATEWAY" in text, True)

    def press(data):
        state = tui.NORMAL
        for byte in data:
            state = terminal.feed(client, state, byte)
        return state

    press(b"\x1b[B")
    check("down arrow selects the next service", client.selected, 1)
    press(b"\x1b[A\x1b[A")
    check("up arrow wraps around", client.selected, len(terminal.dashboard.monitors) - 1)
    check("telnet negotiation is ignored", press(b"\xff\xfd\x18\xff\xfa\x1f\x00\x50\x00\x18\xff\xf0"),
          tui.NORMAL)
    check("negotiation does not quit", client.done, False)
    press(b"\xff\xf4")
    check("telnet interrupt quits", client.done, True)
    client.done = False
    press(b"q")
    check("q quits", client.done, True)


class FakeStream:
    def __init__(self, incoming=b""):
        self.incoming = incoming
        self.out = []
        self.closed = False

    async def read(self, count):
        while not self.incoming:
            await asyncio.sleep(0.02)
        data, self.incoming = self.incoming[:count], self.incoming[count:]
        return data

    def write(self, data):
        self.out.append(data)

    async def drain(self):
        pass

    def close(self):
        self.closed = True

    async def wait_closed(self):
        pass


async def test_terminal_session():
    print("terminal session")
    import tests.render_terminal as render

    terminal, _ = render.build_terminal(False)
    terminal.refresh_seconds = 60
    stream = FakeStream(b"\xff\xfb\x18")
    task = asyncio.create_task(terminal.handle(stream, stream))
    await asyncio.sleep(0.5)
    text = "".join(part for part in stream.out if isinstance(part, str))
    check("first frame is drawn", "MINUTEPING" in text and "GATEWAY" in text, True)
    check("telnet negotiation is sent", stream.out[0][:3], b"\xff\xfb\x01")
    check("one viewer is counted", terminal.clients, 1)

    stream.incoming = b"q"
    await asyncio.wait_for(task, 5)
    check("q ends the session and closes the socket", stream.closed, True)
    check("viewer count returns to zero", terminal.clients, 0)
    check("cursor is restored", "\x1b[?25h" in "".join(
        part for part in stream.out if isinstance(part, str)), True)

    first = FakeStream()
    second = FakeStream()
    extra = FakeStream()
    tasks = [asyncio.create_task(terminal.handle(first, first)),
             asyncio.create_task(terminal.handle(second, second))]
    await asyncio.sleep(0.3)
    await terminal.handle(extra, extra)
    check("a third viewer is turned away", "too many viewers" in "".join(extra.out), True)
    for stream_, task_ in ((first, tasks[0]), (second, tasks[1])):
        stream_.incoming = b"q"
        await asyncio.wait_for(task_, 5)


def test_boot_report():
    print("boot report")
    import tests.render_terminal as render

    terminal, _ = render.build_terminal()
    report = terminal.dashboard.boot_report(True, 2323)
    for label, needle in (("signal", "signal -58 dBm"), ("reset cause", "Reset cause: POWER ON"),
                          ("ip", "192.168.1.50"), ("services", "gateway, homepage"),
                          ("telnet hint", "telnet 192.168.1.50 2323")):
        check("boot report includes " + label, needle in report, True)
    check("without telnet there is no telnet hint",
          "telnet" in terminal.dashboard.boot_report(True, None), False)
    check("without the web server there is no http hint",
          "http://" in terminal.dashboard.boot_report(False, 2323), False)
    check("with neither there is no View line",
          "View:" in terminal.dashboard.boot_report(False, None), False)


def test_telnet_config():
    print("telnet config")
    import config as configuration

    build = configuration.build_telnet
    check("absent means off", build({}), None)
    check("false means off", build({"telnet_server": False}), None)
    check("true uses defaults", build({"telnet_server": True}),
          {"port": 2323, "refresh_seconds": 5})
    check("object overrides", build({"telnet_server": {"port": 23, "refresh_seconds": 2}}),
          {"port": 23, "refresh_seconds": 2})
    for label, value in (("bad type", "yes"), ("port too big", {"port": 70000}),
                         ("zero refresh", {"refresh_seconds": 0})):
        try:
            build({"telnet_server": value})
            check("rejects " + label, True, False)
        except configuration.ConfigError:
            check("rejects " + label, True, True)


def test_formatting():
    print("formatting")
    check("under a day", format_duration(3671), "01:01:11")
    check("with days", format_duration(4 * 86400 + 7325), "4d 02:02:05")


async def main():
    test_ring()
    test_chart()
    test_url_parsing()
    test_alert_template()
    await test_alert_latch()
    await test_delivery_retry()
    await test_notifier_exception()
    test_config()
    test_icmp_parsing()
    test_tls_state()
    await test_icmp_send_retries()
    test_settings_contract()
    test_formatting()
    test_terminal()
    await test_terminal_session()
    test_telnet_config()
    test_boot_report()

    print()
    if failures:
        print("{} FAILED: {}".format(len(failures), ", ".join(failures)))
        sys.exit(1)
    print("all logic tests passed")


if __name__ == "__main__":
    asyncio.run(main())
