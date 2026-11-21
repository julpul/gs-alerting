"""Agent requests for a person to confirm: scoped inbox, no SMS/email, MCP wake-up, expiry, authorization."""

from __future__ import annotations

import orjson
import pytest

from app.core.errors import AppError
from app.core.ids import uuid7
from app.core.redis import get_redis, rkey
from app.runbooks.approvals import decide, request_approval
from app.runbooks.runner import run_step

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
    sms = (await gw.get("/admin/state")).json()["sms"]
    assert not [m for m in sms if "APPROVE" in m["text"]]  # nothing is texted any more

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
    async with seeded.acquire() as conn:
        outcome = await run_step(conn, alert, "s3", {**AGENT, "userId": str(user("piotr@demo"))})
    step = {"approvalId": outcome.step.approvalId}
    other = await agent_request(seeded)

    async def ids(email, query=""):
        r = await client.get(f"/v1/approvals{query}", headers=await auth_headers(email))
        assert r.status_code == 200
        return {a["id"] for a in r.json()}

    assert step["approvalId"] in await ids("piotr@demo")  # the person the agent acts for
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


async def test_a_person_confirms_their_own_request(client, auth_headers, seeded, gw):
    """No four-eyes: the person an agent acts for (or who asked) can confirm it themselves."""
    alert = await make_alert(seeded)
    approval = await agent_request(seeded, alert_id=alert["id"])
    assert approval["requestedByUserId"] == str(user("anna@demo"))
    anna = await auth_headers("anna@demo")
    inbox = (await client.get("/v1/approvals", headers=anna)).json()
    assert next(a for a in inbox if a["id"] == approval["id"])["canDecide"] is False  # not on this team
    piotr = (await client.get("/v1/approvals", headers=await auth_headers("piotr@demo"))).json()
    assert next(a for a in piotr if a["id"] == approval["id"])["canDecide"] is True  # may act on the alert
    master = (await client.get("/v1/approvals", headers=await auth_headers("master@demo"))).json()
    assert next(a for a in master if a["id"] == approval["id"])["canDecide"] is True
    ok = await decide_api(client, auth_headers, "piotr@demo", approval["id"])
    assert ok.status_code == 200 and ok.json()["status"] == "approved"


async def test_requests_do_not_message_anyone(seeded, gw):
    alert = await make_alert(seeded)
    before = await seeded.fetchval("SELECT count(*) FROM deliveries WHERE alert_id = $1", alert["id"])
    await agent_request(seeded, alert_id=alert["id"])
    assert await seeded.fetchval("SELECT count(*) FROM deliveries WHERE alert_id = $1", alert["id"]) == before
    sms = (await gw.get("/admin/state")).json()["sms"]
    assert not [m for m in sms if "APPROVE" in m["text"] or "Approval needed" in m["text"]]
