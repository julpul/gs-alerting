"""Herald UI: `/api/runbooks` (list, save pasted text in any format), `/api/runbooks/parse` (preview).

Parsing is ours (`app.runbooks.parsers`: YAML / Markdown / link deterministic, free text via the AI parser
with the heuristic splitter as fallback). Runbook id = our uuid; a runbook first saved under a Herald slug
remembers it (`attached.heraldId`), so saving the same slug again updates it.
"""

from __future__ import annotations

from typing import Any

import asyncpg
from pydantic import BaseModel

from app.api.deps import Pool
from app.core.errors import AppError, not_found
from app.core.ids import parse_uuid
from app.herald.dsl import scoped_rules
from app.herald.identity import Actor
from app.herald.routing import herald_router
from app.herald.runbook_view import parsed_wire, runbook_wire
from app.runbooks import store
from app.runbooks.parsers import parse_runbook
from app.schemas.internal import User

router = herald_router()
MAX_TEXT = 50_000


class RunbookText(BaseModel):
    text: str
    format: str | None = None


class RunbookIn(BaseModel):
    text: str
    format: str | None = None
    title: str | None = None


async def _used_by(conn: asyncpg.Connection, who: Any) -> dict[str, list[str]]:
    used: dict[str, list[str]] = {}
    for item in await scoped_rules(conn, who.scope):
        if item.rule.runbook_id:
            used.setdefault(str(item.rule.runbook_id), []).append(item.rule.name)
    return used


@router.get("/api/runbooks")
async def list_runbooks(who: Actor, pool: Pool) -> list[dict[str, Any]]:
    async with pool.acquire() as conn:
        runbooks = await store.list_runbooks(conn, who.scope)
        used = await _used_by(conn, who)
    return [runbook_wire(rb, used.get(rb["id"])) for rb in runbooks]


@router.post("/api/runbooks/parse")
async def parse(body: RunbookText, who: Actor) -> dict[str, Any]:
    """Any runbook text -> steps. Nothing is stored."""
    parsed = await parse_runbook(body.text, body.format or "auto")
    return parsed_wire(parsed, body.text)


async def _find(conn: asyncpg.Connection, who: Any, runbook_id: str) -> asyncpg.Record | None:
    uid = parse_uuid(runbook_id)
    if uid is not None:
        row = await conn.fetchrow("SELECT * FROM runbooks WHERE id = $1", uid)
    else:
        row = await conn.fetchrow("SELECT * FROM runbooks WHERE attached->>'heraldId' = $1", runbook_id)
    if row is None:
        return None
    if row["team_id"] is not None and not who.scope.can_see_team(row["team_id"]):
        raise not_found("runbook")
    return row


@router.put("/api/runbooks/{runbook_id}")
async def save_runbook(runbook_id: str, body: RunbookIn, who: Actor, pool: Pool) -> dict[str, Any]:
    """Save a runbook pasted in any format; rules pointing at it see the new steps at once."""
    if not body.text.strip() or len(body.text) > MAX_TEXT:
        raise AppError(422, "validation_error", "the runbook text is empty or too long")
    title = (body.title or "").strip()[:200] or None
    user = User(**who.user)
    async with pool.acquire() as conn:
        row = await _find(conn, who, runbook_id)
        if row is not None:
            saved = await store.update_runbook(
                conn, who.scope, user, row, name=title, fmt=body.format, source=body.text
            )
        else:
            if parse_uuid(runbook_id) is not None:
                raise not_found("runbook")
            saved = await store.create_runbook(
                conn,
                who.scope,
                user,
                name=title or runbook_id,
                fmt=body.format,
                source=body.text,
                steps=None,
                team_ref=who.teams[0].removeprefix("team:") if who.teams else None,
                attached={"heraldId": runbook_id},
            )
        used = await _used_by(conn, who)
    return runbook_wire(saved, used.get(saved["id"]))
