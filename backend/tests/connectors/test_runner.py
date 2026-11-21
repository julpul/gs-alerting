"""The connector role: source scan, restart on config change, end to end into a real worker."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio

from app.connectors import runner
from app.connectors.base import Connector
from app.connectors.runner import Supervisor, load_sources
from tests.connectors.conftest import eventually
from tests.pipeline.conftest import alerts_for


@pytest_asyncio.fixture
async def isolated_sources(seeded, rabbit) -> AsyncIterator[None]:
    """The seeded `trade-gateway` source points at the shared `vistula.trade-gateway` queue of a dev broker;
    during the test it consumes a throw-away prefixed queue instead."""
    original = await seeded.fetchval("SELECT config FROM sources WHERE name = 'trade-gateway'")
    await seeded.execute(
        "UPDATE sources SET config = $1 WHERE name = 'trade-gateway'",
        {**original, "queue": f"{rabbit.prefix}unused.trade-gateway"},
    )
    yield
    await seeded.execute("UPDATE sources SET config = $1 WHERE name = 'trade-gateway'", original)


class FakeConnector(Connector):
    protocol = "rabbitmq"
    started: list[str] = []

    async def run(self, publish) -> None:
        self.started.append(self.source.config.get("queue", ""))
        await asyncio.Event().wait()


@pytest.fixture
def fake_connectors(monkeypatch):
    FakeConnector.started = []
    monkeypatch.setitem(runner.CONNECTORS, "rabbitmq", FakeConnector)
    return FakeConnector


async def test_load_sources_reads_rabbitmq_sources_with_their_config(
    seeded, make_preset_source, isolated_sources
):
    mine = await make_preset_source(type="rabbitmq", config={"queue": "q1", "url": "amqp://x"})
    await make_preset_source(type="webhook")
    sources = await load_sources(seeded)
    spec = sources[str(mine["id"])]
    assert spec.config == {"queue": "q1", "url": "amqp://x"} and spec.type == "rabbitmq"
    assert {s.type for s in sources.values()} == {"rabbitmq"}
    assert "trade-gateway" in {s.name for s in sources.values()}


async def test_supervisor_starts_restarts_and_stops_connectors(
    seeded, rabbit, make_preset_source, fake_connectors, isolated_sources
):
    supervisor = Supervisor(seeded, rabbit)
    mine = await make_preset_source(type="rabbitmq", config={"queue": "first"})
    key = str(mine["id"])
    try:
        await supervisor.sync()
        await asyncio.sleep(0.05)
        assert key in supervisor.running and fake_connectors.started.count("first") == 1

        await supervisor.sync()  # unchanged: no restart
        await asyncio.sleep(0.05)
        assert fake_connectors.started.count("first") == 1

        await seeded.execute("UPDATE sources SET config = $2 WHERE id = $1", mine["id"], {"queue": "second"})
        await supervisor.sync()
        await asyncio.sleep(0.05)
        assert fake_connectors.started.count("second") == 1 and key in supervisor.running

        await seeded.execute("DELETE FROM sources WHERE id = $1", mine["id"])
        await supervisor.sync()
        assert key not in supervisor.running
    finally:
        await supervisor.stop_all()
    assert supervisor.running == set()


async def test_finished_connector_is_started_again_on_next_scan(
    seeded, rabbit, make_preset_source, monkeypatch, isolated_sources
):
    runs = []

    class OneShot(Connector):
        protocol = "rabbitmq"

        async def run(self, publish) -> None:
            runs.append(1)

    monkeypatch.setitem(runner.CONNECTORS, "rabbitmq", OneShot)
    supervisor = Supervisor(seeded, rabbit)
    mine = await make_preset_source(type="rabbitmq", config={"queue": "q"})
    try:
        await supervisor.sync()
        await asyncio.sleep(0.05)
        before = len(runs)
        await supervisor.sync()
        await asyncio.sleep(0.05)
        assert len(runs) > before
    finally:
        await supervisor.stop_all()
        await seeded.execute("DELETE FROM sources WHERE id = $1", mine["id"])


async def test_run_loop_survives_a_failing_scan(seeded, rabbit, monkeypatch):
    calls = 0

    async def broken(pool):
        nonlocal calls
        calls += 1
        raise ConnectionError("db down")

    monkeypatch.setattr(runner, "load_sources", broken)
    stop = asyncio.Event()
    supervisor = Supervisor(seeded, rabbit)
    task = asyncio.create_task(supervisor.run(stop, rescan_seconds=0.05))
    await eventually(lambda: _flag(calls >= 3))
    stop.set()
    await asyncio.wait_for(task, 2)


async def test_role_entry_point_starts_and_stops_cleanly(seeded, isolated_sources, monkeypatch):
    monkeypatch.setattr(runner, "RESCAN_SECONDS", 0.1)
    stop = asyncio.Event()
    task = asyncio.create_task(runner.run(stop))
    await asyncio.sleep(0.5)
    assert not task.done()
    stop.set()
    await asyncio.wait_for(task, 10)


async def test_stop_during_startup_exits_quickly(seeded):
    stop = asyncio.Event()
    task = asyncio.create_task(runner.run(stop))
    await asyncio.sleep(0.01)
    stop.set()
    await asyncio.wait_for(task, 5)


# ---- the whole chain: customer queue -> connector role -> events.in -> worker -> alert ------------------


RULE = {
    "priority": "error",
    "match": {
        "op": "and",
        "conditions": [{"field": "meta.schema", "operator": "eq", "value": "trade.rejected.v2"}],
    },
    "presentation": {"titleTemplate": "Rejected {{payload.currency}} {{payload.notional}}"},
    "dedup": {"fields": ["payload.tradeId"]},
}


async def test_customer_queue_message_creates_an_alert_through_the_connector_role(
    seeded, rabbit, worker_env, make_preset_source, make_rule, customer, planner, isolated_sources
):
    rule = await make_rule(RULE, team="team-payments")
    queue = await customer.new_queue()
    source = await make_preset_source(type="rabbitmq", config={"queue": queue})
    supervisor = Supervisor(seeded, rabbit)
    try:
        await supervisor.sync()
        message = {
            "meta": {"schema": "trade.rejected.v2", "producer": "trade-gateway", "ts": 1790155980000},
            "payload": {"tradeId": "T-42", "desk": "rates", "notional": 3500000, "currency": "EUR"},
        }
        await customer.send(queue, message)
        await customer.send(queue, message)  # a duplicate trade: same dedup fingerprint
        (alert,) = await eventually(lambda: _one(alerts_for(seeded, rule)))
        await eventually(lambda: _flag(_count(seeded, rule, 2)))
        assert alert["title"] == "Rejected EUR 3500000" and alert["source_id"] == source["id"]
        assert planner.reasons == ["created"]
        assert await customer.depth(queue) == 0
    finally:
        await supervisor.stop_all()


async def _one(coro):
    rows = await coro
    return rows if len(rows) == 1 else None


async def _count(pool, rule, expected: int) -> bool:
    rows = await alerts_for(pool, rule)
    return len(rows) == 1 and rows[0]["count"] == expected


async def _flag(value) -> bool:
    return await value if asyncio.iscoroutine(value) else bool(value)
