from utils import *
import uping
import uasyncio as asyncio
import socket


class Service:
    def __init__(self, service_config, notifiers=None):
        if notifiers is None:
            notifiers = []
        self.name = service_config["name"]
        self.host = service_config["host"]
        self.check_interval = (service_config["check_interval"] if "check_interval" in service_config else 60)
        self.timeout = (service_config["timeout"] if "timeout" in service_config else 5)

        self.notifiers = notifiers
        self.notified = [False] * len(self.notifiers)
        self.notify_after_failures = (service_config["notify_after_failures"] if "notify_after_failures" in service_config
                                      else 3)
        self.failures = 0

        self.status = False

        print("Initialized service {} {}".format(self.name, self.host))
        del service_config

    async def monitor(self):
        while True:
            led(0)
            online = await self.test_service()
            led(1)

            if online:
                print(self.name + " online")
                self.failures = 0
                self.status = True

                for notifier in range(len(self.notifiers)):
                    if self.notified[notifier]:
                        self.notified[notifier] = not await self.notifiers[notifier].notify(self, "online")

            else:
                print(self.name + " offline")
                self.failures += 1

                if self.failures >= self.notify_after_failures:
                    print(self.name + " reached failure threshold!")
                    self.status = False

                    for notifier in range(len(self.notifiers)):  # possible to not be notified at all if notifiers fail
                        if not self.notified[notifier]:
                            self.notified = await self.notifiers[notifier].notify(self, "offline")

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
    def __init__(self, service_config, notifiers=None):
        Service.__init__(self, service_config, notifiers)
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
    def __init__(self, service_config, notifiers=None):
        Service.__init__(self, service_config, notifiers)

    async def test_service(self):
        return await uping.ping(self.host, timeout=self.timeout)


class DNSService(Service):
    def __init__(self, service_config, notifiers=None):
        Service.__init__(self, service_config, notifiers)

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
