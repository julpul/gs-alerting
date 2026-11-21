"""/api/me, /api/meta, /api/directory, /api/session-token."""

from __future__ import annotations

from app.herald.identity import herald_role

from .core_helpers import as_person


async def test_me_maps_our_roles_to_herald_roles(client, seeded):
    r = await client.get("/api/me", headers=as_person("anna"))
    assert r.status_code == 200, r.text
    me = r.json()
    assert me["mode"] == "demo" and me["provider"] is None
    assert me["user"] == "person:anna" and me["name"] == "Anna Kowalska"
    assert me["role"] == "responder" and me["teams"] == ["team-payments"]

    root = (await client.get("/api/me", headers=as_person("root.settlements"))).json()
    assert root["role"] == "approver" and "team-settlements" in root["teams"]
    master = (await client.get("/api/me")).json()  # no identity: the demo default is the master
    assert master["user"] == "person:master" and master["role"] == "admin"
    assert {"team-payments", "team-settlements", "team-security"} <= set(master["teams"])
    informed = (await client.get("/api/me", headers=as_person("manager"))).json()
    assert informed["role"] == "responder"


async def test_me_unknown_person_and_bad_role(client, seeded):
    r = await client.get("/api/me", headers=as_person("nobody-here"))
    assert r.status_code == 401 and r.json() == {"detail": "unknown person in X-Herald-User"}
    r = await client.get("/api/me", headers=as_person("anna", **{"X-Herald-Role": "pope"}))
    assert r.status_code == 400 and "unknown role" in r.json()["detail"]


async def test_me_accepts_our_bearer_token(client, seeded, auth_headers):
    me = (await client.get("/api/me", headers=await auth_headers("piotr@demo"))).json()
    assert me["user"] == "person:piotr" and me["teams"] == ["team-settlements"]


async def test_meta_lists_pickers_and_demo_links(client, seeded):
    meta = (await client.get("/api/meta")).json()
    assert set(meta["severities"]) >= {"critical", "error", "warning", "info"}
    assert meta["severities"]["critical"]["postmortem"] is True
    assert "eq" in meta["operators"] and "ne" in meta["operators"]
    assert {"sms", "email", "phone", "ui"} <= set(meta["channels"])
    actions = {a["name"]: a for a in meta["actions"]}
    assert {"http_get", "retry_workflow", "escalate"} <= set(actions)
    assert actions["http_get"]["risk"] in ("low", "medium", "high") and actions["http_get"]["description"]
    assert {"responder", "approver", "editor", "admin"} <= set(meta["roles"])
    assert meta["auth"]["mode"] == "demo"
    assert meta["links"]["mailpit"].startswith("http") and meta["links"]["mockgw"].startswith("http")
    assert meta["version"]


async def test_directory_shape_and_scope(client, seeded):
    d = (await client.get("/api/directory")).json()
    teams = {t["id"]: t for t in d["teams"]}
    assert {"team-payments", "team-settlements", "checkout-project"} <= set(teams)
    pay = teams["team-payments"]
    for key, kind in [
        ("name", str),
        ("members", list),
        ("rota", list),
        ("preferences", dict),
        ("has_rules", bool),
    ]:
        assert isinstance(pay[key], kind), key
    assert "anna" in pay["members"]
    assert pay["lead"] == "root.payments"
    assert pay["on_call"] in pay["members"]
    assert pay["rota"] and pay["shift_days"] == 7
    assert pay["has_rules"] is True  # seeded rules belong to team-payments
    assert pay["preferences"]["critical"]  # first escalation level's channels
    people = {p["id"]: p for p in d["people"]}
    assert people["anna"]["name"] == "Anna Kowalska" and isinstance(people["anna"]["preferences"], dict)
    assert people["anna"]["has_rules"] is False and people["anna"]["tz"] == "Europe/Warsaw"
    assert "phone" not in people["anna"] and "email" not in people["anna"]

    anna = (await client.get("/api/directory", headers=as_person("anna"))).json()
    assert [t["id"] for t in anna["teams"]] == ["team-payments"]
    assert "piotr" not in {p["id"] for p in anna["people"]}


async def test_session_token_works_against_v1(client, seeded):
    r = await client.get("/api/session-token", headers=as_person("anna"))
    assert r.status_code == 200
    token = r.json()
    assert token["tokenType"] == "Bearer" and token["expiresIn"] > 0
    me = await client.get("/v1/auth/me", headers={"Authorization": f"Bearer {token['accessToken']}"})
    assert me.status_code == 200 and me.json()["user"]["email"] == "anna@demo"


async def test_herald_role_of_scope_without_roles():
    from uuid import uuid4

    from app.core.scope import Scope

    assert herald_role(Scope(user_id=uuid4())) == "responder"
