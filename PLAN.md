# minutePing: remaining work after the ESP32 migration

Written 2026-09-13, after porting from the ESP8266 to an ESP32-C3.
Revised the same day, after running Phase 0 on real hardware.

The migration itself is done and tested as far as it can be without hardware:
async TLS, SMTP over TLS/STARTTLS, `errno`-sourced `EINPROGRESS`, an explicit
watchdog timeout, stock-firmware deployment, and a rewritten ICMP probe.

Phase 0 has now been run on an ESP32-C3 (rev v0.4, 4 MB flash, no PSRAM)
running stock MicroPython 1.29.0. Phases 1 and 3 are closed. Phase 2 is still
waiting on its measurement. One thing Phase 0 did not anticipate came out of
it, and it is the only thing left that needs a decision rather than work:
**TLS alerting does not fit on this board with stock firmware.**

---

## Phase 0 — Validate on hardware — DONE

| Check | Result |
|---|---|
| ICMP raw socket on RISC-V lwIP | **Works.** `SOCK_RAW` constructs and round-trips; gateway reports 9–117 ms, typically 11–13 ms. No TCP fallback needed. |
| Watchdog | **Works.** `WDT(timeout=30000)`; the board survives indefinitely when fed, and resets ~30 s after the feeder stops. `LAST RESET` reports `POWER ON` / `HARD RESET` / `WATCHDOG` correctly. |
| TLS verification | **Fails**, but not for the predicted reason. NTP *is* synced before the first alert, so the clock hypothesis is ruled out. See "The TLS blocker" below. |
| STARTTLS | Not exercised — no email notifier configured, so `Stream.s` is still untouched. |
| LED pin | **Still unverified.** Needs someone to look at the board; `led: {pin: 8}` is configured. |

A practical note for anyone doing hardware work here: the watchdog makes
`mpremote` sessions self-destruct. Interrupting `main.py` stops the feeder, so
the board resets ~30 s later and takes the REPL session with it. Re-acquire and
feed it from the test script (`WDT(timeout=60000)` then a `feed()` task) or keep
experiments under ~25 s.

### Two bugs this found

Both were invisible without hardware, and the second is the more dangerous.

**`load_verify_locations` was passing the certificate as a filename.** The first
positional argument is `cafile`, on MicroPython *and* CPython — the comment
claiming MicroPython took `cadata` positionally was simply wrong. The whole PEM
was being handed to `open()`, which failed `ENOENT` on a file that was present
and readable. Now passed as `cafile=`, which also keeps 8 KB of PEM off the heap.

**The dashboard reported `tls verified` with zero failures while every handshake
was being rejected.** MicroPython 1.29 raises `ValueError` carrying mbedTLS's
message text, not `OSError(-0x2700)`, so the numeric match never fired:
`verify_failures` never incremented and the operator hint never printed. This
mattered more than an ordinary bug, because Phase 0's own pass criterion for TLS
was "the dashboard shows TLS verified" — it would have returned a false pass and
hidden the blocker below. `summary()` now reports `VERIFY FAILING` when
verification is on, has failed, and has never succeeded.

### The measurement that gates Phase 2

Running now via `scripts/phase0_watch.py`, logging to `phase0.jsonl`. Early
signal is a **clean link — 100% over the first checks at a steady 11 ms**, which
points at skipping Phase 2. Leave it for a few hours before calling it.

---

## Phase 1 — Split send failures from missing replies — DONE

`probe_icmp` no longer treats a failed send and a missing reply as the same
event. `send_echo_request()` retries a send that never left the board
(`ENOBUFS`, `EAGAIN`, or a `None` return from a non-blocking socket) up to
`ICMP_SEND_RETRIES` times, bounded by the probe's own timeout. A missing reply
is never retried — that is the measurement.

It writes to the socket directly rather than through a `StreamWriter`, because a
failed `drain()` leaves the request in the writer's buffer at an unknown offset
and the retry would concatenate two requests into one malformed datagram. A raw
socket takes the whole packet or none of it.

---

## Phase 2 — Loss measurement (still conditional)

Unchanged from the original plan, and still gated on the gateway data. If the
link reads ~100% over a few hours, skip it: a correct design for a problem you
do not have is still waste.

---

## Phase 3 — Reconsider the per-request CPU clock — RESOLVED, NO CHANGE

Measured, as the plan demanded. The feared `vTaskDelay` stall does not happen —
`esp_pm_configure` applies the change synchronously on this chip, so the loop
never iterates.

| Switch | Median |
|---|---|
| 80 → 160 MHz | 39 µs |
| 160 → 80 MHz | 34 µs |
| 160 → 160 MHz (no-op) | 52 µs |

About 73 µs per request, against an `/api` request measured at 135–161 ms. That
is 0.05%, where the plan had budgeted for a possible 20 ms — a factor of 270 out.
**Keep the switching. Do not set `cpu: {idle_mhz: 160, busy_mhz: 160}`.**

---

## The TLS blocker (new — needs a decision, not work)

The board cannot complete a TLS handshake while the dashboard is resident, so
the Discord notifier never delivers. Console alerting is unaffected.

**It is not the crypto.** The C3 has hardware acceleration and completed a fully
verified handshake to Discord in 39 ms when memory allowed.

**It is the record buffers.** A TLS record may carry up to 16,384 bytes of
plaintext and the *sender* chooses the size, so a receiver must be able to
accept a maximum-size record. mbedTLS allocates ~16.7 KB in and (historically)
the same out at `mbedtls_ssl_setup`, before any data flows — ~33 KB of
*contiguous* memory per connection, whether you are posting a webhook or
streaming a file. RFC 6066 / RFC 8449 allow negotiating smaller records, but the
large public endpoints do not honour them, which is why ESP-IDF's own workaround
is asymmetric: shrink the output buffer, leave the input at 16 KB.

**What makes it fail here specifically** is that MicroPython's GC heap and
ESP-IDF's heap compete for the same SRAM, and the GC heap grows by claiming
contiguous regions. The decisive measurement: allocating an 8 KB `bytearray`
made `gc.mem_free()` *rise* to 109,664 while `idf_largest` collapsed from 65,536
to 28,672, and TLS failed from that point on. More reported free memory is the
symptom of having lost the region, not headroom. Espressif's own C examples
work because they allocate those buffers early, from one unfragmented pool,
with no Python runtime competing.

This is a known, open upstream problem, not something specific to this code:
micropython#6455 (ENOMEM with `mem_free()` > 80 KB), micropython#5303 (the two
16,717-byte buffers; first succeeds, second fails), micropython#16650 (the same
thing on ESP-IDF 5.4, fixed only by lowering `MICROPY_GC_INITIAL_HEAP_SIZE` from
56 KB to 52 KB). **No runtime workaround is documented in any of them.** Every
remedy is a firmware rebuild.

### Options, in the order I would consider them

1. **Rebuild with tuned sdkconfig — done.** Full writeup, build procedure and
   measurements in [PLAN-FIRMWARE.md](PLAN-FIRMWARE.md); current status and
   what's left in [FIRMWARE-HANDOFF.md](FIRMWARE-HANDOFF.md). Landed on: a
   custom build with `MICROPY_GC_INITIAL_HEAP_SIZE` lowered from 64 KB to
   56 KB (board `MINUTEPING_C3_56`, pinned under `firmware/`, built with
   `scripts/build_firmware.sh`), plus `deploy_code.sh` now shipping the
   modules as compiled `.mpy` bytecode instead of source — still a
   two-second upload, no firmware rebuild needed for a code change.
   `CONFIG_MBEDTLS_DYNAMIC_BUFFER=y` (the dynamic TLS buffer route this entry
   originally proposed) turned out to be unusable: it heap-corruption
   crash-loops due to esp-idf#14444, an unmerged upstream bug. Discord test
   alerts now deliver over verified TLS with the dashboard running.
2. **Adopt `.mpy` regardless.** `mpy-cross` on the eight modules takes 77,308
   bytes of source to 29,180 bytes of bytecode and took `idf_largest` in the
   full application from 29,696 to 65,536 — the single biggest lever found, and
   free. It was *not* sufficient on its own, but it is strictly better and
   composes with option 1.
3. **Run without the dashboard.** TLS succeeds repeatably once `web` is not
   resident. Cheap and certain, but it costs the dashboard that Phase 2's `LOSS`
   column was going to be displayed on.
4. **Leave alerting on the console notifier** and revisit later.

### Also worth knowing

The CA bundle only needs Discord's **GTS Root R4**. `fetch_ca.py --common`
produced six roots (8,721 bytes) which cost ~13 KB of exactly the wrong memory
once parsed; the single-root bundle is 872 bytes. Verified on-device both ways:
`discord.com` returns 200, and `expired.badssl.com` is correctly rejected.

Tailscale on this board was considered and ruled out. MicroLink needs ~116 KB
static SRAM, ~140 KB without PSRAM; the C3 has no PSRAM and the full application
leaves ~36 KB of IDF heap. It is also a C/ESP-IDF application, so running it
alongside would mean building it into the firmware as a component with Python
bindings. A subnet router elsewhere on the LAN reaches the dashboard for free.

---

## Explicitly not doing

- **Vendoring `uping.py`.** Its *model* is right and worth taking; its code is
  not. It busy-waits (`utime.sleep_ms(1)` in a loop bounded by the timeout),
  which would block the event loop for up to five seconds per check and undo
  the main reason for moving to the ESP32. It also returns only
  `(n_trans, n_recv)` with no latency, so the charts would go empty, and it
  hardcodes the IP header offset at 20 — the bug this port already fixed.
- **Loss measurement before Phase 0.** Building the column before knowing there
  is loss to show.
- **The redundant post-AUTH `EHLO` in `umail.to()`.** Harmless, and some
  servers are quirky enough that removing it is not worth the risk.

## Background

Two principles behind the ICMP decisions above, recorded so they do not have to
be rediscovered:

**Retries belong in the alerting layer, never the measurement layer.**
Measurement records what happened; alerting decides what is worth waking
someone for. Conflated, the information is destroyed before it is stored and
cannot be recovered. Separated, alerting can be retuned at any time without
corrupting history already collected. minutePing already has the alerting-layer
version in `max_consecutive_failures`, which is visible on the dashboard as
`warn`. The old ICMP retry loop was a second, hidden copy of it at the one
layer where it did damage: at five attempts, a link losing 80% of its packets
still reported 96.9% uptime.

Phase 1 adds the one exception that proves the rule: a send that never reached
the network is not a measurement of anything, so retrying it destroys nothing.

**uping is not a counterexample**, despite sending several packets. Its send is
gated on a counter rather than on success (`if t==interval and c<=count`), so
it transmits the full count regardless of what has already come back, and
returns both numbers. That is sampling. The old loop stopped at the first
success and reported success. Same shape, opposite meaning.
