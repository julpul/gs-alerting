"""Email through SMTP (Mailpit in the demo). HTML with a plain-text alternative, runbook section and
ack / resolve deep links.
"""

from __future__ import annotations

from email.message import EmailMessage
from email.utils import make_msgid

import aiosmtplib

from app.config import get_settings
from app.delivery.channels._client import SEND_TIMEOUT_S
from app.delivery.channels.base import AlertView, ChannelError, Kind, Message, Recipient, SendResult
from app.delivery.channels.registry import register
from app.delivery.render import render_message


@register
class EmailAdapter:
    name = "email"

    def render(self, alert: AlertView, recipient: Recipient, kind: Kind) -> Message:
        return render_message("email", alert, recipient, kind)

    async def send(self, msg: Message, idempotency_key: str) -> SendResult:
        if not msg.to:
            raise ChannelError("recipient has no email address")
        settings = get_settings()
        mail = EmailMessage()
        mail["From"] = settings.smtp_from
        mail["To"] = msg.to
        mail["Subject"] = msg.subject
        mail["Message-ID"] = make_msgid(
            idstring=idempotency_key.replace(":", "."), domain="gs-alerting.local"
        )
        mail["X-Idempotency-Key"] = idempotency_key
        mail.set_content(msg.text)
        if msg.html:
            mail.add_alternative(msg.html, subtype="html")
        try:
            await aiosmtplib.send(
                mail, hostname=settings.smtp_host, port=settings.smtp_port, timeout=SEND_TIMEOUT_S
            )
        except (aiosmtplib.SMTPException, OSError) as exc:
            raise ChannelError(f"smtp: {exc}") from exc
        return SendResult(status="delivered", provider_id=str(mail["Message-ID"]))
