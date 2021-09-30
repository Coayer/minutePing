# uMail (MicroMail) for MicroPython
# Copyright (c) 2018 Shawwwn <shawwwn1@gmai.com>
# License: MIT
import usocket
import uasyncio

DEFAULT_TIMEOUT = 10 # sec
LOCAL_DOMAIN = '127.0.0.1'
CMD_EHLO = 'EHLO'
CMD_STARTTLS = 'STARTTLS'
CMD_AUTH = 'AUTH'
CMD_MAIL = 'MAIL'
AUTH_PLAIN = 'PLAIN'
AUTH_LOGIN = 'LOGIN'

class SMTP:
    def __init__(self, host, port, ssl=False, username=None, password=None):
        import ussl
        self.username = username

        addr = usocket.getaddrinfo(host, port)[0][-1]
        sock = usocket.socket(usocket.AF_INET, usocket.SOCK_STREAM)
        sock.settimeout(DEFAULT_TIMEOUT)
        sock.connect(addr)

        if ssl:
            sock = ussl.wrap_socket(sock)

        self.reader = uasyncio.StreamReader(sock)
        self.writer = uasyncio.StreamWriter(sock, {})

        code = int(await self.reader.read(3))
        await self.reader.readline()

        assert code==220, 'cant connect to server'

        code, resp = await self.cmd(CMD_EHLO + ' ' + LOCAL_DOMAIN)
        assert code==250, '%d' % code
        if CMD_STARTTLS in resp:
            code, resp = await self.cmd(CMD_STARTTLS)
            assert code==220, 'start tls failed %d, %s' % (code, resp)
            sock = ussl.wrap_socket(sock)
            self.reader = uasyncio.StreamReader(sock)
            self.writer = uasyncio.StreamWriter(sock, {})

        if username and password:
            await self.login(username, password)

    async def cmd(self, cmd_str):
        self.writer.write('%s\r\n' % cmd_str)
        await self.writer.drain()

        resp = []
        next = True
        while next:
            code = await self.reader.read(3)
            next = await self.reader.read(1) == b'-'
            resp.append(await self.reader.readline().strip().decode())
        return int(code), resp

    async def login(self, username, password):
        self.username = username
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

    def to(self, addrs, mail_from=None):
        mail_from = self.username if mail_from==None else mail_from
        code, resp = await self.cmd(CMD_EHLO + ' ' + LOCAL_DOMAIN)
        assert code==250, '%d' % code
        code, resp = await self.cmd('MAIL FROM: <%s>' % mail_from)
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

    async def write(self, content):
        self.writer.write(content)
        await self.writer.drain()

    async def send(self, content=''):
        if content:
            await self.write(content)
        self.writer.write('\r\n.\r\n') # the five letter sequence marked for ending
        await self.writer.drain()
        line = await self.reader.readline()
        return (int(line[:3]), line[4:].strip().decode())

    def quit(self):
        await self.cmd("QUIT")
        self.writer.close()
        await self.writer.wait_closed()
