# minutePing

Server status monitoring firmware for the ESP32 in MicroPython.

Checks services over ICMP echo, HTTP, HTTPS, DNS and raw TCP; alerts by email,
webhook or serial console; and serves a monospace status dashboard with
per-service ASCII latency charts. Everything runs cooperatively on `asyncio`,
including TLS, so nothing a check does can stall the others.

Developed on an ESP32-C3. Nothing here is C3-specific beyond the flash offset
and the default clocks in `scripts/`, so other ESP32 variants need only a
different `--chip` and firmware image.

```
╔══ MINUTEPING // STATUS ══════════════════════╗
  UPTIME 4d 02:11:07   FREE HEAP 118.6K
  IP 192.168.1.50      SYNC 14:22:07Z

  [ OK ] GATEWAY      12ms    UP 100.0%
   200┤
   142┤              ╭─────────────╮
    85┤    ╭─────────╯             ╰────
     0┼────╯
╚══════════════════════════════════════════════╝
```

## Contents

- [Installation](#installation)
- [Certificate verification](#certificate-verification)
- [Configuration](#configuration)
- [Dashboard](#dashboard)
- [Upgrading from the ESP8266 build](#upgrading-from-the-esp8266-build)
- [Development](#development)

## Installation

Three steps: flash MicroPython, upload the project's modules, upload
`config.json`.

### 1. Flash the firmware

Stock MicroPython is not enough here: with the dashboard and TLS alerting
both resident, mbedTLS's TLS record buffers and MicroPython's GC heap
compete for the same contiguous SRAM and a handshake fails with what looks
like a certificate error but is really an allocation failure. So this project
builds a custom image — stock MicroPython v1.29.0 with the initial GC heap
lowered from 64 KB to 56 KB, board definition pinned under
[`firmware/MINUTEPING_C3_56/`](firmware/MINUTEPING_C3_56/). The modules
themselves stay ordinary files pushed separately; only the interpreter is
custom-built. See [FIRMWARE-HANDOFF.md](FIRMWARE-HANDOFF.md) for the full
story, including approaches that were tried and ruled out.

```bash
# creates a Python venv and installs the tools
python3 -m venv .flashenv && source .flashenv/bin/activate \
  && pip install --upgrade pip && pip install esptool mpremote mpy-cross

./scripts/build_firmware.sh   # requires Docker; clones MicroPython if needed
./scripts/flash_firmware.sh   # set MINUTEPING_PORT if not /dev/cu.usbmodem101
```

`build_firmware.sh` builds `build/ESP32_GENERIC_C3-v1.29.0-minutePing-heap56.bin`
inside `espressif/idf:v5.5.2` (~12.5 GB image). `flash_firmware.sh` then erases
the flash and writes it; pass `--no-erase` to reflash without wiping
`config.json`. To flash stock firmware instead (e.g. for rollback), set
`MINUTEPING_FIRMWARE` to a build from the
[ESP32-C3 download page](https://micropython.org/download/ESP32_GENERIC_C3/).

> **Flash offset.** The C3 image starts at `0x0`, not the `0x1000` used by the
> original ESP32. Writing a C3 image at `0x1000` produces a board that looks
> bricked. `flash_firmware.sh` handles this; if you flash by hand, do not copy
> an ESP32 command line.

### 2. Upload the modules

```bash
./scripts/deploy_code.sh
```

This cross-compiles `chart.py`, `config.py`, `httpc.py`, `log.py`, `monitors.py`,
`notifiers.py`, `umail.py`, `utils.py` and `web.py` to `.mpy` bytecode with
`mpy-cross` (compiling on the board instead would grow the GC heap into the
same memory TLS needs), uploads them, deletes any stale `.py` copies left by
an earlier deploy, then uploads `main.py` last as source — it runs on import,
so uploading it before its dependencies leaves a board that boots into an
`ImportError`. Re-run it after any code change and reset the board.

### 3. Configure and upload config.json

Edit `config.template.json` (see [Configuration](#configuration)) — anything
that should be a secret (Wi-Fi password, SMTP password, webhook token,
WebREPL password) gets a `${SOME_ENV_VAR}` placeholder instead of a literal
value. Then, in your own terminal, export those variables and generate
`config.json`:

```bash
export MINUTEPING_WIFI_SSID="your-wifi-name"
export MINUTEPING_WIFI_PASSWORD="your-wifi-password"
# ...one export per placeholder used in config.template.json
python3 scripts/make_config.py
```

`config.json` (gitignored) is written from the template with the placeholders
substituted in. This keeps secrets out of shell history for the upload
command, chat logs, and the repo. `scripts/deploy_config.sh` does both steps —
generate then upload — and deletes the local copy afterward:

```bash
./scripts/deploy_config.sh   # set MINUTEPING_PORT if not /dev/cu.usbmodem101
```

It uploads `ca.pem` too, if one exists — see
[Certificate verification](#certificate-verification).

Or by hand:

```bash
python3 scripts/make_config.py
mpremote connect /dev/ttyUSB0 cp config.json :config.json
mpremote connect /dev/ttyUSB0 reset
```

If a `led` pin is configured, it is on while minutePing boots and while a check
is in flight. **A solid LED means the board is stuck** — almost always a
problem with `config.json`.
Connect to the `minutePing` Wi-Fi network (password `pingpong`) and open
http://micropython.org/webrepl/ to read the error and upload a corrected file.

### Uploading config.json over WebREPL

Visit http://micropython.org/webrepl/ and keep the tab open. Browsers may
redirect to HTTPS; if that happens, clear site data for micropython.org and
navigate directly to the HTTP URL.

If the board is already on your network, open its IP in a browser and follow the
**ADMIN CONSOLE** link at the bottom of the dashboard. Otherwise join the
`minutePing` fallback network.

Click connect at the top left, enter the WebREPL password, then use "Send a
file" to upload `config.json`, making sure to click "Send to device". Reset the
board with the RST button or by power cycling.

## Certificate verification

HTTPS works out of the box, but **unverified by default**: the connection is
encrypted, and the server is not authenticated. Stock MicroPython ESP32
firmware carries no root certificate store — the ESP-IDF certificate bundle is
disabled in the port's `sdkconfig.base`, commented *"not implemented in
MicroPython"* — so there is nothing to check a certificate against until you
supply one. The board says so on the console the first time it connects, and
the dashboard shows `TLS UNVERIFIED`.

That is the same posture as `urequests` and it is fine on a trusted LAN, but it
is worth fixing if a webhook URL is a bearer token (Discord's is) or an SMTP
password is going over the link. Without verification, anyone on-path can
present their own certificate, capture the credential, and keep using it long
after they have gone.

`scripts/fetch_ca.py` builds the CA file for you:

```bash
python3 scripts/fetch_ca.py --common discord.com
./scripts/deploy_config.sh     # uploads ca.pem alongside config.json
```

Then in `config.json`:

```json
"tls": { "ca_file": "ca.pem" }
```

That sets the default for every `https://` webhook, HTTP check and SMTP
connection. Any one of them can override it with its own `ca_file`, and
`"ca_file": null` opts a single entry back out.

**How it picks the roots.** For each host it opens a real TLS connection,
verifies it against your machine's trust store, and keeps the self-signed root
the chain terminates at. You are pinning the *root*, not the leaf: Discord's
certificate rotates roughly every 90 days, but the root it chains to — GTS Root
R4 — is valid until 2036, and rotations underneath are invisible to the board.

`--common` also includes the roots behind the major public CAs, discovered the
same way from reference hosts. That costs a few KB and covers the one realistic
way pinning breaks: a host switching CA vendors entirely. Before writing the
file, the script re-checks every host against the new bundle alone, so it never
produces one the board could not have used.

Keep the bundle small. mbedTLS parses the whole file into heap when the context
is built, which is why the board cannot simply carry a desktop's ~140-cert
store.

> **The clock has to be right.** Verification checks the certificate's validity
> window, and the board boots at the start of its epoch, so an unsynchronised
> clock makes every verified connection fail as though the certificate had
> expired. minutePing waits for NTP before sending its first alert, and reports
> a verification failure distinctly from a network failure so the two are not
> confused. Monitoring starts immediately either way — a slow NTP server delays
> alerts, it does not stop checks.

## Configuration

minutePing reads a single `config.json`. Start from `config.example.json`.

```json
{
  "network": { "ssid": "MyWiFiNetwork", "password": "hunter2" },
  "tls": { "ca_file": "ca.pem" },
  "notifiers": [
    { "name": "ops-mail", "type": "email", "recipient_addresses": ["you@example.com"],
      "smtp_server": "smtp.example.org", "port": 465,
      "username": "minuteping@example.org", "password": "hunter2" }
  ],
  "services": [
    { "name": "gateway", "type": "icmp", "endpoint": "192.168.1.1", "update_frequency": 60 }
  ],
  "webrepl": { "password": "pingpong" }
}
```

### Services

Every entry in `services` is checked on its own schedule.

| Key | Required | Default | Meaning |
|---|---|---|---|
| `name` | yes | — | Identifies the service in alerts and on the dashboard. Must be unique |
| `type` | yes | — | `icmp`, `http`, `dns` or `tcp` |
| `endpoint` | yes | — | What to check; the format depends on `type`, see below |
| `update_frequency` | no | `180` | Seconds between checks |
| `timeout` | no | `5` | Seconds to wait for a response |
| `success_code` | no | `200` | HTTP response code treated as healthy |
| `max_consecutive_failures` | no | `3` | Failed checks in a row before an alert is sent |
| `history_length` | no | `60` | Samples kept for the chart and statistics |
| `icmp_attempts` | no | `1` | Echo requests before an `icmp` check gives up |
| `ca_file` | no | the global `tls.ca_file` | Roots for an `https` endpoint |
| `notifiers` | no | all | Names of the notifiers this service alerts through |

Endpoint format by type:

- `icmp` — a host or IP: `192.168.1.1`, `example.com`
- `http` — a **full URL**: `http://example.com:8080/health`
- `tcp` — `host:port`: `mail.internal:25` (a bare host defaults to port 80)
- `dns` — the resolver to query: `9.9.9.9` (or `9.9.9.9:5353`)

The DNS check asks the resolver for `minuteping.test` and treats any
well-formed reply as proof of life, so it works against resolvers that refuse
or NXDOMAIN the name.

The ICMP check sends one echo request per check over a raw socket and times the
round trip in microseconds, rounded to the nearest millisecond.

`icmp_attempts` raises that, but **think before you do**. Retrying inside a
check does not make the link better, it makes the loss invisible: a link
dropping half its packets reports ~87% uptime at 3 attempts and ~97% at 5,
because a check only counts as failed when *every* attempt is lost. At 5
attempts a link losing 80% of its packets still shows 96.9% uptime, so you lose
the gradual degradation that is the whole point of watching a link.

Smoothing transient blips is `max_consecutive_failures`' job. That layer shows
up on the dashboard as `warn` and in the `FAILS` counter, so the noise is
visible rather than silently rewritten out of the history. Raise
`icmp_attempts` only where reachability matters and loss genuinely does not.

This follows [µPing](https://gist.github.com/shawwwn/91cc8979e33e82af6d99ec34c38195fb),
the reference MicroPython ping library, which sends a fixed count and returns
transmitted/received rather than retrying until something works.

`history_length` costs two bytes of RAM per sample per service. The default of
60 is about 120 bytes; raising it well past a few hundred on a service-heavy
config will run the board out of memory.

Example:

```json
{
  "name": "homepage",
  "type": "http",
  "endpoint": "http://192.168.1.10:8080/health",
  "success_code": 204,
  "update_frequency": 120,
  "timeout": 3,
  "max_consecutive_failures": 2,
  "notifiers": ["ops-mail"]
}
```

### Notifiers

Every entry in `notifiers` needs a unique `name` and a `type`. Set
`"test": true` to fire one alert through it at boot, which is the quickest way
to confirm credentials.

An alert is only considered delivered when the notifier confirms it. A failed
send is retried on the service's next check rather than dropped, and a
"recovered" alert only goes to notifiers that were told about the outage.

#### `email`

| Key | Required | Default |
|---|---|---|
| `recipient_addresses` | yes | — (a string or a list) |
| `smtp_server` | yes | — |
| `username` | yes | — |
| `password` | yes | — |
| `port` | no | `587` |
| `security` | no | chosen from the port |
| `ca_file` | no | the global `tls.ca_file` |

Mail goes out encrypted. `security` is `tls` (encrypted from the first byte,
the convention on port 465), `starttls` (connect in plaintext, then upgrade —
the convention on 587), or `none`. Left unset it follows the port, so most
setups need only the port number.

`none` sends the password in the clear and exists only for a relay on your own
LAN that offers nothing else. The ESP8266 build had no other option; this one
does, so prefer 465.

Verification still depends on `ca_file` — see
[Certificate verification](#certificate-verification). Use a dedicated account
on a free SMTP relay rather than exposing a personal mailbox.

#### `webhook`

Sends a JSON POST. Works with Discord, Slack, Home Assistant, ntfy, Gotify and
anything else that accepts a webhook.

| Key | Required | Default |
|---|---|---|
| `url` | yes | — |
| `method` | no | `POST` |
| `headers` | no | `{}` — merged over `Content-Type: application/json` |
| `timeout` | no | `10` |
| `ca_file` | no | the global `tls.ca_file` |

```json
{
  "name": "ntfy",
  "type": "webhook",
  "url": "http://ntfy.internal/alerts",
  "headers": { "Authorization": "Bearer tk_..." }
}
```

The body is:

```json
{
  "service": "gateway", "type": "icmp", "endpoint": "192.168.1.1",
  "status": "offline", "failures": 3, "latency_ms": null,
  "subject": "gateway is offline", "message": "...", "time": "2026-09-08 14:22:07"
}
```

`https://` URLs are fully supported and run on the event loop like everything
else, so a slow handshake delays only that one delivery. Whether the
certificate is *checked* is a separate question — see
[Certificate verification](#certificate-verification).

#### `console`

Prints alerts to the serial console and WebREPL. No options. Useful for a first
run before any SMTP account or webhook exists.

### Network

| Key | Required | Meaning |
|---|---|---|
| `ssid` | yes | Network to join |
| `password` | yes | Network password |
| `static_address` | no | DHCP is used when omitted |
| `hostname` | no | DHCP hostname, so the board is findable by name |
| `power_save` | no | `false`. See below |

Wi-Fi power save is on by default on the ESP32 and parks the radio between
beacons, which adds tens of milliseconds of jitter to every measurement. Since
this board exists to measure latency, minutePing turns it off. Set
`"power_save": true` to trade accuracy back for a little current.

`static_address` needs all four of `ip`, `netmask`, `gateway` and `dns`:

```json
"static_address": {
  "ip": "192.168.1.50", "netmask": "255.255.255.0",
  "gateway": "192.168.1.1", "dns": "9.9.9.9"
}
```

### Other options

| Key | Default | Meaning |
|---|---|---|
| `webrepl` | off | `{"password": "..."}`, 4–9 characters. Omit to disable |
| `web_server` | `true` | Serves the dashboard on port 80 |
| `telnet_server` | off | Live terminal view. `true`, or `{"port": 2323, "refresh_seconds": 5}`. See [Terminal view](#terminal-view) |
| `watchdog` | `true` | Hardware watchdog. `false`, or `{"timeout_ms": 30000}` |
| `log_level` | `"info"` | Console verbosity: `"debug"`, `"info"`, `"warning"` or `"error"`. Lines are stamped with the date and time, or time since boot until NTP syncs |
| `tls` | off | `{"ca_file": "ca.pem"}`. See [above](#certificate-verification) |
| `led` | off | `{"pin": 8, "active_low": true}` |
| `cpu` | see below | `{"idle_mhz": 80, "busy_mhz": 160}` |
| `alert_template` | see below | Message body for alerts |

**`watchdog`** needs an explicit timeout on the ESP32, where the ESP8266 chose
its own, and it panics and reboots rather than quietly resetting. The default
is 30 s — generous, because a slow DNS lookup or TLS handshake runs on the same
task.

**`led`** has no default. ESP32 boards disagree about the status LED far more
than the NodeMCU did: the pin varies, so does the polarity, and on many C3
boards GPIO8 drives an addressable RGB LED that will not respond to a plain
digital write. Nothing is driven unless you name a pin, and an unusable one
logs a warning rather than failing the boot.

**`cpu`** defaults to 80 MHz idle, stepping to 160 MHz while serving a
dashboard request. Valid values are 20, 40, 80, 160 and 240, but **the C3 tops
out at 160** and rejects 240; only a full ESP32 accepts it. Anything below 80
is refused at load, because Wi-Fi does not come up at those clocks and the
failure looks like a wiring fault.

`alert_template` accepts `{name}`, `{type}`, `{endpoint}`, `{status}`,
`{failures}`, `{latency}`, and details of the board that sent the alert:
`{time}` (its clock, UTC), `{board_ip}`, `{hostname}` and `{uptime}`.
The default is:

```
Service {name} ({type} {endpoint}) is {status} after {failures} consecutive failed checks.
Reported by {hostname} ({board_ip}) at {time} UTC, up {uptime}.
```

Webhook payloads also carry `hostname`, `board_ip` and `uptime`.

## Dashboard

Open the board's IP in a browser. The page refreshes every 30 seconds and shows:

- **System** — uptime, free and total heap, IP, SSID, signal strength, CPU
  frequency, last reset cause, last NTP sync, TLS state (`unused`, `verified`
  or `UNVERIFIED`), services online, total checks and alerts sent
- **Per service** — status badge, current/min/avg/p95/max latency, uptime
  percentage over the history window, consecutive failures against the
  threshold, time since the last state change, seconds until the next check, and
  an ASCII latency chart. Failed checks appear as `╳` on the baseline

`GET /api` returns the same data as JSON, for scripting or an external
dashboard.

## Terminal view

Browsers increasingly force `https://` and then fail against the board's plain
HTTP server. For those cases (or just for SSH-style convenience) the board can
serve the same dashboard over a raw TCP socket. Enable it with
`"telnet_server": true` (port 2323, redraw every 5 s) or an object:

```json
"telnet_server": {"port": 2323, "refresh_seconds": 5}
```

```bash
telnet 192.168.1.42 2323      # or: nc 192.168.1.42 2323
```

It shows the same SYSTEM panel and per-service stats and latency charts as the
web page, in ANSI colour, redrawn in place. Keys: `q` or Ctrl-C quits; with more
than three services only one chart is shown at a time and `↑`/`↓` (or `j`/`k`)
choose which. It needs a UTF-8 terminal; `nc` works but is line-buffered, so
press Enter after a key.

It is read-only, unauthenticated and unencrypted, like the web dashboard, so
keep it on a trusted network. At most two viewers connect at once, and it is off
by default. `python3 tests/render_terminal.py` previews a frame without a board.

## Upgrading from the ESP8266 build

The application is board-agnostic MicroPython and carried over unchanged in
shape, but the deployment and a few APIs did not.

| | ESP8266 | ESP32 |
|---|---|---|
| Firmware | custom Docker build, modules frozen in | custom Docker build (56 KB heap), modules pushed separately by `deploy_code.sh` |
| Code changes | rebuild and reflash | upload `.mpy` and reset — no firmware rebuild |
| HTTPS | blocking socket, often out of memory | async, on the event loop |
| SMTP | credentials in the clear | TLS or STARTTLS |
| `WDT()` | picks its own timeout | timeout required, panics on expiry |
| `EINPROGRESS` | 115 | 119 |
| Status LED | GPIO2, active low | no default; set `led` |
| ICMP | 5 tries, a socket each | 1 echo, one socket, µs timing |
| Max clock | 160 MHz | 160 MHz on the C3, 240 on a full ESP32 |

Two of these are silent traps rather than errors. `EINPROGRESS` is read from
`errno` now instead of being hardcoded, because a wrong value makes every
non-blocking connect look like a failure on a board that is working fine. And
the ESP32's watchdog *panics* — a timeout too short for a slow TLS handshake
turns into a reboot loop that looks like a hardware fault.

Config changes are additive; a 2.0 `config.json` still loads. Worth adding:
`tls.ca_file` (see [above](#certificate-verification)), `led` if your board has
a usable one, and `network.hostname`.

## Development

There is no MicroPython toolchain requirement for working on the pure-logic
parts. `tests/` provides stubs for `machine` and `network` plus the `ticks_*`
helpers, so the config loader, history buffer, chart, alert state machine and
dashboard renderer all run under CPython.

```bash
# syntax-check every module the way the board's compiler would
python3 -m py_compile *.py

# alert state machine, ring buffer, chart, config validation
python3 tests/test_logic.py

# render the dashboard to a file and open it in a browser
python3 tests/render_dashboard.py && open tests/out/dashboard.html
```

`render_dashboard.py` builds a mix of healthy, degrading, flapping and failed
services, so the styling can be iterated on without flashing the board.

The stubs deliberately mimic the ESP32 rather than being permissive: `WDT`
rejects a missing timeout and `freq()` rejects 240 MHz, so a C3-invalid change
fails under CPython instead of on the board. `test_logic.py` also parses
`main.py` and checks that every `settings["..."]` it reads is a key that
`config.load()` actually returns. `main.py` runs `boot()` on import, so it
cannot be imported and tested directly, and a typo there would otherwise be a
boot-time `KeyError` found only after flashing.

### Layout

| File | Purpose |
|---|---|
| `main.py` | Boot, Wi-Fi, NTP, watchdog, task supervision |
| `config.py` | Loads and validates `config.json` |
| `monitors.py` | `Monitor` and the ICMP/HTTP/DNS/TCP probes |
| `notifiers.py` | Console, email and webhook delivery |
| `httpc.py` | Async HTTP/HTTPS client and the shared TLS contexts |
| `log.py` | Timestamped, levelled console logging |
| `web.py` | Streaming dashboard and JSON API |
| `tui.py` | Live ANSI terminal view over a TCP socket |
| `chart.py` | ASCII latency charts |
| `utils.py` | LED, CPU clock, ring buffer, formatting helpers |
| `umail.py` | Vendored SMTP client, with TLS and STARTTLS added |

| Script | Purpose |
|---|---|
| `scripts/build_firmware.sh` | Builds the custom firmware image in Docker |
| `scripts/flash_firmware.sh` | Flashes the firmware (`--no-erase` to keep config) |
| `scripts/deploy_code.sh` | Cross-compiles and uploads the modules as `.mpy` over `mpremote` |
| `scripts/deploy_config.sh` | Generates and uploads `config.json` (and `ca.pem`) |
| `scripts/fetch_ca.py` | Builds `ca.pem` from the hosts you talk to |
| `scripts/make_config.py` | Substitutes `${ENV_VAR}` into the config template |

## Licence

MIT. `umail.py` is by Shawwwn and `chart.py` derives from
[asciichart](https://github.com/kroitor/asciichart) by Igor Kroitor, both MIT.
