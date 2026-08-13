"""A drop that only ever appears as somebody's message must still reach us.

The tracker polls shop pages. Plenty of drops surface first — or only — in a
cook group, and by the time a shop page reflects them the thing is gone. These
tests cover the path from an incoming Discord message to a dispatched ping.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.keyword_pinger import IncomingMessage, build_event, handle_message, reset_duplicates
from app.models import KeywordAlert

MSG = IncomingMessage(
    content="Pokémon 30 Jahre Top-Trainer-Box für 59,99 € live bei Geeksheaven!",
    channel_id="4242",
    channel_name="cook-alerts",
    author="Nutzer",
    guild="TCG Cooks",
    url="https://discord.com/channels/1/2/3",
)


@pytest.fixture(autouse=True)
def _clean():
    reset_duplicates()
    yield
    reset_duplicates()


async def _alert(session, **kw) -> KeywordAlert:
    alert = KeywordAlert(
        label=kw.pop("label", "Top-Trainer-Box"),
        positive=kw.pop("positive", "top-trainer-box"),
        negative=kw.pop("negative", ""),
        ranges=kw.pop("ranges", ""),
        channels=kw.pop("channels", ["poke-de"]),
        enabled=kw.pop("enabled", True),
        **kw,
    )
    session.add(alert)
    await session.commit()
    return alert


@pytest.mark.asyncio
class TestHandleMessage:
    async def test_a_hit_dispatches_one_ping(self, session):
        await _alert(session)
        with patch("app.notify.service.dispatch_event", AsyncMock()) as sent:
            fired = await handle_message(session, MSG)
        assert len(fired) == 1
        assert sent.await_count == 1

    async def test_a_miss_is_silent(self, session):
        await _alert(session, positive="charizard")
        with patch("app.notify.service.dispatch_event", AsyncMock()) as sent:
            assert await handle_message(session, MSG) == []
        assert sent.await_count == 0

    async def test_a_disabled_alert_never_fires(self, session):
        await _alert(session, enabled=False)
        with patch("app.notify.service.dispatch_event", AsyncMock()):
            assert await handle_message(session, MSG) == []

    async def test_the_same_message_again_does_not_ping_twice(self, session):
        """A lively channel posts one drop three times in a row."""
        await _alert(session)
        with patch("app.notify.service.dispatch_event", AsyncMock()) as sent:
            await handle_message(session, MSG)
            await handle_message(session, MSG)
        assert sent.await_count == 1

    async def test_hits_are_counted(self, session):
        alert = await _alert(session)
        with patch("app.notify.service.dispatch_event", AsyncMock()):
            await handle_message(session, MSG)
        stored = (await session.execute(select(KeywordAlert))).scalars().one()
        assert stored.hits == 1 and stored.last_hit_at is not None
        assert stored.id == alert.id

    async def test_two_alerts_can_fire_on_one_message(self, session):
        await _alert(session, label="A", positive="top-trainer-box")
        await _alert(session, label="B", positive="geeksheaven")
        with patch("app.notify.service.dispatch_event", AsyncMock()) as sent:
            fired = await handle_message(session, MSG)
        assert len(fired) == 2 and sent.await_count == 2


class TestTheEvent:
    def test_it_carries_the_message_and_its_origin(self):
        alert = KeywordAlert(label="TTB", positive="x", channels=["poke-de"])
        event = build_event(alert, MSG)
        assert "Top-Trainer-Box" in event.message
        assert "TCG Cooks" in event.message and "cook-alerts" in event.message
        assert event.url == MSG.url

    def test_it_goes_to_the_configured_channel(self):
        alert = KeywordAlert(label="TTB", positive="x", channels=["poke-de", "tcg-check"])
        assert build_event(alert, MSG).routes == ["channel:poke-de", "channel:tcg-check"]

    def test_without_a_channel_it_is_not_lost(self):
        """An alert nobody receives is the failure that hid the missed drop."""
        alert = KeywordAlert(label="TTB", positive="x", channels=[])
        assert build_event(alert, MSG).routes == ["system:alarm"]
