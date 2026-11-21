"""A delivery without a person or address (UI channel) went to the team feed, not to "unknown"."""

from __future__ import annotations

from app.core.ids import uuid7
from tests.api import alerts_helpers as h


async def _feed_delivery(pool, alert_id) -> None:
    delivery_id = uuid7()
    await pool.execute(
        "INSERT INTO deliveries (id, alert_id, channel, kind, idempotency_key, status) "
        "VALUES ($1, $2, 'ui', 'soft', $3, 'delivered')",
        delivery_id,
        alert_id,
        f"idem-{delivery_id}",
    )


async def test_team_feed_recipient_in_v1_and_herald(client, auth_headers, seeded):
    alert = await h.make_alert(seeded, title="Feed recipient")
    await _feed_delivery(seeded, alert["id"])
    await h.make_delivery(seeded, alert["id"], channel="sms")
    await h.make_delivery(seeded, alert["id"], channel="phone", user_email=None)

    detail = (await client.get(f"/v1/alerts/{alert['id']}", headers=await auth_headers("anna@demo"))).json()
    recipients = {d["channel"]: d["recipient"] for d in detail["deliveries"]}
    team_name = await seeded.fetchval("SELECT name FROM teams WHERE slug = 'team-payments'")
    assert recipients["ui"] == {
        "kind": "team",
        "id": "team-payments",
        "displayName": f"{team_name} feed",
        "onBehalfOf": None,
    }
    assert recipients["sms"]["kind"] == "user" and recipients["sms"]["displayName"] == "Anna Kowalska"
    assert recipients["phone"]["kind"] == "system" and recipients["phone"]["id"] == "+48500000000"

    anna = {"X-Herald-User": "person:anna"}
    herald = (await client.get(f"/api/alerts/{alert['short_id']}", headers=anna)).json()
    feed = next(d for d in herald["deliveries"] if d["channel"] == "ui")
    assert feed["recipient"] == "team:team-payments" and feed["recipient_kind"] == "team"
    assert feed["recipient_name"] == f"{team_name} feed"
    log = (await client.get("/api/deliveries", params={"limit": 50}, headers=anna)).json()
    mine = [d for d in log if d["alert_id"] == alert["short_id"]]
    assert {d["recipient"] for d in mine} == {"team:team-payments", "person:anna", "+48500000000"}
    assert "unknown" not in {d["recipient"] for d in mine}
