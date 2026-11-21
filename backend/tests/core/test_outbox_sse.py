import asyncio
from uuid import uuid4

import orjson

from app.core.audit import SYSTEM_ACTOR, audit, user_actor
from app.core.ids import uuid7
from app.core.outbox import SSE_TOPIC, add_outbox, publish_outbox_rows
from app.core.rabbit import Rabbit
from app.core.redis import rkey
from app.core.sse import publish_sse, team_channel, user_channel
from app.core.timeline import add_timeline


async def test_outbox_publishes_to_rabbit_and_marks_published(pool, rabbit):
    got: list[dict] = []
    done = asyncio.Event()

    async def handler(msg):
        async with msg.process():
            got.append(orjson.loads(msg.body))
            done.set()

    consumer = Rabbit()
    await consumer.connect()
    try:
        async with pool.acquire() as conn, conn.transaction():
            oid = await add_outbox(conn, "ai", "ai.tasks", {"alertId": "x"}, priority=3)
        assert await pool.fetchval("SELECT published_at FROM outbox WHERE id = $1", oid) is None
        await consumer.consume("ai.tasks", handler)
        await publish_outbox_rows(pool, rabbit, [oid])
        await asyncio.wait_for(done.wait(), 10)
    finally:
        await consumer.close()
    assert got == [{"alertId": "x"}]
    assert await pool.fetchval("SELECT published_at FROM outbox WHERE id = $1", oid) is not None
    await publish_outbox_rows(pool, rabbit, [oid])  # already published: no-op
    await publish_outbox_rows(pool, rabbit, [])


async def test_outbox_sse_topic_goes_to_redis(pool, rabbit, redis):
    pubsub = redis.pubsub()
    await pubsub.subscribe(rkey("sse:test"))
    await pubsub.get_message(timeout=1)
    async with pool.acquire() as conn, conn.transaction():
        oid = await add_outbox(conn, SSE_TOPIC, "sse:test", {"hello": 1})
    await publish_outbox_rows(pool, rabbit, [oid])
    msg = None
    for _ in range(20):
        msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.5)
        if msg:
            break
    await pubsub.aclose()
    assert msg and orjson.loads(msg["data"]) == {"hello": 1}


async def test_publish_sse_reaches_team_and_user_channels(redis):
    team, user = uuid4(), uuid4()
    pubsub = redis.pubsub()
    await pubsub.subscribe(team_channel(team), user_channel(user))
    await pubsub.get_message(timeout=1)
    await publish_sse("alert.created", {"id": "A-1"}, team_ids=[team], user_ids=[user])
    seen = []
    for _ in range(40):
        msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.25)
        if msg:
            seen.append((msg["channel"], orjson.loads(msg["data"])))
        if len(seen) == 2:
            break
    await pubsub.aclose()
    assert {c for c, _ in seen} == {team_channel(team), user_channel(user)}
    assert all(p["type"] == "alert.created" and p["data"] == {"id": "A-1"} and "at" in p for _, p in seen)


async def test_timeline_and_audit_rows(pool):
    alert_id = uuid7()
    async with pool.acquire() as conn:
        await add_timeline(conn, alert_id, "created", "Alert created", data={"k": 1})
        await add_timeline(conn, alert_id, "ack", "Acked", actor=user_actor(uuid4(), "Anna"))
        await audit(conn, SYSTEM_ACTOR, "rule.update", "rule", "r-1", {"v": 2})
    rows = await pool.fetch(
        "SELECT type, actor, data FROM timeline WHERE alert_id = $1 ORDER BY at", alert_id
    )
    assert [r["type"] for r in rows] == ["created", "ack"]
    assert rows[0]["actor"]["type"] == "system" and rows[0]["data"] == {"k": 1}
    assert rows[1]["actor"]["name"] == "Anna"
    log = await pool.fetchrow("SELECT actor, data FROM audit_log WHERE target_id = 'r-1'")
    assert log["actor"]["type"] == "system" and log["data"] == {"v": 2}
