"""Personal access tokens: shown once, hashed at rest, revocable, audited."""

from __future__ import annotations

from app.core.security import hash_token


async def test_create_list_revoke_token(client, auth_headers, seeded):
    headers = await auth_headers("anna@demo")
    created = await client.post("/v1/tokens", json={"name": "claude-code"}, headers=headers)
    assert created.status_code == 201
    body = created.json()
    assert body["token"].startswith("pat_") and body["prefix"] == body["token"][:10]
    assert body["name"] == "claude-code" and body["lastUsedAt"] is None

    stored = await seeded.fetchrow("SELECT token_hash FROM personal_tokens WHERE id = $1::uuid", body["id"])
    assert stored["token_hash"] == hash_token(body["token"]) != body["token"]

    listed = (await client.get("/v1/tokens", headers=headers)).json()
    item = next(t for t in listed if t["id"] == body["id"])
    assert "token" not in item and item["prefix"] == body["prefix"]

    assert (await client.delete(f"/v1/tokens/{body['id']}", headers=headers)).status_code == 204
    assert all(t["id"] != body["id"] for t in (await client.get("/v1/tokens", headers=headers)).json())
    assert (await client.delete(f"/v1/tokens/{body['id']}", headers=headers)).status_code == 404

    actions = await seeded.fetch(
        "SELECT action FROM audit_log WHERE target_type = 'token' AND target_id = $1 ORDER BY at", body["id"]
    )
    assert [a["action"] for a in actions] == ["token.create", "token.revoke"]


async def test_tokens_are_private_to_their_owner(client, auth_headers):
    anna = await auth_headers("anna@demo")
    piotr = await auth_headers("piotr@demo")
    token_id = (await client.post("/v1/tokens", json={"name": "annas"}, headers=anna)).json()["id"]
    assert all(t["id"] != token_id for t in (await client.get("/v1/tokens", headers=piotr)).json())
    assert (await client.delete(f"/v1/tokens/{token_id}", headers=piotr)).status_code == 404
    assert (await client.delete("/v1/tokens/not-a-uuid", headers=piotr)).status_code == 404


async def test_token_validation_and_auth(client, auth_headers):
    headers = await auth_headers("anna@demo")
    assert (await client.post("/v1/tokens", json={"name": ""}, headers=headers)).status_code == 422
    assert (await client.post("/v1/tokens", json={"name": "   "}, headers=headers)).status_code == 422
    assert (await client.post("/v1/tokens", json={})).status_code == 401


async def test_pat_authenticates_rest_and_revocation_returns_401(client, auth_headers):
    headers = await auth_headers("anna@demo")
    created = (await client.post("/v1/tokens", json={"name": "rest"}, headers=headers)).json()
    bearer = {"Authorization": f"Bearer {created['token']}"}
    assert (await client.get("/v1/auth/me", headers=bearer)).status_code == 200
    listed = (await client.get("/v1/tokens", headers=headers)).json()
    assert next(t for t in listed if t["id"] == created["id"])["lastUsedAt"] is not None
    await client.delete(f"/v1/tokens/{created['id']}", headers=headers)
    assert (await client.get("/v1/auth/me", headers=bearer)).status_code == 401
