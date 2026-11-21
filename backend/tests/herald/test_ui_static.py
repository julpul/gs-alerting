"""The Herald web UI is served by the api at `/` without shadowing any API route."""

from __future__ import annotations

import os

import httpx
import pytest
from fastapi import FastAPI

from app.api.mounts import herald_ui
from app.api.mounts.herald_ui import IMMUTABLE, NO_STORE, REVALIDATE, cache_control, is_api_path, is_retired

pytestmark = pytest.mark.asyncio(loop_scope="session")

PAGES = ("index.html", "lab.html", "login.html")


async def test_root_serves_classic_ui(client):
    resp = await client.get("/")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert resp.headers["cache-control"] == NO_STORE
    assert "The Herald" in resp.text


@pytest.mark.parametrize("path", ["/ads", "/ads/", "/ads/index.html", "/ads/assets/index-abc12345.js"])
async def test_retired_second_ui_redirects_home(client, path):
    resp = await client.get(path)
    assert resp.status_code == 302
    assert resp.headers["location"] == "/"


async def test_no_page_links_to_the_retired_ui():
    for name in PAGES:
        assert "/ads" not in (herald_ui.STATIC_DIR / name).read_text(), name
    assert not (herald_ui.STATIC_DIR / "ads").exists()


@pytest.mark.parametrize("path", ["/lab.html", "/login.html", "/login", "/flow/demo.html"])
async def test_other_pages(client, path):
    resp = await client.get(path)
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")


async def test_unhashed_files_revalidate(client):
    resp = await client.get("/brand/" + next((herald_ui.STATIC_DIR / "brand").iterdir()).name)
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == REVALIDATE


async def test_health_and_api_routes_not_shadowed(client, auth_headers):
    assert (await client.get("/healthz")).json() == {"status": "ok"}
    assert (await client.get("/openapi.json")).json()["info"]["title"]
    assert "text/plain" in (await client.get("/metrics")).headers["content-type"]
    me = await client.get("/v1/auth/me", headers=await auth_headers("anna@demo"))
    assert me.status_code == 200 and me.json()["user"]["email"] == "anna@demo"
    assert (await client.get("/docs")).status_code == 200


async def test_unknown_api_path_is_json_not_static(client):
    """`/api/me` before the BFF lands (or any unknown API path) is the JSON 404, never a UI file."""
    for path in ("/api/me", "/api/definitely-missing", "/v1/nope", "/mcp/x"):
        resp = await client.get(path)
        assert "text/html" not in resp.headers.get("content-type", ""), path
        if resp.status_code == 404:
            assert resp.headers["content-type"].startswith("application/json")


async def test_mcp_route_still_reachable(client):
    resp = await client.post("/mcp", json={})
    assert resp.status_code == 401  # PAT middleware answered, not the static files


async def test_missing_ui_file_is_404(client):
    resp = await client.get("/no-such-page.html")
    assert resp.status_code == 404


async def test_post_to_ui_path_is_405(client):
    assert (await client.post("/index.html")).status_code == 405


async def test_is_api_path():
    assert is_api_path("/v1") and is_api_path("/v1/alerts") and is_api_path("/api/me")
    assert is_api_path("/openapi.json") and is_api_path("/healthz")
    assert not is_api_path("/") and not is_api_path("/ads/") and not is_api_path("/apis.html")
    assert not is_api_path("/v1x")
    assert is_retired("/ads") and is_retired("/ads/x") and not is_retired("/adsx")


async def test_cache_control_rules():
    sep = os.sep
    assert cache_control(f"{sep}x{sep}index.html") == NO_STORE
    assert cache_control(f"{sep}x{sep}assets{sep}index-KI_Hairz.js") == IMMUTABLE
    assert cache_control(f"{sep}x{sep}assets{sep}plain.js") == REVALIDATE
    assert cache_control(f"{sep}x{sep}flow{sep}herald-flow.js") == REVALIDATE


async def test_mount_is_noop_without_files(tmp_path):
    app = FastAPI()
    before = app.router.default
    herald_ui.mount(app, tmp_path / "missing")
    assert app.router.default is before


async def test_fallback_on_bare_app(tmp_path):
    """Works on any app regardless of route order: routes added after the mount still win."""
    (tmp_path / "index.html").write_text("<p>ui</p>")
    app = FastAPI()
    herald_ui.mount(app, tmp_path)

    @app.get("/api/late")
    async def late() -> dict[str, bool]:
        return {"late": True}

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        assert (await c.get("/")).text == "<p>ui</p>"
        assert (await c.get("/api/late")).json() == {"late": True}
        assert (await c.get("/api/other")).status_code == 404


async def test_escalation_flows_page_edits_the_real_policy():
    html = (herald_ui.STATIC_DIR / "index.html").read_text()
    assert "/v1/escalation-policies" in html and "method:'PUT'" in html
    assert "localStorage.getItem('herald.flows')" not in html
    assert "The same flow in words" not in html
    assert "A drawing board, saved in this browser" not in html
