"""Placeholder ids in instructions get pasted literally — twice now.

"python -m app.watch_debug <ID>" and "... NEUE_ID" were both run verbatim, and
the tool answered with a usage line that did not help find the id. So no
argument lists the watches, and text searches them.
"""

from __future__ import annotations

import pytest

from app.models import Game, Watch


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


class _Reuse:
    def __init__(self, session):
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, *exc):
        return False
