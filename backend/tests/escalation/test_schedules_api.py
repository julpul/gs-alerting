"""Rotations, schedules and the on-call endpoint."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.core.ids import uuid7
from tests.escalation.helpers import team, user

HOUR = timedelta(hours=1)


def iso(moment: datetime) -> str:
    return moment.isoformat(timespec="milliseconds")


@pytest.fixture
async def squad(seeded):
    """A team in the settlements domain with Anna and Piotr as members, and no shifts."""
    team_id = uuid7()
    slug = f"squad-{team_id.hex[:8]}"
    await seeded.execute("INSERT INTO teams (id, slug, name) VALUES ($1, $2, 'Squad')", team_id, slug)
    await seeded.execute(
        "INSERT INTO team_domains (team_id, domain_id) SELECT $1, id FROM domains WHERE slug = 'settlements'",
        team_id,
    )
    for email in ("anna@demo", "piotr@demo"):
        await seeded.execute(
            "INSERT INTO team_members (team_id, user_id) VALUES ($1, $2)", team_id, user(email)
        )
    await seeded.execute(
        "INSERT INTO role_assignments (user_id, role, scope_type, scope_id) "
        "VALUES ($1, 'member', 'team', $2)",
        user("piotr@demo"),
        team_id,
    )
    yield {"id": team_id, "slug": slug}
    await seeded.execute("DELETE FROM shifts WHERE team_id = $1", team_id)
    await seeded.execute("DELETE FROM rotations WHERE team_id = $1", team_id)
    await seeded.execute("DELETE FROM role_assignments WHERE scope_id = $1", team_id)
    await seeded.execute("DELETE FROM team_members WHERE team_id = $1", team_id)
    await seeded.execute("DELETE FROM team_domains WHERE team_id = $1", team_id)
    await seeded.execute("DELETE FROM teams WHERE id = $1", team_id)


def rotation_body(squad, **overrides):
    return {
        "teamId": squad["slug"],
        "name": "Daily",
        "memberIds": [str(user("anna@demo")), str(user("piotr@demo"))],
        "startsAt": iso(datetime.now(UTC) - 6 * HOUR),
        "shiftHours": 24,
        "handoverTime": None,
        "timezone": "UTC",
        **overrides,
    }


async def test_create_rotation_materialises_shifts_and_oncall_shows_now_and_next(client, auth_headers, squad):
    headers = await auth_headers("root.settlements@demo")
    created = await client.post("/v1/rotations", json=rotation_body(squad), headers=headers)
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["teamId"] == str(squad["id"]) and body["memberIds"][0] == str(user("anna@demo"))

    listed = await client.get("/v1/rotations", params={"teamId": squad["slug"]}, headers=headers)
    assert [r["id"] for r in listed.json()] == [body["id"]]

    shifts = (await client.get("/v1/schedules", params={"teamId": squad["slug"]}, headers=headers)).json()
    assert len(shifts) >= 14 and {s["kind"] for s in shifts} == {"rotation"}
    assert shifts[0]["userId"] == str(user("anna@demo")) and shifts[0]["userName"] == "Anna Kowalska"
    assert shifts[1]["userId"] == str(user("piotr@demo"))

    oncall = (await client.get(f"/v1/teams/{squad['slug']}/oncall", headers=headers)).json()
    assert oncall["teamId"] == str(squad["id"])
    assert oncall["now"]["userId"] == str(user("anna@demo")) and oncall["now"]["kind"] == "rotation"
    assert oncall["next"]["userId"] == str(user("piotr@demo"))
    assert oncall["next"]["start"] == oncall["now"]["end"]
    chain = [a["displayName"] for a in oncall["escalationChain"]]
    assert chain[:1] == ["Anna Kowalska"] and "Stefan Settlements" in chain and chain[-1] == "Mira Master"
    assert {a["kind"] for a in oncall["escalationChain"]} == {"user"}


async def test_override_takes_over_now_and_schedule_window_filters(client, auth_headers, squad):
    headers = await auth_headers("root.settlements@demo")
    await client.post("/v1/rotations", json=rotation_body(squad), headers=headers)
    now = datetime.now(UTC)
    override = await client.post(
        "/v1/schedules",
        json={
            "teamId": squad["slug"],
            "userId": str(user("piotr@demo")),
            "start": iso(now - HOUR),
            "end": iso(now + 2 * HOUR),
            "kind": "override",
        },
        headers=headers,
    )
    assert override.status_code == 201, override.text
    assert override.json()["createdBy"]["displayName"] == "Stefan Settlements"
    oncall = (await client.get(f"/v1/teams/{squad['slug']}/oncall", headers=headers)).json()
    assert (oncall["now"]["userId"], oncall["now"]["kind"]) == (str(user("piotr@demo")), "override")
    assert oncall["next"]["userId"] == str(user("anna@demo"))  # the rotation shift shows through afterwards

    window = await client.get(
        "/v1/schedules",
        params={"teamId": squad["slug"], "from": iso(now + 30 * HOUR), "to": iso(now + 80 * HOUR)},
        headers=headers,
    )
    assert 2 <= len(window.json()) <= 4 and {s["kind"] for s in window.json()} == {"rotation"}


async def test_members_manage_their_own_shifts_only(client, auth_headers, squad):
    piotr = await auth_headers("piotr@demo")
    now = datetime.now(UTC)
    mine = {
        "teamId": squad["slug"],
        "userId": str(user("piotr@demo")),
        "start": iso(now + HOUR),
        "end": iso(now + 3 * HOUR),
    }
    own = await client.post("/v1/schedules", json=mine, headers=piotr)
    assert own.status_code == 201 and own.json()["kind"] == "manual"
    for_anna = await client.post(
        "/v1/schedules", json={**mine, "userId": str(user("anna@demo"))}, headers=piotr
    )
    assert for_anna.status_code == 403
    rotation = await client.post("/v1/rotations", json=rotation_body(squad), headers=piotr)
    assert rotation.status_code == 403


async def test_manager_adds_a_shift_for_a_report(seeded, client, auth_headers):
    """manager@demo manages Anna and sees team-payments (informed)."""
    now = datetime.now(UTC)
    body = {
        "teamId": "team-payments",
        "userId": str(user("anna@demo")),
        "start": iso(now + 100 * HOUR),
        "end": iso(now + 101 * HOUR),
        "kind": "manual",
    }
    try:
        resp = await client.post("/v1/schedules", json=body, headers=await auth_headers("manager@demo"))
        assert resp.status_code == 201
    finally:
        await seeded.execute(
            "DELETE FROM shifts WHERE kind = 'manual' AND team_id = $1", team("team-payments")
        )


async def test_shift_validation(client, auth_headers, squad):
    headers = await auth_headers("master@demo")
    now = datetime.now(UTC)
    ok = {
        "teamId": squad["slug"],
        "userId": str(user("anna@demo")),
        "start": iso(now),
        "end": iso(now + HOUR),
    }
    cases = [
        ({"end": iso(now - HOUR)}, "end"),
        ({"kind": "rotation"}, "kind"),
        ({"userId": "not-a-uuid"}, "userId"),
        ({"userId": str(user("manager@demo"))}, "not a member"),
        ({"teamId": None}, "required"),
    ]
    for override, text in cases:
        resp = await client.post("/v1/schedules", json={**ok, **override}, headers=headers)
        assert resp.status_code == 422, override
        assert text in resp.json()["message"]
    unknown = await client.post("/v1/schedules", json={**ok, "teamId": "no-team"}, headers=headers)
    assert unknown.status_code == 404


@pytest.mark.parametrize(
    ("override", "text"),
    [
        ({"shiftHours": 0}, "shiftHours"),
        ({"memberIds": []}, "memberIds"),
        ({"memberIds": ["nope"]}, "memberIds"),
        ({"memberIds": [str(user("manager@demo"))]}, "not a member"),
        ({"handoverTime": "nine"}, "handoverTime"),
        ({"timezone": "Mars/Base"}, "timezone"),
    ],
)
async def test_rotation_validation(client, auth_headers, squad, override, text):
    resp = await client.post(
        "/v1/rotations", json=rotation_body(squad, **override), headers=await auth_headers("master@demo")
    )
    assert resp.status_code == 422
    assert text in resp.json()["message"]


async def test_schedule_endpoints_respect_scope(client, auth_headers, squad):
    anna = await auth_headers("anna@demo")
    team_id = {"teamId": "team-settlements"}
    assert (await client.get("/v1/schedules", params=team_id, headers=anna)).status_code == 404
    assert (await client.get("/v1/rotations", params=team_id, headers=anna)).status_code == 404
    assert (await client.get("/v1/teams/team-settlements/oncall", headers=anna)).status_code == 404
    assert (await client.get("/v1/schedules", headers=anna)).status_code == 422  # teamId is required
    assert (await client.get("/v1/schedules", params=team_id)).status_code == 401


async def test_oncall_without_shifts_has_no_now_or_next(client, auth_headers, squad):
    resp = await client.get(f"/v1/teams/{squad['id']}/oncall", headers=await auth_headers("master@demo"))
    assert resp.status_code == 200
    body = resp.json()
    assert body["now"] is None and body["next"] is None
    assert body["escalationChain"][0]["displayName"] in {"Anna Kowalska", "Piotr Wisniewski"}


async def test_seeded_oncall_endpoint_for_settlements(client, auth_headers):
    body = (
        await client.get("/v1/teams/team-settlements/oncall", headers=await auth_headers("piotr@demo"))
    ).json()
    assert body["now"]["userName"] == "Piotr Wisniewski"
    assert [a["displayName"] for a in body["escalationChain"]] == [
        "Piotr Wisniewski",
        "Stefan Settlements",
        "Mira Master",
    ]


async def test_oncall_chain_follows_custom_levels(seeded, client, auth_headers, squad):
    custom = {
        "level": 1,
        "target": "custom",
        "recipients": [{"kind": "user", "id": "master@demo"}],
        "channels": ["email"],
        "ackTimeoutSeconds": None,
    }
    master = await auth_headers("master@demo")
    put = await client.put(
        "/v1/escalation-policies",
        json={"scopeType": "team", "scopeId": squad["slug"], "severity": "critical", "levels": [custom]},
        headers=master,
    )
    assert put.status_code == 200
    try:
        body = (await client.get(f"/v1/teams/{squad['slug']}/oncall", headers=master)).json()
        assert [a["displayName"] for a in body["escalationChain"]] == ["Mira Master"]
    finally:
        await seeded.execute("DELETE FROM escalation_policies WHERE scope_id = $1", squad["id"])
