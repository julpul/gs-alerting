"""Shared redis.asyncio client and prefixed key builder."""

from __future__ import annotations

import redis.asyncio as aioredis

from app.config import get_settings

_client: aioredis.Redis | None = None


def get_redis() -> aioredis.Redis:
    global _client
    if _client is None:
        _client = aioredis.from_url(get_settings().redis_url, decode_responses=True)
    return _client


async def close_redis() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


def rkey(*parts: str) -> str:
    """`rkey("evt", id)` -> `<REDIS_PREFIX>evt:<id>`."""
    return get_settings().redis_prefix + ":".join(str(p) for p in parts)
