"""In-app feed. No external call: the worker publishes the `delivery.status` SSE event for every delivery,
which is what the UI feed consumes, so a UI delivery is delivered immediately.
"""

from __future__ import annotations

from app.delivery.channels.base import AlertView, Kind, Message, Recipient, SendResult
from app.delivery.channels.registry import register
from app.delivery.render import render_message


@register
class UiAdapter:
    name = "ui"

    def render(self, alert: AlertView, recipient: Recipient, kind: Kind) -> Message:
        return render_message("ui", alert, recipient, kind)

    async def send(self, msg: Message, idempotency_key: str) -> SendResult:
        return SendResult(status="delivered")
