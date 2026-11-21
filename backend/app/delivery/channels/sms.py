"""SMS through the gateway (`POST {MOCKGW_URL}/sms`). The `Idempotency-Key` header makes redelivery safe."""

from __future__ import annotations

import httpx

from app.config import get_settings
from app.delivery.channels._client import http_client
from app.delivery.channels.base import AlertView, ChannelError, Kind, Message, Recipient, SendResult
from app.delivery.channels.registry import register
from app.delivery.render import render_message


async def post_gateway(path: str, body: dict, idempotency_key: str) -> SendResult:
    """POST to the SMS/voice gateway; any HTTP error (including 503 during an outage) raises ChannelError."""
    url = f"{get_settings().mockgw_url.rstrip('/')}{path}"
    try:
        resp = await http_client().post(url, json=body, headers={"Idempotency-Key": idempotency_key})
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        raise ChannelError(f"gateway {path}: {exc!r}") from exc
    return SendResult(status="sent", provider_id=str(resp.json().get("id")))


@register
class SmsAdapter:
    name = "sms"

    def render(self, alert: AlertView, recipient: Recipient, kind: Kind) -> Message:
        return render_message("sms", alert, recipient, kind)

    async def send(self, msg: Message, idempotency_key: str) -> SendResult:
        if not msg.to:
            raise ChannelError("recipient has no phone number")
        return await post_gateway("/sms", {"to": msg.to, "text": msg.text}, idempotency_key)
