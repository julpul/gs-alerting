"""Stub for the scheduler role; the body is replaced by P3-ESCAL."""

from __future__ import annotations

import asyncio


async def run(stop: asyncio.Event) -> None:
    await stop.wait()
