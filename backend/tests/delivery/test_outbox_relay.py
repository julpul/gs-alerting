"""Outbox relay: stale unpublished rows are republished, fresh and locked rows are left alone."""

from __future__ import annotations

import asyncio

import orjson
import pytest

from app.core.outbox import SSE_TOPIC, add_outbox
from app.core.outbox_relay import relay_once, run_relay
from app.core.redis import rkey


@pytest.fixture(autouse=True)
async def quiet_outbox(seeded):
    await seeded.execute("UPDATE outbox SET published_at = now() WHERE published_at IS NULL")
    yield
    await seeded.execute("UPDATE outbox SET published_at = now() WHERE published_at IS NULL")


async def add_row(pool, topic="ai", key="ai.tasks", payload=None, age_s=10):
    async with pool.acquire() as conn, conn.transaction():
        oid = await add_outbox(conn, topic, key, payload or {"n": 1}, priority=2)
        await conn.execute(
            "UPDATE outbox SET created_at = now() - make_interval(secs => $2) WHERE id = $1", oid, age_s
        )
    return oid


async def published(pool, oid):
    return await pool.fetchval("SELECT published_at FROM outbox WHERE id = $1", oid)


async def drain_ai_queue(rabbit) -> list[dict]:
    channel = await rabbit._connection.channel()
    try:
        queue = await channel.get_queue(rabbit.name("ai.tasks"), ensure=False)
        bodies = []
        while (msg := await queue.get(fail=False, timeout=2)) is not None:
            bodies.append(orjson.loads(msg.body))
            await msg.ack()
        return bodies
    finally:
        await channel.close()


async def test_relay_publishes_stale_rows_only(seeded, rabbit):
    await drain_ai_queue(rabbit)
    stale = await add_row(seeded, payload={"n": "stale"}, age_s=10)
    fresh = await add_row(seeded, payload={"n": "fresh"}, age_s=0)
    assert await relay_once(seeded, rabbit) == 1
    assert await published(seeded, stale) is not None and await published(seeded, fresh) is None
    assert await drain_ai_queue(rabbit) == [{"n": "stale"}]
    assert await relay_once(seeded, rabbit) == 0  # nothing left to do
    assert await relay_once(seeded, rabbit, older_than_s=0) == 1  # the fresh one counts once old enough
    assert await drain_ai_queue(rabbit) == [{"n": "fresh"}]


async def test_relay_honours_limit_and_order(seeded, rabbit):
    await drain_ai_queue(rabbit)
    ids = [await add_row(seeded, payload={"n": i}) for i in range(3)]
    assert await relay_once(seeded, rabbit, limit=2) == 2
    assert await published(seeded, ids[2]) is None
    assert await relay_once(seeded, rabbit, limit=2) == 1
    assert [m["n"] for m in await drain_ai_queue(rabbit)] == [0, 1, 2]


async def test_relay_skips_rows_locked_by_another_relay(seeded, rabbit):
    await drain_ai_queue(rabbit)
    locked = await add_row(seeded, payload={"n": "locked"})
    free = await add_row(seeded, payload={"n": "free"})
    async with seeded.acquire() as other, other.transaction():
        await other.fetchval("SELECT id FROM outbox WHERE id = $1 FOR UPDATE", locked)
        assert await asyncio.wait_for(relay_once(seeded, rabbit), 5) == 1
        assert await published(seeded, free) is not None and await published(seeded, locked) is None
    assert await relay_once(seeded, rabbit) == 1
    assert {m["n"] for m in await drain_ai_queue(rabbit)} == {"locked", "free"}


async def test_relay_routes_sse_rows_to_redis(seeded, rabbit, redis):
    pubsub = redis.pubsub()
    await pubsub.subscribe(rkey("sse:relay-test"))
    await pubsub.get_message(timeout=1)
    oid = await add_row(seeded, topic=SSE_TOPIC, key="sse:relay-test", payload={"hello": "world"})
    assert await relay_once(seeded, rabbit) == 1
    message = None
    for _ in range(20):
        message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.5)
        if message:
            break
    await pubsub.aclose()
    assert message and orjson.loads(message["data"]) == {"hello": "world"}
    assert await published(seeded, oid) is not None


async def test_failed_publish_leaves_row_for_the_next_round(seeded):
    class BrokenRabbit:
        async def publish(self, *args, **kwargs):
            raise ConnectionError("broker down")

    oid = await add_row(seeded)
    assert await relay_once(seeded, BrokenRabbit()) == 0
    assert await published(seeded, oid) is None


async def test_run_relay_loop_publishes_and_stops(seeded, rabbit):
    await drain_ai_queue(rabbit)
    oid = await add_row(seeded, payload={"n": "loop"})
    stop = asyncio.Event()
    task = asyncio.create_task(run_relay(seeded, rabbit, stop, interval_s=0.05))
    for _ in range(40):
        if await published(seeded, oid):
            break
        await asyncio.sleep(0.05)
    stop.set()
    await asyncio.wait_for(task, 5)
    assert await published(seeded, oid) is not None
    assert await drain_ai_queue(rabbit) == [{"n": "loop"}]
