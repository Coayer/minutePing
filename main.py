from json import load
from sys import exit
from machine import Timer, RTC, WDT
import ntptime
import umail
import uping
import logging
import network
import socket

VALID_SERVICE_TYPES = ["icmp", "http", "dns"]
DEFAULT_CHECK_INTERVAL = 60
DEFAULT_TIMEOUT = 5
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

        self.failures = 0
        self.notified = False

        logging.info(
            "Initialized service {}, type {} with host {}".format(service_config["name"], service_config["type"],
                                                                  service_config["host"]))

    def check(self):
        if self.test_service():
            self.failures = 0
            self.notified = False
        else:
            self.failures += 1
            self.check_number_failures()

    def test_service(self):
        return True

    def check_number_failures(self):
        if self.failures >= self.NOTIFY_AFTER_FAILURES and not self.notified:
            self.notified = notify(self)

    def get_name(self):
        return self.NAME

    def get_check_interval(self):
        return self.CHECK_INTERVAL

    def get_number_of_failures(self):
        return self.failures


class HTTPService(Service):
    def __init__(self, service_config):
        Service.__init__(service_config)
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
        s.send(bytes("GET /{} HTTP/1.0\r\nHost: {}}\r\n\r\n".format(self.PATH, self.HOST), "utf-8"))
        data = s.recv(15)   # will not work with HTTP versions >= 10.0
        s.close()
        return str(data, "utf-8").split()[1] == 200


class ICMPService(Service):
    def __init__(self, service_config):
        Service.__init__(service_config)

    def test_service(self):
        return uping.ping(self.HOST, count=1, timeout=self.TIMEOUT*1000, quiet=True)[1] == 1


class DNSService(Service):
    def __init__(self, service_config):
        Service.__init__(service_config)


def notify(service_object):
    minutes_since_failure = service_object.get_check_interval() * service_object.get_number_of_failures() / 60
    current_time = rtc.now()

    logging.info("Sending email notification...")

    try:
        smtp = umail.SMTP(SMTP_SERVER, SMTP_PORT, username=SMTP_USERNAME, password=SMTP_PASSWORD, ssl=SMTP_SSL_ENABLED)

        smtp.to(RECIPIENT_EMAIL_ADDRESSES)
        smtp.send("Subject: Monitored service {} is offline\n\n"
                  "Current time: {}:{}:{} {}/{:02d}/{} UTC+{}"
                  "Monitored service {} was detected as offline {} minutes ago.".format(service_object.get_name(),
                                                                                        current_time[3], current_time[4], current_time[5],
                                                                                        current_time[2], current_time[1], current_time[0],
                                                                                        current_time[7], service_object.get_name(),
                                                                                        minutes_since_failure))
        smtp.quit()
        return True
    except AssertionError as e:
        logging.error("Failed to send email notification: " + e)
        return False


with open("config.json", "r") as config_file:
    try:
        config = load(config_file)
    except ValueError:
        logging.error("Invalid config file!")
        exit(1)

try:
    logging.info("Loading configuration...")

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
    SMTP_SSL_ENABLED = (config["email"]["ssl"] if "ssl" in config["email"] else False)
    SMTP_USERNAME = config["email"]["username"]
    SMTP_PASSWORD = config["email"]["password"]

    monitored_services = []

    for service_config in config["services"]:
        if service_config["type"] not in VALID_SERVICE_TYPES:
            logging.error("Service type {} for {} is invalid".format(service_config["type"], service_config["name"]))
            exit(1)
        elif service_config["type"] == "http":
            monitored_services.append(HTTPService(service_config))
        elif service_config["type"] == "icmp":
            monitored_services.append(ICMPService(service_config))
        elif service_config["type"] == "dns":
            monitored_services.append(DNSService(service_config))

except KeyError as e:
    logging.error("Missing required configuration value " + e.args[0])
    exit(1)

ap_if = network.WLAN(network.AP_IF)
ap_if.active(False)

logging.info("Connecting to WiFi network...")

sta_if = network.WLAN(network.STA_IF)
sta_if.active(True)
if STATIC_ADDRESS is not None:
    sta_if.ifconfig(STATIC_ADDRESS)

sta_if.connect(SSID, WIFI_PASSWORD)
while not sta_if.isconnected():
    pass

logging.info("Setting NTP time...")

rtc = RTC()
ntptime.settime()

logging.info("Starting watchdog timer...")

monitoring_timer = Timer(-1)
wdt = WDT()

monitoring_timer.init(period=1000, mode=Timer.PERIODIC, callback=lambda t: wdt.feed())  # watchdog timer feeder
for service in monitored_services:
    monitoring_timer.init(
        period=service.get_check_interval() * 1000,
        mode=Timer.PERIODIC,
        callback=lambda t: service.check()
    )
