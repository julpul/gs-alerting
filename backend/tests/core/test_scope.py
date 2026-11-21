import time
from uuid import uuid4

import jwt

from app.config import get_settings
from app.core.ids import ORG_ID, stable_id
from app.core.scope import load_scope
from app.core.security import create_token, generate_pat


def _team(slug: str):
    return stable_id("team", slug)


def _domain(slug: str):
    return stable_id("domain", slug)


async def test_master_sees_all_domains_and_teams(seeded):
    async with seeded.acquire() as conn:
        scope = await load_scope(conn, stable_id("user", "master@demo"))
        n_domains = await conn.fetchval("SELECT count(*) FROM domains")
        n_teams = await conn.fetchval("SELECT count(*) FROM teams")
    assert scope.is_master
    assert len(scope.domain_ids) == n_domains >= 4
    assert len(scope.team_ids) == n_teams >= 6
    assert [r.scope_id for r in scope.roles] == [ORG_ID]


async def test_member_sees_only_own_teams(seeded):
    async with seeded.acquire() as conn:
        anna = await load_scope(conn, stable_id("user", "anna@demo"))
        piotr = await load_scope(conn, stable_id("user", "piotr@demo"))
    assert not anna.is_master
    assert _team("team-payments") in anna.team_ids
    assert _domain("payments") in anna.domain_ids
    assert _domain("settlements") not in anna.domain_ids
    assert _team("team-settlements") in piotr.team_ids
    assert not anna.can_see_team(_team("team-settlements"))
    assert not anna.can_see_team(_team("checkout-project"))
    assert piotr.can_see_team(_team("team-settlements"))


async def test_domain_root_sees_domain_teams(seeded):
    async with seeded.acquire() as conn:
        root = await load_scope(conn, stable_id("user", "root.security@demo"))
    assert root.domain_ids >= {_domain("security")}
    assert {_team("team-security"), _team("checkout-project")} <= root.team_ids
    assert _team("team-settlements") not in root.team_ids
    assert root.can_see_domain(_domain("security"))
    assert not root.can_see_domain(_domain("settlements"))


async def test_informed_role_grants_team_scope(seeded):
    async with seeded.acquire() as conn:
        manager = await load_scope(conn, stable_id("user", "manager@demo"))
    assert {_team("team-rates"), _team("team-pricing"), _team("team-settlements")} <= manager.team_ids
    assert {r.role for r in manager.roles} == {"informed"}


async def test_unknown_user_has_empty_scope(seeded):
    async with seeded.acquire() as conn:
        scope = await load_scope(conn, uuid4())
    assert not scope.is_master and not scope.team_ids and not scope.domain_ids


async def test_auth_via_bearer_query_param_and_pat(client, seeded):
    uid = stable_id("user", "anna@demo")
    token = create_token(uid)
    assert (await client.get("/v1/auth/me", headers={"Authorization": f"Bearer {token}"})).status_code == 200
    assert (await client.get(f"/v1/auth/me?access_token={token}")).status_code == 200

    pat, _, digest = generate_pat()
    await seeded.execute(
        "INSERT INTO personal_tokens (id, user_id, token_hash, name) VALUES ($1, $2, $3, 'test')",
        uuid4(),
        uid,
        digest,
    )
    resp = await client.get("/v1/auth/me", headers={"Authorization": f"Bearer {pat}"})
    assert resp.status_code == 200 and resp.json()["user"]["email"] == "anna@demo"
    await seeded.execute("UPDATE personal_tokens SET revoked_at = now() WHERE token_hash = $1", digest)
    assert (await client.get("/v1/auth/me", headers={"Authorization": f"Bearer {pat}"})).status_code == 401


async def test_bad_tokens_are_401(client, seeded):
    assert (await client.get("/v1/auth/me")).status_code == 401
    assert (await client.get("/v1/auth/me", headers={"Authorization": "Bearer junk"})).status_code == 401
    refresh = create_token(stable_id("user", "anna@demo"), "refresh")
    assert (
        await client.get("/v1/auth/me", headers={"Authorization": f"Bearer {refresh}"})
    ).status_code == 401
    ghost = jwt.encode(
        {"sub": str(uuid4()), "typ": "access", "exp": int(time.time()) + 60},
        get_settings().jwt_secret,
        "HS256",
    )
    resp = await client.get("/v1/auth/me", headers={"Authorization": f"Bearer {ghost}"})
    assert resp.status_code == 401 and resp.json()["code"] == "invalid_token"
