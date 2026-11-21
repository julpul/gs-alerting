"""Org API: domains, teams, users (scoped) and role grants."""

from __future__ import annotations

import uuid

import pytest

from app.core.ids import ORG_ID
from tests.api import alerts_helpers as h


@pytest.fixture
async def newbie(seeded):
    """A fresh user so grants never change the seeded users' scope for other tests."""
    user_id = uuid.uuid4()
    await seeded.execute(
        "INSERT INTO users (id, email, display_name, password_hash) VALUES ($1, $2, 'New Bie', 'x')",
        user_id,
        f"newbie-{user_id.hex[:8]}@demo",
    )
    yield user_id
    await seeded.execute("DELETE FROM users WHERE id = $1", user_id)


def _grant(role: str, scope_type: str, scope_id) -> dict:
    return {"role": role, "scopeType": scope_type, "scopeId": str(scope_id)}


# ---------------------------------------------------------------- reads


async def test_domains_scoped(client, seeded, auth_headers):
    master = (await client.get("/v1/domains", headers=await auth_headers("master@demo"))).json()
    assert {d["name"] for d in master} == {"Payments", "Settlements", "Security", "Markets"}
    payments = next(d for d in master if d["name"] == "Payments")
    assert payments["rootUserIds"] == [str(h.user("root.payments@demo"))]

    anna = (await client.get("/v1/domains", headers=await auth_headers("anna@demo"))).json()
    assert {d["name"] for d in anna} == {"Payments"}
    root = (await client.get("/v1/domains", headers=await auth_headers("root.payments@demo"))).json()
    assert "Settlements" not in {d["name"] for d in root}
    assert "Payments" in {d["name"] for d in root}


async def test_teams_scoped(client, seeded, auth_headers):
    master = (await client.get("/v1/teams", headers=await auth_headers("master@demo"))).json()
    assert {t["name"] for t in master} >= {"Team Payments", "Team Settlements", "Checkout Project"}
    checkout = next(t for t in master if t["name"] == "Checkout Project")
    assert set(checkout["domainIds"]) == {str(h.domain("payments")), str(h.domain("security"))}

    piotr = (await client.get("/v1/teams", headers=await auth_headers("piotr@demo"))).json()
    assert [t["name"] for t in piotr] == ["Team Settlements"]
    assert piotr[0]["memberIds"] == [str(h.user("piotr@demo"))]

    manager = (await client.get("/v1/teams", headers=await auth_headers("manager@demo"))).json()
    assert "Checkout Project" not in {t["name"] for t in manager}
    assert "Team Pricing" in {t["name"] for t in manager}


async def test_users_scoped_and_phone_masking(client, seeded, auth_headers):
    master = (await client.get("/v1/users", headers=await auth_headers("master@demo"))).json()
    assert len(master) >= 7
    assert all(u["phone"] for u in master if u["email"].endswith("@demo") and "newbie" not in u["email"])

    piotr = (await client.get("/v1/users", headers=await auth_headers("piotr@demo"))).json()
    by_email = {u["email"]: u for u in piotr}
    assert "anna@demo" not in by_email
    assert "master@demo" not in by_email
    assert {"piotr@demo", "manager@demo", "root.settlements@demo"} <= set(by_email)
    assert by_email["piotr@demo"]["phone"] == "+48500100201"
    assert by_email["manager@demo"]["phone"] is None
    assert by_email["piotr@demo"]["managerId"] == str(h.user("manager@demo"))

    root = (await client.get("/v1/users", headers=await auth_headers("root.settlements@demo"))).json()
    assert next(u for u in root if u["email"] == "piotr@demo")["phone"] == "+48500100201"


async def test_org_endpoints_require_auth(client):
    for path in ("/v1/domains", "/v1/teams", "/v1/users"):
        assert (await client.get(path)).status_code == 401
    resp = await client.post(
        f"/v1/users/{uuid.uuid4()}/roles", json=_grant("member", "team", h.team("team-payments"))
    )
    assert resp.status_code == 401


# ---------------------------------------------------------------- grants


async def test_master_can_grant_everything(client, seeded, auth_headers, newbie):
    headers = await auth_headers("master@demo")
    for body in (
        _grant("domain_root", "domain", h.domain("markets")),
        _grant("member", "team", h.team("team-rates")),
        _grant("informed", "team", h.team("team-pricing")),
        _grant("master", "org", ORG_ID),
    ):
        resp = await client.post(f"/v1/users/{newbie}/roles", json=body, headers=headers)
        assert resp.status_code == 200, (body, resp.text)
        assert resp.json() == body
    roles = await seeded.fetch("SELECT role, granted_by FROM role_assignments WHERE user_id = $1", newbie)
    assert {r["role"] for r in roles} == {"domain_root", "member", "informed", "master"}
    assert {r["granted_by"] for r in roles} == {h.user("master@demo")}
    assert (
        await seeded.fetchval(
            "SELECT count(*) FROM team_members WHERE user_id = $1 AND team_id = $2",
            newbie,
            h.team("team-rates"),
        )
        == 1
    )
    assert (
        await seeded.fetchval(
            "SELECT count(*) FROM audit_log WHERE action = 'role.grant' AND target_id = $1", str(newbie)
        )
        == 4
    )
    # granting twice is a no-op
    again = await client.post(
        f"/v1/users/{newbie}/roles", json=_grant("member", "team", h.team("team-rates")), headers=headers
    )
    assert again.status_code == 200


async def test_member_cannot_grant_domain_root_or_other_teams(client, seeded, auth_headers, newbie):
    headers = await auth_headers("anna@demo")
    denied = [
        _grant("domain_root", "domain", h.domain("payments")),
        _grant("master", "org", ORG_ID),
        _grant("member", "team", h.team("team-settlements")),
    ]
    for body in denied:
        resp = await client.post(f"/v1/users/{newbie}/roles", json=body, headers=headers)
        assert resp.status_code == 403, body
        assert resp.json()["code"] == "forbidden"
    assert await seeded.fetchval("SELECT count(*) FROM role_assignments WHERE user_id = $1", newbie) == 0

    for role in ("member", "informed"):
        resp = await client.post(
            f"/v1/users/{newbie}/roles", json=_grant(role, "team", h.team("team-payments")), headers=headers
        )
        assert resp.status_code == 200


async def test_informed_cannot_grant(client, seeded, auth_headers, newbie):
    resp = await client.post(
        f"/v1/users/{newbie}/roles",
        json=_grant("informed", "team", h.team("team-payments")),
        headers=await auth_headers("manager@demo"),
    )
    assert resp.status_code == 403


async def test_domain_root_grants_within_own_domain(client, seeded, auth_headers, newbie):
    headers = await auth_headers("root.payments@demo")
    allowed = [
        _grant("domain_root", "domain", h.domain("payments")),
        _grant("member", "team", h.team("team-payments")),
        _grant("informed", "team", h.team("checkout-project")),  # cross-domain team linked to payments
    ]
    for body in allowed:
        resp = await client.post(f"/v1/users/{newbie}/roles", json=body, headers=headers)
        assert resp.status_code == 200, (body, resp.text)
    denied = [
        _grant("master", "org", ORG_ID),
        _grant("domain_root", "domain", h.domain("settlements")),
        _grant("member", "team", h.team("team-settlements")),
    ]
    for body in denied:
        resp = await client.post(f"/v1/users/{newbie}/roles", json=body, headers=headers)
        assert resp.status_code == 403, body


async def test_grant_validation(client, seeded, auth_headers, newbie):
    headers = await auth_headers("master@demo")
    wrong_scope = await client.post(
        f"/v1/users/{newbie}/roles", json=_grant("member", "domain", h.domain("payments")), headers=headers
    )
    assert wrong_scope.status_code == 422
    unknown_user = await client.post(
        f"/v1/users/{uuid.uuid4()}/roles",
        json=_grant("member", "team", h.team("team-payments")),
        headers=headers,
    )
    assert unknown_user.status_code == 404
    bad_user = await client.post(
        "/v1/users/nope/roles", json=_grant("member", "team", h.team("team-payments")), headers=headers
    )
    assert bad_user.status_code == 404
    unknown_team = await client.post(
        f"/v1/users/{newbie}/roles", json=_grant("member", "team", uuid.uuid4()), headers=headers
    )
    assert unknown_team.status_code == 422
    wrong_org = await client.post(
        f"/v1/users/{newbie}/roles", json=_grant("master", "org", uuid.uuid4()), headers=headers
    )
    assert wrong_org.status_code == 422
    bad_scope = await client.post(
        f"/v1/users/{newbie}/roles",
        json={"role": "member", "scopeType": "team", "scopeId": "zzz"},
        headers=headers,
    )
    assert bad_scope.status_code == 422
    bad_role = await client.post(
        f"/v1/users/{newbie}/roles",
        json={"role": "god", "scopeType": "org", "scopeId": str(ORG_ID)},
        headers=headers,
    )
    assert bad_role.status_code == 422
