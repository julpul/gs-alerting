"""The people an alert of a team escalates through (level 1..n of the effective policy)."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

import asyncpg

from app.delivery.recipients import Person, domain_roots, masters, resolve_specs
from app.delivery.ruleview import parse_specs
from app.escalation.policies import EffectivePolicy


@dataclass(frozen=True)
class ChainLevel:
    level: int
    target: str
    people: list[Person]


async def level_people(
    conn: asyncpg.Connection, policy: EffectivePolicy, level: int, *, team_id: UUID, domain_id: UUID
) -> tuple[str, list[Person]]:
    """(target, people) of policy level `level`."""
    lvl = policy.level_for(level)
    target = lvl.target.value if lvl else "domain_root"
    if target == "oncall":
        specs = parse_specs([f"oncall:{team_id}"])
        return target, await resolve_specs(conn, specs, team_id=team_id, domain_id=domain_id)
    if target == "master":
        return target, await masters(conn)
    if target == "custom" and lvl and lvl.recipients:
        specs = parse_specs([{"kind": r.kind.value, "id": r.id} for r in lvl.recipients])
        return target, await resolve_specs(conn, specs, team_id=team_id, domain_id=domain_id)
    return "domain_root", await domain_roots(conn, domain_id)


async def escalation_chain(
    conn: asyncpg.Connection, policy: EffectivePolicy, *, team_id: UUID, domain_id: UUID
) -> list[ChainLevel]:
    return [
        ChainLevel(lv.level, target, people)
        for lv in policy.levels
        for target, people in [
            await level_people(conn, policy, lv.level, team_id=team_id, domain_id=domain_id)
        ]
    ]
