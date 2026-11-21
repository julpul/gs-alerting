"""`POST /v1/inbound/sms`: replies from the SMS gateway (spec 5.1)."""

from __future__ import annotations

import hmac
from typing import Annotated

from fastapi import APIRouter, Header
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import Pool, RabbitDep
from app.config import get_settings
from app.core.errors import unauthorized
from app.escalation.inbound import handle_sms

router = APIRouter(prefix="/v1/inbound", tags=["ingest"])


class InboundSms(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    sender: str = Field(alias="from", min_length=1, max_length=64)
    text: str = Field(max_length=2000)


class InboundReply(BaseModel):
    reply: str


@router.post("/sms", response_model=InboundReply)
async def inbound_sms(
    body: InboundSms,
    pool: Pool,
    rabbit: RabbitDep,
    x_inbound_secret: Annotated[str | None, Header()] = None,
) -> InboundReply:
    secret = get_settings().inbound_sms_secret
    if secret and not hmac.compare_digest((x_inbound_secret or "").encode(), secret.encode()):
        raise unauthorized("invalid or missing X-Inbound-Secret")
    return InboundReply(reply=await handle_sms(pool, rabbit, body.sender, body.text))
