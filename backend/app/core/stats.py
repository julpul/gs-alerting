"""Cross-process counters.

Prometheus counters live per process. `/v1/metrics/summary` needs org-wide totals, so every process
mirrors the *deltas* of selected counters into Redis hashes once per `STATS_FLUSH_SECONDS`
(one pipeline round-trip per second, nothing per event). Read totals with `read_stats()`.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from prometheus_client import REGISTRY, start_http_server

from app.config import get_settings
from app.core.redis import get_redis, rkey

log = logging.getLogger(__name__)

MIRRORED = {
    "alerting_ingested_total": "source",
    "alerting_events_outcome_total": "outcome",
    "alerting_deliveries_total": "channel_status",
}


def snapshot() -> dict[tuple[str, str], float]:
    """Current per-process values of mirrored counters keyed by (metric, field)."""
    out: dict[tuple[str, str], float] = {}
    for family in REGISTRY.collect():
        name = family.name + "_total" if not family.name.endswith("_total") else family.name
        if name not in MIRRORED:
            continue
        for sample in family.samples:
            if not sample.name.endswith("_total"):
                continue
            labels = sample.labels
            field = ":".join(labels[k] for k in sorted(labels)) or "all"
            out[(name, field)] = sample.value
    return out


async def flush(last: dict[tuple[str, str], float]) -> dict[tuple[str, str], float]:
    """Write deltas since `last` to Redis; returns the new baseline."""
    current = snapshot()
    deltas = {k: v - last.get(k, 0.0) for k, v in current.items() if v - last.get(k, 0.0) > 0}
    if deltas:
        pipe = get_redis().pipeline(transaction=False)
        for (metric, field), delta in deltas.items():
            pipe.hincrbyfloat(rkey("stats", metric), field, delta)
        await pipe.execute()
    return current


async def read_stats() -> dict[str, dict[str, float]]:
    """Org-wide totals: {metric: {label_values: total}}."""
    redis = get_redis()
    result: dict[str, dict[str, float]] = {}
    for metric in MIRRORED:
        raw = await redis.hgetall(rkey("stats", metric))
        result[metric] = {k: float(v) for k, v in raw.items()}
    return result


async def run_stats_mirror(stop: asyncio.Event) -> None:
    interval = get_settings().stats_flush_seconds
    last: dict[tuple[str, str], float] = {}
    while not stop.is_set():
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=interval)
        try:
            last = await flush(last)
        except Exception as exc:  # redis hiccup must never kill a role
            log.warning("stats flush failed: %r", exc)


def start_metrics_server(port: int) -> None:
    try:
        start_http_server(port)
        log.info("metrics server on :%d", port)
    except OSError as exc:  # port taken (e.g. several role processes on one host)
        log.warning("metrics server not started on :%d: %r", port, exc)
