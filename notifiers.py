"""Alert delivery. Every notifier returns True only on confirmed delivery, so
Monitor's latch retries on the next check rather than dropping the alert.
"""

import asyncio
import json

import httpc
import log
import umail
from utils import FAILED, board, rtc_timestamp


class Notifier:
    def __init__(self, config):
        self.name = config["name"]
        self.test = config.get("test", False)

    async def post(self, subject, body, monitor, status):
        raise NotImplementedError("notifier subclass must implement post()")


class ConsoleNotifier(Notifier):
    """Prints alerts. Useful for a first run without an SMTP account."""

    async def post(self, subject, body, monitor, status):
        print("-" * 48)
        print("ALERT [{}] {}".format(rtc_timestamp(), subject))
        print(body)
        print("-" * 48)
        return True


class EmailNotifier(Notifier):
    def __init__(self, config):
        Notifier.__init__(self, config)
        self.recipients = config["recipient_addresses"]
        self.server = config["smtp_server"]
        self.port = config.get("port", 587)
        self.username = config["username"]
        self.password = config["password"]
        # None lets umail pick from the port: implicit TLS on 465, STARTTLS
        # elsewhere. "none" opts back out and sends the password in the clear.
        self.security = config.get("security")
        self.ca_file = config.get("ca_file")

    async def post(self, subject, body, monitor, status):
        log.info("email", "Sending email via {}...".format(self.server))

        smtp = umail.SMTP()
        try:
            await smtp.login(self.server, self.port, self.username,
                             self.password, self.ca_file, self.security)
            await smtp.to(self.recipients)
            await smtp.send(
                "From: minutePing <{}>\r\n"
                "Subject: minutePing: {}\r\n"
                "\r\n"
                "{}\r\n"
                "\r\n"
                "Time: {} UTC\r\n".format(self.username, subject, body, rtc_timestamp()))
            await smtp.quit()
            log.info("email", "Email sent")
            return True
        except (AssertionError, OSError, ValueError, MemoryError,
                asyncio.TimeoutError) as error:
            log.error("email", "Email delivery failed: {}".format(error))
            try:
                await smtp.close()
            except Exception:
                pass
            return False


class WebhookNotifier(Notifier):
    def __init__(self, config):
        Notifier.__init__(self, config)
        self.url = config["url"]
        self.method = config.get("method", "POST")
        self.headers = config.get("headers", {})
        self.timeout = config.get("timeout", 10)
        self.ca_file = config.get("ca_file")

    async def post(self, subject, body, monitor, status):
        latency = monitor.history.latest() if monitor is not None else FAILED

        payload = json.dumps({
            # "content"/"text" are for Discord and Slack, which only render a
            # message if one of their expected fields is present; otherwise
            # they 400 and the alert is never confirmed delivered.
            "content": body,
            "text": body,
            "service": monitor.name if monitor is not None else "test",
            "type": monitor.type if monitor is not None else "test",
            "endpoint": monitor.endpoint if monitor is not None else "",
            "status": status,
            "failures": monitor.failures if monitor is not None else 0,
            "latency_ms": None if latency == FAILED else latency,
            "subject": subject,
            "message": body,
            "time": rtc_timestamp(),
            "hostname": board.hostname,
            "board_ip": board.address(),
            "uptime": board.uptime_text(),
        })

        headers = {"Content-Type": "application/json"}
        headers.update(self.headers)

        log.info("webhook", "Posting webhook to {}...".format(self.url))

        try:
            code, _ = await httpc.request(
                self.method, self.url, headers=headers,
                body=payload, timeout=self.timeout, ca_file=self.ca_file)
        except (OSError, ValueError, MemoryError, asyncio.TimeoutError) as error:
            log.error("webhook", "Webhook delivery failed: {}".format(error))
            return False

        if code is not None and 200 <= code < 300:
            log.info("webhook", "Webhook accepted ({})".format(code))
            return True

        log.error("webhook", "Webhook rejected (status {})".format(code))
        return False


NOTIFIER_TYPES = {
    "console": ConsoleNotifier,
    "email": EmailNotifier,
    "webhook": WebhookNotifier,
}


async def send_test_alerts(notifiers, report=None):
    """Fires a startup alert through every notifier marked "test": true.

    `report` is the boot summary (signal, reset cause, IP and so on); without
    one the alert only confirms the notifier works.
    """
    for notifier in notifiers:
        if not notifier.test:
            continue
        log.info("notify", "Sending test alert via {}...".format(notifier.name))
        try:
            body = "{}\nNotifier '{}' is configured correctly.".format(
                report or "This is a minutePing test alert.", notifier.name)
            await notifier.post("minutePing started", body, None, "test")
        except Exception as error:
            log.error("notify", "Test alert via {} failed: {}".format(notifier.name, error))
