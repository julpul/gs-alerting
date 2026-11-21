"""Open a draft pull request on GitHub (REST API) carrying the incident brief. The token is never logged."""

from __future__ import annotations

import base64
import re
from typing import Any

import httpx

from app.config import get_settings
from app.core.errors import AppError

TIMEOUT_S = 15.0


def make_client() -> httpx.AsyncClient:
    s = get_settings()
    return httpx.AsyncClient(
        base_url=s.github_api_url.rstrip("/"),
        timeout=TIMEOUT_S,
        headers={
            "Authorization": f"Bearer {s.github_token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "herald",
        },
    )


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
    base: str,
    branch: str,
    file_path: str,
    file_content: str,
    title: str,
    body: str,
) -> dict[str, Any]:
    """Create `branch` from `base`, commit `file_path`, open a draft PR. Returns `{url, number, branch}`."""
    root = f"/repos/{owner}/{repo}"
    async with make_client() as client:
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
