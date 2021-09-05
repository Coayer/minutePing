import usocket
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

    reader, writer = await uasyncio.open_connection(host, 123)

    try:
        writer.write(NTP_QUERY)
        await writer.drain()

        msg = await reader.read(48)
    finally:
        writer.close()
        await writer.wait_closed()

    val = ustruct.unpack("!I", msg[40:44])[0]
    return val - NTP_DELTA


# There's currently no timezone support in MicroPython, and the RTC is set in UTC time.
async def settime():
    t = await time()
    tm = utime.gmtime(t)
    machine.RTC().datetime((tm[0], tm[1], tm[2], tm[6] + 1, tm[3], tm[4], tm[5], 0))
