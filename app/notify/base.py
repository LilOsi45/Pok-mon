"""Notifier interface. Implementations: Discord (working), Email/Telegram (stubs)."""

from __future__ import annotations

from abc import ABC, abstractmethod

from app.events import Event, route_matches


class NotifyError(Exception):
    """Raised when a notifier fails to deliver."""


class NotifierNotConfigured(NotifyError):
    """Raised when a notifier is missing required configuration."""


class Notifier(ABC):
    """A single notification target (one webhook / mailbox / chat)."""

    type: str = "base"

    def __init__(self, name: str, config: dict, routes: list[str] | None = None) -> None:
        self.name = name
        self.config = config
        self.routes = routes or ["*"]

    def matches(self, event: Event) -> bool:
        from app.models import EventType

        if event.type == EventType.TEST:
            return True  # test notifications go everywhere
        return any(route_matches(pattern, event.routes) for pattern in self.routes)

    @abstractmethod
    async def send(self, event: Event) -> None:
        """Deliver the event. Raise NotifyError on failure."""
