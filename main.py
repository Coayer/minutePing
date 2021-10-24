from json import load
from machine import WDT
from services import *
from notifiers import *
from utils import *
import sys
import network
import uasyncio as asyncio

# TODO add exception catches for getaddr timeout incase dns fails


async def web_server_handler(reader, writer):
    print("Handling web request...")

    try:
        while True:
            line = await reader.readline()
            if not line or line == b'\r\n':
                break

        table_rows = '\n'.join(["<tr><td>{}</td><td>{}</td></tr>".format(service.get_name(), "Online" if service.get_status() else "Offline")
                for service in monitored_services])

        response = html.format(table_rows, sta_if.ifconfig()[0])

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

    notifiers = []
    notifiers.append(EmailNotifier(config["email"]))    # TODO change to "notifiers" like service_config

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
            monitored_services.append(HTTPService(service_config, notifiers))
        elif service_config["type"] == "icmp":
            monitored_services.append(ICMPService(service_config, notifiers))
        elif service_config["type"] == "dns":
            monitored_services.append(DNSService(service_config, notifiers))

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
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <style> * {{ font-family: monospace; }} table {{margin: 0 auto;}} h1 {{text-align: center;}} p {{text-align: center;}}</style>
        <head> <title>minutePing</title> </head>
        <body> <h1>Monitored services</h1> 
            <table border="1"> <tr><th>Service name</th><th>Status</th></tr> {} </table>
            <p><a href="http://micropython.org/webrepl/#{}:8266/">Administrator interface</a><p>
        </body>
    </html>"""

led(1)

if watchdog_enabled:
    print("Starting watchdog timer...")
    wdt = WDT()

try:
    asyncio.run(main())
finally:
    asyncio.new_event_loop()
