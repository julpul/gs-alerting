"""/api/deliveries, /api/audit, /api/stats."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

from tests.api import alerts_helpers as h

from .core_helpers import alert, as_person


async def test_deliveries_shape_latency_and_scope(client, seeded):
    row = await alert(seeded, title="Herald delivery log")
    received = datetime.now(UTC) - timedelta(seconds=2)
    await h.make_delivery(
        seeded,
        row["id"],
        status="delivered",
        event_received_at=received,
        delivered_at=received + timedelta(milliseconds=1500),
    )
    await h.make_delivery(seeded, row["id"], channel="email", status="dead_lettered", attempts=4)
    items = (await client.get("/api/deliveries", headers=as_person("anna"))).json()
    mine = [d for d in items if d["alert_id"] == row["short_id"]]
    assert len(mine) == 2
    by_channel = {d["channel"]: d for d in mine}
    sms, email = by_channel["sms"], by_channel["email"]
    assert sms["status"] == "sent" and abs(sms["latency_ms"] - 1500) < 1
    assert sms["title"] == "Herald delivery log" and sms["recipient"] == "person:anna"
    assert isinstance(sms["created_at"], str) and isinstance(sms["attempts"], int)
    assert email["status"] == "dead" and email["attempts"] == 4 and email["latency_ms"] is None

    owned = (
        await client.get(
            "/api/deliveries", params={"owner": "team:team-payments", "limit": 5}, headers=as_person("anna")
        )
    ).json()
    assert len(owned) <= 5
    assert row["short_id"] not in {
        d["alert_id"] for d in (await client.get("/api/deliveries", headers=as_person("piotr"))).json()
    }
    r = await client.get(
        "/api/deliveries", params={"owner": "team:team-payments"}, headers=as_person("piotr")
    )
    assert r.status_code == 403


async def test_audit_is_scoped_for_every_persona(client, seeded):
    pay = await alert(seeded, title="Herald audit pay")
    sett = await alert(
        seeded, team_slug="team-settlements", domain_slug="settlements", title="Herald audit s"
    )
    await client.post(f"/api/alerts/{pay['short_id']}/ack", headers=as_person("anna"))
    await client.post(f"/api/alerts/{sett['short_id']}/ack", headers=as_person("piotr"))

    r = await client.get("/api/audit", params={"limit": 1000}, headers=as_person("anna"))
    assert r.status_code == 200  # a responder sees their teams' entries, never a 403
    anna_targets = {e["target"] for e in r.json()["entries"]}
    assert f"alert:{pay['short_id']}" in anna_targets and f"alert:{sett['short_id']}" not in anna_targets

    d = (await client.get("/api/audit", params={"limit": 1000}, headers=as_person("root.payments"))).json()
    assert d["intact"] is True
    targets = {e["target"] for e in d["entries"]}
    assert f"alert:{pay['short_id']}" in targets and f"alert:{sett['short_id']}" not in targets
    entry = next(e for e in d["entries"] if e["target"] == f"alert:{pay['short_id']}")
    assert entry["action"] == "ack" and entry["actor"] == "person:anna" and entry["our_action"] == "alert.ack"
    assert isinstance(entry["id"], int) and len(entry["hash"]) == 64 and isinstance(entry["detail"], dict)
    ids = [e["id"] for e in d["entries"]]
    assert ids == sorted(ids)

    everything = (await client.get("/api/audit", params={"limit": 1000})).json()["entries"]
    assert f"alert:{sett['short_id']}" in {e["target"] for e in everything}
    assert len((await client.get("/api/audit", params={"limit": 2})).json()["entries"]) == 2


async def test_stats_counts_and_latency(client, seeded):
    since = time.time() - 1
    row = await alert(seeded, title="Herald stats", count=3)
    received = datetime.now(UTC)
    await h.make_delivery(
        seeded,
        row["id"],
        status="delivered",
        event_received_at=received,
        delivered_at=received + timedelta(milliseconds=200),
    )
    await h.make_delivery(seeded, row["id"], channel="email", status="pending")
    s = (await client.get("/api/stats", params={"since": since}, headers=as_person("anna"))).json()
    assert s["delivered"] >= 1 and s["outstanding"] >= 1 and s["matched_events"] >= 3
    assert s["p50_ms"] is not None and s["p99_ms"] >= s["p50_ms"] and s["max_ms"] >= s["p99_ms"]
    assert s["last_delivered"] > since
    assert s["summary"] is None or "eventsPerSecond" in s["summary"]
    none = (await client.get("/api/stats", params={"since": since, "run": "no-such-run"})).json()
    assert none["delivered"] == 0 and none["matched_events"] == 0 and none["p50_ms"] is None


async def test_audit_never_403s_for_a_signed_in_persona(client, seeded):
    users = await seeded.fetch("SELECT email FROM users WHERE email LIKE '%@demo' ORDER BY email LIMIT 40")
    for local in [u["email"].split("@")[0] for u in users]:
        r = await client.get("/api/audit", params={"limit": 5}, headers=as_person(local))
        assert r.status_code != 403, (local, r.text)
        if r.status_code == 200:
            assert isinstance(r.json()["entries"], list)


async def test_outstanding_is_only_what_is_queued_now(client, seeded):
    row = await alert(seeded, title="Herald outstanding")
    await h.make_delivery(seeded, row["id"], channel="email", status="pending")
    await h.make_delivery(seeded, row["id"], channel="slack", status="retrying")
    await h.make_delivery(seeded, row["id"], channel="ui", status="suppressed_digest")
    old = await h.make_delivery(seeded, row["id"], channel="phone", status="pending")
    await seeded.execute("UPDATE deliveries SET created_at = now() - interval '3 hours' WHERE id = $1", old)
    counted = await seeded.fetchval(
        "SELECT count(*) FROM deliveries WHERE status IN ('pending', 'retrying') "
        "AND delivered_at IS NULL AND created_at >= now() - interval '1 hour'"
    )
    s = (await client.get("/api/stats")).json()
    assert s["outstanding"] == counted >= 2
    stale = await seeded.fetchval("SELECT count(*) FROM deliveries WHERE delivered_at IS NULL")
    assert s["outstanding"] < stale
