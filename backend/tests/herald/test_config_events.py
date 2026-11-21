"""`/api/events/recent`, `/api/events/explain`, `/api/fields`."""

from __future__ import annotations

from uuid import uuid4

from tests.herald.config_helpers import ANNA, MASTER, push_recent, sample


async def test_explain_names_the_failed_condition(client):
    small = sample("trade_rejected")
    small["payload"]["notional"] = 2_000_000
    resp = await client.post(
        "/api/events/explain", json={"event": small, "source": "trade-gateway"}, headers=MASTER
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) >= {"attrs", "rules", "items"}
    assert body["attrs"]["type"] == "trade.rejected.v2"
    rates = next(r for r in body["rules"] if r["name"].startswith("Rates"))
    assert set(rates) >= {"id", "name", "owner", "enabled", "passed", "closes", "why", "failed_because"}
    assert rates["passed"] is False and rates["owner"] == "team:team-rates"
    assert rates["failed_because"] == [
        {
            "field": "payload.notional",
            "op": "gt",
            "value": 10000000,
            "actual": 2000000,
            "missing": False,
            "passed": False,
        }
    ]
    big = (
        await client.post(
            "/api/events/explain",
            json={"event": sample("trade_rejected"), "source": "trade-gateway"},
            headers=MASTER,
        )
    ).json()
    assert next(r for r in big["rules"] if r["name"].startswith("Rates"))["passed"] is True


async def test_explain_batches_text_and_closing_rules(client):
    doc = {
        "name": f"closer-{uuid4().hex[:6]}",
        "owner": "team:team-settlements",
        "when": {"field": "type", "op": "eq", "value": "JOB_FAILED"},
        "notify": [{"to": "team:team-settlements", "channels": ["ui"]}],
        "resolve_when": {"field": "type", "op": "eq", "value": "JOB_OK"},
    }
    saved = await client.put(f"/api/rules/{doc['name']}", json=doc, headers=MASTER)
    assert saved.status_code == 200
    body = (
        await client.post(
            "/api/events/explain",
            json={"event": [{"type": "JOB_FAILED"}, {"type": "JOB_OK"}]},
            headers=MASTER,
        )
    ).json()
    assert len(body["items"]) == 2
    first, second = ({r["id"]: r for r in item["rules"]}[saved.json()["id"]] for item in body["items"])
    assert first["passed"] and not first["closes"]
    assert second["closes"] and not second["passed"]
    text = (await client.post("/api/events/explain", json={"event": "plain words"}, headers=MASTER)).json()
    assert text["items"][0]["event"] == {"message": "plain words"}
    alert = {
        "receiver": "x",
        "groupKey": "g",
        "status": "firing",
        "alerts": [{"labels": {"alertname": "A"}}, {"labels": {"alertname": "B"}}],
    }
    split = (
        await client.post(
            "/api/events/explain", json={"event": alert, "source": "prometheus"}, headers=MASTER
        )
    ).json()
    assert len(split["items"]) == 2


async def test_recent_events_shape_fired_and_scope(client, seeded, redis):
    source_id = await seeded.fetchval("SELECT id FROM sources WHERE name = 'workflow-broker'")
    event_id = f"ev-{uuid4()}"
    meta = {
        "type": "WORKFLOW_FAILED",
        "severity": "error",
        "service": "order-settlement",
        "timestamp": "2026-10-23T09:14:02Z",
    }
    await push_recent(redis, source_id, sample("workflow_failed"), meta, event_id)
    rows = (await client.get("/api/events/recent?limit=200", headers=MASTER)).json()
    mine = next(r for r in rows if r["id"] == event_id)
    assert set(mine) >= {"id", "source", "received_at", "raw", "attrs", "fired", "closed"}
    assert mine["source"] == "workflow-broker" and mine["attrs"]["service"] == "order-settlement"
    settle = next(
        r
        for r in (await client.get("/api/rules", headers=MASTER)).json()
        if r["doc"]["name"].startswith("Settlements")
    )
    assert settle["doc"]["id"] in mine["fired"] and mine["closed"] == []
    only = (await client.get("/api/events/recent?source=workflow-broker&limit=5", headers=MASTER)).json()
    assert only and all(r["source"] == "workflow-broker" for r in only)
    anna = (await client.get("/api/events/recent?limit=200", headers=ANNA)).json()
    assert all(r["id"] != event_id for r in anna)
    assert (await client.get("/api/events/recent?limit=500", headers=MASTER)).status_code == 422


async def test_fields_from_recent_events(client, seeded, redis):
    source_id = await seeded.fetchval("SELECT id FROM sources WHERE name = 'pricing-logs'")
    await push_recent(
        redis, source_id, sample("pricing_log"), {"service": "pricing-engine-3"}, f"ev-{uuid4()}"
    )
    fields = (await client.get("/api/fields?source=pricing-logs", headers=MASTER)).json()
    by_path = {f["path"]: f for f in fields}
    assert set(by_path["instrument"]) >= {"source", "path", "type", "example", "seen"}
    assert by_path["latencyMs"]["type"] == "int" and by_path["latencyMs"]["example"] == "1850"
    assert by_path["@service"]["example"] == "pricing-engine-3"
    assert (await client.get("/api/fields?source=pricing-logs", headers=ANNA)).json() == []
