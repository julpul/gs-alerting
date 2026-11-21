"""Small helpers for MCP tests."""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID

import asyncpg
from mcp import Client

from app.core.audit import user_actor
from app.core.ids import stable_id
from app.runbooks.approvals import decide


async def call(client: Client, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    result = await client.call_tool(name, arguments or {})
    assert not result.is_error, "".join(getattr(c, "text", "") for c in result.content)
    return result.structured_content


async def call_error(client: Client, name: str, arguments: dict[str, Any] | None = None) -> str:
    result = await client.call_tool(name, arguments or {})
    assert result.is_error, f"{name} unexpectedly succeeded: {result.structured_content}"
    return "".join(getattr(c, "text", "") for c in result.content)


async def eventually(check, timeout: float = 15.0) -> Any:  # noqa: ASYNC109
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        value = await check()
        if value:
            return value
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"condition not met within {timeout}s (last value: {value!r})")
        await asyncio.sleep(0.05)


async def pending_approval_of(pool: asyncpg.Pool, agent_name: str) -> asyncpg.Record | None:
    return await pool.fetchrow(
        "SELECT * FROM approvals WHERE status = 'pending' AND requested_by->>'name' = $1 "
        "ORDER BY created_at DESC LIMIT 1",
        agent_name,
    )


async def decide_as(
    pool: asyncpg.Pool,
    approval_id: UUID,
    email: str,
    name: str,
    decision: str = "approve",
    comment: str | None = None,
) -> dict[str, Any]:
    async with pool.acquire() as conn:
        return await decide(
            conn,
            approval_id,
            decision=decision,
            actor=user_actor(stable_id("user", email), name),
            comment=comment,
        )
