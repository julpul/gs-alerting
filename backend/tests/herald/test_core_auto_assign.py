"""Herald BFF: the automatic incident manager in list + detail (`assigned_by`, `assign_reason`, timeline)."""

from __future__ import annotations

from app.escalation import auto_assign as aa
from tests.delivery.helpers import make_alert

ANNA = {"X-Herald-User": "person:anna"}
REASON = "on call for Team Payments"


async def test_auto_assignment_is_visible_and_people_can_still_take_over(client, seeded):
    alert = await make_alert(seeded, "team-payments", title="Herald auto-assign")
    async with seeded.acquire() as conn, conn.transaction():
        assert (await aa.auto_assign(conn, alert, None)).reason == REASON
    url = f"/api/alerts/{alert['short_id']}"

    detail = (await client.get(url, headers=ANNA)).json()
    assert detail["incident_manager"] == "person:anna"
    assert detail["assigned_by"] == "system:auto-assign" and detail["assign_reason"] == REASON
    (auto,) = [t for t in detail["timeline"] if t["action"] == "assigned"]
    assert auto["actor"] == "system:auto-assign"
    assert auto["detail"]["to"] == "person:anna" and auto["detail"]["reason"] == REASON
    assert auto["detail"]["auto"] is True
    assert auto["detail"]["text"] == f"Assigned to Anna Kowalska automatically: {REASON}"

    listed = (await client.get("/api/alerts?owner=team:team-payments&limit=500", headers=ANNA)).json()
    (item,) = [a for a in listed if a["id"] == alert["short_id"]]
    assert item["incident_manager"] == "person:anna"
    assert item["assigned_by"] == "system:auto-assign" and item["assign_reason"] == REASON

    handed = await client.post(f"{url}/assign", headers=ANNA, json={"to": "person:root.payments"})
    assert handed.status_code == 200
    detail = (await client.get(url, headers=ANNA)).json()
    assert detail["incident_manager"] == "person:root.payments"
    assert detail["assigned_by"] == "person:anna" and detail["assign_reason"] is None
    manual = [t for t in detail["timeline"] if t["action"] == "assigned"][-1]
    assert manual["detail"]["reason"] is None and manual["detail"]["auto"] is False

    released = await client.post(f"{url}/assign", headers=ANNA, json={"to": None})
    assert released.status_code == 200
    detail = (await client.get(url, headers=ANNA)).json()
    assert detail["incident_manager"] is None
    assert detail["assigned_by"] is None and detail["assign_reason"] is None
    async with seeded.acquire() as conn, conn.transaction():
        fresh = await conn.fetchrow("SELECT * FROM alerts WHERE id = $1", alert["id"])
        assert await aa.auto_assign(conn, fresh, None) is None  # a person released it: stays unassigned
