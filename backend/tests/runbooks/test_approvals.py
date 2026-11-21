"""Approvals: agent requests, scoped inbox, notifications, MCP wake-up, expiry, authorization."""

from __future__ import annotations

import asyncio
from uuid import UUID

import orjson
import pytest

from app.core.errors import AppError
from app.core.ids import uuid7
from app.core.redis import get_redis, rkey
from app.delivery.channels import registry as channels
from app.delivery.channels.base import ChannelError, SendResult
from app.runbooks.approvals import decide, request_approval

from .helpers import make_alert, user

AGENT = {
    "type": "agent",
    "id": "claude-code",
    "name": "Claude Code",
    "onBehalfOf": "Anna Kowalska",
    "userId": str(user("anna@demo")),
}


async def agent_request(pool, **kw):
    async with pool.acquire() as conn:
        return await request_approval(
            conn,
            origin="agent_request",
            action="terraform_apply",
            parameters={"workspace": "prod"},
            reason="Apply the reviewed plan",
            risk="high",
            alert_id=kw.pop("alert_id", None),
            requested_by=AGENT,
            **kw,
        )


async def test_agent_request_goes_to_the_requesters_domain_roots(seeded, client, auth_headers, gw):
    approval = await agent_request(seeded)
    assert approval["status"] == "pending" and approval["origin"] == "agent_request"
    assert approval["requestedBy"] == {
        "kind": "agent",
        "id": "claude-code",
        "displayName": "Claude Code",
        "onBehalfOf": "Anna Kowalska",
    }
    assert [a["displayName"] for a in approval["approvers"]] == ["Paulina Payments"]
    assert approval["alertId"] is None and approval["risk"] == "high"
    texts = [m["text"] for m in (await gw.get("/admin/state")).json()["sms"] if m["to"] == "+48500100110"]
    assert texts and f"APPROVE {approval['shortId']}" in texts[0] and "REJECT" in texts[0]

    assert (await decide_api(client, auth_headers, "anna@demo", approval["id"])).status_code == 403
    assert (
        await decide_api(client, auth_headers, "root.settlements@demo", approval["id"])
    ).status_code == 403
    ok = await decide_api(client, auth_headers, "root.payments@demo", approval["id"], "reject", "no")
    assert ok.status_code == 200 and ok.json()["status"] == "rejected" and ok.json()["comment"] == "no"


async def decide_api(client, auth_headers, email, approval_id, decision="approve", comment=None):
    return await client.post(
        f"/v1/approvals/{approval_id}/decision",
        headers=await auth_headers(email),
        json={"decision": decision, "comment": comment},
    )


async def test_master_can_always_decide_and_short_id_resolves(seeded, gw):
    approval = await agent_request(seeded)
    async with seeded.acquire() as conn:
        result = await decide(
            conn,
            approval["shortId"],  # SMS replies use the numeric short id
            decision="approve",
            actor={"type": "user", "id": str(user("master@demo")), "name": "Mira Master"},
            comment=None,
        )
    assert result["status"] == "approved" and result["decidedBy"]["displayName"] == "Mira Master"


async def test_decide_rejects_agents_and_bad_input(seeded, gw):
    approval = await agent_request(seeded)
    async with seeded.acquire() as conn:
        with pytest.raises(AppError) as agent:
            await decide(conn, approval["id"], decision="approve", actor=AGENT, comment=None)
        assert agent.value.status_code == 403
        with pytest.raises(AppError) as bad:
            await decide(conn, approval["id"], decision="maybe", actor=AGENT, comment=None)
        assert bad.value.status_code == 422
        with pytest.raises(AppError) as missing:
            await decide(
                conn,
                uuid7(),
                decision="approve",
                actor={"type": "user", "id": str(user("master@demo")), "name": "m"},
                comment=None,
            )
        assert missing.value.status_code == 404


async def test_wake_message_for_mcp_long_poll(seeded, gw):
    approval = await agent_request(seeded)
    pubsub = get_redis().pubsub()
    await pubsub.subscribe(rkey("appr", "wait", approval["id"]))
    async with seeded.acquire() as conn:
        await decide(
            conn,
            approval["id"],
            decision="approve",
            actor={"type": "user", "id": str(user("root.payments@demo")), "name": "Paulina Payments"},
            comment="ship it",
        )
    message = None
    for _ in range(10):
        message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.3)
        if message:
            break
    await pubsub.aclose()
    payload = orjson.loads(message["data"])
    assert payload["status"] == "approved" and payload["comment"] == "ship it"
    assert payload["decidedBy"]["name"] == "Paulina Payments"


async def test_inbox_is_scoped_and_filterable(client, auth_headers, seeded, gw):
    alert = await make_alert(seeded)
    step = (
        await client.post(
            f"/v1/alerts/{alert['id']}/runbook/steps/s3/run", headers=await auth_headers("piotr@demo")
        )
    ).json()
    other = await agent_request(seeded)

    async def ids(email, query=""):
        r = await client.get(f"/v1/approvals{query}", headers=await auth_headers(email))
        assert r.status_code == 200
        return {a["id"] for a in r.json()}

    assert step["approvalId"] in await ids("piotr@demo")  # approver and requester
    assert step["approvalId"] in await ids("root.settlements@demo")
    assert step["approvalId"] in await ids("manager@demo")  # informed in the team: can read
    assert step["approvalId"] in await ids("master@demo")
    assert step["approvalId"] not in await ids("anna@demo")  # other team
    assert step["approvalId"] not in await ids("root.payments@demo")
    assert other["id"] not in await ids("piotr@demo")
    assert other["id"] in await ids("anna@demo")  # requester
    assert step["approvalId"] in await ids("piotr@demo", "?status=pending")
    assert step["approvalId"] not in await ids("piotr@demo", "?status=approved")
    assert (
        await client.get("/v1/approvals?status=bogus", headers=await auth_headers("piotr@demo"))
    ).status_code == 422
    assert (await client.get("/v1/approvals")).status_code == 401
    unknown = await decide_api(client, auth_headers, "master@demo", uuid7())
    assert unknown.status_code == 404
    assert (await decide_api(client, auth_headers, "master@demo", "12345678")).status_code == 404


async def test_unknown_origin_and_alert(seeded):
    async with seeded.acquire() as conn:
        with pytest.raises(AppError) as exc:
            await request_approval(
                conn,
                origin="nope",
                action="a",
                parameters={},
                reason=None,
                risk=None,
                alert_id=None,
                requested_by=AGENT,
            )
        assert exc.value.status_code == 422
        with pytest.raises(AppError) as missing:
            await request_approval(
                conn,
                origin="agent_request",
                action="a",
                parameters={},
                reason=None,
                risk=None,
                alert_id=uuid7(),
                requested_by=AGENT,
            )
        assert missing.value.status_code == 404


async def test_sms_failure_falls_back_to_email(seeded, gw, monkeypatch):
    class Broken:
        name = "sms"

        async def send(self, msg, key):
            raise ChannelError("gateway down")

    sent = []

    class Mail:
        name = "email"

        async def send(self, msg, key):
            sent.append(msg)
            return SendResult("delivered")

    restore = [channels.override("sms", Broken()), channels.override("email", Mail())]
    try:
        alert = await make_alert(seeded)
        approval = await agent_request(seeded, alert_id=alert["id"])
    finally:
        for r in restore:
            r()
    assert sent and "APPROVE" in sent[0].text and sent[0].subject.startswith("Approval needed")
    rows = await seeded.fetch(
        "SELECT channel, status, last_error FROM deliveries WHERE idempotency_key LIKE $1",
        f"appr:{approval['id']}:%",
    )
    assert rows and {r["channel"] for r in rows} == {"email"} and {r["status"] for r in rows} == {"delivered"}


async def test_all_channels_failing_is_recorded_not_raised(seeded, gw):
    class Broken:
        def __init__(self, name):
            self.name = name

        async def send(self, msg, key):
            await asyncio.sleep(0)
            raise ChannelError("down")

    restore = [channels.override("sms", Broken("sms")), channels.override("email", Broken("email"))]
    try:
        alert = await make_alert(seeded)
        approval = await agent_request(seeded, alert_id=alert["id"])
    finally:
        for r in restore:
            r()
    assert approval["status"] == "pending"
    statuses = await seeded.fetch(
        "SELECT status, last_error FROM deliveries WHERE idempotency_key LIKE $1", f"appr:{approval['id']}:%"
    )
    assert (
        statuses
        and {r["status"] for r in statuses} == {"dead_lettered"}
        and "down" in statuses[0]["last_error"]
    )


async def test_four_eyes_requester_cannot_approve_own_request(seeded):
    from uuid import uuid4

    from app.core.errors import AppError
    from app.runbooks.approvals import decide

    user_id = await seeded.fetchval("SELECT id FROM users WHERE email = 'root.settlements@demo'")
    async with seeded.acquire() as conn:
        approval_id = await conn.fetchval(
            "INSERT INTO approvals (id, status, origin, requested_by, action, parameters, risk, reason, "
            "approver_user_ids, expires_at, created_at) VALUES ($1, 'pending', 'runbook_step', $2, "
            "'restart_service', '{}', 'high', 'test', ARRAY[$3::uuid], now() + interval '15 minutes', now()) "
            "RETURNING id",
            uuid4(),
            {"type": "user", "id": str(user_id), "name": "Root"},
            user_id,
        )
        actor = {"type": "user", "id": str(user_id), "name": "Root"}
        try:
            await decide(conn, approval_id, decision="approve", actor=actor, comment=None)
        except AppError as exc:
            assert exc.status_code == 403 and "four-eyes" in exc.message
        else:
            raise AssertionError("self-approval must be refused")
        rejected = await decide(conn, approval_id, decision="reject", actor=actor, comment="withdrawn")
        assert rejected["status"] == "rejected"  # withdrawing your own request is fine


async def person_request(pool, email: str, alert_id=None):
    async with pool.acquire() as conn:
        return await request_approval(
            conn,
            origin="runbook_step",
            action="restart_service",
            parameters={"service": "payments-api"},
            reason="restart",
            risk="high",
            alert_id=alert_id,
            requested_by={"type": "user", "id": str(user(email)), "name": email},
        )


async def test_four_eyes_a_person_is_never_listed_as_approver_of_their_own_request(
    client, auth_headers, seeded, gw
):
    root = user("root.payments@demo")
    approval = await person_request(seeded, "root.payments@demo")  # the only Domain Root of payments
    assert approval["approvers"] and str(root) not in {a["id"] for a in approval["approvers"]}
    assert approval["requestedByUserId"] == str(root)
    stored = await seeded.fetchval(
        "SELECT approver_user_ids FROM approvals WHERE id = $1", UUID(approval["id"])
    )
    assert root not in stored  # fell back to the masters

    inbox = (await client.get("/v1/approvals", headers=await auth_headers("root.payments@demo"))).json()
    mine = next(a for a in inbox if a["id"] == approval["id"])
    assert mine["canDecide"] is False and mine["requestedByUserId"] == str(root)
    master = (await client.get("/v1/approvals", headers=await auth_headers("master@demo"))).json()
    assert next(a for a in master if a["id"] == approval["id"])["canDecide"] is True


async def test_agent_request_keeps_the_human_it_acts_for(client, auth_headers, seeded, gw):
    approval = await agent_request(seeded)
    assert approval["requestedByUserId"] == str(user("anna@demo"))
    inbox = (await client.get("/v1/approvals", headers=await auth_headers("root.payments@demo"))).json()
    assert next(a for a in inbox if a["id"] == approval["id"])["canDecide"] is True


async def test_legacy_row_listing_the_requester_hides_them(seeded):
    from app.runbooks.approvals import approvals_wire

    root = user("root.payments@demo")
    async with seeded.acquire() as conn:
        row = await conn.fetchrow(
            "INSERT INTO approvals (id, status, origin, requested_by, action, parameters, risk, reason, "
            "approver_user_ids, expires_at, created_at) VALUES ($1, 'pending', 'runbook_step', $2, "
            "'restart_service', '{}', 'high', 'test', ARRAY[$3::uuid, $4::uuid], "
            "now() + interval '15 minutes', now()) RETURNING *",
            uuid7(),
            {"type": "user", "id": str(root), "name": "Root"},
            root,
            user("master@demo"),
        )
        (wire,) = await approvals_wire(conn, [row], viewer=root)
    assert [a["id"] for a in wire["approvers"]] == [str(user("master@demo"))]
    assert wire["canDecide"] is False
