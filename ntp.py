import socket
import ustruct
import uasyncio
import machine
import utime

# (date(2000, 1, 1) - date(1900, 1, 1)).days * 24*60*60
NTP_DELTA = 3155673600

# The NTP host can be configured at runtime by doing: ntptime.host = 'myhost.org'
host = "pool.ntp.org"


async def time():
    NTP_QUERY = bytearray(48)
    NTP_QUERY[0] = 0x1B

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    addr = socket.getaddrinfo(host, 123)[0][-1]
    sock.setblocking(False)

    try:
        sock.connect(addr)
    except OSError as e:
        if e.errno != 115:
            raise

    reader = uasyncio.StreamReader(sock)
    writer = uasyncio.StreamWriter(sock, {})

    try:
        writer.write(NTP_QUERY)
        await writer.drain()
        msg = await reader.read(48)
    finally:
        writer.close()
        await writer.wait_closed()
        reader.close()
        await reader.wait_closed()
        sock.close()

    val = ustruct.unpack("!I", msg[40:44])[0]
    return val - NTP_DELTA


# There's currently no timezone support in MicroPython, and the RTC is set in UTC time.
async def set_time():
    number_ntp_fetches = 5

    for ntp_fetch_attempt in range(number_ntp_fetches):
        try:
            t = await uasyncio.wait_for(time(), 5)
            tm = utime.gmtime(t)
            machine.RTC().datetime((tm[0], tm[1], tm[2], tm[6] + 1, tm[3], tm[4], tm[5], 0))
            return
        except OSError as e:
            if e.errno != 110:
                raise
        except uasyncio.TimeoutError:
            print("NTP fetch timed out")
            pass

        if ntp_fetch_attempt == number_ntp_fetches - 1:
            print("Failed to set NTP time")
            return False
