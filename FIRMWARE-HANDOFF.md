# Firmware handoff — TLS alerting on the ESP32-C3

Written 2026-09-13 at the end of a working session, so a future chat can pick
it up cold. Measurements and reasoning are in [PLAN-FIRMWARE.md](PLAN-FIRMWARE.md)
(see the two "Result, 2026-09-13" blocks). This file covers **where things stand
and what is left to do**.

## Where things stand

**Alert delivery works.** The board runs a custom MicroPython v1.29.0 with the
initial GC heap lowered from 64 KB to 56 KB, plus `.mpy` modules. Discord test
alerts return `Webhook accepted (204)` over verified TLS while the dashboard is
running, and `tls_memcheck.py` passes.

| Build | Deploy | Boot alert | Ctrl-C on running `main.py` | Harness |
|---|---|---|---|---|
| Stock 1.29.0 (64 KB) | `.py` | fails (memory; reports as a certificate error) | works | FAIL — `idf_largest` 25,600 |
| Dynamic TLS buffers (change 1) | `.py` | delivered, then **heap-corruption crash loop** | works | — |
| 48 KB heap | `.py` | delivered | **ignored** | not runnable |
| **56 KB heap** | `.py` | delivered | works | **PASS** — `idf_largest` 28,672 |
| **56 KB heap** | **`.mpy`** (deployed now) | delivered, 83,648 bytes free when running | works | **PASS** — `idf_largest` 30,720, `mp_free` 89,280 |

- **Change 1 (dynamic TLS buffers) must not be used.** ESP-IDF sizes the dynamic
  receive buffer with the send-side function, and mbedTLS bounds reads by the
  16 KB constant, so a record can overrun it. esp-idf#14444; the fix PR #14614
  was closed unmerged. Unchanged in IDF v5.5.4 and master.
- **Avoid 48 KB.** Alerts work, but a running `main.py` never takes Ctrl-C, so
  `mpremote`, `deploy_code.sh` and the harness cannot reach a live board. It
  reproduces from boot, doesn't happen on stock or 56 KB, and nothing in the
  1.29 source explains it.
- **Change 3 (smaller TLS input buffer) was never needed.** Its safe half (4 KB
  output buffer) is already upstream in `sdkconfig.base`.
- **Soak test not done.** `free_heap` has only been watched for minutes: flat on
  48 KB over 11 minutes, 82,7xx early on 56 KB with `.py`, and **83,088 on 56 KB +
  `.mpy`** (15:14 and 15:17, after resets). That last figure is the soak
  baseline. The plan wants a few hours of flat `free_heap`.

## What is on this machine

**Board:** ESP32-C3 on `/dev/cu.usbmodem1301`, dashboard at `192.168.4.116`.
Flashed with `build/ESP32_GENERIC_C3-v1.29.0-minutePing-heap56.bin`, with
`config.json`, `ca.pem`, eight `.mpy` modules and `main.py` on its filesystem.

**`build/`** (gitignored):

| Image | Status |
|---|---|
| `ESP32_GENERIC_C3-20260824-v1.29.0.bin` | stock; the rollback image |
| `ESP32_GENERIC_C3-v1.29.0-minutePing-heap56.bin` | **the one to use** |
| `ESP32_GENERIC_C3-v1.29.0-minutePing-heap64.bin` | own unmodified build, never flashed |
| `ESP32_GENERIC_C3-v1.29.0-minutePing-heap48.bin` | breaks Ctrl-C — do not deploy |
| `ESP32_GENERIC_C3-v1.29.0-minutePing-dynbuf.bin` | **crash-loops and spams the webhook every ~8 s — never flash** |

**MicroPython clone:** `~/src/micropython`, tag `v1.29.0`, submodules checked out.
- `ports/esp32/boards/MINUTEPING_C3_56/` is the board to keep. It's a copy of
  `ESP32_GENERIC_C3` whose only change is appending to `mpconfigboard.h`:
  ```c
  // minutePing bisection: initial GC heap 56 KB (C3 default 64 KB).
  #define MICROPY_GC_INITIAL_HEAP_SIZE        (56 * 1024)
  ```
  `mpconfigboard.cmake` and `board.json` are identical to upstream.
- `ports/esp32/boards/MINUTEPING_C3_64/` is an unmodified copy, used only for
  isolation.
- **Dirty:** `ports/esp32/boards/ESP32_GENERIC_C3/mpconfigboard.h` still has a
  48 KB define appended from the bisection. Revert it
  (`git -C ~/src/micropython checkout ports/esp32/boards/ESP32_GENERIC_C3`)
  before building `ESP32_GENERIC_C3` for anything.
- The build folders `build-ESP32_GENERIC_C3`, `build-MINUTEPING_C3_56` and
  `build-MINUTEPING_C3_64` are ~265 MB each.

**Docker:** `espressif/idf:v5.5.2` is **12.5 GB**, not the 2–3 GB PLAN-FIRMWARE
estimates. About 14 GB of disk is free.

**Watcher:** `scripts/phase0_watch.py 192.168.4.116 --interval 60 --time-requests`
(started 13:04) is still running and appending to `phase0.jsonl` /
`phase0_watch.log`. That is the soak data.

## Rebuilding the 56 KB image

```bash
docker run --rm -v ~/src/micropython:/project -w /project espressif/idf:v5.5.2 bash -c '
  git config --global --add safe.directory "*" &&
  make -C mpy-cross &&
  cd ports/esp32 && make submodules && make BOARD=MINUTEPING_C3_56 -j8'
# -> ports/esp32/build-MINUTEPING_C3_56/firmware.bin  (flash at 0x0)
```

The `safe.directory` line is required: the container's git refuses the
bind-mounted repo without it. After a config change, delete the build folder,
because ESP-IDF keeps values from an existing `sdkconfig` over new defaults.

## Flashing without losing config

`scripts/flash_firmware.sh` always erases, which wipes `config.json`, and
`deploy_config.sh` is human-only. To swap firmware and keep the filesystem:

```bash
.flashenv/bin/esptool.py --chip esp32c3 --port /dev/cu.usbmodem1301 --baud 460800 \
  write_flash -z 0x0 build/ESP32_GENERIC_C3-v1.29.0-minutePing-heap56.bin
```

This is safe because the factory app partition ends at `0x200000`, images are
~1.71 MiB, and the filesystem lives beyond 2 MiB (`partitions-4MiBplus.csv`).
It was used once on 2026-09-13, to flash the 56 KB image, and the config survived.

## Changes made to the repo this session (uncommitted)

- `scripts/deploy_code.sh` now does the following:
  - cross-compiles the eight modules with `.flashenv/bin/mpy-cross` (override:
    `MPY_CROSS`) into a temp folder;
  - refuses to upload if the board's `sys.implementation._mpy` doesn't match
    the `mpy-cross` version (currently v6.3);
  - uploads the `.mpy` files, then **deletes stale `.py` modules on the board**,
    because 1.29 imports `foo.py` in preference to `foo.mpy`;
  - uploads `main.py` last, still as source.

  Tested against the running 56 KB board: 78,409 bytes of source became 29,430
  of bytecode.
- `PLAN-FIRMWARE.md` has result blocks under change 1, change 2 and the `.mpy`
  section.
- `FIRMWARE-HANDOFF.md` is this file.

## Still to do (future chat)

1. **Soak — in progress.** `scripts/phase0_watch.py` (started 13:04, still
   running) has held flat at `free_heap` 83,088 and `tls verified` since
   15:11, through several `WATCHDOG`/`HARD RESET` cycles (that field is the
   *last boot's* reset cause, not a new reset each poll — uptime keeps
   climbing between them, so this is not a crash loop). That's ~15 minutes
   of clean soak as of 15:22; still short of the few hours the plan wants.
   Check `phase0.jsonl` / `phase0_watch.log` for how much further it's gone,
   and pay particular attention to heap after each real (non-boot) alert.
2. **Pin the firmware in the repo — done.** Board definition copied to
   [`firmware/MINUTEPING_C3_56/`](firmware/MINUTEPING_C3_56/) (mirrors
   `~/src/micropython/ports/esp32/boards/MINUTEPING_C3_56/` as of this
   session). `scripts/build_firmware.sh` clones MicroPython at `v1.29.0` if
   needed, copies the board dir in, deletes any stale `build-MINUTEPING_C3_56`
   so ESP-IDF can't keep an old sdkconfig, and runs the same Docker build as
   above, writing
   `build/ESP32_GENERIC_C3-v1.29.0-minutePing-heap56.bin`.
   `scripts/flash_firmware.sh` now defaults `FIRMWARE` to that image, exits
   with a pointer to `build_firmware.sh` if it's missing rather than trying to
   download it, takes `--no-erase` to reflash without wiping `config.json`,
   and has a corrected header comment.
3. **README.md — done.** § "1. Flash the firmware" now describes the custom
   build and `build_firmware.sh`/`--no-erase`, plus `mpy-cross` in the
   `.flashenv` install line; § "2. Upload the modules" describes `.mpy`
   cross-compilation; the migration table and scripts table are updated.
4. **PLAN.md — done.** Option 1 now says done and points here instead of
   describing `CONFIG_MBEDTLS_DYNAMIC_BUFFER=y` (ruled out) and an untouched
   `deploy_code.sh` (it now ships `.mpy`).
5. **Optional experiments:**
   - Stock firmware + `.mpy` only. PLAN.md says `.mpy` alone failed, but that
     was measured before today's fixes. If it passes, the custom image isn't
     needed.
   - Solve the 48 KB Ctrl-C puzzle (flash `heap64.bin`/`heap48.bin` without
     erasing and run the Ctrl-C test below).
   - Tidy `build/` and the Docker image once the firmware is pinned.

## Gotchas learned the hard way

- **Opening the serial port can reset the C3.** pyserial opens triggered a USB
  chip reset (`rst:0x15`) even with DTR and RTS held low, and not every time.
- **The watchdog ends REPL sessions.** Interrupting `main.py` stops the feeder,
  and the board resets 30 s later. `tls_memcheck.py` sets its own 120 s
  watchdog, so the board reboots ~2 minutes after the harness and sends another
  test alert.
- **Checking whether a live board takes Ctrl-C:**
  1. Open the port and wait for `main.py` to print `running.`, plus 30 s.
  2. Send `b"\r\x03"`.
  3. Look for `KeyboardInterrupt` / `>>>` in the next few seconds.

  After `esptool write_flash` the board is already booting, so don't also send
  a REPL reset.
- **Every boot sends a Discord test alert** (`"test": true` in the template).
  A crash loop becomes webhook spam.
- **On stock firmware, "certificate verification failed" means out of memory,
  not a bad CA.** `openssl s_client -CAfile ca.pem` verifies Discord fine.
- **Don't name a shell loop variable `path` in zsh.** It overwrites `$PATH`
  (`curl: command not found`).
- **zsh doesn't word-split `$var`.** `set -- $r` leaves the whole string in `$1`,
  so `[ "$1" -lt 400 ]` fails silently. Use `read -r a b c <<< "$r"` or
  `${=r}`. This made two background checks falsely report the board as down.
- **The boot log prints most of the webhook URL.** Treat console captures as
  secret.
