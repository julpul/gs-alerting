"""Platform codebase + observability links, alert detail "fix it" bundle, draft PR via a fake GitHub."""

from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta

import httpx
import orjson
import pytest_asyncio

from app.config import get_settings
from app.herald import github
from app.herald.codebase import claude_cmd, clone_cmd, github_slug, render_links, web_url
from tests.api import alerts_helpers as h

from .config_helpers import ANNA, MASTER
from .core_helpers import alert, as_person

REPO = {"repo": "https://github.com/o/n", "branch": "main", "path": "demo/payments-api", "language": "python"}
OBS = {
    "links": [
        {
            "label": "Logs",
            "url": "https://dd.example/logs?q=service:{{labels.service}}&f={{from_ms}}&t={{to_ms}}",
        },
        {"label": "Bad", "url": "{{labels.scheme}}://x.test/"},
        {"label": "Alert", "url": "https://dd.example/a?k={{alert}}&s={{service}}"},
    ]
}


class FakeGitHub:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict]] = []
        self.fail_on: str | None = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = orjson.loads(request.content) if request.content else {}
        self.calls.append((request.method, request.url.path, body))
        assert request.headers["authorization"] == "Bearer tok-123"
        if self.fail_on and self.fail_on in request.url.path:
            return httpx.Response(422, json={"message": "Reference already exists"})
        path = request.url.path
        if request.method == "GET":
            return httpx.Response(200, json={"object": {"sha": "abc123"}})
        if path.endswith("/git/refs"):
            return httpx.Response(201, json={})
        if "/contents/" in path:
            return httpx.Response(201, json={})
        return httpx.Response(201, json={"html_url": "https://github.com/o/n/pull/7", "number": 7})


@pytest_asyncio.fixture
async def linked(seeded):
    await h.ensure_source_and_rule(seeded)
    await seeded.execute(
        "UPDATE sources SET codebase = $2, observability = $3 WHERE id = $1", h.SOURCE_ID, REPO, OBS
    )
    yield
    await seeded.execute(
        "UPDATE sources SET codebase = NULL, observability = NULL WHERE id = $1", h.SOURCE_ID
    )


async def _gh(monkeypatch, token: str = "tok-123") -> FakeGitHub:
    fake = FakeGitHub()
    monkeypatch.setattr(get_settings(), "github_token", token)
    monkeypatch.setattr(
        github,
        "make_client",
        lambda: httpx.AsyncClient(
            base_url="https://api.github.test",
            transport=httpx.MockTransport(fake.handler),
            headers={"Authorization": f"Bearer {token}"},
        ),
    )
    return fake


async def _detail(client, row, person="anna"):
    r = await client.get(f"/api/alerts/{row['short_id']}", headers=as_person(person))
    assert r.status_code == 200, r.text
    return r.json()


async def test_detail_without_codebase(client, seeded):
    row = await alert(seeded, title="No repo")
    d = await _detail(client, row)
    assert d["codebase"] is None and d["observability_links"] == []


async def test_detail_codebase_and_links(client, seeded, linked, monkeypatch):
    monkeypatch.setattr(get_settings(), "github_token", "")
    now = datetime.now(UTC)
    row = await alert(
        seeded,
        title="Payments gateway timeouts",
        raw_event={"labels": {"service": "pay api", "scheme": "javascript"}},
        fields={"Service": "payments-api", "Env": "prod"},
    )
    d = await _detail(client, row)
    cb = d["codebase"]
    assert cb["repo"] == "https://github.com/o/n" and cb["path"] == "demo/payments-api"
    assert cb["web_url"] == "https://github.com/o/n/tree/main/demo/payments-api"
    assert cb["clone_cmd"] == "git clone https://github.com/o/n && cd n/demo/payments-api"
    assert (
        f"A-{row['short_id']}" in cb["claude_prompt"] and "Payments gateway timeouts" in cb["claude_prompt"]
    )
    assert "open a pull request" in cb["claude_prompt"] and "list_alerts" in cb["claude_prompt"]
    assert "- Service: payments-api" in cb["claude_prompt"]
    assert cb["claude_cmd"].startswith('claude "') and cb["claude_cmd"].endswith('"')
    assert cb["pr"]["available"] is False and "GITHUB_TOKEN" in cb["pr"]["reason"] and cb["pr"]["url"] is None
    links = {x["label"]: x["url"] for x in d["observability_links"]}
    assert "Bad" not in links  # a template that renders to a non-http URL is dropped
    start = int((now - timedelta(minutes=15)).timestamp() * 1000)
    assert links["Logs"].startswith("https://dd.example/logs?q=service:pay%20api&f=")
    assert abs(int(links["Logs"].split("f=")[1].split("&")[0]) - start) < 60_000
    assert links["Alert"] == f"https://dd.example/a?k=A-{row['short_id']}&s=pay%20api"


async def test_render_links_encodes_and_rejects_non_http():
    alert_row = {
        "raw_event": {"labels": {"service": "a&b=c d"}, "u": "ftp://x"},
        "first_seen_at": datetime(2026, 1, 1, tzinfo=UTC),
        "last_seen_at": datetime(2026, 1, 1, 0, 1, tzinfo=UTC),
        "short_id": 5,
        "fields": {},
        "source_name": "src",
    }
    obs = {
        "links": [
            {"label": "L", "url": "https://x.test/?s={{labels.service}}"},
            {"label": "F", "url": "ftp://x"},
        ]
    }
    (link,) = render_links(obs, alert_row)
    assert link["url"] == "https://x.test/?s=a%26b%3Dc%20d"
    assert render_links(None, alert_row) == []


def test_pure_helpers():
    assert github_slug("https://github.com/o/n.git") == ("o", "n")
    assert github_slug("https://gitlab.com/o/n") is None
    assert (
        web_url({"repo": "https://github.com/o/n", "branch": "dev", "path": ""})
        == "https://github.com/o/n/tree/dev"
    )
    assert (
        clone_cmd({"repo": "https://github.com/o/n", "path": ""})
        == "git clone https://github.com/o/n && cd n"
    )
    assert claude_cmd('say "hi" $x `y`') == 'claude "say \\"hi\\" \\$x \\`y\\`"'


async def test_pr_flow_idempotent_and_recorded(client, seeded, linked, monkeypatch):
    fake = await _gh(monkeypatch)
    row = await alert(seeded, title="Pay / API: 500s!")
    r = await client.post(f"/api/alerts/{row['short_id']}/codebase/pr", headers=as_person("anna"))
    assert r.status_code == 200, r.text
    n = row["short_id"]
    branch = f"herald/a-{n}-pay-api-500s"
    assert r.json() == {"url": "https://github.com/o/n/pull/7", "number": 7, "branch": branch}

    (_, p1, _), (_, p2, b2), (_, p3, b3), (_, p4, b4) = fake.calls
    assert p1 == "/repos/o/n/git/ref/heads/main"
    assert p2 == "/repos/o/n/git/refs" and b2 == {"ref": f"refs/heads/{branch}", "sha": "abc123"}
    assert p3 == f"/repos/o/n/contents/demo/payments-api/.herald/A-{n}.md" and b3["branch"] == branch
    brief = base64.b64decode(b3["content"]).decode()
    assert f"/#alert-{n}" in brief and "Pay / API: 500s!" in brief
    assert p4 == "/repos/o/n/pulls"
    assert b4["draft"] is True and b4["base"] == "main" and b4["head"] == branch
    assert b4["title"] == f"A-{n}: Pay / API: 500s! (draft fix)"
    assert "Opened from Herald by" in b4["body"]

    again = await client.post(f"/api/alerts/{n}/codebase/pr", headers=as_person("anna"))
    assert again.status_code == 200 and again.json()["url"] == "https://github.com/o/n/pull/7"
    assert len(fake.calls) == 4  # nothing more sent to GitHub

    d = await _detail(client, row)
    assert d["codebase"]["pr"] == {"available": True, "url": "https://github.com/o/n/pull/7", "reason": None}
    entry = next(t for t in d["timeline"] if t["action"] == "pr_opened")
    assert entry["detail"]["number"] == 7 and "Draft pull request opened by" in entry["detail"]["text"]
    assert (
        await seeded.fetchval(
            "SELECT count(*) FROM audit_log WHERE action = 'alert.pr_opened' AND target_id = $1",
            str(row["id"]),
        )
        == 1
    )


async def test_pr_conflicts_and_errors(client, seeded, linked, monkeypatch):
    row = await alert(seeded, title="No token")
    monkeypatch.setattr(get_settings(), "github_token", "")
    r = await client.post(f"/api/alerts/{row['short_id']}/codebase/pr", headers=as_person("anna"))
    assert r.status_code == 409 and "GITHUB_TOKEN" in r.json()["detail"]

    await seeded.execute(
        "UPDATE sources SET codebase = $2 WHERE id = $1",
        h.SOURCE_ID,
        {**REPO, "repo": "https://gitlab.com/o/n"},
    )
    await _gh(monkeypatch)
    r = await client.post(f"/api/alerts/{row['short_id']}/codebase/pr", headers=as_person("anna"))
    assert r.status_code == 409 and "GitHub" in r.json()["detail"]

    await seeded.execute("UPDATE sources SET codebase = $2 WHERE id = $1", h.SOURCE_ID, REPO)
    fake = await _gh(monkeypatch)
    fake.fail_on = "/git/refs"
    r = await client.post(f"/api/alerts/{row['short_id']}/codebase/pr", headers=as_person("anna"))
    assert r.status_code == 502 and "Reference already exists" in r.json()["detail"]
    assert "tok-123" not in r.text
    assert (
        await seeded.fetchval(
            "SELECT count(*) FROM timeline WHERE alert_id = $1 AND type = 'pr_opened'", row["id"]
        )
        == 0
    )


async def test_pr_unreachable_github_is_502(client, seeded, linked, monkeypatch):
    await _gh(monkeypatch)

    def boom() -> httpx.AsyncClient:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("down")

        return httpx.AsyncClient(base_url="https://api.github.test", transport=httpx.MockTransport(handler))

    monkeypatch.setattr(github, "make_client", boom)
    row = await alert(seeded, title="Unreachable")
    r = await client.post(f"/api/alerts/{row['short_id']}/codebase/pr", headers=as_person("anna"))
    assert r.status_code == 502


async def test_pr_without_codebase_and_scope(client, seeded, linked, monkeypatch):
    await _gh(monkeypatch)
    row = await alert(seeded, title="Anna only")
    other = await client.post(f"/api/alerts/{row['short_id']}/codebase/pr", headers=as_person("piotr"))
    assert other.status_code == 404
    unknown = await client.post("/api/alerts/999999999/codebase/pr", headers=as_person("anna"))
    assert unknown.status_code == 404
    assert (await client.post("/api/alerts/nope/codebase/pr", headers=as_person("anna"))).status_code == 404

    await seeded.execute("UPDATE sources SET codebase = NULL WHERE id = $1", h.SOURCE_ID)
    none = await client.post(f"/api/alerts/{row['short_id']}/codebase/pr", headers=as_person("anna"))
    assert none.status_code == 409


async def test_platform_put_codebase_validation_and_removal(client, seeded):
    original = await seeded.fetchrow("SELECT codebase, observability FROM sources WHERE name = 'prometheus'")
    base = {
        "codebase": REPO,
        "observability": {"links": [{"label": "L", "url": "https://x.test/{{service}}"}]},
    }
    r = await client.put("/api/platforms/prometheus", json=base, headers=MASTER)
    assert r.status_code == 200, r.text
    assert r.json()["codebase"]["repo"] == "https://github.com/o/n"
    listed = {p["id"]: p for p in (await client.get("/api/platforms", headers=MASTER)).json()}
    assert listed["prometheus"]["observability"]["links"][0]["label"] == "L"

    for bad in (
        {"codebase": {**REPO, "repo": "http://github.com/o/n"}},
        {"codebase": {**REPO, "repo": "git@github.com:o/n.git"}},
        {"codebase": {**REPO, "path": "../etc"}},
        {"observability": {"links": [{"label": "x", "url": "javascript:alert(1)"}]}},
    ):
        assert (await client.put("/api/platforms/prometheus", json=bad, headers=MASTER)).status_code == 422

    off = await client.put("/api/platforms/prometheus", json={"codebase": None}, headers=MASTER)
    assert off.status_code == 200 and off.json()["codebase"] is None
    assert off.json()["observability"] is not None  # untouched
    assert (
        await client.put("/api/platforms/prometheus", json={"name": "x"}, headers=MASTER)
    ).status_code == 409
    await seeded.execute(
        "UPDATE sources SET codebase = $1, observability = $2 WHERE name = 'prometheus'",
        original["codebase"],
        original["observability"],
    )


async def test_platform_put_needs_edit_rights(client, seeded):
    denied = await client.put("/api/platforms/pricing-logs", json={"codebase": REPO}, headers=ANNA)
    assert denied.status_code in (403, 404)
    assert (
        await client.put("/api/platforms/nope-nope", json={"codebase": REPO}, headers=ANNA)
    ).status_code == 422
