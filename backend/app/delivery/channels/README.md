# Channel adapters

A channel adapter turns an alert into a provider message (`render`) and hands it to the provider
(`send`). Built in: `ui`, `email` (SMTP / Mailpit), `sms` and `phone` (mockgw), `webhook` (HMAC).

## Add a real provider

1. Create `app/delivery/channels/<name>.py` with **one class** decorated with `@register`.
2. Set `name` to the channel it serves. A new provider for an existing channel (for example Twilio for
   `sms`) simply registers under the same name; the last registration wins, so import order or an env
   switch decides which one is active.
3. `send` must raise (any exception, `ChannelError` preferred) when the provider refused or timed out.
   The delivery worker enforces a 3 s timeout, retries after 5 s / 30 s, and after 3 failures dead-letters
   and falls back along `phone -> sms -> email`.
4. Pass `idempotency_key` to the provider if it supports it (Twilio does not, so the worker's
   "skip if already sent" check is the guard; mockgw honours the `Idempotency-Key` header).
5. Add the credentials to `.env` / `app/config.py`. No other file changes: modules in this package are
   imported automatically by `registry.load_adapters()`.

## Sketch: Twilio SMS (about 40 lines)

```python
from __future__ import annotations

import httpx

from app.config import get_settings
from app.delivery.channels._client import http_client
from app.delivery.channels.base import AlertView, ChannelError, Kind, Message, Recipient, SendResult
from app.delivery.channels.registry import register
from app.delivery.render import render_message


@register
class TwilioSmsAdapter:
    name = "sms"

    def render(self, alert: AlertView, recipient: Recipient, kind: Kind) -> Message:
        return render_message("sms", alert, recipient, kind)  # already <= 160 chars

    async def send(self, msg: Message, idempotency_key: str) -> SendResult:
        s = get_settings()  # add twilio_account_sid, twilio_auth_token, twilio_from to Settings
        url = f"https://api.twilio.com/2010-04-01/Accounts/{s.twilio_account_sid}/Messages.json"
        try:
            resp = await http_client().post(
                url,
                auth=(s.twilio_account_sid, s.twilio_auth_token),
                data={"To": msg.to, "From": s.twilio_from, "Body": msg.text},
            )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise ChannelError(f"twilio: {exc!r}") from exc
        return SendResult(status="sent", provider_id=resp.json()["sid"])
```

## Message rendering

`app/delivery/render.py` holds the per-channel formats (SMS <= 160 characters, HTML + text email with
runbook section and ack / resolve links). Alert content is untrusted, so HTML is always escaped.
