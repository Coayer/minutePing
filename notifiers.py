from services import Service
from utils import rtc
import ntptime
import umail
import uasyncio as asyncio


# TODO change to superclass of notifiers

class EmailNotifier:
    def __init__(self, config):
        self.recipient_email_addresses = config["recipient_addresses"]
        self.smtp_server = config["smtp_server"]
        self.smtp_port = config["port"]
        self.smtp_username = config["username"]
        self.smtp_password = config["password"]

        send_test = config["test"] if "test" in config else False
        if send_test:
            asyncio.create_task(self.notify(Service({"name": "TEST EMAIL SERVICE", "host": "email.test"}), "BEING TESTED"))
        del config

    async def notify(self, service_object, status):
        await ntptime.settime()

        minutes_since_failure = service_object.get_check_interval() * service_object.get_number_of_failures() / 60
        minutes_since_failure = int(minutes_since_failure) if int(minutes_since_failure) == minutes_since_failure \
            else round(minutes_since_failure, 1)

        current_time = rtc.datetime()

        print("Sending email notification...")

        try:
            smtp = umail.SMTP()
            # to = RECIPIENT_EMAIL_ADDRESSES if type(RECIPIENT_EMAIL_ADDRESSES) == str else ", ".join(RECIPIENT_EMAIL_ADDRESSES)
            await smtp.login(self.smtp_server, self.smtp_port, self.smtp_username, self.smtp_password)
            await smtp.to(self.recipient_email_addresses)
            await smtp.send("From: minutePing <{}>\n"
                            "Subject: Monitored service {} is {}\n\n"
                            "Current time: {:02d}:{:02d}:{:02d} {:02d}/{:02d}/{} UTC\n\n"
                            "Monitored service {} was detected as {} {:0.0f} minutes ago.\n".format(self.smtp_username,
                                        service_object.get_name(), status,
                                        current_time[4], current_time[5], current_time[6],
                                        current_time[2], current_time[1], current_time[0],
                                        service_object.get_name(), status, minutes_since_failure))
            await smtp.quit()

            print("Email successfully sent")
            return True
        except (AssertionError, OSError, asyncio.TimeoutError) as e:
            print("Failed to send email notification: " + str(e.args[0]))
            return False
