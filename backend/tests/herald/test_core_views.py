"""Pure translation helpers: explain, ids, timeline mapping, report text."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.core.errors import AppError
from app.herald import ids, reports, views
from app.pipeline.fields import EventView


def test_explain_edge_cases():
    ev = EventView(raw={"msg": "boom"}, meta={"severity": "error"})
    assert views.explain(None, ev) is None
    assert views.explain({"op": "xor", "conditions": []}, ev) is None
    why = views.explain(
        {"op": "or", "conditions": [{"field": "msg", "operator": "regex", "value": "(a+)+"}]}, ev
    )
    assert why == {
        "any": [
            {
                "field": "msg",
                "op": "regex",
                "value": "(a+)+",
                "actual": "boom",
                "missing": False,
                "passed": False,
            }
        ],
        "passed": False,
    }


def test_status_and_id_helpers():
    assert ids.our_statuses("firing,acked") == ["open", "escalated", "acknowledged"]
    assert ids.our_statuses(None) == ids.ALL_STATUSES
    with pytest.raises(AppError):
        ids.our_statuses("snoozed")
    assert [ids.short_from_ref(v) for v in (17, "A-17", "AL-0017", "x")] == [17, 17, 17, None]
    assert ids.herald_status("weird") == "firing"
    uid = uuid4()
    refs = {uid: "person:anna"}
    assert ids.actor_ref({"type": "user", "id": str(uid)}, refs) == "person:anna"
    assert ids.actor_ref({"type": "agent", "name": "copilot", "userId": str(uid)}, refs) == "agent:copilot"
    assert ids.actor_ref(None, refs) == "system:herald"
    assert ids.actor_user_ids([{"id": "nope"}, {"userId": str(uid)}, None]) == {uid}


def test_fields_object_accepts_both_stored_shapes():
    assert views.fields_object([{"label": "A", "value": 1}, "junk"]) == {"A": 1}
    assert views.fields_object(None) == {}


def _row(type_, data=None, text="t"):
    return {"type": type_, "data": data or {}, "text": text, "at": datetime.now(UTC), "actor": {}}


def test_timeline_mapping_of_other_entries():
    approval_id = str(uuid4())
    expired = {"status": "expired", "action": "escalate", "origin": "agent_request"}
    approvals = {approval_id: expired}
    requested = views.timeline_out(
        _row("approval", {"approvalId": approval_id, "action": "x"}), {}, {}, approvals
    )
    assert requested["action"] == "approval_requested"
    decided = views.timeline_out(_row("approval", {"approvalId": approval_id}), {}, {}, approvals)
    assert decided["action"] == "approval_expired" and decided["detail"]["action"] == "escalate"
    skipped = views.timeline_out(_row("step_run", {"stepId": "s1", "state": "skipped"}), {}, {}, {})
    assert skipped["action"] == "step_skipped" and skipped["actor"] == "system:herald"
    other = views.timeline_out(_row("escalated", text="Escalated to level 2"), {}, {}, {})
    assert other["action"] == "escalated" and other["detail"]["text"] == "Escalated to level 2"


def _alert(**kw):
    base = {
        "key": "A-1",
        "severity": "info",
        "title": "T",
        "status": "firing",
        "owner": "team:x",
        "first_seen": "2026-11-21T10:00:00+00:00",
        "last_seen": "garbage",
        "count": 1,
        "incident_manager": None,
        "timeline": [],
        "runbook": {"title": "rb", "steps": [{"id": 1, "text": "a"}, {"id": 2, "text": "b"}]},
        "step_state": {"1": {"state": "done"}},
    }
    return {**base, **kw}


def test_report_text_branches():
    now = datetime(2026, 11, 21, 10, 5, tzinfo=UTC)
    text = reports.status_update(_alert(status="acked", acked_at="2026-11-21T10:01:00+00:00"), now, {})
    assert "Being worked on since 10:01 UTC" in text and "Next update when something changes." in text
    assert "Done so far:\n  - a (done)" in text and "Next step: b" in text and "last at unknown" in text
    timeline = [
        {
            "at": "2026-11-21T10:02:00+00:00",
            "actor": "system:auto",
            "action": "escalated",
            "detail": {"text": "L2"},
        },
        {"at": None, "actor": "agent:bot", "action": "ack", "detail": {}},
        {"at": None, "actor": "person:lead", "action": "note", "detail": {"text": "hi"}},
    ]
    md = reports.postmortem(
        _alert(timeline=timeline, resolved_by="system:auto", acked_at="2026-11-21T10:00:30+00:00"),
        lead="lead",
        names=None,
        resolution_notes=[],
        ai_summary="It was DNS",
    )
    assert "Herald (automatic) | escalated: L2" in md and "an agent" in md and "the team lead" in md
    assert "AI draft from the timeline (check it): It was DNS" in md and "Time to acknowledge: 30 s" in md


def _approval_row(requested_by: dict, approvers: list) -> dict:
    return {
        "id": uuid4(),
        "short_id": 7,
        "action": "restart_service",
        "parameters": {},
        "risk": "high",
        "reason": "r",
        "requested_by": requested_by,
        "approver_user_ids": approvers,
        "status": "pending",
        "decided_by": None,
        "comment": None,
        "expires_at": datetime(2026, 11, 21, tzinfo=UTC),
    }


def test_approval_out_never_lists_the_requesting_person():
    anna, root = uuid4(), uuid4()
    refs = {anna: "person:anna", root: "person:root.payments"}
    own = views.approval_out(
        _approval_row({"type": "user", "id": str(anna), "name": "Anna"}, [anna, root]), refs, 3
    )
    assert own["approvers"] == ["person:root.payments"] and own["approver"] == "person:root.payments"
    assert own["requested_by"] == own["requested_by_user"] == "person:anna"
    agent = {"type": "agent", "id": "claude", "name": "Claude", "userId": str(anna)}
    by_agent = views.approval_out(_approval_row(agent, [anna, root]), refs, None)
    assert by_agent["approvers"] == ["person:anna", "person:root.payments"]  # acts for anna: she may approve
    assert by_agent["requested_by"] == "agent:Claude" and by_agent["requested_by_user"] == "person:anna"
