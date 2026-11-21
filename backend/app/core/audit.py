"""Audit log.

Actor dicts: {"type": "user"|"agent"|"system", "id": str|None, "name": str, "onBehalfOf": str|None}.
"""

from __future__ import annotations

from typing import Any

import asyncpg


def user_actor(user_id: Any, name: str) -> dict[str, Any]:
    return {"type": "user", "id": str(user_id), "name": name, "onBehalfOf": None}


SYSTEM_ACTOR: dict[str, Any] = {"type": "system", "id": None, "name": "system", "onBehalfOf": None}


async def audit(
    conn: asyncpg.Connection,
    actor: dict[str, Any],
    action: str,
    target_type: str,
    target_id: str,
    data: dict[str, Any] | None = None,
) -> None:
    await conn.execute(
        "INSERT INTO audit_log (at, actor, action, target_type, target_id, data) "
        "VALUES (now(), $1, $2, $3, $4, $5)",
        actor,
        action,
        target_type,
        str(target_id),
        data or {},
    )
