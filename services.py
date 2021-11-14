from utils import led
from math import isnan
import uasyncio as asyncio
import socket
import time
import uctypes
import ustruct
import uos
import uselect


class Service:
    def __init__(self, config, notifiers=None):
        if notifiers is None:
            notifiers = []
        self.name = config["name"]
        self.host = config["host"]
        self.check_interval = (config["check_interval"] if "check_interval" in config else 180)
        self.timeout = (config["timeout"] if "timeout" in config else 1)

        self.notifiers = notifiers
        self.notified = [False] * len(self.notifiers)
        self.notify_after_failures = (config["notify_after_failures"] if "notify_after_failures" in config else 3)
        self.failures = 0
        self.status = False

        self.history = []
        self.max_history_length = 50   # could be user programmable

        print("Initialized service {} {}".format(self.name, self.host))
        del config

    async def monitor(self):
        while True:
            led(0)
            latency = await self.test_service()
            led(1)

            self.history.append(latency)
            if len(self.history) > self.max_history_length:
                self.history.pop(0)

            if not isnan(latency):
                print("{} online {}ms".format(self.name, latency))
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
                            self.notified[notifier] = await self.notifiers[notifier].notify(self, "offline")

            await asyncio.sleep(self.check_interval)

    async def test_service(self):
        return float("nan")

    def get_name(self):
        return self.name

    def get_number_of_failures(self):
        return self.failures

    def get_check_interval(self):
        return self.check_interval

    def get_status(self):
        return self.status

    def get_history(self):
        return self.history


class HTTPService(Service):
    def __init__(self, config, notifiers=None):
        Service.__init__(self, config, notifiers)
        self.port = (config["port"] if "port" in config else 80)
        self.response_code = (str(config["response_code"]) if "response_code" in config else "200")

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

            start_check_time = time.ticks_ms()
            data = await asyncio.wait_for(reader.read(15), self.timeout)   # 15B will not work with HTTP versions >= 10
            latency = time.ticks_ms() - start_check_time
        except OSError as e:
            if e.errno == 110:
                return float("nan")
            else:
                raise
        except asyncio.TimeoutError:
            return float("nan")
        finally:
            sock.close()
            reader.close()
            await reader.wait_closed()
            writer.close()
            await writer.wait_closed()

        response_code = str(data, "utf-8").split()[1]
        if self.response_code == response_code:
            return latency
        else:
            return float("nan")


class ICMPService(Service):
    def __init__(self, config, notifiers=None):
        Service.__init__(self, config, notifiers)

    def checksum(self, data):
        if len(data) & 0x1:  # Odd number of bytes
            data += b'\0'
        cs = 0
        for pos in range(0, len(data), 2):
            b1 = data[pos]
            b2 = data[pos + 1]
            cs += (b1 << 8) + b2
        while cs >= 0x10000:
            cs = (cs & 0xffff) + (cs >> 16)
        cs = ~cs & 0xffff
        return cs

    async def test_service(self):
        size = 64
        # prepare packet
        pkt = b'Q' * size
        pkt_desc = {
            "type": uctypes.UINT8 | 0,
            "code": uctypes.UINT8 | 1,
            "checksum": uctypes.UINT16 | 2,
            "id": (uctypes.ARRAY | 4, 2 | uctypes.UINT8),
            "seq": uctypes.INT16 | 6,
            "timestamp": uctypes.UINT64 | 8,
        }  # packet header descriptor
        h = uctypes.struct(uctypes.addressof(pkt), pkt_desc, uctypes.BIG_ENDIAN)
        h.type = 8  # ICMP_ECHO_REQUEST
        h.code = 0
        h.checksum = 0
        h.id[0:2] = uos.urandom(2)
        h.seq = 1

        # needed because wifi pings are super temperamental
        for seq in range(5):
            try:
                addr = socket.getaddrinfo(self.host, 1)[0][-1][0]  # ip address
            except IndexError:
                print("Could not determine the address of", self.host)
                return float("nan")

            # init socket
            sock = socket.socket(socket.AF_INET, socket.SOCK_RAW, 1)
            sock.setblocking(False)

            try:
                sock.connect((addr, 1))
            except OSError as e:
                if e.errno != 115:
                    raise

            reader = asyncio.StreamReader(sock)
            writer = asyncio.StreamWriter(sock, {})

            h.seq = seq
            h.timestamp = time.ticks_us()
            h.checksum = self.checksum(pkt)

            try:
                writer.write(pkt)
                await asyncio.wait_for(writer.drain(), self.timeout)

                start_check_time = time.ticks_ms()
                resp = await asyncio.wait_for(reader.readexactly(64), self.timeout)
                latency = time.ticks_ms() - start_check_time
            except OSError as e:
                if e.errno == 110:
                    continue
                else:
                    raise
            except asyncio.TimeoutError:
                continue
            finally:
                sock.close()
                reader.close()
                await reader.wait_closed()
                writer.close()
                await writer.wait_closed()

            resp_mv = memoryview(resp)
            h2 = uctypes.struct(uctypes.addressof(resp_mv[20:]), pkt_desc, uctypes.BIG_ENDIAN)

            if h2.type == 0 and h2.id == h.id and h2.seq == seq:
                return latency

        return float("nan")


class DNSService(Service):
    def __init__(self, config, notifiers=None):
        Service.__init__(self, config, notifiers)

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

            start_check_time = time.ticks_ms()
            result = await asyncio.wait_for(reader.read(8), self.timeout)
            latency = time.ticks_ms() - start_check_time
        except OSError as e:
            if e.errno == 110:
                return float("nan")
            else:
                raise
        except asyncio.TimeoutError:
            return float("nan")
        finally:
            sock.close()
            reader.close()
            await reader.wait_closed()
            writer.close()
            await writer.wait_closed()

        # Checks if response has same ID as request and if RCODE=3 (NXDOMAIN)
        if result[0:2] == b"\xAA\xAA" and result[3] & 0x0F == 3:
            return latency
        else:
            return float("nan")
