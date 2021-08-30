# ESPing


## Installation

---

#TODO

## Configuration

---

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
 - `type`: (Required) Must be `http`, `icmp` or `dns`
 - `port`: (Optional) Specifies port for HTTP services. Defaults to `80`
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
  "password": "hunter2"
}
```

### Email

 - `recipient_addresses`: (Required) Email addresses to send alerts to. Can be single address or array
 - `smtp_server`: (Required) SMTP server
 - `port`: (Required) SMTP port
 - `ssl`: (Optional) Enables SSL. Defaults to `false`
 - `username`: (Required) SMTP username
 - `password`: (Required) SMTP password

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
