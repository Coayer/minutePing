# minutePing

Server status monitoring firmware for the ESP8266 in MicroPython. Supports email notifications.

## Installation

Download the latest stable firmware for your ESP8266 from https://micropython.org/download/esp8266/

Linux:
```bash
git clone https://gitlab.com/coayer/minutePing.git

# creates Python venv in current directory and installs tools
python -m venv minutePing
source minutePing/bin/activate
pip install esptool rshell

sudo usermod -aG dialout $USER  # user needs dialout permissions or root

# might need to adjust path from /dev/ttyUSB0
esptool.py --port /dev/ttyUSB0 erase_flash
esptool.py --port /dev/ttyUSB0 --baud 460800 write\_flash --flash\_size=detect 0 esp8266-*.bin # might need to change firmware file path and baud rate

rshell
connect serial /dev/ttyUSB0
cp main.py umail.py uping.py ntp.py config.json /pyboard
```

To enable remote access, set up the MicroPython WebREPL while inside an rshell REPL prompt:
```bash
rshell
connect serial /dev/ttyUSB0
repl
```
`>>> import webrepl_setup`

Then visit http://micropython.org/webrepl/ and enter your board's IP address and WebREPL password.

#### Updating `config.json` via WebREPL

Visit http://micropython.org/webrepl/. 
Enter your board's IP address and WebREPL password and upload the new `config.json` file with the "Send a file" option. To fetch the existing `config.json` file from the board, use the "Get a file" option.
Click the terminal widget and press `CTRL+C` to stop minutePing. The board will then reboot automatically.

#### Service status webpage

Enter your board's IP address into a browser to see the current status of the monitored services.

## Configuration

minutePing uses a JSON configuration file called `config.json`. For information on each option, refer to the sections below.

See `sample_config.json` for a starter configuration file. Either copy-paste into a new file called `config.json` or rename to `config.json` and fill in the blanks. 

While minutePing is starting and while services are being checked, the LED on the ESP8266 will turn on. If the LED is stuck on, power cycle or reset the board using RST. This is likely due to an issue with your `config.json` file.

minutePing does not support SMTP over SSL/TLS. Use a free SMTP server with a dedicated account to avoid exposing your personal email account.

To test the email configuration, set the `send_test_email` option to `true` in the `email` section. This will send a test email using the settings from `configuration.json` when minutePing starts. minutePing's emails may be marked as spam, so refer to your email provider's documentation on whitelisting email addresses. 

The WebREPL gives remote access to your minutePing installation. It can be used to check logs in realtime and modify the `config.json` file. The WebREPL will only be enabled if a JSON object exists in the configuration with the name `webrepl`.

### Example configuration file format

```json
{
   "services": [{}, {}],
   "network": {},
   "email": {}, 
   "webrepl" : {}
}
```

### Services

 - `name`: (Required) Identifiable name for service used in email notifications
 - `host`: (Required) IP address or hostname (eg `9.9.9.9` or `www.google.com`). HTTP services can include a path (eg `www.google.com/about`)
 - `type`: (Required) Must be `http`, `dns` or `icmp` (ping)
 - `port`: (Optional) Specifies port for HTTP services. Defaults to `80`
 - `response_code`: (Optional) Specifies response code to check against for HTTP services. Defaults to `200`
 - `check_interval`: (Optional) Time between checks in seconds. Defaults to `60`
 - `timeout`: (Optional) Timeout of request in seconds. Defaults to `5`
 - `notify_after_failures`: (Optional) Number of consecutive failures before service offline alert is sent. Defaults to `3`

Example:

```json
{
  "name": "google",
  "check_interval": 60,
  "type": "http",
  "port": 8080,
  "host": "www.google.com/minutePing",
  "response_code": 404, 
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
 - `username`: (Required) SMTP username
 - `password`: (Required) SMTP password
 - `send_test_email`: (Optional) Will send a test email on boot. Defaults to `false`

Example:

```json
{
  "recipient_addresses": "targetemail@domain.com",
  "smtp_server": "smtp.mail.com",
  "port": 587,
  "username": "me@mail.com",
  "password": "123456789"
}
```

### WebREPL

 - `password`: (Required) WebREPL access password. Must have length between 4 and 9 characters

Example:

```json
{
   "password": "minute"
}
```

### Miscellaneous optional boolean flags

 - `watchdog`: Sets watchdog timer
 - `web_server`: Sets web server for status page
