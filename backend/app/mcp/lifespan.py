"""Run an async context manager entirely inside one dedicated task."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager


@asynccontextmanager
async def in_own_task(manager: AbstractAsyncContextManager[None]) -> AsyncIterator[None]:
    """Enter `manager` in a background task and leave it from the same task.

    The MCP session manager owns an anyio task group (cancel scope) that must be exited by the task that
    entered it. Server lifespans (uvicorn) satisfy that, but test fixtures and nested exit stacks may not,
    so the manager lives in its own task for its whole life.
    """
    ready = asyncio.Event()
    stop = asyncio.Event()

    async def run() -> None:
        async with manager:
            ready.set()
            await stop.wait()

    task = asyncio.create_task(run(), name="mcp-session-manager")
    waiter = asyncio.create_task(ready.wait())
    try:
        await asyncio.wait({task, waiter}, return_when=asyncio.FIRST_COMPLETED)
        if task.done():
            task.result()
        yield
    finally:
        waiter.cancel()
        stop.set()
        await task
