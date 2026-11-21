"""Open a draft pull request on GitHub (REST API) carrying the incident brief. The token is never logged."""

from __future__ import annotations

import base64
import json
import re
from typing import Any
from uuid import UUID

import asyncpg
import httpx

from app.config import get_settings
from app.core.errors import AppError
from app.core.security import decrypt_secret

TIMEOUT_S = 15.0
ORG_WIDE = UUID(int=0)
"""`integrations.domain_id` of an organisation-wide integration (the column is NOT NULL)."""


def make_client(token: str) -> httpx.AsyncClient:
    s = get_settings()
    return httpx.AsyncClient(
        base_url=s.github_api_url.rstrip("/"),
        timeout=TIMEOUT_S,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "herald",
        },
    )


async def token_for(conn: asyncpg.Connection, domain_id: UUID | None) -> str:
    """The GitHub token for a domain: its own integration, else the organisation-wide one, else the
    `GITHUB_TOKEN` env var. Empty string when none is configured."""
    rows = await conn.fetch(
        "SELECT domain_id, config_encrypted FROM integrations WHERE kind = 'github' "
        "AND domain_id = ANY($1) AND config_encrypted IS NOT NULL",
        [d for d in (domain_id, ORG_WIDE) if d is not None],
    )
    by_domain = {r["domain_id"]: r["config_encrypted"] for r in rows}
    for key in (domain_id, ORG_WIDE):
        blob = by_domain.get(key)
        if blob is None:
            continue
        try:
            token = json.loads(decrypt_secret(blob)).get("token")
        except Exception:
            token = None
        if token:
            return str(token)
    return get_settings().github_token


async def verify_token(token: str, repo: str | None = None) -> str:
    """Check a token against GitHub (`GET /user`, and `GET /repos/{repo}` when given); returns the login."""
    async with make_client(token) as client:
        try:
            user = await client.get("/user")
            if user.status_code in (401, 403):
                raise AppError(422, "invalid_token", "GitHub rejected this token")
            if user.status_code >= 300:
                raise AppError(502, "bad_gateway", f"GitHub answered HTTP {user.status_code}")
            if repo:
                found = await client.get(f"/repos/{repo.strip('/')}")
                if found.status_code >= 300:
                    raise AppError(422, "invalid_token", f"This token cannot see the repository {repo}")
        except httpx.HTTPError as exc:
            raise AppError(502, "bad_gateway", f"GitHub is unreachable ({type(exc).__name__})") from exc
    return str(user.json().get("login") or "")


def slugify(title: str, max_len: int = 40) -> str:
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:max_len].strip("-") or "alert"


async def _call(client: httpx.AsyncClient, method: str, url: str, **kw: Any) -> dict[str, Any]:
    try:
        resp = await client.request(method, url, **kw)
    except httpx.HTTPError as exc:
        raise AppError(502, "bad_gateway", f"GitHub is unreachable ({type(exc).__name__})") from exc
    if resp.status_code >= 300:
        try:
            message = str(resp.json().get("message") or resp.text)
        except ValueError:
            message = resp.text
        raise AppError(502, "bad_gateway", f"GitHub: {message[:300]} (HTTP {resp.status_code})")
    return resp.json()


async def open_draft_pr(
    owner: str,
    repo: str,
    *,
    token: str,
    base: str,
    branch: str,
    file_path: str,
    file_content: str,
    title: str,
    body: str,
) -> dict[str, Any]:
    """Create `branch` from `base`, commit `file_path`, open a draft PR. Returns `{url, number, branch}`."""
    root = f"/repos/{owner}/{repo}"
    async with make_client(token) as client:
        ref = await _call(client, "GET", f"{root}/git/ref/heads/{base}")
        await _call(
            client,
            "POST",
            f"{root}/git/refs",
            json={"ref": f"refs/heads/{branch}", "sha": ref["object"]["sha"]},
        )
        await _call(
            client,
            "PUT",
            f"{root}/contents/{file_path}",
            json={
                "message": f"{title}: add incident brief",
                "content": base64.b64encode(file_content.encode()).decode(),
                "branch": branch,
            },
        )
        pr = await _call(
            client,
            "POST",
            f"{root}/pulls",
            json={"title": title, "head": branch, "base": base, "body": body, "draft": True},
        )
    return {"url": pr["html_url"], "number": pr["number"], "branch": branch}
