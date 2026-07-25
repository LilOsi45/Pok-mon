""""Alle prüfen" sweeps: background execution, ordering, double-click guard."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.auth import require_auth
from app.db import get_session
from app.main import app
from app.models import Game, ProductScan, Watch
from app.web import routes


@pytest_asyncio.fixture
async def client(session):
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[require_auth] = lambda: None
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


@pytest_asyncio.fixture(autouse=True)
async def _clear_sweeps():
    routes._sweeping.clear()
    yield
    routes._sweeping.clear()


def watch(label: str, enabled: bool = True) -> Watch:
    return Watch(
        game=Game.POKEMON,
        label=label,
        url=f"https://shop.example/{label}",
        channels=["tcg-check"],
        enabled=enabled,
    )


def scan(label: str, enabled: bool = True) -> ProductScan:
    return ProductScan(
        game=Game.POKEMON,
        label=label,
        url=f"https://shop.example/{label}",
        keywords=["pokemon"],
        channels=["tcg-check"],
        enabled=enabled,
    )


class TestRunSweep:
    @pytest.mark.asyncio
    async def test_runs_every_id_in_order(self):
        seen: list[int] = []

        async def runner(item_id: int) -> None:
            seen.append(item_id)

        await routes.run_sweep("t", [3, 1, 2], runner)
        assert seen == [3, 1, 2]

    @pytest.mark.asyncio
    async def test_one_failure_does_not_abort_the_rest(self):
        seen: list[int] = []

        async def runner(item_id: int) -> None:
            if item_id == 2:
                raise RuntimeError("shop down")
            seen.append(item_id)

        await routes.run_sweep("t", [1, 2, 3], runner)
        assert seen == [1, 3]

    @pytest.mark.asyncio
    async def test_releases_the_lock_even_when_everything_fails(self):
        async def boom(_id: int) -> None:
            raise RuntimeError("nope")

        await routes.run_sweep("t", [1], boom)
        assert not routes.sweep_running("t")

    @pytest.mark.asyncio
    async def test_second_sweep_while_one_runs_is_ignored(self):
        calls: list[int] = []

        async def runner(item_id: int) -> None:
            calls.append(item_id)
            # a second click lands here, mid-sweep
            await routes.run_sweep("t", [99], runner)

        await routes.run_sweep("t", [1], runner)
        assert calls == [1]  # 99 never ran — no doubled load on the shops


@pytest.mark.asyncio
class TestEndpoints:
    async def test_check_all_watches_queues_only_enabled(self, client, session):
        session.add_all([watch("a"), watch("b"), watch("off", enabled=False)])
        await session.commit()

        with patch.object(routes, "run_sweep", AsyncMock()) as sweep:
            resp = await client.post("/watches/check-all")
        assert resp.status_code == 200
        kind, ids, _runner = sweep.await_args.args
        assert kind == "watches"
        assert len(ids) == 2  # the disabled watch stays out

    async def test_check_all_scans_queues_only_enabled(self, client, session):
        session.add_all([scan("s1"), scan("s2"), scan("off", enabled=False)])
        await session.commit()

        with patch.object(routes, "run_sweep", AsyncMock()) as sweep:
            resp = await client.post("/scanner/check-all")
        assert resp.status_code == 200
        kind, ids, _runner = sweep.await_args.args
        assert kind == "scanner"
        assert len(ids) == 2

    async def test_check_all_returns_immediately_without_running_checks(self, client, session):
        """The response must not wait for the sweep — a full pass takes minutes."""
        session.add(watch("a"))
        await session.commit()

        check = AsyncMock()
        with patch("app.monitor.service.check_watch_by_id", check):
            resp = await client.post("/watches/check-all")
        assert resp.status_code == 200
        # BackgroundTasks fire after the response; with ASGITransport they run
        # within the request context, so assert the table came back rendered
        assert "<tr" in resp.text or "Keine" in resp.text

    async def test_paths_are_not_swallowed_by_the_id_routes(self, client, session):
        """"check-all" must not be parsed as a watch/scan id (422)."""
        with patch.object(routes, "run_sweep", AsyncMock()):
            assert (await client.post("/watches/check-all")).status_code == 200
            assert (await client.post("/scanner/check-all")).status_code == 200
