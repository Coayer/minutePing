from json import load
from machine import RTC, Pin
import sys
import ntp
import umail
import uping
import network
import socket
import time
import uasyncio

#TODO umail might not be running asynchronously
#TODO add exception catches for getaddr incase dns fails
#TODO test watchdog timer (coroutines should crash whole program thanks to global exception handler)


class Service:
    def __init__(self, service_config):
        self.name = service_config["name"]
        self.host = service_config["host"]
        self.check_interval = (service_config["check_interval"] if "check_interval" in service_config else 60)
        self.timeout = (service_config["timeout"] if "timeout" in service_config else 1)
        self.notify_after_failures = (service_config["notify_after_failures"] if "notify_after_failures" in service_config
                                      else 3)
        self.failures = 0
        self.notified = False

        print("Initialized service {} {}".format(service_config["name"], service_config["host"]))

    async def monitor(self):
        print("Checking service {}...".format(self.name))

        while True:
            led(0)
            online = await self.test_service()
            led(1)

            if online:
                print(self.name + " online")
                self.failures = 0

                if self.notified:
                    await notify(self, "online")
                    self.notified = False

            else:
                print(self.name + " offline")
                self.failures += 1

                if self.failures >= self.notify_after_failures and not self.notified:
                    print(self.name + " reached failure threshold!")
                    self.notified = await notify(self, "offline")

            await uasyncio.sleep(self.check_interval)

    async def test_service(self):
        return True

    def get_name(self):
        return self.name

    def get_number_of_failures(self):
        return self.failures

    def get_check_interval(self):
        return self.check_interval


class HTTPService(Service):
    def __init__(self, service_config):
        Service.__init__(self, service_config)
        self.port = (service_config["port"] if "port" in service_config else 80)

        split = self.host.split('/', 1)
        if len(split) == 2:
            self.host, self.path = split
        else:
            self.path = '/'

    async def test_service(self):
        address = socket.getaddrinfo(self.host, self.port)[0][-1]

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(address)

        reader = uasyncio.StreamReader(sock)
        writer = uasyncio.StreamWriter(sock, {})

        writer.write(bytes("GET /{} HTTP/1.0\r\nHost: {}\r\n\r\n".format(self.path, self.host), "utf-8"))
        await writer.drain()

        try:
            data = await reader.read(15)   # will not work with HTTP versions >= 10.0
        except OSError as e:
            if e.errno == 110:
                return False
            else:
                raise

        sock.close()
        reader.close()
        await reader.wait_closed()
        writer.close()
        await writer.wait_closed()

        response_code = str(data, "utf-8").split()[1]
        return response_code == "200"


class ICMPService(Service):
    def __init__(self, service_config):
        Service.__init__(self, service_config)

    async def test_service(self):
        return await uping.ping(self.host, timeout=self.timeout)


class DNSService(Service):
    def __init__(self, service_config):
        Service.__init__(self, service_config)

    async def test_service(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        address = socket.getaddrinfo(self.host, 53)[0][-1]
        sock.settimeout(self.timeout)
        sock.connect(address)

        reader = uasyncio.StreamReader(sock)
        writer = uasyncio.StreamWriter(sock, {})

        writer.write(b"\xAA\xAA\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00"
                     b"\x0a\x6d\x69\x6e\x75\x74\x65\x70\x69\x6e\x67\x04\x74\x65\x73\x74"  # minuteping.test
                     b"\x00\x00\x01\x00\x01")
        await writer.drain()

        try:
            result = await reader.read(8)
        except OSError as e:
            if e.errno == 110:
                return False
            else:
                raise

        sock.close()
        reader.close()
        await reader.wait_closed()
        writer.close()
        await writer.wait_closed()

        # Checks if response has same ID as request and if RCODE=3 (NXDOMAIN)
        return result[0:2] == b"\xAA\xAA" and result[3] & 0x0F == 3


async def notify(service_object, status):
    await ntp.set_time()

    minutes_since_failure = service_object.get_check_interval() * service_object.get_number_of_failures() / 60
    minutes_since_failure = int(minutes_since_failure) if int(minutes_since_failure) == minutes_since_failure \
        else round(minutes_since_failure, 1)

    current_time = rtc.datetime()

    print("Sending email notification...")

    try:
        smtp = umail.SMTP()
        await smtp.login(smtp_server, smtp_port, smtp_username, smtp_password, ssl=smtp_ssl_enabled)
        # to = RECIPIENT_EMAIL_ADDRESSES if type(RECIPIENT_EMAIL_ADDRESSES) == str else ", ".join(RECIPIENT_EMAIL_ADDRESSES)
        await smtp.to(recipient_email_addresses)
        await smtp.send("From: minutePing <{}>\n"
                  "Subject: Monitored service {} is {}\n\n"
                   "Current time: {:02d}:{:02d}:{:02d} {:02d}/{:02d}/{} UTC\n\n"
                   "Monitored service {} was detected as {} {} minutes ago.".format(smtp_username, service_object.get_name(), status,
                                                                                    current_time[4], current_time[5], current_time[6],
                                                                                    current_time[2], current_time[1], current_time[0],
                                                                                    service_object.get_name(), status, minutes_since_failure))
        await smtp.quit()

        print("Email successfully sent")
        return True
    except (AssertionError, OSError) as e:
        print("Failed to send email notification: " + str(e.args[0]))
        return False


# for debugging https://github.com/peterhinch/micropython-async/blob/master/v3/docs/TUTORIAL.md#22-coroutines-and-tasks
def set_global_exception():
    def handle_exception(loop, context):
        sys.print_exception(context["exception"])
        sys.exit()
    loop = uasyncio.get_event_loop()
    loop.set_exception_handler(handle_exception)


async def main():
    global monitored_services

    set_global_exception()

    for service in monitored_services:
        uasyncio.create_task(service.monitor())

    while True:
        # wdt.feed()?
        await uasyncio.sleep(1)

with open("config.json", "r") as config_file:
    try:
        config = load(config_file)
    except ValueError:
        print("Invalid config file!")
        sys.exit(1)

try:
    print("Loading configuration...")

    ssid = config["network"]["ssid"]
    wifi_password = config["network"]["password"]

    if "static_address" in config["network"]:
        static_address = (
            config["network"]["static_address"]["ip"],
            config["network"]["static_address"]["netmask"],
            config["network"]["static_address"]["gateway"],
            config["network"]["static_address"]["dns"]
        )
    else:
        static_address = None

    recipient_email_addresses = config["email"]["recipient_addresses"]
    smtp_server = config["email"]["smtp_server"]
    smtp_port = config["email"]["port"]
    smtp_ssl_enabled = config["email"]["ssl"] if "ssl" in config["email"] else False
    smtp_username = config["email"]["username"]
    smtp_password = config["email"]["password"]

    send_test_email = config["email"]["send_test_email"] if "send_test_email" in config["email"] else False

    monitored_services = []

    for service_config in config["services"]:
        if service_config["type"] not in ["icmp", "http", "dns"]:
            print("Service configuration {} is of invalid type {}".format(service_config["name"], service_config["type"]))
            sys.exit(1)
        elif service_config["type"] == "http":
            monitored_services.append(HTTPService(service_config))
        elif service_config["type"] == "icmp":
            monitored_services.append(ICMPService(service_config))
        elif service_config["type"] == "dns":
            monitored_services.append(DNSService(service_config))

except KeyError as e:
    print("Missing required configuration value " + e.args[0])
    sys.exit(1)

print("Activating Wi-Fi...")
ap_if = network.WLAN(network.AP_IF)
ap_if.active(False)

sta_if = network.WLAN(network.STA_IF)
sta_if.active(True)

if static_address is not None:
    print("Using static network address :", static_address)
    sta_if.ifconfig(static_address)

print("Connecting to Wi-Fi network...")
sta_if.connect(ssid, wifi_password)
while not sta_if.isconnected():
    pass

print("Starting real-time clock...")
rtc = RTC()

if send_test_email:
    notify(Service({"name": "TEST EMAIL SERVICE", "host": "email.test"}), "BEING TESTED")

led = Pin(2, Pin.OUT, value=1)

try:
    uasyncio.run(main())
finally:
    uasyncio.new_event_loop()
