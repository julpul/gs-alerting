"""Fixtures for the connector / preset tests: preset + rabbitmq sources, a tap on `events.in`."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import aio_pika
import orjson
import pytest_asyncio

from app.core.ids import stable_id
from app.core.rabbit import Rabbit
from app.core.security import hash_token
from app.pipeline.presets.mappings import preset_mapping
from tests.pipeline.conftest import (  # noqa: F401  (fixtures used by the tests in this package)
    eventually,
    index,
    make_rule,
    make_source,
    outcomes,
    planner,
    purge_events_queue,
    worker_env,
)


@pytest_asyncio.fixture
async def make_preset_source(seeded) -> AsyncIterator[Callable[..., Awaitable[dict]]]:
    """Insert a source row: `type` webhook|rabbitmq, optional preset (mapping stored like the API does)."""
    pool = seeded
    created: list[UUID] = []

    async def factory(
        preset: str | None = None,
        *,
        type: str = "webhook",
        config: dict | None = None,
        team: str = "team-payments",
        default_severity: str = "warning",
    ) -> dict:
        key = f"test-key-{uuid4().hex}"
        team_id = stable_id("team", team)
        domain_id = await pool.fetchval(
            "SELECT domain_id FROM team_domains WHERE team_id = $1 ORDER BY domain_id LIMIT 1", team_id
        )
        source_id = uuid4()
        created.append(source_id)
        name = f"src-{source_id.hex[:8]}"
        mapping = preset_mapping(preset)
        await pool.execute(
            "INSERT INTO sources (id, name, team_id, domain_id, type, preset, key_prefix, key_hash, "
            "config, mapping, mapping_status, default_severity) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)",
            source_id,
            name,
            team_id,
            domain_id,
            type,
            preset,
            key[:9],
            hash_token(key),
            config or {},
            mapping,
            mapping["status"] if mapping else None,
            default_severity,
        )
        return {"id": source_id, "name": name, "key": key, "team_id": team_id, "domain_id": domain_id}

    yield factory
    await pool.execute("DELETE FROM sources WHERE id = ANY($1)", created)


class Tap:
    """Consumes `events.in` and collects the envelopes of one source."""

    def __init__(self, rabbit: Rabbit) -> None:
        self.rabbit = rabbit
        self.items: list[tuple[dict, int]] = []

    async def handler(self, message: aio_pika.abc.AbstractIncomingMessage) -> None:
        async with message.process():
            self.items.append((orjson.loads(message.body), message.priority))

    def of(self, source: dict) -> list[dict]:
        return [e for e, _ in self.items if e.get("source_id") == str(source["id"])]

    async def wait_for(self, n: int, source: dict, timeout: float = 30) -> list[dict]:  # noqa: ASYNC109
        async def enough() -> bool:
            return len(self.of(source)) >= n

        await eventually(enough, timeout=timeout)
        return self.of(source)


@pytest_asyncio.fixture
async def tap(rabbit) -> AsyncIterator[Tap]:
    await purge_events_queue(rabbit)
    consumer = Rabbit()
    await consumer.connect()
    t = Tap(consumer)
    await consumer.consume("events.in", t.handler, prefetch=200)
    yield t
    await consumer.close()
    await purge_events_queue(rabbit)


@pytest_asyncio.fixture
async def customer(rabbit) -> AsyncIterator[SimpleNamespace]:
    """A "customer" broker connection (the test broker itself) with helpers to publish/inspect queues."""
    connection = await aio_pika.connect(rabbit.url)
    channel = await connection.channel()
    queues: list[str] = []

    async def new_queue(declare: bool = True) -> str:
        name = f"{rabbit.prefix}cust.{uuid4().hex[:10]}"
        queues.append(name)
        if declare:
            await channel.declare_queue(name, durable=True)
        return name

    async def send(queue: str, body: bytes | dict, **props: Any) -> None:
        payload = body if isinstance(body, bytes) else orjson.dumps(body)
        await channel.default_exchange.publish(aio_pika.Message(payload, **props), routing_key=queue)

    async def depth(queue: str) -> int:
        probe = await aio_pika.connect(rabbit.url)
        try:
            declared = await (await probe.channel()).declare_queue(queue, passive=True)
            return declared.declaration_result.message_count
        finally:
            await probe.close()

    try:
        yield SimpleNamespace(new_queue=new_queue, send=send, depth=depth, url=rabbit.url, channel=channel)
    finally:
        for name in queues:
            try:
                await (await connection.channel()).queue_delete(name)
            except Exception:
                pass
        await connection.close()


async def wait_until(condition: Callable[[], Awaitable[Any]], timeout: float = 20) -> Any:  # noqa: ASYNC109
    """`eventually` under a name that reads well in tests."""
    return await eventually(condition, timeout=timeout)


def settle() -> Awaitable[None]:
    return asyncio.sleep(0.2)


@pytest_asyncio.fixture(autouse=True)
async def purge_side_queues(rabbit) -> AsyncIterator[None]:
    """Alerts created here enqueue AI/delivery work nobody consumes; other modules read those queues."""
    yield
    channel = rabbit._channel()
    for base in ("ai.tasks", "deliveries.ui", "deliveries.email", "deliveries.sms", "deliveries.phone"):
        queue = await channel.get_queue(rabbit.name(base), ensure=False)
        await queue.purge()
