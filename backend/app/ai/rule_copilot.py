"""Rule copilot (spec 7.6): natural language -> `RuleDef` draft, validated by the rule compiler.

The caller (rules router) runs the dry-run on the returned draft; nothing is saved here.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

import asyncpg
from pydantic import BaseModel

from app.ai import prompts
from app.ai.llm import AIUnavailable, complete_for_domain
from app.core.errors import AppError
from app.core.redis import get_redis
from app.core.scope import Scope
from app.pipeline.rules.compiler import compile_rule
from app.pipeline.rules.model import RuleDef, RuleValidationError, parse_rule
from app.repositories import rules as rules_repo
from app.repositories import sources as sources_repo

log = logging.getLogger(__name__)

MAX_SOURCES_PER_TEAM = 8
MAX_PATHS = 40
SAMPLE_EVENTS = 5


class RuleAnswer(BaseModel):
    rule: dict[str, Any]
    explanation: str = ""


def field_paths(raw: Any, prefix: str = "", depth: int = 0) -> list[str]:
    """Dotted paths of scalar leaves (first element of arrays), no values."""
    if depth > 5:
        return []
    paths: list[str] = []
    if isinstance(raw, dict):
        for key, value in raw.items():
            paths += field_paths(value, f"{prefix}.{key}" if prefix else str(key), depth + 1)
    elif isinstance(raw, list):
        if raw:
            paths += field_paths(raw[0], f"{prefix}.0" if prefix else "0", depth + 1)
    elif prefix:
        paths.append(prefix)
    return paths


async def build_catalog(conn: asyncpg.Connection, scope: Scope, team_id: UUID | None) -> dict[str, Any]:
    """Teams, their sources (detected schemas and sample field paths) and their members, within `scope`."""
    if team_id is not None:
        allowed = [team_id]
    elif scope.is_master:
        allowed = [r["id"] for r in await conn.fetch("SELECT id FROM teams")]
    else:
        allowed = list(scope.team_ids)
    redis = get_redis()
    teams: list[dict[str, Any]] = []
    for team in await conn.fetch(
        "SELECT id, slug, name FROM teams WHERE id = ANY($1) ORDER BY slug", allowed
    ):
        sources: list[dict[str, Any]] = []
        rows = await conn.fetch(
            "SELECT id, name, type, preset FROM sources WHERE team_id = $1 ORDER BY name LIMIT $2",
            team["id"],
            MAX_SOURCES_PER_TEAM,
        )
        for src in rows:
            events = await sources_repo.read_recent(redis, src["id"], limit=SAMPLE_EVENTS)
            schemas = sorted({str(e.meta.get("schema")) for e in events if e.meta.get("schema")})
            paths: list[str] = []
            for event in events:
                for path in field_paths(event.raw):
                    if path not in paths:
                        paths.append(path)
            sources.append(
                {
                    "name": src["name"],
                    "type": src["type"],
                    "preset": src["preset"],
                    "detectedSchemas": schemas,
                    "derivedFields": [
                        "_severity",
                        "_service",
                        "_environment",
                        "_type",
                        "_error_code",
                        "_title",
                    ],
                    "fieldPaths": paths[:MAX_PATHS],
                }
            )
        members = await conn.fetch(
            "SELECT u.display_name, u.email FROM team_members tm JOIN users u ON u.id = tm.user_id "
            "WHERE tm.team_id = $1 ORDER BY u.display_name",
            team["id"],
        )
        teams.append(
            {
                "id": team["slug"],
                "name": team["name"],
                "sources": sources,
                "members": [{"name": m["display_name"], "id": m["email"]} for m in members],
            }
        )
    return {"teams": teams}


async def _resolve_team(conn: asyncpg.Connection, scope: Scope, ref: Any) -> UUID:
    team_id = await rules_repo.resolve_team(conn, str(ref) if ref else None)
    if team_id is None or not scope.can_see_team(team_id):
        raise AppError(422, "invalid_rule", "the drafted rule targets a team you cannot use")
    return team_id


async def draft_rule(
    text: str, scope: Scope, conn: asyncpg.Connection, *, team_id: UUID | None = None
) -> tuple[RuleDef, str]:
    """Returns the validated draft (`id`/`version` unset) and the model's explanation."""
    catalog = await build_catalog(conn, scope, team_id)
    schema = RuleDef.model_json_schema(by_alias=True)
    system, user = prompts.rule_prompt(text, catalog, schema)
    domain_id = await sources_repo.team_domain(conn, team_id) if team_id else None
    error = ""
    for attempt in (1, 2):
        prompt = user if not error else f"{user}\n\nYour previous draft was rejected: {error}\nFix it."
        result = await complete_for_domain(
            conn, domain_id, system=system, user=prompt, schema=RuleAnswer, tier="smart"
        )
        data = {k: v for k, v in result.parsed.rule.items() if k not in ("id", "version")}
        try:
            data["teamId"] = str(team_id or await _resolve_team(conn, scope, data.get("teamId")))
            rule = parse_rule(data)
            compile_rule(rule)
        except RuleValidationError as exc:
            error = f"{exc.path}: {exc.message}"
            log.info("rule copilot draft invalid (attempt %d): %s", attempt, error)
            continue
        return rule, result.parsed.explanation.strip() or "Draft generated from your description."
    raise AIUnavailable(f"the model could not produce a valid rule: {error}")
