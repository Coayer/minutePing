"""On-device memory and TLS harness. Run it ON the board, not on your Mac:

    mpremote connect /dev/cu.usbmodemXXXX run scripts/tls_memcheck.py

It reproduces the deployed footprint -- dashboard resident, monitors running --
and then reports the numbers that decide whether TLS can work, followed by four
TLS attempts. Use it to compare a firmware build against the baseline in
PLAN-FIRMWARE.md; the only number that has predicted success so far is
idf_largest, so that is the one to watch.

Everything it needs comes from config.json already on the board, so no
credentials are passed in from the host.

Two notes on why it is shaped this way:

The watchdog kills REPL sessions. Interrupting main.py stops the supervisor
feeding it, so the board resets ~30 s later and takes the session with it. This
re-acquires the WDT and feeds it from a task, which is what lets the harness run
longer than that.

It binds the dashboard on port 8080 rather than 80, so it can be run while
main.py's own server is up without colliding on the port.
"""

import asyncio
import gc

import esp32
import network
import ntptime
from machine import WDT

import config as configuration
import httpc
import web
from utils import Uptime

# Well above the run time, so a slow handshake cannot reset the board mid-test.
WATCHDOG_MS = 120000
SETTLE_SECONDS = 10
VERIFIED_HOST = "https://discord.com/"
PLAIN_HOST = "http://example.com/"


def heaps():
    """(mp_free, idf_free, idf_largest) after a collect."""
    gc.collect()
    info = esp32.idf_heap_info(esp32.HEAP_DATA)
    return (gc.mem_free(),
            sum(region[1] for region in info),
            max(region[2] for region in info))


def report(label):
    mp_free, idf_free, idf_largest = heaps()
    print("  {:<26} mp_free={:>6}  idf_free={:>7}  idf_largest={:>6}".format(
        label, mp_free, idf_free, idf_largest))
    return idf_largest


async def feed_watchdog(watchdog):
    while True:
        watchdog.feed()
        await asyncio.sleep_ms(500)


async def attempt(label, url, ca_file):
    """One request, reported with the contiguous block it had to work with."""
    _, _, idf_largest = heaps()
    status, latency = await httpc.request("GET", url, timeout=15,
                                          ca_file=ca_file)
    print("  {:<26} idf_largest={:>6}  status={!s:<5} {}".format(
        label, idf_largest, status,
        "OK" if status is not None else "FAILED"))
    return status is not None


async def run():
    watchdog = WDT(timeout=WATCHDOG_MS)
    asyncio.create_task(feed_watchdog(watchdog))

    print("\n--- baseline ---")
    report("after imports")

    settings = configuration.load()
    station = network.WLAN(network.STA_IF)
    station.active(True)
    try:
        station.config(pm=network.WLAN.PM_NONE)
    except (AttributeError, OSError, ValueError):
        pass

    if not station.isconnected():
        station.connect(settings["ssid"], settings["password"])
        while not station.isconnected():
            await asyncio.sleep_ms(200)
    report("wifi connected")

    # Verification checks the certificate's validity window, so an unset clock
    # reads as an expired certificate and would confuse the result.
    ntptime.timeout = 2
    try:
        ntptime.settime()
    except (OSError, OverflowError, ValueError) as error:
        print("  NTP failed ({}); TLS results will not be trustworthy".format(error))
    report("clock set")

    monitors = settings["monitors"]
    dashboard = web.Dashboard(monitors, station, Uptime(), webrepl_enabled=False,
                              idle_mhz=settings["idle_mhz"],
                              busy_mhz=settings["busy_mhz"])
    await asyncio.start_server(dashboard.handle, "0.0.0.0", 8080, 5)
    report("dashboard resident")

    for index, monitor in enumerate(monitors):
        asyncio.create_task(monitor.run(start_delay=index))
    await asyncio.sleep(SETTLE_SECONDS)
    deployed = report("monitors running")

    print("\n--- TLS in the deployed footprint ---")
    results = []
    results.append(await attempt("verified https", VERIFIED_HOST,
                                 settings["ca_file"]))
    # Captured here: the unverified attempt below deliberately turns
    # verification off, which would otherwise overwrite this for the verdict.
    verified_state = httpc.tls_state.summary()
    await asyncio.sleep(2)
    # Verification off still needs the record buffers, so this separates "the
    # certificate is wrong" from "the handshake cannot get memory".
    results.append(await attempt("unverified https", VERIFIED_HOST, None))
    await asyncio.sleep(2)
    # The control: if this fails too, the problem is the network, not memory.
    results.append(await attempt("plain http (control)", PLAIN_HOST, None))

    print("\n--- verdict ---")
    print("  idf_largest in deployment : {}".format(deployed))
    print("  tls state (verified run)  : {}".format(verified_state))
    if results[0]:
        print("  PASS -- verified TLS works in the deployed footprint.")
    elif results[1]:
        print("  PARTIAL -- the handshake got memory, but the chain did not "
              "verify. That is a CA file problem, not a memory problem.")
    elif results[2]:
        print("  FAIL -- plain http works and both TLS attempts did not, so "
              "this is the memory ceiling, not the network.")
    else:
        print("  INCONCLUSIVE -- even plain http failed; check the network "
              "before reading anything into the TLS results.")


asyncio.run(run())
