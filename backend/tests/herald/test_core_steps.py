"""Runbook steps via /api/alerts/{id}/steps/{step}/run (inline confirm); agent requests via /api/approvals."""

from __future__ import annotations

import pytest_asyncio

from app.runbooks.runner import run_step
from tests.delivery.conftest import gw, mockgw_server  # noqa: F401  (re-exported fixtures)
from tests.runbooks.helpers import make_alert, user

from .core_helpers import as_person


@pytest_asyncio.fixture(autouse=True, loop_scope="session")
async def default_trust(seeded):
    await seeded.execute("DELETE FROM action_trust")
    yield


AGENT = {
    "type": "agent",
    "id": "claude-code",
    "name": "Claude Code",
    "onBehalfOf": "Piotr Wisniewski",
    "userId": str(user("piotr@demo")),
}


async def agent_run(pool, alert, step_id):
    async with pool.acquire() as conn:
        return await run_step(conn, alert, step_id, AGENT)


async def _detail(client, short_id, person="piotr"):
    r = await client.get(f"/api/alerts/{short_id}", headers=as_person(person))
    assert r.status_code == 200, r.text
    return r.json()


async def _run(client, short_id, step, person="piotr"):
    return await client.post(f"/api/alerts/{short_id}/steps/{step}/run", headers=as_person(person))


async def test_runbook_steps_shape_and_safe_action(client, seeded, gw):  # noqa: F811
    alert = await make_alert(seeded)
    d = await _detail(client, alert["short_id"])
    rb = d["runbook"]
    assert rb["title"] == "settlement-timeout" and rb["id"]
    steps = rb["steps"]
    assert [s["id"] for s in steps] == [1, 2, 3]
    check, retry, escalate = steps
    assert check["kind"] == "check" and check["action"] == "http_get" and check["approval"] == "none"
    assert retry["params"] == {"workflowId": "WF-20261023-00042"} and retry["uid"] == "s2"
    assert escalate["approval"] == "required" and escalate["condition"]
    for s in steps:
        assert {"id", "kind", "text", "action", "params", "approval", "condition", "suggest"} <= set(s)

    r = await _run(client, alert["short_id"], 1)
    assert r.status_code == 200, r.text
    body = r.json()
    assert (
        body["state"] == "done" and body["result"]["ok"] is True and "HTTP 200" in body["result"]["message"]
    )
    d = await _detail(client, alert["short_id"])
    assert d["step_state"]["1"]["state"] == "done" and d["step_state"]["1"]["by"] == "person:piotr"
    assert d["step_state"]["1"]["result"]["ok"] is True
    assert any(t["action"] == "action_run" and t["detail"]["step"] == 1 for t in d["timeline"])


async def test_risky_step_needs_the_inline_confirm_then_runs(client, seeded, gw):  # noqa: F811
    alert = await make_alert(seeded)
    refused = await _run(client, alert["short_id"], "3")
    assert refused.status_code == 409 and "confirm" in refused.json()["detail"]
    d = await _detail(client, alert["short_id"])
    assert "3" not in d["step_state"] and d["approvals"] == []

    r = await client.post(
        f"/api/alerts/{alert['short_id']}/steps/3/run", headers=as_person("piotr"), json={"confirmed": True}
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["state"] == "done" and body["result"]["ok"] is True and "paged" in body["result"]["message"]

    d = await _detail(client, alert["short_id"])
    assert d["step_state"]["3"]["state"] == "done" and d["approvals"] == []
    confirmed = [t for t in d["timeline"] if t["detail"].get("text", "").startswith("confirmed and ran")]
    assert confirmed and "escalate by Piotr Wisniewski" in confirmed[0]["detail"]["text"]


async def test_agent_request_shows_on_the_alert_and_a_person_confirms(client, seeded, gw):  # noqa: F811
    alert = await make_alert(seeded)
    await agent_run(seeded, alert, "s3")
    d = await _detail(client, alert["short_id"])
    assert d["step_state"]["3"]["state"] == "awaiting_approval"
    (ap,) = d["approvals"]
    assert ap["step_id"] == 3 and ap["status"] == "pending" and ap["action"] == "escalate"
    assert ap["requested_by"].startswith("agent:") or "claude" in ap["requested_by"].lower()

    # someone outside the alert's teams cannot even see the request
    other = await client.post(
        f"/api/approvals/{ap['id']}/decide", headers=as_person("anna"), json={"approve": True}
    )
    assert other.status_code == 404
    bad = await client.post(f"/api/approvals/{ap['id']}/decide", headers=as_person("piotr"), json={})
    assert bad.status_code == 422

    r = await client.post(
        f"/api/approvals/{ap['id']}/decide", headers=as_person("piotr"), json={"approve": True}
    )
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["status"] == "approved" and out["result"]["ok"] is True and "paged" in out["result"]["message"]
    again = await client.post(
        f"/api/approvals/{ap['id']}/decide", headers=as_person("piotr"), json={"approve": True}
    )
    assert again.status_code == 409

    d = await _detail(client, alert["short_id"])
    state = d["step_state"]["3"]
    assert state["state"] == "done" and state["approved_by"] == "person:piotr"
    assert d["approvals"][0]["status"] == "approved"


async def test_agent_request_cancelled_and_unknown_steps(client, seeded, gw):  # noqa: F811
    alert = await make_alert(seeded)
    await agent_run(seeded, alert, "s3")
    (ap,) = (await _detail(client, alert["short_id"]))["approvals"]
    r = await client.post(
        f"/api/approvals/{ap['id']}/decide",
        headers=as_person("root.settlements"),
        json={"decision": "reject", "comment": "not now"},
    )
    assert r.status_code == 200 and r.json()["status"] == "rejected" and r.json()["result"] is None
    d = await _detail(client, alert["short_id"])
    assert d["step_state"]["3"]["state"] == "rejected"
    assert d["step_state"]["3"]["rejected_by"] == "person:root.settlements"

    assert (await _run(client, alert["short_id"], 9)).status_code == 404
    assert (await _run(client, alert["short_id"], 1, person="anna")).status_code == 404
    assert (await _run(client, alert["short_id"], 1, person="manager")).status_code == 403
    missing = await client.post("/api/approvals/999999/decide", json={"approve": True})
    assert missing.status_code == 404


async def test_manual_step_marked_done(client, seeded, gw):  # noqa: F811
    from tests.runbooks.helpers import runbook_id

    rb = runbook_id("herald-manual")
    await seeded.execute(
        "INSERT INTO runbooks (id, team_id, name, format, source, steps, attached) "
        "VALUES ($1, $2, 'herald-manual', 'text', 'Call the desk', $3, '{}') ON CONFLICT DO NOTHING",
        rb,
        None,
        [{"id": "s1", "order": 1, "kind": "text", "text": "Call the desk", "approval": "none"}],
    )
    alert = await make_alert(seeded, runbook=rb)
    r = await _run(client, alert["short_id"], 1)
    assert r.status_code == 200 and r.json() == {"state": "done"}
    d = await _detail(client, alert["short_id"])
    assert d["runbook"]["steps"][0]["kind"] == "manual"
    assert d["step_state"]["1"] == {"state": "done", "by": "person:piotr"}
    assert any(t["action"] == "step_done" for t in d["timeline"])
