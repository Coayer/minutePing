#!/usr/bin/env python3
"""Polls a running board's /api and records what Phase 0 needs to decide.

    python3 scripts/phase0_watch.py 192.168.1.42
    python3 scripts/phase0_watch.py minuteping --once

The gating measurement is the gateway monitor's uptime percentage over a few
hours at icmp_attempts: 1. ~100% means there is no loss to show and Phase 2 of
PLAN.md is not worth building; consistently below it means there is, and the
sampling work pays for itself.

Every poll is appended to a JSONL log so the run survives this terminal, and
the summary printed on exit (or Ctrl-C) is computed from the board's own
totals rather than from the samples this script happened to catch.

Polling costs the board a request at the busy clock, so the default interval
is the monitor's own, not something tighter. Timing that request is also the
Phase 3 measurement: --time-requests reports the round trip separately.
"""
import argparse
import json
import statistics
import sys
import time
import urllib.error
import urllib.request

DEFAULT_INTERVAL = 60


def poll(host, timeout):
    """Returns (payload, elapsed_ms). Raises on anything that went wrong."""
    started = time.monotonic()
    with urllib.request.urlopen(
            "http://{}/api".format(host), timeout=timeout) as response:
        payload = json.load(response)
    return payload, (time.monotonic() - started) * 1000


def service(payload, name):
    for entry in payload.get("services", []):
        if entry["name"] == name:
            return entry
    return None


def describe(payload, elapsed_ms, show_timing):
    system = payload["system"]
    parts = ["up {:>6}s".format(system["uptime_seconds"]),
             "heap {:>6}".format(system["free_heap"]),
             "reset {}".format(system["reset_cause"]),
             "tls {}".format(system["tls"])]

    if show_timing:
        parts.append("api {:.0f}ms @{}MHz".format(elapsed_ms, system["cpu_mhz"]))

    for entry in payload.get("services", []):
        parts.append("{} {:.1f}% ({}/{} failed{})".format(
            entry["name"], entry["uptime_percent"], entry["total_failures"],
            entry["total_checks"],
            "" if entry["latency_ms"] is None
            else ", {}ms".format(entry["latency_ms"])))

    return "  ".join(parts)


def summarise(last, timings, gateway_name):
    if last is None:
        print("\nNo successful poll, so nothing to summarise.")
        return

    system = last["system"]
    print("\n" + "=" * 68)
    print("Phase 0 summary after {}s of board uptime".format(
        system["uptime_seconds"]))
    print("=" * 68)

    # LAST RESET is the watchdog check: POWER ON means the board was never
    # rebooted out from under itself.
    print("  reset cause      {}".format(system["reset_cause"]))
    print("  tls              {} ({} verify failures{})".format(
        system["tls"], system["tls_verify_failures"],
        "" if not system["tls_last_error"]
        else ", last: {}".format(system["tls_last_error"])))
    print("  ntp              {}".format(system["last_ntp_sync"] or "never"))
    print("  free heap        {}".format(system["free_heap"]))

    if timings:
        print("  api round trip   median {:.0f}ms, max {:.0f}ms, n={}".format(
            statistics.median(timings), max(timings), len(timings)))

    print()
    for entry in last.get("services", []):
        print("  {:<14} {:>6.1f}% uptime  {}/{} checks failed".format(
            entry["name"], entry["uptime_percent"],
            entry["total_failures"], entry["total_checks"]))

    gateway = service(last, gateway_name)
    print()
    if gateway is None:
        print("No '{}' monitor, so the Phase 2 gate is untested.".format(
            gateway_name))
    elif gateway["total_checks"] < 30:
        print("Only {} checks so far. Leave it running for a few hours before "
              "reading anything into the number.".format(
                  gateway["total_checks"]))
    elif gateway["total_failures"] == 0:
        print("Clean link: {}/{} echoes came back. PLAN.md says skip Phase 2 -- "
              "a loss column here would always read zero.".format(
                  gateway["total_checks"], gateway["total_checks"]))
    else:
        print("{} of {} echoes to the gateway went missing ({:.1f}% uptime). "
              "That is loss on a LAN target, which is what Phase 2 exists to "
              "measure and attribute.".format(
                  gateway["total_failures"], gateway["total_checks"],
                  gateway["uptime_percent"]))


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("host", help="the board's IP address or hostname")
    parser.add_argument("--interval", type=float, default=DEFAULT_INTERVAL,
                        help="seconds between polls (default {})".format(
                            DEFAULT_INTERVAL))
    parser.add_argument("--timeout", type=float, default=10)
    parser.add_argument("--log", default="phase0.jsonl",
                        help="JSONL log to append to (default phase0.jsonl)")
    parser.add_argument("--once", action="store_true",
                        help="poll once and print the summary")
    parser.add_argument("--time-requests", action="store_true",
                        help="report /api round trip, the Phase 3 measurement")
    arguments = parser.parse_args()

    last = None
    timings = []

    try:
        while True:
            try:
                payload, elapsed_ms = poll(arguments.host, arguments.timeout)
            except (urllib.error.URLError, OSError, ValueError) as error:
                # The board being unreachable is itself worth a line: it is
                # either a reboot or the thing the watchdog check is looking for.
                print("{}  unreachable: {}".format(
                    time.strftime("%H:%M:%S"), error))
            else:
                last = payload
                timings.append(elapsed_ms)
                print("{}  {}".format(
                    time.strftime("%H:%M:%S"),
                    describe(payload, elapsed_ms, arguments.time_requests)))

                with open(arguments.log, "a") as handle:
                    handle.write(json.dumps(
                        {"at": time.time(), "api_ms": elapsed_ms,
                         "payload": payload}) + "\n")

            sys.stdout.flush()

            if arguments.once:
                break

            time.sleep(arguments.interval)
    except KeyboardInterrupt:
        pass

    summarise(last, timings, "gateway")


if __name__ == "__main__":
    main()
