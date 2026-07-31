"""Placeholder ids in instructions get pasted literally — twice now.

"python -m app.watch_debug <ID>" and "... NEUE_ID" were both run verbatim, and
the tool answered with a usage line that did not help find the id. So no
argument lists the watches, and text searches them.
"""

from __future__ import annotations

import pytest

from app.models import Game, Watch

# As long as the real thing: the reason must survive to the end of the line.
LONG_REASON = (
    "brightdata returned an empty body for https://www.pokemoncenter.com/de-de "
    "(x-brd-error=Timeout, x-brd-error-code=timeout, x-brd-status-code=502)"
)


def _watch(**kw) -> Watch:
    return Watch(
        game=Game.POKEMON,
        label=kw.pop("label", "W"),
        url=kw.pop("url", "https://shop.de/products/x"),
        enabled=kw.pop("enabled", True),
        **kw,
    )


@pytest.mark.asyncio
class TestListing:
    async def _seed(self, session):
        session.add_all(
            [
                _watch(
                    label="Pokémon Center Queue-Alarm DE", url="https://www.pokemoncenter.com/de-de"
                ),
                _watch(label="Geeksheaven TTB", url="https://geeksheaven.de/products/ttb"),
                _watch(label="Alte Watch", enabled=False),
            ]
        )
        await session.commit()

    async def test_no_argument_lists_every_watch(self, session, capsys, monkeypatch):
        from app import watch_debug

        await self._seed(session)
        monkeypatch.setattr("app.watch_debug.get_sessionmaker", lambda: lambda: _Reuse(session))

        await watch_debug.list_watches()

        out = capsys.readouterr().out
        assert "Pokémon Center Queue-Alarm DE" in out
        assert "Geeksheaven TTB" in out

    async def test_text_searches_by_label(self, session, capsys, monkeypatch):
        from app import watch_debug

        await self._seed(session)
        monkeypatch.setattr("app.watch_debug.get_sessionmaker", lambda: lambda: _Reuse(session))

        await watch_debug.list_watches("queue")

        out = capsys.readouterr().out
        assert "Queue-Alarm" in out
        assert "Geeksheaven" not in out

    async def test_text_searches_by_url(self, session, capsys, monkeypatch):
        from app import watch_debug

        await self._seed(session)
        monkeypatch.setattr("app.watch_debug.get_sessionmaker", lambda: lambda: _Reuse(session))

        await watch_debug.list_watches("pokemoncenter")

        assert "Queue-Alarm" in capsys.readouterr().out

    async def test_disabled_watches_are_marked(self, session, capsys, monkeypatch):
        from app import watch_debug

        await self._seed(session)
        monkeypatch.setattr("app.watch_debug.get_sessionmaker", lambda: lambda: _Reuse(session))

        await watch_debug.list_watches("alte")

        assert "(aus)" in capsys.readouterr().out

    async def test_no_match_says_so(self, session, capsys, monkeypatch):
        from app import watch_debug

        await self._seed(session)
        monkeypatch.setattr("app.watch_debug.get_sessionmaker", lambda: lambda: _Reuse(session))

        await watch_debug.list_watches("gibtsnicht")

        assert "Keine passende" in capsys.readouterr().out


@pytest.mark.asyncio
class TestRepeatedCheck:
    """One green check proves the route can work, not that it works.

    The Pokémon Center unlocker was measured turning us away on roughly one
    attempt in three, so for an alarm the success *rate* is the question — and
    a rate has to be counted, not sampled once and declared fine.
    """

    async def _run(self, session, monkeypatch, capsys, adapter, times=3):
        from app import watch_debug

        watch = _watch(label="Queue-Alarm", url="https://www.pokemoncenter.com/de-de")
        session.add(watch)
        await session.commit()
        monkeypatch.setattr("app.watch_debug.get_sessionmaker", lambda: lambda: _Reuse(session))
        monkeypatch.setattr(watch_debug, "resolve_adapter", lambda *a, **kw: adapter)
        monkeypatch.setattr(watch_debug, "REPEAT_GAP_SECONDS", 0)

        await watch_debug.repeat_watch(watch.id, times)
        return capsys.readouterr().out

    async def test_a_flaky_route_is_reported_as_a_rate(self, session, monkeypatch, capsys):
        out = await self._run(session, monkeypatch, capsys, _Flaky(fail_on={2}))

        assert "2 von 3 Versuchen erfolgreich" in out
        assert "FEHLER" in out
        assert "615031 Zeichen" in out or "615031" in out

    async def test_a_stable_route_says_so(self, session, monkeypatch, capsys):
        out = await self._run(session, monkeypatch, capsys, _Flaky(fail_on=set()))

        assert "3 von 3" in out
        assert "stabil" in out

    async def test_a_dead_route_is_not_dressed_up(self, session, monkeypatch, capsys):
        out = await self._run(session, monkeypatch, capsys, _Flaky(fail_on={1, 2, 3}))

        assert "0 von 3" in out
        assert "stimmt etwas nicht" in out

    async def test_the_failure_reason_is_not_cut_off(self, session, monkeypatch, capsys):
        """A sliced message hid "Timeout" behind "x-brd-error=Tim" and sent the
        diagnosis after a block that was never there."""
        out = await self._run(session, monkeypatch, capsys, _Flaky(fail_on={1}), times=1)

        assert LONG_REASON in out


class _Flaky:
    """An adapter that fails on the given attempt numbers."""

    def __init__(self, fail_on: set[int]):
        self.fail_on = fail_on
        self.calls = 0

    async def fetch(self, url: str):
        from app.monitor.base import PageResult

        self.calls += 1
        if self.calls in self.fail_on:
            raise RuntimeError(LONG_REASON)
        return PageResult(url=url, final_url=url, status_code=200, text="x" * 615031)

    def parse(self, page):
        from app.models import StockStatus
        from app.monitor.base import StockResult

        return StockResult(status=StockStatus.OUT_OF_STOCK, note="idle")


class _Reuse:
    def __init__(self, session):
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, *exc):
        return False


@pytest.mark.asyncio
class TestScannerListing:
    """Same trap as the watches: "python -m app.scan_debug <scanner-id>" is not
    an answer when you know the shop's name and not its number."""

    async def _seed(self, session):
        from app.models import Game, ProductScan

        session.add_all(
            [
                ProductScan(
                    game=Game.POKEMON,
                    label="Fantasywelt Neuheiten",
                    url="https://www.fantasywelt.de/pokemon-neu",
                ),
                ProductScan(
                    game=Game.POKEMON,
                    label="Games Island",
                    url="https://games-island.eu/neu",
                ),
            ]
        )
        await session.commit()

    async def test_a_shop_name_finds_the_scanner(self, session, capsys, monkeypatch):
        from app import scan_debug

        await self._seed(session)
        monkeypatch.setattr("app.scan_debug.get_sessionmaker", lambda: lambda: _Reuse(session))

        await scan_debug.list_scans("fantasywelt")

        out = capsys.readouterr().out
        assert "Fantasywelt Neuheiten" in out
        assert "Games Island" not in out

    async def test_no_argument_lists_all(self, session, capsys, monkeypatch):
        from app import scan_debug

        await self._seed(session)
        monkeypatch.setattr("app.scan_debug.get_sessionmaker", lambda: lambda: _Reuse(session))

        await scan_debug.list_scans()

        assert "Games Island" in capsys.readouterr().out

    async def test_an_unknown_name_says_so(self, session, capsys, monkeypatch):
        from app import scan_debug

        await self._seed(session)
        monkeypatch.setattr("app.scan_debug.get_sessionmaker", lambda: lambda: _Reuse(session))

        await scan_debug.list_scans("gibtsnicht")

        assert "Kein passender Scanner" in capsys.readouterr().out
