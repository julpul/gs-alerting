"""Seed the org-default escalation policies (spec 10.1). Existing rows are left alone (they are editable)."""

from __future__ import annotations

from typing import Any

import asyncpg

from app.core.ids import ORG_ID


async def load(conn: asyncpg.Connection, fixtures: dict[str, Any]) -> None:
    for severity, policy in fixtures["policies"]["org"].items():
        await conn.execute(
            "INSERT INTO escalation_policies "
            "(scope_type, scope_id, severity, levels, renotify_seconds, soft_channels, soft_digest_minutes) "
            "VALUES ('org', $1, $2, $3, $4, $5, $6) ON CONFLICT DO NOTHING",
            ORG_ID,
            severity,
            policy["levels"],
            policy.get("renotifySeconds"),
            policy.get("softChannels", []),
            policy.get("softDigestMinutes"),
        )
