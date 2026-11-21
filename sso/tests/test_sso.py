"""Vistula ID end to end: authorize -> code -> token (PKCE + client secret) -> verified ID token.

Run from the repo root: `cd backend && uv run pytest ../sso/tests`.
"""

from __future__ import annotations

import base64
import hashlib
import os
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import jwt
import pytest
from fastapi.testclient import TestClient

os.environ.update(
    SSO_ISSUER="http://sso.test",
    SSO_CLIENT_ID="herald",
    SSO_CLIENT_SECRET="s3cret",
    SSO_REDIRECT_URIS="http://app.test/auth/callback",
    SSO_PASSWORD="pw-for-tests",
)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app as sso

REDIRECT = "http://app.test/auth/callback"
VERIFIER = "v" * 50
CHALLENGE = base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()).rstrip(b"=").decode()
PARAMS = {
    "client_id": "herald",
    "redirect_uri": REDIRECT,
    "state": "st",
    "nonce": "nn",
    "code_challenge": CHALLENGE,
    "code_challenge_method": "S256",
    "scope": "openid profile email",
}


@pytest.fixture
def client():
    sso._failures.clear()
    return TestClient(sso.app, base_url="http://sso.internal")


def _login(client, username="anna", password="pw-for-tests"):
    return client.post(
        "/authorize", data={**PARAMS, "username": username, "password": password}, follow_redirects=False
    )


def _code(response) -> str:
    assert response.status_code == 303, response.text
    query = parse_qs(urlparse(response.headers["location"]).query)
    assert query["state"] == ["st"]
    return query["code"][0]


def _token(client, code, verifier=VERIFIER, secret="s3cret"):
    return client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT,
            "code_verifier": verifier,
            "client_id": "herald",
            "client_secret": secret,
        },
    )


def test_discovery_names_public_issuer_and_backchannel_on_the_asked_host(client):
    meta = client.get("/.well-known/openid-configuration").json()
    assert meta["issuer"] == "http://sso.test"
    assert meta["authorization_endpoint"] == "http://sso.test/authorize"
    assert meta["token_endpoint"] == "http://sso.internal/token"
    assert meta["jwks_uri"] == "http://sso.internal/jwks"


def test_full_code_flow_gives_a_verifiable_id_token(client):
    page = client.get("/authorize", params=PARAMS)
    assert page.status_code == 200 and "Sign in" in page.text and "Herald" in page.text
    response = _token(client, _code(_login(client, "anna@demo")))
    assert response.status_code == 200
    id_token = response.json()["id_token"]
    jwk = client.get("/jwks").json()["keys"][0]
    claims = jwt.decode(
        id_token, jwt.PyJWK(jwk).key, algorithms=["RS256"], audience="herald", issuer="http://sso.test"
    )
    assert claims["email"] == "anna@demo" and claims["nonce"] == "nn" and claims["name"] == "Anna Kowalska"
    info = client.get("/userinfo", headers={"Authorization": "Bearer " + response.json()["access_token"]})
    assert info.json()["preferred_username"] == "anna"


def test_code_is_single_use_and_needs_the_right_verifier_and_secret(client):
    code = _code(_login(client))
    assert _token(client, code, secret="wrong").status_code == 401
    assert _token(client, code, verifier="x" * 50).json() == {"error": "invalid_grant"}
    code = _code(_login(client))
    assert _token(client, code).status_code == 200
    assert _token(client, code).json() == {"error": "invalid_grant"}


def test_wrong_password_unknown_redirect_and_lockout(client):
    assert _login(client, password="nope").status_code == 401
    bad = client.get("/authorize", params={**PARAMS, "redirect_uri": "http://evil.test/cb"})
    assert bad.status_code == 400 and "not registered" in bad.text
    no_pkce = client.get("/authorize", params={**PARAMS, "code_challenge_method": "plain"})
    assert no_pkce.status_code == 400
    for _ in range(5):
        _login(client, "piotr", "nope")
    assert _login(client, "piotr").status_code == 429


def test_session_cookie_offers_the_account_picker_and_logout_returns_to_the_app(client):
    _code(_login(client))
    picker = client.get("/authorize", params=PARAMS)
    assert "Pick an account" in picker.text and "Anna Kowalska" in picker.text
    again = client.post("/authorize", data={**PARAMS, "action": "continue"}, follow_redirects=False)
    assert _token(client, _code(again)).status_code == 200
    out = client.get(
        "/logout", params={"post_logout_redirect_uri": "http://app.test/login"}, follow_redirects=False
    )
    assert out.status_code == 303 and out.headers["location"] == "http://app.test/login"
    assert "Sign in" in client.get("/authorize", params=PARAMS).text
    assert client.get("/logout", params={"post_logout_redirect_uri": "http://evil.test/"}).status_code == 200
