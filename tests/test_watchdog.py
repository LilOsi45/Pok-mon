"""A tracker that fails quietly is worse than none.

A Pokémon Center scanner sat broken for days and the drop it was meant to catch
was nearly missed. Three things hid it, and all three were real:

  * the scanner watched the wrong URL, so it could never have matched anything;
  * its failure was reported only in the daily heartbeat;
  * that heartbeat matched no notifier, so it was never delivered — and the
    "matched no notifiers" line was logged at INFO, which this install does not
    show.

So: broken things now raise their own alert within the hour, and an event that
reaches no channel is recorded and logged as an error instead of vanishing.
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, patch

import pytest

from app import watchdog
from app.models import Game, ProductScan, Watch


@pytest.fixture(autouse=True)
def _clean():
    watchdog.reset_for_tests()
    yield
    watchdog.reset_for_tests()


def _watch(**kw) -> Watch:
    return Watch(
        game=Game.POKEMON,
        label=kw.pop("label", "W"),
        url=kw.pop("url", "https://shop.de/products/x"),
        enabled=True,
        **kw,
    )


def _scan(**kw) -> ProductScan:
    return ProductScan(
        game=Game.POKEMON,
        label=kw.pop("label", "S"),
        url=kw.pop("url", "https://shop.de/collections/neu"),
        keywords=[],
        enabled=True,
        **kw,
    )


def _age(seconds: float) -> None:
    """Pretend everything currently failing started that long ago."""
    for key in watchdog._failing_since:
        watchdog._failing_since[key] = time.monotonic() - seconds


@pytest.mark.asyncio
class TestGracePeriod:
    async def test_a_fresh_failure_does_not_alert_yet(self, session):
        session.add(_scan(label="PC", last_error="brightdata leer"))
        await session.commit()

        assert await watchdog.collect_broken(session) == []

    async def test_it_alerts_once_the_failure_persists(self, session):
        session.add(_scan(label="PC", last_error="brightdata leer"))
        await session.commit()

        await watchdog.collect_broken(session)  # first sighting starts the clock
        _age(watchdog.BROKEN_AFTER_SECONDS + 1)

        broken = await watchdog.collect_broken(session)
        assert [label for _k, label, _e in broken] == ["Scanner „PC“"]

    async def test_recovery_clears_the_clock(self, session):
        scan = _scan(label="PC", last_error="kaputt")
        session.add(scan)
        await session.commit()
        await watchdog.collect_broken(session)
        _age(watchdog.BROKEN_AFTER_SECONDS + 1)
        assert await watchdog.collect_broken(session)

        scan.last_error = None
        await session.commit()

        assert await watchdog.collect_broken(session) == []
        assert watchdog._failing_since == {}

    async def test_a_healthy_install_is_silent(self, session):
        session.add(_watch(label="fine"))
        await session.commit()
        assert await watchdog.build_alarm(session) is None


@pytest.mark.asyncio
class TestAlarm:
    async def _broken(self, session, count: int = 1):
        for i in range(count):
            session.add(_scan(label=f"S{i}", last_error=f"Fehler {i}"))
        await session.commit()
        await watchdog.collect_broken(session)
        _age(watchdog.BROKEN_AFTER_SECONDS + 1)

    async def test_the_alarm_names_what_is_broken(self, session):
        await self._broken(session)

        event = await watchdog.build_alarm(session)

        assert event is not None
        assert "S0" in event.message
        assert "Fehler 0" in event.message

    async def test_it_is_priority_so_quiet_hours_cannot_swallow_it(self, session):
        await self._broken(session)
        event = await watchdog.build_alarm(session)
        assert event.priority is True

    async def test_it_is_routed_widely(self, session):
        """An alarm no channel receives is exactly how the drop was missed."""
        await self._broken(session)
        event = await watchdog.build_alarm(session)
        assert len(event.routes) > 1

    async def test_it_does_not_repeat_immediately(self, session):
        await self._broken(session)
        assert await watchdog.build_alarm(session) is not None

        assert await watchdog.build_alarm(session) is None, "alarm repeated straight away"

    async def test_it_repeats_after_the_cooldown(self, session):
        await self._broken(session)
        await watchdog.build_alarm(session)

        for key in watchdog._alerted:
            watchdog._alerted[key] = time.monotonic() - watchdog.REPEAT_AFTER_SECONDS - 1

        assert await watchdog.build_alarm(session) is not None

    async def test_a_long_list_is_truncated(self, session):
        await self._broken(session, count=watchdog.MAX_LISTED + 4)
        event = await watchdog.build_alarm(session)
        assert "weitere" in event.message
        assert len(event.message) < 3900

    async def test_run_watchdog_never_raises(self):
        with patch("app.db.get_sessionmaker", side_effect=RuntimeError("db down")):
            await watchdog.run_watchdog()  # must not propagate


@pytest.mark.asyncio
class TestUnroutableEventsAreVisible:
    """Regression: a misrouted event left no trace at all."""

    async def test_it_is_recorded_as_a_failed_notification(self, session):
        from sqlalchemy import select

        from app.events import Event
        from app.models import EventType, Notification
        from app.notify.service import dispatch_event

        event = Event(
            type=EventType.HEARTBEAT,
            game=None,
            title="Tracker läuft",
            routes=["system:heartbeat"],
        )
        with patch("app.notify.service.load_notifiers", AsyncMock(return_value=[])):
            assert await dispatch_event(session, event) == []

        rows = (await session.execute(select(Notification))).scalars().all()
        assert len(rows) == 1
        assert rows[0].success is False
        assert "kein Notifier" in (rows[0].error or "")
        assert "system:heartbeat" in (rows[0].error or "")
