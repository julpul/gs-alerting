"""HTTP helpers shared by the built-in actions."""

from __future__ import annotations

import ipaddress
from typing import Any
from urllib.parse import SplitResult, quote, urlsplit

import httpx

from app.config import get_settings
from app.runbooks.actions.registry import ActionResult

TIMEOUT_S = 10.0


def gateway(path: str) -> str:
    return f"{get_settings().mockgw_url.rstrip('/')}{path}"


def segment(value: object) -> str:
    """One URL path segment built from untrusted text."""
    return quote(str(value), safe="")


def validate_external_url(url: str) -> SplitResult:
    """http(s) only; no loopback / link-local / unspecified / multicast addresses (SSRF guard)."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError(f"not an http(s) URL: {url!r}")
    host = parts.hostname.lower()
    if host == "localhost":
        raise ValueError("localhost is not allowed")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return parts
    if ip.is_loopback or ip.is_link_local or ip.is_unspecified or ip.is_multicast:
        raise ValueError(f"address {host} is not allowed")
    return parts


async def call(method: str, url: str, **kwargs: Any) -> ActionResult:
    """One request; 2xx is ok, anything else (and network errors) a failed result with the reason."""
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
            resp = await client.request(method, url, **kwargs)
    except httpx.HTTPError as exc:
        return ActionResult(False, f"{method} {url} failed: {type(exc).__name__}: {exc}"[:500])
    body = resp.text[:300].strip()
    return ActionResult(
        resp.is_success,
        f"{method} {url} -> HTTP {resp.status_code}" + (f": {body}" if body else ""),
        {"status": resp.status_code},
    )
