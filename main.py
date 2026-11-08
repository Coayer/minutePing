"""minutePing: server status monitoring for the ESP32.

Boot order: read the config, join the network, set the clock, then hand control
to the asyncio loop. Anything that goes wrong before the loop starts drops the
board into an access point so the problem can be read and the config replaced.

The clock is set before any alert goes out on purpose. Certificate
verification checks the certificate's validity window, and the ESP32 boots at
the start of its epoch, so an unsynchronised clock makes every verified TLS
connection fail as though the certificate had expired.
"""

import asyncio
import gc
import sys
import time
import network
from machine import WDT

import config as configuration
import log
import web
from notifiers import send_test_alerts
from utils import Uptime, board, configure_led, led_off, led_on, rtc_timestamp, set_freq

WIFI_TIMEOUT_SECONDS = 30
NTP_RESYNC_SECONDS = 6 * 3600
NTP_RETRY_SECONDS = 30
INITIAL_NTP_RETRY_SECONDS = 5
INITIAL_NTP_TIMEOUT_SECONDS = 60
CHECK_STAGGER_SECONDS = 3  # spread first checks so they don't all allocate at once

AP_SSID = "minutePing"
AP_PASSWORD = "pingpong"


def access_point_fallback(message):
    """Last resort: start an open-ish AP with WebREPL so the config can be fixed.

    Never returns. The message repeats on the serial console and WebREPL so it
    is visible however the board is reached.
    """
    print("=" * 60)
    print("minutePing cannot start:")
    print(message)
    print("Connect to Wi-Fi '{}' (password '{}') and open".format(
        AP_SSID, AP_PASSWORD))
    print("http://micropython.org/webrepl/ to upload a corrected config.json.")
    print("=" * 60)

    led_on()  # solid LED means "stuck, needs attention"

    try:
        import webrepl

        access_point = network.WLAN(network.AP_IF)
        access_point.active(True)
        access_point.config(essid=AP_SSID, authmode=network.AUTH_WPA_WPA2_PSK,
                            password=AP_PASSWORD)
        webrepl.start(password=AP_PASSWORD)
    except Exception as error:
        log.error("main", "Could not start the fallback access point: {}".format(error))

    while True:
        log.error("main", message)
        time.sleep(10)


def wifi_failure_reason(station):
    """Turns station.status() into something worth acting on, if it can.

    The ESP32 tells a rejected password apart from a network that is not there,
    which the ESP8266 largely did not. That is the difference between fixing a
    typo and moving the board, so it is worth putting in the fallback message.
    """
    reasons = {}
    for name, reason in (
            ("STAT_NO_AP_FOUND", "no access point with that SSID is in range"),
            ("STAT_WRONG_PASSWORD", "the access point rejected the password"),
            ("STAT_BEACON_TIMEOUT", "the access point stopped responding"),
            ("STAT_HANDSHAKE_TIMEOUT", "the WPA handshake timed out"),
            ("STAT_ASSOC_FAIL", "the access point refused to associate"),
            ("STAT_CONNECT_FAIL", "the connection was refused")):
        value = getattr(network, name, None)
        if value is not None:
            reasons[value] = reason

    try:
        return reasons.get(station.status())
    except (OSError, ValueError):
        return None


def connect_wifi(ssid, password, static_address, hostname, power_save):
    """Joins the network. Returns (station, reason); station is None on failure."""
    station = network.WLAN(network.STA_IF)
    station.active(True)

    if hostname:
        try:
            network.hostname(hostname)
        except (AttributeError, OSError, ValueError) as error:
            log.warning("wifi", "Could not set hostname '{}': {}".format(hostname, error))

    if not power_save:
        # Wi-Fi power save is on by default on the ESP32 and parks the radio
        # between beacons, which adds tens of milliseconds of jitter to every
        # measurement. This board exists to measure latency, so it is off
        # unless asked for.
        try:
            station.config(pm=network.WLAN.PM_NONE)
        except (AttributeError, OSError, ValueError) as error:
            log.warning("wifi", "Could not disable Wi-Fi power save: {}".format(error))

    if static_address is not None:
        log.info("wifi", "Using static address {}".format(static_address))
        station.ifconfig(static_address)

    if not station.isconnected():
        log.info("wifi", "Connecting to '{}'...".format(ssid))
        station.connect(ssid, password)

        deadline = time.ticks_add(time.ticks_ms(), WIFI_TIMEOUT_SECONDS * 1000)
        while not station.isconnected():
            if time.ticks_diff(deadline, time.ticks_ms()) <= 0:
                return None, wifi_failure_reason(station)
            time.sleep_ms(200)

    log.info("wifi", "Connected: {}".format(station.ifconfig()))
    return station, None


def sync_time():
    """One blocking NTP round trip. Returns True if the RTC was set.

    ntptime uses a blocking socket, unlike everything else here, so this
    briefly stalls the event loop. Bounded by ntptime.timeout and run rarely.
    """
    import ntptime

    ntptime.timeout = 2

    try:
        ntptime.settime()
        return True
    except (OSError, OverflowError, ValueError) as error:
        log.warning("ntp", "sync failed ({})".format(error))
        return False


async def initial_time_sync(dashboard, verification_enabled):
    """Sets the clock before the first alert can try to verify a certificate.

    Bounded: a dead NTP server should delay alerting, never prevent the board
    from monitoring. ntp_task keeps trying afterwards either way.
    """
    deadline = time.ticks_add(time.ticks_ms(), INITIAL_NTP_TIMEOUT_SECONDS * 1000)

    while True:
        if sync_time():
            dashboard.last_sync = rtc_timestamp()
            log.info("ntp", "synchronised: {} UTC".format(dashboard.last_sync))
            return True

        if time.ticks_diff(deadline, time.ticks_ms()) <= 0:
            if verification_enabled:
                log.warning("ntp", "the clock is not set and TLS verification is "
                            "on, so https alerts will fail as 'expired certificate' "
                            "until NTP succeeds.")
            else:
                log.warning("ntp", "continuing without a synchronised clock; alert "
                            "timestamps will be wrong until NTP succeeds.")
            return False

        await asyncio.sleep(INITIAL_NTP_RETRY_SECONDS)


async def ntp_task(dashboard):
    """Keeps the RTC roughly correct so alert timestamps stay meaningful."""
    while True:
        await asyncio.sleep(
            NTP_RESYNC_SECONDS if dashboard.last_sync else NTP_RETRY_SECONDS)

        if sync_time():
            dashboard.last_sync = rtc_timestamp()
            log.info("ntp", "synchronised: {} UTC".format(dashboard.last_sync))


async def supervisor(watchdog, uptime):
    """Feeds the watchdog and advances the rollover-safe uptime counter."""
    while True:
        if watchdog is not None:
            watchdog.feed()
        uptime.tick()
        await asyncio.sleep_ms(500)


def report_exception(loop, context):
    exception = context.get("exception")
    if exception is not None:
        log.exception("main", "Unhandled exception in a task:", exception)
    else:
        log.error("main", "Unhandled exception in a task: {}".format(
            context.get("message", context)))


async def main(settings, station, uptime):
    asyncio.get_event_loop().set_exception_handler(report_exception)

    monitors = settings["monitors"]
    dashboard = web.Dashboard(monitors, station, uptime,
                              webrepl_enabled=settings["webrepl_password"] is not None,
                              idle_mhz=settings["idle_mhz"],
                              busy_mhz=settings["busy_mhz"])

    if settings["web_server"]:
        log.info("web", "Starting web server on http://{}/".format(dashboard.address()))
        asyncio.create_task(asyncio.start_server(dashboard.handle, "0.0.0.0", 80, 5))

    if settings["telnet"]:
        import tui

        terminal = tui.Terminal(dashboard, settings["telnet"]["refresh_seconds"])
        log.info("tui", "Starting terminal view on telnet {} {}".format(
            dashboard.address(), settings["telnet"]["port"]))
        asyncio.create_task(asyncio.start_server(
            terminal.handle, "0.0.0.0", settings["telnet"]["port"], 2))

    for index, monitor in enumerate(monitors):
        asyncio.create_task(monitor.run(start_delay=index * CHECK_STAGGER_SECONDS))

    watchdog = None
    if settings["watchdog_ms"]:
        log.info("main", "Starting watchdog timer ({} ms)".format(settings["watchdog_ms"]))
        # The ESP32 needs an explicit timeout, where the ESP8266 chose its own,
        # and it panics and reboots rather than quietly resetting.
        watchdog = WDT(timeout=settings["watchdog_ms"])

    asyncio.create_task(supervisor(watchdog, uptime))

    # Monitoring is already running by this point, so a slow NTP server delays
    # only the alerts that need a trustworthy clock.
    await initial_time_sync(dashboard, settings["ca_file"] is not None)
    asyncio.create_task(ntp_task(dashboard))

    telnet = settings["telnet"]
    await send_test_alerts(settings["notifiers"],
                           dashboard.boot_report(settings["web_server"],
                                                 telnet["port"] if telnet else None))

    gc.collect()
    log.info("main", "minutePing {} running. {} bytes free.".format(
        web.VERSION, gc.mem_free()))
    led_off()

    while True:
        await asyncio.sleep(3600)


def boot():
    print()
    log.info("main", "minutePing {} starting...".format(web.VERSION))

    try:
        settings = configuration.load()
    except configuration.ConfigError as error:
        access_point_fallback(str(error))
        return

    log.set_level(settings["log_level"])
    configure_led(settings["led_pin"], settings["led_active_low"])
    led_on()
    set_freq(settings["idle_mhz"])

    station, reason = connect_wifi(settings["ssid"], settings["password"],
                                   settings["static_address"], settings["hostname"],
                                   settings["power_save"])
    if station is None:
        access_point_fallback(
            "Could not join Wi-Fi network '{}' within {} seconds{}. Check the "
            "SSID and password in config.json.".format(
                settings["ssid"], WIFI_TIMEOUT_SECONDS,
                ": {}".format(reason) if reason else ""))
        return

    if settings["webrepl_password"] is not None:
        log.info("main", "Starting WebREPL")
        import webrepl

        webrepl.start(password=settings["webrepl_password"])

    uptime = Uptime()
    board.station = station
    board.hostname = settings["hostname"]
    board.uptime = uptime
    gc.collect()

    try:
        asyncio.run(main(settings, station, uptime))
    finally:
        asyncio.new_event_loop()


boot()
