# MinutePing

A status notifier written for the ESP8266 in MicroPython. Supports email notifications.

## Installation

Download the latest stable firmware for your ESP8266 from https://micropython.org/download/esp8266/

Linux:
```bash
git clone https://gitlab.com/coayer/MinutePing.git

# creates Python venv in current directory and installs tools
python -m venv MinutePing
source MinutePing/bin/activate
pip install esptool rshell

sudo usermod -aG dialout $USER  # user needs dialout permissions or root

# might need to adjust path from /dev/ttyUSB0
esptool.py --port /dev/ttyUSB0 erase_flash
esptool.py --port /dev/ttyUSB0 --baud 460800 write\_flash --flash\_size=detect 0 esp8266-20210618-v1.16.bin # might need to change firmware file path and baud rate

rshell
connect serial /dev/ttyUSB0
cp main.py umail.py uping.py config.json /pyboard
```

To enable remote access, set up the MicroPython WebREPL while inside an rshell REPL prompt:
```bash
rshell
connect serial /dev/ttyUSB0
repl
```
`>>> import webrepl_setup`

Then visit http://micropython.org/webrepl/ and enter your board's IP address and WebREPL password.

## Updating

#### MicroPython firmware

Follow the installation commands from the `esptool.py` commands onwards.

#### MinutePing or `config.json` via `rshell`:
```bash
git clone https://gitlab.com/coayer/MinutePing.git
rshell
connect serial /dev/ttyUSB0
cp MinutePing/main.py /pyboard  # might also require an update to config.json
```

#### MinutePing or `config.json` file via WebREPL

Follow the remote access setup above (under Installation) and visit http://micropython.org/webrepl/. 
Enter your board's IP address and WebREPL password and upload the new files with the "Send a file" option.
Click the terminal widget and press `CTRL+D` to reboot.


## Configuration

MinutePing uses a JSON configuration file called `config.json` for its settings. For information on each option, refer to the sections below.

See the `sample_config.json` file for a starter configuration file. Either copy-paste into a new file called `config.json` or rename to `config.json` and fill in the blanks. 

To test the email configuration is working, set the `send_test_email` option to `true` in the `email` section. This will send a test email using the settings from the `configuration.json` file when MinutePing starts. It may be marked as spam, so refer to your email provider's documentation on whitelisting email addresses. 

### Configuration file format

```json
{
  "services": [{...}, {...}],

  "network": {...},

  "email": {...}
}
```

### Services

 - `name`: (Required) Identifiable name for service used in email notifications
 - `host`: (Required) IP address or hostname (eg `9.9.9.9` or `www.google.com`). HTTP services can include a path (eg `www.google.com/about`)
 - `type`: (Required) Must be `http`, `dns` or `icmp` (ping)
 - `port`: (Optional) Specifies port for HTTP services. Defaults to `80`
 - `check_interval`: (Optional) Time between checks in seconds. Defaults to `60`
 - `timeout`: (Optional) Timeout of request in seconds. Defaults to `1`
 - `notify_after_failures`: (Optional) Number of consecutive failures before service offline alert is sent. Defaults to `3`

Example:

```json
{
  "name": "google",
  "check_interval": 60,
  "type": "http",
  "port": 8080,
  "host": "www.google.com",
  "timeout": 2,
  "notify_after_failures": 2
}
```

### Network

 - `ssid`: (Required) SSID of WiFi network to connect to
 - `password`: (Required) Password of WiFi network
 - `static_address`: (Optional) Off by default (uses DHCP)
    - `ip`: (Required) IP address
    - `netmask`: (Required) Netmask of network
    - `gateway`: (Required) Network router
    - `dns`: (Required) DNS server

Example:

```json
{
  "ssid": "MyWiFiNetwork",
  "password": "hunter2", 
  "static_address": {
     "ip": "192.168.1.50", 
     "netmask": "255.255.255.0", 
     "gateway": "192.168.1.1", 
     "dns": "9.9.9.9"
    }
}
```

### Email

 - `recipient_addresses`: (Required) Email addresses to send alerts to. Can be single address or array
 - `smtp_server`: (Required) SMTP server
 - `port`: (Required) SMTP port
 - `ssl`: (Optional) Enables SSL. Defaults to `false`
 - `username`: (Required) SMTP username
 - `password`: (Required) SMTP password
 - `send_test_email`: (Optional) Will send a test email on boot. Defaults to `false`

Example:

```json
{
  "recipient_addresses": "targetemail@domain.com",
  "smtp_server": "smtp.mail.com",
  "port": 587,
  "ssl": true,
  "username": "me@mail.com",
  "password": "123456789"
}
```
