"""Login / me / health against the contract shapes in docs/openapi.yaml."""

from app.core.ids import ORG_ID, stable_id
from app.schemas import api as wire


async def test_login_returns_contract_shape(client, seeded):
    resp = await client.post("/v1/auth/login", json={"email": "anna@demo", "password": "demo"})
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"accessToken", "refreshToken", "expiresIn"}
    assert isinstance(body["accessToken"], str) and isinstance(body["expiresIn"], int)
    assert body["accessToken"] != body["refreshToken"]


async def test_login_is_case_insensitive_on_email(client, seeded):
    resp = await client.post("/v1/auth/login", json={"email": " Anna@Demo ", "password": "demo"})
    assert resp.status_code == 200


async def test_login_wrong_password_and_unknown_user_are_401_error_shape(client, seeded):
    for payload in (
        {"email": "anna@demo", "password": "nope"},
        {"email": "ghost@demo", "password": "demo"},
    ):
        resp = await client.post("/v1/auth/login", json=payload)
        assert resp.status_code == 401
        err = wire.Error.model_validate(resp.json())
        assert err.code == "invalid_credentials"


async def test_login_validation_error_has_field_path(client):
    resp = await client.post("/v1/auth/login", json={"email": "anna@demo"})
    assert resp.status_code == 422
    body = resp.json()
    assert body["code"] == "validation_error"
    assert body["details"]["errors"][0]["path"] == "body.password"


async def test_me_for_member(client, auth_headers):
    resp = await client.get("/v1/auth/me", headers=await auth_headers("anna@demo"))
    assert resp.status_code == 200
    me = wire.Me.model_validate(resp.json())
    assert me.user.email == "anna@demo"
    assert me.user.phone == "+48500100200"
    assert me.user.id == str(stable_id("user", "anna@demo"))
    assert me.user.managerId == str(stable_id("user", "manager@demo"))
    assert [(r.role.value, r.scopeType.value) for r in me.roles] == [("member", "team")]
    slugs_to_ids = {t.id for t in me.teams}
    assert str(stable_id("team", "team-payments")) in slugs_to_ids
    payments = next(t for t in me.teams if t.id == str(stable_id("team", "team-payments")))
    assert payments.domainIds == [str(stable_id("domain", "payments"))]
    assert str(stable_id("user", "anna@demo")) in payments.memberIds


async def test_me_for_master_lists_org_role_and_all_teams(client, auth_headers, seeded):
    resp = await client.get("/v1/auth/me", headers=await auth_headers("master@demo"))
    me = wire.Me.model_validate(resp.json())
    assert [(r.role.value, r.scopeType.value, r.scopeId) for r in me.roles] == [
        ("master", "org", str(ORG_ID))
    ]
    assert len(me.teams) == await seeded.fetchval("SELECT count(*) FROM teams")
    checkout = next(t for t in me.teams if t.name == "Checkout Project")
    assert len(checkout.domainIds) == 2


async def test_me_requires_auth(client):
    resp = await client.get("/v1/auth/me")
    assert resp.status_code == 401
    assert wire.Error.model_validate(resp.json()).code == "unauthorized"


async def test_healthz_readyz_and_metrics(client, seeded):
    assert (await client.get("/healthz")).json() == {"status": "ok"}
    ready = await client.get("/readyz")
    assert ready.status_code == 200
    assert ready.json()["checks"] == {"postgres": "ok", "redis": "ok", "rabbitmq": "ok"}
    metrics = await client.get("/metrics")
    assert metrics.status_code == 200
    for name in ("alerting_ingested_total", "alerting_events_outcome_total", "alerting_sse_clients"):
        assert name in metrics.text


async def test_unknown_route_uses_error_shape(client):
    resp = await client.get("/v1/nope")
    assert resp.status_code == 404
    assert resp.json()["code"] == "not_found"
