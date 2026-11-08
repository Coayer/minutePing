#!/usr/bin/env bash
# Run this yourself, in your own terminal — not through an assistant.
#
# Generates config.json from config.template.json + env vars, uploads it
# straight to the board over serial, then deletes the local copy so it never
# sits on disk afterward. Requires MINUTEPING_WIFI_SSID, MINUTEPING_WIFI_PASSWORD,
# MINUTEPING_GATEWAY_IP (or whatever placeholders config.template.json uses)
# to already be exported, and MINUTEPING_PORT to override the auto-detected serial port.
# MPREMOTE can point at a specific binary (e.g. a venv's) if mpremote isn't on PATH.
#
# If ca.pem exists (see scripts/fetch_ca.py) it is uploaded too, since
# config.json's tls.ca_file refers to it and the board validates that the file
# is present at boot.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

# The ESP32-C3 usually enumerates over its native USB-Serial-JTAG, so it shows
# up as a usbmodem rather than the usbserial a UART bridge gives you.
# The number in the name changes with the USB port, so auto-detect unless
# MINUTEPING_PORT is set.
if [ -z "${MINUTEPING_PORT:-}" ]; then
  shopt -s nullglob
  candidates=(/dev/cu.usbmodem* /dev/cu.usbserial*)
  shopt -u nullglob
  if [ "${#candidates[@]}" -eq 0 ]; then
    echo "No board found (/dev/cu.usbmodem* or /dev/cu.usbserial*); set MINUTEPING_PORT." >&2
    exit 1
  elif [ "${#candidates[@]}" -gt 1 ]; then
    echo "Several serial ports found: ${candidates[*]}; set MINUTEPING_PORT." >&2
    exit 1
  fi
  MINUTEPING_PORT="${candidates[0]}"
fi
PORT="$MINUTEPING_PORT"
echo "Using $PORT"
if [ -n "${MPREMOTE:-}" ]; then
  : # explicit override
elif [ -x ".flashenv/bin/mpremote" ]; then
  MPREMOTE=".flashenv/bin/mpremote"
else
  MPREMOTE="mpremote"
fi

python3 scripts/make_config.py

trap 'rm -f config.json' EXIT

"$MPREMOTE" connect "$PORT" cp config.json :config.json

if [ -f ca.pem ]; then
  "$MPREMOTE" connect "$PORT" cp ca.pem :ca.pem
  echo "ca.pem uploaded."
fi

echo "config.json uploaded and removed locally."
