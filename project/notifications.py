import os
from queue import Queue
from threading import Thread

from apprise import Apprise, AppriseConfig

from .config import CONFIG_PATH


class NotificationHandler:
    APPRISE_CONFIG_PATH = os.path.join(CONFIG_PATH, "apprise.yml")

    def __init__(self, enabled: bool | None = True):
        if enabled and os.path.exists(self.APPRISE_CONFIG_PATH):
            self.apobj = Apprise()
            config = AppriseConfig()
            config.add(self.APPRISE_CONFIG_PATH)
            self.apobj.add(config)
            self.queue: Queue = Queue()
            self.start_worker()
            self.enabled = True
        else:
            self.enabled = False

    def start_worker(self):
        Thread(target=self.process_queue, daemon=True).start()

    def process_queue(self):
        while True:
            message, attachments = self.queue.get()
            if attachments:
                self.apobj.notify(message, attach=attachments)
            else:
                self.apobj.notify(message)
            self.queue.task_done()

    def send_notification(self, message, attachments=None):
        if self.enabled:
            self.queue.put((message, attachments or []))
