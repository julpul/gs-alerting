"""Numbers for GET /v1/metrics/summary.

Counters come from the Redis hashes every process mirrors once per second (`app/core/stats.py`):
`rkey("stats", <prometheus metric name>)`, field = label value(s), value = float total.
Latency percentiles come from Postgres (`percentile_cont`), queue depths from the RabbitMQ management API.
"""

from __future__ import annotations

import logging
import os
import time
from collections import deque
from typing import Any
from urllib.parse import quote, unquote, urlsplit

import asyncpg
import httpx
import redis.asyncio as aioredis

from app.config import get_settings
from app.core.rabbit import CHANNELS
from app.core.redis import rkey
from app.core.scope import Scope

log = logging.getLogger(__name__)

INGESTED = "alerting_ingested_total"
OUTCOMES = "alerting_events_outcome_total"
EVENTS_QUEUE = "events.in"
DEAD_LETTER_QUEUE = "deliveries.dlq"
DEPTH_QUEUES = (EVENTS_QUEUE, "ai.tasks", *(f"deliveries.{c}" for c in CHANNELS))
RATE_WINDOW_SECONDS = 10.0

_samples: deque[tuple[float, float]] = deque(maxlen=64)


async def stats_hash(redis: aioredis.Redis, metric: str) -> dict[str, float]:
    raw = await redis.hgetall(rkey("stats", metric))
    out: dict[str, float] = {}
    for field, value in raw.items():
        try:
            out[field] = float(value)
        except ValueError:
            continue
    return out


async def exact_outcomes(redis: aioredis.Redis) -> dict[str, float]:
    """Per-event outcome counts the worker writes in the same Redis pipeline that marks the event done
    (`stats:source:<id>` hashes). Exact even when a worker is SIGKILLed, unlike the 1 s Prometheus
    mirror, so `lost` does not show the killed process's unflushed counter deltas."""
    totals: dict[str, float] = {}
    async for key in redis.scan_iter(match=rkey("stats", "source", "*"), count=500):
        for field, value in (await redis.hgetall(key)).items():
            try:
                totals[field] = totals.get(field, 0.0) + float(value)
            except ValueError:
                continue
    return totals


def events_per_second(ingested_total: float, now: float | None = None) -> float:
    """Rate of the ingested counter over the last ~10 s of samples taken by successive calls."""
    now = time.monotonic() if now is None else now
    _samples.append((now, ingested_total))
    while len(_samples) > 1 and now - _samples[0][0] > RATE_WINDOW_SECONDS:
        _samples.popleft()
    t0, v0 = _samples[0]
    if now - t0 < 0.25:
        return 0.0
    return max(0.0, (ingested_total - v0) / (now - t0))


def mgmt_base_url() -> tuple[str, tuple[str, str] | None, str]:
    """(base url, basic auth, vhost) of the RabbitMQ management API, derived from RABBITMQ_URL."""
    parts = urlsplit(get_settings().rabbitmq_url)
    vhost = unquote(parts.path[1:]) if len(parts.path) > 1 else "/"
    auth = (unquote(parts.username or "guest"), unquote(parts.password or "guest"))
    override = os.environ.get("RABBITMQ_MGMT_URL")
    return (override or f"http://{parts.hostname or 'localhost'}:15672").rstrip("/"), auth, vhost


async def queue_depths() -> dict[str, int] | None:
    """Base queue name -> messages (ready + unacked) for this deployment's prefix; None if unreachable."""
    base, auth, vhost = mgmt_base_url()
    prefix = get_settings().mq_prefix
    try:
        async with httpx.AsyncClient(timeout=1.5, auth=auth) as client:
            resp = await client.get(f"{base}/api/queues/{quote(vhost, safe='')}")
            resp.raise_for_status()
            queues = resp.json()
    except (httpx.HTTPError, ValueError):
        log.debug("RabbitMQ management API unreachable", exc_info=True)
        return None
    return {
        q["name"][len(prefix) :]: int(q.get("messages", 0)) for q in queues if q["name"].startswith(prefix)
    }


async def _delivery_totals(conn: asyncpg.Connection, scope: Scope) -> tuple[int, int]:
    row = await conn.fetchrow(
        "SELECT count(*) FILTER (WHERE d.delivered_at IS NOT NULL OR d.status IN ('delivered', 'acked')) "
        "AS delivered, count(*) FILTER (WHERE d.status = 'dead_lettered') AS dead "
        "FROM deliveries d JOIN alerts a ON a.id = d.alert_id "
        "WHERE ($1::bool OR a.team_id = ANY($2))",
        scope.is_master,
        list(scope.team_ids),
    )
    return row["delivered"], row["dead"]


def _percentiles(row: asyncpg.Record) -> dict[str, float | None]:
    return {"p50": row["p50"], "p99": row["p99"], "count": row["n"]}


async def _delivery_latency(
    conn: asyncpg.Connection, scope: Scope, minutes: int
) -> tuple[dict[str, dict[str, Any]], dict[str, Any] | None]:
    """Per-channel and overall `delivered_at - event_received_at` in ms over the window."""
    rows = await conn.fetch(
        "SELECT x.channel, count(*) AS n, "
        "percentile_cont(0.5) WITHIN GROUP (ORDER BY x.ms) AS p50, "
        "percentile_cont(0.99) WITHIN GROUP (ORDER BY x.ms) AS p99 "
        "FROM (SELECT d.channel, extract(epoch FROM d.delivered_at - d.event_received_at) * 1000 AS ms "
        "      FROM deliveries d JOIN alerts a ON a.id = d.alert_id "
        "      WHERE d.delivered_at >= now() - $1 * interval '1 minute' "
        "        AND d.event_received_at IS NOT NULL AND ($2::bool OR a.team_id = ANY($3))) x "
        "GROUP BY ROLLUP (x.channel)",
        minutes,
        scope.is_master,
        list(scope.team_ids),
    )
    per_channel = {r["channel"]: _percentiles(r) for r in rows if r["channel"] is not None}
    overall = next((_percentiles(r) for r in rows if r["channel"] is None), None)
    return per_channel, overall


async def _processing_latency(conn: asyncpg.Connection, scope: Scope, minutes: int) -> dict[str, Any] | None:
    row = await conn.fetchrow(
        "SELECT count(*) AS n, "
        "percentile_cont(0.5) WITHIN GROUP (ORDER BY ms) AS p50, "
        "percentile_cont(0.99) WITHIN GROUP (ORDER BY ms) AS p99 "
        "FROM (SELECT extract(epoch FROM a.created_at - a.first_event_received_at) * 1000 AS ms "
        "      FROM alerts a WHERE a.created_at >= now() - $1 * interval '1 minute' "
        "        AND a.first_event_received_at IS NOT NULL AND ($2::bool OR a.team_id = ANY($3))) x",
        minutes,
        scope.is_master,
        list(scope.team_ids),
    )
    return _percentiles(row) if row["n"] else None


async def metrics_summary(
    pool: asyncpg.Pool, redis: aioredis.Redis, scope: Scope, window_minutes: int = 15
) -> dict[str, Any]:
    ingested_by_source = await stats_hash(redis, INGESTED)
    outcomes = await exact_outcomes(redis) or await stats_hash(redis, OUTCOMES)
    ingested = sum(ingested_by_source.values())
    depths = await queue_depths()

    async with pool.acquire() as conn:
        delivered, dead_rows = await _delivery_totals(conn, scope)
        per_channel, overall = await _delivery_latency(conn, scope, window_minutes)
        processing = await _processing_latency(conn, scope, window_minutes)

    in_flight = (depths or {}).get(EVENTS_QUEUE, 0)
    lost = max(0, round(ingested - sum(outcomes.values()) - in_flight)) if outcomes or not ingested else None
    queue_depth = None if depths is None else sum(depths.get(q, 0) for q in DEPTH_QUEUES)
    dead_letter = dead_rows + (depths or {}).get(DEAD_LETTER_QUEUE, 0)
    return {
        "eventsPerSecond": round(events_per_second(ingested), 2),
        "latencyP50Ms": overall["p50"] if overall else None,
        "latencyP99Ms": overall["p99"] if overall else None,
        "queueDepth": queue_depth,
        "eventsIngested": round(ingested),
        "notificationsDelivered": delivered,
        "inDeadLetter": dead_letter,
        "lost": lost,
        "windowMinutes": window_minutes,
        "outcomes": {k: round(v) for k, v in outcomes.items()},
        "queues": depths,
        "processingLatencyMs": processing,
        "deliveryLatencyMs": per_channel,
    }
