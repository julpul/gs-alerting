"""Shape translation: our rows / wire models -> the JSON the Herald UI reads (`ui-ads/src/api.ts`).

Pure functions only (no I/O): callers load rows and the id maps (`app/herald/ids.py`) and pass them in.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any
from uuid import UUID

import orjson

from app.core.ids import format_short_id, parse_uuid
from app.herald.ids import actor_ref, herald_status, owner_ref
from app.pipeline.fields import MISSING, EventView
from app.pipeline.rules.compiler import _compile_condition
from app.pipeline.rules.model import Condition, ConditionGroup, RuleValidationError
from app.schemas import api as wire

SEVERITIES = ("critical", "error", "warning", "info")
COLOURS = {"critical": "#C9372C", "error": "#E2483D", "warning": "#E2B203", "info": "#1D7AFC"}
# Our operator names -> Herald's (`alertsvc/rules/operators.py`); `not_in` becomes `not` + `in`.
OPERATORS = {"neq": "ne"}
HERALD_OPERATORS = ["eq", "ne", "gt", "gte", "lt", "lte", "in", "contains", "glob", "regex", "exists"]
DELIVERY_STATUS = {
    "sent": "sent",
    "delivered": "sent",
    "acked": "sent",
    "retrying": "retrying",
    "dead_lettered": "dead",
    "failed": "dead",
}
STEP_STATE = {
    "succeeded": "done",
    "failed": "failed",
    "pending_approval": "awaiting_approval",
    "running": "running",
}
KIND = {"text": "manual", "check": "check", "action": "action"}

# What a severity means and how to respond (shown next to the severity picker; Herald `SEVERITY_GUIDE`).
SEVERITY_GUIDE: dict[str, dict[str, Any]] = {
    "critical": {
        "level": 1,
        "meaning": "A service or business process is down for everyone, or money, data or a regulatory "
        "duty is at risk.",
        "response": "Page now, any hour. One person takes charge. Updates every 30 minutes. "
        "Postmortem required.",
        "update_every_minutes": 30,
        "postmortem": True,
    },
    "error": {
        "level": 2,
        "meaning": "Down for some users, or a core function is badly impaired.",
        "response": "Page now, any hour. Updates every 60 minutes. Postmortem required.",
        "update_every_minutes": 60,
        "postmortem": True,
    },
    "warning": {
        "level": 3,
        "meaning": "Degraded but usable, or there is a workaround.",
        "response": "Handled by the owning team in working hours. Update when something changes.",
        "update_every_minutes": 180,
        "postmortem": False,
    },
    "info": {
        "level": None,
        "meaning": "Worth knowing, nothing to do right now.",
        "response": "Shown in the feed only. Nobody is paged.",
        "update_every_minutes": None,
        "postmortem": False,
    },
}


def iso(value: datetime | None) -> str | None:
    return value.isoformat(timespec="milliseconds") if value else None


def field_name(name: str) -> str:
    """Our `_severity` (normalized meta) is Herald's `@severity`; raw paths are the same."""
    return "@" + name[1:] if name.startswith("_") else name


def fields_object(fields: Any) -> dict[str, Any]:
    if isinstance(fields, dict):
        return dict(fields)
    if isinstance(fields, list):
        return {str(f.get("label", "")): f.get("value") for f in fields if isinstance(f, dict)}
    return {}


def delivery_status(status: str) -> str:
    """Herald `sent|retrying|dead|pending`; our held-back (`suppressed_*`) deliveries are `suppressed`."""
    if status.startswith("suppressed"):
        return "suppressed"
    return DELIVERY_STATUS.get(status, "pending")


def delivery_out(row: Any, refs: dict[UUID, str], *, short_id: int | None = None) -> dict[str, Any]:
    """`deliveries` row (+ optional `title`, `latency_ms`) -> Herald `Delivery`."""
    keys = row.keys()
    recipient = refs.get(row["recipient_user_id"]) if row["recipient_user_id"] else None
    latency = row["latency_ms"] if "latency_ms" in keys else None
    team = {k: row[k] for k in ("team_slug", "team_name") if k in keys}
    feed = recipient is None and not row["recipient_address"] and team.get("team_slug")
    if feed:
        recipient = f"team:{team['team_slug']}"
    return {
        "id": str(row["id"]),
        "alert_id": short_id,
        "title": row["title"] if "title" in keys else None,
        "recipient": recipient or row["recipient_address"] or "unknown",
        "recipient_kind": "team" if feed else ("person" if recipient else "address"),
        "recipient_name": f"{team.get('team_name') or team['team_slug']} feed" if feed else None,
        "channel": row["channel"],
        "status": delivery_status(row["status"]),
        "attempts": row["attempts"],
        "last_error": row["last_error"],
        "error": row["last_error"],
        "created_at": iso(row["created_at"]),
        "latency_ms": float(latency) if latency is not None else None,
        "kind": row["kind"],
        "our_status": row["status"],
    }


def alert_out(
    row: Any,
    *,
    slug: str,
    refs: dict[UUID, str],
    deliveries: list[dict[str, Any]],
    resolved_by: str | None,
) -> dict[str, Any]:
    """`alerts` row (DETAIL/extras columns + `source_name`) -> Herald `Alert` (+ a few of our fields)."""
    return {
        "id": row["short_id"],
        "key": format_short_id(row["short_id"]),
        "uuid": str(row["id"]),
        "rule_id": str(row["rule_id"]) if row["rule_id"] else None,
        "owner": owner_ref(slug),
        "status": herald_status(row["status"]),
        "our_status": row["status"],
        "severity": row["severity"],
        "title": row["title"],
        "body": row["body"] or "",
        "fields": fields_object(row["fields"]),
        "highlight": [],
        "colour": row["color"] or COLOURS.get(row["severity"], COLOURS["info"]),
        "count": row["count"],
        "first_seen": iso(row["first_seen_at"]),
        "last_seen": iso(row["last_seen_at"]),
        "acked_at": iso(row["acked_at"]),
        "resolved_at": iso(row["resolved_at"]),
        "acked_by": refs.get(row["acked_by"]) if row["acked_by"] else None,
        "resolved_by": resolved_by,
        "incident_manager": refs.get(row["assignee_id"]) if row["assignee_id"] else None,
        **assignment_out(row, refs),
        "source": row["source_name"],
        "flags": [f for f in (row["flags"] or []) if f in {x.value for x in wire.Flag}],
        "escalation_level": row["escalation_level"],
        "escalates_at": iso(row["ack_deadline"]),
        "deliveries": deliveries,
    }


def assignment_out(row: Any, refs: dict[UUID, str]) -> dict[str, Any]:
    """Who made the current assignment (`system:auto-assign` or `person:x`) and the automatic reason.

    Both are null while nobody owns the alert or for assignments older than this bookkeeping."""
    by = row["assigned_by"] if row["assignee_id"] else None
    return {
        "assigned_by": actor_ref(by, refs) if by else None,
        "assign_reason": row["assign_reason"] if by else None,
    }


def step_approval(step: wire.RunbookStep) -> str:
    """Herald `approval`: `required` whenever our runner would ask for one before running the step."""
    if not step.action:
        return "required" if step.approval == wire.Approval1.required else "none"
    gated = (
        step.approval == wire.Approval1.required
        or (step.risk is not None and step.risk.value == "high")
        or (step.trustLevel is not None and step.trustLevel.value != "auto")
    )
    return "required" if gated else "none"


def step_out(step: wire.RunbookStep) -> dict[str, Any]:
    """Our `RunbookStep` -> Herald `Step`; `id` is the 1-based order, our id is `uid`."""
    return {
        "id": step.order,
        "uid": step.id,
        "kind": KIND.get(step.kind.value if step.kind else "text", "manual"),
        "text": step.text or "",
        "action": step.action,
        "params": step.parameters or {},
        "approval": step_approval(step),
        "condition": step.guard,
        "suggest": None,
        "risk": step.risk.value if step.risk else None,
        "url": step.url,
        "state": step.state.value if step.state else None,
        "guard_met": step.guardMet,
    }


def runbook_out(view: Any) -> dict[str, Any]:
    """`RunbookView` (rendered for the alert) -> Herald `runbook`."""
    steps = list(view.steps or [])
    return {
        "id": view.id,
        "title": view.name,
        "link": next((s.url for s in steps if s.url), None),
        "text": view.source if not steps else None,
        "steps": [step_out(s) for s in steps],
    }


def step_state_out(view: Any, refs: dict[UUID, str], approvals: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Herald `step_state` keyed by `str(order)` from the latest run of each step.

    `approvals` maps our approval uuid (str) -> approvals row, for `approved_by` / `rejected_by`."""
    out: dict[str, dict[str, Any]] = {}
    for step in view.steps or []:
        state = step.state.value if step.state else "not_started"
        if state == "not_started":
            continue
        last = step.lastRun
        by = (
            actor_ref({"type": last.by.kind, "id": last.by.id, "name": last.by.displayName}, refs)
            if last and last.by
            else None
        )
        approval = approvals.get(step.approvalId or "")
        decided = approval["decided_by"] if approval is not None else None
        entry: dict[str, Any] = {"state": STEP_STATE.get(state, state), "by": by}
        if state == "skipped" and approval is not None and approval["status"] == "rejected":
            entry["state"] = "rejected"
            entry["rejected_by"] = actor_ref(decided, refs)
        if approval is not None and approval["status"] == "approved":
            entry["approved_by"] = actor_ref(decided, refs)
            entry["approval"] = approval["short_id"]
        if state == "pending_approval" and approval is not None:
            entry["approval"] = approval["short_id"]
        if step.action and state in ("succeeded", "failed"):
            message = last.output if last and last.output else ""
            entry["result"] = {"ok": state == "succeeded", "message": message, "error": None}
            if state == "failed":
                entry["result"]["error"] = message
        out[str(step.order)] = entry
    return out


def approval_out(row: Any, refs: dict[UUID, str], step_order: int | None) -> dict[str, Any]:
    """`approvals` row -> Herald `Approval` (`id` = short id); `requested_by_user` is the human behind it."""
    requester = actor_ref(row["requested_by"], refs)
    approvers = [refs[u] for u in row["approver_user_ids"] if u in refs and refs[u] != requester]
    approver = approvers[0] if approvers else None
    stored = row["requested_by"] or {}
    human = stored.get("userId") or (stored.get("id") if stored.get("type") == "user" else None)
    human_id = parse_uuid(str(human or ""))
    human_ref = refs.get(human_id) if human_id else None
    return {
        "id": row["short_id"],
        "uuid": str(row["id"]),
        "step_id": step_order,
        "action": row["action"],
        "params": row["parameters"] or {},
        "required": "required",
        "risk": row["risk"],
        "reason": row["reason"],
        "requested_by": requester,
        "requested_by_user": human_ref,
        "approver": approver,
        "approvers": approvers,
        "status": row["status"],
        "decided_by": actor_ref(row["decided_by"], refs) if row["decided_by"] else None,
        "decided_via": "ui" if row["decided_by"] else None,
        "comment": row["comment"],
        "expires_at": iso(row["expires_at"]),
    }


def timeline_out(
    row: Any,
    refs: dict[UUID, str],
    steps: dict[str, dict[str, Any]],
    approvals: dict[str, Any],
) -> dict[str, Any] | None:
    """`timeline` row -> Herald `{at, actor, action, detail}`; None for entries Herald shows once only.

    `steps` maps our step id -> Herald step (`id` = order, `action`); `approvals` our approval uuid -> row.
    """
    kind, data = row["type"], dict(row["data"] or {})
    detail: dict[str, Any] = {"text": row["text"], **data}
    action = kind
    if kind == "acked":
        action, detail["via"] = "ack", data.get("via", "ui")
    elif kind == "resolved":
        action, detail["via"] = "resolve", data.get("via", "ui")
        if data.get("resolutionNote"):
            detail["note"] = data["resolutionNote"]
    elif kind == "assigned":
        assignee = data.get("assigneeId")
        action = "assigned"
        detail["to"] = refs.get(UUID(assignee)) if assignee else None
        detail["reason"] = data.get("reason")
        detail["auto"] = bool(data.get("auto"))
    elif kind == "comment":
        action = "note"
    elif kind == "step_run":
        step = steps.get(data.get("stepId") or "", {})
        detail["step"] = step.get("id")
        detail["action"] = step.get("action")
        state = data.get("state")
        if state == "pending_approval":
            action = "approval_requested"
        elif state in ("succeeded", "failed") and step.get("action"):
            action = "action_run"
            detail["result"] = {"ok": state == "succeeded", "message": row["text"]}
        elif state == "succeeded":
            action = "step_done"
        else:
            action = f"step_{state}"
    elif kind == "approval":
        approval = approvals.get(data.get("approvalId") or "")
        if data.get("action"):
            if approval is not None and approval["origin"] == "runbook_step":
                return None  # the step_run entry already says "asked for approval"
            action = "approval_requested"
        elif approval is not None:
            action = {"approved": "approved", "rejected": "rejected"}.get(
                approval["status"], "approval_expired"
            )
            detail.update(action=approval["action"], via="ui")
    return {"at": iso(row["at"]), "actor": actor_ref(row["actor"], refs), "action": action, "detail": detail}


def _leaf(cond: Condition, ev: EventView, path: str) -> dict[str, Any]:
    actual = ev.lookup(cond.field)
    try:
        passed = bool(_compile_condition(cond, path)(ev))
    except RuleValidationError:
        passed = False
    op = cond.operator
    node = {
        "field": field_name(cond.field),
        "op": "in" if op == "not_in" else OPERATORS.get(op, op),
        "value": cond.value,
        "actual": None if actual is MISSING else actual,
        "missing": actual is MISSING,
        "passed": not passed if op == "not_in" else passed,
    }
    return {"not": node, "passed": passed} if op == "not_in" else node


def _explain(group: ConditionGroup, ev: EventView, path: str) -> dict[str, Any]:
    children = []
    for i, node in enumerate(group.conditions):
        child_path = f"{path}.conditions[{i}]"
        if isinstance(node, ConditionGroup):
            children.append(_explain(node, ev, child_path))
        else:
            children.append(_leaf(node, ev, child_path))
    key, combine = ("all", all) if group.op == "and" else ("any", any)
    return {key: children, "passed": combine(c["passed"] for c in children)}


def explain(match: dict[str, Any] | None, ev: EventView) -> dict[str, Any] | None:
    """Herald `Why`: the rule's condition tree with `passed` on every node and `actual` on leaves."""
    if not match:
        return None
    try:
        group = ConditionGroup.model_validate(match)
    except ValueError:
        return None
    return _explain(group, ev, "match")


AUDIT_ACTIONS = {
    "alert.ack": "ack",
    "alert.resolve": "resolve",
    "alert.assign": "assigned",
    "alert.comment": "note",
    "approval.request": "approval_requested",
    "approval.approved": "approved",
    "approval.rejected": "rejected",
    "runbook.step.succeeded": "action_run",
    "runbook.step.failed": "action_run",
    "runbook.step.pending_approval": "approval_requested",
}


def audit_out(row: Any, refs: dict[UUID, str], alert_shorts: dict[str, int]) -> dict[str, Any]:
    """`audit_log` row -> Herald `AuditEntry`; `hash` is the sha256 of the stored row."""
    target_id = row["target_id"]
    if row["target_type"] == "alert" and target_id in alert_shorts:
        target_id = str(alert_shorts[target_id])
    body = {
        "id": row["id"],
        "at": iso(row["at"]),
        "actor": row["actor"],
        "action": row["action"],
        "target": f"{row['target_type']}:{row['target_id']}",
        "data": row["data"],
    }
    return {
        "id": row["id"],
        "at": iso(row["at"]),
        "actor": actor_ref(row["actor"], refs),
        "action": AUDIT_ACTIONS.get(row["action"], row["action"]),
        "our_action": row["action"],
        "target": f"{row['target_type']}:{target_id}",
        "detail": row["data"] or {},
        "hash": hashlib.sha256(orjson.dumps(body, default=str, option=orjson.OPT_SORT_KEYS)).hexdigest(),
    }
