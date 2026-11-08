from array import array
from machine import Pin, RTC
from machine import freq as _freq
import time

import log

rtc = RTC()

FAILED = -1  # latency sentinel for a check that did not succeed
MAX_LATENCY = 32000  # clamp so results always fit an int16


def set_freq(mhz):
    """Sets the CPU clock, tolerating a value this particular chip rejects.

    The allowed set varies by variant -- the ESP32-C3 stops at 160 MHz and
    raises ValueError for 240, where a plain ESP32 accepts it -- so a bad value
    in config costs a warning rather than a boot loop.
    """
    try:
        _freq(mhz * 1000000)
    except (ValueError, OSError) as error:
        log.warning("cpu", "frequency {} MHz rejected by this chip ({})".format(mhz, error))


def cpu_mhz():
    return _freq() // 1000000


class Led:
    """Status LED, on boards that have one.

    ESP32 boards disagree about this far more than the ESP8266 did: the pin
    number varies, so does the polarity, and plenty of dev boards have nothing
    usable at all -- on many ESP32-C3 boards GPIO8 drives an addressable RGB
    LED that will not respond to a plain digital write. So the pin comes from
    the config and anything that goes wrong degrades to a no-op, because a
    status light is never worth failing a boot over.
    """

    def __init__(self):
        self.pin = None
        self.active_low = True

    def configure(self, number, active_low=True):
        self.pin = None
        self.active_low = active_low

        if number is None:
            return

        try:
            self.pin = Pin(number, Pin.OUT, value=1 if active_low else 0)
        except (ValueError, TypeError) as error:
            log.warning("led", "pin {} is unusable ({}); continuing without it".format(
                number, error))

    def set(self, lit):
        if self.pin is not None:
            self.pin(0 if lit == self.active_low else 1)


led = Led()


def configure_led(number, active_low=True):
    led.configure(number, active_low)


def led_on():
    led.set(True)


def led_off():
    led.set(False)


class Ring:
    """Fixed-size circular buffer of int16 latencies in milliseconds.

    A sample of FAILED (-1) means the check failed. The backing array is
    allocated once at construction, so appending never reallocates and never
    fragments the heap.
    """

    def __init__(self, length=60):
        self.length = length
        self.buffer = array("h", bytearray(2 * length))
        self.index = 0  # where the next sample lands
        self.count = 0  # samples written, capped at length

    def append(self, latency):
        if latency > MAX_LATENCY:
            latency = MAX_LATENCY
        elif latency < FAILED:
            latency = FAILED

        self.buffer[self.index] = latency
        self.index = (self.index + 1) % self.length
        if self.count < self.length:
            self.count += 1

    def __len__(self):
        return self.count

    def __iter__(self):
        """Yields samples oldest to newest."""
        if self.count < self.length:
            start = 0
        else:
            start = self.index

        for offset in range(self.count):
            yield self.buffer[(start + offset) % self.length]

    def latest(self):
        if self.count == 0:
            return FAILED
        return self.buffer[(self.index - 1) % self.length]

    def stats(self):
        """Returns (current, minimum, average, maximum, p95, uptime_percent).

        Only successful samples feed the latency figures. With no successful
        samples the four latency values are all FAILED.
        """
        successful = [latency for latency in self if latency != FAILED]

        if not successful:
            return (FAILED, FAILED, FAILED, FAILED, FAILED, 0.0)

        successful.sort()
        average = sum(successful) // len(successful)
        uptime = 100.0 * len(successful) / self.count

        # Nearest rank, 1-based, so p95 of 20 samples is the 19th.
        rank = (95 * len(successful) + 99) // 100
        p95 = successful[rank - 1]

        return (self.latest(), successful[0], average, successful[-1], p95, uptime)


class Uptime:
    """Seconds since boot, accumulated so it survives ticks_ms rollover.

    time.ticks_ms wraps after about 12.4 days on MicroPython, so a plain
    subtraction from a boot timestamp would go backwards. tick() must be
    called well inside that period; the supervisor loop calls it twice a second.
    """

    def __init__(self):
        self.mark = time.ticks_ms()
        self.seconds = 0

    def tick(self):
        elapsed = time.ticks_diff(time.ticks_ms(), self.mark)
        if elapsed >= 1000:
            whole = elapsed // 1000
            self.seconds += whole
            self.mark = time.ticks_add(self.mark, whole * 1000)
        return self.seconds


def format_duration(seconds):
    """Compact uptime string: 4d 02:11:07, or 02:11:07 under a day."""
    seconds = int(seconds)
    days = seconds // 86400
    seconds -= days * 86400
    hours = seconds // 3600
    seconds -= hours * 3600
    minutes = seconds // 60
    seconds -= minutes * 60

    if days:
        return "{}d {:02d}:{:02d}:{:02d}".format(days, hours, minutes, seconds)
    return "{:02d}:{:02d}:{:02d}".format(hours, minutes, seconds)


def rtc_timestamp():
    """Current RTC time as YYYY-MM-DD HH:MM:SS. Only meaningful after an NTP sync."""
    year, month, day, _, hour, minute, second, _ = rtc.datetime()
    return "{:04d}-{:02d}-{:02d} {:02d}:{:02d}:{:02d}".format(
        year, month, day, hour, minute, second)


class Board:
    """What alerts need to say about this device: where it is and how it is doing.

    main.py fills it in once Wi-Fi is up. Every read tolerates a missing station
    or a driver that does not support a query, because an alert must still go
    out with whatever could be gathered.
    """

    def __init__(self):
        self.station = None
        self.hostname = None
        self.uptime = None

    def address(self):
        try:
            return self.station.ifconfig()[0]
        except (OSError, AttributeError):
            return "?"

    def rssi(self):
        try:
            return "{} dBm".format(self.station.status("rssi"))
        except (OSError, ValueError, AttributeError, TypeError):
            return "n/a"

    def uptime_text(self):
        if self.uptime is None:
            return "?"
        return format_duration(self.uptime.seconds)


board = Board()


def format_latency(latency):
    return "--" if latency == FAILED else "{}ms".format(latency)


def format_bytes(count):
    if count >= 1024:
        return "{:.1f}K".format(count / 1024)
    return "{}B".format(count)


def escape_html(text):
    """Minimal escaping for config-supplied strings rendered into the page."""
    text = str(text)
    for character, entity in (("&", "&amp;"), ("<", "&lt;"), (">", "&gt;"), ('"', "&quot;")):
        text = text.replace(character, entity)
    return text
