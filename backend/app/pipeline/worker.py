"""Stub for the event worker role; the body is replaced by P2-INGEST."""

from __future__ import annotations

import asyncio


async def run(stop: asyncio.Event) -> None:
    await stop.wait()
