"""Server-side sign-in sessions of the Herald UI (AUTH_MODE=password|oidc).

The cookie holds a random token; Redis holds `sha256(token) -> {uid, provider, authTime}` with a fixed expiry
(SESSION_TTL_SECONDS, 8 h). Logging out deletes the key. Failed password attempts are counted per
username + IP.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import time
from typing import Any
from uuid import UUID

from fastapi import Request, Response
from redis.asyncio import Redis

from app.config import get_settings
from app.core.redis import rkey

SESSION_COOKIE = "herald_session"
LOGIN_COOKIE = "herald_login"


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def is_https(request: Request) -> bool:
    forwarded = request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower()
    return (
        request.url.scheme == "https"
        or forwarded == "https"
        or get_settings().public_base_url.startswith("https:")
    )


def client_ip(request: Request) -> str:
    if get_settings().trust_forwarded_for:
        forwarded = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
        if forwarded:
            return forwarded
    return request.client.host if request.client else "unknown"


async def open_session(redis: Redis, user_id: UUID, provider: str) -> str:
    """A new session; returns the token for the cookie."""
    token = secrets.token_urlsafe(32)
    value = {"uid": str(user_id), "provider": provider, "authTime": int(time.time())}
    await redis.set(rkey("sess", token_hash(token)), json.dumps(value), ex=get_settings().session_ttl_seconds)
    return token


async def load_session(redis: Redis, token: str | None) -> dict[str, Any] | None:
    if not token:
        return None
    raw = await redis.get(rkey("sess", token_hash(token)))
    return json.loads(raw) if raw else None


async def close_session(redis: Redis, token: str | None) -> dict[str, Any] | None:
    """Ends the session; returns what it held (None when there was none)."""
    session = await load_session(redis, token)
    if token:
        await redis.delete(rkey("sess", token_hash(token)))
    return session


def set_session_cookie(response: Response, request: Request, token: str) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=get_settings().session_ttl_seconds,
        path="/",
        httponly=True,
        samesite="lax",
        secure=is_https(request),
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/")


def _failure_key(username: str, ip: str) -> str:
    return rkey("login-fail", hashlib.sha256(f"{username.lower()}|{ip}".encode()).hexdigest())


async def is_locked(redis: Redis, username: str, ip: str) -> bool:
    count = await redis.get(_failure_key(username, ip))
    return count is not None and int(count) >= get_settings().login_max_failures


async def record_failure(redis: Redis, username: str, ip: str) -> int:
    key = _failure_key(username, ip)
    count = await redis.incr(key)
    if count == 1:
        await redis.expire(key, get_settings().login_failure_window_seconds)
    return int(count)


async def clear_failures(redis: Redis, username: str, ip: str) -> None:
    await redis.delete(_failure_key(username, ip))
