#!/usr/bin/env bash
# Flashes the minutePing ESP32-C3 firmware.
#
# Stock MicroPython runs out of contiguous SRAM for mbedTLS's TLS buffers
# once the dashboard and TLS alerting are both resident, so this project
# flashes a custom build with the initial GC heap lowered from 64 KB to
# 56 KB (see FIRMWARE-HANDOFF.md and PLAN-FIRMWARE.md). Build it first with
# scripts/build_firmware.sh; deploy_code.sh then pushes the project's modules
# separately, as compiled .mpy bytecode -- still a two-second upload, no
# firmware rebuild needed for a code change.
#
# Requires esptool. Set MINUTEPING_PORT if the board is not on the default.
# Pass --no-erase to reflash without wiping config.json/ca.pem -- safe only
# because the filesystem lives past 0x200000 and this firmware is ~1.71 MiB
# (see FIRMWARE-HANDOFF.md, "Flashing without losing config").
set -euo pipefail

NO_ERASE=0
if [ "${1:-}" = "--no-erase" ]; then
  NO_ERASE=1
  shift
fi

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD="$ROOT/build"

# Override to flash stock firmware instead (e.g. for rollback) -- newer
# stock builds are listed at https://micropython.org/download/ESP32_GENERIC_C3/
FIRMWARE="${MINUTEPING_FIRMWARE:-ESP32_GENERIC_C3-v1.29.0-minutePing-heap56.bin}"
PORT="${MINUTEPING_PORT:-/dev/cu.usbmodem101}"

# Unlike the original ESP32, the C3 has no second-stage bootloader offset:
# its image starts at 0x0. Flashing a C3 image at 0x1000 produces a board
# that looks bricked.
OFFSET="${MINUTEPING_FLASH_OFFSET:-0x0}"

if [ -n "${ESPTOOL:-}" ]; then
  : # explicit override
elif [ -x "$ROOT/.flashenv/bin/esptool.py" ]; then
  ESPTOOL="$ROOT/.flashenv/bin/esptool.py"
else
  ESPTOOL="esptool.py"
fi

mkdir -p "$BUILD"
if [ ! -f "$BUILD/$FIRMWARE" ]; then
  case "$FIRMWARE" in
    *minutePing*)
      echo "Missing $BUILD/$FIRMWARE -- build it first: scripts/build_firmware.sh" >&2
      exit 1
      ;;
    *)
      echo "Downloading $FIRMWARE..."
      curl -fL "https://micropython.org/resources/firmware/$FIRMWARE" -o "$BUILD/$FIRMWARE"
      ;;
  esac
fi

if [ "$NO_ERASE" = "1" ]; then
  echo "Writing $FIRMWARE at $OFFSET (filesystem preserved)..."
else
  echo "Erasing flash on $PORT..."
  "$ESPTOOL" --chip esp32c3 --port "$PORT" erase_flash
  echo "Writing $FIRMWARE at $OFFSET..."
fi
"$ESPTOOL" --chip esp32c3 --port "$PORT" --baud 460800 \
  write_flash -z "$OFFSET" "$BUILD/$FIRMWARE"

echo
if [ "$NO_ERASE" = "1" ]; then
  echo "Done. Config preserved -- next: scripts/deploy_code.sh"
else
  echo "Done. Next: scripts/deploy_code.sh, then scripts/deploy_config.sh"
fi
