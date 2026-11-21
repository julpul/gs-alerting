"""Herald UI: `/api/runbooks` (list, save pasted text in any format), `/api/runbooks/parse` (preview).

Parsing is ours (`app.runbooks.parsers`: YAML / Markdown / link deterministic, free text via the AI parser
with the heuristic splitter as fallback). `ai: false` asks for the fixed rules only ("Split into steps"),
`ai: true` reads the text as free text with the AI parser ("Write with AI"; still falls back to the splitter
when the model does not answer, `source` then says `heuristic`). An AI preview or `draft: true` also
returns `draft_steps` (our step shape); sending them back on save stores exactly the reviewed steps.
Runbook id = our uuid; a runbook first saved under a Herald slug remembers it (`attached.heraldId`), so
saving the same slug again updates it. A runbook saved from a reviewed draft remembers who accepted it and the
file it came from (`attached.acceptedBy`, `attached.sourceFile`).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import asyncpg
from pydantic import BaseModel

from app.ai.runbook_parse import split_plain
from app.api.deps import Pool
from app.core.errors import AppError, not_found
from app.core.ids import parse_uuid
from app.herald.dsl import scoped_rules
from app.herald.identity import Actor
from app.herald.routing import herald_router
from app.herald.runbook_view import parsed_wire, runbook_wire
from app.runbooks import store
from app.runbooks.parsers import parse_runbook
from app.schemas import api as wire
from app.schemas.internal import User

router = herald_router()
MAX_TEXT = 50_000


class RunbookText(BaseModel):
    text: str
    format: str | None = None
    ai: bool | None = None
    draft: bool | None = None


class RunbookIn(BaseModel):
    text: str
    format: str | None = None
    title: str | None = None
    steps: list[wire.RunbookStep] | None = None
    ai: bool | None = None
    source_file: str | None = None


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
    if body.ai:
        parsed = await parse_runbook(body.text, "text", use_ai=True)
    else:
        parsed = await parse_runbook(body.text, body.format or "auto", use_ai=body.ai is None)
    out = parsed_wire(parsed, body.text)
    if body.ai and _model_did_not_answer(parsed, body.text):
        out.update(parsed_by="parser", source="heuristic")
    if body.ai or body.draft:
        out["draft_steps"] = [s.model_dump(mode="json", exclude_none=True) for s in parsed.steps]
    return out


def _model_did_not_answer(parsed: Any, text: str) -> bool:
    """The AI parser degrades to plain list items when the model fails and still says `ai`: spot that."""
    if parsed.parsed_by != "ai":
        return True
    plain = [p.text for p in split_plain(text)]
    return not any(s.action for s in parsed.steps) and [s.text for s in parsed.steps] == plain


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


def _accepted(body: RunbookIn, user: User) -> dict[str, Any]:
    out: dict[str, Any] = {"acceptedBy": user.display_name, "acceptedAt": datetime.now(UTC).isoformat()}
    if body.source_file:
        out["sourceFile"] = body.source_file.strip()[:200]
    return out


@router.put("/api/runbooks/{runbook_id}")
async def save_runbook(runbook_id: str, body: RunbookIn, who: Actor, pool: Pool) -> dict[str, Any]:
    """Save a runbook pasted in any format; rules pointing at it see the new steps at once."""
    if not body.text.strip() or len(body.text) > MAX_TEXT:
        raise AppError(422, "validation_error", "the runbook text is empty or too long")
    title = (body.title or "").strip()[:200] or None
    user = User(**who.user)
    async with pool.acquire() as conn:
        row = await _find(conn, who, runbook_id)
        fmt = "text" if body.ai else body.format
        accepted = _accepted(body, user) if body.steps else {}
        if row is not None:
            saved = await store.update_runbook(
                conn,
                who.scope,
                user,
                row,
                name=title,
                fmt=fmt,
                source=body.text,
                steps=body.steps or None,
                attached={**accepted, **({"parsedBy": "ai"} if body.ai and body.steps else {})},
            )
        else:
            if parse_uuid(runbook_id) is not None:
                raise not_found("runbook")
            saved = await store.create_runbook(
                conn,
                who.scope,
                user,
                name=title or runbook_id,
                fmt=fmt,
                source=body.text,
                steps=body.steps or None,
                team_ref=who.teams[0].removeprefix("team:") if who.teams else None,
                attached={
                    "heraldId": runbook_id,
                    **accepted,
                    **({"parsedBy": "ai"} if body.ai and body.steps else {}),
                },
            )
        used = await _used_by(conn, who)
    return runbook_wire(saved, used.get(saved["id"]))
