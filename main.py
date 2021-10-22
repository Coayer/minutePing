from json import load
from machine import RTC, Pin, WDT
import sys
import ntptime
import umail
import uping
import network
import socket
import uasyncio as asyncio

# TODO add exception catches for getaddr timeout incase dns fails


class Service:
    def __init__(self, service_config):
        self.name = service_config["name"]
        self.host = service_config["host"]
        self.check_interval = (service_config["check_interval"] if "check_interval" in service_config else 60)
        self.timeout = (service_config["timeout"] if "timeout" in service_config else 5)
        self.notify_after_failures = (service_config["notify_after_failures"] if "notify_after_failures" in service_config
                                      else 3)
        self.failures = 0
        self.notified = False
        self.status = False

        print("Initialized service {} {}".format(service_config["name"], service_config["host"]))

    async def monitor(self):
        while True:
            led(0)
            online = await self.test_service()
            led(1)

            if online:
                print(self.name + " online")
                self.failures = 0
                self.status = True

                if self.notified:
                    await notify(self, "online")
                    self.notified = False

            else:
                print(self.name + " offline")
                self.failures += 1

                if self.failures >= self.notify_after_failures and not self.notified:
                    print(self.name + " reached failure threshold!")
                    self.status = False
                    self.notified = await notify(self, "offline")

            await asyncio.sleep(self.check_interval)

    async def test_service(self):
        return True

    def get_name(self):
        return self.name

    def get_number_of_failures(self):
        return self.failures

    def get_check_interval(self):
        return self.check_interval

    def get_status(self):
        return self.status


class HTTPService(Service):
    def __init__(self, service_config):
        Service.__init__(self, service_config)
        self.port = (service_config["port"] if "port" in service_config else 80)
        self.response_code = (str(service_config["response_code"]) if "response_code" in service_config else "200")

        split = self.host.split('/', 1)
        if len(split) == 2:
            self.host, self.path = split
        else:
            self.path = '/'

    async def test_service(self):
        address = socket.getaddrinfo(self.host, self.port)[0][-1]

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setblocking(False)

        try:
            sock.connect(address)
        except OSError as e:
            if e.errno != 115:
                raise

        reader = asyncio.StreamReader(sock)
        writer = asyncio.StreamWriter(sock, {})

        try:
            writer.write(bytes("GET /{} HTTP/1.0\r\nHost: {}\r\n\r\n".format(self.path, self.host), "utf-8"))
            await asyncio.wait_for(writer.drain(), self.timeout)

            data = await asyncio.wait_for(reader.read(15), self.timeout)   # 15B will not work with HTTP versions >= 10
        except OSError as e:
            if e.errno == 110:
                return False
            else:
                raise
        except asyncio.TimeoutError:
            return False
        finally:
            sock.close()
            reader.close()
            await reader.wait_closed()
            writer.close()
            await writer.wait_closed()

        response_code = str(data, "utf-8").split()[1]
        return self.response_code == response_code


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
        sock.setblocking(False)

        try:
            sock.connect(address)
        except OSError as e:
            if e.errno != 115:
                raise

        reader = asyncio.StreamReader(sock)
        writer = asyncio.StreamWriter(sock, {})

        try:
            writer.write(b"\xAA\xAA\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00"
                         b"\x0a\x6d\x69\x6e\x75\x74\x65\x70\x69\x6e\x67\x04\x74\x65\x73\x74"  # minuteping.test
                         b"\x00\x00\x01\x00\x01")
            await asyncio.wait_for(writer.drain(), self.timeout)

            result = await asyncio.wait_for(reader.read(8), self.timeout)
        except OSError as e:
            if e.errno == 110:
                return False
            else:
                raise
        except asyncio.TimeoutError:
            return False
        finally:
            sock.close()
            reader.close()
            await reader.wait_closed()
            writer.close()
            await writer.wait_closed()

        # Checks if response has same ID as request and if RCODE=3 (NXDOMAIN)
        return result[0:2] == b"\xAA\xAA" and result[3] & 0x0F == 3


async def notify(service_object, status):
    await ntptime.set_time()

    minutes_since_failure = service_object.get_check_interval() * service_object.get_number_of_failures() / 60
    minutes_since_failure = int(minutes_since_failure) if int(minutes_since_failure) == minutes_since_failure \
        else round(minutes_since_failure, 1)

    current_time = rtc.datetime()

    print("Sending email notification...")

    try:
        smtp = umail.SMTP()
        # to = RECIPIENT_EMAIL_ADDRESSES if type(RECIPIENT_EMAIL_ADDRESSES) == str else ", ".join(RECIPIENT_EMAIL_ADDRESSES)
        await smtp.login(smtp_server, smtp_port, smtp_username, smtp_password)
        await smtp.to(recipient_email_addresses)
        await smtp.send("From: minutePing <{}>\n"
                        "Subject: Monitored service {} is {}\n\n"
                        "Current time: {:02d}:{:02d}:{:02d} {:02d}/{:02d}/{} UTC\n\n"
                        "Monitored service {} was detected as {} {} minutes ago.\n".format(smtp_username, service_object.get_name(), status,
                                    current_time[4], current_time[5], current_time[6],
                                    current_time[2], current_time[1], current_time[0],
                                    service_object.get_name(), status, minutes_since_failure))
        await smtp.quit()

        print("Email successfully sent")
        return True
    except (AssertionError, OSError, asyncio.TimeoutError) as e:
        print("Failed to send email notification: " + str(e.args[0]))
        return False


async def web_server_handler(reader, writer):
    print("Handling web request...")

    try:
        while True:
            line = await reader.readline()
            if not line or line == b'\r\n':
                break

        rows = ["<tr><td>{}</td><td>{}</td></tr>".format(service.get_name(), "Online" if service.get_status() else "Offline")
                for service in monitored_services]
        response = html.format('\n'.join(rows))

        writer.write('HTTP/1.0 200 OK\r\nContent-type: text/html\r\n\r\n')
        await writer.drain()
        writer.write(response)
        await writer.drain()
    except OSError as e:
        print("Web server encountered OSError " + str(e))
    finally:
        reader.close()
        await reader.wait_closed()
        writer.close()
        await writer.wait_closed()


def wifi_ap_fallback(message):
    import webrepl, time

    ap_if = network.WLAN(network.AP_IF)
    ap_if.config(essid=b"minutePing", authmode=network.AUTH_WPA_WPA2_PSK, password=b"pingpong")
    webrepl.start(password="pingpong")

    while True:
        print(message)
        time.sleep(5)


# for debugging https://github.com/peterhinch/micropython-async/blob/master/v3/docs/TUTORIAL.md#22-coroutines-and-tasks
def set_global_exception():
    def handle_exception(loop, context):
        sys.print_exception(context["exception"])
        sys.exit()
    loop = asyncio.get_event_loop()
    loop.set_exception_handler(handle_exception)


async def main():
    set_global_exception()

    for service in monitored_services:
        asyncio.create_task(service.monitor())

    while True:
        if watchdog_enabled:
            wdt.feed()
        await asyncio.sleep(1)

led = Pin(2, Pin.OUT, value=1)
led(0)

try:
    with open("config.json", "r") as config_file:
        config = load(config_file)
except ValueError:
    wifi_ap_fallback("Invalid JSON in config file!")
except OSError:
    wifi_ap_fallback("minutePing successfully installed. See documentation for creating a config.json file.")

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
    smtp_username = config["email"]["username"]
    smtp_password = config["email"]["password"]

    send_test_email = config["email"]["send_test_email"] if "send_test_email" in config["email"] else False

    webrepl_enabled = "webrepl" in config
    if webrepl_enabled:
        webrepl_password = config["webrepl"]["password"]
        if len(webrepl_password) < 4 or len(webrepl_password) > 9:
            raise ValueError("WebREPL password must be between 4 and 9 characters")

    web_server_enabled = config["web_server"] if "web_server" in config else True

    watchdog_enabled = config["watchdog"] if "watchdog" in config else True

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
    wifi_ap_fallback("Missing required configuration value " + e.args[0])

del config  # only needed for config loading

print("Activating Wi-Fi...")

sta_if = network.WLAN(network.STA_IF)
sta_if.active(True)

if static_address is not None:
    print("Using static network address :", static_address)
    sta_if.ifconfig(static_address)

print("Connecting to Wi-Fi network...")
sta_if.connect(ssid, wifi_password)
while not sta_if.isconnected():
    pass

print("Connected with network configuration " + str(sta_if.ifconfig()))

if webrepl_enabled:
    print("Starting WebREPL...")
    import webrepl
    webrepl.start(password=webrepl_password)

if web_server_enabled:
    asyncio.create_task(asyncio.start_server(web_server_handler, "0.0.0.0", 80, 20))

    html = """<!DOCTYPE html>
    <html>
        <head> <title>minutePing</title> </head>
        <body> <h1>Monitored services</h1>
            <table border="1"> <tr><th>Service name</th><th>Status</th></tr> {} </table>
        </body>
    </html>"""

print("Starting real-time clock...")
rtc = RTC()

if send_test_email:
    asyncio.create_task(notify(Service({"name": "TEST EMAIL SERVICE", "host": "email.test"}), "BEING TESTED"))

led(1)

if watchdog_enabled:
    print("Starting watchdog timer...")
    wdt = WDT()

try:
    asyncio.run(main())
finally:
    asyncio.new_event_loop()
