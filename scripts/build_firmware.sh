#!/usr/bin/env bash
# Builds the minutePing ESP32-C3 firmware: stock MicroPython v1.29.0 with the
# initial GC heap lowered from 64 KB to 56 KB, so mbedTLS's 16 KB TLS input
# buffer has contiguous SRAM to allocate from without heap corruption. See
# FIRMWARE-HANDOFF.md and PLAN-FIRMWARE.md for why, and what was ruled out.
#
# Requires Docker. Clones MicroPython at the pinned tag (if not already
# present -- set MICROPYTHON_DIR to reuse an existing clone) and builds
# inside espressif/idf:v5.5.2, which is ~12.5 GB.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BOARD="MINUTEPING_C3_56"
MP_TAG="v1.29.0"
MICROPYTHON_DIR="${MICROPYTHON_DIR:-$HOME/src/micropython}"
IDF_IMAGE="espressif/idf:v5.5.2"

if [ ! -d "$MICROPYTHON_DIR" ]; then
  echo "Cloning MicroPython $MP_TAG into $MICROPYTHON_DIR..."
  git clone --branch "$MP_TAG" --depth 1 --recurse-submodules \
    https://github.com/micropython/micropython "$MICROPYTHON_DIR"
fi

CHECKED_OUT="$(git -C "$MICROPYTHON_DIR" describe --tags 2>/dev/null || echo unknown)"
if [ "$CHECKED_OUT" != "$MP_TAG" ]; then
  echo "Warning: $MICROPYTHON_DIR is at '$CHECKED_OUT', not $MP_TAG -- the build may not match FIRMWARE-HANDOFF.md's numbers." >&2
fi

BOARD_DIR="$MICROPYTHON_DIR/ports/esp32/boards/$BOARD"
mkdir -p "$BOARD_DIR"
cp "$ROOT/firmware/$BOARD"/* "$BOARD_DIR/"

# ESP-IDF keeps values from an existing sdkconfig over new board defaults, so
# a stale build directory can silently ignore this board's heap size.
rm -rf "$MICROPYTHON_DIR/ports/esp32/build-$BOARD"

echo "Building $BOARD in $IDF_IMAGE (this can take a while on first run)..."
docker run --rm -v "$MICROPYTHON_DIR:/project" -w /project "$IDF_IMAGE" bash -c "
  git config --global --add safe.directory '*' &&
  make -C mpy-cross &&
  cd ports/esp32 && make submodules && make BOARD=$BOARD -j8
"

mkdir -p "$ROOT/build"
OUT="$ROOT/build/ESP32_GENERIC_C3-v1.29.0-minutePing-heap56.bin"
cp "$MICROPYTHON_DIR/ports/esp32/build-$BOARD/firmware.bin" "$OUT"

echo
echo "Done: $OUT"
echo "Flash with: scripts/flash_firmware.sh"
