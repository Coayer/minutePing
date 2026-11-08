"""Minimal CPython stand-in for MicroPython's machine module."""

PWRON_RESET = 0
HARD_RESET = 1
WDT_RESET = 2
DEEPSLEEP_RESET = 3
SOFT_RESET = 4
BROWNOUT_RESET = 5

_frequency = 80000000


class Pin:
    OUT = 1
    IN = 0

    def __init__(self, identifier, mode=None, value=None):
        self.identifier = identifier
        self.value = value

    def __call__(self, value=None):
        if value is None:
            return self.value
        self.value = value
        return None


class RTC:
    def __init__(self):
        pass

    def datetime(self, value=None):
        import time as _time

        if value is not None:
            return None
        now = _time.gmtime()
        return (now.tm_year, now.tm_mon, now.tm_mday, now.tm_wday + 1,
                now.tm_hour, now.tm_min, now.tm_sec, 0)


class WDT:
    """Mirrors the ESP32 port, which requires a positive timeout in ms.

    The ESP8266 chose its own and ignored the argument entirely, so rejecting
    a bad value here is what keeps that difference from going unnoticed.
    """

    def __init__(self, id=0, timeout=5000):
        if id != 0:
            raise ValueError("only WDT(0) exists")
        if timeout is None or timeout <= 0:
            raise ValueError("WDT timeout too short")
        self.timeout = timeout

    def feed(self):
        pass


VALID_FREQUENCIES = (20, 40, 80, 160)  # an ESP32-C3; 240 is a plain ESP32 only


def freq(value=None):
    global _frequency
    if value is None:
        return _frequency
    if value // 1000000 not in VALID_FREQUENCIES:
        raise ValueError("frequency must be 20MHz, 40MHz, 80Mhz or 160MHz")
    _frequency = value
    return None


def reset_cause():
    return PWRON_RESET


def reset():
    raise SystemExit("machine.reset()")
