"""Connector role (`--role connector`): one connector task per configured source, kept in sync with the DB.

Sources are re-read every `RESCAN_SECONDS` (the sources API does not publish change events): a new source
starts a connector, a deleted one or one whose config changed is stopped (and restarted with the new
config). Adding a protocol = a `Connector` subclass registered in `CONNECTORS`.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import AsyncExitStack, suppress

import asyncpg

from app.connectors.base import Connector, SourceSpec, make_publisher
from app.connectors.rabbitmq import RabbitMQSourceConnector
from app.core.db import create_pool
from app.core.rabbit import Rabbit

log = logging.getLogger(__name__)

RESCAN_SECONDS = 30.0
CONNECTORS: dict[str, type[Connector]] = {RabbitMQSourceConnector.protocol: RabbitMQSourceConnector}


async def load_sources(pool: asyncpg.Pool) -> dict[str, SourceSpec]:
    """Sources a connector exists for, keyed by id."""
    rows = await pool.fetch(
        "SELECT id, name, type, preset, default_severity, config FROM sources WHERE type = ANY($1)",
        list(CONNECTORS),
    )
    return {str(r["id"]): SourceSpec(**dict(r)) for r in rows}


class Supervisor:
    def __init__(self, pool: asyncpg.Pool, rabbit: Rabbit) -> None:
        self.pool = pool
        self.rabbit = rabbit
        self._running: dict[str, tuple[tuple, asyncio.Task]] = {}

    @property
    def running(self) -> set[str]:
        return set(self._running)

    async def sync(self) -> None:
        """Start connectors for new sources, restart changed ones, stop removed ones."""
        wanted = await load_sources(self.pool)
        for source_id in list(self._running):
            signature, task = self._running[source_id]
            spec = wanted.get(source_id)
            if spec is None or spec.signature != signature or task.done():
                await self._stop(source_id)
        for source_id, spec in wanted.items():
            if source_id not in self._running:
                self._start(source_id, spec)

    def _start(self, source_id: str, spec: SourceSpec) -> None:
        connector = CONNECTORS[spec.type](spec)
        task = asyncio.create_task(
            connector.run(make_publisher(self.rabbit, spec)), name=f"connector-{spec.name}"
        )
        self._running[source_id] = (spec.signature, task)
        log.info("connector started for source %s (%s)", spec.name, spec.type)

    async def _stop(self, source_id: str) -> None:
        _, task = self._running.pop(source_id)
        task.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await task

    async def stop_all(self) -> None:
        for source_id in list(self._running):
            await self._stop(source_id)

    async def run(self, stop: asyncio.Event, rescan_seconds: float = RESCAN_SECONDS) -> None:
        while not stop.is_set():
            try:
                await self.sync()
            except Exception:
                log.warning("source scan failed; keeping current connectors", exc_info=True)
            with suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), rescan_seconds)


async def _serve(stop: asyncio.Event) -> None:
    async with AsyncExitStack() as stack:
        pool = await create_pool()
        stack.push_async_callback(pool.close)
        rabbit = Rabbit()
        await rabbit.connect()
        await rabbit.declare_topology()
        stack.push_async_callback(rabbit.close)
        supervisor = Supervisor(pool, rabbit)
        stack.push_async_callback(supervisor.stop_all)
        await supervisor.run(stop)


async def run(stop: asyncio.Event) -> None:
    """Role entry point."""
    serving = asyncio.create_task(_serve(stop), name="connector-serve")
    stopper = asyncio.create_task(stop.wait())
    done, _ = await asyncio.wait({serving, stopper}, return_when=asyncio.FIRST_COMPLETED)
    stopper.cancel()
    if serving in done:
        await serving
        return
    serving.cancel()
    with suppress(asyncio.CancelledError):
        await serving
