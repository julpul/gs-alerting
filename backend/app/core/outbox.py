"""Transactional outbox: rows are written in the caller's TX and published to RabbitMQ after commit.

`topic` is the base exchange name (e.g. `deliveries`, `ai`); the special topic `sse` publishes the
payload to the Redis channel named by `routing_key`. A relay (scheduler) republishes rows that stayed
unpublished.
"""

from __future__ import annotations

import logging
from typing import Any

import asyncpg
import orjson

from app.core.rabbit import Rabbit
from app.core.redis import get_redis, rkey

log = logging.getLogger(__name__)

SSE_TOPIC = "sse"


async def add_outbox(
    conn: asyncpg.Connection, topic: str, routing_key: str, payload: dict[str, Any], priority: int = 0
) -> int:
    return await conn.fetchval(
        "INSERT INTO outbox (topic, routing_key, priority, payload) VALUES ($1, $2, $3, $4) RETURNING id",
        topic,
        routing_key,
        priority,
        payload,
    )


async def publish_outbox_rows(pool: asyncpg.Pool, rabbit: Rabbit, ids: list[int]) -> None:
    """Publish the given rows (with confirm) and mark them published. Failures leave rows for the relay."""
    if not ids:
        return
    rows = await pool.fetch(
        "SELECT id, topic, routing_key, priority, payload FROM outbox "
        "WHERE id = ANY($1) AND published_at IS NULL ORDER BY id",
        ids,
    )
    done: list[int] = []
    for row in rows:
        try:
            if row["topic"] == SSE_TOPIC:
                await get_redis().publish(rkey(row["routing_key"]), orjson.dumps(row["payload"]).decode())
            else:
                await rabbit.publish(
                    row["topic"], row["routing_key"], row["payload"], priority=row["priority"]
                )
            done.append(row["id"])
        except Exception:
            log.exception("outbox publish failed for row %s; relay will retry", row["id"])
    if done:
        await pool.execute("UPDATE outbox SET published_at = now() WHERE id = ANY($1)", done)
