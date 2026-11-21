"""Stub for the AI worker role; the body is replaced by P3-AI."""

from __future__ import annotations

import asyncio


async def run(stop: asyncio.Event) -> None:
    await stop.wait()
