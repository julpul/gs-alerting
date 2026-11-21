"""Scheduler role (spec 10.2): a 1 s loop over short, lock-skipping jobs.

Every second: escalation, renotify, approval expiry, outbox relay. Slower jobs: shift materialisation
(60 s) and digests (`DIGEST_INTERVAL_SECONDS`, default 300 s). Each job is isolated: a failing job is
logged and retried on the next tick. Several schedulers may run side by side (rows are claimed with
`FOR UPDATE SKIP LOCKED`).
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Awaitable, Callable

import asyncpg

from app.core.db import create_pool
from app.core.metrics import OUTBOX_UNPUBLISHED
from app.core.outbox_relay import relay_once
from app.core.rabbit import Rabbit
from app.core.redis import close_redis
from app.escalation import jobs

log = logging.getLogger(__name__)

TICK_SECONDS = 1.0
SHIFT_INTERVAL_SECONDS = 60.0
DEFAULT_DIGEST_INTERVAL_SECONDS = 300.0
GAUGE_INTERVAL_SECONDS = 5.0


def digest_interval() -> float:
    try:
        return float(os.environ.get("DIGEST_INTERVAL_SECONDS", DEFAULT_DIGEST_INTERVAL_SECONDS))
    except ValueError:
        return DEFAULT_DIGEST_INTERVAL_SECONDS


class Scheduler:
    def __init__(self, pool: asyncpg.Pool, rabbit: Rabbit) -> None:
        self.pool = pool
        self.rabbit = rabbit
        self._due: dict[str, float] = {}

    def _is_due(self, name: str, interval: float) -> bool:
        now = time.monotonic()
        if now < self._due.get(name, 0.0):
            return False
        self._due[name] = now + interval
        return True

    async def _safe(self, name: str, job: Callable[[], Awaitable[object]]) -> None:
        try:
            await job()
        except Exception:
            log.exception("scheduler job %s failed", name)

    async def tick(self) -> None:
        """One pass over the per-second jobs (and the slower ones when they are due)."""
        await self._safe("escalate", lambda: jobs.escalate_due(self.pool, self.rabbit))
        await self._safe("renotify", lambda: jobs.renotify_due(self.pool, self.rabbit))
        await self._safe("approvals", lambda: jobs.expire_approvals(self.pool))
        await self._safe("relay", lambda: relay_once(self.pool, self.rabbit))
        if self._is_due("gauge", GAUGE_INTERVAL_SECONDS):
            await self._safe("gauge", self._update_gauge)
        if self._is_due("shifts", SHIFT_INTERVAL_SECONDS):
            await self._safe("shifts", lambda: jobs.materialise_shifts(self.pool))
        if self._is_due("digests", digest_interval()):
            await self._safe("digests", lambda: jobs.send_digests(self.pool))

    async def _update_gauge(self) -> None:
        OUTBOX_UNPUBLISHED.set(
            await self.pool.fetchval("SELECT count(*) FROM outbox WHERE published_at IS NULL")
        )

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            await self.tick()
            try:
                await asyncio.wait_for(stop.wait(), TICK_SECONDS)
            except TimeoutError:
                pass


async def run(stop: asyncio.Event) -> None:
    pool = await create_pool()
    rabbit = Rabbit()
    await rabbit.connect()
    await rabbit.declare_topology()
    log.info("scheduler started (digest every %.0f s)", digest_interval())
    try:
        await Scheduler(pool, rabbit).run(stop)
    finally:
        await rabbit.close()
        await close_redis()
        await pool.close()
