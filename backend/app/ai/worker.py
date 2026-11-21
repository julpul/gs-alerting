"""AI worker role: consumes `ai.tasks` (never on the hot path; spec 13.1).

Task payloads: `{kind: "suggest_fix", alert_id, team_id, domain_id, knowledge_key, flags}` and
`{kind: "index_resolution", resolution_id | alert_id}`. A sweep also indexes resolutions that have no
knowledge document yet (seeded history, tasks lost while the worker was down).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from typing import Any
from uuid import UUID

import asyncpg
import orjson
from aio_pika.abc import AbstractIncomingMessage
from prometheus_client import start_http_server

from app.ai.embeddings import warm_up
from app.ai.suggest import generate_suggestion
from app.core.db import create_pool
from app.core.rabbit import AI_QUEUE, Rabbit
from app.knowledge.indexer import index_resolution, on_resolved, sweep_unindexed

log = logging.getLogger(__name__)

PREFETCH = 4
NACK_DELAY_S = 1.0
SWEEP_INTERVAL_S = 15.0


async def handle_task(pool: asyncpg.Pool, payload: dict[str, Any]) -> str:
    """Process one task; returns what happened (`suggested|skipped|indexed|ignored`)."""
    kind = payload.get("kind")
    async with pool.acquire() as conn:
        if kind == "suggest_fix":
            created = await generate_suggestion(conn, UUID(payload["alert_id"]))
            return "suggested" if created else "skipped"
        if kind == "index_resolution":
            if payload.get("resolution_id"):
                done = int(await index_resolution(conn, UUID(payload["resolution_id"])))
            else:
                done = await on_resolved(conn, UUID(payload["alert_id"]))
            return "indexed" if done else "skipped"
    log.warning("unknown ai task kind %r", kind)
    return "ignored"


class AIWorker:
    def __init__(self, pool: asyncpg.Pool, rabbit: Rabbit) -> None:
        self.pool = pool
        self.rabbit = rabbit

    async def start(self) -> None:
        await self.rabbit.consume(AI_QUEUE, self._on_message, prefetch=PREFETCH)

    async def _on_message(self, msg: AbstractIncomingMessage) -> None:
        try:
            payload = orjson.loads(msg.body)
            if not isinstance(payload, dict):
                raise ValueError("task must be an object")
        except (ValueError, orjson.JSONDecodeError):
            log.error("dropping malformed ai task: %r", msg.body[:200])
            await msg.ack()
            return
        try:
            await handle_task(self.pool, payload)
        except Exception:
            if msg.redelivered:
                log.exception("ai task failed twice, dropping: %s", payload.get("kind"))
                await msg.ack()
                return
            log.exception("ai task failed, requeueing once: %s", payload.get("kind"))
            await asyncio.sleep(NACK_DELAY_S)
            await msg.nack(requeue=True)
            return
        await msg.ack()


async def _sweep_loop(pool: asyncpg.Pool, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            async with pool.acquire() as conn:
                indexed = await sweep_unindexed(conn)
            if indexed:
                log.info("indexed %d resolution notes", indexed)
        except Exception:
            log.warning("resolution sweep failed", exc_info=True)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), SWEEP_INTERVAL_S)


async def run(stop: asyncio.Event) -> None:
    try:
        start_http_server(int(os.environ.get("METRICS_PORT", "9101")))
    except OSError:
        log.info("metrics port already in use; another role in this process serves /metrics")
    pool = await create_pool()
    rabbit = Rabbit()
    await rabbit.connect()
    await rabbit.declare_topology()
    warmup = asyncio.create_task(warm_up())
    await AIWorker(pool, rabbit).start()
    sweeper = asyncio.create_task(_sweep_loop(pool, stop))
    log.info("ai worker consuming %s", AI_QUEUE)
    try:
        await stop.wait()
    finally:
        sweeper.cancel()
        warmup.cancel()
        await rabbit.close()
        await pool.close()
