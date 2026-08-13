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


class TestHeadline:
    """The ping has to say which keyword fired and under which setting — with a
    dozen keywords in one setting, otherwise you work it out from the product
    name."""

    def _alert(self, label="Erste Partner Illustrations-Kollektion Serie 3"):
        return KeywordAlert(label=label, positive="erste-partner", ranges="min15€;max30€")

    def test_it_names_keyword_setting_and_filter(self):
        from app.keyword_match import parse_rules
        from app.keyword_pinger import headline

        alert = self._alert()
        rules = parse_rules(alert.positive, alert.negative or "", alert.ranges)
        line = headline(alert, rules, "erste-partner", mention="@LilOSi45")

        assert line.startswith("@LilOSi45 dein Keyword wurde gepingt:")
        assert "**(erste-partner)**" in line
        assert 'in Setting: **"Erste Partner Illustrations-Kollektion Serie 3"**' in line
        assert "mit Filter: **min15€; max30€**" in line

    def test_without_a_filter_that_part_is_left_out(self):
        from app.keyword_match import parse_rules
        from app.keyword_pinger import headline

        alert = KeywordAlert(label="Nur Wort", positive="x", ranges="")
        line = headline(alert, parse_rules("x", "", ""), "x")
        assert "mit Filter" not in line


@pytest.mark.asyncio
class TestEmbedsAndBlocking:
    """Monitor bots put the product, price and shop inside an embed and leave
    the message body empty; matching only the body would miss exactly the
    messages worth having."""

    EMBED_ONLY = IncomingMessage(
        content="",
        channel_id="4242",
        embed_text="Pokemon Erste-Partner Illustration Kollektion Serie 2\nPrice €36.75\nIn Stock Yes",
        links=("https://www.comicplanet.de/erste-partner-serie-2",),
    )

    async def test_a_hit_inside_an_embed_still_fires(self, session):
        await _alert(session, positive="erste-partner")
        with patch("app.notify.service.dispatch_event", AsyncMock()) as sent:
            assert await handle_message(session, self.EMBED_ONLY)
        assert sent.await_count == 1

    async def test_a_blocked_link_stays_quiet(self, session):
        from app.keyword_pinger import block_url

        await _alert(session, positive="erste-partner")
        await block_url(session, self.EMBED_ONLY.links[0])

        with patch("app.notify.service.dispatch_event", AsyncMock()) as sent:
            assert await handle_message(session, self.EMBED_ONLY) == []
        assert sent.await_count == 0

    async def test_blocking_the_same_link_twice_is_reported(self, session):
        from app.keyword_pinger import block_url

        assert await block_url(session, "https://shop.de/x") is True
        assert await block_url(session, "https://shop.de/x") is False

    async def test_another_link_is_unaffected(self, session):
        from app.keyword_pinger import block_url

        await _alert(session, positive="erste-partner")
        await block_url(session, "https://anderer-shop.de/y")

        with patch("app.notify.service.dispatch_event", AsyncMock()) as sent:
            assert await handle_message(session, self.EMBED_ONLY)
        assert sent.await_count == 1
