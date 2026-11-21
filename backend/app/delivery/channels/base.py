"""Channel adapter contract (spec 9.3) and the value types shared by all adapters."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol
from uuid import UUID

Kind = Literal["hard", "soft"]


class ChannelError(Exception):
    """Raised by `send` when the provider refused or could not be reached; triggers retry / DLQ."""


@dataclass(frozen=True)
class AlertView:
    """The slice of an alert (plus its runbook) that messages are rendered from."""

    id: UUID
    short_id: int
    title: str
    body: str
    severity: str
    status: str
    count: int
    escalation_level: int
    team_id: UUID
    fields: dict[str, Any] = field(default_factory=dict)
    runbook_name: str | None = None
    runbook_steps: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Recipient:
    """Who a delivery is for. `address` is channel specific (email address, phone number, webhook URL)."""

    name: str
    address: str | None
    user_id: UUID | None = None
    secret: str | None = None


@dataclass(frozen=True)
class Message:
    channel: str
    to: str | None
    text: str
    subject: str = ""
    html: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SendResult:
    status: Literal["sent", "delivered"] = "delivered"
    provider_id: str | None = None


class ChannelAdapter(Protocol):
    name: str

    def render(self, alert: AlertView, recipient: Recipient, kind: Kind) -> Message: ...

    async def send(self, msg: Message, idempotency_key: str) -> SendResult:
        """Deliver the message. Must raise (any exception) on failure; the worker enforces a 3 s timeout."""
        ...
