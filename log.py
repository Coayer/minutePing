"""Timestamped, levelled logging for the serial console.

    import log
    log.info("web", "listening on %s" % address)
    log.warning("httpc", "TLS verification is OFF")

Each line reads `2026-11-08 12:00:00 INFO  web: listening on ...`. Until NTP has
set the clock the RTC still reports the epoch, so lines are stamped with time
since boot instead (`+00:00:12`) rather than a misleading 2000-01-01.

Deliberately dependency-free (no utils import) so every module can use it
without risking an import cycle, and it never allocates more than the one line
it prints.
"""

import sys
import time
from machine import RTC

DEBUG = 10
INFO = 20
WARNING = 30
ERROR = 40

_NAMES = {DEBUG: "DEBUG", INFO: "INFO ", WARNING: "WARN ", ERROR: "ERROR"}
_BY_NAME = {"debug": DEBUG, "info": INFO, "warning": WARNING, "warn": WARNING,
            "error": ERROR}

# Any RTC year at or after this means the clock has been set (the ESP32 boots
# at year 2000), so a stale reading is never mistaken for real time.
_FIRST_VALID_YEAR = 2024

level = INFO
_rtc = RTC()


def parse_level(name):
    """Maps 'debug'/'info'/'warning'/'error' to a level, or None if unknown."""
    return _BY_NAME.get(str(name).lower())


def set_level(value):
    global level
    level = value


def timestamp():
    year, month, day, _, hour, minute, second, _ = _rtc.datetime()
    if year >= _FIRST_VALID_YEAR:
        return "{:04d}-{:02d}-{:02d} {:02d}:{:02d}:{:02d}".format(
            year, month, day, hour, minute, second)

    seconds = time.ticks_ms() // 1000
    return "+{:02d}:{:02d}:{:02d}".format(
        seconds // 3600, seconds // 60 % 60, seconds % 60)


def log(severity, tag, message):
    if severity >= level:
        print("{} {} {}: {}".format(timestamp(), _NAMES[severity], tag, message))


def debug(tag, message):
    log(DEBUG, tag, message)


def info(tag, message):
    log(INFO, tag, message)


def warning(tag, message):
    log(WARNING, tag, message)


def error(tag, message):
    log(ERROR, tag, message)


def exception(tag, message, exc):
    """Logs at ERROR, then prints the traceback (unstamped continuation lines)."""
    log(ERROR, tag, message)
    sys.print_exception(exc)
