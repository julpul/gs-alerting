"""Voice call through the gateway (`POST {MOCKGW_URL}/call`); the gateway page accepts "press 1" = ACK."""

from __future__ import annotations

from app.delivery.channels.base import AlertView, ChannelError, Kind, Message, Recipient, SendResult
from app.delivery.channels.registry import register
from app.delivery.channels.sms import post_gateway
from app.delivery.render import render_message


@register
class PhoneAdapter:
    name = "phone"

    def render(self, alert: AlertView, recipient: Recipient, kind: Kind) -> Message:
        return render_message("phone", alert, recipient, kind)

    async def send(self, msg: Message, idempotency_key: str) -> SendResult:
        if not msg.to:
            raise ChannelError("recipient has no phone number")
        return await post_gateway("/call", {"to": msg.to, "text": msg.text}, idempotency_key)
