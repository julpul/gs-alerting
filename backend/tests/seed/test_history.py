"""Seeded incident history: resolved alerts whose knowledge keys equal what the live pipeline computes."""

from collections import Counter
from datetime import UTC, datetime, timedelta
from importlib import import_module

import pytest

from app.api.routers import ingest
from app.core.ids import stable_id
from app.pipeline.fingerprint import knowledge_key
from app.pipeline.normalize import normalize
from seed.seed import load_fixtures

DOMAIN_OF_TEAM = {
    "team-settlements": "settlements",
    "team-payments": "payments",
    "team-pricing": "markets",
    "team-rates": "markets",
    "team-security": "security",
}
TIMELINE_TYPES = {
    "created",
    "deduplicated",
    "notified",
    "escalated",
    "acked",
    "assigned",
    "comment",
    "step_run",
    "approval_requested",
    "approval_decided",
    "suggestion",
    "resolved",
}
history = import_module("seed.loaders.90_history")


@pytest.fixture
def captured(monkeypatch):
    """Envelopes the real ingest endpoints would publish (no broker involved)."""
    envelopes: list[dict] = []

    async def fake_publish(rabbit, items, priority):
        envelopes.extend(items)

    monkeypatch.setattr(ingest, "_publish", fake_publish)
    return envelopes


async def _live_key(client, seeded, captured, name: str, key_header: str, raw: dict, domain_slug: str) -> str:
    """Send `raw` to the live endpoint of source `name`, normalize like the worker, return the key."""
    captured.clear()
    if name == "pr-review-agent":
        resp = await client.post("/v1/alerts/raise", json=raw, headers={"X-Api-Key": key_header})
    else:
        resp = await client.post(f"/v1/ingest/{key_header}", json=raw)
    assert resp.status_code == 202, resp.text
    envelope = captured[-1]
    async with seeded.acquire() as conn:
        ctx = await history._load_context(conn)
    ev = normalize(envelope, ctx.sources[name])
    return knowledge_key(
        stable_id("domain", domain_slug),
        ev.meta.get("type") or ev.meta.get("schema"),
        ev.meta.get("service"),
        ev.meta.get("error_code"),
    )


async def test_forty_resolved_alerts_with_one_resolution_each(full_pool):
    assert await full_pool.fetchval("SELECT count(*) FROM alerts") == 40
    assert await full_pool.fetchval("SELECT count(*) FROM alerts WHERE status <> 'resolved'") == 0
    assert await full_pool.fetchval("SELECT count(*) FROM resolutions") == 40
    mismatched = await full_pool.fetchval(
        "SELECT count(*) FROM alerts a JOIN resolutions r ON r.alert_id = a.id "
        "WHERE r.knowledge_key IS DISTINCT FROM a.knowledge_key OR r.domain_id <> a.domain_id "
        "OR r.team_id <> a.team_id OR a.resolved_at IS NULL OR a.resolved_at <= a.first_seen_at"
    )
    assert mismatched == 0
    assert await full_pool.fetchval("SELECT count(*) FROM alerts WHERE rule_id IS NULL") == 0
    assert (
        await full_pool.fetchval(
            "SELECT count(*) FROM alerts a JOIN rules r ON r.id = a.rule_id WHERE r.team_id <> a.team_id"
        )
        == 0
    )


async def test_timestamps_are_relative_to_seed_time(full_pool):
    first, last = await full_pool.fetchrow("SELECT min(first_seen_at), max(resolved_at) FROM alerts")
    now = datetime.now(UTC)
    assert now - timedelta(days=30) <= first <= now - timedelta(days=28)
    assert last < now
    assert await full_pool.fetchval("SELECT count(*) FROM alerts WHERE first_seen_at > now()") == 0


async def test_timeline_has_contract_types_and_resolution_note(full_pool):
    rows = await full_pool.fetch("SELECT type, actor, data FROM timeline")
    types = Counter(r["type"] for r in rows)
    assert set(types) <= TIMELINE_TYPES
    assert types["created"] == 40 and types["resolved"] == 40
    resolved = [r for r in rows if r["type"] == "resolved"]
    assert all(r["data"]["resolutionNote"] for r in resolved)
    assert all(r["actor"]["type"] in ("user", "system") for r in rows)
    assert any(r["actor"]["type"] == "user" and r["actor"]["name"] == "Piotr Wisniewski" for r in rows)


async def test_every_incident_has_the_knowledge_key_of_the_live_pipeline(client, seeded, full_pool, captured):
    rows = await full_pool.fetch(
        "SELECT a.id, a.knowledge_key, a.team_id, a.raw_event, s.name AS source, s.key_prefix "
        "FROM alerts a JOIN sources s ON s.id = a.source_id"
    )
    teams = {r["id"]: r["slug"] for r in await full_pool.fetch("SELECT id, slug FROM teams")}
    assert len(rows) == 40
    demo_keys = {s["name"]: s["key"] for s in load_fixtures()["sources"]["sources"]}
    for row in rows:
        live = await _live_key(
            client,
            seeded,
            captured,
            row["source"],
            demo_keys[row["source"]],
            row["raw_event"],
            DOMAIN_OF_TEAM[teams[row["team_id"]]],
        )
        assert live == row["knowledge_key"], row["raw_event"]


async def test_typed_meta_equals_what_the_raise_endpoint_builds(client, full_pool, captured):
    row = await full_pool.fetchrow(
        "SELECT a.raw_event FROM alerts a JOIN sources s ON s.id = a.source_id "
        "WHERE s.name = 'pr-review-agent'"
    )
    resp = await client.post(
        "/v1/alerts/raise", json=row["raw_event"], headers={"X-Api-Key": "demo-key-pr-review-agent"}
    )
    assert resp.status_code == 202
    live = captured[-1]
    ours = history.typed_meta({"name": "pr-review-agent"}, live["raw"], live["received_at"])
    assert ours == live["_meta"]


async def test_fixed_before_families_repeat_as_designed(client, seeded, full_pool, captured):
    demo_keys = {s["name"]: s["key"] for s in load_fixtures()["sources"]["sources"]}
    counts = {}
    for family, source, domain, where in (
        ("timeout", "workflow-broker", "settlements", "raw_event->'error'->>'code' = 'TIMEOUT'"),
        ("high-error-rate", "prometheus", "payments", "raw_event->>'alertname' = 'HighErrorRate'"),
        ("pricing-3", "pricing-logs", "markets", "raw_event->>'logger' = 'pricing-engine-3'"),
        ("secret", "pr-review-agent", "security", "raw_event->>'type' = 'security.secret_leaked'"),
        ("rates", "trade-gateway", "markets", "raw_event->'meta'->>'schema' = 'trade.rejected.v2'"),
    ):
        sample = await full_pool.fetchval(
            f"SELECT raw_event FROM alerts a JOIN sources s ON s.id = a.source_id "
            f"WHERE s.name = $1 AND {where} LIMIT 1",
            source,
        )
        key = await _live_key(client, seeded, captured, source, demo_keys[source], sample, domain)
        counts[family] = await full_pool.fetchval(
            "SELECT count(*) FROM resolutions WHERE knowledge_key = $1", key
        )
    assert counts == {"timeout": 5, "high-error-rate": 4, "pricing-3": 4, "secret": 3, "rates": 5}


async def test_resolution_notes_are_indexed_as_knowledge(full_pool):
    assert (
        await full_pool.fetchval("SELECT count(*) FROM knowledge_docs WHERE kind = 'resolution_note'") == 40
    )
    row = await full_pool.fetchrow(
        "SELECT c.text, c.embedding, d.title FROM knowledge_chunks c "
        "JOIN knowledge_docs d ON d.id = c.doc_id "
        "WHERE d.kind = 'resolution_note' LIMIT 1"
    )
    assert row["title"] in row["text"] and row["embedding"] is None


async def test_core_profile_skips_history(monkeypatch):
    monkeypatch.setenv("SEED_PROFILE", "core")

    class Boom:
        def __getattr__(self, name):
            raise AssertionError("core profile must not touch the database")

    await history.load(Boom(), {})
