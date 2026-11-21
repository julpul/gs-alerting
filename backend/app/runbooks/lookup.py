"""Which runbook applies to an alert, and what its steps look like for that alert.

Lookup order (spec 11.1): the rule's runbook (`runbookId`), runbooks attached to the rule id, runbooks whose
`attached.matches` (`{type: WORKFLOW_FAILED, "error.code": TIMEOUT}`, values may use `*` globs) match the
event, then runbooks attached to the alert's schema / type. Runbooks of another team are never considered
unless the rule points at them.
"""

from __future__ import annotations

import fnmatch
import re
from datetime import UTC, datetime
from typing import Any

import asyncpg

from app.pipeline.fields import MISSING, EventView
from app.pipeline.mapping import apply_mapping
from app.pipeline.rules.templates import render
from app.pipeline.schema_detect import detect_schema
from app.runbooks.actions.registry import default_risk
from app.runbooks.model import RunbookView, from_stored
from app.runbooks.store import split_attached
from app.runbooks.trust import Trust, get_trust
from app.schemas import api as wire

MAX_RENDER = 2000
_GUARD = re.compile(r"^\s*previous\.failures\s*(>=|>|==)\s*(\d+)\s*$")


async def build_event_view(conn: asyncpg.Connection, alert: dict[str, Any]) -> EventView:
    """The alert's raw event with a recomputed `_meta` (ingest mapping rules, no per-source mapping)."""
    raw = alert.get("raw_event")
    if raw is None and "raw_event" not in alert:
        raw = await conn.fetchval("SELECT raw_event FROM alerts WHERE id = $1", alert["id"])
    raw = raw if isinstance(raw, dict) else {}
    source_name = ""
    if alert.get("source_id"):
        source_name = await conn.fetchval("SELECT name FROM sources WHERE id = $1", alert["source_id"]) or ""
    meta = apply_mapping(
        raw,
        None,
        schema=detect_schema(raw),
        source_name=source_name,
        default_severity=alert.get("severity") or "warning",
        received_at=alert.get("first_seen_at") or datetime.now(UTC),
    )
    meta["flags"] = list(alert.get("flags") or [])
    return EventView(raw=raw, meta=meta)


def _value(ev: EventView, key: str) -> Any:
    found = ev.lookup(key)
    return ev.meta.get(key, MISSING) if found is MISSING else found


def _equals(expected: Any, actual: Any) -> bool:
    if actual is MISSING or actual is None:
        return False
    if isinstance(expected, list):
        return any(_equals(e, actual) for e in expected)
    want, got = str(expected), str(actual)
    return fnmatch.fnmatchcase(got, want) if any(c in want for c in "*?") else want == got


def matches(attached: dict[str, Any], ev: EventView) -> bool:
    conditions = attached.get("matches") or {}
    return bool(conditions) and all(_equals(v, _value(ev, k)) for k, v in conditions.items())


def _by_attachment(rows: list[asyncpg.Record], alert: dict[str, Any], ev: EventView) -> asyncpg.Record | None:
    rule_id = str(alert["rule_id"]) if alert.get("rule_id") else None
    schema = ev.meta.get("schema")
    kind = ev.meta.get("type")
    tiers: list[list[tuple[int, asyncpg.Record]]] = [[], [], []]
    for row in rows:
        attached = row["attached"] or {}
        if rule_id and rule_id in [str(i) for i in attached.get("ruleIds") or []]:
            tiers[0].append((0, row))
        elif attached.get("matches") and matches(attached, ev):
            tiers[1].append((-len(attached["matches"]), row))
        elif (attached.get("schema") and attached["schema"] == schema) or (
            attached.get("type") and attached["type"] == kind
        ):
            tiers[2].append((0, row))
    for tier in tiers:
        if tier:
            return min(tier, key=lambda t: (t[0], t[1]["name"]))[1]
    return None


async def find_runbook(
    conn: asyncpg.Connection, alert: dict[str, Any], ev: EventView | None = None
) -> asyncpg.Record | None:
    runbook_id = alert.get("runbook_id")
    if runbook_id is None and alert.get("rule_id"):
        runbook_id = await conn.fetchval(
            "SELECT NULLIF(definition->>'runbookId', '')::uuid FROM rules WHERE id = $1", alert["rule_id"]
        )
    if runbook_id is not None:
        row = await conn.fetchrow("SELECT * FROM runbooks WHERE id = $1", runbook_id)
        if row is not None:
            return row
    rows = await conn.fetch(
        "SELECT * FROM runbooks WHERE team_id IS NULL OR team_id = $1 ORDER BY name LIMIT 500",
        alert["team_id"],
    )
    if not rows:
        return None
    return _by_attachment(rows, alert, ev or await build_event_view(conn, alert))


def render_value(value: Any, ev: EventView) -> Any:
    if isinstance(value, str):
        return render(value, ev, max_len=MAX_RENDER)
    if isinstance(value, dict):
        return {k: render_value(v, ev) for k, v in value.items()}
    if isinstance(value, list):
        return [render_value(v, ev) for v in value]
    return value


def guard_met(guard: str | None, previous_failures: int) -> bool | None:
    if not guard:
        return None
    m = _GUARD.match(guard)
    if m is None:
        return None
    op, n = m.group(1), int(m.group(2))
    return {">=": previous_failures >= n, ">": previous_failures > n, "==": previous_failures == n}[op]


async def step_states(conn: asyncpg.Connection, alert_id: Any) -> dict[str, asyncpg.Record]:
    rows = await conn.fetch(
        "SELECT DISTINCT ON (step_id) step_id, state, approval_id, output, run_by, at FROM step_runs "
        "WHERE alert_id = $1 ORDER BY step_id, at DESC, id DESC",
        alert_id,
    )
    return {r["step_id"]: r for r in rows}


async def failure_counts(conn: asyncpg.Connection, alert_id: Any) -> dict[str, int]:
    rows = await conn.fetch(
        "SELECT step_id, count(*) AS n FROM step_runs "
        "WHERE alert_id = $1 AND state = 'failed' GROUP BY step_id",
        alert_id,
    )
    return {r["step_id"]: r["n"] for r in rows}


def _actor_wire(stored: dict[str, Any] | None) -> dict[str, Any]:
    stored = stored or {}
    return {
        "kind": stored.get("type") or stored.get("kind") or "system",
        "id": str(stored.get("id") or "system"),
        "displayName": stored.get("name") or stored.get("displayName") or "system",
        "onBehalfOf": stored.get("onBehalfOf"),
    }


def render_step(
    stored: dict[str, Any],
    ev: EventView,
    *,
    state: asyncpg.Record | None,
    trust: Trust | None,
    previous_failures: int,
) -> wire.RunbookStep:
    step = from_stored(stored)
    data = step.model_dump(mode="json", exclude_none=True)
    data["text"] = render(step.text or "", ev, max_len=MAX_RENDER)
    data["parameters"] = render_value(step.parameters or {}, ev)
    if step.url:
        data["url"] = render(step.url, ev, max_len=MAX_RENDER)
    data["state"] = state["state"] if state else "not_started"
    if step.action and not step.risk:
        data["risk"] = default_risk(step.action)
    if state and state["approval_id"]:
        data["approvalId"] = str(state["approval_id"])
    if state and state["state"] != "not_started":
        data["lastRun"] = {
            "by": _actor_wire(state["run_by"]),
            "at": state["at"],
            "output": state["output"] or "",
        }
    if trust is not None and step.action:
        data["trustLevel"] = trust.level
    met = guard_met(step.guard, previous_failures)
    if met is not None:
        data["guardMet"] = met
    return wire.RunbookStep.model_validate(data)


async def render_runbook(
    conn: asyncpg.Connection, row: asyncpg.Record, alert: dict[str, Any], ev: EventView
) -> RunbookView:
    states = await step_states(conn, alert["id"])
    failures = await failure_counts(conn, alert["id"])
    trusts: dict[str, Trust] = {}
    steps: list[wire.RunbookStep] = []
    previous_id: str | None = None
    for stored in row["steps"] or []:
        action = stored.get("action")
        if action and action not in trusts:
            trusts[action] = await get_trust(conn, alert["domain_id"], action)
        steps.append(
            render_step(
                stored,
                ev,
                state=states.get(stored.get("id")),
                trust=trusts.get(action) if action else None,
                previous_failures=failures.get(previous_id, 0) if previous_id else 0,
            )
        )
        previous_id = stored.get("id")
    return RunbookView(
        id=str(row["id"]),
        name=row["name"],
        format=row["format"],
        source=row["source"],
        steps=steps,
        teamId=str(row["team_id"]) if row["team_id"] else None,
        attached=split_attached(row["attached"])[0],
        parsedBy=split_attached(row["attached"])[1],
    )


async def runbook_for_alert(conn: asyncpg.Connection, alert: dict[str, Any]) -> RunbookView | None:
    """`alert` is an `alerts` row as dict. Never mutates; safe to call from the delivery worker."""
    ev = await build_event_view(conn, alert)
    row = await find_runbook(conn, alert, ev)
    if row is None:
        return None
    return await render_runbook(conn, row, alert, ev)
