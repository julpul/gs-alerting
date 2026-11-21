import asyncio

import orjson
import pytest

from app.core.rabbit import Rabbit, retry_headers


@pytest.fixture
async def consumer_rabbit(rabbit):
    """A separate connection per test so consumers never leak into other tests."""
    r = Rabbit()
    await r.connect()
    yield r
    await r.close()


async def _collect(r: Rabbit, queue: str, n: int, wait: float = 10.0) -> list[dict]:
    got: list[dict] = []
    done = asyncio.Event()

    async def handler(msg):
        async with msg.process():
            got.append({"body": orjson.loads(msg.body), "priority": msg.priority, "headers": msg.headers})
            if len(got) >= n:
                done.set()

    await r.consume(queue, handler, prefetch=50)
    await asyncio.wait_for(done.wait(), wait)
    return got


async def test_topology_names_use_prefix(rabbit, session_env):
    prefix = session_env["mq_prefix"]
    assert rabbit.name("events.in") == f"{prefix}events.in"
    for base in ("events.in", "deliveries.sms", "deliveries.retry.30s", "deliveries.dlq", "ai.tasks"):
        q = await rabbit._channel().get_queue(rabbit.name(base), ensure=True)
        assert q.name == f"{prefix}{base}"


async def test_publish_with_confirm_and_consume(rabbit, consumer_rabbit):
    await rabbit.publish("events", "events.in", {"hello": "world"}, priority=6, headers={"k": "v"})
    got = await _collect(consumer_rabbit, "events.in", 1)
    assert got[0]["body"] == {"hello": "world"}
    assert got[0]["priority"] == 6
    assert got[0]["headers"]["k"] == "v"


async def test_publish_batch(rabbit, consumer_rabbit):
    await rabbit.publish_batch("ai", "ai.tasks", [{"i": i} for i in range(100)])
    got = await _collect(consumer_rabbit, "ai.tasks", 100)
    assert sorted(g["body"]["i"] for g in got) == list(range(100))


async def test_publish_raw_bytes_and_channel_routing(rabbit, consumer_rabbit):
    await rabbit.publish("deliveries", "sms", b'{"to": "+48"}')
    got = await _collect(consumer_rabbit, "deliveries.sms", 1)
    assert got[0]["body"] == {"to": "+48"}


async def test_retry_queue_returns_message_to_channel_queue(rabbit, consumer_rabbit):
    await rabbit.publish("deliveries.retry", "5s", {"attempt": 1}, headers=retry_headers("email"))
    got = await _collect(consumer_rabbit, "deliveries.email", 1, wait=15)
    assert got[0]["body"] == {"attempt": 1}


async def test_publish_before_connect_fails():
    r = Rabbit()
    assert not r.is_connected
    with pytest.raises(RuntimeError):
        await r.publish("events", "events.in", {})
    with pytest.raises(RuntimeError):
        await r.consume("events.in", lambda m: None)
