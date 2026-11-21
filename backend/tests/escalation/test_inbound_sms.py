"""`POST /v1/inbound/sms`: replies identified by the sender's phone number."""

from __future__ import annotations

import pytest

from app.core.ids import format_short_id
from tests.escalation.helpers import SseCollector, make_alert, make_rule, team, user

URL = "/v1/inbound/sms"
PIOTR = "+48500100201"
ANNA = "+48500100200"


async def sms(client, sender, text):
    return await client.post(URL, json={"from": sender, "text": text})


async def alert_status(pool, alert_id):
    return await pool.fetchval("SELECT status FROM alerts WHERE id = $1", alert_id)


async def test_ack_by_phone_acknowledges_and_stops_escalation(seeded, client):
    alert = await make_alert(seeded, await make_rule(seeded))
    short = alert["short_id"]
    assert alert["ack_deadline"] is not None
    async with SseCollector(team("team-settlements")) as sse:
        resp = await sms(client, PIOTR, f"ACK {short}")
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"reply": f"{format_short_id(short)} acknowledged."}
        events = await sse.of_type("alert.acked")
    assert events[0]["data"]["alertId"] == str(alert["id"])
    row = await seeded.fetchrow("SELECT * FROM alerts WHERE id = $1", alert["id"])
    assert row["status"] == "acknowledged" and row["acked_by"] == user("piotr@demo")
    assert row["ack_deadline"] is None and row["next_renotify_at"] is None
    again = await sms(client, PIOTR, f"ack {short}")
    assert again.json()["reply"] == f"{format_short_id(short)} was already acknowledged."


@pytest.mark.parametrize("template", ["  ack a-{n}  ", "Ack A-{n}", "ACK {n}\n"])
async def test_command_formats(seeded, client, template):
    alert = await make_alert(seeded, None, plan=False)
    resp = await sms(client, "+48 500-100-201", template.format(n=alert["short_id"]))
    assert resp.status_code == 200 and "acknowledged" in resp.json()["reply"]
    assert await alert_status(seeded, alert["id"]) == "acknowledged"


async def test_resolve_by_phone_with_note(seeded, client):
    alert = await make_alert(seeded, None, plan=False)
    resp = await sms(client, "0048500100201", f"RESOLVE {alert['short_id']} restarted the pod")
    assert resp.json()["reply"].endswith("resolved.")
    assert await alert_status(seeded, alert["id"]) == "resolved"
    note = await seeded.fetchval("SELECT note FROM resolutions WHERE alert_id = $1", alert["id"])
    assert note == "restarted the pod"
    assert (
        (await sms(client, PIOTR, f"RESOLVE {alert['short_id']}"))
        .json()["reply"]
        .endswith("already resolved.")
    )


async def test_unknown_sender_is_404(client):
    assert (await sms(client, "+48 111 222 333", "ACK 1")).status_code == 404
    assert (await sms(client, "no digits", "ACK 1")).status_code == 404


async def test_user_without_role_on_the_team_cannot_ack(seeded, client):
    alert = await make_alert(seeded, None, plan=False)
    resp = await sms(client, ANNA, f"ACK {alert['short_id']}")  # Anna is on team-payments
    assert resp.status_code == 200
    assert "not found" in resp.json()["reply"]
    assert await alert_status(seeded, alert["id"]) == "open"


async def test_informed_user_cannot_ack(seeded, client):
    alert = await make_alert(seeded, None, plan=False)
    resp = await sms(client, "+48500100300", f"ACK {alert['short_id']}")  # manager@demo: informed only
    assert "member role" in resp.json()["reply"]
    assert await alert_status(seeded, alert["id"]) == "open"


async def test_resolved_alert_cannot_be_acked_and_unknown_alert_is_reported(seeded, client):
    alert = await make_alert(seeded, None, plan=False)
    await seeded.execute("UPDATE alerts SET status = 'resolved' WHERE id = $1", alert["id"])
    assert "cannot be acknowledged" in (await sms(client, PIOTR, f"ACK {alert['short_id']}")).json()["reply"]
    assert "not found" in (await sms(client, PIOTR, "ACK 99999999")).json()["reply"]


@pytest.mark.parametrize("text", ["hello", "", "ACK", "ACK twelve", "ACKNOWLEDGE 12", "STOP"])
async def test_unrecognised_text_gets_a_help_reply(client, text):
    resp = await sms(client, PIOTR, text)
    assert resp.status_code == 200
    assert resp.json()["reply"].startswith("Unknown command")


async def test_payload_validation(client):
    assert (await client.post(URL, json={"text": "ACK 1"})).status_code == 422
    assert (await client.post(URL, json={"from": PIOTR})).status_code == 422


@pytest.mark.parametrize("text", ["APPROVE 4", "reject 4"])
async def test_approval_codes_are_no_longer_understood(client, text):
    resp = await sms(client, PIOTR, text)
    assert resp.status_code == 200
    assert resp.json()["reply"] == "Unknown command. Reply ACK <n> or RESOLVE <n>."


async def test_shared_secret_required_when_configured(seeded, client, monkeypatch):
    from app.config import get_settings

    monkeypatch.setenv("INBOUND_SMS_SECRET", "s3cret")
    get_settings.cache_clear()
    try:
        alert = await make_alert(seeded, None, plan=False)
        body = {"from": PIOTR, "text": f"ACK {alert['short_id']}"}
        assert (await client.post(URL, json=body)).status_code == 401
        wrong = await client.post(URL, json=body, headers={"X-Inbound-Secret": "nope"})
        assert wrong.status_code == 401
        assert await alert_status(seeded, alert["id"]) == "open"
        ok = await client.post(URL, json=body, headers={"X-Inbound-Secret": "s3cret"})
        assert ok.status_code == 200 and "acknowledged" in ok.json()["reply"]
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()


async def test_no_secret_configured_accepts_any_caller(seeded, client):
    alert = await make_alert(seeded, None, plan=False)
    resp = await client.post(
        URL, json={"from": PIOTR, "text": f"ACK {alert['short_id']}"}, headers={"X-Inbound-Secret": "ignored"}
    )
    assert resp.status_code == 200
