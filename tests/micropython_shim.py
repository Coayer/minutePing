"""Makes the minutePing modules importable under CPython.

MicroPython's `time` carries ticks_* helpers that CPython's does not, and the
board modules (machine, network, ntptime) do not exist off-device. Import this
before importing anything from the project.
"""

import asyncio
import gc
import os
import sys
import time

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STUBS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "stubs")


def install():
    for path in (STUBS, PROJECT_ROOT):
        if path not in sys.path:
            sys.path.insert(0, path)

    # MicroPython ticks helpers, added onto the real time module rather than
    # shadowing it so everything else in the stdlib keeps working.
    if not hasattr(time, "ticks_ms"):
        time.ticks_ms = lambda: int(time.monotonic() * 1000) & 0x3FFFFFFF
        time.ticks_us = lambda: int(time.monotonic() * 1000000) & 0x3FFFFFFF
        time.ticks_diff = lambda new, old: new - old
        time.ticks_add = lambda ticks, delta: ticks + delta
        time.sleep_ms = lambda milliseconds: time.sleep(milliseconds / 1000)

    # MicroPython reports heap usage; CPython's gc does not.
    if not hasattr(gc, "mem_free"):
        gc.mem_free = lambda: 21936
        gc.mem_alloc = lambda: 14016

    if not hasattr(asyncio, "sleep_ms"):
        asyncio.sleep_ms = lambda milliseconds: asyncio.sleep(milliseconds / 1000)


install()
