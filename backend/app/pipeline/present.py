"""Matched rule + event -> what the alert looks like (`RenderedAlert`) and its fingerprint (spec 7.4, 8)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from uuid import UUID

from app.pipeline.alert_writer import RenderedAlert
from app.pipeline.fields import EventView
from app.pipeline.fingerprint import compute_fingerprint
from app.pipeline.ruleview import cfg

Renderer = Callable[..., str]

DEFAULT_GROUP_WINDOW_SECONDS = 300
TITLE_FALLBACK_PATHS = ("title", "message", "msg", "summary")
MAX_TITLE = 500


def _title(rule: Any, ev: EventView, render: Renderer) -> str:
    template = cfg(rule, "presentation.title_template")
    title = render(template, ev).strip() if template else ""
    if not title:
        title = ev.meta.get("title") or next(
            (v for p in TITLE_FALLBACK_PATHS if isinstance(v := ev.get(p), str) and v.strip()), ""
        )
    return (title or f"{ev.meta.get('schema', 'event')} from {ev.meta.get('source', 'unknown')}")[:MAX_TITLE]


def _body(rule: Any, ev: EventView, render: Renderer, typed: bool) -> str | None:
    template = cfg(rule, "presentation.body_template")
    if template:
        return render(template, ev).strip() or None
    description = ev.get("description") if typed else None
    return description if isinstance(description, str) and description.strip() else None


def build_alert(
    rule: Any,
    ev: EventView,
    envelope: dict[str, Any],
    *,
    render: Renderer,
    domain_id: UUID,
    now: float | None = None,
) -> tuple[RenderedAlert, str]:
    """Render title/body/fields, pick severity and compute the fingerprint (+ grouping window)."""
    typed = bool(envelope.get("typed"))
    title = _title(rule, ev, render)
    fields = []
    for path in cfg(rule, "presentation.fields", []):
        value = render("{{" + path + "}}", ev).strip()
        if value:
            fields.append({"label": path, "value": value})

    severity = ev.meta.get("severity") or "warning"
    if not typed and cfg(rule, "priority"):
        severity = cfg(rule, "priority")

    dedup_fields = cfg(rule, "dedup.fields", [])
    values: list[Any] = [ev.get(f) for f in dedup_fields] if dedup_fields else [title]
    if typed and ev.get("dedupKey"):
        values = [ev.get("dedupKey")]

    flags = list(ev.meta.get("flags", []))
    if typed:
        flags.append("from_agent")
    by_field = cfg(rule, "grouping.by_field")
    group_value = window = grouped_item = None
    if by_field:
        window = cfg(rule, "grouping.window_seconds") or DEFAULT_GROUP_WINDOW_SECONDS
        group_value = ev.get(by_field)
        flags.append("grouped")
        grouped_item = {
            "ts": ev.meta.get("timestamp"),
            "eventId": envelope["event_id"],
            by_field: group_value,
            "title": ev.meta.get("title"),
        }

    fingerprint = compute_fingerprint(
        cfg(rule, "id"), values, group_value=group_value, window_seconds=window, now=now
    )
    rendered = RenderedAlert(
        title=title,
        body=_body(rule, ev, render, typed),
        severity=severity,
        color=cfg(rule, "presentation.color"),
        fields=fields,
        flags=flags,
        domain_id=domain_id,
        grouped_item=grouped_item,
    )
    return rendered, fingerprint
