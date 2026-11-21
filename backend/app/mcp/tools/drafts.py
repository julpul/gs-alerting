"""Draft tools: AI proposals and dry-runs. Nothing here is ever saved (spec 13.1.2)."""

import logging
from typing import Annotated, Any

from pydantic import Field

from app.ai.mapping import suggest_mapping
from app.ai.rule_copilot import draft_rule
from app.core.errors import AppError, forbidden, not_found
from app.core.ids import parse_uuid
from app.mcp.context import ToolContext
from app.mcp.registry import tool
from app.pipeline.mapping_heuristic import suggest_mapping_heuristic
from app.pipeline.rules.compiler import compile_rule
from app.pipeline.rules.dryrun import dry_run
from app.pipeline.rules.model import RuleValidationError, parse_rule
from app.repositories import sources as sources_repo
from app.repositories.rules import resolve_team
from app.runbooks.model import to_stored
from app.runbooks.parsers import parse_runbook

log = logging.getLogger(__name__)


def _ai_unavailable(exc: Exception) -> AppError:
    log.warning("AI draft failed: %s", exc)
    return AppError(503, "ai_unavailable", "AI drafting is not available right now")


@tool()
async def propose_rule(
    tc: ToolContext, text: Annotated[str, Field(min_length=3, max_length=4000)], team: str
) -> dict:
    """Draft an alert rule from plain text for a team (slug or id) and dry-run it on recent events.
    The draft is NOT saved: show it to the user, who saves it in the UI."""
    async with tc.pool.acquire() as conn:
        team_id = await resolve_team(conn, team)
        if team_id is None or not tc.scope.can_see_team(team_id):
            raise forbidden("team not in your scope")
        try:
            draft, explanation = await draft_rule(text, tc.scope, conn, team_id=team_id)
        except AppError:
            raise
        except Exception as exc:
            raise _ai_unavailable(exc) from exc
        result = await _dry_run(conn, tc, draft)
    return {"status": "draft", "draft": draft.to_wire(), "explanation": explanation, "dryRun": result}


async def _dry_run(conn: Any, tc: ToolContext, rule: Any) -> dict[str, Any]:
    try:
        return await dry_run(conn, tc.redis, rule)
    except RuleValidationError as exc:
        raise AppError(
            422, "invalid_rule", f"{exc.path}: {exc.message}".lstrip(": "), {"path": exc.path}
        ) from exc


@tool()
async def test_rule(
    tc: ToolContext, rule: Annotated[dict[str, Any], Field(description="rule definition (camelCase JSON)")]
) -> dict:
    """Dry-run a rule definition against the last hour of events of its team's sources. Nothing is saved."""
    data = {k: v for k, v in rule.items() if k not in ("id", "version")}
    async with tc.pool.acquire() as conn:
        try:
            ref = data.pop("team_id", None) or data.get("teamId")
            if ref is not None:
                team_id = await resolve_team(conn, str(ref))
                if team_id is None:
                    raise RuleValidationError("teamId", "unknown team")
                data["teamId"] = str(team_id)
            parsed = parse_rule(data)
            compile_rule(parsed)
        except RuleValidationError as exc:
            raise AppError(
                422, "invalid_rule", f"{exc.path}: {exc.message}".lstrip(": "), {"path": exc.path}
            ) from exc
        if not tc.scope.can_see_team(parsed.team_id):
            raise forbidden("team not in your scope")
        return await _dry_run(conn, tc, parsed)


@tool()
async def propose_schema_mapping(tc: ToolContext, source_id: str, sample: dict[str, Any]) -> dict:
    """Suggest how a source's JSON sample maps to severity/service/title/...; a draft, not applied."""
    ident = parse_uuid(source_id)
    async with tc.pool.acquire() as conn:
        row = await sources_repo.get_source(conn, tc.scope, ident) if ident else None
    if row is None:
        raise not_found("source")
    source = {k: row[k] for k in ("id", "name", "type", "preset", "default_severity")}
    try:
        draft = await suggest_mapping(sample, source)
    except Exception as exc:
        log.warning("AI mapping suggestion failed, using the heuristic: %s", exc)
        draft = suggest_mapping_heuristic(sample)
    return {**draft, "status": "draft"}


@tool()
async def propose_runbook(
    tc: ToolContext, text: Annotated[str, Field(min_length=1, max_length=20000)]
) -> dict:
    """Turn runbook text (YAML, Markdown, free text or a link) into structured steps; a draft, not saved."""
    parsed = await parse_runbook(text, "auto")
    return {
        "status": "draft",
        "name": parsed.name,
        "format": parsed.format,
        "steps": [to_stored(s) for s in parsed.steps],
        "attached": parsed.attached,
        "parsedBy": parsed.parsed_by,
    }
