"""Helpers for the HB-CORE Herald BFF tests (no conftest here: tests/herald is shared with HB-CONFIG)."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import asyncpg

from app.core.ids import stable_id
from tests.api import alerts_helpers as h

MATCH = {
    "op": "and",
    "conditions": [
        {"field": "type", "operator": "eq", "value": "TEST"},
        {
            "op": "or",
            "conditions": [
                {"field": "n", "operator": "gte", "value": 1},
                {"field": "missing.path", "operator": "exists", "value": True},
            ],
        },
        {"field": "env", "operator": "not_in", "value": ["dev"]},
        {"field": "_severity", "operator": "neq", "value": "info"},
    ],
}
WHY_RULE = stable_id("test-rule", "herald-why")


def as_person(person: str, **extra: str) -> dict[str, str]:
    return {"X-Herald-User": f"person:{person}", **extra}


async def why_rule(pool: asyncpg.Pool, team_slug: str = "team-payments") -> UUID:
    await pool.execute(
        "INSERT INTO rules (id, team_id, name, definition) VALUES ($1, $2, 'Herald why rule', $3) "
        "ON CONFLICT DO NOTHING",
        WHY_RULE,
        h.team(team_slug),
        {"name": "Herald why rule", "teamId": str(h.team(team_slug)), "match": MATCH},
    )
    return WHY_RULE


async def alert(pool: asyncpg.Pool, **kw: Any) -> asyncpg.Record:
    """An alert via the /v1 alerts test helper (team-payments by default)."""
    return await h.make_alert(pool, **kw)
