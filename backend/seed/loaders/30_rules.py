"""Seed the four demo rules (TECH_SPEC 7.1). Rules that were edited through the API (version > 1) are kept."""

from __future__ import annotations

from typing import Any

import asyncpg

from app.core.ids import stable_id
from app.pipeline.rules.compiler import compile_rule
from app.pipeline.rules.model import parse_rule


async def load(conn: asyncpg.Connection, fixtures: dict[str, Any]) -> None:
    created_by = stable_id("user", "master@demo")
    for item in fixtures["rules"]["rules"]:
        data = dict(item["rule"])
        data["teamId"] = str(stable_id("team", data["teamId"]))
        if item.get("runbook"):
            data["runbookId"] = str(stable_id("runbook", item["runbook"]))
        rule = parse_rule(data)
        compile_rule(rule)
        rule_id = stable_id("rule", item["key"])
        definition = rule.definition()
        await conn.execute(
            "INSERT INTO rules (id, team_id, name, enabled, version, definition, created_by) "
            "VALUES ($1, $2, $3, $4, 1, $5, $6) "
            "ON CONFLICT (id) DO UPDATE SET team_id = EXCLUDED.team_id, name = EXCLUDED.name, "
            "enabled = EXCLUDED.enabled, definition = EXCLUDED.definition, updated_at = now() "
            "WHERE rules.version = 1",
            rule_id,
            rule.team_id,
            rule.name,
            rule.enabled,
            definition,
            created_by,
        )
        await conn.execute(
            "INSERT INTO rule_versions (rule_id, version, definition, changed_by) VALUES ($1, 1, $2, $3) "
            "ON CONFLICT (rule_id, version) DO NOTHING",
            rule_id,
            definition,
            created_by,
        )
