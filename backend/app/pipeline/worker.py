"""Event worker (spec 4.4.2): `events.in` -> idempotency -> normalize -> rules -> alert upsert -> SSE -> ack.

Messages are handled concurrently (bounded by a semaphore; RabbitMQ prefetch bounds the unacked window).
Delivery guarantee: at-least-once. Infrastructure errors requeue the message until the dependency is
back; any other unexpected error requeues once, then the event is counted `invalid` and acked
(no poison loop).
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Callable
from contextlib import AsyncExitStack, suppress
from typing import Any
from uuid import UUID

import asyncpg
import orjson
import redis.asyncio as aioredis
import redis.exceptions as redis_exc
from aio_pika.abc import AbstractIncomingMessage
from prometheus_client import start_http_server

from app.core.db import create_pool
from app.core.metrics import EVENTS_OUTCOME, PROCESSING_LATENCY, RULE_EVAL
from app.core.outbox import publish_outbox_rows
from app.core.rabbit import EVENTS_QUEUE, Rabbit
from app.core.redis import get_redis, rkey
from app.core.sse import publish_sse
from app.pipeline.alert_writer import UpsertResult, alert_summary, upsert_alert
from app.pipeline.envelope import parse_received_at
from app.pipeline.normalize import normalize
from app.pipeline.present import Renderer, build_alert
from app.pipeline.presets import RESOLVE, get_preset
from app.pipeline.presets.autoresolve import resolve_from_source
from app.pipeline.ruleview import cfg

log = logging.getLogger(__name__)

DEFAULT_PREFETCH = 200
DEFAULT_CONCURRENCY = 64
RECENT_MAXLEN = 5000
CLAIM_TTL = 300
DONE_TTL = 3600
SOURCE_TTL = 5.0
TEAM_TTL = 60.0
SSE_THROTTLE_MS = 250
TRANSIENT_BACKOFF = 1.0
CLOSE_TIMEOUT = 5.0
TRANSIENT_ERRORS: tuple[type[BaseException], ...] = (
    OSError,
    TimeoutError,
    asyncpg.PostgresConnectionError,
    asyncpg.InterfaceError,
    asyncpg.TooManyConnectionsError,
    redis_exc.ConnectionError,
    redis_exc.TimeoutError,
)


class TTLCache:
    """Tiny async cache: one loader call per key per TTL."""

    def __init__(self, ttl: float, loader: Callable[[Any], Any]) -> None:
        self._ttl = ttl
        self._loader = loader
        self._items: dict[Any, tuple[float, Any]] = {}

    async def get(self, key: Any) -> Any:
        hit = self._items.get(key)
        now = time.monotonic()
        if hit and now - hit[0] < self._ttl:
            return hit[1]
        value = await self._loader(key)
        self._items[key] = (now, value)
        return value


def _default_render() -> Renderer:
    from app.pipeline.rules.templates import render

    return render


def _default_index() -> Any:
    from app.pipeline.rules.index import RuleIndex

    return RuleIndex()


class Worker:
    def __init__(
        self,
        pool: asyncpg.Pool,
        rabbit: Rabbit,
        redis: aioredis.Redis,
        *,
        index: Any,
        render: Renderer,
        planner: Callable[..., Any] | None = None,
        concurrency: int = DEFAULT_CONCURRENCY,
        prefetch: int = DEFAULT_PREFETCH,
    ) -> None:
        self.pool = pool
        self.rabbit = rabbit
        self.redis = redis
        self.index = index
        self.render = render
        self.planner = planner
        self.prefetch = prefetch
        self._sem = asyncio.Semaphore(concurrency)
        self._sources = TTLCache(SOURCE_TTL, self._load_source)
        self._team_domains = TTLCache(TEAM_TTL, self._load_team_domains)
        self._trailing: set[UUID] = set()
        self._tasks: set[asyncio.Task] = set()
        self._handlers: set[asyncio.Task] = set()

    async def start(self) -> None:
        await self.rabbit.consume(EVENTS_QUEUE, self.handle, prefetch=self.prefetch)

    async def close(self) -> None:
        """Let in-flight messages finish (the consumer is already closed), then drop background tasks."""
        if self._handlers:
            await asyncio.wait(self._handlers, timeout=CLOSE_TIMEOUT)
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    # ---- message handling -------------------------------------------------------------------

    async def handle(self, message: AbstractIncomingMessage) -> None:
        current = asyncio.current_task()
        if current is not None:
            self._handlers.add(current)
        try:
            await self._handle(message)
        finally:
            self._handlers.discard(current)

    async def _handle(self, message: AbstractIncomingMessage) -> None:
        async with self._sem:
            try:
                await self.process(message.body, redelivered=bool(message.redelivered))
            except TRANSIENT_ERRORS:
                log.warning("dependency unavailable; requeueing event", exc_info=True)
                await asyncio.sleep(TRANSIENT_BACKOFF)
                await message.nack(requeue=True)
                return
            except Exception:
                log.exception("event processing failed", extra={"redelivered": message.redelivered})
                if not message.redelivered:
                    await message.nack(requeue=True)
                    return
                EVENTS_OUTCOME.labels("invalid").inc()
            await message.ack()

    async def process(self, body: bytes, *, redelivered: bool = False) -> str:
        """Run one event through the pipeline; returns its outcome (the metric label)."""
        envelope = self._parse(body)
        if envelope is None:
            return self._count("invalid")
        key = rkey("evt", envelope["event_id"])
        if not await self._claim(key, redelivered):
            return "duplicate"
        try:
            outcome = await self._run(envelope)
        except Exception:
            await self.redis.delete(key)
            raise
        await self.redis.set(key, "1", ex=DONE_TTL)
        return outcome

    @staticmethod
    def _parse(body: bytes) -> dict[str, Any] | None:
        try:
            envelope = orjson.loads(body)
            UUID(envelope["source_id"])
            parse_received_at(envelope["received_at"])
            if not isinstance(envelope["raw"], dict) or not envelope["event_id"]:
                return None
        except (ValueError, KeyError, TypeError):
            return None
        return envelope

    async def _claim(self, key: str, redelivered: bool) -> bool:
        """Step 2.1. A redelivered message may take over a crashed worker's in-flight claim (`p`)."""
        if await self.redis.set(key, "p", nx=True, ex=CLAIM_TTL):
            return True
        return redelivered and await self.redis.get(key) == "p"

    @staticmethod
    def _count(outcome: str) -> str:
        EVENTS_OUTCOME.labels(outcome).inc()
        return outcome

    # ---- the pipeline -----------------------------------------------------------------------

    async def _run(self, envelope: dict[str, Any]) -> str:
        source_id = UUID(envelope["source_id"])
        source = await self._sources.get(source_id)
        if source is None:
            log.warning("event for unknown source %s", source_id)
            return await self._finish(envelope, source_id, "invalid", None)
        ev = normalize(envelope, source)
        envelope["_meta"] = ev.meta

        preset = get_preset(source.get("preset"))
        if preset and preset.on_event and preset.on_event(ev.raw) == RESOLVE:
            return await self._auto_resolve(preset, ev, envelope, source, source_id)

        started = time.perf_counter()
        matched = []
        for candidate in self.index.candidates(ev):
            try:
                if candidate.matches(ev):
                    matched.append(candidate)
            except Exception:
                log.exception("rule evaluation failed", extra={"rule_id": str(cfg(candidate.rule, "id"))})
        RULE_EVAL.observe(time.perf_counter() - started)

        outcome = "unrouted"
        for candidate in matched:
            result = await self._write(candidate, ev, envelope, source)
            outcome = self._merge_outcome(outcome, result, cfg(candidate.rule, "grouping.by_field"))
        return await self._finish(envelope, source_id, outcome, ev)

    async def _auto_resolve(
        self, preset: Any, ev: Any, envelope: dict[str, Any], source: dict[str, Any], source_id: UUID
    ) -> str:
        """A preset's "resolved" notification: resolve the matching open alerts, never create one."""
        matched = []
        for candidate in self.index.candidates(ev):
            try:
                if candidate.matches(ev):
                    matched.append(candidate)
            except Exception:
                log.exception("rule evaluation failed", extra={"rule_id": str(cfg(candidate.rule, "id"))})
        resolved = await resolve_from_source(
            self.pool,
            self.rabbit,
            matched,
            ev,
            envelope,
            domain_id=source["domain_id"],
            render=self.render,
            note=preset.resolve_note,
        )
        return await self._finish(envelope, source_id, "auto_resolved" if resolved else "unrouted", ev)

    @staticmethod
    def _merge_outcome(current: str, result: UpsertResult, grouping: Any) -> str:
        """An event that matched several rules ends in ONE outcome: new > grouped > deduplicated."""
        mine = "alerted_new" if result.inserted else ("grouped" if grouping else "deduplicated")
        order = {"unrouted": 0, "deduplicated": 1, "grouped": 2, "alerted_new": 3}
        return mine if order[mine] > order[current] else current

    async def _write(
        self, candidate: Any, ev: Any, envelope: dict[str, Any], source: dict[str, Any]
    ) -> UpsertResult:
        rule = candidate.rule
        domain_id = await self._domain_for(UUID(str(cfg(rule, "team_id"))), source)
        rendered, fingerprint = build_alert(rule, ev, envelope, render=self.render, domain_id=domain_id)
        async with self.pool.acquire() as conn:
            result = await upsert_alert(
                conn,
                rule=candidate,
                ev=ev,
                envelope=envelope,
                rendered=rendered,
                fingerprint=fingerprint,
                planner=self.planner,
            )
        await publish_outbox_rows(self.pool, self.rabbit, result.outbox_ids)
        if not result.inserted and not result.severity_increased:
            await self._count_changed(result.alert)
        return result

    async def _finish(self, envelope: dict[str, Any], source_id: UUID, outcome: str, ev: Any) -> str:
        """Counters, latency, recent stream (step 2.2) and per-source stats; one Redis round trip."""
        self._count(outcome)
        if outcome != "invalid":
            received = parse_received_at(envelope["received_at"])
            PROCESSING_LATENCY.observe(max(0.0, time.time() - received.timestamp()))
        try:
            pipe = self.redis.pipeline(transaction=False)
            if ev is not None:
                entry = {k: envelope[k] for k in ("event_id", "source_id", "received_at", "raw", "_meta")}
                pipe.xadd(
                    rkey("recent", str(source_id)),
                    {"envelope": orjson.dumps(entry).decode()},
                    maxlen=RECENT_MAXLEN,
                    approximate=True,
                )
            pipe.hincrby(rkey("stats", "source", str(source_id)), outcome, 1)
            await pipe.execute()
        except Exception:
            log.warning("recording event stats failed", exc_info=True)
        return outcome

    # ---- lookups ------------------------------------------------------------------------------

    async def _load_source(self, source_id: UUID) -> dict[str, Any] | None:
        row = await self.pool.fetchrow(
            "SELECT id, name, team_id, domain_id, type, preset, mapping, mapping_status, default_severity "
            "FROM sources WHERE id = $1",
            source_id,
        )
        if row is None:
            return None
        source = dict(row)
        status = source.pop("mapping_status")
        if isinstance(source["mapping"], dict) and status:
            source["mapping"] = {**source["mapping"], "status": status}
        return source

    async def _load_team_domains(self, team_id: UUID) -> list[UUID]:
        rows = await self.pool.fetch(
            "SELECT domain_id FROM team_domains WHERE team_id = $1 ORDER BY domain_id", team_id
        )
        return [r["domain_id"] for r in rows]

    async def _domain_for(self, team_id: UUID, source: dict[str, Any]) -> UUID:
        """The source's domain when the rule's team belongs to it (cross-domain teams), else the team's."""
        if source["team_id"] == team_id:
            return source["domain_id"]
        domains = await self._team_domains.get(team_id)
        if source["domain_id"] in domains or not domains:
            return source["domain_id"]
        return domains[0]

    # ---- SSE for count changes ---------------------------------------------------------------

    async def _count_changed(self, alert: dict[str, Any]) -> None:
        """`alert.updated` for a plain count increment: at most one per alert per 250 ms (+ trailing edge)."""
        if await self.redis.set(rkey("sse", "throttle", str(alert["id"])), "1", nx=True, px=SSE_THROTTLE_MS):
            await publish_sse("alert.updated", alert_summary(alert), team_ids=[alert["team_id"]])
        elif alert["id"] not in self._trailing:
            self._trailing.add(alert["id"])
            task = asyncio.get_running_loop().create_task(self._trailing_update(alert["id"]))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def _trailing_update(self, alert_id: UUID) -> None:
        """Publish the final count once the throttle window ended, so the UI never stays stale."""
        try:
            await asyncio.sleep(SSE_THROTTLE_MS / 1000 + 0.02)
            self._trailing.discard(alert_id)
            row = await self.pool.fetchrow("SELECT * FROM alerts WHERE id = $1", alert_id)
            if row is not None:
                await publish_sse("alert.updated", alert_summary(dict(row)), team_ids=[row["team_id"]])
        except Exception:
            self._trailing.discard(alert_id)
            log.warning("trailing SSE update failed", exc_info=True)


_metrics_started = False


def _ensure_metrics_server() -> None:
    """Non-API roles expose Prometheus metrics on METRICS_PORT (9101); once per process."""
    global _metrics_started
    if _metrics_started:
        return
    _metrics_started = True
    port = int(os.environ.get("METRICS_PORT", "9101"))
    try:
        start_http_server(port)
    except OSError:
        log.warning("metrics port %s unavailable; skipping metrics server", port)


async def _cancel(task: asyncio.Task) -> None:
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


async def _serve(stop: asyncio.Event) -> None:
    async with AsyncExitStack() as stack:
        pool = await create_pool()
        stack.push_async_callback(pool.close)
        index = _default_index()
        await index.load_all(pool)
        stack.push_async_callback(_cancel, await index.start_hot_reload(pool))
        rabbit = Rabbit()
        await rabbit.connect()
        await rabbit.declare_topology()
        worker = Worker(pool, rabbit, get_redis(), index=index, render=_default_render())
        stack.push_async_callback(worker.close)  # runs after rabbit.close: no new messages, drain, then pool
        stack.push_async_callback(rabbit.close)
        _ensure_metrics_server()
        await worker.start()
        log.info("worker consuming %s", EVENTS_QUEUE)
        await stop.wait()


async def run(stop: asyncio.Event) -> None:
    """Role entry point. Stopping during start-up cancels it cleanly (everything opened so far is closed)."""
    serving = asyncio.create_task(_serve(stop), name="worker-serve")
    stopper = asyncio.create_task(stop.wait())
    done, _ = await asyncio.wait({serving, stopper}, return_when=asyncio.FIRST_COMPLETED)
    stopper.cancel()
    if serving in done:
        await serving
        return
    serving.cancel()
    with suppress(asyncio.CancelledError):
        await serving
