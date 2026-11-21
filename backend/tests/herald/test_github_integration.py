"""GitHub token set through the integrations API: verify on save, resolution order, PR flow, disconnect."""

from __future__ import annotations

import httpx
import pytest_asyncio

from app.config import get_settings
from app.herald import github
from tests.api import alerts_helpers as h
from tests.herald.test_codebase import REPO

from .core_helpers import alert, as_person

PUT = "/v1/integrations/github"
BAD = "ghp_bad_token"


def _fake(monkeypatch, valid: dict[str, str]) -> list[tuple[str, str]]:
    """GitHub fake: `valid` maps accepted tokens to logins. Records (path, authorization)."""
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        auth = request.headers["authorization"]
        seen.append((request.url.path, auth))
        login = valid.get(auth.removeprefix("Bearer "))
        if login is None:
            return httpx.Response(401, json={"message": "Bad credentials"})
        if request.url.path == "/user":
            return httpx.Response(200, json={"login": login})
        if request.url.path == "/repos/o/missing":
            return httpx.Response(404, json={"message": "Not Found"})
        if request.method == "GET" and request.url.path.startswith("/repos/o/n/git/ref"):
            return httpx.Response(200, json={"object": {"sha": "abc"}})
        if request.url.path.startswith("/repos/o/n/pulls"):
            return httpx.Response(201, json={"html_url": "https://github.com/o/n/pull/9", "number": 9})
        return httpx.Response(200 if request.method == "GET" else 201, json={})

    monkeypatch.setattr(
        github,
        "make_client",
        lambda tok: httpx.AsyncClient(
            base_url="https://api.github.test",
            transport=httpx.MockTransport(handler),
            headers={"Authorization": f"Bearer {tok}"},
        ),
    )
    return seen


@pytest_asyncio.fixture
async def linked(seeded, monkeypatch):
    monkeypatch.setattr(get_settings(), "github_token", "")
    await h.ensure_source_and_rule(seeded)
    await seeded.execute("UPDATE sources SET codebase = $2 WHERE id = $1", h.SOURCE_ID, REPO)
    yield
    await seeded.execute("UPDATE sources SET codebase = NULL WHERE id = $1", h.SOURCE_ID)
    await seeded.execute("DELETE FROM integrations WHERE kind = 'github'")


async def _pr(client, row) -> dict:
    r = await client.get(f"/api/alerts/{row['short_id']}", headers=as_person("anna"))
    assert r.status_code == 200, r.text
    return r.json()["codebase"]["pr"]


async def test_save_verifies_stores_login_and_never_returns_token(client, auth_headers, linked, monkeypatch):
    seen = _fake(monkeypatch, {"tok-root": "octo-payments"})
    root = await auth_headers("root.payments@demo")
    r = await client.put(PUT, json={"config": {"token": "tok-root"}}, headers=root)
    assert r.status_code == 200, r.text
    assert r.json()["login"] == "octo-payments" and "tok-root" not in r.text
    assert seen == [("/user", "Bearer tok-root")]

    listing = await client.get("/v1/integrations", headers=root)
    row = next(i for i in listing.json() if i["kind"] == "github" and i["status"] == "connected")
    assert row["login"] == "octo-payments" and row["canEdit"] is True and "tok-root" not in listing.text
    anna = await client.get("/v1/integrations", headers=await auth_headers("anna@demo"))
    gh = next(i for i in anna.json() if i["kind"] == "github")
    assert gh["canEdit"] is False and "tok-root" not in anna.text


async def test_invalid_token_is_422(client, auth_headers, linked, monkeypatch):
    _fake(monkeypatch, {})
    root = await auth_headers("root.payments@demo")
    r = await client.put(PUT, json={"config": {"token": BAD}}, headers=root)
    assert r.status_code == 422 and r.json()["message"] == "GitHub rejected this token"
    assert BAD not in r.text
    missing = await client.put(PUT, json={"config": {"note": "x"}}, headers=root)
    assert missing.status_code == 422


async def test_repo_in_payload_is_checked(client, auth_headers, linked, monkeypatch):
    _fake(monkeypatch, {"tok": "octo"})
    root = await auth_headers("root.payments@demo")
    r = await client.put(PUT, json={"config": {"token": "tok", "repo": "o/missing"}}, headers=root)
    assert r.status_code == 422 and "o/missing" in r.json()["message"]
    ok = await client.put(PUT, json={"config": {"token": "tok", "repo": "o/n"}}, headers=root)
    assert ok.status_code == 200


async def test_unreachable_github_is_502(client, auth_headers, linked, monkeypatch):
    def boom(tok: str) -> httpx.AsyncClient:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("down")

        return httpx.AsyncClient(base_url="https://api.github.test", transport=httpx.MockTransport(handler))

    monkeypatch.setattr(github, "make_client", boom)
    root = await auth_headers("root.payments@demo")
    r = await client.put(PUT, json={"config": {"token": "t"}}, headers=root)
    assert r.status_code == 502


async def test_resolution_order_and_pr_flow_use_db_token(client, auth_headers, seeded, linked, monkeypatch):
    seen = _fake(monkeypatch, {"tok-env": "env", "tok-org": "org", "tok-dom": "dom"})
    row = await alert(seeded, title="Token order")
    assert (await _pr(client, row))["available"] is False

    monkeypatch.setattr(get_settings(), "github_token", "tok-env")
    assert (await _pr(client, row))["available"] is True  # env stays as last fallback
    monkeypatch.setattr(get_settings(), "github_token", "")

    master = await auth_headers("master@demo")
    org = await client.put(PUT, json={"config": {"token": "tok-org"}}, headers=master)
    assert org.status_code == 200 and org.json()["scopeDomainId"] is None
    pr = await _pr(client, row)
    assert pr["available"] is True and pr["reason"] is None

    root = await auth_headers("root.payments@demo")
    dom = await client.put(PUT, json={"config": {"token": "tok-dom"}}, headers=root)
    assert dom.json()["scopeDomainId"] is not None

    seen.clear()
    opened = await client.post(f"/api/alerts/{row['short_id']}/codebase/pr", headers=as_person("anna"))
    assert opened.status_code == 200, opened.text
    assert {auth for _, auth in seen} == {"Bearer tok-dom"}  # the alert's domain wins over org-wide


async def test_org_wide_used_when_domain_has_none(client, auth_headers, seeded, linked, monkeypatch):
    seen = _fake(monkeypatch, {"tok-org": "org"})
    master = await auth_headers("master@demo")
    await client.put(PUT, json={"config": {"token": "tok-org"}}, headers=master)
    row = await alert(seeded, title="Org wide")
    seen.clear()
    opened = await client.post(f"/api/alerts/{row['short_id']}/codebase/pr", headers=as_person("anna"))
    assert opened.status_code == 200 and {a for _, a in seen} == {"Bearer tok-org"}


async def test_disconnect_makes_pr_unavailable(client, auth_headers, seeded, linked, monkeypatch):
    _fake(monkeypatch, {"tok": "octo"})
    root = await auth_headers("root.payments@demo")
    row = await alert(seeded, title="Disconnect")
    await client.put(PUT, json={"config": {"token": "tok"}}, headers=root)
    assert (await _pr(client, row))["available"] is True

    gone = await client.delete(PUT, headers=root)
    assert gone.status_code == 200 and gone.json()["status"] == "not_connected"
    pr = await _pr(client, row)
    assert pr["available"] is False and "Connect GitHub" in pr["reason"]

    assert (await client.delete("/v1/integrations/nope", headers=root)).status_code == 422
    anna = await auth_headers("anna@demo")
    assert (await client.delete(PUT, headers=anna)).status_code == 422
    assert (await client.delete(f"{PUT}?scopeDomainId=settlements", headers=root)).status_code == 403
