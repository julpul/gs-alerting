"""`request_approval` long-poll (Redis `appr:wait:{id}`), status polling and `run_runbook_step`."""

from __future__ import annotations

import asyncio
import time
from uuid import UUID, uuid4

from tests.runbooks.helpers import make_alert as make_runbook_alert

from .helpers import call, call_error, decide_as, eventually, pending_approval_of

REQUEST = {
    "action": "terraform_apply",
    "parameters": {"workspace": "prod"},
    "reason": "Apply the reviewed plan",
    "risk": "high",
}


async def start_request(agent, **extra):
    return asyncio.create_task(call(agent, "request_approval", {**REQUEST, **extra}))


async def test_request_approval_returns_approved_after_a_concurrent_decision(make_pat, connect, seeded):
    pat = await make_pat("anna@demo", f"claude-{uuid4().hex[:6]}")
    async with connect(pat.token) as agent:
        waiting = await start_request(agent, timeout_seconds=30)

        async def pending():
            return await pending_approval_of(seeded, pat.name)

        row = await eventually(pending)
        assert row["origin"] == "agent_request" and row["risk"] == "high"
        anna_name = await seeded.fetchval("SELECT display_name FROM users WHERE email = 'anna@demo'")
        assert row["requested_by"]["onBehalfOf"] == anna_name and row["requested_by"]["type"] == "agent"
        assert not waiting.done()
        started = time.monotonic()
        await decide_as(
            seeded, row["id"], "root.payments@demo", "Paulina Payments", comment="Plan looks fine"
        )
        result = await asyncio.wait_for(waiting, 10)
        assert time.monotonic() - started < 5  # woken by the pub/sub message, not by the timeout
        assert result == {
            "status": "approved",
            "approvalId": str(row["id"]),
            "decidedBy": "Paulina Payments",
            "comment": "Plan looks fine",
        }
        status = await call(agent, "get_approval_status", {"approval_id": str(row["id"])})
        assert status["status"] == "approved" and status["action"] == "terraform_apply"


async def test_request_approval_reports_a_rejection(make_pat, connect, seeded):
    pat = await make_pat("anna@demo", f"claude-{uuid4().hex[:6]}")
    async with connect(pat.token) as agent:
        waiting = await start_request(agent, timeout_seconds=30)
        row = await eventually(lambda: pending_approval_of(seeded, pat.name))
        await decide_as(seeded, row["id"], "root.payments@demo", "Paulina Payments", "reject", "Not now")
        result = await asyncio.wait_for(waiting, 10)
    assert result["status"] == "rejected" and result["comment"] == "Not now"


async def test_request_approval_returns_pending_on_timeout(make_pat, connect, seeded):
    pat = await make_pat("anna@demo", f"claude-{uuid4().hex[:6]}")
    async with connect(pat.token) as agent:
        started = time.monotonic()
        result = await call(agent, "request_approval", {**REQUEST, "timeout_seconds": 1})
        assert 0.9 <= time.monotonic() - started < 8
        assert result["status"] == "pending" and UUID(result["approvalId"])
        assert set(result) == {"status", "approvalId"}

        polled = await call(agent, "get_approval_status", {"approval_id": result["approvalId"]})
        assert polled["status"] == "pending"
        await decide_as(seeded, UUID(result["approvalId"]), "root.payments@demo", "Paulina Payments")
        later = await call(agent, "get_approval_status", {"approval_id": result["approvalId"]})
        assert later["status"] == "approved" and later["decidedBy"] == "Paulina Payments"

        # timeout 0 never waits
        immediate = await call(agent, "request_approval", {**REQUEST, "timeout_seconds": 0})
        assert immediate["status"] == "pending"


async def test_request_approval_validation_and_visibility(make_pat, connect, seeded):
    anna = await make_pat("anna@demo", f"claude-{uuid4().hex[:6]}")
    piotr = await make_pat("piotr@demo", f"claude-{uuid4().hex[:6]}")
    alert = await make_runbook_alert(seeded, team_slug="team-payments")
    async with connect(anna.token) as agent:
        assert "validation_error" in await call_error(
            agent, "request_approval", {**REQUEST, "risk": "extreme"}
        )
        assert "validation_error" in await call_error(agent, "request_approval", {**REQUEST, "reason": " "})
        assert "not_found" in await call_error(
            agent, "request_approval", {**REQUEST, "alert_id": str(uuid4()), "timeout_seconds": 0}
        )
        on_alert = await call(
            agent, "request_approval", {**REQUEST, "alert_id": str(alert["id"]), "timeout_seconds": 0}
        )
    approval_id = on_alert["approvalId"]
    timeline = await seeded.fetch("SELECT type, text FROM timeline WHERE alert_id = $1", alert["id"])
    assert any(t["type"] == "approval" and "terraform_apply" in t["text"] for t in timeline)
    async with connect(piotr.token) as agent:
        assert "not_found" in await call_error(agent, "get_approval_status", {"approval_id": approval_id})
        assert "not_found" in await call_error(
            agent, "request_approval", {**REQUEST, "alert_id": str(alert["id"])}
        )


async def test_run_runbook_step_runs_or_asks_for_approval(make_pat, connect, seeded):
    alert = await make_runbook_alert(seeded)
    pat = await make_pat("piotr@demo", f"runner-{uuid4().hex[:6]}")
    async with connect(pat.token) as agent:
        detail = await call(agent, "get_alert", {"id": str(alert["id"])})
        assert [s["id"] for s in detail["runbook"]["steps"]] == ["s1", "s2", "s3"]
        outcome = await call(agent, "run_runbook_step", {"alert_id": str(alert["id"]), "step_id": "s3"})
        assert outcome["step"]["state"] == "pending_approval"
        assert outcome["approval"]["origin"] == "runbook_step"
        assert outcome["approval"]["requestedBy"]["kind"] == "agent"
        assert "not_found" in await call_error(
            agent, "run_runbook_step", {"alert_id": str(alert["id"]), "step_id": "nope"}
        )
        again = await call(agent, "run_runbook_step", {"alert_id": str(alert["id"]), "step_id": "s3"})
        assert again["approval"]["id"] == outcome["approval"]["id"]  # one pending approval per step

    other = await make_pat("anna@demo", f"runner-{uuid4().hex[:6]}")
    async with connect(other.token) as agent:
        assert "not_found" in await call_error(
            agent, "run_runbook_step", {"alert_id": str(alert["id"]), "step_id": "s1"}
        )
