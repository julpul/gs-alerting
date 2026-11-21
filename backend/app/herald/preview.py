"""`POST /api/rules/preview`: how a draft rule looks for one event, rendered by the code that really sends.

Title/body/severity come from `app.pipeline.present.build_alert`, the SMS / phone / email texts from
`app.delivery.render`, who would be told from `app.delivery.recipients` + `channel_select` + the effective
escalation policy (recipient preferences are not consulted, so this is the rule's own choice).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import asyncpg

from app.delivery.channel_select import select_channels
from app.delivery.channels.base import AlertView, Recipient
from app.delivery.recipients import resolve_specs
from app.delivery.render import render_email, render_phone, render_sms
from app.delivery.ruleview import rule_view
from app.escalation.policies import effective_policy
from app.herald.dsl import Directory, field_to_herald
from app.herald.runbook_view import steps_wire
from app.pipeline.envelope import build_envelope
from app.pipeline.fields import EventView
from app.pipeline.present import build_alert
from app.pipeline.rules.compiler import compile_rule
from app.pipeline.rules.model import RuleDef
from app.pipeline.rules.templates import render
from app.repositories.sources import team_domain
from app.runbooks.lookup import render_step

PREVIEW_SHORT_ID = 123
COLOURS = {"critical": "red", "error": "orange", "warning": "amber", "info": "blue"}


async def _runbook(conn: asyncpg.Connection, rule: RuleDef, ev: EventView) -> dict[str, Any] | None:
    try:
        runbook_id = UUID(str(rule.runbook_id)) if rule.runbook_id else None
    except ValueError:
        return None
    row = await conn.fetchrow("SELECT * FROM runbooks WHERE id = $1", runbook_id) if runbook_id else None
    if row is None:
        return None
    steps = [
        render_step(s, ev, state=None, trust=None, previous_failures=0).model_dump(
            mode="json", exclude_none=True
        )
        for s in row["steps"] or []
    ]
    link = (row["source"] or "").strip() if row["format"] == "link" else None
    return {
        "id": str(row["id"]),
        "title": row["name"],
        "link": link,
        "text": row["source"],
        "steps": steps_wire(steps),
    }


async def _notify(
    conn: asyncpg.Connection, rule: RuleDef, severity: str, directory: Directory
) -> list[dict[str, str]]:
    domain_id = await team_domain(conn, rule.team_id)
    if domain_id is None:
        return []
    rv = rule_view(rule)
    policy = await effective_policy(
        conn, rule=rule, team_id=rule.team_id, domain_id=domain_id, severity=severity
    )
    now = datetime.now(UTC)
    out: list[dict[str, str]] = []
    seen: set[UUID] = set()
    for kind, specs in (("hard", rv.hard), ("soft", rv.soft)):
        for person in await resolve_specs(conn, specs, team_id=rule.team_id, domain_id=domain_id, at=now):
            if person.id in seen:
                continue
            seen.add(person.id)
            channels = select_channels(
                kind=kind, severity=severity, rule=rv, prefs=None, policy=policy, level=1, now=now
            )
            ref = f"person:{directory.person_of(person.email)}"
            out += [{"recipient": ref, "channel": c, "kind": kind} for c in channels]
    return out


async def preview(
    conn: asyncpg.Connection,
    rule: RuleDef,
    ev: EventView,
    *,
    highlight: list[str],
    directory: Directory,
) -> dict[str, Any]:
    compiled = compile_rule(rule)
    envelope = build_envelope(source_id=UUID(int=0), raw=ev.raw, priority=3)
    rendered, _ = build_alert(rule, ev, envelope, render=render, domain_id=UUID(int=0))
    severity = rendered.severity
    runbook = await _runbook(conn, rule, ev)
    view = AlertView(
        id=UUID(int=0),
        short_id=PREVIEW_SHORT_ID,
        title=rendered.title,
        body=rendered.body or "",
        severity=severity,
        status="open",
        count=1,
        escalation_level=1,
        team_id=rule.team_id,
        fields={f["label"]: f["value"] for f in rendered.fields},
        runbook_name=runbook["title"] if runbook else None,
        runbook_steps=[s["text"] for s in runbook["steps"]] if runbook else [],
    )
    _, email_text, _ = render_email(view, Recipient(name="preview", address=None), "hard")
    return {
        "matches": compiled.matches(ev),
        "severity": severity,
        "colour": rendered.color or COLOURS.get(severity, "grey"),
        "title": rendered.title,
        "body": rendered.body or "",
        "fields": [
            {"path": field_to_herald(p), "value": ev.get(p), "highlight": field_to_herald(p) in highlight}
            for p in rule.presentation.fields
        ],
        "sms": render_sms(view, "hard"),
        "voice": render_phone(view),
        "email": email_text,
        "notify": await _notify(conn, rule, severity, directory),
        "runbook": runbook,
    }
