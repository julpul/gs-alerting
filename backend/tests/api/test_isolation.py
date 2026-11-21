"""Data isolation (NFR-7): users never see or touch another team's alerts."""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio

from tests.api import alerts_helpers as h

PAYMENTS_ONLY = "isolation-payments"


@pytest_asyncio.fixture(scope="module", loop_scope="session")
async def annas_alert(seeded):
    """An alert of Team Payments with a delivery, a comment and a suggestion."""
    row = await h.make_alert(seeded, title=f"{PAYMENTS_ONLY} {uuid.uuid4().hex[:6]}")
    await h.make_delivery(seeded, row["id"])
    await seeded.execute(
        "INSERT INTO suggestions (id, alert_id, status, content) VALUES ($1, $2, 'draft', $3)",
        uuid.uuid4(),
        row["id"],
        {"diagnosis": "secret diagnosis"},
    )
    return row


@pytest_asyncio.fixture(scope="module", loop_scope="session")
async def checkout_alert(seeded):
    """Cross-domain team (payments + security)."""
    team_id = h.team("checkout-project")
    row = await seeded.fetchrow(
        "INSERT INTO alerts (id, team_id, domain_id, fingerprint, title, severity) "
        "VALUES ($1, $2, $3, $4, 'checkout isolation', 'error') RETURNING id, short_id",
        uuid.uuid4(),
        team_id,
        h.domain("payments"),
        f"fp-{uuid.uuid4()}",
    )
    return row


async def _alert_ids(client, headers, **params) -> set[str]:
    resp = await client.get("/v1/alerts", params={"limit": 200, **params}, headers=headers)
    assert resp.status_code == 200
    return {a["id"] for a in resp.json()["items"]}


async def test_member_of_other_team_cannot_see_alert(client, auth_headers, annas_alert):
    piotr = await auth_headers("piotr@demo")
    aid = str(annas_alert["id"])
    assert aid not in await _alert_ids(client, piotr)
    assert aid not in await _alert_ids(client, piotr, q=PAYMENTS_ONLY)
    assert aid not in await _alert_ids(client, piotr, teamId=str(h.team("team-payments")))
    for ref in (aid, f"A-{annas_alert['short_id']}"):
        resp = await client.get(f"/v1/alerts/{ref}", headers=piotr)
        assert resp.status_code == 404
        assert "isolation" not in resp.text


async def test_member_of_other_team_cannot_mutate_alert(client, seeded, auth_headers, annas_alert):
    piotr = await auth_headers("piotr@demo")
    aid = annas_alert["id"]
    timeline_before = await seeded.fetchval("SELECT count(*) FROM timeline WHERE alert_id = $1", aid)
    calls = [
        ("ack", {}),
        ("resolve", {"json": {"resolutionNote": "hacked"}}),
        ("assign", {"json": {"userId": str(h.user("piotr@demo"))}}),
        ("comments", {"json": {"text": "hi"}}),
    ]
    for action, kwargs in calls:
        resp = await client.post(f"/v1/alerts/{aid}/{action}", headers=piotr, **kwargs)
        assert resp.status_code == 404, action
    row = await seeded.fetchrow("SELECT status, assignee_id FROM alerts WHERE id = $1", aid)
    assert row["status"] == "open"
    assert row["assignee_id"] is None
    assert await seeded.fetchval("SELECT count(*) FROM resolutions WHERE alert_id = $1", aid) == 0
    assert await seeded.fetchval("SELECT count(*) FROM timeline WHERE alert_id = $1", aid) == timeline_before


async def test_other_domain_root_cannot_see_alert(client, auth_headers, annas_alert):
    root = await auth_headers("root.settlements@demo")
    assert str(annas_alert["id"]) not in await _alert_ids(client, root)
    assert (await client.get(f"/v1/alerts/{annas_alert['id']}", headers=root)).status_code == 404
    assert (await client.post(f"/v1/alerts/{annas_alert['id']}/ack", headers=root)).status_code == 404


async def test_informed_manager_reads_and_comments_but_cannot_act(client, seeded, auth_headers, annas_alert):
    manager = await auth_headers("manager@demo")
    aid = annas_alert["id"]
    assert str(aid) in await _alert_ids(client, manager, q=PAYMENTS_ONLY)
    detail = await client.get(f"/v1/alerts/{aid}", headers=manager)
    assert detail.status_code == 200
    assert detail.json()["suggestion"]["diagnosis"] == "secret diagnosis"
    assert (
        await client.post(f"/v1/alerts/{aid}/comments", json={"text": "fyi"}, headers=manager)
    ).status_code == 201
    assert (await client.post(f"/v1/alerts/{aid}/ack", headers=manager)).status_code == 403
    assert (await client.post(f"/v1/alerts/{aid}/resolve", headers=manager)).status_code == 403
    body = {"userId": str(h.user("anna@demo"))}
    assert (await client.post(f"/v1/alerts/{aid}/assign", json=body, headers=manager)).status_code == 403
    assert await seeded.fetchval("SELECT status FROM alerts WHERE id = $1", aid) == "open"


async def test_master_sees_all_teams(client, auth_headers, annas_alert, seeded):
    master = await auth_headers("master@demo")
    other = await h.make_alert(
        seeded, team_slug="team-settlements", domain_slug="settlements", title=f"{PAYMENTS_ONLY} other"
    )
    ids = await _alert_ids(client, master, q=PAYMENTS_ONLY)
    assert {str(annas_alert["id"]), str(other["id"])} <= ids
    assert (await client.get(f"/v1/alerts/{other['id']}", headers=master)).status_code == 200


async def test_cross_domain_team_alert_visibility(client, auth_headers, checkout_alert, annas_alert):
    aid = str(checkout_alert["id"])
    # domain roots of both linked domains see it; the payments root does not see security-only data and v.v.
    for email in ("root.payments@demo", "root.security@demo", "master@demo"):
        assert (
            await client.get(f"/v1/alerts/{aid}", headers=await auth_headers(email))
        ).status_code == 200, email
    for email in ("root.settlements@demo", "piotr@demo", "anna@demo"):
        assert (
            await client.get(f"/v1/alerts/{aid}", headers=await auth_headers(email))
        ).status_code == 404, email
    # the security root sees the checkout alert but not Team Payments' own alerts
    security = await auth_headers("root.security@demo")
    assert str(annas_alert["id"]) not in await _alert_ids(client, security)
    assert aid in await _alert_ids(client, security)


async def test_org_api_hides_other_teams(client, auth_headers, annas_alert):
    piotr = await auth_headers("piotr@demo")
    teams = (await client.get("/v1/teams", headers=piotr)).json()
    assert {t["name"] for t in teams} == {"Team Settlements"}
    users = {u["email"] for u in (await client.get("/v1/users", headers=piotr)).json()}
    assert "anna@demo" not in users
    domains = {d["name"] for d in (await client.get("/v1/domains", headers=piotr)).json()}
    assert domains == {"Settlements"}


async def test_stream_refuses_other_teams(client, auth_headers):
    resp = await client.get(
        "/v1/stream",
        params={"teamId": str(h.team("team-payments"))},
        headers=await auth_headers("piotr@demo"),
    )
    assert resp.status_code == 403


def _has_route(app, prefix: str) -> bool:
    return any(getattr(r, "path", "").startswith(prefix) for r in app.routes)


async def test_rules_and_sources_are_isolated_when_routers_exist(client, app, auth_headers, annas_alert):
    """Active once the rules and sources routers are merged; skipped otherwise."""
    if not (_has_route(app, "/v1/rules") and _has_route(app, "/v1/sources")):
        pytest.skip("rules/sources routers not present on this branch")
    piotr = await auth_headers("piotr@demo")
    rule = await client.get(f"/v1/rules/{h.RULE_ID}", headers=piotr)
    assert rule.status_code in (403, 404)
    sources = await client.get("/v1/sources", headers=piotr)
    assert sources.status_code == 200
    assert str(h.SOURCE_ID) not in sources.text
