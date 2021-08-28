from json import load
from sys import exit
from machine import Timer
import logging

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

    def get_check_interval(self):
        return self.CHECK_INTERVAL

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
            self.notified = self.notify()

    def notify(self):
        print("AHHHHHH!!!!!!!!")
        return True


class HTTPService(Service):
    def __init__(self, service_config):
        Service.__init__(service_config)
        self.PORT = (service_config["port"] if "port" in service_config else 80)


class ICMPService(Service):
    def __init__(self, service_config):
        Service.__init__(service_config)


class DNSService(Service):
    def __init__(self, service_config):
        Service.__init__(service_config)


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
    SMTP_SSL_ENABLED = config["email"]["ssl"]
    SMTP_USERNAME = config["email"]["username"]
    SMTP_PASSWORD = config["email"]["password"]

    monitored_services = []

    for service_config in config["email"]["services"]:
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

monitoring_timer = Timer(-1)
for service in monitored_services:
    monitoring_timer.init(
        period=service.get_check_interval() * 1000,
        mode=Timer.PERIODIC,
        callback=lambda: service.check()
    )
