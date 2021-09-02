from json import load
from sys import exit
from machine import RTC
import ntptime
import umail
import uping
import network
import socket
import time

VALID_SERVICE_TYPES = ["icmp", "http", "dns"]
DEFAULT_CHECK_INTERVAL = 60
DEFAULT_TIMEOUT = 1
DEFAULT_NOTIFY_AFTER_FAILURES = 3


class Service:
    def __init__(self, service_config):
        self.NAME = service_config["name"]
        self.HOST = service_config["host"]
        self.CHECK_INTERVAL = (service_config["check_interval"] if "check_interval" in service_config
                               else DEFAULT_CHECK_INTERVAL)
        self.TIMEOUT = (service_config["timeout"] if "timeout" in service_config else DEFAULT_TIMEOUT)
        self.NOTIFY_AFTER_FAILURES = (service_config["notify_after_failures"] if "notify_after_failures" in service_config
                                      else DEFAULT_NOTIFY_AFTER_FAILURES)

        self.last_checked = 0

        self.failures = 0
        self.notified = False

        print("Initialized service {} {}".format(service_config["name"], service_config["host"]))

    def check(self):
        self.last_checked = time.time()
        print("Checking service {}...".format(self.NAME))

        if self.test_service():
            print("Success")
            self.failures = 0

            if self.notified:
                notify(self, "online")
                self.notified = False

        else:
            print("Failure")
            self.failures += 1

            if self.failures >= self.NOTIFY_AFTER_FAILURES and not self.notified:
                print(self.NAME + " reached failure threshold!")
                self.notified = notify(self, "offline")

    def test_service(self):
        return True

    def get_last_checked(self):
        return self.last_checked

    def get_name(self):
        return self.NAME

    def get_check_interval(self):
        return self.CHECK_INTERVAL

    def get_number_of_failures(self):
        return self.failures


class HTTPService(Service):
    def __init__(self, service_config):
        Service.__init__(self, service_config)
        self.PORT = (service_config["port"] if "port" in service_config else 80)

        split = self.HOST.split('/', 1)
        if len(split) == 2:
            self.HOST, self.PATH = split
        else:
            self.PATH = '/'

    def test_service(self):
        addr = socket.getaddrinfo(self.HOST, self.PORT)[0][-1]
        s = socket.socket()
        s.connect(addr)
        s.send(bytes("GET /{} HTTP/1.0\r\nHost: {}\r\n\r\n".format(self.PATH, self.HOST), "utf-8"))
        data = s.recv(15)   # will not work with HTTP versions >= 10.0
        s.close()
        response_code = str(data, "utf-8").split()[1]
        print("HTTP response: " + response_code)
        return response_code == "200"


class ICMPService(Service):
    def __init__(self, service_config):
        Service.__init__(self, service_config)

    def test_service(self):
        return uping.ping(self.HOST, count=1, timeout=self.TIMEOUT*1000, quiet=True)[1] == 1


class DNSService(Service):
    def __init__(self, service_config):
        Service.__init__(self, service_config)

    def test_service(self):
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.sendto(b"\xAA\xAA\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00"
                        b"\x0a\x6d\x69\x6e\x75\x74\x65\x70\x69\x6e\x67\x04\x74\x65\x73\x74"
                        b"\x00\x00\x01\x00\x01", (self.HOST, 53))
            sock.settimeout(self.TIMEOUT)
            result = sock.recvfrom(4096)[0]
            sock.close()
            # TODO accept NXDOMAIN responses
            # Checks if response has same ID as request, no errors reported and 1 question 1 answer
            return result[0:2] == b"\xAA\xAA" and result[3] & 0x0F == 0 and result[5] == result[7] == 1
        except OSError:
            return False


def set_time():
    print("Fetching NTP time...")

    number_ntp_fetches = 10  # ntp fetching is temperamental
    for ntp_fetch in range(number_ntp_fetches):
        try:
            ntptime.settime()
            break
        except (OSError, OverflowError):
            if ntp_fetch == number_ntp_fetches - 1:
                print("Failed to set NTP time")
                return False
            else:
                pass


def notify(service_object, status):
    set_time()

    minutes_since_failure = service_object.get_check_interval() * service_object.get_number_of_failures() / 60
    minutes_since_failure = int(minutes_since_failure) if int(minutes_since_failure) == minutes_since_failure \
        else round(minutes_since_failure, 1)

    current_time = rtc.datetime()

    print("Sending email notification...")

    try:
        smtp = umail.SMTP(SMTP_SERVER, SMTP_PORT, username=SMTP_USERNAME, password=SMTP_PASSWORD, ssl=SMTP_SSL_ENABLED)
        # to = RECIPIENT_EMAIL_ADDRESSES if type(RECIPIENT_EMAIL_ADDRESSES) == str else ", ".join(RECIPIENT_EMAIL_ADDRESSES)
        smtp.to(RECIPIENT_EMAIL_ADDRESSES)
        smtp.send("From: MinutePing <{}>\n"
                  "Subject: Monitored service {} is {}\n\n"
                   "Current time: {:02d}:{:02d}:{:02d} {:02d}/{:02d}/{} UTC\n\n"
                   "Monitored service {} was detected as {} {} minutes ago.".format(SMTP_USERNAME, service_object.get_name(), status,
                                                                                        current_time[4], current_time[5], current_time[6],
                                                                                        current_time[2], current_time[1], current_time[0],
                                                                                        service_object.get_name(), status, minutes_since_failure))
        smtp.quit()

        print("Email successfully sent")
        return True
    except (AssertionError, OSError) as e:
        print("Failed to send email notification: " + e.args[0])
        return False


with open("config.json", "r") as config_file:
    try:
        config = load(config_file)
    except ValueError:
        print("Invalid config file!")
        exit(1)

try:
    print("Loading configuration...")

    SSID = config["network"]["ssid"]
    WIFI_PASSWORD = config["network"]["password"]

    if "static_address" in config["network"]:
        STATIC_ADDRESS = (
            config["network"]["static_address"]["ip"],
            config["network"]["static_address"]["netmask"],
            config["network"]["static_address"]["gateway"],
            config["network"]["static_address"]["dns"]
        )
    else:
        STATIC_ADDRESS = None

    RECIPIENT_EMAIL_ADDRESSES = config["email"]["recipient_addresses"]
    SMTP_SERVER = config["email"]["smtp_server"]
    SMTP_PORT = config["email"]["port"]
    SMTP_SSL_ENABLED = config["email"]["ssl"] if "ssl" in config["email"] else False
    SMTP_USERNAME = config["email"]["username"]
    SMTP_PASSWORD = config["email"]["password"]

    SEND_TEST_EMAIL = config["email"]["send_test_email"] if "send_test_email" in config["email"] else False

    monitored_services = []

    for service_config in config["services"]:
        if service_config["type"] not in VALID_SERVICE_TYPES:
            print(service_config["type"], service_config["name"])
            exit(1)
        elif service_config["type"] == "http":
            monitored_services.append(HTTPService(service_config))
        elif service_config["type"] == "icmp":
            monitored_services.append(ICMPService(service_config))
        elif service_config["type"] == "dns":
            monitored_services.append(DNSService(service_config))

except KeyError as e:
    print("Missing required configuration value " + e.args[0])
    exit(1)

print("Activating WiFi...")
ap_if = network.WLAN(network.AP_IF)
ap_if.active(False)

sta_if = network.WLAN(network.STA_IF)
sta_if.active(True)

if STATIC_ADDRESS is not None:
    print("Using static network address :", STATIC_ADDRESS)
    sta_if.ifconfig(STATIC_ADDRESS)

print("Connecting to WiFi network...")
sta_if.connect(SSID, WIFI_PASSWORD)
while not sta_if.isconnected():
    pass

print("Starting real-time clock...")
rtc = RTC()

if SEND_TEST_EMAIL:
    notify(Service({"name": "TEST EMAIL SERVICE", "host": "email.test"}), "BEING TESTED")

while True:
    for service in monitored_services:
        if time.time() - service.get_last_checked() > service.get_check_interval():
            service.check()
