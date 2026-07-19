"""Heartbeat status message, quiet hours, and @everyone priority payloads."""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.events import Event
from app.heartbeat import build_heartbeat_event
from app.models import EventType, Game, StockCheck, Watch, utcnow
from app.notify.discord import build_payload
from app.notify.service import is_quiet_hour, parse_quiet_hours


class TestHeartbeat:
    @pytest.mark.asyncio
    async def test_stats_and_ok_title(self, session, watch):
        session.add(watch)
        await session.commit()
        session.add_all(
            [
                StockCheck(watch_id=watch.id, checked_at=utcnow()),
                StockCheck(watch_id=watch.id, checked_at=utcnow(), error="boom"),
                StockCheck(watch_id=watch.id, checked_at=utcnow() - timedelta(hours=30)),
            ]
        )
        await session.commit()
        event = await build_heartbeat_event(session)
        assert event.type == EventType.HEARTBEAT
        assert "Tracker läuft" in event.title
        assert "2 Checks" in event.message  # 30h-old check not counted
        assert "1 Fehler" in event.message
        assert "1 Watches" in event.message
        assert event.routes == ["system:heartbeat"]

    @pytest.mark.asyncio
    async def test_failing_watch_listed(self, session):
        session.add(
            Watch(
                game=Game.POKEMON, label="Kaputte Watch", url="https://x/p",
                enabled=True, last_error="robots.txt disallows",
            )
        )
        await session.commit()
        event = await build_heartbeat_event(session)
        assert "mit Problemen" in event.title
        assert "Kaputte Watch" in event.message


class TestQuietHours:
    def test_parse(self):
        assert parse_quiet_hours("22-7") == (22, 7)
        assert parse_quiet_hours("") is None
        assert parse_quiet_hours("kaputt") is None
        assert parse_quiet_hours("25-3") is None
        assert parse_quiet_hours("7-7") is None

    def test_wrapping_window(self):
        assert is_quiet_hour("22-7", 23)
        assert is_quiet_hour("22-7", 3)
        assert not is_quiet_hour("22-7", 12)
        assert not is_quiet_hour("22-7", 7)  # end is exclusive

    def test_plain_window(self):
        assert is_quiet_hour("13-15", 13)
        assert is_quiet_hour("13-15", 14)
        assert not is_quiet_hour("13-15", 15)

    def test_disabled(self):
        assert not is_quiet_hour("", 3)


class TestPriorityPayload:
    def _event(self, priority: bool) -> Event:
        return Event(
            type=EventType.BACK_IN_STOCK, game=Game.POKEMON,
            title="Back in stock: X", priority=priority, routes=["stock:pokemon"],
        )

    def test_priority_mentions_everyone(self):
        payload = build_payload(self._event(True))
        assert payload["content"] == "@everyone"
        assert payload["allowed_mentions"] == {"parse": ["everyone"]}

    def test_normal_event_has_no_mention(self):
        payload = build_payload(self._event(False))
        assert "content" not in payload
