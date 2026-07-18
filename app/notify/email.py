"""Email (SMTP) notifier — STUB.

TODO: implement real delivery. Suggested approach:
  - build a MIME message from the event (subject = event.title, HTML body with
    image/price/buy link), then send via smtplib.SMTP over STARTTLS in a thread
    (`asyncio.to_thread`) using settings.smtp_* from .env.
Config keys (config.yaml `notifiers[].config`): {"to": "...", "from": "..."} —
credentials always come from SMTP_* env vars, never from config.yaml.
"""

from __future__ import annotations

import logging

from app.events import Event
from app.notify.base import Notifier, NotifierNotConfigured

log = logging.getLogger(__name__)


class EmailNotifier(Notifier):
    type = "email"

    async def send(self, event: Event) -> None:
        raise NotifierNotConfigured(
            f"email notifier '{self.name}' is a stub — see app/notify/email.py TODO"
        )
