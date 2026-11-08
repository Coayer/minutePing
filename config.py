"""Loads and validates config.json, and builds the runtime objects from it.

Validation is strict and up front: a board with a bad config should say exactly
what is wrong over the AP fallback rather than fail obscurely an hour later.
"""

import json

import log

from monitors import PROBES, Monitor, set_alert_template
from notifiers import NOTIFIER_TYPES

CONFIG_PATH = "config.json"

# Generous, because a slow DNS lookup or TLS handshake happens on this
# same task and the ESP32 watchdog reboots the board rather than warning.
DEFAULT_WATCHDOG_MS = 30000


class ConfigError(Exception):
    """Raised with a message meant to be read by a human on the serial console."""


def require(section, key, where):
    if key not in section:
        raise ConfigError("{} is missing required key '{}'".format(where, key))
    return section[key]


def require_int(section, key, where, minimum=1):
    value = section.get(key)
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise ConfigError("{}: '{}' must be an integer >= {}".format(where, key, minimum))
    return value


def require_number(section, key, where):
    """Like require_int but allows a fractional value, for sub-second timeouts."""
    value = section.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ConfigError("{}: '{}' must be a positive number".format(where, key))
    return value


def require_choice(section, key, where, valid):
    value = section.get(key)
    if value is None:
        return None
    if value not in valid:
        raise ConfigError("{}: '{}' must be one of {}".format(
            where, key, ", ".join(valid)))
    return value


def check_ca_file(path, where):
    """Fails at boot rather than at the first alert if a CA file is missing.

    A typo here would otherwise stay invisible until something actually went
    down and the alert quietly failed to send -- the worst possible moment to
    discover it.
    """
    if path is None:
        return None

    try:
        with open(path, "r") as handle:
            contents = handle.read()
    except OSError:
        raise ConfigError(
            "{}: ca_file '{}' is not on the board. Upload it alongside "
            "config.json, or remove the key to run without certificate "
            "verification.".format(where, path))

    if "-----BEGIN CERTIFICATE-----" not in contents:
        raise ConfigError(
            "{}: ca_file '{}' contains no PEM certificate. It must be one or "
            "more certificates in PEM form, not DER.".format(where, path))

    return path


def load_file(path=CONFIG_PATH):
    try:
        with open(path, "r") as handle:
            return json.load(handle)
    except OSError:
        raise ConfigError(
            "No config.json found. minutePing is installed but not configured; "
            "see the documentation for creating one.")
    except ValueError as error:
        raise ConfigError("config.json is not valid JSON: {}".format(error))


def build_network(config):
    network = require(config, "network", "config.json")

    settings = {
        "ssid": require(network, "ssid", "network"),
        "password": require(network, "password", "network"),
        "hostname": network.get("hostname"),
        # Off by default: the ESP32 parks its radio between beacons otherwise,
        # which adds jitter to every latency figure this board exists to report.
        "power_save": network.get("power_save", False),
        "static_address": None,
    }

    static = network.get("static_address")
    if static is not None:
        settings["static_address"] = tuple(
            require(static, key, "network.static_address")
            for key in ("ip", "netmask", "gateway", "dns"))

    return settings


def build_tls(config):
    """The default CA file for every https connection, or None for no checking."""
    tls = config.get("tls")
    if tls is None:
        return None

    if not isinstance(tls, dict):
        raise ConfigError("config.json: 'tls' must be an object")

    return check_ca_file(tls.get("ca_file"), "tls")


def build_led(config):
    """Returns (pin, active_low). Defaults to no LED.

    Guessing is not safe on the ESP32 the way it was on the NodeMCU: the pin
    that carries a plain LED on one board is a WS2812 data line or a strapping
    pin on the next, so nothing is driven unless the config names it.
    """
    led = config.get("led")
    if led is None:
        return None, True

    if not isinstance(led, dict):
        raise ConfigError("config.json: 'led' must be an object, or omitted")

    pin = require_int(led, "pin", "led", minimum=0)
    if pin is None:
        raise ConfigError("led: 'pin' is required when 'led' is present")

    return pin, led.get("active_low", True)


def build_cpu(config):
    """Returns (idle_mhz, busy_mhz).

    The ESP32-C3 tops out at 160 MHz and rejects 240 with a ValueError, so the
    defaults stay where every variant agrees; a plain ESP32 can be pushed to
    240 in config.
    """
    cpu = config.get("cpu")
    if cpu is None:
        return 80, 160

    if not isinstance(cpu, dict):
        raise ConfigError("config.json: 'cpu' must be an object")

    valid = (20, 40, 80, 160, 240)
    for key in ("idle_mhz", "busy_mhz"):
        value = cpu.get(key)
        if value is None:
            continue
        if value not in valid:
            raise ConfigError("cpu: '{}' must be one of {}".format(
                key, ", ".join(str(option) for option in valid)))
        # MicroPython pins the clock (esp_pm_configure with min == max), so
        # there is no scaling back up for the radio. Below 80 MHz Wi-Fi simply
        # does not come up, which would look like a wiring fault.
        if value < 80:
            raise ConfigError(
                "cpu: '{}' is {} MHz, but Wi-Fi needs at least 80 MHz".format(
                    key, value))

    return cpu.get("idle_mhz", 80), cpu.get("busy_mhz", 160)


def build_watchdog(config):
    """Returns the watchdog timeout in ms, or None when disabled.

    Unlike the ESP8266, the ESP32 requires an explicit timeout, and it panics
    and reboots rather than just resetting if it is not fed in time.
    """
    watchdog = config.get("watchdog", True)

    if watchdog is False:
        return None
    if watchdog is True:
        return DEFAULT_WATCHDOG_MS
    if not isinstance(watchdog, dict):
        raise ConfigError("config.json: 'watchdog' must be true, false, or an object")

    timeout = require_int(watchdog, "timeout_ms", "watchdog", minimum=1000)
    return timeout if timeout is not None else DEFAULT_WATCHDOG_MS


def build_log_level(config):
    """Returns a log.* level from the optional top-level "log_level" key."""
    name = config.get("log_level", "info")
    level = log.parse_level(name) if isinstance(name, str) else None
    if level is None:
        raise ConfigError(
            "config.json: 'log_level' must be one of debug, info, warning, error")
    return level


def build_notifiers(config, default_ca_file):
    notifiers = []
    names = set()

    for index, entry in enumerate(config.get("notifiers", [])):
        where = "notifiers[{}]".format(index)
        name = entry.get("name", "notifier-{}".format(index))
        entry["name"] = name

        if name in names:
            raise ConfigError("{}: duplicate notifier name '{}'".format(where, name))
        names.add(name)

        kind = require(entry, "type", where)
        if kind not in NOTIFIER_TYPES:
            raise ConfigError("{} '{}' has unknown type '{}'. Valid types: {}".format(
                where, name, kind, ", ".join(NOTIFIER_TYPES)))

        if kind == "webhook":
            url = require(entry, "url", "notifier '{}'".format(name))
            if not (url.startswith("http://") or url.startswith("https://")):
                raise ConfigError(
                    "notifier '{}': url must start with http:// or https://".format(name))

            if url.startswith("https://"):
                # An explicit "ca_file": null opts this one notifier out of a
                # verification default set globally.
                entry.setdefault("ca_file", default_ca_file)
                check_ca_file(entry["ca_file"], "notifier '{}'".format(name))

        if kind == "email":
            require_choice(entry, "security", "notifier '{}'".format(name),
                           ("tls", "starttls", "none"))
            if entry.get("security") != "none":
                entry.setdefault("ca_file", default_ca_file)
                check_ca_file(entry["ca_file"], "notifier '{}'".format(name))

        try:
            notifiers.append(NOTIFIER_TYPES[kind](entry))
        except KeyError as error:
            raise ConfigError("notifier '{}' is missing required key {}".format(
                name, error.args[0]))

    return notifiers


def build_monitors(config, notifiers, default_ca_file):
    by_name = {notifier.name: notifier for notifier in notifiers}
    monitors = []
    names = set()

    services = config.get("services", [])
    if not services:
        raise ConfigError("config.json defines no services to monitor")

    for index, entry in enumerate(services):
        where = "services[{}]".format(index)
        name = require(entry, "name", where)

        if name in names:
            raise ConfigError("{}: duplicate service name '{}'".format(where, name))
        names.add(name)

        kind = require(entry, "type", "service '{}'".format(name))
        if kind not in PROBES:
            raise ConfigError("service '{}' has unknown type '{}'. Valid types: {}".format(
                name, kind, ", ".join(PROBES)))

        endpoint = require(entry, "endpoint", "service '{}'".format(name))
        if kind == "http" and not (endpoint.startswith("http://")
                                   or endpoint.startswith("https://")):
            raise ConfigError(
                "service '{}': an http service needs a full URL, "
                "for example http://{}".format(name, endpoint))

        if kind == "http" and endpoint.startswith("https://"):
            entry.setdefault("ca_file", default_ca_file)
            check_ca_file(entry["ca_file"], "service '{}'".format(name))

        for key in ("update_frequency", "max_consecutive_failures",
                    "history_length", "icmp_attempts"):
            require_int(entry, key, "service '{}'".format(name))

        require_number(entry, "timeout", "service '{}'".format(name))

        selected = entry.get("notifiers")
        if selected is None:
            chosen = notifiers
        else:
            chosen = []
            for notifier_name in selected:
                if notifier_name not in by_name:
                    raise ConfigError(
                        "service '{}' references unknown notifier '{}'".format(
                            name, notifier_name))
                chosen.append(by_name[notifier_name])

        monitors.append(Monitor(entry, chosen))
        log.info("config", "Configured {} service '{}' -> {}".format(kind, name, endpoint))

    return monitors


def build_webrepl(config):
    if "webrepl" not in config:
        return None

    password = require(config["webrepl"], "password", "webrepl")
    if not 4 <= len(password) <= 9:
        raise ConfigError("webrepl.password must be between 4 and 9 characters")

    return password


def build_telnet(config):
    """`telnet_server` is false/absent (off), true (defaults), or an object."""
    value = config.get("telnet_server", False)
    if value is False or value is None:
        return None
    if value is True:
        value = {}
    if not isinstance(value, dict):
        raise ConfigError("telnet_server must be true, false or an object")

    port = require_int(value, "port", "telnet_server") or 2323
    if port > 65535:
        raise ConfigError("telnet_server: 'port' must be at most 65535")
    refresh = require_int(value, "refresh_seconds", "telnet_server") or 5
    return {"port": port, "refresh_seconds": refresh}


def load(path=CONFIG_PATH):
    """Returns a settings dict, or raises ConfigError with a readable message."""
    config = load_file(path)

    if not isinstance(config, dict):
        raise ConfigError("config.json must contain a JSON object")

    network = build_network(config)
    set_alert_template(config.get("alert_template"))

    # Cheap structural checks first, so a bad key is reported before the
    # console fills up with "Configured ..." lines for the services.
    default_ca_file = build_tls(config)
    led_pin, led_active_low = build_led(config)
    idle_mhz, busy_mhz = build_cpu(config)
    watchdog_ms = build_watchdog(config)
    log_level = build_log_level(config)

    notifiers = build_notifiers(config, default_ca_file)
    monitors = build_monitors(config, notifiers, default_ca_file)

    return {
        "ssid": network["ssid"],
        "password": network["password"],
        "static_address": network["static_address"],
        "hostname": network["hostname"],
        "power_save": network["power_save"],
        "webrepl_password": build_webrepl(config),
        "web_server": config.get("web_server", True),
        "telnet": build_telnet(config),
        "watchdog_ms": watchdog_ms,
        "log_level": log_level,
        "ca_file": default_ca_file,
        "led_pin": led_pin,
        "led_active_low": led_active_low,
        "idle_mhz": idle_mhz,
        "busy_mhz": busy_mhz,
        "notifiers": notifiers,
        "monitors": monitors,
    }
