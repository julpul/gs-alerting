"""Sign-in of the Herald UI: password mode, OpenID Connect (stubbed IdP, local JWKS), sessions."""

from __future__ import annotations

import time
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
import pytest
import pytest_asyncio
from cryptography.hazmat.primitives.asymmetric import rsa

from app.config import Settings, get_settings
from app.herald import oidc

from .core_helpers import as_person

ISSUER = "http://idp.test/realms/vistula"
CLIENT = "herald"
SECRET = "x" * 40


@pytest.fixture
def settings():
    """The live settings object (other tests clear the cache, so the session-wide fixture can be stale)."""
    return get_settings()


@pytest.fixture
def mode(settings, monkeypatch):
    """`mode("password")` switches the shared settings object for the test."""

    def _set(value: str, **extra) -> None:
        monkeypatch.setattr(settings, "auth_mode", value)
        monkeypatch.setattr(settings, "trust_forwarded_for", True)
        for key, val in extra.items():
            monkeypatch.setattr(settings, key, val)

    return _set


@pytest_asyncio.fixture
async def web(app):
    """A fresh client with its own cookie jar (the shared `client` fixture keeps cookies across tests)."""
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def _login(web, user="anna", password="demo", ip="10.0.0.1", **extra):
    return await web.post(
        "/auth/password",
        json={"username": user, "password": password, **extra},
        headers={"x-forwarded-for": ip},
    )


# ---- password mode -------------------------------------------------------------------------------


async def test_password_login_sets_session_cookie_and_me_works(web, mode):
    mode("password")
    assert (await web.get("/api/me")).status_code == 401
    r = await _login(web, "anna", next="/#alert-1")
    assert r.status_code == 200 and r.json() == {"next": "/#alert-1"}
    cookie = r.headers["set-cookie"].lower()
    assert "herald_session=" in cookie and "httponly" in cookie and "samesite=lax" in cookie
    assert "secure" not in cookie  # plain http in tests
    me = (await web.get("/api/me")).json()
    assert me["mode"] == "password" and me["user"] == "person:anna" and me["role"] == "responder"
    assert me["provider"] == "password" and me["teams"] == ["team-payments"]


async def test_cookie_is_secure_behind_https(web, mode):
    mode("password")
    r = await web.post(
        "/auth/password",
        json={"username": "anna@demo", "password": "demo"},
        headers={"x-forwarded-for": "10.0.0.2", "x-forwarded-proto": "https"},
    )
    assert r.status_code == 200 and "secure" in r.headers["set-cookie"].lower()


async def test_identity_headers_and_bearer_are_ignored_in_password_mode(web, mode, auth_headers):
    mode("password")
    r = await web.get("/api/me", headers=as_person("master"))
    assert r.status_code == 401 and r.json() == {"detail": "sign in required"}
    r = await web.get("/api/me", headers=await auth_headers("master@demo"))
    assert r.status_code == 401
    await _login(web, "anna", ip="10.0.0.3")
    me = (await web.get("/api/me", headers=as_person("master", **{"X-Herald-Role": "admin"}))).json()
    assert me["user"] == "person:anna" and me["role"] == "responder"
    assert (await web.get("/api/directory", headers=as_person("master"))).status_code == 200


async def test_v1_bearer_auth_is_unchanged_in_password_mode(client, mode, auth_headers):
    mode("password")
    r = await client.get("/v1/auth/me", headers=await auth_headers("anna@demo"))
    assert r.status_code == 200 and r.json()["user"]["email"] == "anna@demo"


async def test_wrong_password_and_unknown_user_are_401(web, mode):
    mode("password")
    for user, password in (("anna", "nope"), ("ghost", "demo")):
        r = await _login(web, user, password, ip="10.0.0.4")
        assert r.status_code == 401 and r.json() == {"detail": "invalid username or password"}
    assert "set-cookie" not in r.headers


async def test_failed_attempts_are_rate_limited_per_username_and_ip(web, mode, pool):
    mode("password", login_max_failures=3)
    for _ in range(3):
        assert (await _login(web, "piotr", "bad", ip="10.9.9.9")).status_code == 401
    locked = await _login(web, "piotr", "demo", ip="10.9.9.9")  # even the right password waits
    assert locked.status_code == 429
    assert (await _login(web, "piotr", "demo", ip="10.9.9.8")).status_code == 200  # other address unaffected
    assert await pool.fetchval(
        "SELECT count(*) FROM audit_log WHERE action = 'login_locked' AND actor->>'name' = 'piotr'"
    )


async def test_logout_ends_session_and_is_audited(web, mode, pool):
    mode("password")
    await _login(web, "manager", ip="10.0.0.5")
    assert (await web.get("/api/me")).status_code == 200
    r = await web.post("/auth/logout")
    assert r.status_code == 200 and r.json() == {"logout_url": "/login"}
    assert (await web.get("/api/me")).status_code == 401
    actions = {
        row["action"]
        for row in await pool.fetch(
            "SELECT action FROM audit_log WHERE actor->>'name' = 'Manager' OR target_id <> ''"
        )
    }
    assert {"login", "logout"} <= actions
    assert (await web.post("/auth/logout")).status_code == 200  # idempotent


async def test_session_expires(web, mode, redis):
    mode("password")
    await _login(web, "anna", ip="10.0.0.6")
    token = web.cookies["herald_session"]
    from app.core.redis import rkey
    from app.herald.session import token_hash

    assert 0 < await redis.ttl(rkey("sess", token_hash(token))) <= 8 * 3600
    await redis.delete(rkey("sess", token_hash(token)))
    assert (await web.get("/api/me")).status_code == 401


async def test_app_pages_redirect_to_login_until_signed_in(web, mode):
    mode("password")
    r = await web.get("/", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/login?next=%2F"
    assert (await web.get("/login.html")).status_code == 200
    await _login(web, "anna", ip="10.0.0.7")
    assert (await web.get("/", follow_redirects=False)).status_code == 200


async def test_providers_per_mode(web, mode):
    assert (await web.get("/auth/providers")).status_code == 404  # demo
    assert (
        await web.post("/auth/password", json={"username": "anna", "password": "demo"})
    ).status_code == 404
    mode("password")
    assert (await web.get("/auth/providers")).json() == [
        {"id": "password", "label": "Username and password", "type": "password"}
    ]
    assert (await web.get("/api/meta")).json()["auth"] == {"mode": "password", "sso": False}


async def test_demo_mode_is_untouched(web):
    assert (await web.get("/api/me", headers=as_person("anna"))).json()["mode"] == "demo"
    assert (await web.get("/", follow_redirects=False)).status_code == 200
    assert (await web.get("/auth/login", follow_redirects=False)).headers["location"] == "/login"


# ---- OpenID Connect --------------------------------------------------------------------------------


def _key(kid: str):
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = jwt.algorithms.RSAAlgorithm.to_jwk(private.public_key(), as_dict=True)
    return private, dict(public, kid=kid, use="sig", alg="RS256")


@pytest.fixture(scope="module")
def keys():
    private, public = _key("k1")
    forged, _ = _key("k1")
    return private, {"keys": [public]}, forged


class Idp:
    """The identity provider as seen over HTTP: discovery, keys, token endpoint."""

    def __init__(self, private, jwks):
        self.private, self.jwks = private, jwks
        self.claims: dict = {}
        self.token_status = 200
        self.seen_token_form: dict = {}

    def id_token(self, key=None, **overrides) -> str:
        now = int(time.time())
        claims = {
            "iss": ISSUER,
            "aud": CLIENT,
            "sub": "u1",
            "iat": now,
            "exp": now + 300,
            **self.claims,
            **overrides,
        }
        claims = {k: v for k, v in claims.items() if v is not None}
        return jwt.encode(claims, key or self.private, algorithm="RS256", headers={"kid": "k1"})

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/.well-known/openid-configuration"):
            return httpx.Response(
                200,
                json={
                    "issuer": ISSUER,
                    "authorization_endpoint": "http://idp.test/auth",
                    "token_endpoint": "http://idp.test/token",
                    "jwks_uri": "http://idp.test/certs",
                    "end_session_endpoint": "http://idp.test/logout",
                },
            )
        if path == "/certs":
            return httpx.Response(200, json=self.jwks)
        if path == "/token":
            self.seen_token_form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            if self.token_status != 200:
                return httpx.Response(self.token_status, json={"error": "invalid_grant"})
            return httpx.Response(200, json={"id_token": self.id_token()})
        return httpx.Response(404)


@pytest.fixture
def idp(keys, mode, monkeypatch):
    private, jwks, _ = keys
    provider = Idp(private, jwks)
    real = httpx.AsyncClient

    def fake_client(*args, **kwargs):
        return real(*args, transport=httpx.MockTransport(provider.handler), **kwargs)

    monkeypatch.setattr(oidc.httpx, "AsyncClient", fake_client)
    oidc.reset_caches()
    mode(
        "oidc",
        oidc_issuer=ISSUER,
        oidc_client_id=CLIENT,
        oidc_client_secret=SECRET,
        oidc_discovery_url=f"{ISSUER}/.well-known/openid-configuration",
    )
    yield provider
    oidc.reset_caches()


async def _start(web, **query):
    r = await web.get("/auth/login", params={"provider": "sso", **query}, follow_redirects=False)
    assert r.status_code == 302, r.text
    return r, {k: v[0] for k, v in parse_qs(urlsplit(r.headers["location"]).query).items()}


async def test_oidc_providers_lists_sso_and_password(web, idp):
    listed = (await web.get("/auth/providers")).json()
    assert listed[0] == {"id": "sso", "label": "Continue with SSO", "type": "oidc"}
    assert listed[-1]["type"] == "password"


async def test_oidc_start_redirects_with_pkce_state_and_nonce(web, idp):
    r, q = await _start(web, next="/#alert-5")
    assert r.headers["location"].startswith("http://idp.test/auth?")
    assert q["response_type"] == "code" and q["client_id"] == CLIENT and q["scope"] == "openid profile email"
    assert q["code_challenge_method"] == "S256" and q["redirect_uri"].endswith("/auth/callback")
    assert q["state"] and q["nonce"] and "prompt" not in q
    cookie = r.headers["set-cookie"].lower()
    assert "herald_login=" in cookie and "httponly" in cookie and "path=/auth" in cookie
    flow = oidc.unseal("test-app-secret", web.cookies["herald_login"])  # settings.app_secret in tests
    assert oidc.pkce_challenge(flow["v"]) == q["code_challenge"] and flow["s"] == q["state"]
    _, again = await _start(web, reauth="1")
    assert again["prompt"] == "login" and again["state"] != q["state"]


async def _callback(web, idp, q, **claims):
    idp.claims = {"nonce": q["nonce"], **claims}
    return await web.get(
        "/auth/callback", params={"code": "abc", "state": q["state"]}, follow_redirects=False
    )


async def test_oidc_callback_with_valid_token_creates_session(web, idp, pool):
    _, q = await _start(web, next="/#alert-5")
    r = await _callback(web, idp, q, preferred_username="anna", email="anna@demo")
    assert r.status_code == 302 and r.headers["location"] == "/#alert-5"
    assert idp.seen_token_form["code_verifier"] and idp.seen_token_form["client_secret"] == SECRET
    me = (await web.get("/api/me")).json()
    assert me["mode"] == "oidc" and me["user"] == "person:anna" and me["provider"] == "sso"
    assert "herald_login" not in web.cookies
    assert await pool.fetchval(
        "SELECT count(*) FROM audit_log WHERE action = 'login' AND data->>'provider' = 'sso'"
    )


async def test_oidc_matches_by_username_when_no_email_claim(web, idp):
    _, q = await _start(web)
    r = await _callback(web, idp, q, preferred_username="Piotr")
    assert r.status_code == 302
    assert (await web.get("/api/me")).json()["user"] == "person:piotr"


async def test_oidc_unknown_user_is_refused_and_audited(web, idp, pool):
    _, q = await _start(web)
    r = await _callback(web, idp, q, preferred_username="mallory", email="mallory@evil.example")
    assert r.status_code == 302 and r.headers["location"] == "/login?error=no_access"
    assert (await web.get("/api/me")).status_code == 401
    row = await pool.fetchrow(
        "SELECT actor, data FROM audit_log WHERE action = 'login_denied' ORDER BY id DESC"
    )
    assert row["actor"]["name"] == "mallory"


async def test_oidc_callback_rejects_bad_state_cookie_signature_and_provider_errors(web, idp, keys):
    _, q = await _start(web)
    bad_state = await web.get(
        "/auth/callback", params={"code": "abc", "state": "other"}, follow_redirects=False
    )
    assert bad_state.headers["location"] == "/login?error=login_failed"
    idp.claims = {"nonce": q["nonce"], "preferred_username": "anna"}
    _, _, forged = keys
    monkey_token = idp.id_token(key=forged)
    idp.id_token = lambda *a, **k: monkey_token  # token signed by a key the provider does not publish
    _, q = await _start(web)
    r = await web.get("/auth/callback", params={"code": "abc", "state": q["state"]}, follow_redirects=False)
    assert r.headers["location"] == "/login?error=login_failed"
    assert (await web.get("/api/me")).status_code == 401
    r = await web.get(
        "/auth/callback", params={"error": "access_denied", "state": q["state"]}, follow_redirects=False
    )
    assert r.headers["location"] == "/login?error=login_failed"
    web.cookies.clear()  # no login cookie: a callback somebody else started
    r = await web.get("/auth/callback", params={"code": "abc", "state": q["state"]}, follow_redirects=False)
    assert r.headers["location"] == "/login?error=login_failed"


async def test_oidc_token_endpoint_failure_is_login_failed(web, idp):
    _, q = await _start(web)
    idp.token_status = 400
    r = await _callback(web, idp, q, preferred_username="anna")
    assert r.headers["location"] == "/login?error=login_failed"


async def test_oidc_wrong_nonce_is_refused(web, idp):
    _, q = await _start(web)
    idp.claims = {"nonce": "replayed", "preferred_username": "anna"}
    r = await web.get("/auth/callback", params={"code": "abc", "state": q["state"]}, follow_redirects=False)
    assert r.headers["location"] == "/login?error=login_failed"


async def test_oidc_next_is_same_origin_only(web, idp):
    for evil in ("https://evil.example/", "//evil.example", "/\\evil.example", "/login", "/auth/login"):
        _, q = await _start(web, next=evil)
        r = await _callback(web, idp, q, preferred_username="anna")
        assert r.headers["location"] == "/", evil


async def test_oidc_password_form_still_works_and_logout_goes_to_provider(web, idp):
    assert (await _login(web, "anna", ip="10.0.1.1")).status_code == 200
    assert (await web.post("/auth/logout")).json() == {
        "logout_url": "/login"
    }  # password session: nothing at the IdP
    _, q = await _start(web)
    await _callback(web, idp, q, preferred_username="anna")
    url = (await web.post("/auth/logout")).json()["logout_url"]
    assert (
        url.startswith("http://idp.test/logout?")
        and "client_id=herald" in url
        and "post_logout_redirect_uri" in url
    )


async def test_oidc_login_without_provider_config_fails_closed(web, idp, settings, monkeypatch):
    monkeypatch.setattr(settings, "oidc_issuer", "")
    r = await web.get("/auth/login", follow_redirects=False)
    assert r.headers["location"] == "/login?error=login_failed"


async def test_oidc_discovery_failure_is_login_failed(web, idp, monkeypatch):
    oidc.reset_caches()
    monkeypatch.setattr(idp, "handler", lambda request: httpx.Response(503))
    r = await web.get("/auth/login", params={"provider": "sso"}, follow_redirects=False)
    assert r.headers["location"] == "/login?error=login_failed"


# ---- startup checks and small pieces ---------------------------------------------------------------


def _settings(**kw) -> Settings:
    return Settings(_env_file=None, **kw)


def test_demo_mode_has_no_startup_problems():
    assert oidc.config_problems(_settings()) == []


def test_oidc_refuses_to_start_with_default_secret_or_no_provider():
    problems = oidc.config_problems(
        _settings(
            auth_mode="oidc", app_secret="dev-app-secret-change-me", jwt_secret="dev-jwt-secret-change-me"
        )
    )
    assert any("APP_SECRET" in p for p in problems) and any("JWT_SECRET" in p for p in problems)
    assert any("no sign-in provider" in p for p in problems)
    ok = _settings(
        auth_mode="oidc",
        app_secret=SECRET,
        jwt_secret="y" * 40,
        oidc_issuer=ISSUER,
        oidc_client_id=CLIENT,
        oidc_client_secret=SECRET,
    )
    assert oidc.config_problems(ok) == []
    assert any(
        "ENTRA_TENANT_ID" in p
        for p in oidc.config_problems(_settings(auth_mode="oidc", entra_tenant_id="common"))
    )


def test_password_mode_needs_real_secrets_but_no_provider():
    assert oidc.config_problems(_settings(auth_mode="password", app_secret="short"))
    assert oidc.config_problems(_settings(auth_mode="password", app_secret=SECRET, jwt_secret="y" * 40)) == []
    assert oidc.config_problems(_settings(auth_mode="magic")) == ["AUTH_MODE must be demo, password or oidc"]


def test_create_app_refuses_to_start_when_problems(monkeypatch, settings):
    from app.api.app import create_app

    monkeypatch.setattr(settings, "auth_mode", "oidc")
    with pytest.raises(RuntimeError, match="refusing to start"):
        create_app()


def test_entra_provider_is_single_tenant():
    tenant = "11111111-2222-3333-4444-555555555555"
    found = oidc.providers(_settings(entra_tenant_id=tenant, entra_client_id="c", entra_client_secret="s"))
    assert found["microsoft"].issuer == f"https://login.microsoftonline.com/{tenant}/v2.0"
    assert (
        oidc.providers(_settings(entra_tenant_id="common", entra_client_id="c", entra_client_secret="s"))
        == {}
    )


def test_sealed_values_cannot_be_changed_or_reused_after_expiry():
    value = oidc.seal(SECRET, {"a": 1}, now=1000)
    assert oidc.unseal(SECRET, value, now=1001)["a"] == 1
    assert oidc.unseal("other-secret", value, now=1001) is None
    assert oidc.unseal(SECRET, value, now=1000 + oidc.LOGIN_COOKIE_SECONDS + 1) is None
    assert oidc.unseal(SECRET, "garbage", now=1001) is None and oidc.unseal(SECRET, None) is None
    payload, _, mac = value.rpartition(".")
    assert oidc.unseal(SECRET, f"{payload}x.{mac}", now=1001) is None


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("/#alert-1", "/#alert-1"),
        ("/lab?x=1", "/lab?x=1"),
        (None, "/"),
        ("", "/"),
        ("https://evil.example", "/"),
        ("//evil.example", "/"),
        ("/\\evil", "/"),
        ("/a\nb", "/"),
        ("/login.html", "/"),
        ("/auth/callback", "/"),
        ("/loginx", "/loginx"),
    ],
)
def test_safe_next(given, expected):
    assert oidc.safe_next(given) == expected
