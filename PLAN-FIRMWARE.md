# Building a firmware that can do TLS on the ESP32-C3

Written 2026-09-13, after Phase 0 established that stock MicroPython 1.29.0
cannot complete a TLS handshake on this board while the dashboard is resident.
Background and measurements are in [PLAN.md](PLAN.md); this document is only the
build.

The goal is narrow: **make alert delivery work.** Today minutePing monitors well
and cannot tell anyone about it, which is half a product. Everything below is in
service of getting one webhook through.

---

## What we are actually fixing

A TLS record may carry up to 16,384 bytes of plaintext and the *sender* chooses
the size, so mbedTLS allocates ~16.7 KB in and (historically) the same out at
`mbedtls_ssl_setup`, before any data flows — roughly 33 KB of **contiguous**
memory per connection. MicroPython's GC heap and ESP-IDF's heap compete for the
same SRAM, and the GC heap grows by claiming contiguous regions. It wins, and
TLS then has nowhere to put its buffers.

None of this is reachable from Python. The options are all compile-time, which
is why this needs a build rather than a patch.

### The numbers to beat

Measured on the deployed configuration with `scripts/tls_memcheck.py`:

| | mp_free | idf_free | **idf_largest** | TLS |
|---|---|---|---|---|
| Deployed (dashboard + monitors) | 102,480 | 35,932 | **27,648** | FAIL |
| Same, modules as `.mpy` | 78,336 | ~102,000 | **65,536** | FAIL |
| Minimal footprint (no `web`) | 93,344 | 102,216 | **65,536** | **200 OK** |

`idf_largest` is the number that matters, but note row 2: it is necessary and
not sufficient. Two runs at 65,536 differed, and the passing one also had more
`mp_free`. Treat "≥ 65,536 **and** TLS actually succeeds" as the bar, and trust
the harness verdict over any single statistic.

The control that makes the harness trustworthy: plain HTTP returns 200 in the
same breath that both TLS attempts fail, so this is a memory ceiling and not the
network.

---

## Prerequisites

- **Docker.** Present (v29.6.1). The existing `larsks/esp-open-sdk` image is the
  **ESP8266** xtensa toolchain and cannot build for the C3, which is RISC-V —
  it is a leftover from before the migration and is not used here.
- **Image: `espressif/idf:v5.5.2`.** MicroPython v1.29.0's `ports/esp32/README.md`
  names v5.5.2 as recommended, and also supports v5.3, v5.4, v5.4.1, v5.4.2,
  v5.5.1 and v5.5.4. Version mismatch is the most common way these builds fail,
  so pin it.
- **Disk.** ~19 GB free. The image is 2–3 GB, it installs its toolchain on first
  use, and the MicroPython clone adds more — budget 5–6 GB. `docker builder
  prune` reclaims ~3.5 GB if it gets tight.
- **Flashing happens on the host, not in the container.** Docker Desktop on
  macOS cannot pass USB through. `.flashenv/bin/esptool` already works, and
  `flash_firmware.sh` honours `MINUTEPING_FIRMWARE`, so it can point at a
  locally built image with no change to the script.

---

## The configuration changes

In order. **Apply and test them one at a time** — if all three go in at once and
it works, we will not know which mattered, and two of them carry risk that is
only worth accepting if it is buying something.

### 1. Dynamic TLS buffers (try first, lowest risk)

```
CONFIG_MBEDTLS_DYNAMIC_BUFFER=y
CONFIG_MBEDTLS_DYNAMIC_FREE_CONFIG_DATA=y
CONFIG_MBEDTLS_SSL_KEEP_PEER_CERTIFICATE=n
```

Espressif's own answer to this exact problem: the record buffers are allocated
and freed around use instead of being held for the life of the connection.
Crucially it keeps full 16 KB capability, so unlike shrinking the buffers it
does not gamble on what servers send. `KEEP_PEER_CERTIFICATE` defaults on since
IDF v5.0 and holds the peer certificate in RAM after the handshake for no
benefit to minutePing, which never inspects it.

*Known rough edge:* esp-idf#6104 reported a leak with dynamic buffers under
`esp_http_client`. Old, and a different client than ours — but it is an
Espressif patch on top of upstream mbedTLS, so watch the heap over hours rather
than declaring victory on one successful alert. See "Soak test" below.

**Result, 2026-09-13: unsafe on this build — do not use.** Built and flashed
with `sdkconfig.board` appended to `SDKCONFIG_DEFAULTS` (the file does not
exist upstream). The memory side worked: the Discord test alert was
delivered and the dashboard read `tls verified`. Then the board crash-looped,
panicking about 3 s into nearly every webhook POST with load/store access
faults in the TLSF allocator, lwIP, FreeRTOS and `mbedtls_ssl_encrypt_buf`
(decoded against the ELF). One dump had ASCII ("connec"/"tion") in the heap's
free-list pointers. That is heap corruption.

Cause, read from the IDF source: `esp_mbedtls_add_rx_buffer` sizes the RX
buffer with `tx_buffer_len()`, so it ignores `SSL_IN_CONTENT_LEN`. It falls
back to `OUT_BUFFER_LEN` (4 KB, because MicroPython's `sdkconfig.base` sets
`ASYMMETRIC_CONTENT_LEN`) and reuses a cached buffer without checking its size.
`mbedtls_ssl_fetch_input` bounds reads by the constant `MBEDTLS_SSL_IN_BUFFER_LEN`
(16 KB), because the per-buffer length only exists under
`SSL_VARIABLE_BUFFER_LENGTH`, and dynamic buffers require that off. Nothing
stops the overflow. Reported as esp-idf#14444; the fix (PR #14614) was closed
unmerged. Unchanged in v5.5.4 and master, so an IDF bump does not help.

Two traps for anyone revisiting it:
`CONFIG_MBEDTLS_DYNAMIC_BUFFER` depends on `!MBEDTLS_SSL_PROTO_DTLS`, which
MicroPython enables, so without `CONFIG_MBEDTLS_SSL_PROTO_DTLS=n` Kconfig
silently drops it. And `DYNAMIC_FREE_CA_CERT` defaults to `y`, which would break
`httpc`'s cached `SSLContext` after the first handshake.

### 2. Smaller initial MicroPython heap (safe, needs bisection)

```
MICROPY_GC_INITIAL_HEAP_SIZE  (lower it)
```

The micropython#16650 fix. Starves the GC heap of its opening grab so it takes
less of the contiguous region. That issue found 52 KB worked where 54 KB did
not, so expect to bisect rather than get it right first time. Safe in kind: the
heap still grows on demand, it just starts smaller.

**Result, 2026-09-13: 56 KB works — deploy this one.** Built as separate board
variants in the MicroPython clone (`boards/MINUTEPING_C3_56`,
`boards/MINUTEPING_C3_64`). Each is a copy of `ESP32_GENERIC_C3` with only
`MICROPY_GC_INITIAL_HEAP_SIZE` added to `mpconfigboard.h`, on stock mbedTLS
(change 1 fully reverted). All tested on the deployed code and config.

| Initial heap | Boot test alert | Ctrl-C on running `main.py` | `tls_memcheck.py` |
|---|---|---|---|
| 64 KB (stock image) | fails: "certificate verification failed" | works | FAIL, `idf_largest` 25,600, `idf_free` 36,208 |
| **56 KB** | **`Webhook accepted (204)`** | **works** | **PASS**, `idf_largest` 28,672, `idf_free` 50,992 |
| 48 KB | `Webhook accepted (204)` | **ignored** | not runnable, see below |

- **The `idf_largest` ≥ 65,536 bar above was wrong.** 56 KB passes at 28,672.
  What moved was total IDF free memory (+14.8 KB). Trust the verdict, as the
  numbers section already says.
- **Avoid 48 KB.** It delivers alerts, but a running `main.py` never takes
  Ctrl-C, so `mpremote` cannot enter the raw REPL. That breaks `deploy_code.sh`
  and the harness against a live board. It reproduced from early boot after a
  hard reset. Stock, 56 KB and the 64 KB dynamic-buffer build all took Ctrl-C
  on a running `main.py`. Nothing found in the 1.29 source connects heap size
  to Ctrl-C delivery, so this is unexplained. `free_heap` on 48 KB held at
  67,472–67,568 over 11 minutes.
- **The false certificate error on stock is memory.** `ca.pem` verifies
  Discord's chain with `openssl`, and the same file verifies on 56 KB.
- **Flashing without erase keeps `config.json`, `ca.pem` and the modules**, so
  no `deploy_config.sh` re-run is needed. The factory app partition ends at
  `0x200000`, the images are ~1.71 MiB, and the filesystem starts beyond 2 MiB:
  `esptool.py --chip esp32c3 --port <port> --baud 460800 write_flash -z 0x0 <image>`.
  `flash_firmware.sh` always erases.
- Still to do: the soak test on 56 KB. My own unmodified 64 KB build was built
  but never flashed.

### 3. Smaller TLS input buffer (last resort only)

```
CONFIG_MBEDTLS_ASYMMETRIC_CONTENT_LEN=y
CONFIG_MBEDTLS_SSL_OUT_CONTENT_LEN=4096
CONFIG_MBEDTLS_SSL_IN_CONTENT_LEN=8192   # only if 1 and 2 were not enough
```

Shrinking *output* is nearly free — we control what we send. Shrinking **input**
is the one with teeth: if any server ever sends a record larger than the cap,
the connection dies and there is no way to negotiate out of it. Discord's chain
is ~4 KB so 8 KB is very likely fine *for this endpoint*, but it is a
per-endpoint bet that would quietly break a future notifier pointed at a host
with a fatter chain. Only take it if 1 and 2 leave us short, and write it down
in the README if we do.

---

## Procedure

```bash
# 1. Pull the pinned toolchain
docker pull espressif/idf:v5.5.2

# 2. Clone MicroPython at the matching release
git clone --branch v1.29.0 --depth 1 \
    https://github.com/micropython/micropython.git ~/src/micropython

# 3. Edit the board config (change 1 first, on its own)
#    ports/esp32/boards/ESP32_GENERIC_C3/sdkconfig.board

# 4. Build inside the container, sources mounted from the host
docker run --rm -it -v ~/src/micropython:/project -w /project \
    espressif/idf:v5.5.2 bash -c '
        make -C mpy-cross &&
        cd ports/esp32 &&
        make submodules &&
        make BOARD=ESP32_GENERIC_C3'
```

The image lands at
`ports/esp32/build-ESP32_GENERIC_C3/firmware.bin`.

```bash
# 5. Flash from the host (the C3 image starts at 0x0, not 0x1000)
cd ~/Documents/minutePing
MINUTEPING_PORT=/dev/cu.usbmodem1301 \
MINUTEPING_FIRMWARE=~/src/micropython/ports/esp32/build-ESP32_GENERIC_C3/firmware.bin \
  ./scripts/flash_firmware.sh

# 6. Redeploy code and config
MINUTEPING_PORT=/dev/cu.usbmodem1301 ./scripts/deploy_code.sh
MINUTEPING_PORT=/dev/cu.usbmodem1301 ./scripts/deploy_config.sh   # run this yourself
```

`flash_firmware.sh` erases the flash, so `config.json` and `ca.pem` go with it —
step 6 is not optional. `deploy_config.sh` wants the Wi-Fi password in the
environment and is meant to be run by a human, not an assistant.

---

## Verifying

Run the harness before flashing and after, and compare:

```bash
.flashenv/bin/mpremote connect /dev/cu.usbmodem1301 run scripts/tls_memcheck.py
```

It reproduces the deployed footprint, prints `idf_largest` at each stage, and
ends in one of four verdicts — PASS, PARTIAL (memory fine, CA file wrong), FAIL
(memory ceiling, proven by plain HTTP succeeding alongside), or INCONCLUSIVE
(the network is broken, ignore the TLS result).

Baseline to beat: **`idf_largest` 27,648, verdict FAIL.**

Then the real test, which is the whole point:

```bash
# Reboot and watch a genuine alert attempt go out
mpremote connect /dev/cu.usbmodem1301 exec "import machine; machine.reset()"
```

Success is the boot log showing the test alert **delivered** rather than
`Webhook rejected (status None)`, and the dashboard reading `tls verified`
rather than `VERIFY FAILING`. Those two now mean what they say — before today
the dashboard reported `verified` with zero failures while every handshake was
being rejected.

### Soak test

Because of the known dynamic-buffer leak report, do not stop at one success:

```bash
python3 scripts/phase0_watch.py <board-ip> --interval 60 --time-requests
```

Leave it for a few hours and check `free_heap` is flat rather than trending
down. This is the same script already running for the Phase 2 gate, so it costs
nothing extra.

---

## If it does not work

- **Still FAIL after change 1** — add change 2 and bisect the heap size.
  Halve the gap each time; three or four flashes should find the edge.
- **PARTIAL** — memory is fixed and the CA file is wrong. That is a good
  outcome, and a much easier problem: rebuild `ca.pem` with
  `python3 scripts/fetch_ca.py discord.com`. Do **not** use `--common`; six
  roots cost ~13 KB of exactly the wrong memory once parsed, where the single
  GTS Root R4 is 872 bytes.
- **Still FAIL after 1 and 2** — take change 3, accept the per-endpoint caveat,
  and document it.
- **Still FAIL after all three** — stop. That is the signal that the C3 is the
  wrong chip for TLS alerting, and the answer is an **ESP32-S3 with PSRAM**,
  where MicroPython's heap moves to external RAM and leaves internal SRAM to the
  IDF for exactly these buffers. That is stock firmware again, with no custom
  image to maintain — the deployment story the migration wanted. Confirm any
  board actually has PSRAM before buying, and verify `gc.mem_free()` reports
  megabytes on first boot.

## Rolling back

One command, about a minute:

```bash
MINUTEPING_PORT=/dev/cu.usbmodem1301 ./scripts/flash_firmware.sh   # stock .bin
```

The stock image is already cached in `build/`. Then redeploy code and config as
in step 6. Nothing about this is a one-way door.

---

## Worth doing regardless of the outcome

**Deploy `.mpy` instead of `.py`.** `mpy-cross` is already in `.flashenv`. It
takes 77,308 bytes of source to 29,180 bytes of bytecode and moved `idf_largest`
in the full application from 29,696 to 65,536 — the single biggest lever found,
and free. It was not sufficient alone, but it composes with everything above and
there is no reason not to take it. It would mean `deploy_code.sh` cross-compiles
before uploading, and `main.py` staying source so it still auto-runs.

The reason it helps is worth recording: it is not the size of the bytecode, it
is that compiling a 15 KB source file on-device forces the GC heap to grow, and
once grown it never gives the region back.

**Result, 2026-09-13: done, on top of the 56 KB firmware.** `deploy_code.sh`
now cross-compiles and uploads `.mpy` (78,409 → 29,430 bytes). It checks the
mpy version first and deletes stale `.py` modules on the board, because 1.29
imports `foo.py` in preference to `foo.mpy`. The harness still PASSes with
`idf_largest` 28,672 → 30,720 and `mp_free` 87,376 → 89,280. `main.py` runs
with 83,648 bytes free, up from 79,664, and alerts and Ctrl-C both still work.

Current state and remaining work: [FIRMWARE-HANDOFF.md](FIRMWARE-HANDOFF.md).
