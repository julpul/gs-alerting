"""Vistula ID: a small OpenID Connect provider that plays the company's single sign-on in the demo.

Real protocol, toy directory: authorization code + PKCE (S256), RS256-signed ID tokens, discovery, JWKS,
userinfo and end-session. Herald talks to it exactly like to Entra ID or Okta, so replacing it is config
(OIDC_ISSUER / OIDC_CLIENT_ID / OIDC_CLIENT_SECRET on the api).

The directory is `users.json`; every account shares one demo password (SSO_PASSWORD, else DEMO_PASSWORD).
Browsers use the public issuer URL; servers may call the back-channel endpoints on any host name (the
discovery document names them on the host that was asked), so `http://sso:8180` works inside docker.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlparse

import jwt
import page
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

ISSUER = os.environ.get("SSO_ISSUER", "http://localhost:8180").rstrip("/")
CLIENT_ID = os.environ.get("SSO_CLIENT_ID") or os.environ.get("OIDC_CLIENT_ID") or "herald"
CLIENT_SECRET = os.environ.get("SSO_CLIENT_SECRET") or os.environ.get("OIDC_CLIENT_SECRET") or ""
CLIENT_NAME = os.environ.get("SSO_CLIENT_NAME", "Herald")
REDIRECT_URIS = [u.strip() for u in os.environ.get("SSO_REDIRECT_URIS", "").split(",") if u.strip()]
PASSWORD = os.environ.get("SSO_PASSWORD") or os.environ.get("DEMO_PASSWORD") or "demo"
SESSION_SECRET = (os.environ.get("SSO_SESSION_SECRET") or secrets.token_hex(32)).encode()
SESSION_COOKIE = "vid_session"
SESSION_SECONDS = 8 * 3600
CODE_SECONDS = 60
TOKEN_SECONDS = 300
MAX_FAILURES = 5

USERS: dict[str, dict[str, str]] = {
    u["username"]: u for u in json.loads((Path(__file__).parent / "users.json").read_text())["users"]
}


def _load_key() -> rsa.RSAPrivateKey:
    pem = os.environ.get("SSO_PRIVATE_KEY", "").replace("\\n", "\n").strip()
    if pem:
        key = serialization.load_pem_private_key(pem.encode(), password=None)
        assert isinstance(key, rsa.RSAPrivateKey)
        return key
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


KEY = _load_key()
_pub = KEY.public_key().public_numbers()
KID = hashlib.sha256(str(_pub.n).encode()).hexdigest()[:16]


def _b64(n: int) -> str:
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


JWKS = {
    "keys": [{"kty": "RSA", "use": "sig", "alg": "RS256", "kid": KID, "n": _b64(_pub.n), "e": _b64(_pub.e)}]
}

_codes: dict[str, dict[str, Any]] = {}
_access: dict[str, tuple[float, str]] = {}
_failures: dict[str, list[float]] = {}

app = FastAPI(title="Vistula ID", docs_url=None, redoc_url=None, openapi_url=None)


# ---- helpers -------------------------------------------------------------------------------------


def _find_user(login: str) -> dict[str, str] | None:
    name = login.strip().lower()
    return USERS.get(name.split("@", 1)[0]) if name else None


def _sign_session(username: str) -> str:
    exp = str(int(time.time()) + SESSION_SECONDS)
    body = f"{username}|{exp}"
    mac = hmac.new(SESSION_SECRET, body.encode(), hashlib.sha256).hexdigest()
    return f"{body}|{mac}"


def _session_user(request: Request) -> dict[str, str] | None:
    raw = request.cookies.get(SESSION_COOKIE, "")
    parts = raw.split("|")
    if len(parts) != 3:
        return None
    username, exp, mac = parts
    good = hmac.new(SESSION_SECRET, f"{username}|{exp}".encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(mac, good) or int(exp) < time.time():
        return None
    return USERS.get(username)


def _redirect_ok(uri: str) -> bool:
    return uri in REDIRECT_URIS if REDIRECT_URIS else uri.startswith(("http://localhost", "http://127.0.0.1"))


def _origin_ok(uri: str) -> bool:
    origins = {f"{urlparse(u).scheme}://{urlparse(u).netloc}" for u in REDIRECT_URIS}
    target = urlparse(uri)
    return f"{target.scheme}://{target.netloc}" in origins if origins else False


def _locked(key: str) -> bool:
    now = time.time()
    recent = [t for t in _failures.get(key, []) if now - t < 60]
    _failures[key] = recent
    return len(recent) >= MAX_FAILURES


def _issue_code(params: dict[str, str], user: dict[str, str]) -> RedirectResponse:
    code = secrets.token_urlsafe(32)
    _codes[code] = {**params, "user": user["username"], "exp": time.time() + CODE_SECONDS}
    query = urlencode({"code": code, "state": params.get("state", "")})
    response = RedirectResponse(f"{params['redirect_uri']}?{query}", status_code=303)
    response.set_cookie(
        SESSION_COOKIE,
        _sign_session(user["username"]),
        max_age=SESSION_SECONDS,
        httponly=True,
        samesite="lax",
        secure=ISSUER.startswith("https://"),
    )
    return response


AUTH_PARAMS = (
    "client_id",
    "redirect_uri",
    "state",
    "nonce",
    "code_challenge",
    "code_challenge_method",
    "scope",
)


def _check_request(params: dict[str, str]) -> str | None:
    if params.get("client_id") != CLIENT_ID:
        return "This application is not registered with Vistula ID."
    if not _redirect_ok(params.get("redirect_uri", "")):
        return "The address to return to is not registered for this application."
    if params.get("code_challenge_method") != "S256" or not params.get("code_challenge"):
        return "The application must use PKCE (S256)."
    return None


# ---- endpoints -----------------------------------------------------------------------------------


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/.well-known/openid-configuration")
def discovery(request: Request) -> dict[str, Any]:
    back = str(request.base_url).rstrip("/")
    return {
        "issuer": ISSUER,
        "authorization_endpoint": f"{ISSUER}/authorize",
        "end_session_endpoint": f"{ISSUER}/logout",
        "token_endpoint": f"{back}/token",
        "jwks_uri": f"{back}/jwks",
        "userinfo_endpoint": f"{back}/userinfo",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code"],
        "subject_types_supported": ["public"],
        "id_token_signing_alg_values_supported": ["RS256"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["client_secret_post", "client_secret_basic"],
        "scopes_supported": ["openid", "profile", "email"],
        "claims_supported": ["sub", "email", "name", "preferred_username", "department"],
    }


@app.get("/jwks")
def jwks() -> dict[str, Any]:
    return JWKS


@app.get("/authorize", response_class=HTMLResponse)
def authorize(request: Request) -> Response:
    params = {k: v for k, v in request.query_params.items() if k in AUTH_PARAMS}
    problem = _check_request(params)
    if problem:
        return HTMLResponse(page.error(problem), status_code=400)
    user = _session_user(request)
    if user and request.query_params.get("prompt") != "login":
        return HTMLResponse(page.picker(params, user, CLIENT_NAME))
    hint = request.query_params.get("login_hint", "")
    return HTMLResponse(page.login(params, CLIENT_NAME, username=hint))


@app.post("/authorize", response_class=HTMLResponse)
async def authorize_submit(request: Request) -> Response:
    form = {k: str(v) for k, v in (await request.form()).items()}
    params = {k: form.get(k, "") for k in AUTH_PARAMS if form.get(k)}
    problem = _check_request(params)
    if problem:
        return HTMLResponse(page.error(problem), status_code=400)
    if form.get("action") == "continue":
        user = _session_user(request)
        if user:
            return _issue_code(params, user)
        return HTMLResponse(page.login(params, CLIENT_NAME))
    username = form.get("username", "")
    key = f"{username.lower()}|{request.client.host if request.client else ''}"
    if _locked(key):
        return HTMLResponse(
            page.login(params, CLIENT_NAME, username, "Too many attempts. Wait a minute and try again."), 429
        )
    user = _find_user(username)
    if user is None or not hmac.compare_digest(form.get("password", ""), PASSWORD):
        _failures.setdefault(key, []).append(time.time())
        return HTMLResponse(
            page.login(params, CLIENT_NAME, username, "That username and password don't match."), 401
        )
    return _issue_code(params, user)


def _client_auth(request: Request, form: dict[str, str]) -> tuple[str, str]:
    header = request.headers.get("authorization", "")
    if header.lower().startswith("basic "):
        cid, _, secret = base64.b64decode(header[6:]).decode().partition(":")
        return cid, secret
    return form.get("client_id", ""), form.get("client_secret", "")


def _token_error(error: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"error": error}, status_code=status)


@app.post("/token")
async def token(request: Request) -> JSONResponse:
    form = {k: str(v) for k, v in (await request.form()).items()}
    cid, secret = _client_auth(request, form)
    if cid != CLIENT_ID or not CLIENT_SECRET or not hmac.compare_digest(secret, CLIENT_SECRET):
        return _token_error("invalid_client", 401)
    if form.get("grant_type") != "authorization_code":
        return _token_error("unsupported_grant_type")
    grant = _codes.pop(form.get("code", ""), None)
    if grant is None or grant["exp"] < time.time() or grant["client_id"] != cid:
        return _token_error("invalid_grant")
    if form.get("redirect_uri") != grant["redirect_uri"]:
        return _token_error("invalid_grant")
    verifier = form.get("code_verifier", "")
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    if not hmac.compare_digest(challenge, grant["code_challenge"]):
        return _token_error("invalid_grant")
    user = USERS[grant["user"]]
    now = int(time.time())
    claims = {
        "iss": ISSUER,
        "sub": user["username"],
        "aud": cid,
        "iat": now,
        "exp": now + TOKEN_SECONDS,
        "auth_time": now,
        "email": user["email"],
        "email_verified": True,
        "preferred_username": user["username"],
        "name": user["name"],
        "department": user.get("department", ""),
    }
    if grant.get("nonce"):
        claims["nonce"] = grant["nonce"]
    id_token = jwt.encode(claims, KEY, algorithm="RS256", headers={"kid": KID})
    access = secrets.token_urlsafe(32)
    _access[access] = (now + TOKEN_SECONDS, user["username"])
    return JSONResponse(
        {"access_token": access, "token_type": "Bearer", "expires_in": TOKEN_SECONDS, "id_token": id_token},
        headers={"Cache-Control": "no-store"},
    )


@app.get("/userinfo")
def userinfo(request: Request) -> JSONResponse:
    header = request.headers.get("authorization", "")
    held = _access.get(header[7:]) if header.lower().startswith("bearer ") else None
    if held is None or held[0] < time.time():
        return JSONResponse({"error": "invalid_token"}, status_code=401)
    user = USERS[held[1]]
    return JSONResponse(
        {
            "sub": user["username"],
            "email": user["email"],
            "name": user["name"],
            "preferred_username": user["username"],
        }
    )


@app.get("/logout", response_class=HTMLResponse)
def logout(request: Request) -> Response:
    target = request.query_params.get("post_logout_redirect_uri", "")
    response: Response = (
        RedirectResponse(target, status_code=303)
        if target and _origin_ok(target)
        else HTMLResponse(page.signed_out())
    )
    response.delete_cookie(SESSION_COOKIE)
    return response


@app.get("/", response_class=HTMLResponse)
def home(request: Request) -> Response:
    user = _session_user(request)
    return HTMLResponse(page.home(user, len(USERS)))
