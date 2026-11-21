"""Serves the Herald web UI (`backend/static/herald`) from the api at `/`, same origin as `/v1` and `/api`.

The UI is installed as the router's *fallback* (`app.router.default`), not as a route: Starlette calls it
only when no route matched, so it can never shadow `/v1`, `/api`, `/mcp` (appended later by its own mount),
`/docs`, `/metrics`, `/healthz`, ... whatever order the mounts are discovered in. Paths under an API prefix
never reach the files and keep the normal JSON 404.

`/ads` and everything under it (the retired second UI) redirect to `/`: there is one web app, the classic one.

With AUTH_MODE=password|oidc the app pages (`/`, `/lab`) redirect to `/login?next=...` until signed in.

Cache: HTML is `no-store` (a new build shows up on reload), hashed build assets (`name-<hash>.js`) are
immutable for a year, everything else revalidates (`no-cache` + ETag).
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp, Receive, Scope, Send

from app.config import get_settings
from app.herald.session import SESSION_COOKIE, load_session

STATIC_DIR = Path(__file__).resolve().parents[3] / "static" / "herald"

API_PREFIXES = (
    "/v1",
    "/api",
    "/mcp",
    "/ingest",
    "/inbound",
    "/auth",
    "/docs",
    "/redoc",
    "/openapi.json",
    "/metrics",
    "/healthz",
    "/readyz",
)
ALIASES = {"/login": "/login.html", "/lab": "/lab.html"}
RETIRED = "/ads"
PROTECTED_PAGES = ("/", "/index.html", "/lab", "/lab.html")
HASHED = re.compile(r"-[A-Za-z0-9_-]{8,}\.(?:js|css|woff2?|ttf|svg|png|jpg|webp|gif|ico|map)$")

NO_STORE = "no-store"
IMMUTABLE = "public, max-age=31536000, immutable"
REVALIDATE = "no-cache"


def is_api_path(path: str) -> bool:
    return any(path == p or path.startswith(f"{p}/") for p in API_PREFIXES)


def is_retired(path: str) -> bool:
    return path == RETIRED or path.startswith(f"{RETIRED}/")


async def needs_sign_in(scope: Scope, receive: Receive) -> bool:
    """True for the app pages when sign-in is on (AUTH_MODE=password|oidc) and this browser has no session."""
    if get_settings().auth_mode == "demo" or scope.get("path") not in PROTECTED_PAGES:
        return False
    request = Request(scope, receive)
    return await load_session(request.app.state.redis, request.cookies.get(SESSION_COOKIE)) is None


def cache_control(file_path: str) -> str:
    name = os.path.basename(file_path)
    if name.endswith(".html"):
        return NO_STORE
    if f"{os.sep}assets{os.sep}" in file_path and HASHED.search(name):
        return IMMUTABLE
    return REVALIDATE


class HeraldStatic(StaticFiles):
    """`StaticFiles(html=True)` with the cache headers above."""

    def file_response(
        self,
        full_path: str | os.PathLike[str],
        stat_result: os.stat_result,
        scope: Scope,
        status_code: int = 200,
    ) -> Response:
        response = super().file_response(full_path, stat_result, scope, status_code)
        response.headers["Cache-Control"] = cache_control(os.fspath(full_path))
        return response


class UiFallback:
    """Router fallback: API paths get the original 404, everything else is looked up in the UI files."""

    def __init__(self, static: ASGIApp, not_found: ASGIApp) -> None:
        self.static = static
        self.not_found = not_found

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        if scope["type"] != "http" or is_api_path(path):
            await self.not_found(scope, receive, send)
            return
        if is_retired(path):
            await RedirectResponse("/", status_code=302)(scope, receive, send)
            return
        if await needs_sign_in(scope, receive):
            query = scope.get("query_string", b"").decode()
            target = path + (f"?{query}" if query else "")
            await RedirectResponse(f"/login?next={quote(target, safe='')}", status_code=302)(
                scope, receive, send
            )
            return
        if path in ALIASES:
            scope = {**scope, "path": ALIASES[path], "raw_path": ALIASES[path].encode()}
        await self.static(scope, receive, send)


def mount(app: FastAPI, directory: Path = STATIC_DIR) -> None:
    """Install the UI as the fallback of `app.router` (no-op when the files are not there)."""
    if not directory.is_dir():
        return
    static = HeraldStatic(directory=directory, html=True)
    app.router.default = UiFallback(static, app.router.default)
