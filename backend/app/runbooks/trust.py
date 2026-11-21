"""Trust ladder (spec 11.5): per (domain, action) `suggest -> run_after_approval -> auto`.

- No row: `auto` for low/medium risk actions, `run_after_approval` for high risk ones.
- Only levels below `auto` make a step ask for approval; `suggest` additionally keeps agents from running it.
- Promotion: 5 consecutive successful runs that went through an approval move the action one level up,
  never above `cap` (set by the Domain Root; high-risk actions are capped at `run_after_approval`).
- Demotion: any failed run moves it one level down and resets the streak.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

import asyncpg

from app.runbooks.actions.registry import actions, default_risk, get_action

Level = Literal["suggest", "run_after_approval", "auto"]
LEVELS: tuple[Level, ...] = ("suggest", "run_after_approval", "auto")
PROMOTE_AFTER = 5


@dataclass
class Trust:
    domain_id: UUID
    action: str
    level: Level
    cap: Level | None
    successes: int = 0
    failures: int = 0
    changed_at: datetime | None = None
    stored: bool = False


def _rank(level: str) -> int:
    return LEVELS.index(level)  # type: ignore[arg-type]


def ceiling(action: str, cap: str | None) -> Level:
    """Highest level the action may ever reach: its cap, and `run_after_approval` if it is high risk."""
    top: Level = "auto"
    if default_risk(action) == "high":
        top = "run_after_approval"
    if cap and _rank(cap) < _rank(top):
        top = cap  # type: ignore[assignment]
    return top


def default_level(action: str) -> Level:
    return "run_after_approval" if default_risk(action) == "high" else "auto"


def _clamp(level: str, action: str, cap: str | None) -> Level:
    top = ceiling(action, cap)
    return level if _rank(level) <= _rank(top) else top  # type: ignore[return-value]


def _from_row(domain_id: UUID, action: str, row: asyncpg.Record | None) -> Trust:
    if row is None:
        return Trust(domain_id, action, default_level(action), None)
    return Trust(
        domain_id,
        action,
        _clamp(row["level"], action, row["cap"]),
        row["cap"],
        row["successes"],
        row["failures"],
        row["updated_at"],
        stored=True,
    )


async def get_trust(conn: asyncpg.Connection, domain_id: UUID, action: str) -> Trust:
    row = await conn.fetchrow(
        "SELECT level, cap, successes, failures, updated_at FROM action_trust "
        "WHERE domain_id = $1 AND action = $2",
        domain_id,
        action,
    )
    return _from_row(domain_id, action, row)


async def list_trust(conn: asyncpg.Connection, domain_id: UUID) -> list[Trust]:
    """One entry per registered action plus any stored row for an action that is no longer registered."""
    rows = {
        r["action"]: r
        for r in await conn.fetch(
            "SELECT action, level, cap, successes, failures, updated_at "
            "FROM action_trust WHERE domain_id = $1",
            domain_id,
        )
    }
    names = sorted({*actions(), *rows})
    return [_from_row(domain_id, name, rows.get(name)) for name in names]


async def set_trust(
    conn: asyncpg.Connection,
    domain_id: UUID,
    action: str,
    *,
    level: str | None,
    cap: str | None,
    cap_given: bool,
) -> Trust:
    """Manual change by a Domain Root. `level` above the ceiling is clamped by `get_trust` afterwards."""
    current = await get_trust(conn, domain_id, action)
    new_cap = cap if cap_given else current.cap
    new_level = level or current.level
    await conn.execute(
        "INSERT INTO action_trust (domain_id, action, level, cap, successes, failures, updated_at) "
        "VALUES ($1, $2, $3, $4, $5, $6, now()) "
        "ON CONFLICT (domain_id, action) DO UPDATE SET level = EXCLUDED.level, cap = EXCLUDED.cap, "
        "successes = 0, updated_at = now()",
        domain_id,
        action,
        _clamp(new_level, action, new_cap),
        new_cap,
        0,
        current.failures,
    )
    return await get_trust(conn, domain_id, action)


async def record_outcome(
    conn: asyncpg.Connection, domain_id: UUID, action: str, *, success: bool, approved: bool
) -> Trust:
    """Bookkeeping after a run. Read-only actions (`affects_trust = False`) are not counted."""
    spec = get_action(action)
    current = await get_trust(conn, domain_id, action)
    if spec is None or not spec.affects_trust:
        return current
    level, successes, failures = current.level, current.successes, current.failures
    if not success:
        failures += 1
        successes = 0
        level = LEVELS[max(_rank(level) - 1, 0)]
    elif approved:
        successes += 1
        if successes >= PROMOTE_AFTER and _rank(level) < _rank(ceiling(action, current.cap)):
            level = LEVELS[_rank(level) + 1]
            successes = 0
    await conn.execute(
        "INSERT INTO action_trust (domain_id, action, level, cap, successes, failures, updated_at) "
        "VALUES ($1, $2, $3, $4, $5, $6, now()) "
        "ON CONFLICT (domain_id, action) DO UPDATE SET level = EXCLUDED.level, "
        "successes = EXCLUDED.successes, failures = EXCLUDED.failures, "
        "updated_at = CASE WHEN action_trust.level <> EXCLUDED.level "
        "THEN now() ELSE action_trust.updated_at END",
        domain_id,
        action,
        level,
        current.cap,
        successes,
        failures,
    )
    return await get_trust(conn, domain_id, action)
