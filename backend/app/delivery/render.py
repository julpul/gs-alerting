"""Message rendering per channel. Alert content is untrusted: HTML output is always escaped and no
template engine is involved.
"""

from __future__ import annotations

import html
from typing import Any

from app.config import get_settings
from app.core.ids import format_short_id
from app.delivery.channels.base import AlertView, Kind, Message, Recipient

SEVERITY_TAG = {"critical": "CRIT", "error": "ERR", "warning": "WARN", "info": "INFO"}
SEVERITY_COLOR = {"critical": "#c62828", "error": "#e65100", "warning": "#f9a825", "info": "#1565c0"}
SMS_LIMIT = 160
ELLIPSIS = "..."


def _tag(alert: AlertView) -> str:
    tag = SEVERITY_TAG.get(alert.severity, alert.severity.upper())
    if alert.status == "resolved":
        return "RESOLVED"
    if alert.escalation_level > 1:
        return f"{tag} L{alert.escalation_level}"
    return tag


def alert_link(alert: AlertView, action: str | None = None) -> str:
    base = get_settings().public_base_url.rstrip("/")
    link = f"{base}/a/{alert.short_id}"
    return f"{link}?action={action}" if action else link


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: max(limit - len(ELLIPSIS), 0)].rstrip() + ELLIPSIS


def render_sms(alert: AlertView, kind: Kind) -> str:
    """`[CRIT] A-1234 Settlement failed validate-counterparty. Reply ACK 1234. {base}/a/1234` (<= 160)."""
    tail = f" {alert_link(alert)}" if kind == "soft" else f" Reply ACK {alert.short_id}. {alert_link(alert)}"
    head = f"[{_tag(alert)}] {format_short_id(alert.short_id)} "
    room = SMS_LIMIT - len(head) - len(tail)
    return head + _clip(alert.title, room) + tail


def render_phone(alert: AlertView) -> str:
    return (
        f"Alert {format_short_id(alert.short_id)}, severity {alert.severity}. "
        f"{_clip(alert.title, 120)}. Press 1 to acknowledge."
    )


def render_email(alert: AlertView, recipient: Recipient, kind: Kind) -> tuple[str, str, str]:
    """(subject, plain text, html)."""
    sid = format_short_id(alert.short_id)
    subject = f"[{_tag(alert)}] {sid} {_clip(alert.title, 120)}"
    steps = alert.runbook_steps
    field_lines = [f"{k}: {v}" for k, v in alert.fields.items()]

    text_parts = [
        f"{alert.title}",
        f"{sid} - severity {alert.severity}, status {alert.status}, occurrences {alert.count}",
    ]
    if alert.body:
        text_parts.append(alert.body)
    if field_lines:
        text_parts.append("\n".join(field_lines))
    text_parts.append(
        "Runbook: "
        + (alert.runbook_name or "")
        + ("\n" + "\n".join(f"{i}. {s}" for i, s in enumerate(steps, 1)))
        if steps
        else "Runbook: none attached yet."
    )
    if kind == "hard" and alert.status != "resolved":
        text_parts.append(f"Acknowledge: {alert_link(alert, 'ack')}")
    text_parts.append(f"Resolve: {alert_link(alert, 'resolve')}\nOpen: {alert_link(alert)}")

    e = html.escape
    color = SEVERITY_COLOR.get(alert.severity, "#455a64")
    rows = "".join(
        f"<tr><td><b>{e(str(k))}</b></td><td>{e(str(v))}</td></tr>" for k, v in alert.fields.items()
    )
    runbook_html = (
        f"<p><b>Runbook{': ' + e(alert.runbook_name) if alert.runbook_name else ''}</b></p><ol>"
        + "".join(f"<li>{e(s)}</li>" for s in steps)
        + "</ol>"
        if steps
        else "<p><b>Runbook</b>: none attached yet.</p>"
    )
    buttons = ""
    if kind == "hard" and alert.status != "resolved":
        buttons += (
            f'<a href="{e(alert_link(alert, "ack"))}" style="{_BUTTON}background:#2e7d32">Acknowledge</a> '
        )
    buttons += f'<a href="{e(alert_link(alert, "resolve"))}" style="{_BUTTON}background:#455a64">Resolve</a> '
    buttons += f'<a href="{e(alert_link(alert))}" style="{_BUTTON}background:#1565c0">Open</a>'
    html_body = (
        f'<div style="font-family:sans-serif">'
        f'<h2 style="color:{color}">[{e(_tag(alert))}] {e(alert.title)}</h2>'
        f"<p>{e(sid)} &middot; {e(alert.severity)} &middot; {e(alert.status)} &middot; x{alert.count}</p>"
        f"{('<p>' + e(alert.body) + '</p>') if alert.body else ''}"
        f"<table cellpadding='4'>{rows}</table>{runbook_html}<p>{buttons}</p></div>"
    )
    return subject, "\n\n".join(text_parts), html_body


_BUTTON = "padding:8px 14px;color:#fff;text-decoration:none;border-radius:4px;"


def render_webhook(alert: AlertView, kind: Kind) -> dict[str, Any]:
    return {
        "type": "alert.notification",
        "kind": kind,
        "alert": {
            "id": str(alert.id),
            "shortId": format_short_id(alert.short_id),
            "title": alert.title,
            "severity": alert.severity,
            "status": alert.status,
            "count": alert.count,
            "escalationLevel": alert.escalation_level,
            "fields": alert.fields,
            "url": alert_link(alert),
        },
    }


def render_message(channel: str, alert: AlertView, recipient: Recipient, kind: Kind) -> Message:
    """Dispatch used by the built-in adapters."""
    if channel == "email":
        subject, text, html_body = render_email(alert, recipient, kind)
        return Message("email", recipient.address, text, subject=subject, html=html_body)
    if channel == "sms":
        return Message("sms", recipient.address, render_sms(alert, kind))
    if channel == "phone":
        return Message("phone", recipient.address, render_phone(alert))
    if channel == "webhook":
        return Message("webhook", recipient.address, "", meta={"payload": render_webhook(alert, kind)})
    return Message("ui", None, _clip(alert.title, 200), subject=alert.title)
