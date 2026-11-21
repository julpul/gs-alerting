"""Stub for the delivery worker role; the body is replaced by P2-DELIVERY."""

from __future__ import annotations

import asyncio


async def run(stop: asyncio.Event) -> None:
    await stop.wait()
