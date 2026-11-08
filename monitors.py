"""Service monitoring: one Monitor per configured service, plus the probes.

Probes are plain async functions selected by a dict rather than a subclass per
type; each returns a latency in milliseconds, or utils.FAILED.
"""

import asyncio
import errno
import os
import select
import socket
import struct
import time

import httpc
import log
from utils import FAILED, Ring, board, rtc_timestamp, led_off, led_on

# Read from errno rather than hardcoded: the ESP8266 reported EINPROGRESS as
# 115, but the ESP32 passes lwIP's own value through and reports 119. Taking
# it from the module means a non-blocking connect is judged correctly on
# whichever port this ends up running on.
CONNECT_IN_PROGRESS = errno.EINPROGRESS

ICMP_PROTOCOL = 1  # socket.IPPROTO_ICMP is not exported by the esp32 port
ICMP_ECHO_REQUEST = 8
ICMP_ECHO_REPLY = 0
ICMP_PAYLOAD = b"minutePing" + b"Q" * 14  # 24 bytes, so the message is 32
# One echo per check, so a lost packet is recorded as a lost packet. Retrying
# here would hide loss inside a "successful" check: at three attempts a link
# dropping half its packets still reports ~87% uptime. Smoothing over transient
# blips is what max_consecutive_failures is for, and that layer is visible on
# the dashboard instead of silently rewriting the history. Raise it per service
# only for a link where reachability matters and loss genuinely does not.
ICMP_ATTEMPTS = 1
# Retries for a send that never left the board, which is a different thing
# from a packet the network lost: see send_echo_request. Two is enough for
# lwIP to free a pbuf; more would only delay the check behind a fault that is
# not going to clear.
ICMP_SEND_RETRIES = 2
# Enough for a 60-byte IP header with options, the 8-byte ICMP header and the
# echoed payload, read in one go because a raw socket read takes one datagram.
ICMP_READ_SIZE = 128
MIN_IP_HEADER = 20

DEFAULT_ALERT_TEMPLATE = ("Service {name} ({type} {endpoint}) is {status} "
                          "after {failures} consecutive failed checks.\n"
                          "Reported by {hostname} ({board_ip}) at {time} UTC, "
                          "up {uptime}.")

_alert_template = DEFAULT_ALERT_TEMPLATE


def set_alert_template(template):
    global _alert_template
    _alert_template = template or DEFAULT_ALERT_TEMPLATE


def format_alert(monitor, status):
    """Renders the configured alert template for a monitor's current state."""
    fields = dict(
        name=monitor.name,
        type=monitor.type,
        endpoint=monitor.endpoint,
        status=status,
        failures=monitor.failures,
        latency=monitor.history.latest(),
        time=rtc_timestamp(),
        board_ip=board.address(),
        hostname=board.hostname or "minutePing",
        uptime=board.uptime_text(),
    )
    try:
        return _alert_template.format(**fields)
    except (KeyError, IndexError, ValueError):
        # A bad template in config must never stop an alert going out.
        return DEFAULT_ALERT_TEMPLATE.format(**fields)


def split_host_port(endpoint, default_port):
    if ":" in endpoint:
        host, _, port_text = endpoint.rpartition(":")
        return host, int(port_text)
    return endpoint, default_port


def checksum(data):
    """Standard internet checksum over a bytes-like object."""
    if len(data) & 1:
        data += b"\0"

    total = 0
    for position in range(0, len(data), 2):
        total += (data[position] << 8) + data[position + 1]

    while total >= 0x10000:
        total = (total & 0xFFFF) + (total >> 16)

    return ~total & 0xFFFF


def echo_request(identifier, sequence):
    """Builds an echo request with its checksum filled in."""
    header = struct.pack("!BBHHH", ICMP_ECHO_REQUEST, 0, 0, identifier, sequence)
    header = struct.pack("!BBHHH", ICMP_ECHO_REQUEST, 0,
                         checksum(header + ICMP_PAYLOAD), identifier, sequence)
    return header + ICMP_PAYLOAD


def parse_echo_reply(packet):
    """Returns (type, id, sequence), or None if this is not a usable reply.

    The IP header length is read from the packet rather than assumed to be 20
    bytes. A router that inserts IP options shifts the ICMP header along, and
    reading at a fixed offset would turn a perfectly healthy reply into a
    reported outage.
    """
    if len(packet) < MIN_IP_HEADER:
        return None

    header_length = (packet[0] & 0x0F) * 4
    if header_length < MIN_IP_HEADER or len(packet) < header_length + 8:
        return None

    reply_type, _, _, reply_id, reply_sequence = struct.unpack(
        "!BBHHH", packet[header_length:header_length + 8])

    return reply_type, reply_id, reply_sequence


async def send_echo_request(sock, identifier, sequence, timeout):
    """Puts one echo request on the wire, retrying a send that failed. Bool.

    A send that raises never reached the network -- ENOBUFS with lwIP short of
    pbufs, or EAGAIN with the driver queue full -- so it says nothing at all
    about the link, and recording it as a lost packet blames the network for
    the board's own problem. That is also why retrying is safe here and only
    here: the retry covers a local fault, so it destroys no measurement. A
    missing reply is the opposite -- that is the measurement, and it is never
    retried.

    Written against the socket rather than a StreamWriter because a failed
    drain() leaves the request sitting in the writer's buffer at an unknown
    offset, and the retry would then concatenate two requests into one
    malformed datagram. A raw socket takes the whole packet or none of it.
    """
    request = echo_request(identifier, sequence)
    deadline = time.ticks_add(time.ticks_ms(), int(timeout * 1000))

    for attempt in range(ICMP_SEND_RETRIES + 1):
        try:
            sent = sock.send(request)
            if sent is None:  # non-blocking socket with nowhere to put it
                raise OSError(errno.EAGAIN)
            if sent == len(request):
                return True
        except OSError:
            pass

        if attempt == ICMP_SEND_RETRIES:
            return False
        if time.ticks_diff(deadline, time.ticks_ms()) <= 0:
            return False

        await asyncio.sleep_ms(20)

    return False


async def await_echo_reply(reader, identifier, sequence, started, timeout):
    """Reads until our reply turns up or the deadline passes; ms, or None.

    A raw socket sees every ICMP packet from the peer, including late replies
    to attempts that already timed out, so a packet that is not ours must not
    be allowed to consume the attempt -- it just goes round again on whatever
    time is left.
    """
    deadline = time.ticks_add(started, int(timeout * 1000000))

    while True:
        remaining = time.ticks_diff(deadline, time.ticks_us())
        if remaining <= 0:
            return None

        reply = await asyncio.wait_for(
            reader.read(ICMP_READ_SIZE), remaining / 1000000)
        elapsed = time.ticks_diff(time.ticks_us(), started)

        if not reply:
            return None

        parsed = parse_echo_reply(reply)
        if parsed is None:
            continue

        reply_type, reply_id, reply_sequence = parsed
        if (reply_type == ICMP_ECHO_REPLY and reply_id == identifier
                and reply_sequence == sequence):
            # Timed in microseconds and rounded, not truncated: on a LAN the
            # round trip is often a millisecond or two, where truncation loses
            # most of the signal.
            return (elapsed + 500) // 1000


async def probe_icmp(monitor):
    """ICMP echo over a single raw socket; one attempt unless configured otherwise.

    One socket for the whole probe rather than one per attempt: the ESP32's
    lwIP has a hard socket limit -- the port has to force a GC when it hits
    ENFILE -- and several monitors retrying at once is exactly how you reach
    it. The address is resolved once for the same reason DNS is not free.
    """
    try:
        address = socket.getaddrinfo(monitor.endpoint, 1)[0][-1][0]
    except (IndexError, OSError):
        return FAILED

    identifier = struct.unpack("!H", os.urandom(2))[0]

    sock = socket.socket(socket.AF_INET, socket.SOCK_RAW, ICMP_PROTOCOL)
    sock.setblocking(False)

    try:
        sock.connect((address, 1))
    except OSError as error:
        if error.errno != CONNECT_IN_PROGRESS:
            sock.close()
            return FAILED

    reader = asyncio.StreamReader(sock)

    try:
        for sequence in range(monitor.icmp_attempts):
            if not await send_echo_request(
                    sock, identifier, sequence, monitor.timeout):
                # Nothing went out, so there is nothing measured. FAILED is
                # still the only answer a probe can give, but it is reported
                # as the board's fault rather than filed away silently as a
                # lost packet.
                log.error("icmp", "{}: send failed".format(monitor.name))
                return FAILED

            started = time.ticks_us()
            try:
                latency = await await_echo_reply(
                    reader, identifier, sequence, started, monitor.timeout)
            except (OSError, asyncio.TimeoutError, EOFError):
                # No reply came back inside the timeout. That is the
                # measurement, not an error: the attempt is spent.
                latency = None

            if latency is not None:
                return latency

        return FAILED
    finally:
        sock.close()
        reader.close()
        await reader.wait_closed()


async def probe_http(monitor):
    status, latency = await httpc.request(
        "GET", monitor.endpoint, timeout=monitor.timeout,
        ca_file=monitor.ca_file)

    if status is None:
        return FAILED

    return latency if status == monitor.success_code else FAILED


async def probe_tcp(monitor):
    """Connect-only port check.

    Polls for writability rather than awaiting drain(): MicroPython's drain()
    returns immediately when the write buffer is empty, so it would report a
    closed port as reachable. POLLERR/POLLHUP is what a refused connection
    raises on lwIP, and it is checked before POLLOUT because a refused socket
    reports both.
    """
    host, port = split_host_port(monitor.endpoint, 80)

    try:
        address = socket.getaddrinfo(host, port)[0][-1]
    except (IndexError, OSError):
        return FAILED

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setblocking(False)
    started = time.ticks_ms()

    try:
        sock.connect(address)
    except OSError as error:
        if error.errno != CONNECT_IN_PROGRESS:
            sock.close()
            return FAILED  # refused outright

    poller = select.poll()
    poller.register(sock, select.POLLOUT)
    deadline = time.ticks_add(started, int(monitor.timeout * 1000))

    try:
        while True:
            events = poller.poll(0)
            if events:
                flags = events[0][1]
                if flags & (select.POLLERR | select.POLLHUP):
                    return FAILED
                if flags & select.POLLOUT:
                    return time.ticks_diff(time.ticks_ms(), started)

            if time.ticks_diff(deadline, time.ticks_ms()) <= 0:
                return FAILED

            await asyncio.sleep_ms(20)
    except OSError:
        return FAILED
    finally:
        poller.unregister(sock)
        sock.close()


async def probe_dns(monitor):
    """Queries minuteping.test and accepts any well-formed reply as proof of life."""
    host, port = split_host_port(monitor.endpoint, 53)

    try:
        address = socket.getaddrinfo(host, port)[0][-1]
    except (IndexError, OSError):
        return FAILED

    query_id = os.urandom(2)
    query = (query_id
             + b"\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00"
             + b"\x0aminuteping\x04test\x00"
             + b"\x00\x01\x00\x01")

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setblocking(False)

    try:
        sock.connect(address)
    except OSError as error:
        if error.errno != CONNECT_IN_PROGRESS:
            sock.close()
            return FAILED

    reader = asyncio.StreamReader(sock)
    writer = asyncio.StreamWriter(sock, {})

    try:
        writer.write(query)
        await asyncio.wait_for(writer.drain(), monitor.timeout)

        started = time.ticks_ms()
        reply = await asyncio.wait_for(reader.read(12), monitor.timeout)
        latency = time.ticks_diff(time.ticks_ms(), started)
    except (OSError, asyncio.TimeoutError):
        return FAILED
    finally:
        sock.close()
        writer.close()
        await writer.wait_closed()

    if not reply or len(reply) < 4:
        return FAILED

    # Matching id plus the response bit is enough to prove the resolver answered;
    # the RCODE itself does not matter.
    if reply[0:2] == query_id and reply[2] & 0x80:
        return latency

    return FAILED


PROBES = {
    "icmp": probe_icmp,
    "http": probe_http,
    "dns": probe_dns,
    "tcp": probe_tcp,
}


class Monitor:
    """One monitored service: its schedule, history, and alert state."""

    def __init__(self, config, notifiers):
        self.name = config["name"]
        self.type = config["type"]
        self.endpoint = config["endpoint"]
        self.update_frequency = config.get("update_frequency", 180)
        self.timeout = config.get("timeout", 5)
        self.success_code = config.get("success_code", 200)
        self.max_consecutive_failures = config.get("max_consecutive_failures", 3)
        # Only meaningful for https endpoints; None means no verification.
        self.ca_file = config.get("ca_file")
        self.icmp_attempts = config.get("icmp_attempts", ICMP_ATTEMPTS)

        self.probe = PROBES[self.type]
        self.history = Ring(config.get("history_length", 60))

        self.notifiers = notifiers
        self.notified = [False] * len(notifiers)

        self.failures = 0
        self.online = True  # assume healthy until a check says otherwise
        self.total_checks = 0
        self.total_failures = 0
        self.alerts_sent = 0

        now = time.ticks_ms()
        self.last_check = now
        self.last_change = now
        self.checked_once = False

    def state(self):
        """One of 'ok', 'warn' (failing but under threshold) or 'fail'."""
        if not self.online:
            return "fail"
        if self.failures:
            return "warn"
        return "ok"

    def seconds_until_next_check(self):
        remaining = self.update_frequency - time.ticks_diff(
            time.ticks_ms(), self.last_check) // 1000
        return remaining if remaining > 0 else 0

    def seconds_since_change(self):
        return time.ticks_diff(time.ticks_ms(), self.last_change) // 1000

    async def run(self, start_delay=0):
        """The monitoring loop. Spawn with asyncio.create_task."""
        if start_delay:
            await asyncio.sleep(start_delay)

        while True:
            led_on()
            try:
                latency = await self.probe(self)
            except Exception as error:  # a probe must never kill its own task
                log.error("monitor", "{}: probe raised {}".format(self.name, error))
                latency = FAILED
            led_off()

            self.last_check = time.ticks_ms()
            self.checked_once = True
            self.total_checks += 1
            self.history.append(latency)

            if latency == FAILED:
                await self.record_failure()
            else:
                await self.record_success(latency)

            await asyncio.sleep(self.update_frequency)

    async def record_success(self, latency):
        log.info("monitor", "{} online {}ms".format(self.name, latency))

        self.failures = 0
        if not self.online:
            self.online = True
            self.last_change = time.ticks_ms()

        # Only tell anyone who was told about the outage.
        for index in range(len(self.notifiers)):
            if self.notified[index]:
                delivered = await self.deliver(index, "online")
                self.notified[index] = not delivered

    async def record_failure(self):
        self.failures += 1
        self.total_failures += 1
        log.warning("monitor", "{} offline ({}/{})".format(
            self.name, self.failures, self.max_consecutive_failures))

        if self.failures < self.max_consecutive_failures:
            return

        if self.online:
            self.online = False
            self.last_change = time.ticks_ms()
            log.error("monitor", "{} reached failure threshold".format(self.name))

        for index in range(len(self.notifiers)):
            if not self.notified[index]:
                self.notified[index] = await self.deliver(index, "offline")

    async def deliver(self, index, status):
        """Sends one alert. Returns True only if the notifier confirmed delivery."""
        notifier = self.notifiers[index]
        subject = "{} is {}".format(self.name, status)
        body = format_alert(self, status)

        try:
            delivered = await notifier.post(subject, body, self, status)
        except Exception as error:
            log.error("monitor", "notifier {} raised {}".format(notifier.name, error))
            return False

        if delivered:
            self.alerts_sent += 1

        return delivered
