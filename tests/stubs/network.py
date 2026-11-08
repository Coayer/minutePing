"""Minimal CPython stand-in for MicroPython's network module.

Modelled on the ESP32 port: the legacy module-level STA_IF/AP_IF and AUTH_*
constants are still there alongside the newer WLAN.IF_STA and WLAN.PM_*, which
is what v1.29 actually exposes.
"""

STA_IF = 0
AP_IF = 1

AUTH_OPEN = 0
AUTH_WEP = 1
AUTH_WPA_PSK = 2
AUTH_WPA2_PSK = 3
AUTH_WPA_WPA2_PSK = 4

STAT_IDLE = 0
STAT_CONNECTING = 1
STAT_WRONG_PASSWORD = 2
STAT_NO_AP_FOUND = 3
STAT_CONNECT_FAIL = 4
STAT_GOT_IP = 5
STAT_BEACON_TIMEOUT = 6
STAT_ASSOC_FAIL = 7
STAT_HANDSHAKE_TIMEOUT = 8

_hostname = "minuteping"


def hostname(name=None):
    global _hostname
    if name is None:
        return _hostname
    _hostname = name
    return None


class WLAN:
    IF_STA = STA_IF
    IF_AP = AP_IF

    PM_NONE = 0
    PM_PERFORMANCE = 1
    PM_POWERSAVE = 2

    def __init__(self, interface=STA_IF):
        self.interface = interface
        self._active = False
        self._connected = True
        self._config = {"essid": "STUB-NET", "ssid": "STUB-NET"}

    def active(self, state=None):
        if state is None:
            return self._active
        self._active = state
        return None

    def connect(self, ssid=None, password=None):
        self._connected = True

    def disconnect(self):
        self._connected = False

    def isconnected(self):
        return self._connected

    def status(self, field=None):
        if field == "rssi":
            return -58
        return STAT_GOT_IP

    def ifconfig(self, addresses=None):
        if addresses is not None:
            return None
        return ("192.168.1.50", "255.255.255.0", "192.168.1.1", "9.9.9.9")

    def config(self, *args, **kwargs):
        if args and len(args) == 1 and not kwargs:
            return self._config.get(args[0], "STUB-NET")
        self._config.update(kwargs)
        return None
