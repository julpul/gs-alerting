"""Stub for the source connector role; the body is replaced by P4-CONNECT."""

from __future__ import annotations

import asyncio


async def run(stop: asyncio.Event) -> None:
    await stop.wait()
