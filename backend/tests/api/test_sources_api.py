"""Sources REST API: list/create (key once), rotate, samples, mapping (+ suggest), snippets, scope."""

from __future__ import annotations

import sys
import types
from uuid import UUID, uuid4

import orjson
import pytest

from app.core.ids import stable_id
from app.core.redis import rkey
from app.core.security import hash_token
from app.schemas import api as wire

PAYMENTS = str(stable_id("team", "team-payments"))
PROMETHEUS = str(stable_id("source", "prometheus"))


@pytest.fixture
async def made(seeded):
    ids: list[str] = []
    yield ids
    await seeded.execute("DELETE FROM sources WHERE id = ANY($1)", [UUID(i) for i in ids])


async def create(client, headers, made, **overrides):
    payload = {"name": f"src-{uuid4().hex[:8]}", "teamId": "team-payments", "type": "webhook", **overrides}
    resp = await client.post("/v1/sources", json=payload, headers=headers)
    assert resp.status_code == 201, resp.text
    made.append(resp.json()["id"])
    return resp.json()


async def push_events(redis, source_id: str, raws: list[dict]) -> str:
    key = rkey("recent", source_id)
    await redis.delete(key)
    for i, raw in enumerate(raws):
        env = {
            "event_id": f"ev{i}",
            "source_id": source_id,
            "received_at": "2026-11-21T10:00:00.000Z",
            "raw": raw,
        }
        await redis.xadd(key, {"envelope": orjson.dumps(env).decode()})
    return key


async def test_list_is_scoped_and_contract_shaped(client, auth_headers, redis):
    anna = await auth_headers("anna@demo")
    rows = (await client.get("/v1/sources", headers=anna)).json()
    assert {r["name"] for r in rows} >= {"prometheus"}
    assert "workflow-broker" not in {r["name"] for r in rows}
    row = next(r for r in rows if r["name"] == "prometheus")
    wire.Source1.model_validate(row)
    assert row["teamId"] == PAYMENTS and row["type"] == "webhook" and row["preset"] == "alertmanager"
    assert row["mappingStatus"] == "accepted" and row["ingestUrl"].endswith("/v1/ingest/YOUR_SOURCE_KEY")
    assert "apiKey" not in row and "key_hash" not in row

    piotr = await auth_headers("piotr@demo")
    names = {r["name"] for r in (await client.get("/v1/sources", headers=piotr)).json()}
    assert {"workflow-broker", "custody"} <= names and not names & {
        "prometheus",
        "pricing-logs",
        "trade-gateway",
    }
    master = await auth_headers("master@demo")
    assert {"workflow-broker", "prometheus", "pricing-logs", "pr-review-agent", "trade-gateway"} <= {
        r["name"] for r in (await client.get("/v1/sources", headers=master)).json()
    }
    assert (await client.get("/v1/sources")).status_code == 401


async def test_list_reports_event_activity_from_the_recent_stream(client, auth_headers, redis):
    anna = await auth_headers("anna@demo")
    key = await push_events(redis, PROMETHEUS, [{"n": i} for i in range(3)])
    try:
        row = next(r for r in (await client.get("/v1/sources", headers=anna)).json() if r["id"] == PROMETHEUS)
        assert row["eventsLast24h"] == 3 and row["lastEventAt"] is not None
    finally:
        await redis.delete(key)
    row = next(r for r in (await client.get("/v1/sources", headers=anna)).json() if r["id"] == PROMETHEUS)
    assert row["eventsLast24h"] == 0 and row["lastEventAt"] is None


async def test_create_returns_key_once_and_stores_only_its_hash(client, auth_headers, made, seeded):
    anna = await auth_headers("anna@demo")
    created = await create(client, anna, made)
    key = created["apiKey"]
    assert key.startswith("sk_") and len(key) > 30
    assert created["ingestUrl"].endswith(f"/v1/ingest/{key}")
    assert created["mappingStatus"] == "none" and created["teamId"] == PAYMENTS
    row = await seeded.fetchrow("SELECT * FROM sources WHERE id = $1", UUID(created["id"]))
    assert row["key_hash"] == hash_token(key) and key not in row["key_hash"]
    assert row["key_prefix"] == key[: len(row["key_prefix"])] and row["key_prefix"] != key
    assert row["domain_id"] == stable_id("domain", "payments")
    listed = (await client.get("/v1/sources", headers=anna)).json()
    assert key not in orjson.dumps(listed).decode()
    again = await create(client, anna, made)
    assert again["apiKey"] != key


async def test_create_with_preset_prefills_mapping(client, auth_headers, made, seeded):
    anna = await auth_headers("anna@demo")
    created = await create(client, anna, made, preset="alertmanager")
    assert created["preset"] == "alertmanager" and created["mappingStatus"] == "accepted"
    mapping = await seeded.fetchval("SELECT mapping FROM sources WHERE id = $1", UUID(created["id"]))
    assert mapping["fields"]["severity"] == "labels.severity"


async def test_create_rabbitmq_source_needs_url_and_queue(client, auth_headers, made, seeded):
    master = await auth_headers("master@demo")
    bad = await client.post(
        "/v1/sources", json={"name": "q", "teamId": "team-rates", "type": "rabbitmq"}, headers=master
    )
    assert bad.status_code == 422 and bad.json()["details"]["path"] == "rabbitmq"
    created = await create(
        client, master, made, teamId="team-rates", type="rabbitmq",
        rabbitmq={"url": "amqp://u:p@broker:5672/", "queue": "customer.q"},
        preset="alertmanager",
    )  # fmt: skip
    row = await seeded.fetchrow("SELECT * FROM sources WHERE id = $1", UUID(created["id"]))
    assert row["type"] == "rabbitmq" and row["config"] == {
        "url": "amqp://u:p@broker:5672/",
        "queue": "customer.q",
    }
    assert row["preset"] is None and row["domain_id"] == stable_id("domain", "markets")
    assert "amqp" not in orjson.dumps(created).decode()


async def test_create_validation_and_scope_denial(client, auth_headers):
    piotr = await auth_headers("piotr@demo")
    denied = await client.post(
        "/v1/sources", json={"name": "x", "teamId": "team-rates", "type": "webhook"}, headers=piotr
    )
    assert denied.status_code == 403
    manager = await auth_headers("manager@demo")  # informed only
    assert (
        await client.post(
            "/v1/sources", json={"name": "x", "teamId": "team-payments", "type": "webhook"}, headers=manager
        )
    ).status_code == 403
    anna = await auth_headers("anna@demo")
    unknown = await client.post(
        "/v1/sources", json={"name": "x", "teamId": "nope", "type": "webhook"}, headers=anna
    )
    assert unknown.status_code == 422 and unknown.json()["details"]["path"] == "teamId"
    bad_type = await client.post(
        "/v1/sources", json={"name": "x", "teamId": "team-payments", "type": "smtp"}, headers=anna
    )
    assert bad_type.status_code == 422
    assert (
        await client.post("/v1/sources", json={"teamId": "team-payments", "type": "webhook"}, headers=anna)
    ).status_code == 422


async def test_rotate_key_invalidates_the_old_key(client, auth_headers, made, seeded):
    anna = await auth_headers("anna@demo")
    created = await create(client, anna, made)
    resp = await client.post(f"/v1/sources/{created['id']}/rotate-key", headers=anna)
    assert resp.status_code == 200
    new = resp.json()
    assert new["apiKey"] != created["apiKey"] and new["id"] == created["id"]
    assert await seeded.fetchval(
        "SELECT key_hash FROM sources WHERE id = $1", UUID(created["id"])
    ) == hash_token(new["apiKey"])
    assert (
        await seeded.fetchval(
            "SELECT count(*) FROM sources WHERE key_hash = $1", hash_token(created["apiKey"])
        )
        == 0
    )

    piotr = await auth_headers("piotr@demo")
    assert (await client.post(f"/v1/sources/{created['id']}/rotate-key", headers=piotr)).status_code == 404
    manager = await auth_headers("manager@demo")
    assert (await client.post(f"/v1/sources/{created['id']}/rotate-key", headers=manager)).status_code == 403
    assert (await client.post(f"/v1/sources/{uuid4()}/rotate-key", headers=anna)).status_code == 404
    assert (await client.post("/v1/sources/zzz/rotate-key", headers=anna)).status_code == 404


async def test_sample_events_newest_first_with_limit(client, auth_headers, redis):
    anna = await auth_headers("anna@demo")
    key = await push_events(redis, PROMETHEUS, [{"n": i} for i in range(5)])
    try:
        resp = await client.get(f"/v1/sources/{PROMETHEUS}/sample-events", params={"limit": 3}, headers=anna)
        assert resp.status_code == 200
        events = resp.json()
        assert [e["payload"]["n"] for e in events] == [4, 3, 2]
        for e in events:
            wire.RawEvent.model_validate(e)
        assert events[0]["eventId"] == "ev4" and events[0]["receivedAt"].startswith("2026-11-21T10:00:00")
        assert len((await client.get(f"/v1/sources/{PROMETHEUS}/sample-events", headers=anna)).json()) == 5
        assert (
            await client.get(f"/v1/sources/{PROMETHEUS}/sample-events", params={"limit": 0}, headers=anna)
        ).status_code == 422
        piotr = await auth_headers("piotr@demo")
        assert (await client.get(f"/v1/sources/{PROMETHEUS}/sample-events", headers=piotr)).status_code == 404
    finally:
        await redis.delete(key)
    assert (await client.get(f"/v1/sources/{PROMETHEUS}/sample-events", headers=anna)).json() == []


async def test_sample_events_skip_malformed_entries_and_accept_bare_payloads(client, auth_headers, redis):
    anna = await auth_headers("anna@demo")
    key = rkey("recent", PROMETHEUS)
    await redis.delete(key)
    await redis.xadd(key, {"envelope": "not json"})
    await redis.xadd(key, {"other": "field"})
    await redis.xadd(key, {"envelope": orjson.dumps({"plain": "payload"}).decode()})
    await redis.xadd(key, {"envelope": orjson.dumps([1, 2]).decode()})
    try:
        events = (await client.get(f"/v1/sources/{PROMETHEUS}/sample-events", headers=anna)).json()
        assert [e["payload"] for e in events] == [{"plain": "payload"}]
    finally:
        await redis.delete(key)


async def test_suggest_mapping_falls_back_to_the_heuristic(client, auth_headers, monkeypatch):
    monkeypatch.setitem(sys.modules, "app.ai.mapping", None)
    anna = await auth_headers("anna@demo")
    sample = {"level": "ERROR", "logger": "x-1", "message": "boom", "ts": 1792833245000}
    resp = await client.post(
        f"/v1/sources/{PROMETHEUS}/mapping/suggest", json={"sample": sample}, headers=anna
    )
    assert resp.status_code == 200
    out = resp.json()
    wire.SchemaMapping.model_validate(
        {**out, "fields": {k: v for k, v in out["fields"].items() if k in wire.Fields.model_fields}}
    )
    assert (
        out["status"] == "draft"
        and out["fields"]["severity"] == "level"
        and out["fields"]["timestamp"] == "ts"
    )
    assert out["explanation"].startswith("AI unavailable, heuristic draft.")
    assert (
        await client.post(f"/v1/sources/{PROMETHEUS}/mapping/suggest", json={}, headers=anna)
    ).status_code == 422
    piotr = await auth_headers("piotr@demo")
    assert (
        await client.post(f"/v1/sources/{PROMETHEUS}/mapping/suggest", json={"sample": sample}, headers=piotr)
    ).status_code == 404


async def test_suggest_mapping_prefers_the_ai_module_when_present(client, auth_headers, monkeypatch):
    seen = {}

    async def suggest_mapping(sample, source):
        seen.update(sample=sample, source=source)
        return wire.SchemaMapping(
            status="accepted",
            detectedSchema="x",
            fields=wire.Fields(severity="a.b"),
            explanation="from the model",
        )

    fake = types.ModuleType("app.ai.mapping")
    fake.suggest_mapping = suggest_mapping
    monkeypatch.setitem(sys.modules, "app.ai.mapping", fake)
    anna = await auth_headers("anna@demo")
    resp = await client.post(
        f"/v1/sources/{PROMETHEUS}/mapping/suggest", json={"sample": {"a": {"b": 1}}}, headers=anna
    )
    assert resp.status_code == 200
    assert resp.json() == {
        "status": "draft",
        "detectedSchema": "x",
        "fields": {"severity": "a.b"},
        "explanation": "from the model",
    }
    assert seen["sample"] == {"a": {"b": 1}} and seen["source"]["name"] == "prometheus"

    async def failing(sample, source):
        raise RuntimeError("all providers down")

    fake.suggest_mapping = failing
    resp = await client.post(
        f"/v1/sources/{PROMETHEUS}/mapping/suggest", json={"sample": {"level": "WARN"}}, headers=anna
    )
    assert resp.status_code == 200 and resp.json()["explanation"].startswith("AI unavailable")

    async def as_dict(sample, source):
        return {"status": "accepted", "fields": {"severity": "level"}}

    fake.suggest_mapping = as_dict
    resp = await client.post(
        f"/v1/sources/{PROMETHEUS}/mapping/suggest", json={"sample": {"level": "WARN"}}, headers=anna
    )
    assert resp.json() == {"status": "draft", "fields": {"severity": "level"}}


async def test_put_mapping_saves_and_updates_status(client, auth_headers, made, seeded):
    anna = await auth_headers("anna@demo")
    created = await create(client, anna, made)
    mapping = {
        "status": "accepted",
        "detectedSchema": "trade.rejected.v2",
        "fields": {
            "severity": "labels.severity",
            "service": "meta.producer",
            "type": "meta.schema",
            "error_code": "error.code",
        },
        "severityValueMap": {"ERROR": "error", "critical": "critical"},
        "explanation": "ok",
    }
    resp = await client.put(f"/v1/sources/{created['id']}/mapping", json=mapping, headers=anna)
    assert resp.status_code == 200 and resp.json() == mapping
    row = await seeded.fetchrow(
        "SELECT mapping, mapping_status FROM sources WHERE id = $1", UUID(created["id"])
    )
    assert row["mapping"] == mapping and row["mapping_status"] == "accepted"
    draft = await client.put(
        f"/v1/sources/{created['id']}/mapping", json={"status": "draft", "fields": {}}, headers=anna
    )
    assert draft.status_code == 200
    listed = next(
        r for r in (await client.get("/v1/sources", headers=anna)).json() if r["id"] == created["id"]
    )
    assert listed["mappingStatus"] == "draft"


async def test_put_mapping_validation_and_scope(client, auth_headers, made):
    anna = await auth_headers("anna@demo")
    created = await create(client, anna, made)
    url = f"/v1/sources/{created['id']}/mapping"
    assert (await client.put(url, json={"fields": {"color": "x"}}, headers=anna)).status_code == 422
    assert (await client.put(url, json={"fields": {"severity": ""}}, headers=anna)).status_code == 422
    assert (await client.put(url, json={"severityValueMap": {"X": "fatal"}}, headers=anna)).status_code == 422
    assert (await client.put(url, json={"status": "maybe"}, headers=anna)).status_code == 422
    piotr = await auth_headers("piotr@demo")
    assert (await client.put(url, json={"fields": {}}, headers=piotr)).status_code == 404
    manager = await auth_headers("manager@demo")
    assert (await client.put(url, json={"fields": {}}, headers=manager)).status_code == 403


async def test_snippets_use_the_public_base_url(client, auth_headers, settings):
    anna = await auth_headers("anna@demo")
    resp = await client.get(f"/v1/sources/{PROMETHEUS}/snippets", headers=anna)
    assert resp.status_code == 200
    snippets = resp.json()
    assert set(snippets) == {"curl", "python", "js", "java"}
    base = settings.public_base_url.rstrip("/")
    for text in snippets.values():
        assert f"{base}/v1/ingest/YOUR_SOURCE_KEY" in text
    assert snippets["curl"].startswith("curl -X POST") and "httpx.post" in snippets["python"]
    assert "fetch(" in snippets["js"] and "HttpClient" in snippets["java"]
    piotr = await auth_headers("piotr@demo")
    assert (await client.get(f"/v1/sources/{PROMETHEUS}/snippets", headers=piotr)).status_code == 404
