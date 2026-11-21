"""Sign-in of the Herald web UI: `/auth/providers`, `/auth/login` + `/auth/callback` (OpenID Connect),
`/auth/password` and `/auth/logout`. Only active with AUTH_MODE=password|oidc (404 / redirect in demo mode).

Identity is always OUR `users` row: an SSO person is matched by e-mail / `preferred_username`;
roles and teams are read from our database. Whoever signs in at the provider but is not in `users`
is refused and audited."""

from __future__ import annotations

import asyncio
import hmac
import logging
from typing import Any

import asyncpg
import httpx
from fastapi import Query, Request
from pydantic import BaseModel
from starlette.responses import JSONResponse, RedirectResponse, Response

from app.api.deps import Pool
from app.config import get_settings
from app.core.audit import SYSTEM_ACTOR, audit, user_actor
from app.core.errors import AppError
from app.core.security import hash_password, verify_password
from app.herald import oidc, session
from app.herald.routing import herald_router

router = herald_router(prefix="/auth", tags=["herald-auth"])
log = logging.getLogger(__name__)

_DUMMY_HASH: str | None = None


def _redirect_uri() -> str:
    return f"{get_settings().public_base_url.rstrip('/')}/auth/callback"


def _denied(error: str) -> RedirectResponse:
    response = RedirectResponse(f"/login?error={error}", status_code=302)
    response.delete_cookie(session.LOGIN_COOKIE, path="/auth")
    return response


def _require_mode(*modes: str) -> None:
    if get_settings().auth_mode not in modes:
        raise AppError(404, "not_found", "sign-in is not enabled")


@router.get("/providers")
async def auth_providers() -> list[dict[str, str]]:
    """What the sign-in page offers: SSO buttons (oidc mode) and the password form (both modes)."""
    _require_mode("password", "oidc")
    settings = get_settings()
    buttons = []
    if settings.auth_mode == "oidc":
        buttons = [{"id": p.id, "label": p.label, "type": "oidc"} for p in oidc.providers(settings).values()]
    return [*buttons, {"id": "password", "label": "Username and password", "type": "password"}]


@router.get("/login")
async def auth_login(
    provider: str | None = None, next_path: str = Query("/", alias="next"), reauth: bool = False
) -> RedirectResponse:
    """Start single sign-on: Authorization Code + PKCE, with state/nonce sealed into a cookie."""
    settings = get_settings()
    if settings.auth_mode != "oidc":
        return RedirectResponse("/login", status_code=302)
    available = oidc.providers(settings)
    chosen = available.get(provider or "") or next(iter(available.values()), None)
    if chosen is None:
        return _denied("login_failed")
    params, sealed = oidc.start(chosen, next_path, settings.app_secret, reauth=reauth)
    try:
        url = await oidc.authorization_url(chosen, params, _redirect_uri())
    except (oidc.AuthError, httpx.HTTPError, KeyError, ValueError) as error:
        log.warning("sign-in could not start: %s", error)
        return _denied("login_failed")
    response = RedirectResponse(url, status_code=302)
    response.set_cookie(
        session.LOGIN_COOKIE,
        sealed,
        max_age=oidc.LOGIN_COOKIE_SECONDS,
        path="/auth",
        httponly=True,
        samesite="lax",
        secure=settings.public_base_url.startswith("https:"),
    )
    return response


async def _find_user(conn: asyncpg.Connection, claims: dict[str, Any]) -> asyncpg.Record | None:
    for claim in ("email", "preferred_username"):
        value = str(claims.get(claim) or "").strip().lower()
        if not value:
            continue
        column = "lower(email)" if "@" in value else "split_part(lower(email), '@', 1)"
        row = await conn.fetchrow(f"SELECT id, email, display_name FROM users WHERE {column} = $1", value)
        if row is not None:
            return row
    return None


@router.get("/callback")
async def auth_callback(
    request: Request, pool: Pool, code: str = "", state: str = "", error: str = ""
) -> Response:
    settings = get_settings()
    flow = oidc.unseal(settings.app_secret, request.cookies.get(session.LOGIN_COOKIE))
    provider = oidc.providers(settings).get(flow["p"]) if flow and settings.auth_mode == "oidc" else None
    if flow is None or provider is None or error or not code or not hmac.compare_digest(state, flow["s"]):
        return _denied("login_failed")
    try:
        claims = await oidc.finish(provider, code, flow["v"], flow["n"], _redirect_uri())
    except (oidc.AuthError, httpx.HTTPError, KeyError, ValueError) as failure:
        log.warning("sign-in failed: %s", failure)
        return _denied("login_failed")
    username = str(claims.get("preferred_username") or claims.get("email") or claims["sub"])
    async with pool.acquire() as conn:
        user = await _find_user(conn, claims)
        if user is None:
            actor = {**SYSTEM_ACTOR, "name": username}
            await audit(conn, actor, "login_denied", "session", username, {"provider": provider.id})
            return _denied("no_access")
        await audit(
            conn,
            user_actor(user["id"], user["display_name"]),
            "login",
            "session",
            str(user["id"]),
            {"provider": provider.id},
        )
    redis = request.app.state.redis
    await session.close_session(redis, request.cookies.get(session.SESSION_COOKIE))
    token = await session.open_session(redis, user["id"], "sso")
    response = RedirectResponse(oidc.safe_next(flow.get("next")), status_code=302)
    session.set_session_cookie(response, request, token)
    response.delete_cookie(session.LOGIN_COOKIE, path="/auth")
    return response


class PasswordLogin(BaseModel):
    username: str
    password: str
    next: str | None = None


async def _check_password(conn: asyncpg.Connection, username: str, password: str) -> asyncpg.Record | None:
    """The user row when the password is right. Unknown users cost the same time (no probing)."""
    global _DUMMY_HASH
    name = username.strip().lower()
    column = "lower(email)" if "@" in name else "split_part(lower(email), '@', 1)"
    row = await conn.fetchrow(
        f"SELECT id, email, display_name, password_hash FROM users WHERE {column} = $1", name
    )
    if row is None:
        _DUMMY_HASH = _DUMMY_HASH or hash_password("dummy")
        await asyncio.to_thread(verify_password, password, _DUMMY_HASH)
        return None
    ok = await asyncio.to_thread(verify_password, password, row["password_hash"])
    return row if ok else None


@router.post("/password")
async def auth_password(body: PasswordLogin, request: Request, pool: Pool) -> JSONResponse:
    _require_mode("password", "oidc")
    redis = request.app.state.redis
    username, ip = body.username.strip().lower(), session.client_ip(request)
    if await session.is_locked(redis, username, ip):
        raise AppError(429, "too_many_attempts", "too many attempts, try again later")
    async with pool.acquire() as conn:
        user = await _check_password(conn, username, body.password)
        if user is None:
            count = await session.record_failure(redis, username, ip)
            if count == get_settings().login_max_failures:
                await audit(
                    conn, {**SYSTEM_ACTOR, "name": username}, "login_locked", "session", username, {"ip": ip}
                )
            raise AppError(401, "invalid_credentials", "invalid username or password")
        await audit(
            conn,
            user_actor(user["id"], user["display_name"]),
            "login",
            "session",
            str(user["id"]),
            {"provider": "password", "ip": ip},
        )
    await session.clear_failures(redis, username, ip)
    await session.close_session(redis, request.cookies.get(session.SESSION_COOKIE))
    token = await session.open_session(redis, user["id"], "password")
    response = JSONResponse({"next": oidc.safe_next(body.next)})
    session.set_session_cookie(response, request, token)
    return response


@router.post("/logout")
async def auth_logout(request: Request, pool: Pool) -> JSONResponse:
    """Ends the session here, then (SSO only) tells the browser where to end the provider's session too."""
    held = await session.close_session(request.app.state.redis, request.cookies.get(session.SESSION_COOKIE))
    logout_url = "/login"
    if held is not None:
        async with pool.acquire() as conn:
            name = await conn.fetchval("SELECT display_name FROM users WHERE id = $1::uuid", held["uid"])
            await audit(
                conn,
                user_actor(held["uid"], name or ""),
                "logout",
                "session",
                held["uid"],
                {"provider": held["provider"]},
            )
        logout_url = await _provider_logout_url(held["provider"])
    response = JSONResponse({"logout_url": logout_url})
    session.clear_session_cookie(response)
    return response


async def _provider_logout_url(provider_kind: str) -> str:
    settings = get_settings()
    if provider_kind != "sso" or settings.auth_mode != "oidc":
        return "/login"
    for provider in oidc.providers(settings).values():
        try:
            end = (await oidc.discovery(provider)).get("end_session_endpoint")
        except (oidc.AuthError, httpx.HTTPError, ValueError):
            continue
        if end:
            query = httpx.QueryParams(
                {
                    "client_id": provider.client_id,
                    "post_logout_redirect_uri": f"{settings.public_base_url.rstrip('/')}/login",
                }
            )
            return f"{end}{'&' if '?' in end else '?'}{query}"
    return "/login"
