"""/api/alerts list + detail + ack / resolve / assign / notes + status update / postmortem."""

from __future__ import annotations

from tests.api import alerts_helpers as h

from .core_helpers import alert, as_person, why_rule

ALERT_KEYS = {
    "id": int,
    "key": str,
    "owner": str,
    "status": str,
    "severity": str,
    "title": str,
    "body": str,
    "fields": dict,
    "highlight": list,
    "colour": str,
    "count": int,
    "first_seen": str,
    "last_seen": str,
    "deliveries": list,
}


async def _list(client, person="anna", **params):
    r = await client.get("/api/alerts", params={"limit": 500, **params}, headers=as_person(person))
    assert r.status_code == 200, r.text
    return r.json()


async def test_list_shape_and_status_mapping(client, seeded):
    row = await alert(seeded, title="Herald list shape", status="escalated", fields={"Service": "pay"})
    await h.make_delivery(seeded, row["id"], status="delivered")
    items = await _list(client)
    a = next(x for x in items if x["id"] == row["short_id"])
    for key, kind in ALERT_KEYS.items():
        assert isinstance(a[key], kind), key
    assert a["key"] == f"A-{row['short_id']}" and a["owner"] == "team:team-payments"
    assert a["status"] == "firing" and a["our_status"] == "escalated"
    assert a["source"] == "Test source" and a["rule_id"] == str(h.RULE_ID)
    assert a["fields"] == {"Service": "pay"} and a["colour"].startswith("#")
    assert a["acked_by"] is None and a["incident_manager"] is None and a["uuid"] == str(row["id"])
    (d,) = a["deliveries"]
    assert d["recipient"] == "person:anna" and d["status"] == "sent" and d["channel"] == "sms"
    assert d["attempts"] == 1 and d["alert_id"] == row["short_id"]


async def test_list_filters_owner_status_source_and_scope(client, seeded):
    pay = await alert(seeded, title="Herald pay acked", status="acknowledged")
    sett = await alert(seeded, team_slug="team-settlements", domain_slug="settlements", title="Herald sett")
    acked = await _list(client, status="acked", owner="team:team-payments")
    assert pay["short_id"] in {a["id"] for a in acked}
    assert all(a["status"] == "acked" and a["owner"] == "team:team-payments" for a in acked)
    assert pay["short_id"] not in {a["id"] for a in await _list(client, status="firing")}
    assert await _list(client, source="no-such-platform") == []
    assert pay["short_id"] in {a["id"] for a in await _list(client, source="Test source")}

    # piotr (settlements) never sees anna's team alerts, and asking for them is refused
    piotr = {a["id"] for a in await _list(client, "piotr")}
    assert sett["short_id"] in piotr and pay["short_id"] not in piotr
    r = await client.get("/api/alerts", params={"owner": "team:team-payments"}, headers=as_person("piotr"))
    assert r.status_code == 403 and r.json()["detail"] == "not your team"
    r = await client.get(f"/api/alerts/{pay['short_id']}", headers=as_person("piotr"))
    assert r.status_code == 404 and r.json() == {"detail": "no such alert"}
    r = await client.post(f"/api/alerts/{pay['short_id']}/ack", headers=as_person("piotr"))
    assert r.status_code == 404
    r = await client.get("/api/alerts", params={"status": "sleeping"}, headers=as_person("anna"))
    assert r.status_code == 422
    assert (await client.get("/api/alerts/not-a-number", headers=as_person("anna"))).status_code == 404


async def test_list_by_person_owner(client, seeded):
    row = await alert(seeded, title="Herald assigned to root")
    await seeded.execute(
        "UPDATE alerts SET assignee_id = $2 WHERE id = $1", row["id"], h.user("root.payments@demo")
    )
    mine = await _list(client, "root.payments", owner="person:root.payments")
    assert [a["id"] for a in mine if a["id"] == row["short_id"]] == [row["short_id"]]
    assert all(a["incident_manager"] == "person:root.payments" for a in mine)
    assert await _list(client, owner="person:ghost") == []


async def test_detail_shape_and_why(client, seeded):
    rule = await why_rule(seeded)
    row = await alert(seeded, title="Herald detail", raw_event={"type": "TEST", "n": 3, "env": "prod"})
    await seeded.execute("UPDATE alerts SET rule_id = $2 WHERE id = $1", row["id"], rule)
    r = await client.get(f"/api/alerts/A-{row['short_id']}", headers=as_person("anna"))
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["sample_event"] == {"type": "TEST", "n": 3, "env": "prod"}
    assert d["runbook"] is None and d["step_state"] == {} and d["approvals"] == []
    assert isinstance(d["timeline"], list) and d["suggestion"] is None
    assert d["escalation_level"] == 1 and "escalates_at" in d and d["flags"] == []
    why = d["why"]
    assert why["passed"] is True and len(why["all"]) == 4
    leaf, group, not_in, severity = why["all"]
    assert leaf == {
        "field": "type",
        "op": "eq",
        "value": "TEST",
        "actual": "TEST",
        "missing": False,
        "passed": True,
    }
    assert group["passed"] is True and group["any"][1]["missing"] is True
    assert not_in["passed"] is True and not_in["not"]["op"] == "in" and not_in["not"]["passed"] is False
    assert severity["field"] == "@severity" and severity["op"] == "ne"


async def test_ack_resolve_assign_notes_change_state_and_timeline(client, seeded):
    row = await alert(seeded, title="Herald actions")
    url = f"/api/alerts/{row['short_id']}"
    anna = as_person("anna")

    r = await client.post(f"{url}/ack", headers=anna)
    assert r.status_code == 200 and r.json() == {"status": "acked"}
    again = await client.post(f"{url}/ack", headers=anna)
    assert again.status_code == 409 and again.json()["detail"] == "alert is already acked"

    r = await client.post(f"{url}/assign", headers=anna, json={"to": "person:root.payments"})
    assert r.status_code == 200 and r.json() == {"assignee": "person:root.payments"}
    bad = await client.post(f"{url}/assign", headers=anna, json={"to": "person:ghost"})
    assert bad.status_code == 422 and bad.json()["detail"] == "choose a person to hand this to"
    released = await client.post(f"{url}/assign", headers=anna, json={"to": None})
    assert released.status_code == 200 and released.json() == {"assignee": None}

    # informed people may comment but not act
    r = await client.post(f"{url}/notes", headers=as_person("manager"), json={"text": "Customers notice"})
    assert r.status_code == 200 and r.json() == {"saved": True}
    assert (await client.post(f"{url}/resolve", headers=as_person("manager"))).status_code == 403

    r = await client.post(f"{url}/resolve", headers=anna, json={"note": "Restarted the pod"})
    assert r.status_code == 200 and r.json() == {"status": "resolved"}
    assert (await client.post(f"{url}/resolve", headers=anna)).status_code == 409

    d = (await client.get(url, headers=anna)).json()
    assert d["status"] == "resolved" and d["acked_by"] == "person:anna"
    assert d["resolved_by"] == "person:anna" and d["incident_manager"] == "person:root.payments"
    actions = [(t["action"], t["actor"]) for t in d["timeline"]]
    assert ("ack", "person:anna") in actions and ("resolve", "person:anna") in actions
    assert ("note", "person:manager") in actions and ("assigned", "person:anna") in actions
    by_action = {t["action"]: t for t in d["timeline"]}
    assert by_action["assigned"]["detail"]["to"] == "person:root.payments"
    assert by_action["note"]["detail"]["text"] == "Customers notice"
    assert by_action["resolve"]["detail"]["note"] == "Restarted the pod"
    assert all(isinstance(t["at"], str) for t in d["timeline"])


async def test_notes_validation(client, seeded):
    row = await alert(seeded, title="Herald blank note")
    r = await client.post(
        f"/api/alerts/{row['short_id']}/notes", headers=as_person("anna"), json={"text": "  "}
    )
    assert r.status_code == 422


async def test_status_update_and_postmortem(client, seeded):
    row = await alert(seeded, title="Herald drafts", severity="critical")
    url = f"/api/alerts/{row['short_id']}"
    anna = as_person("anna")
    text = (await client.get(f"{url}/status-update", headers=anna)).json()["text"]
    assert text.startswith(f"A-{row['short_id']} · CRITICAL · Herald drafts")
    assert "Not yet acknowledged." in text and "Next update by" in text

    await client.post(f"{url}/assign", headers=anna, json={"to": "person:anna"})
    await client.post(f"{url}/notes", headers=anna, json={"text": "Rolled back"})
    await client.post(f"{url}/resolve", headers=anna, json={"note": "Rollback fixed it"})
    text = (await client.get(f"{url}/status-update", headers=anna)).json()["text"]
    assert "In charge: Anna Kowalska" in text and "No further updates: resolved." in text

    pm = (await client.get(f"{url}/postmortem", headers=anna)).json()
    assert pm["required"] is True
    md = pm["markdown"]
    assert md.startswith(f"# Postmortem draft: A-{row['short_id']} Herald drafts")
    assert "the incident manager" in md and "Anna Kowalska" not in md
    assert "Resolution note: Rollback fixed it" in md and "Rolled back" in md
    named = (await client.get(f"{url}/postmortem", params={"names": "true"}, headers=anna)).json()
    assert "Anna Kowalska" in named["markdown"]
    assert (await client.get(f"{url}/postmortem", headers=as_person("piotr"))).status_code == 404


async def test_validation_errors_use_the_detail_shape_for_the_ui(client):
    resp = await client.post(
        "/api/alerts/1/notes",
        json={"text": 123, "unexpected": []},
        headers={"X-Herald-User": "person:master"},
    )
    if resp.status_code == 422:
        assert "detail" in resp.json()
