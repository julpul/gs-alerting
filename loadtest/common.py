"""Helpers shared by report.py and reconcile.py (stdlib + httpx only).

Pure functions are separated from the functions doing HTTP so they can be unit tested.
"""

from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

LOADTEST_DIR = Path(__file__).resolve().parent
RAW_DIR = Path(os.environ.get("RAW_DIR", LOADTEST_DIR / "results" / "raw"))

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://alerting:alerting@localhost:5432/alerting")
PROM_URL = os.environ.get("PROM_URL", "http://localhost:9090").rstrip("/")
RABBIT_URL = os.environ.get("RABBIT_URL", "http://localhost:15672").rstrip("/")
RABBIT_AUTH = tuple(os.environ.get("RABBIT_AUTH", "guest:guest").split(":", 1))
API_URL = os.environ.get("API_URL", "http://localhost:8080").rstrip("/")

# Queues that must be empty before reconciling (the DLQ legitimately keeps messages).
DRAIN_QUEUE_RE = re.compile(
    r"^(events\.in|deliveries\.(ui|email|sms|phone|webhook)|deliveries\.retry\.\d+s)$"
)


def parse_ts(value: str) -> datetime:
    """Parse an ISO-8601 timestamp (with Z or offset) into an aware UTC datetime."""
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def load_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None


# ---------------------------------------------------------------- Prometheus


def counter_increase(samples: Iterable[tuple[float, float]]) -> float:
    """Total increase of one counter series from (timestamp, value) samples.

    A drop in value means the process restarted (e.g. a killed worker): the new value is
    counted from zero, exactly like PromQL ``increase`` but without extrapolation.
    """
    total = 0.0
    prev: float | None = None
    for _, value in sorted(samples):
        if prev is not None:
            total += value - prev if value >= prev else value
        prev = value
    return total


def series_increase(series: list[dict[str, Any]]) -> float:
    """Sum of counter_increase over every series of a query_range result."""
    return sum(counter_increase((float(t), float(v)) for t, v in s["values"]) for s in series)


def group_increase(series: list[dict[str, Any]], label: str) -> dict[str, float]:
    out: dict[str, float] = {}
    for s in series:
        key = s["metric"].get(label, "")
        out[key] = out.get(key, 0.0) + series_increase([s])
    return out


def prom_reachable(url: str = PROM_URL) -> bool:
    try:
        return httpx.get(f"{url}/-/ready", timeout=2).status_code == 200
    except httpx.HTTPError:
        return False


def prom_query_range(
    query: str, start: datetime, end: datetime, step: float = 2.0, url: str = PROM_URL
) -> list[dict[str, Any]]:
    resp = httpx.get(
        f"{url}/api/v1/query_range",
        params={"query": query, "start": start.timestamp(), "end": end.timestamp(), "step": step},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["data"]["result"]


# ------------------------------------------------------------------ RabbitMQ


def rabbit_queue_depths() -> dict[str, int]:
    """name -> ready + unacked for every queue (RabbitMQ management API)."""
    resp = httpx.get(f"{RABBIT_URL}/api/queues", auth=RABBIT_AUTH, timeout=5)
    resp.raise_for_status()
    return {
        q["name"]: int(q.get("messages_ready", 0)) + int(q.get("messages_unacknowledged", 0))
        for q in resp.json()
    }


def pending_in_drain_queues(depths: dict[str, int]) -> dict[str, int]:
    return {name: n for name, n in depths.items() if DRAIN_QUEUE_RE.match(name) and n > 0}


def wait_drain(
    timeout_s: float,
    poll_s: float = 2.0,
    fetch: Callable[[], dict[str, int]] = rabbit_queue_depths,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> tuple[bool, dict[str, int], float]:
    """Poll until events.in and deliveries.* are empty twice in a row or the timeout expires.

    Returns (drained, still_pending, waited_seconds).
    """
    started = clock()
    clear_polls = 0
    pending: dict[str, int] = {}
    while True:
        try:
            pending = pending_in_drain_queues(fetch())
        except httpx.HTTPError as exc:
            pending = {f"<management API unreachable: {exc.__class__.__name__}>": 1}
        clear_polls = clear_polls + 1 if not pending else 0
        if clear_polls >= 2:
            return True, {}, clock() - started
        if clock() - started >= timeout_s:
            return False, pending, clock() - started
        sleep(poll_s)
