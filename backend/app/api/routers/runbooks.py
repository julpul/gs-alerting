"""Runbooks REST: list, create (steps or pasted content in any format) and parse preview (spec 11.1)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.api.deps import CurrentScope, CurrentUser, Pool
from app.runbooks import store
from app.runbooks.model import to_stored
from app.runbooks.parsers import parse_runbook
from app.schemas import api as wire

router = APIRouter(prefix="/v1/runbooks", tags=["runbooks"])


class ParseRequest(BaseModel):
    content: str
    format: str | None = "auto"


class RunbookIn(BaseModel):
    name: str | None = Field(default=None, max_length=200)
    format: str | None = None
    source: str | None = None
    steps: list[wire.RunbookStep] | None = None
    teamId: str | None = None
    attached: dict[str, Any] | None = None


@router.get("", response_model=list[wire.Runbook], response_model_exclude_none=True)
async def list_runbooks(scope: CurrentScope, pool: Pool) -> list[dict[str, Any]]:
    async with pool.acquire() as conn:
        return await store.list_runbooks(conn, scope)


@router.post("", status_code=201, response_model=wire.Runbook, response_model_exclude_none=True)
async def create_runbook(
    body: RunbookIn, scope: CurrentScope, user: CurrentUser, pool: Pool
) -> dict[str, Any]:
    async with pool.acquire() as conn:
        return await store.create_runbook(
            conn,
            scope,
            user,
            name=body.name,
            fmt=body.format,
            source=body.source,
            steps=body.steps,
            team_ref=body.teamId,
            attached=body.attached,
        )


@router.post("/parse", response_model=wire.Runbook, response_model_exclude_none=True)
async def parse(body: ParseRequest, user: CurrentUser) -> dict[str, Any]:
    """Draft only, nothing is stored. Format `auto` (default) detects YAML/JSON, Markdown, link, text."""
    parsed = await parse_runbook(body.content, body.format)
    return {
        "name": parsed.name,
        "format": parsed.format,
        "source": body.content,
        "steps": [to_stored(s) for s in parsed.steps],
        "attached": parsed.attached,
        "parsedBy": parsed.parsed_by,
    }
