"""Minimal async HTTP client, shared by the HTTP probe and the webhook notifier.

On the ESP32 both plain HTTP and HTTPS run on non-blocking sockets driven by
asyncio streams. asyncio.open_connection() wraps the socket with
do_handshake_on_connect=False and drives the handshake through the event loop,
so a secure request no longer stalls every other monitor. That is precisely
what the ESP8266's axTLS could not do, and the reason this project changed
boards -- the blocking-socket workaround that used to live here is gone.

Certificate verification is off unless a CA file is configured. Stock
MicroPython ESP32 firmware carries no root store at all: the ESP-IDF
certificate bundle is disabled in the port's sdkconfig.base, commented
"Disable certificate bundle as it's not implemented in MicroPython". With no
roots there is nothing to verify against, so the default is an encrypted but
unauthenticated connection -- and it says so, loudly, once.
"""

import asyncio
import gc
import time

import log

# mbedTLS reports a rejected chain as OSError(-0x2700, "X509 - Certificate
# verification failed"). Worth telling apart from an unreachable host, because
# the causes and the fixes are unrelated: a wrong CA file, or an RTC that has
# not been NTP-synced yet, since verification checks the validity window.
CERT_VERIFY_FAILED = -0x2700
# ...but only on ports that surface the code. MicroPython 1.29 on the ESP32-C3
# raises ValueError with mbedTLS's own message text instead, so the numeric
# match never fires there and a rejected chain would be counted as an ordinary
# error. Matched on substrings because the message concatenates one line per
# reason ("expired" and "not correctly signed" often arrive together).
CERT_VERIFY_MESSAGES = ("trusted CA", "certificate validity has expired",
                        "CRL", "X509")
# mbedTLS error codes are large negative numbers; socket errnos are positive.
# The port passes small negative values through as real errnos, so anything
# below this came from the TLS layer rather than the network.
TLS_ERROR_FLOOR = -256


class TlsState:
    """What the dashboard reports about TLS, and why the last attempt failed."""

    def __init__(self):
        self.unverified = False
        self.verified = False
        self.handshakes = 0
        self.verify_failures = 0
        self.last_error = None

    def summary(self):
        """What the dashboard prints. "verified" has to mean a chain actually
        verified, not merely that verification was switched on: the two came
        apart on the C3, where every handshake was being rejected while the
        dashboard still read "verified" and reported no failures."""
        if not (self.verified or self.unverified):
            return "unused"
        if self.unverified:
            return "UNVERIFIED"
        if self.verify_failures and not self.handshakes:
            return "VERIFY FAILING"
        return "verified"


tls_state = TlsState()

_contexts = {}


def tls_context(ca_file):
    """Returns a cached SSLContext for this CA file; None means no verification.

    Cached because building one parses the CA chain, which costs time and a few
    KB of heap. Alerts come from a fixed handful of notifiers, so the cache
    stays small and bounded.
    """
    if ca_file in _contexts:
        return _contexts[ca_file]

    import ssl

    gc.collect()
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)

    if ca_file is None:
        # PROTOCOL_TLS_CLIENT defaults to CERT_REQUIRED, which can never
        # succeed with no roots loaded, so turning it off has to be explicit.
        context.verify_mode = ssl.CERT_NONE
        tls_state.unverified = True
        log.warning("httpc", "TLS certificate verification is OFF. Traffic is "
                    "encrypted, but the server is not authenticated -- anyone "
                    "on-path can impersonate it. Set tls.ca_file in config.json.")
    else:
        # By keyword, and by path rather than by contents. The first
        # positional argument is cafile on both MicroPython and CPython, so
        # passing the PEM text positionally hands the whole certificate to
        # open() as a filename and fails with ENOENT -- a missing-file error
        # for a file that is present and readable. Letting mbedtls read it
        # also keeps 8 KB of PEM off a heap that has ~90 KB to spare.
        # Several PEM roots may be concatenated in one file, which is what
        # keeps a CA rotation at the far end from silently killing alerting.
        context.load_verify_locations(cafile=ca_file)
        context.verify_mode = ssl.CERT_REQUIRED
        tls_state.verified = True
        log.info("httpc", "TLS verification enabled from {}".format(ca_file))

    _contexts[ca_file] = context
    return context


def describe_tls_error(error):
    """Turns an exception from a handshake into something worth printing."""
    code = error.args[0] if error.args else None

    if code == CERT_VERIFY_FAILED or (
            isinstance(code, str)
            and any(text in code for text in CERT_VERIFY_MESSAGES)):
        tls_state.verify_failures += 1
        detail = " ({})".format(" ".join(code.split())) if isinstance(code, str) else ""
        return ("certificate verification failed -- wrong CA file, or the "
                "clock is not NTP-synced yet{}".format(detail))

    if isinstance(code, int) and code < TLS_ERROR_FLOOR:
        return "TLS handshake failed ({})".format(error)

    return str(error)


def parse_url(url):
    """Splits a URL into (secure, host, port, path)."""
    if url.startswith("https://"):
        secure = True
        rest = url[8:]
        port = 443
    elif url.startswith("http://"):
        secure = False
        rest = url[7:]
        port = 80
    else:
        raise ValueError("URL must start with http:// or https://")

    separator = rest.find("/")
    if separator == -1:
        authority, path = rest, "/"
    else:
        authority, path = rest[:separator], rest[separator:]

    if ":" in authority:
        host, _, port_text = authority.rpartition(":")
        port = int(port_text)
    else:
        host = authority

    if not host:
        raise ValueError("URL is missing a host")

    return secure, host, port, path


def build_request(method, host, port, secure, path, headers=None, body=None):
    default_port = 443 if secure else 80
    authority = host if port == default_port else "{}:{}".format(host, port)

    lines = [
        "{} {} HTTP/1.0".format(method, path),
        "Host: {}".format(authority),
        "Connection: close",
    ]

    if body is not None:
        lines.append("Content-Length: {}".format(len(body)))

    if headers:
        for name in headers:
            lines.append("{}: {}".format(name, headers[name]))

    request = "\r\n".join(lines) + "\r\n\r\n"
    if body is not None:
        request += body

    return request.encode("utf-8")


def parse_status(status_line):
    """Pulls the numeric code out of a status line, or None if malformed."""
    parts = status_line.split(None, 2)
    if len(parts) < 2:
        return None
    try:
        return int(parts[1])
    except ValueError:
        return None


async def request(method, url, headers=None, body=None, timeout=5, ca_file=None):
    """Performs a request and returns (status_code, latency_ms).

    status_code is None when the request failed to produce a usable response.
    ca_file applies only to https:// URLs; None means no verification.
    """
    secure, host, port, path = parse_url(url)
    payload = build_request(method, host, port, secure, path, headers, body)

    context = None
    if secure:
        try:
            context = tls_context(ca_file)
        except (OSError, ValueError) as error:
            # A missing or malformed CA file must not look like a down service.
            tls_state.last_error = "CA file {}: {}".format(ca_file, error)
            log.error("httpc", tls_state.last_error)
            return (None, -1)

        gc.collect()  # the handshake wants a few KB of contiguous heap

    reader = writer = None
    try:
        # open_connection does the non-blocking connect, and for TLS defers the
        # handshake so it is driven by the event loop rather than blocking it.
        # It also sources EINPROGRESS from errno, which matters here: the ESP32
        # reports 119 where the ESP8266 reported 115.
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port, ssl=context,
                                    server_hostname=host if secure else None),
            timeout)

        writer.write(payload)
        await asyncio.wait_for(writer.drain(), timeout)

        started = time.ticks_ms()
        status_line = await asyncio.wait_for(reader.readline(), timeout)
        latency = time.ticks_diff(time.ticks_ms(), started)

        # Counted here rather than after open_connection: the handshake is
        # deferred, so it only really happens on this first write and read.
        if secure:
            tls_state.handshakes += 1
    except asyncio.TimeoutError:
        if secure:
            tls_state.last_error = "timed out after {}s".format(timeout)
        return (None, -1)
    except (OSError, ValueError, MemoryError) as error:
        message = describe_tls_error(error) if secure else str(error)
        if secure:
            # Only TLS failures, so the dashboard's tls_last_error is not
            # muddied by an ordinary unreachable host.
            tls_state.last_error = message
        log.warning("httpc", "request to {} failed: {}".format(host, message))
        return (None, -1)
    finally:
        if writer is not None:
            try:
                writer.close()
                await writer.wait_closed()
            except (OSError, ValueError):
                pass
        # Not just tidiness: a wait_for that times out inside open_connection
        # abandons a socket we never got a handle on, and collecting here runs
        # its finaliser. lwIP allows only a handful of sockets, so leaving that
        # to chance would eventually starve the probes.
        gc.collect()

    if not status_line:
        return (None, -1)

    return (parse_status(status_line.decode("utf-8")), latency)
