"""OpenID Connect sign-in: authorization code flow with PKCE (Keycloak demo, Entra ID, any provider).

The browser never holds a token. The api exchanges the code itself, checks the ID token's signature against
the provider's published keys (RS256 only) and maps the person to OUR users table (the router is
herald_auth.py); roles and teams stay in our database. State, nonce and PKCE verifier travel
in a short-lived sealed cookie, so a sign-in started by someone else cannot be completed in this browser.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

import httpx
import jwt

from app.config import Settings

LOGIN_COOKIE_SECONDS = 600
CLOCK_LEEWAY_SECONDS = 30
DISCOVERY_TTL_SECONDS = 3600
DEFAULT_SECRETS = {"dev-app-secret-change-me", "dev-jwt-secret-change-me", ""}
_GUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)
_SIGN_IN_PAGES = re.compile(r"^/(login(\.html)?|auth)(/|\?|#|$)")


class AuthError(Exception):
    """Sign-in failed. The message is for the log; the person sees a generic page."""


@dataclass(frozen=True)
class Provider:
    id: str
    label: str
    issuer: str
    client_id: str
    client_secret: str = field(repr=False)
    discovery_url: str


def providers(settings: Settings) -> dict[str, Provider]:
    """Providers with a complete configuration, in the order the sign-in page shows them."""
    found: dict[str, Provider] = {}
    tenant = settings.entra_tenant_id.strip()
    if tenant and settings.entra_client_id and settings.entra_client_secret and _GUID.match(tenant):
        # One tenant only: the multi-tenant endpoints (common, organizations) admit any Microsoft account.
        issuer = f"https://login.microsoftonline.com/{tenant}/v2.0"
        found["microsoft"] = Provider(
            "microsoft",
            "Continue with Microsoft",
            issuer,
            settings.entra_client_id,
            settings.entra_client_secret,
            f"{issuer}/.well-known/openid-configuration",
        )
    issuer = settings.oidc_issuer.strip().rstrip("/")
    if issuer and settings.oidc_client_id and settings.oidc_client_secret:
        found["sso"] = Provider(
            "sso",
            settings.oidc_label,
            issuer,
            settings.oidc_client_id,
            settings.oidc_client_secret,
            settings.oidc_discovery_url or f"{issuer}/.well-known/openid-configuration",
        )
    return found


def config_problems(settings: Settings) -> list[str]:
    """Why a non-demo mode must not start (empty when it may). The api refuses to start otherwise."""
    if settings.auth_mode not in ("demo", "password", "oidc"):
        return ["AUTH_MODE must be demo, password or oidc"]
    if settings.auth_mode == "demo":
        return []
    problems = []
    if settings.app_secret in DEFAULT_SECRETS or len(settings.app_secret) < 32:
        problems.append("APP_SECRET must be set to a random value of at least 32 characters")
    if settings.jwt_secret in DEFAULT_SECRETS:
        problems.append("JWT_SECRET must not be the built-in default")
    if settings.auth_mode == "oidc":
        tenant = settings.entra_tenant_id.strip()
        if tenant and not _GUID.match(tenant):
            problems.append("ENTRA_TENANT_ID must be the tenant's ID (a GUID), not a domain or 'common'")
        if not providers(settings):
            problems.append(
                "no sign-in provider is configured (OIDC_ISSUER/OIDC_CLIENT_ID/OIDC_CLIENT_SECRET or ENTRA_*)"
            )
    return problems


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def pkce_challenge(verifier: str) -> str:
    """RFC 7636, method S256."""
    return _b64url(hashlib.sha256(verifier.encode()).digest())


def safe_next(value: str | None) -> str:
    """Where to land after signing in: a path on this site only, never another site (open redirect)."""
    if not value or not value.startswith("/") or value.startswith("//") or "\\" in value:
        return "/"
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        return "/"
    if _SIGN_IN_PAGES.match(value):
        return "/"
    return value


def seal(secret: str, data: dict[str, Any], now: float | None = None) -> str:
    """A short-lived value the browser carries but cannot change: JSON plus an HMAC over it."""
    body = dict(data, exp=int(now if now is not None else time.time()) + LOGIN_COOKIE_SECONDS)
    payload = _b64url(json.dumps(body, separators=(",", ":")).encode())
    mac = hmac.new(secret.encode(), f"herald-login.{payload}".encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{mac}"


def unseal(secret: str, value: str | None, now: float | None = None) -> dict[str, Any] | None:
    """The data, or None if the value was changed, signed with another secret, or is too old."""
    payload, _, mac = (value or "").rpartition(".")
    if not payload:
        return None
    expected = hmac.new(secret.encode(), f"herald-login.{payload}".encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(mac, expected):
        return None
    try:
        data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except ValueError:
        return None
    if not isinstance(data, dict) or data.get("exp", 0) < (now if now is not None else time.time()):
        return None
    return data


_discovery: dict[str, tuple[float, dict[str, Any]]] = {}
_jwks: dict[str, dict[str, Any]] = {}


def reset_caches() -> None:
    _discovery.clear()
    _jwks.clear()


async def discovery(provider: Provider) -> dict[str, Any]:
    cached = _discovery.get(provider.id)
    if cached and cached[0] > time.monotonic():
        return cached[1]
    async with httpx.AsyncClient(timeout=5) as client:
        response = await client.get(provider.discovery_url)
    if response.status_code != 200:
        raise AuthError(f"{provider.id}: discovery answered {response.status_code}")
    meta = response.json()
    if str(meta.get("issuer", "")).rstrip("/") != provider.issuer:
        raise AuthError(
            f"{provider.id}: discovery names issuer {meta.get('issuer')!r}, expected {provider.issuer!r}"
        )
    _discovery[provider.id] = (time.monotonic() + DISCOVERY_TTL_SECONDS, meta)
    return meta


def start(
    provider: Provider, next_path: str | None, secret: str, reauth: bool = False
) -> tuple[dict[str, str], str]:
    """Fresh state, nonce and PKCE verifier: (query parameters for the provider, sealed cookie value).

    `reauth` asks the provider for credentials again so another person can sign in after a refusal."""
    state, nonce, verifier = secrets.token_urlsafe(24), secrets.token_urlsafe(24), secrets.token_urlsafe(48)
    params = {
        "response_type": "code",
        "client_id": provider.client_id,
        "scope": "openid profile email",
        "state": state,
        "nonce": nonce,
        "code_challenge": pkce_challenge(verifier),
        "code_challenge_method": "S256",
    }
    if reauth:
        params.update(prompt="login", max_age="0")
    sealed = seal(
        secret, {"p": provider.id, "s": state, "n": nonce, "v": verifier, "next": safe_next(next_path)}
    )
    return params, sealed


async def authorization_url(provider: Provider, params: dict[str, str], redirect_uri: str) -> str:
    endpoint = (await discovery(provider))["authorization_endpoint"]
    return endpoint + ("&" if "?" in endpoint else "?") + urlencode({**params, "redirect_uri": redirect_uri})


async def finish(
    provider: Provider, code: str, verifier: str, nonce: str, redirect_uri: str
) -> dict[str, Any]:
    """Exchange the code (PKCE verifier + client secret) and return the verified ID token claims."""
    meta = await discovery(provider)
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.post(
            meta["token_endpoint"],
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "code_verifier": verifier,
                "client_id": provider.client_id,
                "client_secret": provider.client_secret,
            },
        )
    if response.status_code != 200:
        raise AuthError(f"{provider.id}: token endpoint answered {response.status_code}")
    id_token = response.json().get("id_token")
    if not id_token:
        raise AuthError(f"{provider.id}: no ID token in the response")
    kid = jwt.get_unverified_header(id_token).get("kid")
    keys = await _signing_keys(provider, meta["jwks_uri"], kid)
    return verify_id_token(
        id_token, jwks=keys, issuer=provider.issuer, audience=provider.client_id, nonce=nonce
    )


async def _signing_keys(provider: Provider, jwks_uri: str, kid: str | None) -> dict[str, Any]:
    """The provider's keys, fetched again once when a token names a key we have not seen (rotation)."""
    keys = _jwks.get(provider.id)
    if keys is None or not any(k.get("kid") == kid for k in keys.get("keys", [])):
        async with httpx.AsyncClient(timeout=5) as client:
            response = await client.get(jwks_uri)
        if response.status_code != 200:
            raise AuthError(f"{provider.id}: key set answered {response.status_code}")
        keys = _jwks[provider.id] = response.json()
    return keys


def verify_id_token(
    token: str, *, jwks: dict[str, Any], issuer: str, audience: str, nonce: str
) -> dict[str, Any]:
    """Signature (RS256 only, key chosen by `kid`), issuer, audience, expiry and nonce. Raises AuthError."""
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError as error:
        raise AuthError(f"unreadable ID token: {error}") from None
    if header.get("alg") != "RS256":
        raise AuthError(f"ID token signed with {header.get('alg')!r}; only RS256 is accepted")
    key = None
    for candidate in jwks.get("keys", []):
        if candidate.get("kid") == header.get("kid") and candidate.get("use", "sig") == "sig":
            try:
                key = jwt.PyJWK(candidate, algorithm="RS256").key
            except jwt.PyJWTError:
                continue
            break
    if key is None:
        raise AuthError("ID token signed with a key the provider does not publish")
    try:
        claims = jwt.decode(
            token,
            key,
            algorithms=["RS256"],
            audience=audience,
            issuer=issuer,
            leeway=CLOCK_LEEWAY_SECONDS,
            options={"require": ["exp", "iat", "iss", "aud", "sub"]},
        )
    except jwt.PyJWTError as error:
        raise AuthError(f"ID token rejected: {error}") from None
    if not hmac.compare_digest(str(claims.get("nonce", "")), nonce):
        raise AuthError("ID token nonce does not match this sign-in")
    return claims
