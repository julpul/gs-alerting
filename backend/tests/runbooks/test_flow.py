"""Settlement runbook end to end: checks and actions run against mockgw, risky steps wait for approval."""

from __future__ import annotations

import asyncio

import orjson

from app.core.ids import uuid7
from app.core.redis import get_redis
from app.core.sse import team_channel

from .helpers import make_alert, team, user


def run_url(alert, step):
    return f"/v1/alerts/{alert['id']}/runbook/steps/{step}/run"


async def run(client, auth_headers, email, alert, step):
    return await client.post(run_url(alert, step), headers=await auth_headers(email))


async def decide(client, auth_headers, email, approval_id, decision="approve", comment=None):
    return await client.post(
        f"/v1/approvals/{approval_id}/decision",
        headers=await auth_headers(email),
        json={"decision": decision, "comment": comment},
    )


async def collect_sse(pubsub, wanted: set[str], window=2.0) -> dict[str, dict]:
    seen: dict[str, dict] = {}
    deadline = asyncio.get_running_loop().time() + window
    while wanted - seen.keys() and asyncio.get_running_loop().time() < deadline:
        msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.2)
        if msg:
            event = orjson.loads(msg["data"])
            seen[event["type"]] = event["data"]
    return seen


async def test_settlement_runbook_check_retry_then_escalate_with_approval(client, auth_headers, seeded, gw):
    alert = await make_alert(seeded)
    pubsub = get_redis().pubsub()
    await pubsub.subscribe(team_channel(team("team-settlements")))

    # 1. check: real GET against mockgw (host without dots -> /svc/<name>/health)
    r = await run(client, auth_headers, "piotr@demo", alert, "s1")
    assert r.status_code == 200, r.text
    check = r.json()
    assert check["state"] == "succeeded" and "HTTP 200" in check["lastRun"]["output"]
    assert check["lastRun"]["by"]["displayName"] == "Piotr Wisniewski"

    # 2. retry_workflow (approval: none, medium risk, default trust auto) runs now
    r = await run(client, auth_headers, "piotr@demo", alert, "s2")
    assert r.json()["state"] == "succeeded"
    assert (await gw.get("/admin/state")).json()["workflows"][-1] == {
        "workflowId": "WF-20261023-00042",
        "at": (await gw.get("/admin/state")).json()["workflows"][-1]["at"],
        "result": "retried",
    }

    # the retry "failed twice": the guarded escalation step now says its guard is met
    for _ in range(2):
        await seeded.execute(
            "INSERT INTO step_runs (id, alert_id, step_id, state, output, run_by) "
            "VALUES ($1, $2, 's2', 'failed', 'HTTP 500', '{}')",
            uuid7(),
            alert["id"],
        )
    detail = (await client.get(f"/v1/alerts/{alert['id']}", headers=await auth_headers("piotr@demo"))).json()
    escalate = detail["runbook"]["steps"][2]
    assert escalate["guardMet"] is True and escalate["state"] == "not_started"

    # 3. escalate requires approval
    r = await run(client, auth_headers, "piotr@demo", alert, "s3")
    assert r.status_code == 200
    step = r.json()
    assert step["state"] == "pending_approval" and step["approvalId"]
    approval_id = step["approvalId"]
    row = await seeded.fetchrow("SELECT * FROM approvals WHERE id = $1::uuid", approval_id)
    assert row["status"] == "pending" and row["origin"] == "runbook_step" and row["action"] == "escalate"
    assert round((row["expires_at"] - row["created_at"]).total_seconds()) == 900
    assert user("root.settlements@demo") in row["approver_user_ids"]
    assert user("piotr@demo") in row["approver_user_ids"]

    sms = (await gw.get("/admin/state")).json()["sms"]
    root_sms = next(m for m in sms if m["to"] == "+48500100120")
    assert f"Reply APPROVE {row['short_id']}" in root_sms["text"] and len(root_sms["text"]) <= 160
    assert (
        await seeded.fetchval(
            "SELECT count(*) FROM deliveries WHERE alert_id = $1 AND idempotency_key LIKE 'appr:%'",
            alert["id"],
        )
        >= 2
    )

    # asking again does not create a second approval
    again = (await run(client, auth_headers, "piotr@demo", alert, "s3")).json()
    assert again["approvalId"] == approval_id
    assert await seeded.fetchval("SELECT count(*) FROM approvals WHERE alert_id = $1", alert["id"]) == 1

    # people outside the approver list cannot decide
    assert (await decide(client, auth_headers, "anna@demo", approval_id)).status_code == 403
    assert (await decide(client, auth_headers, "manager@demo", approval_id)).status_code == 403
    assert (
        await seeded.fetchval("SELECT status FROM approvals WHERE id = $1::uuid", approval_id)
    ) == "pending"

    # 4. the Domain Root approves: the action runs
    r = await decide(client, auth_headers, "root.settlements@demo", approval_id, comment="go")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "approved" and body["decidedBy"]["displayName"] == "Stefan Settlements"
    assert body["comment"] == "go" and body["shortId"] == row["short_id"]
    assert body["requestedBy"]["displayName"] == "Piotr Wisniewski"

    detail = (await client.get(f"/v1/alerts/{alert['id']}", headers=await auth_headers("piotr@demo"))).json()
    escalate = detail["runbook"]["steps"][2]
    assert escalate["state"] == "succeeded" and "paged" in escalate["lastRun"]["output"]
    states = [
        r["state"]
        for r in await seeded.fetch(
            "SELECT state FROM step_runs WHERE alert_id = $1 AND step_id = 's3' ORDER BY at, id", alert["id"]
        )
    ]
    assert states == ["pending_approval", "succeeded"]
    assert (
        await seeded.fetchval(
            "SELECT count(*) FROM deliveries WHERE alert_id = $1 AND idempotency_key LIKE '%:esc:%'",
            alert["id"],
        )
        == 1
    )

    # audit + timeline evidence
    audited = {
        r["action"]
        for r in await seeded.fetch(
            "SELECT action FROM audit_log WHERE target_id = ANY($1)", [str(alert["id"]), approval_id]
        )
    }
    assert {
        "runbook.step.succeeded",
        "runbook.step.pending_approval",
        "approval.request",
        "approval.approved",
    } <= audited
    timeline = [
        r["type"] for r in await seeded.fetch("SELECT type FROM timeline WHERE alert_id = $1", alert["id"])
    ]
    assert timeline.count("step_run") >= 4 and "approval" in timeline and "escalated" in timeline

    # deciding twice is a conflict
    assert (await decide(client, auth_headers, "root.settlements@demo", approval_id)).status_code == 409

    events = await collect_sse(pubsub, {"approval.requested", "approval.decided", "alert.updated"})
    assert {"approval.requested", "approval.decided", "alert.updated"} <= events.keys()
    assert events["approval.decided"]["status"] == "approved"
    await pubsub.aclose()


async def test_rejected_approval_skips_the_step(client, auth_headers, seeded, gw):
    alert = await make_alert(seeded)
    step = (await run(client, auth_headers, "piotr@demo", alert, "s3")).json()
    assert step["state"] == "pending_approval"
    r = await decide(client, auth_headers, "root.settlements@demo", step["approvalId"], "reject", "not now")
    assert r.status_code == 200 and r.json()["status"] == "rejected"
    detail = (await client.get(f"/v1/alerts/{alert['id']}", headers=await auth_headers("piotr@demo"))).json()
    s3 = detail["runbook"]["steps"][2]
    assert s3["state"] == "skipped" and "rejected by Stefan Settlements: not now" in s3["lastRun"]["output"]
    assert (
        await seeded.fetchval(
            "SELECT count(*) FROM deliveries WHERE alert_id = $1 AND idempotency_key LIKE '%:esc:%'",
            alert["id"],
        )
        == 0
    )
    assert (
        await seeded.fetchval(
            "SELECT count(*) FROM audit_log WHERE action = 'approval.rejected' AND target_id = $1",
            step["approvalId"],
        )
        == 1
    )
    # a rejected step can be requested again
    again = (await run(client, auth_headers, "piotr@demo", alert, "s3")).json()
    assert again["state"] == "pending_approval" and again["approvalId"] != step["approvalId"]


async def test_failed_action_is_recorded_as_failed(client, auth_headers, seeded, gw):
    alert = await make_alert(seeded)
    await gw.post("/admin/fail", json={"channel": "workflow", "count": 1})
    step = (await run(client, auth_headers, "piotr@demo", alert, "s2")).json()
    assert step["state"] == "failed" and "HTTP 500" in step["lastRun"]["output"]
    # the failure demoted retry_workflow: the next run needs an approval
    nxt = (await run(client, auth_headers, "piotr@demo", alert, "s2")).json()
    assert nxt["state"] == "pending_approval" and nxt["trustLevel"] == "run_after_approval"


async def test_check_failing_does_not_demote_trust(client, auth_headers, seeded, gw):
    alert = await make_alert(seeded)
    await gw.post("/admin/fail", json={"channel": "svc:counterparty-service", "seconds": 30})
    step = (await run(client, auth_headers, "piotr@demo", alert, "s1")).json()
    assert step["state"] == "failed" and "HTTP 503" in step["lastRun"]["output"]
    again = (await run(client, auth_headers, "piotr@demo", alert, "s1")).json()
    assert again["state"] == "failed"  # still runs directly


async def test_expired_approval_is_marked_and_cannot_be_decided(client, auth_headers, seeded, gw):
    from app.runbooks.approvals import request_approval

    alert = await make_alert(seeded)
    step = (await run(client, auth_headers, "piotr@demo", alert, "s3")).json()
    await seeded.execute(
        "UPDATE approvals SET expires_at = now() - interval '1 second' WHERE id = $1::uuid",
        step["approvalId"],
    )
    r = await decide(client, auth_headers, "root.settlements@demo", step["approvalId"])
    assert r.status_code == 409
    row = await seeded.fetchrow("SELECT status FROM approvals WHERE id = $1::uuid", step["approvalId"])
    assert row["status"] == "expired"
    state = await seeded.fetchval(
        "SELECT state FROM step_runs WHERE approval_id = $1::uuid ORDER BY at DESC, id DESC LIMIT 1",
        step["approvalId"],
    )
    assert state == "skipped"

    async with seeded.acquire() as conn:
        created = await request_approval(
            conn,
            origin="agent_request",
            action="terraform_apply",
            parameters={},
            reason="x",
            risk="high",
            alert_id=None,
            requested_by={
                "type": "agent",
                "id": "claude-code",
                "name": "Claude Code",
                "userId": str(user("anna@demo")),
            },
            ttl_seconds=-1,
        )
    listed = await client.get("/v1/approvals?status=expired", headers=await auth_headers("master@demo"))
    assert created["id"] in [a["id"] for a in listed.json()]


async def test_text_step_is_marked_done_and_errors(client, auth_headers, seeded):
    rb = uuid7()
    await seeded.execute(
        "INSERT INTO runbooks (id, name, format, steps, attached, team_id) "
        "VALUES ($1, 'mixed', 'markdown', $2, '{}', $3)",
        rb,
        [
            {
                "id": "t1",
                "order": 1,
                "kind": "text",
                "text": "Call {{workflowName}} owner",
                "approval": "none",
            },
            {"id": "x1", "order": 2, "kind": "action", "action": "does_not_exist", "approval": "none"},
            {
                "id": "x2",
                "order": 3,
                "kind": "action",
                "action": "retry_workflow",
                "parameters": {"workflowId": ""},
                "approval": "none",
            },
        ],
        team("team-settlements"),
    )
    alert = await make_alert(seeded, runbook=rb)
    done = await run(client, auth_headers, "piotr@demo", alert, "t1")
    assert done.json()["state"] == "succeeded" and done.json()["text"] == "Call order-settlement owner"
    assert (await run(client, auth_headers, "piotr@demo", alert, "x1")).status_code == 422
    bad = await run(client, auth_headers, "piotr@demo", alert, "x2")
    assert bad.status_code == 422 and "parameters" in bad.json()["message"]
    assert (await run(client, auth_headers, "piotr@demo", alert, "nope")).status_code == 404


async def test_run_endpoint_scope_and_roles(client, auth_headers, seeded):
    alert = await make_alert(seeded)
    assert (await run(client, auth_headers, "anna@demo", alert, "s1")).status_code == 404  # other team
    assert (await run(client, auth_headers, "manager@demo", alert, "s1")).status_code == 403  # informed
    assert (await client.post(run_url(alert, "s1"))).status_code == 401
    no_runbook = await make_alert(seeded, team_slug="team-security", raw={"nothing": 1})
    assert (await run(client, auth_headers, "master@demo", no_runbook, "s1")).status_code == 404
    unknown = f"/v1/alerts/{uuid7()}/runbook/steps/s1/run"
    assert (await client.post(unknown, headers=await auth_headers("master@demo"))).status_code == 404


async def test_domain_root_and_master_can_run_steps(client, auth_headers, seeded, gw):
    alert = await make_alert(seeded)
    assert (await run(client, auth_headers, "root.settlements@demo", alert, "s1")).json()[
        "state"
    ] == "succeeded"
    assert (await run(client, auth_headers, "master@demo", alert, "s1")).json()["state"] == "succeeded"


async def test_high_risk_markdown_step_needs_approval_even_without_flag(client, auth_headers, seeded, gw):
    rb = uuid7()
    await seeded.execute(
        "INSERT INTO runbooks (id, name, format, steps, attached, team_id) "
        "VALUES ($1, 'risky', 'markdown', $2, '{}', $3)",
        rb,
        [
            {
                "id": "r1",
                "order": 1,
                "kind": "action",
                "action": "restart_service",
                "parameters": {"service": "{{workflowName}}"},
                "approval": "none",
            }
        ],
        team("team-settlements"),
    )
    alert = await make_alert(seeded, runbook=rb)
    step = (await run(client, auth_headers, "piotr@demo", alert, "r1")).json()
    assert step["state"] == "pending_approval" and step["risk"] == "high"
    assert (await gw.get("/admin/state")).json()["restarts"] == []
    r = await decide(client, auth_headers, "root.settlements@demo", step["approvalId"])
    assert r.json()["status"] == "approved"
    assert (await gw.get("/admin/state")).json()["restarts"][0]["service"] == "order-settlement"
