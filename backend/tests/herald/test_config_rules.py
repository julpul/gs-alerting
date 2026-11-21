"""`/api/rules`, `PUT /api/rules/{id}` (versions + hot reload), `/api/rules/test`, `/api/rules/preview`."""

from __future__ import annotations

import asyncio
from uuid import UUID, uuid4

from app.pipeline.rules.index import RuleIndex
from tests.herald.config_helpers import ANNA, MASTER, brief_rules, eventually, push_recent, sample


def _rates_doc(name: str) -> dict:
    doc = next(d for d in brief_rules() if d["id"] == "rates-large-trade-rejected")
    return {**doc, "name": name}


async def test_list_rules_shape(client):
    resp = await client.get("/api/rules", headers=MASTER)
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) >= 5
    row = next(r for r in rows if r["doc"]["name"].startswith("Settlements"))
    assert set(row) >= {"doc", "version", "source", "updated_at", "updated_by", "alerts", "last_fired"}
    assert row["source"] in ("seed", "api") and isinstance(row["alerts"], int)
    doc = row["doc"]
    assert set(doc) >= {
        "id",
        "name",
        "owner",
        "enabled",
        "severity",
        "when",
        "group",
        "notify",
        "present",
        "runbook",
    }
    assert UUID(doc["id"]) and doc["owner"] == "team:team-settlements"
    assert doc["when"]["all"][0] == {"field": "type", "op": "eq", "value": "WORKFLOW_FAILED"}
    assert doc["notify"][0] == {"to": "team:team-settlements/on-call", "channels": ["phone", "sms"]}
    assert doc["runbook"] and doc["present"]["colour"] == "red"


async def test_list_is_scoped(client):
    names = {r["doc"]["name"] for r in (await client.get("/api/rules", headers=ANNA)).json()}
    assert any(n.startswith("Anna") for n in names)
    assert not any(n.startswith("Rates") for n in names)


async def test_put_creates_versions_and_hot_reloads(client, seeded):
    index = RuleIndex()
    await index.load_all(seeded)
    task = await index.start_hot_reload(seeded)
    try:
        client_id = f"big-trades-{uuid4().hex[:6]}"
        started = asyncio.get_running_loop().time()
        resp = await client.put(
            f"/api/rules/{client_id}", json=_rates_doc("Herald big trades"), headers=MASTER
        )
        assert resp.status_code == 200, resp.text
        saved = resp.json()
        assert saved["version"] == 1
        rule_id = UUID(saved["id"])
        await eventually(lambda: asyncio.sleep(0, index.get(rule_id)), timeout=2.0)
        assert asyncio.get_running_loop().time() - started < 2.0

        again = await client.put(
            f"/api/rules/{client_id}", json=_rates_doc("Herald big trades v2"), headers=MASTER
        )
        assert again.json() == {"id": saved["id"], "version": 2}
        by_id = await client.put(
            f"/api/rules/{saved['id']}",
            json={**_rates_doc("Herald big trades v3"), "enabled": False},
            headers=MASTER,
        )
        assert by_id.json()["version"] == 3
        await eventually(lambda: asyncio.sleep(0, index.get(rule_id) is None), timeout=2.0)
    finally:
        task.cancel()

    row = await seeded.fetchrow("SELECT name, enabled, definition FROM rules WHERE id = $1", rule_id)
    assert row["name"] == "Herald big trades v3" and row["enabled"] is False
    assert row["definition"]["herald"]["clientId"] == client_id
    assert await seeded.fetchval("SELECT count(*) FROM rule_versions WHERE rule_id = $1", rule_id) == 3
    listed = next(
        r for r in (await client.get("/api/rules", headers=MASTER)).json() if r["doc"]["id"] == saved["id"]
    )
    assert listed["version"] == 3 and listed["source"] == "api" and listed["updated_by"] == "person:master"
    assert listed["doc"]["present"]["highlight"] == [] and listed["doc"]["enabled"] is False
    v1 = await client.get(f"/v1/rules/{saved['id']}", headers=await _bearer(client))
    assert "herald" not in v1.json()


async def _bearer(client) -> dict:
    resp = await client.post("/v1/auth/login", json={"email": "master@demo", "password": "demo"})
    return {"Authorization": f"Bearer {resp.json()['accessToken']}"}


async def test_put_person_owner_reads_back_as_team_and_keeps_highlight(client):
    doc = {
        **next(d for d in brief_rules() if d["id"] == "anna-payments-critical"),
        "name": f"Anna {uuid4().hex[:6]}",
    }
    doc["present"] = {**doc["present"], "highlight": ["labels.region"]}
    resp = await client.put("/api/rules/anna-x", json=doc, headers=ANNA)
    assert resp.status_code == 200, resp.text
    listed = next(
        r
        for r in (await client.get("/api/rules", headers=ANNA)).json()
        if r["doc"]["id"] == resp.json()["id"]
    )
    assert listed["doc"]["owner"] == "team:team-payments"
    assert listed["doc"]["present"]["highlight"] == ["labels.region"]
    assert listed["doc"]["notify"] == doc["notify"]


async def test_put_rejections(client):
    bad = {**_rates_doc("bad"), "when": {"not": {"field": "x", "op": "contains", "value": "y"}}}
    resp = await client.put("/api/rules/bad-one", json=bad, headers=MASTER)
    assert resp.status_code == 422 and "contains" in resp.json()["detail"]
    regex = {**_rates_doc("bad"), "when": {"field": "x", "op": "regex", "value": "(a+)+"}}
    resp = await client.put("/api/rules/bad-two", json=regex, headers=MASTER)
    assert resp.status_code == 422 and "regex" in resp.json()["detail"]
    resp = await client.put("/api/rules/other-team", json=_rates_doc("not mine"), headers=ANNA)
    assert resp.status_code == 403
    rates = next(
        r
        for r in (await client.get("/api/rules", headers=MASTER)).json()
        if r["doc"]["name"].startswith("Rates")
    )
    resp = await client.put(f"/api/rules/{rates['doc']['id']}", json=rates["doc"], headers=ANNA)
    assert resp.status_code == 403


async def test_rule_test_with_events_and_recent(client, seeded, redis):
    when = {
        "all": [
            {"field": "payload.desk", "op": "eq", "value": "rates"},
            {"field": "payload.notional", "op": "gt", "value": 10_000_000},
        ]
    }
    small = sample("trade_rejected")
    small["payload"]["notional"] = 5
    resp = await client.post(
        "/api/rules/test", json={"when": when, "events": [sample("trade_rejected"), small]}, headers=MASTER
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert (body["matched"], body["total"]) == (1, 2)
    miss = body["results"][1]
    assert set(miss) >= {"id", "source", "received_at", "passed", "failed_because", "event", "attrs"}
    assert (
        miss["failed_because"][0]["field"] == "payload.notional" and miss["failed_because"][0]["actual"] == 5
    )

    source_id = await seeded.fetchval("SELECT id FROM sources WHERE name = 'trade-gateway'")
    await push_recent(
        redis, source_id, sample("trade_rejected"), {"type": "trade.rejected.v2"}, f"ev-{uuid4()}"
    )
    recent = (
        await client.post("/api/rules/test", json={"when": when, "source": "trade-gateway"}, headers=MASTER)
    ).json()
    assert (
        recent["total"] >= 1
        and recent["results"][0]["passed"]
        and recent["results"][0]["source"] == "trade-gateway"
    )
    assert recent["results"][0]["attrs"]["type"] == "trade.rejected.v2"
    resp = await client.post(
        "/api/rules/test", json={"when": {"field": "a", "op": "nope", "value": 1}}, headers=MASTER
    )
    assert resp.status_code == 422


async def test_preview_shape(client):
    doc = next(d for d in brief_rules() if d["id"] == "settlements-order-settlement-failed")
    resp = await client.post(
        "/api/rules/preview",
        json={"rule": doc, "event": sample("workflow_failed"), "source": "workflow-broker"},
        headers=MASTER,
    )
    assert resp.status_code == 200, resp.text
    p = resp.json()
    assert set(p) >= {
        "matches",
        "severity",
        "colour",
        "title",
        "body",
        "fields",
        "sms",
        "voice",
        "email",
        "notify",
        "runbook",
    }
    assert p["matches"] is True and p["severity"] == "critical" and p["colour"] == "red"
    assert p["title"] == "Settlement failed at validate-counterparty"
    assert p["body"] == "TIMEOUT: Counterparty service did not respond in 5000 ms"
    assert {"path": "step", "value": "validate-counterparty", "highlight": True} in p["fields"]
    assert len(p["sms"]) <= 160 and "A-123" in p["sms"]
    assert "acknowledge" in p["voice"].lower() and "Settlement failed" in p["email"]
    assert all(
        set(n) >= {"recipient", "channel"} and n["recipient"].startswith("person:") for n in p["notify"]
    )
    assert {n["channel"] for n in p["notify"]} & {"phone", "sms", "ui"}

    other = {**doc, "runbook": None, "severity": None}
    trade = (
        await client.post(
            "/api/rules/preview", json={"rule": other, "event": sample("trade_rejected")}, headers=MASTER
        )
    ).json()
    assert trade["matches"] is False and trade["runbook"] is None
    resp = await client.post(
        "/api/rules/preview", json={"rule": {**doc, "owner": "team:x"}, "event": {}}, headers=MASTER
    )
    assert resp.status_code == 422 and "owner" in resp.json()["detail"]


async def test_preview_renders_the_rule_runbook(client):
    rows = (await client.get("/api/rules", headers=MASTER)).json()
    settle = next(r for r in rows if r["doc"]["name"].startswith("Settlements"))["doc"]
    p = (
        await client.post(
            "/api/rules/preview", json={"rule": settle, "event": sample("workflow_failed")}, headers=MASTER
        )
    ).json()
    assert p["runbook"]["id"] == settle["runbook"]
    assert p["runbook"]["steps"][0]["id"] == 1
    assert any("WF-20261023-00042" in str(s["params"]) for s in p["runbook"]["steps"])
