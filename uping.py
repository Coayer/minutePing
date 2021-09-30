# µPing (MicroPing) for MicroPython
# copyright (c) 2018 Shawwwn <shawwwn1@gmail.com>
# License: MIT

# Internet Checksum Algorithm
# Author: Olav Morken
# https://github.com/olavmrk/python-ping/blob/master/ping.py

import uasyncio
import utime
import uctypes
import usocket
import ustruct
import uos

# @data: bytes
def checksum(data):
    if len(data) & 0x1: # Odd number of bytes
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

async def ping(host, timeout=5000, size=64):
    # prepare packet
    assert size >= 16, "pkt size too small"
    pkt = b'Q'*size
    pkt_desc = {
        "type": uctypes.UINT8 | 0,
        "code": uctypes.UINT8 | 1,
        "checksum": uctypes.UINT16 | 2,
        "id": (uctypes.ARRAY | 4, 2 | uctypes.UINT8),
        "seq": uctypes.INT16 | 6,
        "timestamp": uctypes.UINT64 | 8,
    } # packet header descriptor
    h = uctypes.struct(uctypes.addressof(pkt), pkt_desc, uctypes.BIG_ENDIAN)
    h.type = 8 # ICMP_ECHO_REQUEST
    h.code = 0
    h.checksum = 0
    h.id[0:2] = uos.urandom(2)
    h.seq = 1

    # init socket
    sock = usocket.socket(usocket.AF_INET, usocket.SOCK_RAW, 1)
    sock.settimeout(timeout/1000)
    addr = usocket.getaddrinfo(host, 1)[0][-1][0] # ip address
    sock.connect((addr, 1))

    reader = uasyncio.StreamReader(sock)
    writer = uasyncio.StreamWriter(sock, {})

    # send packet
    h.checksum = 0
    h.seq = 0
    h.timestamp = utime.ticks_us()
    h.checksum = checksum(pkt)

    writer.write(pkt)
    await writer.drain()

    resp = await reader.read(4096)
    resp_mv = memoryview(resp)
    h2 = uctypes.struct(uctypes.addressof(resp_mv[20:]), pkt_desc, uctypes.BIG_ENDIAN)
    seq = h2.seq

    writer.close()
    await writer.wait_closed()

    return h2.type==0 and h2.id==h.id and seq==0 # 0: ICMP_ECHO_REPLY
