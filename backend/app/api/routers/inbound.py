"""`POST /v1/inbound/sms`: replies from the SMS gateway (spec 5.1)."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import Pool, RabbitDep
from app.escalation.inbound import handle_sms

router = APIRouter(prefix="/v1/inbound", tags=["ingest"])


class InboundSms(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    sender: str = Field(alias="from", min_length=1, max_length=64)
    text: str = Field(max_length=2000)


class InboundReply(BaseModel):
    reply: str


@router.post("/sms", response_model=InboundReply)
async def inbound_sms(body: InboundSms, pool: Pool, rabbit: RabbitDep) -> InboundReply:
    return InboundReply(reply=await handle_sms(pool, rabbit, body.sender, body.text))
