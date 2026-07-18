"""Telegram notifier — STUB.

TODO: implement real delivery. Suggested approach:
  - POST https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto (or
    sendMessage) with chat_id from TELEGRAM_CHAT_ID env or notifier config,
    parse_mode="HTML", and a caption built from the event (title, price,
    retailer, buy link). Retry on 429 honoring `retry_after`.
Config keys (config.yaml `notifiers[].config`): {"chat_id": "..."} — the bot
token always comes from the TELEGRAM_BOT_TOKEN env var.
"""

from __future__ import annotations

import logging

from app.events import Event
from app.notify.base import Notifier, NotifierNotConfigured

log = logging.getLogger(__name__)


class TelegramNotifier(Notifier):
    type = "telegram"

    async def send(self, event: Event) -> None:
        raise NotifierNotConfigured(
            f"telegram notifier '{self.name}' is a stub — see app/notify/telegram.py TODO"
        )
