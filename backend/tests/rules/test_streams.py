"""`recent:{source_id}` stream helpers used by sample-events, dry-run and the sources list."""

from __future__ import annotations

import time
from uuid import uuid4

import orjson

from app.core.redis import rkey
from app.repositories.sources import parse_stream_entry, read_recent, stream_stats


async def test_stream_stats_counts_only_the_last_24_hours_when_the_stream_is_old(redis):
    source_id = uuid4()
    key = rkey("recent", str(source_id))
    now_ms = int(time.time() * 1000)
    env = orjson.dumps({"raw": {"a": 1}}).decode()
    try:
        assert await stream_stats(redis, source_id) == (0, None)
        await redis.xadd(key, {"envelope": env}, id=f"{now_ms - 30 * 3600 * 1000}-0")
        await redis.xadd(key, {"envelope": env}, id=f"{now_ms - 3600 * 1000}-0")
        await redis.xadd(key, {"envelope": env}, id=f"{now_ms - 1000}-0")
        count, last = await stream_stats(redis, source_id)
        assert count == 2 and last is not None and abs(last.timestamp() * 1000 - (now_ms - 1000)) < 5
        recent = await read_recent(redis, source_id, limit=10, since_ms=now_ms - 1800 * 1000)
        assert len(recent) == 1
    finally:
        await redis.delete(key)


def test_parse_stream_entry_variants():
    ev = parse_stream_entry(
        "1792833245000-0",
        {
            "envelope": orjson.dumps(
                {"event_id": "x", "raw": {"a": 1}, "_meta": {"severity": "error"}}
            ).decode()
        },
    )
    assert ev.event_id == "x" and ev.raw == {"a": 1} and ev.meta == {"severity": "error"}
    assert ev.received_at.year == 2026
    bare = parse_stream_entry("1792833245000-3", {"envelope": orjson.dumps({"a": 1}).decode()})
    assert bare.event_id == "1792833245000-3" and bare.raw == {"a": 1} and bare.meta == {}
    odd = parse_stream_entry(
        "1792833245000-0",
        {"envelope": orjson.dumps({"raw": {"a": 1}, "received_at": "garbage", "_meta": 5}).decode()},
    )
    assert odd.received_at.year == 2026 and odd.meta == {}
    assert parse_stream_entry("1-0", {"envelope": orjson.dumps({"raw": "text"}).decode()}) is None
    assert parse_stream_entry("1-0", {}) is None
