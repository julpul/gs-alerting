"""Outbox relay: republishes rows whose after-commit publish never happened (spec 4.4 step 6).

`relay_once` is safe to run in several scheduler replicas: rows are claimed with `FOR UPDATE SKIP LOCKED`
inside one transaction that also marks them published.
"""

from __future__ import annotations

import asyncio
import logging

import asyncpg
import orjson

from app.core.metrics import OUTBOX_UNPUBLISHED
from app.core.outbox import SSE_TOPIC
from app.core.rabbit import Rabbit
from app.core.redis import get_redis, rkey

log = logging.getLogger(__name__)

DEFAULT_OLDER_THAN_S = 5.0
DEFAULT_LIMIT = 500


async def relay_once(
    pool: asyncpg.Pool,
    rabbit: Rabbit,
    older_than_s: float = DEFAULT_OLDER_THAN_S,
    limit: int = DEFAULT_LIMIT,
) -> int:
    """Publish up to `limit` unpublished rows older than `older_than_s`; returns how many were published."""
    published: list[int] = []
    async with pool.acquire() as conn, conn.transaction():
        rows = await conn.fetch(
            "SELECT id, topic, routing_key, priority, payload FROM outbox "
            "WHERE published_at IS NULL AND created_at < now() - make_interval(secs => $1) "
            "ORDER BY id LIMIT $2 FOR UPDATE SKIP LOCKED",
            older_than_s,
            limit,
        )
        for row in rows:
            try:
                if row["topic"] == SSE_TOPIC:
                    await get_redis().publish(rkey(row["routing_key"]), orjson.dumps(row["payload"]).decode())
                else:
                    await rabbit.publish(
                        row["topic"], row["routing_key"], row["payload"], priority=row["priority"]
                    )
                published.append(row["id"])
            except Exception:
                log.exception("outbox relay: publishing row %s failed; will retry", row["id"])
        if published:
            await conn.execute("UPDATE outbox SET published_at = now() WHERE id = ANY($1)", published)
    if published:
        log.info("outbox relay published %d row(s)", len(published))
    return len(published)


async def run_relay(
    pool: asyncpg.Pool,
    rabbit: Rabbit,
    stop: asyncio.Event,
    *,
    interval_s: float = 1.0,
    older_than_s: float = DEFAULT_OLDER_THAN_S,
) -> None:
    """Loop for the scheduler role: relay every `interval_s` and keep the backlog gauge current."""
    while not stop.is_set():
        try:
            await relay_once(pool, rabbit, older_than_s)
            OUTBOX_UNPUBLISHED.set(
                await pool.fetchval("SELECT count(*) FROM outbox WHERE published_at IS NULL")
            )
        except Exception:
            log.exception("outbox relay iteration failed")
        try:
            await asyncio.wait_for(stop.wait(), interval_s)
        except TimeoutError:
            pass
