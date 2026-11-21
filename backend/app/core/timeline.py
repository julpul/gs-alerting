"""Alert timeline entries (written inside the caller's transaction)."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import asyncpg

from app.core.ids import uuid7


async def add_timeline(
    conn: asyncpg.Connection,
    alert_id: UUID,
    type: str,
    text: str,
    *,
    actor: dict[str, Any] | None = None,
    data: dict[str, Any] | None = None,
) -> None:
    await conn.execute(
        "INSERT INTO timeline (id, alert_id, at, type, actor, text, data) "
        "VALUES ($1, $2, now(), $3, $4, $5, $6)",
        uuid7(),
        alert_id,
        type,
        actor or {"type": "system", "id": None, "name": "system"},
        text,
        data or {},
    )
