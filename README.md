# minutePing

Server status monitoring firmware for the ESP8266 in MicroPython. Supports email notifications.

## Installation

Download `minutePing.bin` from the latest release on https://github.com/Coayer/minutePing/releases.

Connect your board to your PC.

```bash
# creates Python venv in current directory and installs tools
python3 -m venv minutePing && source minutePing/bin/activate && pip install --upgrade pip
&& pip install esptool

#adds user to dialout group
sudo usermod -aG dialout $USER

# might need to adjust path from /dev/ttyUSB0
esptool.py --port /dev/ttyUSB0 erase_flash # wipes storage
esptool.py --port /dev/ttyUSB0 --baud 460800 write\_flash --flash\_size=detect 0 minutePing.bin # installs firmware

rm -rf minutePing # (optional) deletes tools
```

Before continuing with the next steps, read the [configuration documentation](#configuration) below.

Visit http://micropython.org/webrepl/ and keep the tab open. Browsers may redirect to HTTPS. If this happens, clear site data for micropython.org and navigate directly to the HTTP URL.

Connect to the Wi-Fi network `minutePing` with password `pingpong`.

At the top left of your WebREPL browser tab, click connect. Enter the password `pingpong` when prompted.

Upload your `config.json` file with the "Send a file" option at the top right of the screen, making sure to click "Send to device".

Reset your board by disconnecting it from power or using its RST button.

If there is a problem with the uploaded `config.json` file, you can modify it using the same process (after the bash commands).

#### Updating `config.json` via WebREPL

The WebREPL gives remote access to your minutePing installation. It can be used to check logs in realtime and modify the `config.json` file.

Enter the IP address of your board in a web browser. You should see the status page. Click the "Administrator interface" link. To fetch the existing `config.json` file from the board, use the "Get a file" option.
Click the terminal widget and press `CTRL+C` to stop minutePing. The board will then reboot automatically.

#### Updating minutePing firmware

Before flashing, check release on GitLab for breaking changes with `config.json`.

```bash
source minutePing/bin/activate # if this fails, instead enter:
# python -m venv minutePing && source minutePing/bin/activate && pip install esptool

sudo esptool.py --port /dev/ttyUSB0 --baud 460800 write\_flash --flash\_size=detect 0 minutePing.bin
```

#### Service status webpage

Enter your board's IP address into a browser to see the current status of the monitored services.

## Configuration

minutePing uses a JSON configuration file called `config.json`. For information on each option, refer to the sections below.

See [Sample configuration file](#sample-configuration-file) for a starter configuration file. Copy-paste into a new file called `config.json` and fill in the blanks. 

While minutePing is starting and while services are being checked, the LED on the ESP8266 will turn on. If the LED is stuck on, power cycle or reset the board using RST. This is likely due to an issue with your `config.json` file.

minutePing does not support SMTP over SSL/TLS. Use a free SMTP server with a dedicated account to avoid exposing your personal email account.

To test the email configuration, set the `send_test_email` option to `true` in the `email` section. This will send a test email using the settings from `configuration.json` when minutePing starts. minutePing's emails may be marked as spam, so refer to your email provider's documentation on whitelisting email addresses. 

### Sample configuration file

```json
{
  "services": [
    {
      "name": "",
      "type": "",
      "host": ""
    }
  ],

  "network": {
    "ssid": "",
    "password": ""
  },

  "email": {
    "recipient_addresses": "",
    "smtp_server": "",
    "port": 587,
    "username": "",
    "password": "",
    "send_test_email": true
  },

  "webrepl" : {
    "password": "pingpong"
  }
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

To disable, remove the `webrepl` section from `config.json`.

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
