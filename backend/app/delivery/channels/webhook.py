"""Signed webhook: JSON body, `X-Signature: sha256=<hex HMAC-SHA256(secret, body)>`."""

from __future__ import annotations

import hashlib
import hmac

import httpx
import orjson

from app.config import get_settings
from app.delivery.channels._client import http_client
from app.delivery.channels.base import AlertView, ChannelError, Kind, Message, Recipient, SendResult
from app.delivery.channels.registry import register
from app.delivery.render import render_message


def sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


@register
class WebhookAdapter:
    name = "webhook"

    def render(self, alert: AlertView, recipient: Recipient, kind: Kind) -> Message:
        msg = render_message("webhook", alert, recipient, kind)
        return Message("webhook", msg.to, "", meta={**msg.meta, "secret": recipient.secret})

    async def send(self, msg: Message, idempotency_key: str) -> SendResult:
        if not msg.to:
            raise ChannelError("webhook has no URL")
        body = orjson.dumps({**msg.meta["payload"], "idempotencyKey": idempotency_key})
        secret = msg.meta.get("secret") or get_settings().app_secret
        headers = {
            "Content-Type": "application/json",
            "X-Signature": sign(secret, body),
            "Idempotency-Key": idempotency_key,
        }
        try:
            resp = await http_client().post(msg.to, content=body, headers=headers)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise ChannelError(f"webhook: {exc!r}") from exc
        return SendResult(status="sent")
