"""`POST /v1/agent/chat`: the in-app copilot, streamed as Server-Sent Events (spec 14)."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.api.deps import CurrentScope, CurrentUser, Pool, RabbitDep, RedisDep
from app.mcp.context import ToolContext, copilot_principal
from app.mcp.copilot import MAX_MESSAGE_CHARS, MAX_MESSAGES, run_copilot

router = APIRouter(prefix="/v1/agent", tags=["stream"])


class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: Annotated[str, Field(max_length=MAX_MESSAGE_CHARS * 4)]


class ChatRequest(BaseModel):
    messages: Annotated[list[ChatMessage], Field(min_length=1, max_length=MAX_MESSAGES * 2)]
    alertId: str | None = None
    context: dict[str, Any] | None = None


@router.post("/chat")
async def chat(
    body: ChatRequest,
    user: CurrentUser,
    scope: CurrentScope,
    pool: Pool,
    rabbit: RabbitDep,
    redis: RedisDep,
) -> StreamingResponse:
    alert_id = body.alertId or (body.context or {}).get("alertId")
    tc = ToolContext(pool, rabbit, redis, copilot_principal(user, scope))
    frames = run_copilot(tc, [m.model_dump() for m in body.messages], str(alert_id) if alert_id else None)
    return StreamingResponse(
        frames,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
