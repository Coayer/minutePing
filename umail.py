# uMail (MicroMail) for MicroPython
# Copyright (c) 2018 Shawwwn <shawwwn1@gmai.com>
# License: MIT
#
# Vendored for minutePing. Changes from upstream:
#  - quit() was `def` containing `await`, a SyntaxError that made `import umail`
#    fail outright. It is now `async def`.
#  - added close(), so a failed login can release the socket.
#  - modern stdlib module names.
#  - TLS. Upstream sends AUTH over a bare socket, which hands the mailbox
#    password to anyone on-path; on the ESP8266 there was no fix for that, but
#    the ESP32's mbedTLS makes both forms of SMTP encryption workable. Implicit
#    TLS (port 465) and STARTTLS (port 587) are both supported, chosen from the
#    port unless "security" says otherwise.
#  - the socket is opened by asyncio.open_connection() rather than by hand, so
#    EINPROGRESS comes from errno instead of a hardcoded 115 (the ESP32 reports
#    119) and the TLS handshake is driven by the event loop.
import asyncio

import httpc
import log

TIMEOUT = 5  # seconds
LOCAL_DOMAIN = '127.0.0.1'
CMD_EHLO = 'EHLO'
CMD_STARTTLS = 'STARTTLS'
CMD_AUTH = 'AUTH'
CMD_MAIL = 'MAIL'
AUTH_PLAIN = 'PLAIN'
AUTH_LOGIN = 'LOGIN'

IMPLICIT_TLS_PORT = 465


class SMTP:
    def __init__(self):
        self.reader = None
        self.writer = None
        self.username = None
        self.host = None

    async def cmd(self, cmd_str):
        self.writer.write(('%s\r\n' % cmd_str).encode())
        await asyncio.wait_for(self.writer.drain(), TIMEOUT)

        resp = []
        next = True
        while next:
            code = await asyncio.wait_for(self.reader.readexactly(3), TIMEOUT)
            next = await asyncio.wait_for(self.reader.readexactly(1), TIMEOUT) == b'-'
            resp.append((await asyncio.wait_for(self.reader.readline(), TIMEOUT)).strip().decode())

        return int(code), resp

    async def connect(self, host, port, ca_file=None, security=None):
        """Opens the session and returns the EHLO feature list.

        security is 'tls' (encrypted from the first byte), 'starttls' (upgrade
        an established plaintext session), or 'none'. Left unset it follows the
        port, which is the convention every provider uses.
        """
        self.host = host

        if security is None:
            security = 'tls' if port == IMPLICIT_TLS_PORT else 'starttls'

        context = httpc.tls_context(ca_file) if security == 'tls' else None

        self.reader, self.writer = await asyncio.wait_for(
            asyncio.open_connection(host, port, ssl=context,
                                    server_hostname=host if context else None),
            TIMEOUT)

        code = int(await asyncio.wait_for(self.reader.readexactly(3), TIMEOUT))
        await asyncio.wait_for(self.reader.readline(), TIMEOUT)
        assert code == 220, 'cant connect to server'

        code, resp = await self.cmd(CMD_EHLO + ' ' + LOCAL_DOMAIN)
        assert code == 250, '%d, %s' % (code, resp)

        if security == 'starttls':
            resp = await self.starttls(resp, ca_file)

        return resp

    async def starttls(self, features, ca_file):
        """Upgrades a live plaintext session to TLS, then re-issues EHLO.

        MicroPython's asyncio has no equivalent of CPython's start_tls(), so the
        socket is taken back out of the stream and re-wrapped by hand. Stream.s
        is an internal, but it is the only way to upgrade a connection that is
        already open, and the alternative is shipping the password in the clear.
        """
        if not any(feature.upper().startswith(CMD_STARTTLS) for feature in features):
            raise OSError(
                '{} does not offer STARTTLS on this port. Use port {} for '
                'implicit TLS, or set "security": "none" on the notifier to '
                'accept sending the password in the clear.'.format(
                    self.host, IMPLICIT_TLS_PORT))

        code, resp = await self.cmd(CMD_STARTTLS)
        assert code == 220, 'starttls refused %d, %s' % (code, resp)

        context = httpc.tls_context(ca_file)
        sock = context.wrap_socket(self.writer.s, server_hostname=self.host,
                                   do_handshake_on_connect=False)
        sock.setblocking(False)

        stream = asyncio.StreamReader(sock)
        self.reader = stream
        self.writer = stream

        # EHLO again: the advertised features differ across the upgrade, and
        # AUTH is usually withheld until the channel is encrypted.
        code, resp = await self.cmd(CMD_EHLO + ' ' + LOCAL_DOMAIN)
        assert code == 250, '%d, %s' % (code, resp)
        return resp

    async def authenticate(self, username, password, features):
        auths = None
        for feature in features:
            if feature[:4].upper() == CMD_AUTH:
                auths = feature[4:].strip('=').upper().split()
        assert auths != None, "no auth method"

        from binascii import b2a_base64 as b64
        if AUTH_PLAIN in auths:
            cren = b64(("\0%s\0%s" % (username, password)).encode())[:-1].decode()
            code, resp = await self.cmd('%s %s %s' % (CMD_AUTH, AUTH_PLAIN, cren))
        elif AUTH_LOGIN in auths:
            code, resp = await self.cmd("%s %s %s" % (
                CMD_AUTH, AUTH_LOGIN, b64(username.encode())[:-1].decode()))
            assert code == 334, 'wrong username %d, %s' % (code, resp)
            code, resp = await self.cmd(b64(password.encode())[:-1].decode())
        else:
            raise Exception("auth(%s) not supported " % ', '.join(auths))

        assert code == 235 or code == 503, 'auth error %d, %s' % (code, resp)
        return code, resp

    async def login(self, host, port, username, password, ca_file=None, security=None):
        self.username = username
        features = await self.connect(host, port, ca_file, security)
        return await self.authenticate(username, password, features)

    async def to(self, addrs):
        code, resp = await self.cmd(CMD_EHLO + ' ' + LOCAL_DOMAIN)
        assert code == 250, '%d' % code
        code, resp = await self.cmd('MAIL FROM: <%s>' % self.username)
        assert code == 250, 'sender refused %d, %s' % (code, resp)

        if isinstance(addrs, str):
            addrs = [addrs]
        count = 0
        for addr in addrs:
            code, resp = await self.cmd('RCPT TO: <%s>' % addr)
            if code != 250 and code != 251:
                log.warning('smtp', '%s refused, %s' % (addr, resp))
                count += 1
        assert count != len(addrs), 'recipient refused, %d, %s' % (code, resp)

        code, resp = await self.cmd('DATA')
        assert code == 354, 'data refused, %d, %s' % (code, resp)
        return code, resp

    async def send(self, content=''):
        if content:
            self.writer.write(content.encode() if isinstance(content, str) else content)
            await asyncio.wait_for(self.writer.drain(), TIMEOUT)
        self.writer.write(b'\r\n.\r\n')  # the five letter sequence marked for ending
        await asyncio.wait_for(self.writer.drain(), TIMEOUT)
        line = await asyncio.wait_for(self.reader.readline(), TIMEOUT)
        return (int(line[:3]), line[4:].strip().decode())

    async def quit(self):
        try:
            await self.cmd("QUIT")
        finally:
            await self.close()

    async def close(self):
        """Releases the connection. Safe to call more than once."""
        if self.writer is None:
            return

        try:
            self.writer.close()
            await self.writer.wait_closed()
        except (OSError, ValueError):
            pass

        self.reader = None
        self.writer = None
