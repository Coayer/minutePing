#!/usr/bin/env bash
# Uploads the project's modules to the board.
#
# These used to be frozen into a custom firmware image because the ESP8266 had
# no heap to spare. On the ESP32-C3 they are ordinary files, so changing one is
# an upload and a reset rather than a rebuild.
#
# The modules go up as .mpy bytecode, cross-compiled here. What matters is not
# the smaller file but where the compiling happens: compiling a 15 KB source
# file on the board grows the GC heap, and a grown heap never hands the region
# back to ESP-IDF -- which is exactly the memory a TLS handshake needs. main.py
# stays source so it still runs at boot.
#
# Set MINUTEPING_PORT if the board is not on the default. MPREMOTE and MPY_CROSS
# can point at specific binaries if they are not on PATH.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

PORT="${MINUTEPING_PORT:-/dev/cu.usbmodem101}"
if [ -n "${MPREMOTE:-}" ]; then
  : # explicit override
elif [ -x ".flashenv/bin/mpremote" ]; then
  MPREMOTE=".flashenv/bin/mpremote"
else
  MPREMOTE="mpremote"
fi
if [ -n "${MPY_CROSS:-}" ]; then
  : # explicit override
elif [ -x ".flashenv/bin/mpy-cross" ]; then
  MPY_CROSS=".flashenv/bin/mpy-cross"
else
  MPY_CROSS="mpy-cross"
fi

MODULES=(chart config httpc log monitors notifiers tui umail utils web)

OUT="$(mktemp -d)"
trap 'rm -rf "$OUT"' EXIT

# A .mpy from a mismatched mpy-cross fails at import with "incompatible .mpy
# file", which on this board is a boot loop, so check before uploading.
# sys.implementation._mpy keeps the version in its low byte and the
# sub-version in the next two bits.
board_mpy=$("$MPREMOTE" connect "$PORT" exec \
  "import sys; v = sys.implementation._mpy; print('v{}.{}'.format(v & 0xff, v >> 8 & 3))" \
  | tr -d '\r' | tail -1)
cross_mpy=$("$MPY_CROSS" --version | sed -n 's/.*mpy \(v[0-9]*\.[0-9]*\).*/\1/p')
if [ "$board_mpy" != "$cross_mpy" ]; then
  echo "mpy-cross emits ${cross_mpy:-unknown} but the board expects ${board_mpy:-unknown};" \
       "install an mpy-cross matching the firmware." >&2
  exit 1
fi

for module in "${MODULES[@]}"; do
  "$MPY_CROSS" -o "$OUT/$module.mpy" "$module.py"
done

for module in "${MODULES[@]}"; do
  echo "  $module.mpy"
  "$MPREMOTE" connect "$PORT" cp "$OUT/$module.mpy" ":$module.mpy"
done

# Import prefers foo.py over foo.mpy, so a .py left by an earlier deploy would
# silently keep being compiled on the board.
"$MPREMOTE" connect "$PORT" exec "
import os
for name in ($(printf "'%s.py'," "${MODULES[@]}")):
    try:
        os.remove(name)
        print('  removed stale', name)
    except OSError:
        pass
"

# main.py last: it runs on import, so uploading it before its dependencies
# would leave a board that boots into an ImportError.
echo "  main.py"
"$MPREMOTE" connect "$PORT" cp main.py :main.py

echo
echo "Uploaded ${#MODULES[@]} modules as .mpy + main.py. Reset the board to start."
