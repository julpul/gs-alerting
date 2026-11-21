"""Alerts REST: list, detail, ack, resolve, assign, comments."""

from __future__ import annotations

import sys
import uuid
from datetime import UTC, datetime, timedelta
from types import ModuleType

import orjson

from app.core.ids import format_short_id
from app.core.sse import team_channel
from tests.api import alerts_helpers as h


def _tag() -> str:
    return uuid.uuid4().hex[:8]


async def _next_event(pubsub, wait: float = 3.0) -> dict:
    deadline = datetime.now(UTC) + timedelta(seconds=wait)
    while datetime.now(UTC) < deadline:
        msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.2)
        if msg and msg["type"] == "message":
            return orjson.loads(msg["data"])
    raise AssertionError("no SSE message received")


# ---------------------------------------------------------------- list


async def test_list_is_scoped_and_hides_resolved_by_default(client, seeded, auth_headers):
    tag = _tag()
    mine = await h.make_alert(seeded, title=f"mine {tag}")
    other_team = await h.make_alert(
        seeded, team_slug="team-settlements", domain_slug="settlements", title=f"other {tag}"
    )
    done = await h.make_alert(seeded, title=f"done {tag}", status="resolved")

    resp = await client.get("/v1/alerts", params={"q": tag}, headers=await auth_headers("anna@demo"))
    assert resp.status_code == 200
    ids = {a["id"] for a in resp.json()["items"]}
    assert str(mine["id"]) in ids
    assert str(other_team["id"]) not in ids
    assert str(done["id"]) not in ids

    resp = await client.get("/v1/alerts", params={"q": tag}, headers=await auth_headers("master@demo"))
    master_ids = {a["id"] for a in resp.json()["items"]}
    assert {str(mine["id"]), str(other_team["id"])} <= master_ids
    assert str(done["id"]) not in master_ids


async def test_list_item_shape(client, seeded, auth_headers):
    tag = _tag()
    deadline = datetime.now(UTC) + timedelta(minutes=5)
    row = await h.make_alert(
        seeded,
        title=f"shape {tag}",
        severity="critical",
        flags=["from_agent", "bogus"],
        ack_deadline=deadline,
    )
    resp = await client.get("/v1/alerts", params={"q": tag}, headers=await auth_headers("anna@demo"))
    item = resp.json()["items"][0]
    assert item["shortId"] == format_short_id(row["short_id"])
    assert item["severity"] == "critical"
    assert item["teamName"] == "Team Payments"
    assert item["sourceName"] == "Test source"
    assert item["ruleName"] == "Test rule"
    assert item["flags"] == ["from_agent"]
    assert item["escalatesAt"] == item["ackDeadline"] is not None
    assert item["assignee"] is None
    assert item["escalationLevel"] == 1


async def test_list_filters(client, seeded, auth_headers):
    tag = _tag()
    await h.make_alert(seeded, title=f"f-crit {tag}", severity="critical")
    await h.make_alert(seeded, title=f"f-warn {tag}", severity="warning", status="acknowledged")
    await h.make_alert(seeded, title=f"f-info {tag}", severity="info", status="resolved")
    headers = await auth_headers("master@demo")

    async def titles(**params):
        resp = await client.get("/v1/alerts", params={"q": tag, **params}, headers=headers)
        assert resp.status_code == 200, resp.text
        return {a["title"] for a in resp.json()["items"]}

    assert await titles() == {f"f-crit {tag}", f"f-warn {tag}"}
    assert await titles(severity="critical") == {f"f-crit {tag}"}
    assert await titles(severity="critical,warning") == {f"f-crit {tag}", f"f-warn {tag}"}
    assert await titles(status="resolved") == {f"f-info {tag}"}
    assert await titles(status="open,resolved") == {f"f-crit {tag}", f"f-info {tag}"}
    assert await titles(teamId=str(h.team("team-settlements"))) == set()
    assert await titles(teamId=str(h.team("team-payments")), domainId=str(h.domain("payments"))) == {
        f"f-crit {tag}",
        f"f-warn {tag}",
    }
    assert await titles(sourceId=str(h.SOURCE_ID)) == {f"f-crit {tag}", f"f-warn {tag}"}
    assert await titles(q=f"f-crit {tag}") == {f"f-crit {tag}"}


async def test_list_q_escapes_like_wildcards(client, seeded, auth_headers):
    tag = _tag()
    await h.make_alert(seeded, title=f"100% done {tag}")
    await h.make_alert(seeded, title=f"1000 done {tag}")
    resp = await client.get(
        "/v1/alerts", params={"q": f"100% done {tag}"}, headers=await auth_headers("anna@demo")
    )
    assert [a["title"] for a in resp.json()["items"]] == [f"100% done {tag}"]


async def test_list_pagination_is_stable_across_pages(client, seeded, auth_headers):
    tag = _tag()
    base = datetime.now(UTC) - timedelta(hours=1)
    created = []
    for i in range(7):
        # i=3 and i=4 share a timestamp: the id tie-breaker must keep pages disjoint and complete
        ts = base + timedelta(seconds=min(i, 3))
        row = await h.make_alert(seeded, title=f"page {tag} {i}", last_seen_at=ts)
        created.append(str(row["id"]))
    headers = await auth_headers("anna@demo")

    seen: list[str] = []
    cursor = None
    pages = 0
    while True:
        params = {"q": tag, "limit": 3}
        if cursor:
            params["cursor"] = cursor
        body = (await client.get("/v1/alerts", params=params, headers=headers)).json()
        seen.extend(a["id"] for a in body["items"])
        pages += 1
        cursor = body["nextCursor"]
        if cursor is None:
            break
    assert pages == 3
    assert sorted(seen) == sorted(created)
    assert len(set(seen)) == 7
    full = (await client.get("/v1/alerts", params={"q": tag, "limit": 50}, headers=headers)).json()["items"]
    assert [a["id"] for a in full] == seen
    stamps = [a["lastSeenAt"] for a in full]
    assert stamps == sorted(stamps, reverse=True)


async def test_list_invalid_params(client, seeded, auth_headers):
    headers = await auth_headers("anna@demo")
    assert (await client.get("/v1/alerts", params={"status": "bogus"}, headers=headers)).status_code == 422
    assert (await client.get("/v1/alerts", params={"severity": "loud"}, headers=headers)).status_code == 422
    assert (await client.get("/v1/alerts", params={"teamId": "nope"}, headers=headers)).status_code == 422
    assert (await client.get("/v1/alerts", params={"cursor": "###"}, headers=headers)).status_code == 422
    assert (await client.get("/v1/alerts", params={"limit": 201}, headers=headers)).status_code == 422


async def test_list_requires_auth(client):
    assert (await client.get("/v1/alerts")).status_code == 401


# ---------------------------------------------------------------- detail


async def test_detail_has_everything(client, seeded, auth_headers):
    fields = [{"label": "Service", "value": "api"}, {"label": "Retries", "value": 3}]
    row = await h.make_alert(seeded, title=f"detail {_tag()}", fields=fields)
    first = await h.make_delivery(seeded, row["id"], channel="sms", status="delivered")
    await h.make_delivery(seeded, row["id"], channel="email", kind="soft", user_email=None, status="retrying")
    system = {"type": "system", "name": "system"}
    for kind, text, ago in (("created", "Created", 2), ("notified", "Notified", 0)):
        await seeded.execute(
            "INSERT INTO timeline (id, alert_id, at, type, actor, text) "
            "VALUES ($1, $2, now() - $3 * interval '1 minute', $4, $5, $6)",
            uuid.uuid4(),
            row["id"],
            ago,
            kind,
            system,
            text,
        )
    await seeded.execute(
        "INSERT INTO suggestions (id, alert_id, status, content) VALUES ($1, $2, 'draft', $3)",
        uuid.uuid4(),
        row["id"],
        {"diagnosis": "Old", "confidence": 0.1},
    )
    newest = uuid.uuid4()
    await seeded.execute(
        "INSERT INTO suggestions (id, alert_id, status, content, created_at) "
        "VALUES ($1, $2, 'draft', $3, now() + interval '1 second')",
        newest,
        row["id"],
        {"diagnosis": "Latest", "nextStep": "Restart", "confidence": 0.8},
    )

    resp = await client.get(f"/v1/alerts/{row['id']}", headers=await auth_headers("anna@demo"))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["id"] == str(row["id"])
    assert body["body"] == "Body"
    assert body["fields"] == [{"label": "Service", "value": "api"}, {"label": "Retries", "value": "3"}]
    assert body["rawEvent"] == {"type": "TEST", "n": 1}
    assert body["groupedItems"] == []
    assert body["runbook"] is None
    assert body["suggestion"]["id"] == str(newest)
    assert body["suggestion"]["diagnosis"] == "Latest"
    assert body["suggestion"]["status"] == "draft"
    assert [t["type"] for t in body["timeline"]] == ["created", "notified"]
    assert body["timeline"][0]["actor"] == {
        "kind": "system",
        "id": "system",
        "displayName": "system",
        "onBehalfOf": None,
    }
    deliveries = {d["channel"]: d for d in body["deliveries"]}
    assert deliveries["sms"]["id"] == str(first)
    assert deliveries["sms"]["recipient"]["displayName"] == "Anna Kowalska"
    assert deliveries["sms"]["kind"] == "hard"
    assert deliveries["sms"]["attempts"] == 1
    assert deliveries["email"]["recipient"]["displayName"] == "+48500000000"
    assert deliveries["email"]["status"] == "retrying"


async def test_detail_accepts_short_id(client, seeded, auth_headers):
    row = await h.make_alert(seeded, title=f"short {_tag()}")
    headers = await auth_headers("anna@demo")
    for ref in (format_short_id(row["short_id"]), format_short_id(row["short_id"]).lower()):
        resp = await client.get(f"/v1/alerts/{ref}", headers=headers)
        assert resp.status_code == 200
        assert resp.json()["id"] == str(row["id"])


async def test_detail_unknown_and_garbage_ids_are_404(client, seeded, auth_headers):
    headers = await auth_headers("anna@demo")
    assert (await client.get(f"/v1/alerts/{uuid.uuid4()}", headers=headers)).status_code == 404
    assert (await client.get("/v1/alerts/A-999999999", headers=headers)).status_code == 404
    resp = await client.get("/v1/alerts/not-an-id", headers=headers)
    assert resp.status_code == 404
    assert resp.json()["code"] == "not_found"


async def test_detail_runbook_comes_from_lazy_lookup(client, seeded, auth_headers, monkeypatch):
    row = await h.make_alert(seeded, title=f"rb {_tag()}")

    async def runbook_for_alert(conn, alert):
        assert alert["id"] == row["id"]
        return ModuleType("view")  # no model_dump: falls back to dict() which fails -> handled

    pkg, mod = ModuleType("app.runbooks"), ModuleType("app.runbooks.lookup")
    mod.runbook_for_alert = runbook_for_alert
    monkeypatch.setitem(sys.modules, "app.runbooks", pkg)
    monkeypatch.setitem(sys.modules, "app.runbooks.lookup", mod)
    headers = await auth_headers("anna@demo")
    resp = await client.get(f"/v1/alerts/{row['id']}", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["runbook"] is None  # broken lookup never breaks the detail

    class View:
        def model_dump(self, mode="python"):
            return {"id": "rb1", "name": "Restart", "format": "text", "steps": [{"id": "s1", "order": 1}]}

    async def good(conn, alert):
        return View()

    mod.runbook_for_alert = good
    body = (await client.get(f"/v1/alerts/{row['id']}", headers=headers)).json()
    assert body["runbook"]["name"] == "Restart"
    assert body["runbook"]["steps"][0]["id"] == "s1"


# ---------------------------------------------------------------- ack


async def test_ack_clears_deadlines_and_records_everything(client, seeded, redis, auth_headers):
    deadline = datetime.now(UTC) + timedelta(minutes=5)
    row = await h.make_alert(seeded, title=f"ack {_tag()}", ack_deadline=deadline)
    await seeded.execute("UPDATE alerts SET next_renotify_at = $2 WHERE id = $1", row["id"], deadline)
    delivery = await h.make_delivery(seeded, row["id"], status="sent")
    soft = await h.make_delivery(seeded, row["id"], kind="soft", channel="email", status="sent")
    pubsub = redis.pubsub()
    await pubsub.subscribe(team_channel(row["team_id"]))
    headers = await auth_headers("anna@demo")

    resp = await client.post(f"/v1/alerts/{format_short_id(row['short_id'])}/ack", headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "acknowledged"
    assert body["ackDeadline"] is None

    stored = await seeded.fetchrow("SELECT * FROM alerts WHERE id = $1", row["id"])
    assert stored["status"] == "acknowledged"
    assert stored["ack_deadline"] is None
    assert stored["next_renotify_at"] is None
    assert stored["acked_at"] is not None
    assert stored["acked_by"] == h.user("anna@demo")
    assert await seeded.fetchval("SELECT status FROM deliveries WHERE id = $1", delivery) == "acked"
    assert await seeded.fetchval("SELECT status FROM deliveries WHERE id = $1", soft) == "sent"
    timeline = await seeded.fetch("SELECT type, actor FROM timeline WHERE alert_id = $1", row["id"])
    assert [t["type"] for t in timeline] == ["acked"]
    assert timeline[0]["actor"]["name"] == "Anna Kowalska"
    assert (
        await seeded.fetchval(
            "SELECT count(*) FROM audit_log WHERE action = 'alert.ack' AND target_id = $1", str(row["id"])
        )
        == 1
    )

    event = await _next_event(pubsub)
    assert event["type"] == "alert.acked"
    assert event["data"]["alertId"] == str(row["id"])
    assert event["data"]["by"]["displayName"] == "Anna Kowalska"
    await pubsub.aclose()


async def test_ack_twice_is_idempotent(client, seeded, auth_headers):
    row = await h.make_alert(seeded, title=f"ack2 {_tag()}")
    headers = await auth_headers("anna@demo")
    assert (await client.post(f"/v1/alerts/{row['id']}/ack", headers=headers)).status_code == 200
    resp = await client.post(f"/v1/alerts/{row['id']}/ack", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["status"] == "acknowledged"
    assert await seeded.fetchval("SELECT count(*) FROM timeline WHERE alert_id = $1", row["id"]) == 1


async def test_ack_escalated_alert_and_resolved_conflict(client, seeded, auth_headers):
    escalated = await h.make_alert(seeded, title=f"esc {_tag()}", status="escalated")
    resolved = await h.make_alert(seeded, title=f"res {_tag()}", status="resolved")
    headers = await auth_headers("anna@demo")
    assert (await client.post(f"/v1/alerts/{escalated['id']}/ack", headers=headers)).json()[
        "status"
    ] == "acknowledged"
    resp = await client.post(f"/v1/alerts/{resolved['id']}/ack", headers=headers)
    assert resp.status_code == 409
    assert resp.json()["code"] == "conflict"


async def test_ack_denied_for_informed_and_invisible(client, seeded, auth_headers):
    row = await h.make_alert(seeded, title=f"deny {_tag()}")
    resp = await client.post(f"/v1/alerts/{row['id']}/ack", headers=await auth_headers("manager@demo"))
    assert resp.status_code == 403
    resp = await client.post(f"/v1/alerts/{row['id']}/ack", headers=await auth_headers("piotr@demo"))
    assert resp.status_code == 404
    assert await seeded.fetchval("SELECT status FROM alerts WHERE id = $1", row["id"]) == "open"


async def test_domain_root_and_master_can_ack(client, seeded, auth_headers):
    first = await h.make_alert(seeded, title=f"root {_tag()}")
    second = await h.make_alert(seeded, title=f"master {_tag()}")
    assert (
        await client.post(f"/v1/alerts/{first['id']}/ack", headers=await auth_headers("root.payments@demo"))
    ).status_code == 200
    assert (
        await client.post(f"/v1/alerts/{second['id']}/ack", headers=await auth_headers("master@demo"))
    ).status_code == 200
    other_root = await client.post(
        f"/v1/alerts/{(await h.make_alert(seeded, title=f'x {_tag()}'))['id']}/ack",
        headers=await auth_headers("root.settlements@demo"),
    )
    assert other_root.status_code == 404


# ---------------------------------------------------------------- resolve


async def test_resolve_writes_resolution_with_knowledge_key(client, seeded, redis, auth_headers):
    row = await h.make_alert(
        seeded, title=f"resolve {_tag()}", knowledge_key="kk-abc", ack_deadline=datetime.now(UTC)
    )
    pubsub = redis.pubsub()
    await pubsub.subscribe(team_channel(row["team_id"]))
    headers = await auth_headers("anna@demo")

    resp = await client.post(
        f"/v1/alerts/{row['id']}/resolve", json={"resolutionNote": "Restarted the pod"}, headers=headers
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "resolved"

    stored = await seeded.fetchrow("SELECT * FROM alerts WHERE id = $1", row["id"])
    assert stored["status"] == "resolved"
    assert stored["resolved_at"] is not None
    assert stored["ack_deadline"] is None
    res = await seeded.fetchrow("SELECT * FROM resolutions WHERE alert_id = $1", row["id"])
    assert res["knowledge_key"] == "kk-abc"
    assert res["note"] == "Restarted the pod"
    assert res["domain_id"] == row["domain_id"]
    assert res["team_id"] == row["team_id"]
    assert res["resolved_by"] == h.user("anna@demo")
    timeline = await seeded.fetchrow("SELECT * FROM timeline WHERE alert_id = $1", row["id"])
    assert timeline["type"] == "resolved"
    assert timeline["data"]["resolutionNote"] == "Restarted the pod"
    assert (
        await seeded.fetchval(
            "SELECT count(*) FROM audit_log WHERE action = 'alert.resolve' AND target_id = $1", str(row["id"])
        )
        == 1
    )
    event = await _next_event(pubsub)
    assert event["type"] == "alert.resolved"
    await pubsub.aclose()


async def test_resolve_without_body_or_note_and_twice(client, seeded, auth_headers):
    row = await h.make_alert(seeded, title=f"nonote {_tag()}")
    headers = await auth_headers("anna@demo")
    assert (await client.post(f"/v1/alerts/{row['id']}/resolve", headers=headers)).status_code == 200
    assert await seeded.fetchval("SELECT count(*) FROM resolutions WHERE alert_id = $1", row["id"]) == 0
    again = await client.post(
        f"/v1/alerts/{row['id']}/resolve", json={"resolutionNote": "late"}, headers=headers
    )
    assert again.status_code == 200
    assert await seeded.fetchval("SELECT count(*) FROM resolutions WHERE alert_id = $1", row["id"]) == 0
    assert await seeded.fetchval("SELECT count(*) FROM timeline WHERE alert_id = $1", row["id"]) == 1


async def test_resolve_denied_for_informed(client, seeded, auth_headers):
    row = await h.make_alert(seeded, title=f"inf {_tag()}")
    resp = await client.post(f"/v1/alerts/{row['id']}/resolve", headers=await auth_headers("manager@demo"))
    assert resp.status_code == 403
    assert await seeded.fetchval("SELECT status FROM alerts WHERE id = $1", row["id"]) == "open"


async def test_resolve_plans_soft_updates_through_routing(client, seeded, auth_headers, monkeypatch):
    row = await h.make_alert(seeded, title=f"soft {_tag()}")
    calls = []

    async def plan_deliveries(conn, alert, rule, *, reason, level=1):
        calls.append((alert["id"], alert["status"], reason))
        await conn.execute(
            "INSERT INTO outbox (topic, routing_key, payload) VALUES ('deliveries', 'test-noop', $1)",
            {"alertId": str(alert["id"])},
        )
        return []

    mod = ModuleType("app.delivery.routing")
    mod.plan_deliveries = plan_deliveries
    monkeypatch.setitem(sys.modules, "app.delivery.routing", mod)

    published_ids: list[int] = []

    async def fake_publish(pool, rabbit, ids):
        published_ids.extend(ids)

    monkeypatch.setattr("app.api.routers.alerts.publish_outbox_rows", fake_publish)
    resp = await client.post(f"/v1/alerts/{row['id']}/resolve", headers=await auth_headers("anna@demo"))
    assert resp.status_code == 200
    assert calls == [(row["id"], "resolved", "resolved")]
    rows = await seeded.fetch("SELECT id FROM outbox WHERE payload->>'alertId' = $1", str(row["id"]))
    assert [r["id"] for r in rows] == published_ids != []
    await seeded.execute("DELETE FROM outbox WHERE id = ANY($1)", published_ids)  # keep the relay clean


async def test_resolve_survives_failing_routing(client, seeded, auth_headers, monkeypatch):
    row = await h.make_alert(seeded, title=f"boom {_tag()}")

    async def plan_deliveries(conn, alert, rule, *, reason, level=1):
        await conn.execute("SELECT * FROM table_that_does_not_exist")

    mod = ModuleType("app.delivery.routing")
    mod.plan_deliveries = plan_deliveries
    monkeypatch.setitem(sys.modules, "app.delivery.routing", mod)
    resp = await client.post(f"/v1/alerts/{row['id']}/resolve", headers=await auth_headers("anna@demo"))
    assert resp.status_code == 200
    assert await seeded.fetchval("SELECT status FROM alerts WHERE id = $1", row["id"]) == "resolved"


# ---------------------------------------------------------------- assign & comments


async def test_assign_and_validation(client, seeded, auth_headers):
    row = await h.make_alert(seeded, title=f"assign {_tag()}")
    headers = await auth_headers("anna@demo")
    resp = await client.post(
        f"/v1/alerts/{row['id']}/assign", json={"userId": str(h.user("anna@demo"))}, headers=headers
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["assignee"]["displayName"] == "Anna Kowalska"
    timeline = await seeded.fetchrow("SELECT type, text FROM timeline WHERE alert_id = $1", row["id"])
    assert timeline["type"] == "assigned"
    assert (
        await seeded.fetchval(
            "SELECT count(*) FROM audit_log WHERE action = 'alert.assign' AND target_id = $1", str(row["id"])
        )
        == 1
    )
    # same assignee again: no new timeline entry
    await client.post(
        f"/v1/alerts/{row['id']}/assign", json={"userId": str(h.user("anna@demo"))}, headers=headers
    )
    assert await seeded.fetchval("SELECT count(*) FROM timeline WHERE alert_id = $1", row["id"]) == 1

    outsider = await client.post(
        f"/v1/alerts/{row['id']}/assign", json={"userId": str(h.user("piotr@demo"))}, headers=headers
    )
    assert outsider.status_code == 422
    unknown = await client.post(
        f"/v1/alerts/{row['id']}/assign", json={"userId": str(uuid.uuid4())}, headers=headers
    )
    assert unknown.status_code == 422
    bad = await client.post(f"/v1/alerts/{row['id']}/assign", json={"userId": "zzz"}, headers=headers)
    assert bad.status_code == 422
    informed = await client.post(
        f"/v1/alerts/{row['id']}/assign",
        json={"userId": str(h.user("anna@demo"))},
        headers=await auth_headers("manager@demo"),
    )
    assert informed.status_code == 403


async def test_comments_allowed_for_informed(client, seeded, auth_headers):
    row = await h.make_alert(seeded, title=f"comment {_tag()}")
    resp = await client.post(
        f"/v1/alerts/{row['id']}/comments",
        json={"text": "  looking into it "},
        headers=await auth_headers("manager@demo"),
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["type"] == "comment"
    assert body["text"] == "looking into it"
    assert body["actor"]["displayName"] == "Marta Manager"
    detail = await client.get(f"/v1/alerts/{row['id']}", headers=await auth_headers("anna@demo"))
    assert [t["type"] for t in detail.json()["timeline"]] == ["comment"]


async def test_comment_validation_and_scope(client, seeded, auth_headers):
    row = await h.make_alert(seeded, title=f"c2 {_tag()}")
    headers = await auth_headers("anna@demo")
    assert (
        await client.post(f"/v1/alerts/{row['id']}/comments", json={"text": ""}, headers=headers)
    ).status_code == 422
    assert (
        await client.post(f"/v1/alerts/{row['id']}/comments", json={"text": "   "}, headers=headers)
    ).status_code == 422
    resp = await client.post(
        f"/v1/alerts/{row['id']}/comments", json={"text": "hi"}, headers=await auth_headers("piotr@demo")
    )
    assert resp.status_code == 404
