# uMail (MicroMail) for MicroPython
# Copyright (c) 2018 Shawwwn <shawwwn1@gmai.com>
# License: MIT
import usocket
import uasyncio as asyncio

TIMEOUT = 5 # sec
LOCAL_DOMAIN = '127.0.0.1'
CMD_EHLO = 'EHLO'
CMD_STARTTLS = 'STARTTLS'
CMD_AUTH = 'AUTH'
CMD_MAIL = 'MAIL'
AUTH_PLAIN = 'PLAIN'
AUTH_LOGIN = 'LOGIN'

class SMTP:
    def __init__(self):
        pass

    async def cmd(self, cmd_str):
        self.writer.write('%s\r\n' % cmd_str)
        await asyncio.wait_for(self.writer.drain(), TIMEOUT)

        resp = []
        next = True
        while next:
            code = await asyncio.wait_for(self.reader.readexactly(3), TIMEOUT)
            next = await asyncio.wait_for(self.reader.readexactly(1), TIMEOUT) == b'-'
            resp.append((await asyncio.wait_for(self.reader.readline(), TIMEOUT)).strip().decode())

        return int(code), resp

    async def login(self, host, port, username, password):
        self.username = username

        addr = usocket.getaddrinfo(host, port)[0][-1]
        self.sock = usocket.socket(usocket.AF_INET, usocket.SOCK_STREAM)
        self.sock.setblocking(False)

        try:
            self.sock.connect(addr)
        except OSError as e:
            if e.errno != 115:
                raise

        self.reader = asyncio.StreamReader(self.sock)
        self.writer = asyncio.StreamWriter(self.sock, {})

        code = int(await asyncio.wait_for(self.reader.readexactly(3), TIMEOUT))
        await asyncio.wait_for(self.reader.readline(), TIMEOUT)

        assert code == 220, 'cant connect to server'

        code, resp = await self.cmd(CMD_EHLO + ' ' + LOCAL_DOMAIN)
        assert code==250, '%d, %s' % (code, resp)

        auths = None
        for feature in resp:
            if feature[:4].upper() == CMD_AUTH:
                auths = feature[4:].strip('=').upper().split()
        assert auths!=None, "no auth method"

        from ubinascii import b2a_base64 as b64
        if AUTH_PLAIN in auths:
            cren = b64("\0%s\0%s" % (username, password))[:-1].decode()
            code, resp = await self.cmd('%s %s %s' % (CMD_AUTH, AUTH_PLAIN, cren))
        elif AUTH_LOGIN in auths:
            code, resp = await self.cmd("%s %s %s" % (CMD_AUTH, AUTH_LOGIN, b64(username)[:-1].decode()))
            assert code==334, 'wrong username %d, %s' % (code, resp)
            code, resp = await self.cmd(b64(password)[:-1].decode())
        else:
            raise Exception("auth(%s) not supported " % ', '.join(auths))

        assert code==235 or code==503, 'auth error %d, %s' % (code, resp)
        return code, resp

    async def to(self, addrs):
        code, resp = await self.cmd(CMD_EHLO + ' ' + LOCAL_DOMAIN)
        assert code==250, '%d' % code
        code, resp = await self.cmd('MAIL FROM: <%s>' % self.username)
        assert code==250, 'sender refused %d, %s' % (code, resp)

        if isinstance(addrs, str):
            addrs = [addrs]
        count = 0
        for addr in addrs:
            code, resp = await self.cmd('RCPT TO: <%s>' % addr)
            if code!=250 and code!=251:
                print('%s refused, %s' % (addr, resp))
                count += 1
        assert count!=len(addrs), 'recipient refused, %d, %s' % (code, resp)

        code, resp = await self.cmd('DATA')
        assert code==354, 'data refused, %d, %s' % (code, resp)
        return code, resp

    async def send(self, content=''):
        if content:
            self.writer.write(content)
            await asyncio.wait_for(self.writer.drain(), TIMEOUT)
        self.writer.write('\r\n.\r\n') # the five letter sequence marked for ending
        await asyncio.wait_for(self.writer.drain(), TIMEOUT)
        line = await asyncio.wait_for(self.reader.readline(), TIMEOUT)
        return (int(line[:3]), line[4:].strip().decode())

    def quit(self):
        await self.cmd("QUIT")

        self.sock.close()
        self.reader.close()
        await self.reader.wait_closed()
        self.writer.close()
        await self.writer.wait_closed()
